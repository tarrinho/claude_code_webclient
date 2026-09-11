#!/usr/bin/env python3
"""UserPromptSubmit hook: tell a session when webconsole.service restarted.

Detects the restart from the service's own state (systemd's
ActiveEnterTimestamp for webconsole.service) rather than from who issued the
restart command. That is the point: a PostToolUse hook watching for
`systemctl --user restart webconsole.service` in a Bash call would only catch
restarts an agent ran itself, and would miss the two kinds that matter most --
systemd's own `Restart=always` after a crash, and wc-health.sh's own
auto-restart on a stalled write path (CHANGELOG, "the health check now
notices a server that has stopped writing"). Neither goes through any
session's Bash tool.

Fires on every prompt, in every session working on this repo, and keeps a
per-session_id watermark of the last ActiveEnterTimestamp it saw
(~/.claude/state/wc-restart-watermarks.json). When the current value differs
from what this session last recorded, it injects a reminder via
hookSpecificOutput.additionalContext -- so a live session finds out on its own
next turn, without any other agent having to remember to broadcast it. See
CLAUDE.md rule 9: a restart cancels in-flight turns, which is exactly the kind
of thing a session working from stale assumptions would not otherwise notice.

A session's first prompt only records the current timestamp; it does not
alert, because a restart that predates the session joining is not news to it.

Mirrors log_pt_request.py's `_in_scope` scoping and is registered the same
way, at user level (~/.claude/settings.json) rather than this repo's
.claude/settings.json -- project-level registration only fires for sessions
launched inside the repo, and the cweb sessions are launched one directory up
(/home/kali/projects), which is the exact gap that hook's own docstring
records having to fix once already.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
STATE_DIR = Path.home() / ".claude" / "state"
STATE_FILE = STATE_DIR / "wc-restart-watermarks.json"
UNIT = "webconsole.service"


def _in_scope(cwd: str) -> bool:
    """Whether a session in *cwd* is plausibly working on this repo.

    Same three cases as log_pt_request.py's version, for the same reason: the
    hook is registered at user level and fires for every session on this
    machine, and the cweb sessions are rooted one directory above the repo.
    """
    if not cwd:
        return False
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError):
        return False
    return here == REPO or REPO in here.parents or here in REPO.parents


def _active_enter_timestamp() -> str | None:
    """Read webconsole.service's current ActiveEnterTimestamp, or None.

    None covers every failure mode alike (systemctl missing, unit unknown,
    permission denied, timeout) -- all of them mean "cannot tell right now",
    and the hook should say nothing rather than guess.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", UNIT,
             "--property=ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict) -> None:
    """Atomic write (temp + rename), same pattern db.py uses for session
    files -- several sessions' hooks can fire within the same second."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    if not _in_scope(payload.get("cwd") or os.getcwd()):
        return

    current = _active_enter_timestamp()
    if not current:
        return

    session_id = payload.get("session_id") or "_unknown"
    state = _load_state()
    previous = state.get(session_id)
    state[session_id] = current
    _save_state(state)

    if previous is None or previous == current:
        return

    context = (
        f"webconsole.service restarted (ActiveEnterTimestamp changed from "
        f"`{previous}` to `{current}`) since your last turn in this session. "
        "Any turn you had running against it may have been cancelled -- "
        "check before assuming a prior action completed, per CLAUDE.md "
        "rule 9. If you have work in flight elsewhere, this is worth "
        "mentioning to peer sessions too."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
