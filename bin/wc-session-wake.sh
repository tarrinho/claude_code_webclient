#!/usr/bin/env bash
# Launch a resumed Claude session inside a detached screen window.
#
# Reads the standby record written by wc-session-standby.sh and starts
# `claude --resume` inside `screen -d -m` so the session survives
# independently of the webconsole or any terminal.
#
# If a screen window for the same session name already exists, reuses it
# instead of creating a second one.
#
# JSON via python3, not jq: jq is not installed on this host.
set -uo pipefail

STANDBY_DIR="${HOME}/.claude/standby"

usage() {
    echo "Usage: $(basename "$0") <name>" >&2
    exit 2
}

[ $# -eq 1 ] || usage
name="$1"

if ! command -v screen >/dev/null 2>&1; then
    echo "screen is not installed — cannot launch detached session" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (standby records are JSON)" >&2
    exit 1
fi

record="${STANDBY_DIR}/${name}.json"
if [ ! -f "$record" ]; then
    echo "no standby record for '${name}' at ${record}" >&2
    exit 1
fi

# Parse the record. python3 writes to a temp file so we can read it without
# shell quoting issues (paths may contain spaces, newlines, etc.).
tmpvals=$(mktemp)
trap 'rm -f "$tmpvals"' EXIT
python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sid = d.get('sessionId') or ''
c   = d.get('cwd') or ''
t   = d.get('standbyAt') or ''
with open(sys.argv[2], 'w') as f:
    print(sid, file=f)
    print(c, file=f)
    print(t, file=f)
" "$record" "$tmpvals"

session_id=$(sed -n '1p' "$tmpvals")
cwd=$(sed -n '2p' "$tmpvals")

if [ -z "$session_id" ]; then
    echo "standby record ${record} has no sessionId" >&2
    exit 1
fi

# Reuse an existing screen session with the same name, or launch a new one.
if screen -ls 2>/dev/null | grep -q "(${name}\s*(Detached))"; then
    echo "screen window '${name}' already exists — the session is running." >&2
    echo "Reattach with: screen -r ${name}" >&2
    rm -f "$record"
    exit 0
fi

# Build and launch the command.
# `exec` goes immediately before claude, never in front of the whole command.
# `exec cd "$cwd" && claude ...` asks the shell to exec `cd`, which is a
# builtin and not a program: bash answers "exec: cd: not found" and the shell
# dies instantly, so screen's window closes and nothing runs. Measured
# 2026-09-14 waking cweb4 -- the session vanished exactly as it had before the
# fix, for a new reason introduced by the fix.
if [ -n "$cwd" ]; then
    launch="cd \"$cwd\" && exec claude --resume \"$session_id\""
else
    launch="exec claude --resume \"$session_id\""
fi
echo "# launching: $launch" >&2

# Through a shell, and the exit status is checked. Both halves were missing.
#
# `screen -d -m -S name "$launch"` hands screen the whole string as a *program
# name* to exec. `cd "/x" && claude --resume "..."` is not an executable, so
# screen exits immediately and nothing is started. The script then printed
# "launched in screen (detached)", exited 0, and deleted the record.
#
# Measured 2026-09-14 waking cweb2: that message, exit 0, no screen session,
# and the standby record gone -- the operator told it worked, the pointer to
# the session destroyed. The session was only recoverable because the id was
# still on screen from the standby a few minutes earlier.
#
# `bash -c` because the command is shell syntax by construction (a cd and a
# conditional), and `exec` so the shell is replaced by claude rather than
# lingering as its parent.
# The waking session's own identity must not reach the woken one.
#
# Wake is normally run from inside a Claude session, so everything screen
# starts inherits that session's environment. Measured 2026-09-14 on cweb4:
# the resumed window printed "Transcript saving is off -- inherited
# CLAUDE_CODE_CHILD_SESSION marker", so the work done in a woken session was
# not being persisted, which is the opposite of what standby/wake is for. It
# was also missing from ~/.claude/sessions entirely -- invisible to the
# session list and impossible to stand down again -- and its
# CLAUDE_CODE_MESSAGING_SOCKET pointed at the *waking* session, so
# cross-session messages addressed to it would have arrived somewhere else.
#
# Cleared by name, not by prefix: CLAUDE_CODE_MAX_OUTPUT_TOKENS and friends
# are deliberate settings on this host (see CLAUDE.md §0.1), and wiping the
# whole CLAUDE_* family to fix an identity leak would take those with it.
if ! env -u CLAUDE_CODE_CHILD_SESSION \
         -u CLAUDE_CODE_SESSION_ID \
         -u CLAUDE_PID \
         -u CLAUDE_CODE_MESSAGING_SOCKET \
         -u CLAUDE_CODE_SESSION_ATTENDED \
         screen -d -m -S "$name" bash -c "$launch"; then
    echo "screen failed to start a session for '${name}'." >&2
    echo "The standby record is kept at ${record} -- nothing was launched," >&2
    echo "so the session is still recoverable." >&2
    exit 1
fi

echo "Session '${name}' launched in screen (detached)." >&2
echo "View it with: screen -r ${name}" >&2

# Only after a launch that actually succeeded.
rm -f "$record"
