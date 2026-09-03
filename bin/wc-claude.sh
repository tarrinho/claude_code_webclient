#!/usr/bin/env bash
# wc-claude — start Claude Code on the backend WebConsole is configured to use.
#
# WebConsole's "active machine" and default model govern the turns *it* spawns:
# claude_proxy reads the machine record and puts the endpoint and key into the
# child's environment. A session started by hand from a shell never touches that
# database, so it silently ignores the configuration and falls through to the
# CLI's own default — api.anthropic.com on the host login.
#
# The consequence is not visible anywhere. You set "use the AI machine" in the
# one place the product offers, the web chats obey it, and six terminal sessions
# keep spending Anthropic credit with nothing to say so.
#
# This resolves the same machine the proxy would, exports the same variables the
# proxy would, and execs claude. Use it instead of `claude`:
#
#     bin/wc-claude.sh --dangerously-skip-permissions --resume cweb2
#
# or make it the default for interactive shells:
#
#     alias claude='/home/kali/projects/claude-code-webconsole/bin/wc-claude.sh'
#
# Flags are passed through untouched. --model is added only when you did not
# give one, so an explicit --model always wins.
#
# WC_CLAUDE_HOTSWAP=1, combined with --resume <name>, keeps the session on the
# active machine as it changes: see wc_claude_supervised_loop below.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${WC_DB_PATH:-$HERE/data/webconsole.db}"

# Sets global RESUME_NAME to the value following --resume in "$@", or "".
# Used both for the transcript-doctor mismatch check and to gate hot-swap.
detect_resume_name() {
    RESUME_NAME=""
    local i j
    for i in $(seq 1 $#); do
        if [ "${!i}" = "--resume" ]; then
            j=$((i + 1))
            RESUME_NAME="${!j:-}"
            return
        fi
    done
}

# Read-only, always. Opening this database read-write from a second process is
# what took production's write path down for 37 minutes (registry #41): the
# server's connection could never upgrade its transaction.
#
# Pure: prints one tab-separated line, sets nothing. Called by resolve_backend
# on startup and, from Task 2 onward, by the poller on a timer -- one query
# definition, not two copies to keep in sync.
query_backend() {
    python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
try:
    row = con.execute(
        "SELECT name, provider, base_url, api_key, model FROM ai_machines "
        "WHERE active = 1 LIMIT 1"
    ).fetchone()
    setting = con.execute(
        "SELECT value FROM settings WHERE key = 'default_model'"
    ).fetchone()
finally:
    con.close()

def clean(value):
    # Any whitespace would break the field split, and a newline in a value
    # would let it forge a field. Empty becomes "-" so the positions hold.
    text = "" if value is None else str(value).strip()
    return "".join(text.split()) or "-"


def last_field(value):
    # The name is read last, and `read` puts everything remaining into the final
    # variable -- so spaces are safe there and only newlines are not. Collapsing
    # them like the others printed "CurrentAIMachine" in the banner, which is a
    # small thing to get wrong in the one line that tells you which backend you
    # are about to talk to.
    text = "" if value is None else str(value).strip()
    return " ".join(text.split()) or "-"

if row is None:
    print("- - - - -")
else:
    # The machine's own model first, then the global default -- the same order
    # runner.get_default_model uses once a chat pins nothing.
    model = (row["model"] or "").strip() or (
        (setting["value"] if setting else "") or "").strip()
    fields = [clean(x) for x in (
        row["provider"], row["base_url"], row["api_key"], model)]
    print("\t".join([*fields, last_field(row["name"])]))
PY
}

# Sets globals PROVIDER BASE_URL API_KEY MODEL NAME from query_backend's output.
resolve_backend() {
    read -r PROVIDER BASE_URL API_KEY MODEL NAME <<<"$(query_backend)"
}

# Mirror claude_proxy._backend_env exactly, including what it *removes*. A
# non-anthropic backend must not inherit Anthropic variables from this shell,
# and a machine with no base_url means "the official API" — leaving an inherited
# one there is registry #68, the bug whose workaround was to unset these by hand.
apply_env() {
    unset ANTHROPIC_AUTH_TOKEN
    if [ "$PROVIDER" != "anthropic" ]; then
        unset ANTHROPIC_BASE_URL
    elif [ "$BASE_URL" != "-" ]; then
        export ANTHROPIC_BASE_URL="$BASE_URL"
    else
        unset ANTHROPIC_BASE_URL
    fi

    if [ "$PROVIDER" = "anthropic" ] && [ "$API_KEY" != "-" ]; then
        export ANTHROPIC_API_KEY="$API_KEY"
    else
        # No key: the CLI must fall back to the host's own login, which it will
        # not do while CLAUDE_CODE_SIMPLE is set.
        unset ANTHROPIC_API_KEY
        unset CLAUDE_CODE_SIMPLE
    fi
}

# --model only if the caller did not pass one. Sets global array MODEL_ARGS.
build_model_args() {
    local want_model=1 arg
    for arg in "$@"; do
        case "$arg" in
            --model|--model=*) want_model=0 ;;
        esac
    done
    MODEL_ARGS=()
    if [ "$want_model" = 1 ] && [ "$MODEL" != "-" ]; then
        MODEL_ARGS=(--model "$MODEL")
    fi
}

# Switching a session between providers is what poisons a transcript: a thinking
# block carries a provider-specific signature, and the other provider rejects the
# whole conversation from then on. WebConsole repairs that before every turn; a
# terminal session has nothing doing it. So warn before resuming a session whose
# last turn ran on a different model, and say what to do about it.
#
# Uses the RESUME_NAME global (set by detect_resume_name) rather than
# re-scanning "$@" -- from Task 3 onward this runs again on every hot-swap
# restart, and the resume target never changes across those restarts.
check_transcript_doctor() {
    [ -z "$RESUME_NAME" ] && return
    local last
    last="$(python3 - "$RESUME_NAME" <<'PY' 2>/dev/null || true
import json, pathlib, sys
name = sys.argv[1]
sess = pathlib.Path.home() / ".claude" / "sessions"
proj = pathlib.Path.home() / ".claude" / "projects"
sid = name
for meta in sess.glob("*.json"):
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        continue
    if data.get("name") == name and data.get("sessionId"):
        sid = data["sessionId"]
        break
for path in proj.glob(f"*/{sid}.jsonl"):
    seen = ""
    for line in path.read_text(errors="replace").splitlines():
        if '"model"' not in line:
            continue
        try:
            message = (json.loads(line).get("message") or {})
        except ValueError:
            continue
        if message.get("role") == "assistant" and message.get("model"):
            model = message["model"]
            if model != "<synthetic>":
                seen = model
    print(seen)
    break
PY
)"
    if [ -n "$last" ] && [ "$MODEL" != "-" ] && [ "$last" != "$MODEL" ]; then
        # Repair rather than warn. A warning puts the work on someone who
        # has to remember it every time, and the whole reason this failure
        # cost five sessions is that nothing was watching.
        #
        # This is the one moment the repair is safe to run unattended: the
        # session is closed, because we are the thing about to open it. The
        # doctor's own banner says to close it first, and here that is
        # guaranteed rather than requested.
        #
        # Safe in both directions. Going to a gateway there is nothing to
        # remove -- every block Anthropic wrote is signed -- so it is a
        # no-op. Going back to Anthropic it removes exactly the blocks that
        # would otherwise fail the conversation permanently. The doctor
        # keeps <file>.orig (never overwritten) and a .bak per run, and
        # refuses to install anything that does not parse or that breaks the
        # uuid chain.
        if [ "${WC_CLAUDE_NO_REPAIR:-}" = "1" ]; then
            echo "wc-claude: '$last' -> '$MODEL'; repair skipped " \
                 "(WC_CLAUDE_NO_REPAIR=1)" >&2
        else
            echo "wc-claude: '$last' -> '$MODEL' — checking the transcript" >&2
            if ! "$HERE/bin/claude-transcript-doctor.py" --fix "$RESUME_NAME" >&2; then
                cat >&2 <<'WARN'
wc-claude: the transcript repair did not succeed. Starting anyway, but if this
           conversation was written by a different provider the API may refuse
           it. Run bin/claude-transcript-doctor.py --fix <name> by hand.
WARN
            fi
        fi
    fi
}

# Writes PROVIDER BASE_URL API_KEY MODEL (four fields; NAME is cosmetic and
# excluded) to FILE, atomically. This is the single source the poller compares
# against, written by the main script whenever it accepts a new resolution.
write_backend_state() {
    local file="$1"
    printf '%s\t%s\t%s\t%s\n' "$PROVIDER" "$BASE_URL" "$API_KEY" "$MODEL" \
        > "${file}.tmp"
    mv "${file}.tmp" "$file"
}

# Forks a background poller comparing query_backend's output against FILE
# every WC_CLAUDE_POLL_S seconds (default 5), signalling TARGET_PID with
# SIGUSR1 on a real, non-empty change. Sets global POLLER_PID.
start_poller() {
    local file="$1" target="$2"
    (
        while sleep "${WC_CLAUDE_POLL_S:-5}"; do
            current="$(query_backend | cut -f1-4)"
            last="$(cat "$file" 2>/dev/null || true)"
            # query_backend returns tab-separated when there's an active machine,
            # but "- - - - -" (space-separated) when there isn't. Extract provider
            # carefully to handle both formats.
            if [[ "$current" == *$'\t'* ]]; then
                provider="$(printf '%s' "$current" | cut -f1)"
            else
                # No tabs means "- - - - -" (no active machine), so provider is "-"
                provider="-"
            fi
            if [ "$provider" != "-" ] && [ "$current" != "$last" ]; then
                kill -USR1 "$target" 2>/dev/null || true
            fi
        done
    ) &
    POLLER_PID=$!
}

stop_poller() {
    [ -n "${POLLER_PID:-}" ] && kill "$POLLER_PID" 2>/dev/null || true
}

detect_resume_name "$@"

if [ ! -f "$DB" ]; then
    echo "wc-claude: no WebConsole database at $DB — starting claude unchanged" >&2
    exec claude "$@"
fi

resolve_backend

if [ "$PROVIDER" = "-" ]; then
    echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
    exec claude "$@"
fi

apply_env
build_model_args "$@"
check_transcript_doctor

echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2

# WC_CLAUDE_DRY_RUN=1 reports what would happen and exits, so the resolution can
# be checked without starting a session or burning a turn. Secrets are reported
# as set/unset and by length, never printed.
if [ "${WC_CLAUDE_DRY_RUN:-}" = "1" ]; then
    echo "would exec: claude ${MODEL_ARGS[*]} $*"
    for v in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN \
             CLAUDE_CODE_SIMPLE; do
        val="${!v-}"
        if [ -n "$val" ]; then
            case "$v" in
                *KEY*|*TOKEN*) echo "  $v = <set, ${#val} chars>" ;;
                *) echo "  $v = $val" ;;
            esac
        else
            echo "  $v = (unset)"
        fi
    done
    exit 0
fi

HOTSWAP=0
if [ "${WC_CLAUDE_HOTSWAP:-}" = "1" ] && [ -n "$RESUME_NAME" ]; then
    HOTSWAP=1
fi

if [ "$HOTSWAP" != "1" ]; then
    exec claude "${MODEL_ARGS[@]}" "$@"
fi

# ---- Supervised loop: claude runs as a child, this process stays resident ----
#
# Never exec here. screen/tmux track the pty, not the pid inside it; whatever
# is exec'd becomes the pty's only foreground process, and killing it to swap
# backends would leave nothing in the window. Running claude as a child keeps
# this script itself resident in the pty for the whole session.
STATE_FILE="$(mktemp)"
cleanup() {
    stop_poller
    # Bash does not signal background jobs just because the shell itself was
    # signalled -- without this, killing the wrapper (e.g. closing the
    # screen/tmux window) orphans the running claude child instead of ending
    # the session. Same grace-then-kill sequence as the main loop's own
    # restart path: a bare SIGTERM with no wait can cut the child off mid-
    # flush of the transcript --resume depends on -- exactly the loss this
    # discipline exists to prevent. A trap's EXIT handler can safely sleep
    # here; it is not itself interrupted by the signal already in flight.
    if [ -n "${CHILD:-}" ]; then
        kill -TERM "$CHILD" 2>/dev/null || true
        for _ in 1 2 3 4; do
            kill -0 "$CHILD" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "$CHILD" 2>/dev/null && kill -KILL "$CHILD" 2>/dev/null || true
    fi
    rm -f "$STATE_FILE"
}
trap cleanup EXIT

write_backend_state "$STATE_FILE"

RESTART=0
trap 'RESTART=1' USR1
start_poller "$STATE_FILE" "$$"

while true; do
    claude "${MODEL_ARGS[@]}" "$@" &
    CHILD=$!

    while true; do
        RESTART=0
        # `wait` returns 128+signum when it is interrupted by our own trapped
        # SIGUSR1, and that non-zero status would trip `set -e` and abort the
        # whole wrapper right here -- before RESTART is ever checked -- if it
        # were a bare statement. Using it as an `if` condition is the
        # standard way to read $? without tripping errexit.
        if wait "$CHILD"; then
            STATUS=0
        else
            STATUS=$?
        fi
        if kill -0 "$CHILD" 2>/dev/null; then
            # The child is still alive: wait was interrupted by our own
            # trapped SIGUSR1, not by the child exiting.
            if [ "$RESTART" = "1" ]; then
                resolve_backend
                # Bare command substitutions under `set -e`: a query_backend
                # failure (sqlite/python error, DB replaced under a long
                # session) or a vanished state file must not kill the whole
                # wrapper. The poller already reads the same two things
                # defensively; match it here. An unreadable state counts as
                # "no confirmed change yet" -- loop back and keep waiting on
                # the current child.
                if ! NEW_STATE="$(query_backend | cut -f1-4)"; then
                    continue
                fi
                OLD_STATE="$(cat "$STATE_FILE" 2>/dev/null || true)"
                if [ "$NEW_STATE" = "$OLD_STATE" ]; then
                    # False alarm (e.g. two rapid ticks collapsed into one
                    # signal) -- the child is fine, keep waiting on it.
                    continue
                fi
                break
            fi
            continue
        else
            # The child exited on its own (user ran /exit, Ctrl-D, or it
            # crashed). That is not ours to override.
            exit "$STATUS"
        fi
    done

    # A real change was confirmed above: bring the child down cleanly.
    # --resume <name> is already in "$@" and does not change across a
    # hot-swap, only the environment does -- UNLESS the confirmed change was
    # a deactivation to no active machine at all, handled below.
    kill -TERM "$CHILD" 2>/dev/null || true
    for _ in 1 2 3 4; do
        kill -0 "$CHILD" 2>/dev/null || break
        sleep 0.5
    done
    kill -0 "$CHILD" 2>/dev/null && kill -KILL "$CHILD" 2>/dev/null || true
    wait "$CHILD" 2>/dev/null || true

    # CONTROLLER RULING (task-2 review found the underlying cause; this
    # closes the matching gap in this loop): the poller's own guard refuses
    # to signal a transition INTO "no active machine" -- see start_poller's
    # `[ "$provider" != "-" ]` check -- so this branch is unreachable via a
    # single clean tick. It remains reachable via a race: two DB writes
    # landing within roughly one poll interval (active -> different active
    # -> deactivated) can still resolve here with PROVIDER="-" once
    # resolve_backend re-reads the row fresh, above. Restarting into that
    # state with no ANTHROPIC_*/MODEL_ARGS would silently fall through to
    # the CLI's own bare default -- exactly what the two startup guards
    # above (no DB file / no active machine) exist to avoid announcing
    # loudly instead of doing quietly. Same treatment here: stop supervising
    # and hand off to a plain, unmodified exec, the same as those two
    # existing fallbacks.
    if [ "$PROVIDER" = "-" ]; then
        # exec discards this process's background children (the poller would
        # survive, reparented to pid 1, still polling and still holding this
        # pid as a signal target it no longer owns) and its own EXIT trap
        # (STATE_FILE would never be removed) -- clean both up by hand before
        # handing off. apply_env is idempotent and already has the branch for
        # PROVIDER="-" that unsets everything: without it, the previous
        # active machine's ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY (exported by
        # this same loop, earlier) would survive the exec and leak into the
        # child that this banner claims is "unchanged".
        stop_poller
        rm -f "$STATE_FILE"
        apply_env
        echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
        exec claude "$@"
    fi

    apply_env
    build_model_args "$@"
    check_transcript_doctor
    echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2
    write_backend_state "$STATE_FILE"
done
