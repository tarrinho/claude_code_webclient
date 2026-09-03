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
# This resolves the same machine the proxy would and exports the same variables
# the proxy would. Use it instead of `claude`:
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
# By default this just execs claude once and gets out of the way (the "single
# exec" path, near the bottom of this file). Set WC_CLAUDE_HOTSWAP=1 together
# with --resume <name> and it instead runs a supervised loop: claude runs as a
# child of this script rather than replacing it, a background poller (see
# start_poller) checks the active machine every WC_CLAUDE_POLL_S seconds
# (default 5) against the database, and a real change kills and relaunches the
# child under the same --resume target with the new backend's environment --
# all without this script itself ever exiting, so the screen/tmux window
# stays alive across the swap (screen/tmux track the pty, not the pid inside
# it, and exec'ing a replacement claude would leave nothing else in the
# window if it needed to be killed to swap backends).
#
# Known limitation: under WC_CLAUDE_HOTSWAP=1, Ctrl-C currently ends the whole
# supervised session rather than interrupting only the running turn.
# Backgrounding a job in a script with no job control (`set -m` is not used
# here) marks SIGINT/SIGQUIT as ignored for that child, so claude can never
# see Ctrl-C; this script has no INT trap of its own, so Ctrl-C kills this
# script instead, and its EXIT trap then tears the child down too. Hosting an
# interactive TUI as a properly signal-transparent supervised child needs
# process-group and controlling-terminal management this script does not do
# (or a structural pivot to driving the restart through `screen -X`/`tmux
# respawn-pane` instead of a bash child). Tracked as a known gap, not fixed
# here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${WC_DB_PATH:-$HERE/data/webconsole.db}"

# Captured once, before apply_env ever runs, so apply_env's has-key branch can
# restore the caller's own CLAUDE_CODE_SIMPLE rather than leaving it unset
# forever after the first machine with no key -- see apply_env below.
if [ -n "${CLAUDE_CODE_SIMPLE+x}" ]; then
    CLAUDE_CODE_SIMPLE_WAS_SET=1
    CLAUDE_CODE_SIMPLE_ORIG="$CLAUDE_CODE_SIMPLE"
else
    CLAUDE_CODE_SIMPLE_WAS_SET=0
fi

# Sets global RESUME_NAME to the value following --resume (or after the "="
# in --resume=<name>) in "$@", or "". Used both for the transcript-doctor
# mismatch check and to gate hot-swap -- build_model_args already handles
# both forms for --model, so --resume=<name> silently getting no hot-swap
# would be a real gap now that RESUME_NAME gates the whole feature.
detect_resume_name() {
    RESUME_NAME=""
    local i j arg
    for i in $(seq 1 $#); do
        arg="${!i}"
        case "$arg" in
            --resume=*)
                RESUME_NAME="${arg#--resume=}"
                return
                ;;
            --resume)
                j=$((i + 1))
                RESUME_NAME="${!j:-}"
                return
                ;;
        esac
    done
}

# Read-only, always. Opening this database read-write from a second process is
# what took production's write path down for 37 minutes (registry #41): the
# server's connection could never upgrade its transaction.
#
# Pure: prints one tab-separated line, sets nothing. Called by resolve_backend
# on startup and by the poller on a timer -- one query definition, not two
# copies to keep in sync.
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
    # Tab-joined like the real-row case below, not space-joined -- a caller
    # that blindly does `cut -f1` (tab-delimited by default) must get the
    # same field shape whether or not a machine is active. This file has
    # already been bitten twice by a space/tab mismatch here.
    print("\t".join(["-"] * 5))
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
        # Restore whatever CLAUDE_CODE_SIMPLE the caller originally had (it
        # may have been unset by an earlier call to this same function, on a
        # previous hot-swap restart, for a different, keyless machine) rather
        # than leaving it unset from here on. Without this, a hot-swap from a
        # keyless anthropic machine to one with a key would land somewhere a
        # fresh launch on that same backend never would.
        if [ "$CLAUDE_CODE_SIMPLE_WAS_SET" = "1" ]; then
            export CLAUDE_CODE_SIMPLE="$CLAUDE_CODE_SIMPLE_ORIG"
        fi
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
# re-scanning "$@" -- this runs again on every hot-swap restart, and the
# resume target never changes across those restarts.
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

# sha256 of a single, already-unambiguous string (typically a tab-joined
# field list, where the tab guarantees no two distinct field combinations can
# collide the way naive concatenation could). Used so that neither the state
# file nor the poller's own working state ever needs to hold a live API key.
backend_digest() {
    printf '%s' "$1" | sha256sum | cut -d' ' -f1
}

# Writes a digest of PROVIDER BASE_URL API_KEY MODEL (four fields; NAME is
# cosmetic and excluded) to FILE, atomically. This is the single source the
# poller compares against, written by the main script whenever it accepts a
# new resolution. A digest, not the raw fields: this file only ever needs to
# answer "did the backend change", never "to what", and the raw fields would
# put a live API key on disk -- in a temp file created under the process
# umask (more permissive than the 0600 mktemp itself would suggest, since
# `mv` within one filesystem is a rename that keeps the written file's own
# mode) and never cleaned up at all if the wrapper is ever SIGKILLed.
write_backend_state() {
    local file="$1"
    backend_digest "$(printf '%s\t%s\t%s\t%s' \
        "$PROVIDER" "$BASE_URL" "$API_KEY" "$MODEL")" > "${file}.tmp"
    mv "${file}.tmp" "$file"
}

# Forks a background poller comparing a digest of query_backend's output
# against FILE every WC_CLAUDE_POLL_S seconds (default 5), signalling
# TARGET_PID with SIGUSR1 on a real, non-empty change. Sets global
# POLLER_PID.
start_poller() {
    local file="$1" target="$2"
    (
        while sleep "${WC_CLAUDE_POLL_S:-5}"; do
            # A transient query_backend failure (sqlite/python error, DB
            # replaced under a long session) must not take the poller down
            # with it under `set -e` -- it would go silently dead for the
            # rest of the session, with nothing left to notice a real change
            # ever again. Skip this tick and try again next time.
            current="$(query_backend | cut -f1-4)" || continue
            # query_backend's sentinel for "no active machine" is tab-joined
            # the same as a real row, so the provider field is always at a
            # fixed position -- no format-sniffing needed here.
            provider="$(printf '%s' "$current" | cut -f1)"
            digest="$(backend_digest "$current")"
            last="$(cat "$file" 2>/dev/null || true)"
            if [ "$provider" != "-" ] && [ "$digest" != "$last" ]; then
                kill -USR1 "$target" 2>/dev/null || true
            fi
        done
    ) &
    POLLER_PID=$!
}

stop_poller() {
    [ -n "${POLLER_PID:-}" ] && kill "$POLLER_PID" 2>/dev/null || true
}

# Stops supervising and hands off to a plain, unmanaged `claude` -- used both
# at startup when there is no active machine to resolve, and mid-session when
# a hot-swap restart resolves into that same state. The mid-session case is
# reachable only via a race: two DB writes landing within roughly one poll
# interval (active -> a different active -> deactivated) can leave
# resolve_backend seeing "no active machine" by the time this process reacts
# to a signal the poller sent for a perfectly real, different change.
#
# exec discards this process's background children -- the poller would
# otherwise survive, reparented to pid 1, still polling and still holding
# this pid as a signal target it no longer owns -- and discards this
# process's own EXIT trap, so STATE_FILE would otherwise never be removed.
# Both are cleaned up by hand here rather than left to exec. apply_env is
# called once more so a machine that just went inactive does not leave its
# own credentials exported into the child this hands off to.
#
# Only safe to call once PROVIDER has actually been resolved to "-" by
# resolve_backend; the very first startup guard (no database file at all)
# calls plain `exec claude "$@"` instead, for exactly that reason.
handoff_unmanaged() {
    stop_poller
    rm -f "${STATE_FILE:-}"
    apply_env
    echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
    exec claude "$@"
}

detect_resume_name "$@"

if [ ! -f "$DB" ]; then
    echo "wc-claude: no WebConsole database at $DB — starting claude unchanged" >&2
    exec claude "$@"
fi

resolve_backend

if [ "$PROVIDER" = "-" ]; then
    handoff_unmanaged "$@"
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
        # Indirect expansion into a variable first. `${#!v}` is not valid bash --
        # it fails with "bad substitution" and, under `set -e`, took the rest of
        # this report with it. Length and indirection cannot be combined in one
        # expansion.
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
# `wait`'s own exit status when it is interrupted by our trapped SIGUSR1 --
# computed rather than hardcoded, though SIGUSR1 is signal 10 (status 138) on
# every Linux this runs on. Used below to tell "the child is still alive and
# we were woken by our own signal" apart from "the child actually exited"
# without relying on `kill -0`, which cannot distinguish "still alive" from
# "exited but not yet reaped by this shell" and could otherwise relaunch a
# session right as the user's own /exit lands in the same wait interruption.
USR1_WAIT_STATUS=$((128 + $(kill -l USR1)))
start_poller "$STATE_FILE" "$$"

while true; do
    # Explicit stdin redirection is required here, not decorative: bash
    # redirects a backgrounded command's stdin from /dev/null by default in a
    # script with no job control (`set -m` is not used here), so without
    # `<&0` claude would start with no terminal input at all and exit
    # immediately -- silently, since nothing about that failure is distinct
    # from a clean exit.
    claude "${MODEL_ARGS[@]}" "$@" <&0 &
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
        if [ "$STATUS" = "$USR1_WAIT_STATUS" ]; then
            # wait was interrupted by our own trapped SIGUSR1, not by the
            # child exiting.
            if [ "$RESTART" = "1" ]; then
                resolve_backend
                # A transient query_backend failure (sqlite/python error, DB
                # replaced under a long session) or a vanished state file
                # must not take the whole wrapper down under `set -e`.
                # Treat either as "no confirmed change yet" and keep waiting
                # on the current child, the same as the poller does for the
                # same failure.
                if ! RAW_STATE="$(query_backend | cut -f1-4)"; then
                    continue
                fi
                NEW_STATE="$(backend_digest "$RAW_STATE")"
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

    # The poller's own guard never signals a transition INTO "no active
    # machine" (see the `[ "$provider" != "-" ]` check in start_poller), so
    # this cannot happen from a single clean poll tick. It remains reachable
    # via a race: two DB writes landing within roughly one poll interval
    # (active -> a different active -> deactivated) can still resolve here
    # with PROVIDER="-" once resolve_backend re-read the row fresh, above.
    # Handling it exactly the way the startup guard for this same state
    # handles it is what stops it from silently falling through to the CLI's
    # own bare default with no ANTHROPIC_*/MODEL_ARGS at all.
    if [ "$PROVIDER" = "-" ]; then
        handoff_unmanaged "$@"
    fi

    apply_env
    build_model_args "$@"
    check_transcript_doctor
    echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2
    write_backend_state "$STATE_FILE"
done
