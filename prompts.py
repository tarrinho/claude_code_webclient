# prompts.py — answer a terminal session's interactive prompt from the browser.
#
# A question asked by an interactive Claude Code session cannot be answered
# through any of the obvious channels: AskUserQuestion is absent from the tool
# list of every --print turn, a cross-session message is not processed while the
# session is blocked inside the prompt, dev.tty.legacy_tiocsti is 0 on this
# kernel so keystroke injection via the tty is refused, and the session's stdin
# is a tty rather than a pipe.
#
# What does work is the terminal multiplexer hosting the session. GNU screen's
# `stuff` and tmux's `send-keys` both write into a window's input queue, which
# is indistinguishable from typing. This module finds the window a session lives
# in and delivers a keystroke to it.
#
# Everything here is best-effort and read-mostly: a session that is not hosted
# by a multiplexer simply has no target, and the caller reports that rather than
# pretending the answer was delivered.
from __future__ import annotations

import json
import os
import re
import shutil

# Fixed argv only, never a shell -- see _run.
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import Any, Final

_SESSIONS_DIR: Final[Path] = Path.home() / ".claude" / "sessions"
_CMD_TIMEOUT_S: Final[float] = 5.0
# Enough to cover a prompt plus the surrounding turn; hardcopy is a whole screen.
_SNAPSHOT_MAX: Final[int] = 20000

# Upper bound on a request typed into a terminal from the web. Reached only by
# refusing the send outright -- never by truncating, which is what this used to
# do: a prompt over the limit was silently cut and typed anyway, Enter was
# pressed on the fragment, and the caller was told the delivery succeeded. From
# the web UI that looked like a message that vanished (2026-09-14, reported as
# "I've sent several messages and I don't receive any reply"), and the server
# log said "prompt delivered to live terminal" for every one of them.
_TEXT_MAX: Final[int] = 25000

# One `screen -X stuff` / `tmux send-keys` call carries at most this much.
# Longer text is sent as several calls in order, then one Enter -- the limit
# here is the multiplexer's own argument handling, not anything about a prompt,
# so it stays well under any plausible ARG_MAX rather than close to it.
_STUFF_CHUNK: Final[int] = 900

# Bracketed paste. A multi-call send arrives at the TUI as thousands of
# individual keystrokes spread over the time the calls take, so anything the
# person at that terminal types lands *between* them -- measured 2026-09-15,
# a 24,080-character delivery interleaved with typing and one message was
# stored as "reply with just the number\rAdd this test to the test batter",
# the \r being this module's own Enter caught mid-word. Wrapping the payload
# tells the TUI to take everything between the markers as one paste rather
# than as typing, which is what the sequence exists for.
#
# Verified rather than assumed: `screen -X stuff` passes the escape bytes
# through unaltered (probed against `cat -A`, which showed
# ^[[200~HELLO PASTE^[[201~). Applied only when the text needs more than one
# call -- a short prompt already arrives atomically, and leaving it alone
# keeps the common path byte-identical to what it has always sent.
_PASTE_START: Final[str] = "\x1b[200~"
_PASTE_END: Final[str] = "\x1b[201~"

# The frame Claude draws around the command a prompt is asking about. Used as a
# stop when reading the question text upwards, so the framed command does not
# get read back as the question.
_BOX_CHARS: Final[str] = "│─╭╮╰╯├┤┌┐└┘┃━"
_PROMPT_TEXT_MAX_LINES: Final[int] = 4
_PROMPT_CACHE_TTL_S: Final[float] = 3.0
_PROMPT_CACHE_MAX: Final[int] = 256
# session_id -> (monotonic time of the capture, whether a prompt was showing)
_prompt_cache: dict[str, tuple[float, bool]] = {}
_PERMISSION_RE: Final[re.Pattern[str]] = re.compile(
    r"Permission rule|requires confirmation|wants to (?:run|use|edit|create)",
    re.IGNORECASE,
)

# Keystrokes we are willing to deliver. A prompt is answered by moving a
# selection and confirming it, so nothing else needs to be expressible, and
# refusing the rest keeps this from becoming a general remote-typing hole.
_KEYS: Final[dict[str, str]] = {
    "enter": "\r",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "escape": "\x1b",
}
# Option numbers a prompt may offer. Bounded so a caller cannot send arbitrary
# text through the "digit" path.
_DIGITS: Final[frozenset[str]] = frozenset("123456789")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a fixed argv, returning None if the binary is missing or hangs."""
    if not shutil.which(argv[0]):
        return None
    try:
        # argv is a fixed list and shell is False, so nothing is interpreted.
        return subprocess.run(  # nosec B603
            argv, capture_output=True, text=True, timeout=_CMD_TIMEOUT_S, check=False
        )
    except (subprocess.SubprocessError, OSError):
        return None


def _cmdline_argv0(pid: int) -> str:
    """The first argument of *pid*'s command line, or "" if unreadable."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8")
    except OSError:
        return ""
    return cmdline.split("\0", 1)[0]


def _is_claude(pid: int) -> bool:
    """Whether *pid* is a Claude Code process.

    The session file only claims a pid; this is the kernel's opinion of what
    that pid actually is. Without it a file naming any live pid would be enough
    to aim keystrokes at that process's terminal.

    ``comm`` alone stopped being enough once the CLI started installing each
    version as ``~/.local/share/claude/versions/<version>`` and running that
    path directly: the kernel's short name for the process is then the bare
    version string (``2.1.260``), which contains no "claude" at all, and every
    live session on this host failed this check at once -- silently, since a
    failure here reads as "no pending prompt" rather than an error. `cmdline`'s
    first argument is still the full launch path, which does contain it.
    """
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if "claude" in comm.lower():
        return True
    return "claude" in _cmdline_argv0(pid).lower()


def _self_and_ancestors() -> set[int]:
    """This process and everything that launched it.

    ``db.write_claude_session_file`` records ``os.getpid()`` -- the WebConsole
    server -- for sessions created through the web. Walking up from that pid
    reaches the shell and multiplexer window the server was launched from, so
    the whole ancestry has to be refused, not merely the exact pid.
    """
    return {pid for pid, _name in _parents(os.getpid(), limit=40)}


def session_pid(session_id: str) -> int | None:
    """Return the OS pid running *session_id*, or None if it cannot be trusted.

    Claude writes ~/.claude/sessions/<pid>.json holding the sessionId, so the
    mapping exists on disk -- but that file is writable by any process running
    as this user, which includes every agent this console spawns with
    --dangerously-skip-permissions. It is therefore a claim to be checked, not
    an authority:

    * the pid must exist and identify as claude (``_is_claude``);
    * it must not be this process or any ancestor of it, because a web-created
      session file names the server itself;
    * two live pids claiming the same session is a contradiction, and a
      contradiction is refused rather than resolved -- picking one would let a
      planted file win a race against the real entry.

    What this does *not* fix: a file naming a genuinely live, unrelated claude
    process still passes every check above, because the mapping fundamentally
    lives in a file an agent may write. Narrowing who can be nominated is all
    that is available while the agents share this account -- see F-02 in
    docs/threat-model.md.
    """
    if not session_id:
        return None
    try:
        entries = sorted(_SESSIONS_DIR.glob("*.json"))
    except OSError:
        return None

    forbidden = _self_and_ancestors()
    found: set[int] = set()
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        # The filename is the pid for live sessions; files named by session id
        # instead carry it inside. Both are claims, so both get checked.
        for candidate in (path.stem, data.get("pid")):
            try:
                pid = int(candidate)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if pid in forbidden or not _is_claude(pid):
                continue
            found.add(pid)

    if len(found) != 1:
        return None
    return found.pop()


def _parents(pid: int, limit: int = 12) -> list[tuple[int, str]]:
    """Walk up the process tree, returning (pid, comm) pairs."""
    chain: list[tuple[int, str]] = []
    current = pid
    for _ in range(limit):
        try:
            stat = Path(f"/proc/{current}/status").read_text(encoding="utf-8")
        except OSError:
            break
        name = ""
        parent = None
        for line in stat.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("PPid:"):
                try:
                    parent = int(line.split(":", 1)[1].strip())
                except ValueError:
                    parent = None
        chain.append((current, name))
        if not parent or parent <= 1:
            break
        current = parent
    return chain


def _screen_sessions() -> list[str]:
    """Return screen session names, newest listing first."""
    result = _run(["screen", "-ls"])
    if result is None:
        return []
    # `screen -ls` exits non-zero when listing, so stdout is what matters.
    return re.findall(r"^\s*(\d+\.\S+)", result.stdout, flags=re.MULTILINE)


def _screen_windows(session: str) -> list[str]:
    result = _run(["screen", "-S", session, "-Q", "windows"])
    if result is None or not result.stdout:
        return []
    # e.g. "0* bash  2- bash" -> ["0", "2"]
    return re.findall(r"(?:^|\s)(\d+)[*\-$@ ]", result.stdout + " ")


def screen_snapshot(session: str, window: str) -> str:
    """Return what a screen window is currently displaying, or ""."""
    # screen writes the dump itself, so it must be a path screen can reach.
    out = Path(f"/tmp/wc-prompt-{session}-{window}.hardcopy")  # nosec B108
    try:
        out.unlink(missing_ok=True)
    except OSError:
        return ""
    if _run(["screen", "-S", session, "-p", window, "-X", "hardcopy", str(out)]) is None:
        return ""
    # hardcopy is asynchronous; the file appears a moment later.
    for _ in range(10):
        try:
            if out.exists() and out.stat().st_size:
                return out.read_text(encoding="utf-8", errors="replace")[:_SNAPSHOT_MAX]
        except OSError:
            return ""
        time.sleep(0.05)
    return ""


def _tmux_panes() -> list[str]:
    result = _run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"])
    if result is None or not result.stdout:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def refresh(target: dict[str, Any]) -> str:
    """Re-read what *target* is displaying."""
    if target.get("kind") == "screen":
        return screen_snapshot(str(target.get("session")), str(target.get("window")))
    if target.get("kind") == "tmux":
        result = _run(["tmux", "capture-pane", "-p", "-t", str(target.get("window"))])
        return result.stdout if result else ""
    return ""


def _environ(pid: int) -> dict[str, str]:
    """Read a process's environment, or {} if it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" not in chunk:
            continue
        key, _, value = chunk.partition(b"=")
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _window_of(pid: int) -> dict[str, str] | None:
    """The multiplexer window *pid* runs in, read from the process environment."""
    for candidate, _name in _parents(pid):
        env = _environ(candidate)
        pane = env.get("TMUX_PANE")
        if pane:
            return {"kind": "tmux", "session": env.get("TMUX", ""), "window": pane}
        sty, window = env.get("STY"), env.get("WINDOW")
        if sty and window and window.isdigit():
            return {"kind": "screen", "session": sty, "window": window}
    return None


def _same_window(a: dict[str, Any], b: dict[str, str] | None) -> bool:
    if not b:
        return False
    return (
        a.get("kind") == b.get("kind")
        and a.get("session") == b.get("session")
        and a.get("window") == b.get("window")
    )


def locate(session_id: str) -> dict[str, Any] | None:
    """Identify the multiplexer window a session runs in, from its own process.

    screen exports STY (session) and WINDOW (index) into every window, and tmux
    exports TMUX_PANE, so the window is stated by the environment rather than
    inferred. This replaced matching on screen contents, which was actively
    dangerous: two sessions in the same screen session were distinguished only
    by what was on their screens, and quoting one session's question inside
    another's output was enough to select the wrong window -- observed, not
    hypothetical.

    ``shares_server_window`` is reported rather than refused, and the
    distinction is worth stating because the obvious guard is wrong. This
    console runs inside screen on the deployment it was written for -- STY
    2126909.pts-5, window 0 -- and an interactive agent runs in that same
    window. Refusing our own window therefore refuses a legitimate target, not
    an attack: it is where the foreground agent actually is.

    What closes the "session file names the server" hole is the ancestry check
    in :func:`session_pid`, which rejects our own pid and everything above it.
    Once that holds, a resolved pid in our window is a *different*, live,
    verified claude process -- which is exactly the case the feature exists to
    serve. So the flag travels to the caller, and a caller about to type free
    text may decline it; nothing is silently dropped.
    """
    pid = session_pid(session_id)
    if not pid:
        return None
    target = _window_of(pid)
    if target is None:
        return None
    target["shares_server_window"] = _same_window(target, _window_of(os.getpid()))
    return target


def looks_like_a_prompt(snapshot: str) -> bool:
    """Whether *snapshot* shows a selectable prompt awaiting an answer.

    Delivering navigation keys to a window that is not prompting would type
    into whatever it is doing instead, so identity is not enough on its own --
    the window has to be visibly asking something.
    """
    if not snapshot:
        return False
    has_options = bool(re.search(r"^\s*[❯>]?\s*\d+\.\s+\S", snapshot, re.MULTILINE))
    hints = ("Enter to select", "to navigate", "Esc to cancel")
    return has_options and any(hint in snapshot for hint in hints)


def find_target(session_id: str, needle: str = "") -> dict[str, Any] | None:
    """Return the window hosting *session_id*, if it is showing a prompt.

    *needle* is an extra confirmation, not the means of selection: the window
    comes from the process environment, and the needle merely checks that the
    prompt on screen is the question we think we are answering.
    """
    target = locate(session_id)
    if target is None:
        return None
    snapshot = refresh(target)
    if not looks_like_a_prompt(snapshot):
        return None
    if needle and needle.strip()[:60] not in snapshot:
        return None
    return {**target, "snapshot": snapshot}


def deliver(target: dict[str, Any], key: str) -> bool:
    """Send one keystroke to *target*. Returns whether it was accepted.

    *key* is a name from _KEYS or a single digit; anything else is refused, so
    this cannot be used to type arbitrary text into somebody's terminal.
    """
    if not isinstance(target, dict):
        return False
    if key in _DIGITS:
        payload = key
    elif key in _KEYS:
        payload = _KEYS[key]
    else:
        return False

    if target.get("kind") == "screen":
        session, window = target.get("session"), target.get("window")
        if not session or window is None:
            return False
        result = _run(
            ["screen", "-S", str(session), "-p", str(window), "-X", "stuff", payload]
        )
        return bool(result and result.returncode == 0)
    if target.get("kind") == "tmux":
        pane = target.get("window")
        if not pane:
            return False
        # -l sends the payload literally rather than as a key name.
        result = _run(["tmux", "send-keys", "-t", str(pane), "-l", payload])
        return bool(result and result.returncode == 0)
    return False


def send_text(
    target: dict[str, Any], text: str, progress: dict[str, Any] | None = None,
) -> bool:
    """Type *text* into *target*'s input queue and press Enter.

    Deliberately separate from deliver(), which stays restricted to a fixed key
    set so the prompt-answering path cannot be turned into remote typing. This
    one IS remote typing, and exists for a narrower reason: a request made in
    the web UI for a conversation that is linked to a live terminal session
    should reach that session, so the user watches the request and every step
    of the answer in the window they are looking at.

    Without it a web turn spawns `claude --resume` as a second process against
    the same transcript -- the work happens correctly and invisibly, in a
    window nobody is watching, while two processes append to one file.

    Control characters are stripped: a payload carrying its own newlines or
    escapes could submit more than the one request the caller intended.

    Text longer than one multiplexer call can carry is typed as several calls
    in order (_STUFF_CHUNK each) followed by a single Enter. Enter is sent only
    once every chunk has been accepted: a half-typed request must never be
    submitted, so a chunk failing part-way leaves the fragment sitting unsent
    in the window -- visible to whoever is watching it -- and returns False so
    the caller falls back to a headless turn rather than believing this worked.

    Over _TEXT_MAX the send is refused outright and nothing is typed at all.
    Truncating instead (what this did until 2026-09-14) is the one behaviour
    that must not come back: it submitted a fragment while reporting success.

    *progress*, when given, is filled in with ``chunks_total``/``chunks_sent``
    and a ``refused`` reason, so a caller reporting a failure can say how much
    reached the window. A bare "the terminal refused the input" does not
    distinguish nothing-was-typed from most-of-it-was, and those want different
    responses from whoever is watching the window.
    """
    def _note(**fields: Any) -> None:
        if progress is not None:
            progress.update(fields)

    if not isinstance(target, dict) or not isinstance(text, str):
        _note(refused="bad target or text")
        return False
    cleaned = "".join(ch for ch in text if ch == " " or (ch.isprintable() and ch != "\x7f"))
    cleaned = cleaned.strip()
    if not cleaned:
        _note(refused="empty after sanitising")
        return False
    if len(cleaned) > _TEXT_MAX:
        _note(refused=f"{len(cleaned)} chars exceeds the {_TEXT_MAX} limit")
        return False

    chunks = [
        cleaned[i:i + _STUFF_CHUNK] for i in range(0, len(cleaned), _STUFF_CHUNK)
    ]
    # Only a multi-call send needs the paste brackets; see _PASTE_START.
    bracketed = len(chunks) > 1
    if bracketed:
        chunks[0] = _PASTE_START + chunks[0]
        chunks[-1] = chunks[-1] + _PASTE_END
    _note(chunks_total=len(chunks), chunks_sent=0, bracketed=bracketed)

    kind = target.get("kind")
    if kind == "screen":
        session, window = target.get("session"), target.get("window")
        if not session or window is None:
            _note(refused="incomplete screen target")
            return False
        argv_for = lambda chunk: [  # noqa: E731 - one shape per kind, read together
            "screen", "-S", str(session), "-p", str(window), "-X", "stuff", chunk]
    elif kind == "tmux":
        pane = target.get("window")
        if not pane:
            _note(refused="incomplete tmux target")
            return False
        argv_for = lambda chunk: ["tmux", "send-keys", "-t", str(pane), "-l", chunk]  # noqa: E731
    else:
        _note(refused=f"unsupported target kind {kind!r}")
        return False

    # The newline is sent separately, after every chunk has landed, so a
    # failure to type cannot still submit what did land.
    for index, chunk in enumerate(chunks):
        typed = _run(argv_for(chunk))
        if not (typed and typed.returncode == 0):
            # A bracketed send that stops part-way has opened a paste the TUI
            # is still inside: every subsequent keystroke from the person at
            # that terminal would be swallowed into it. Close the bracket so
            # the window is usable again. Enter is still not sent -- the
            # fragment stays visible and unsubmitted, which is the point.
            if bracketed and index > 0:
                _run(argv_for(_PASTE_END))
            _note(
                chunks_sent=index,
                refused=f"chunk {index + 1} of {len(chunks)} was refused",
            )
            return False
    _note(chunks_sent=len(chunks))
    return deliver(target, "enter")


def _server_window() -> tuple[str, str]:
    """The multiplexer window this process is itself running in."""
    import os as _os
    return _os.environ.get("STY", ""), _os.environ.get("WINDOW", "")


def _process_name(pid: int) -> str:
    """The command name of *pid*, or "" if it is gone or unreadable.

    Its own function so the identification guard has a seam a test can hold:
    patching pathlib globally reached other readers and let the guard's test
    pass down an exception path instead of exercising the check.
    """
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _is_claude_process(pid: int | None) -> bool:
    """Whether *pid* is a running claude, and not this process or an ancestor.

    locate() derives a window from a pid taken out of
    ~/.claude/sessions/<id>.json. For a web-created session that pid is the
    WebConsole's own, written by db.write_claude_session_file -- so the window
    it resolves to is the server's. Typing into that window types at whatever
    is there, and if it is a shell then Enter runs it. An authenticated web
    request must never become a shell command, so the pid has to be positively
    identified before its environment is trusted.
    """
    import os as _os
    if not pid or pid <= 0:
        return False
    if pid == _os.getpid():
        return False
    for _ancestor_pid, _name in _parents(_os.getpid()):
        if _ancestor_pid == pid:
            return False
    name = _process_name(pid)
    if name and "claude" in name.lower():
        return True
    # Same fallback as _is_claude: the CLI's own binary is installed and run
    # as ~/.local/share/claude/versions/<version>, whose `comm` is the bare
    # version string once that path is what actually gets exec'd.
    return "claude" in _cmdline_argv0(pid).lower()


def deliver_request(session_id: str, text: str) -> dict[str, Any]:
    """Type *text* into the live terminal running *session_id*.

    Refuses unless the target can be positively identified as a claude process
    in a window that is not this server's own. Every refusal falls back to the
    headless turn, so the unsafe direction is also the inert one.
    """
    pid = session_pid(session_id)
    if not _is_claude_process(pid):
        return {
            "delivered": False,
            "reason": "no identified claude process for this session",
            "target": None,
        }
    target = locate(session_id)
    if not target:
        return {"delivered": False, "reason": "no live terminal window", "target": None}
    # Sharing the server's window is NOT a veto, though it looks like one.
    # Measured on this deployment: the console runs in STY 2126909.pts-5
    # window 0 and cweb2's claude runs in the same window, because the console
    # was launched with `&` from it. Refusing a window match would refuse every
    # agent started that way -- including the session this routing exists for.
    #
    # It is also redundant. F-01 works by naming the console's own pid so the
    # walk upwards lands in the console's window; refusing our pid and our
    # whole ancestry kills that at the source. A pid that has passed those
    # checks is a different, live, verified claude, and a window match then
    # means only that it shares a window with us. Logged, not refused, so a
    # delivery stays attributable afterwards.
    shared = bool(target.get("shares_server_window"))
    progress: dict[str, Any] = {}
    ok = send_text(target, text, progress)
    return {
        "delivered": ok,
        # Naming what was refused, and how much of the text had already
        # reached the window, so a partial send is distinguishable from one
        # that never started -- the two leave the terminal in different
        # states and the caller's log is the only place that shows it.
        "reason": "" if ok else (
            progress.get("refused") or "the terminal refused the input"),
        "chunks_total": progress.get("chunks_total", 0),
        "chunks_sent": progress.get("chunks_sent", 0),
        "target": target,
        "pid": pid,
        "shares_server_window": shared,
    }


def selected_index(snapshot: str) -> int | None:
    """Return which option a prompt currently has highlighted, 1-based.

    Claude marks the highlighted row with "❯". Knowing where the cursor sits is
    what makes answering deterministic: pressing Enter blindly would confirm
    whatever happened to be selected.
    """
    for line in snapshot.splitlines():
        if "❯" not in line:
            continue
        match = re.search(r"❯\s*(\d+)\.", line)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def visible_options(snapshot: str) -> list[dict[str, Any]]:
    """Return the numbered options a prompt is currently showing.

    The live prompt offers more than the tool call declared: Claude appends its
    own choices, such as free text and "Chat about this". Reading them off the
    screen is the only way to show every answer that is actually available.
    """
    options: list[dict[str, Any]] = []
    for line in snapshot.splitlines():
        match = re.match(r"\s*[❯>]?\s*(\d+)\.\s+(.*?)\s*$", line)
        if not match:
            continue
        index, label = match.group(1), match.group(2).strip()
        if not label:
            continue
        try:
            number = int(index)
        except ValueError:
            continue
        if any(existing["index"] == number for existing in options):
            continue
        options.append({
            "index": number,
            "label": label,
            "selected": "❯" in line,
        })
    return sorted(options, key=lambda option: option["index"])


def prompt_lines(snapshot: str) -> list[str]:
    """The question's lines, top to bottom, exactly as they appear on screen.

    Kept as lines rather than one string because the two consumers need
    different things. Display wants them joined; the needle that confirms the
    prompt is still on screen must be a *contiguous* run of characters the
    screen actually contains, and a joined string is not -- the lines are
    separate rows there, so ``needle in snapshot`` would always be false and
    every terminal-read prompt would be reported unanswerable.

    Read backwards from the first numbered option, because that is the only
    landmark whose position is fixed: the question above it runs to one line or
    several, and everything below it is options and key hints.

    Collection stops at a box-drawing character -- the frame Claude draws around
    the command it is asking about -- and at a run of two blank lines. Without
    the first stop the command's own text, which can be dozens of lines of diff
    or commit message, would be read back as the question.

    A *single* blank line is skipped rather than treated as the end, because
    Claude puts blank lines inside the question block itself. Stopping on one
    kept only the last line, which for a permission prompt is the useless half:
    "Do you want to proceed?" with no trace of what was being proposed.
    """
    lines = snapshot.splitlines()
    first: int | None = None
    for index, line in enumerate(lines):
        if re.match(r"\s*[❯>]?\s*\d+\.\s+\S", line):
            first = index
            break
    if first is None:
        return []
    collected: list[str] = []
    blanks = 0
    for line in reversed(lines[:first]):
        text = line.strip()
        if not text:
            blanks += 1
            # Two in a row is a real gap between the prompt and whatever
            # preceded it. One is part of the prompt's own layout.
            if blanks >= 2:
                break
            continue
        if any(char in text for char in _BOX_CHARS):
            break
        blanks = 0
        collected.append(text)
        if len(collected) >= _PROMPT_TEXT_MAX_LINES:
            break
    collected.reverse()
    return collected


def prompt_text(snapshot: str) -> str:
    """The question the prompt on screen is asking, as one line."""
    return " ".join(prompt_lines(snapshot))[:_TEXT_MAX]


def read_prompt(session_id: str) -> dict[str, Any] | None:
    """The prompt *session_id* is blocked on, read from its own terminal.

    The transcript is the usual source for a pending question and it cannot see
    every prompt. A permission prompt -- "Permission rule Bash(git push*)
    requires confirmation for this command" -- is a TUI interaction that the CLI
    never writes to the JSONL, so ``transcripts.pending_question`` returns None
    while the session sits blocked indefinitely. The same is true of anything
    else the terminal raises on its own account rather than through a tool call.

    Returned in the shape ``transcripts.pending_question`` uses, so callers do
    not branch on where the question came from. ``options`` is left empty for
    the same reason :func:`transcripts._approval_block` leaves it empty: the
    endpoint reads the real labels off the terminal with :func:`visible_options`,
    and a list invented here would offer answers the terminal never showed.

    ``source`` records that this was observed on screen rather than recorded by
    the CLI. It is the honest provenance for a question with no tool_use id, and
    the console does not claim a question it has not actually seen.
    """
    target = locate(session_id)
    if target is None:
        return None
    snapshot = refresh(target)
    if not looks_like_a_prompt(snapshot):
        return None
    if not visible_options(snapshot):
        return None
    lines = prompt_lines(snapshot)
    text = " ".join(lines)[:_TEXT_MAX]
    # The longest line, not the last: it is the most specific, and the last is
    # usually "Do you want to proceed?" -- which every permission prompt shows,
    # so it would confirm a *different* prompt just as readily as this one.
    needle = max(lines, key=len) if lines else ""
    return {
        # No tool_use id exists: nothing recorded this question. Empty rather
        # than synthesised, so it cannot be mistaken for a transcript id.
        "id": "",
        "questions": [{
            "question": text or "This session is waiting on a prompt.",
            "header": "Permission" if _PERMISSION_RE.search(text) else "Waiting",
            "multi_select": False,
            "options": [],
        }],
        "approval": True,
        # Confirms the same prompt is still on screen when the keystroke is
        # delivered. It came off the screen, so it matches unless the prompt
        # changed in between -- which is the race worth catching, since the
        # keystroke would otherwise land on whatever replaced it.
        "needle": needle,
        "source": "terminal",
    }


def has_prompt(session_id: str, ttl_s: float = _PROMPT_CACHE_TTL_S) -> bool:
    """Whether *session_id* is showing a prompt, cheaply enough to poll.

    :func:`read_prompt` captures the terminal, which costs a subprocess and can
    wait up to ``_CMD_TIMEOUT_S``. The conversation list and the members panel
    are polled every few seconds and only need the yes/no, so the answer is
    cached briefly and shared between them.

    The cache makes this answer up to ``ttl_s`` stale, which is the right
    trade for a badge marker and the wrong one for delivering a keystroke.
    Callers about to answer a prompt use :func:`read_prompt` directly and get a
    fresh capture, because acting on a prompt that has since been replaced
    would send the answer to whatever replaced it.
    """
    now = time.monotonic()
    cached = _prompt_cache.get(session_id)
    if cached is not None and now - cached[0] < ttl_s:
        return cached[1]
    try:
        found = read_prompt(session_id) is not None
    except Exception:
        return False
    # Bounded so a long-lived server does not accumulate an entry per session
    # id it has ever seen. Cleared wholesale rather than by age: the entries are
    # equivalent and the cache is a few seconds deep, so nothing is lost.
    if len(_prompt_cache) > _PROMPT_CACHE_MAX:
        _prompt_cache.clear()
    _prompt_cache[session_id] = (now, found)
    return found


def answer(target: dict[str, Any], want: int, max_moves: int = 12) -> dict[str, Any]:
    """Move the prompt's highlight to option *want* and confirm it.

    Navigating and verifying, rather than pressing Enter and hoping, is the
    whole point: Enter confirms whatever is highlighted, and the highlight is
    wherever the last person left it. Each move is checked against a fresh
    snapshot, and the selection is confirmed only once it is demonstrably on
    the requested option.
    """
    snapshot = refresh(target) or str(target.get("snapshot") or "")
    current = selected_index(snapshot)
    if current is None:
        return {"ok": False, "reason": "Could not see which option is selected."}
    options = visible_options(snapshot)
    if want not in [option["index"] for option in options]:
        return {"ok": False, "reason": f"Option {want} is not on offer."}

    moves = 0
    while current != want and moves < max_moves:
        key = "down" if want > current else "up"
        if not deliver(target, key):
            return {"ok": False, "reason": "Could not reach the terminal."}
        moves += 1
        time.sleep(0.15)
        snapshot = refresh(target)
        moved = selected_index(snapshot)
        if moved is None:
            return {"ok": False, "reason": "Lost track of the selection."}
        if moved == current:
            # The highlight did not move, so pressing Enter now would confirm
            # the wrong option. Stop rather than guess.
            return {"ok": False, "reason": "The selection did not move."}
        current = moved
    if current != want:
        return {"ok": False, "reason": "Could not reach that option."}

    chosen = next(
        (o["label"] for o in visible_options(snapshot) if o["index"] == want), ""
    )
    if not deliver(target, "enter"):
        return {"ok": False, "reason": "Could not confirm the selection."}
    time.sleep(0.6)
    return {"ok": True, "index": want, "label": chosen, "moves": moves,
            "after": refresh(target)[-1500:]}


def dismiss(target: dict[str, Any], settle_s: float = 0.5) -> dict[str, Any]:
    """Close a prompt without answering it, by delivering Escape.

    Nothing new is delivered here -- "Esc to cancel" is the prompt's own offer
    and escape has always been in _KEYS. What is new is checking afterwards,
    and reporting *delivered* separately from *ok*, because the caller needs
    those two failures to be told apart:

    * the terminal refused the key, so nothing happened and a retry is safe;
    * the key went in and the prompt is still on screen, where a second escape
      is not a retry at all -- it would reach whatever the session moved on to
      and interrupt that instead.

    A snapshot that cannot be read afterwards is its own third case, and is
    reported as a failure rather than a success. ``looks_like_a_prompt("")`` is
    False, so treating an unreadable window as closed would turn "we cannot see
    the terminal" into "the question is gone" -- the one claim that leaves a
    session blocked while the UI says it is not.
    """
    if not deliver(target, "escape"):
        return {
            "ok": False,
            "delivered": False,
            "reason": "Could not reach the terminal.",
        }
    time.sleep(settle_s)
    after = refresh(target)
    if not after:
        return {
            "ok": False,
            "delivered": True,
            "reason": "Escape was delivered, but the terminal could not be read "
                      "afterwards, so whether the prompt closed is unknown.",
        }
    if looks_like_a_prompt(after):
        return {
            "ok": False,
            "delivered": True,
            "reason": "The prompt is still open at the terminal.",
        }
    return {"ok": True, "delivered": True, "after": after[-1500:]}
