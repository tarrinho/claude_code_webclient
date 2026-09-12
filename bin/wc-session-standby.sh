#!/usr/bin/env bash
# Suspend a running `claude` terminal session to free its RAM, leaving enough
# behind to resume it exactly where it left off.
#
# Closing a terminal window does not do this: the process keeps running
# detached and holds its ~250-380MB regardless. This sends the process itself
# SIGTERM, after recording what `wc-session-wake.sh` needs to bring it back --
# the session id, its cwd, and the friendly name it registered (e.g. "cweb2"),
# all already written by the CLI itself to ~/.claude/sessions/<pid>.json.
#
# Refuses on a session mid-turn ("status":"busy") rather than killing it out
# from under a running turn -- same class of risk as a webconsole.service
# restart cancelling an in-flight turn (CLAUDE.md rule 9), and the session's
# own status field already tells us which state it is in.
#
# JSON via python3, not jq: jq is not installed on this host and python3
# already is (every other bin/ script that touches JSON uses it).
set -uo pipefail

SESSIONS_DIR="${HOME}/.claude/sessions"
STANDBY_DIR="${HOME}/.claude/standby"

usage() {
    echo "Usage: $(basename "$0") <name-or-pid>" >&2
    echo "  Looks up a running claude session by its friendly name (e.g. cweb4)" >&2
    echo "  or by pid, and suspends it. Resume later with wc-session-wake.sh." >&2
    exit 2
}

[ $# -eq 1 ] || usage
target="$1"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (session files are JSON)" >&2
    exit 1
fi

# Find the session file: by pid directly, or by scanning for a matching name.
# Session files not named <pid>.json (the older webconsole-entrypoint ones)
# are skipped -- they describe a session this script cannot signal a live
# process for.
match=""
if [ -f "${SESSIONS_DIR}/${target}.json" ]; then
    match="${SESSIONS_DIR}/${target}.json"
else
    for f in "${SESSIONS_DIR}"/*.json; do
        [ -f "$f" ] || continue
        base="$(basename "$f" .json)"
        case "$base" in
            ''|*[!0-9]*) continue ;;  # only <pid>.json files hold a live process
        esac
        name="$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('name',''))
except Exception:
    pass" "$f" 2>/dev/null)"
        if [ "$name" = "$target" ]; then
            match="$f"
            break
        fi
    done
fi

if [ -z "$match" ]; then
    echo "no running session found matching '${target}' (checked pid and name)" >&2
    exit 1
fi

eval "$(python3 -c "
import json, shlex
d = json.load(open('${match}'))
print('pid=' + shlex.quote(str(d.get('pid', ''))))
print('session_id=' + shlex.quote(str(d.get('sessionId', ''))))
print('cwd=' + shlex.quote(str(d.get('cwd', ''))))
print('name=' + shlex.quote(str(d.get('name', ''))))
print('status=' + shlex.quote(str(d.get('status', ''))))
")"

if ! kill -0 "$pid" 2>/dev/null; then
    echo "pid ${pid} (from ${match}) is not running -- stale session file, nothing to standby" >&2
    exit 1
fi

if [ "$status" = "busy" ]; then
    echo "session '${name:-$target}' (pid ${pid}) is mid-turn (status: busy) -- refusing to" >&2
    echo "standby it. Wait for it to finish, or confirm you want to interrupt the turn and" >&2
    echo "re-run; this script does not force past that on its own." >&2
    exit 1
fi

mkdir -p "$STANDBY_DIR"
record="${STANDBY_DIR}/${name:-$target}.json"
python3 -c "
import json, sys
json.dump({
    'sessionId': sys.argv[1],
    'cwd': sys.argv[2],
    'name': sys.argv[3],
    'standbyAt': sys.argv[4],
}, open(sys.argv[5], 'w'))
" "$session_id" "$cwd" "${name:-$target}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$record"

echo "standby recorded: ${record}"
echo "  name=${name:-$target} sessionId=${session_id} cwd=${cwd}"
echo "sending SIGTERM to pid ${pid}..."
kill "$pid" 2>/dev/null || true

for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || { echo "pid ${pid} exited -- standby complete."; exit 0; }
    sleep 0.5
done

echo "pid ${pid} did not exit within 5s after SIGTERM." >&2
echo "The standby record was still written -- resume with wc-session-wake.sh once" >&2
echo "the process actually exits (check with: kill -0 ${pid})." >&2
exit 1
