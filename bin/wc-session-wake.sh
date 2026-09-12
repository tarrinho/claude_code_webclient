#!/usr/bin/env bash
# Print the command to resume a session that wc-session-standby.sh suspended.
#
# Deliberately prints rather than execs: a resumed `claude` needs an
# interactive terminal someone is actually attached to, and this script has no
# way to hand its own terminal to a different session or open a new window on
# the user's behalf. `eval "$(...)"` in the terminal you want it in is the
# user's call, not this script's.
#
# JSON via python3, not jq: jq is not installed on this host and python3
# already is (every other bin/ script that touches JSON uses it).
set -uo pipefail

STANDBY_DIR="${HOME}/.claude/standby"

usage() {
    echo "Usage: $(basename "$0") <name>" >&2
    echo "  Prints the command to resume a session wc-session-standby.sh suspended." >&2
    echo "  Run it yourself in the terminal you want the session back in." >&2
    exit 2
}

[ $# -eq 1 ] || usage
name="$1"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (standby records are JSON)" >&2
    exit 1
fi

record="${STANDBY_DIR}/${name}.json"
if [ ! -f "$record" ]; then
    echo "no standby record for '${name}' at ${record}" >&2
    echo "(only sessions suspended by wc-session-standby.sh are tracked here)" >&2
    exit 1
fi

eval "$(python3 -c "
import json, shlex
d = json.load(open('${record}'))
print('session_id=' + shlex.quote(str(d.get('sessionId', ''))))
print('cwd=' + shlex.quote(str(d.get('cwd', ''))))
print('standby_at=' + shlex.quote(str(d.get('standbyAt', ''))))
")"

echo "# '${name}' was put on standby at ${standby_at}" >&2
echo "# session id: ${session_id}" >&2
if [ -n "$cwd" ]; then
    echo "cd $(printf '%q' "$cwd") && claude --resume ${session_id}"
else
    echo "claude --resume ${session_id}"
fi

# Once resumed, the CLI writes a fresh <new-pid>.json for the new process and
# re-registers the same name via its own naming handshake -- this record's job
# is done. Leaving it behind would let a stale cwd/session pairing resurface
# if the name is ever reused for an unrelated session later.
rm -f "$record"
