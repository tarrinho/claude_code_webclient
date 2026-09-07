#!/usr/bin/env python3
"""UserPromptSubmit hook: append every prompt Pedro sends to PT_request.md.

Guarantees the (date, request) half of the record even across context
compaction or a new session -- the harness runs this on every submitted
prompt, so it cannot be forgotten the way a remembered habit can. The
"result" half is filled in later, by the agent handling the turn, by
replacing the `_pending_` marker on this same entry (see CLAUDE.md).

This hook fires for *any* `claude` process launched under this repo's
`.claude/` config -- not only Pedro's interactive sessions. Confirmed live:
routes/machines.py's backend-test probe spawns `claude -p "Reply with
exactly: ok"` with no `cwd=` override, inheriting the WebConsole service's
own cwd (the repo root), and that call fired this hook three times with a
prompt Pedro never typed. `PROJECTS_ROOT` (`config.py`) is also nested
inside the repo, so real conversation turns and supervisor subtasks --
whose `work_dir` lives under it -- are an unconfirmed but plausible second
source of the same leak.

Three filters, because each one alone was shown to leak. The first two are
about *who spawned the CLI*; the third is about *what the harness delivered
into a session it did not spawn*, which is a separate leak with a separate
cause -- see `_HARNESS_PREFIXES`.

**`WC_INTERNAL_SPAWN`** is the reliable one. Every programmatic spawn of the
CLI sets it -- `runner._build_env`, `claude_proxy._backend_env` and
`routes/machines.py`'s backend probe -- and `bin/wc-claude.sh` deliberately
does not, so an interactive session Pedro started is exactly the case that
arrives without it. Env-based rather than prompt-based on purpose: a
blocklist of known internal prompt strings would miss the supervisor's
task-specific ones entirely.

**`TERM`** is kept as a second line of defence, not as the primary test. The
original filter relied on it alone -- the reasoning being that the WebConsole
service is a systemd --user unit with no controlling terminal, so its spawns
have none. That holds for the service, and only for the service:
`runner.py`'s env allowlist passes `TERM` straight through, so the same probe
launched from a shell (a peer running pytest, or the Settings "Test" button
in a terminal-rooted session) inherits it and was logged. Eleven such
`Reply with exactly: ok` entries accumulated in PT_request.md before this was
found. If a new spawn site is ever added without the marker, `TERM` still
catches the service path.

**Scope.** Registered in `~/.claude/settings.json` so it fires regardless of
which directory a session was launched from -- the previous project-level
registration only covered sessions rooted *inside* this repo, and the cweb
sessions launched from the parent (`/home/kali/projects`) logged nothing at
all while appearing to be covered. `_in_scope` below keeps that widening from
turning into cross-project noise.
"""
import datetime
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LOG_PATH = REPO / "PT_request.md"

# Prompts the harness delivers on its own, which reach this hook indistinguishable
# from something Pedro typed. `WC_INTERNAL_SPAWN` cannot catch these -- they are
# delivered *into* a live interactive session, so there is no separate spawn to
# mark and TERM is legitimately present. Six `<task-notification>` entries were
# logged as requests before this filter existed. CLAUDE.md §10 says these are not
# his requests and must not be recorded.
#
# Matched as a prefix on the whole prompt rather than anywhere inside it: a real
# request that *quotes* one of these tags while asking about it is still a real
# request, and must keep being logged.
_HARNESS_PREFIXES = (
    "<task-notification>",
    "<cross-session-message",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
)


def _in_scope(cwd: str) -> bool:
    """Whether a session in *cwd* is plausibly working on this repo.

    The hook is registered at user level, so it fires for every session on
    this machine. Without this, a session working on an unrelated project
    would append to this repo's request log.

    Three cases, and the middle one is the reason this function exists:

    - `cwd` is the repo, or inside it: certainly in scope.
    - the repo is inside `cwd` (a session rooted at `/home/kali/projects`,
      which is how the cweb sessions are actually launched): in scope. This
      is the gap the user-level move exists to close, so it has to pass.
    - neither contains the other: out of scope, skip.

    The middle case is deliberately permissive and its cost is stated
    plainly: a parent-rooted session working on some *other* project under
    `/home/kali/projects` will still be logged here. That is a smaller and
    more visible fault than a whole session class silently recording nothing,
    which is the state this replaces.
    """
    if not cwd:
        return False
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    return here == REPO or REPO in here.parents or here in REPO.parents


def main() -> None:
    # Set by every programmatic spawn of the CLI; absent for the interactive
    # sessions this log is meant to record. See the module docstring.
    if os.environ.get("WC_INTERNAL_SPAWN"):
        return
    if not os.environ.get("TERM"):
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return
    if prompt.startswith(_HARNESS_PREFIXES):
        return
    # The payload carries the session's own directory; os.getcwd() here is the
    # hook process's, which the harness does not promise to be the same thing.
    if not _in_scope(payload.get("cwd") or os.getcwd()):
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {ts}\n\n**Request:** {prompt}\n\n**Result:** _pending_\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)


if __name__ == "__main__":
    main()
