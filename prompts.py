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


def session_pid(session_id: str) -> int | None:
    """Return the OS pid running *session_id*, from its session file.

    Claude writes ~/.claude/sessions/<pid>.json holding the sessionId, so the
    mapping already exists on disk and does not need to be inferred.
    """
    if not session_id:
        return None
    try:
        entries = sorted(_SESSIONS_DIR.glob("*.json"))
    except OSError:
        return None
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        # The filename is the pid for live sessions; some files are named by
        # session id instead, in which case the pid inside is authoritative.
        for candidate in (path.stem, data.get("pid")):
            try:
                pid = int(candidate)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if Path(f"/proc/{pid}").is_dir():
                return pid
    return None


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


def locate(session_id: str) -> dict[str, str] | None:
    """Identify the multiplexer window a session runs in, from its own process.

    screen exports STY (session) and WINDOW (index) into every window, and tmux
    exports TMUX_PANE, so the window is stated by the environment rather than
    inferred. This replaced matching on screen contents, which was actively
    dangerous: two sessions in the same screen session were distinguished only
    by what was on their screens, and quoting one session's question inside
    another's output was enough to select the wrong window -- observed, not
    hypothetical.
    """
    pid = session_pid(session_id)
    if not pid:
        return None
    for candidate, _name in _parents(pid):
        env = _environ(candidate)
        pane = env.get("TMUX_PANE")
        if pane:
            return {"kind": "tmux", "session": env.get("TMUX", ""), "window": pane}
        sty, window = env.get("STY"), env.get("WINDOW")
        if sty and window and window.isdigit():
            return {"kind": "screen", "session": sty, "window": window}
    return None


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
