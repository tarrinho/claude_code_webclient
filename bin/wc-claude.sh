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

# The real CLI binary, resolved by path rather than by name.
#
# Required because ~/.local/bin/claude is now a shim that execs this script:
# invoking `claude` here would re-enter the shim and recurse until the process
# limit. The shim exists so that shells started before the alias was added --
# cweb2's parent bash has been running since Aug 31 -- still get routed, since
# bash caches the resolved *path* of a command and that path is unchanged.
#
# Resolution order, most explicit first. Each candidate is checked for the shim
# marker, so a mistake anywhere in this chain fails loudly instead of forking
# forever.
CLAUDE_SHIM_MARKER="wc-claude-shim-do-not-exec-from-wrapper"

_is_shim() {
    [ -f "$1" ] && grep -qF "$CLAUDE_SHIM_MARKER" "$1" 2>/dev/null
}

resolve_claude_bin() {
    local candidate
    for candidate in \
        "${WC_CLAUDE_PATH:-}" \
        "$HOME/.local/bin/claude-real" \
        "$(ls -1d "$HOME"/.local/share/claude/versions/* 2>/dev/null | sort -V | tail -1)" \
        "$(command -v claude 2>/dev/null || true)"
    do
        [ -n "$candidate" ] || continue
        [ -x "$candidate" ] || continue
        _is_shim "$candidate" && continue
        CLAUDE_BIN="$candidate"
        return 0
    done
    echo "wc-claude: cannot find the real claude binary." >&2
    echo "  Looked at: \$WC_CLAUDE_PATH, ~/.local/bin/claude-real," >&2
    echo "  the newest ~/.local/share/claude/versions/*, and PATH." >&2
    echo "  Every candidate was missing, not executable, or was this wrapper's" >&2
    echo "  own shim -- exec'ing that would recurse." >&2
    exit 127
}
resolve_claude_bin

# ── Memory admission ───────────────────────────────────────────────────
# Interactive agents are the dominant consumer on this host: measured
# 2026-09-08, seven of them held 1884 MB of the 2520 MB that claude processes
# were using, while console-spawned turns held 3 MB. config.MAX_CONCURRENT and
# claude_proxy's own limit gate only that 3 MB, which is why neither has ever
# prevented the box from going into swap and taking the webconsole with it.
#
# Checked once here, at the top, and deliberately NOT inside the hot-swap loop
# further down: that loop relaunches a session that is already running and
# already holds its memory, so refusing there would kill a live session to save
# memory it was not about to allocate.
#
# See docs/superpowers/specs/2026-09-08-resource-guard-design.md. Refusal is
# hard -- no prompt, no y/N -- and WC_RESOURCE_GUARD=off is the documented,
# logged way past it.
# This check WARNS. It does not block, and that is deliberate.
#
# It shipped as a hard refusal (exit 75). Pedro commented it out, and when it
# was restored with a lower floor he asked for the blockage removed outright --
# reaffirmed after the trade-off below was put to him, so it is a settled
# decision rather than an oversight. Do not turn it back into a refusal without
# asking him.
#
# The reasoning against blocking, which is sound: a refusal here stops the
# operator from starting the session they need in order to *fix* the overloaded
# host. It fails in the worst direction -- the fuller the box gets, the more
# certainly it locks you out of the one tool that could free it. A warning
# carries the same information and leaves the judgement with the human, who can
# see which sessions are disposable and which are mid-task; this script cannot.
#
# What is lost is real and worth stating: nothing now prevents an OOM kill of
# webconsole.service, which has happened before. The thresholds stay honest so
# the warning is worth reading -- re-measured 2026-09-08 across 6 live agents,
# mean RSS 337 MB, median 349 MB, so the previous 320 MB cost was an
# UNDER-estimate. On a 3816 MB host, six agents at ~340 MB leave no room for a
# seventh whatever any threshold says; when this warns, the honest fix is to
# close a session.
#
# WC_AGENT_COST_MB / WC_AGENT_FLOOR_MB tune the numbers.
# WC_RESOURCE_GUARD=off silences it entirely.
if ! guard_output="$(python3 -m resource_guard \
        --cost-mb "${WC_AGENT_COST_MB:-350}" \
        --floor-mb "${WC_AGENT_FLOOR_MB:-250}" 2>&1)"; then
    echo "wc-claude: warning — this host is short of memory; starting anyway." >&2
    printf '%s\n' "$guard_output" >&2
fi

# ── Be the OOM victim, so the webconsole is not ─────────────────────────
# The paragraph above ends on "nothing now prevents an OOM kill of
# webconsole.service, which has happened before". It happened again on
# 2026-09-10: SIGKILL twice in ten minutes, and each kill cold-starts the
# transcript caches, whose first scans spike memory and invite the next one.
#
# The kernel was picking the wrong process, and by a wide margin. Measured
# that morning: webconsole.service sat at oom_score 813 on 141 MB while seven
# of these CLIs sat at 698-709 on 167-366 MB each -- the process using the
# least memory was first in line, purely because the user manager's
# DefaultOOMScoreAdjust=200 applies to services and not to shell children.
#
# The service cannot fix this from its own side: a unit can ask for
# OOMScoreAdjust=0, but the user manager runs at adj 100 and lowering below
# its own value needs CAP_SYS_RESOURCE, so the kernel silently clamps it to
# 100 (813 -> 746, still above these). Raising *our own* score always works,
# so the correction lives here.
#
# The trade is deliberate and it is the right way round: a killed CLI resumes
# from its transcript, while a killed webconsole takes every session's UI down
# at once and starts the cold-start loop. Recoverable beats shared.
#
# Set once, at the top: oom_score_adj survives exec (so all four exec paths
# below are covered) and is inherited on fork (so the hot-swap loop's child
# is too). Console-spawned turns are unaffected -- they go through
# claude_proxy with -p and never reach this wrapper.
#
# WC_AGENT_OOM_ADJ=0 opts out. Failure is only possible somewhere without a
# writable /proc, and is never worth refusing a session over.
_wc_oom_adj="${WC_AGENT_OOM_ADJ:-200}"
if [ "$_wc_oom_adj" != "0" ]; then
    if ! printf '%s' "$_wc_oom_adj" > /proc/self/oom_score_adj 2>/dev/null; then
        echo "wc-claude: note — could not raise this session's OOM score;" \
             "webconsole.service stays the kernel's preferred victim." >&2
    fi
fi
unset _wc_oom_adj

# --wc-profile <name> pins this session to one backend for its whole life,
# instead of following whatever the console is currently routing to. Consumed
# here and removed from the arguments, because `claude` does not know the flag.
#
# Needed rather than nice: a model id is only meaningful against the backend
# serving it. The shell aliases c2..c6 pin gateway model ids (`azure_ai/...`,
# `vllm/...`) that exist on one backend only, so following the active machine
# would send them wherever the console happened to point and fail with a 429
# that reads as capacity rather than routing.
WC_PROFILE_REQUEST=""
_wc_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --wc-profile=*) WC_PROFILE_REQUEST="${1#--wc-profile=}"; shift ;;
        --wc-profile)
            WC_PROFILE_REQUEST="${2:-}"
            if [ -z "$WC_PROFILE_REQUEST" ]; then
                echo "wc-claude: --wc-profile needs a name" >&2
                exit 2
            fi
            shift 2
            ;;
        *) _wc_args+=("$1"); shift ;;
    esac
done
set -- ${_wc_args[@]+"${_wc_args[@]}"}

# The selector passed to bin/wc-backend-env.py, empty when following the active
# machine. Kept as an array so an empty value expands to no arguments at all.
WC_PROFILE_ARGS=()
if [ -n "$WC_PROFILE_REQUEST" ]; then
    WC_PROFILE_ARGS=(--profile "$WC_PROFILE_REQUEST")
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
    python3 "$HERE/bin/wc-backend-env.py" \
        ${WC_PROFILE_ARGS[@]+"${WC_PROFILE_ARGS[@]}"} --fields
}

# Sets globals PROVIDER BASE_URL API_KEY MODEL NAME from query_backend's output.
#
# The exit status is captured and checked rather than left to `set -e`. A
# failing helper prints its diagnosis to stderr and nothing to stdout, and
# `read ... <<<"$(...)"` on empty input still succeeds -- it reads one empty
# line -- so the failure produced five empty globals and a return of 0. What
# followed looked like a successful resolution of a nameless backend: the
# PROVIDER="-" guard below did not fire (the value was "" and not "-"),
# build_model_args appended a literal empty `--model ''`, and the session was
# launched unrouted with an argument the CLI rejects. That is the whole
# observed "cannot start on the Anthropic backend" failure, and every layer
# of it reported success.
resolve_backend() {
    local out status
    out="$(query_backend)" && status=0 || status=$?
    if [ "$status" != "0" ] || [ -z "${out//[[:space:]]/}" ]; then
        echo "wc-claude: could not resolve a backend (wc-backend-env.py exited" \
             "${status}). Refusing to start rather than launching unrouted." >&2
        exit "${status:-1}"
    fi
    read -r PROVIDER BASE_URL API_KEY MODEL NAME <<<"$out"
}

# The environment the active backend implies. One definition, shared with
# claude_proxy and runner via backend_env.deltas and emitted for eval by
# bin/wc-backend-env.py.
#
# This used to reimplement the rule in bash, under a comment reading "Mirror
# claude_proxy._backend_env exactly, including what it removes" -- which is an
# instruction to keep two files in step by hand, and they drifted. Putting the
# same machine records through both implementations found the direct runner
# exporting a whitespace-only key as the key, and crashing outright on a
# non-string base_url, in cases this bash version handled correctly.
#
# The removals are why this is eval'd rather than handed over as a dict: a
# wrapper cannot replace the interactive environment it was launched in, and an
# inherited ANTHROPIC_AUTH_TOKEN outranks the key we set, an inherited
# ANTHROPIC_BASE_URL sends turns to a gateway nobody selected (registry #68),
# and an inherited CLAUDE_CODE_SIMPLE stops the CLI reading the host login it
# has just been told to fall back to.
#
# The helper reads the database itself, so no credential passes through a
# command line: /proc/<pid>/cmdline is world-readable.
apply_env() {
    # Same reasoning as resolve_backend: the helper's exit status has to be
    # read before its output is applied. `eval "$(cmd)"` discards it, so a
    # helper that refused to resolve anything eval'd to nothing at all and
    # left the caller's own ANTHROPIC_* untouched -- the silent, undisclosed
    # credential change this whole wrapper exists to prevent.
    local sh status
    sh="$(python3 "$HERE/bin/wc-backend-env.py" \
        ${WC_PROFILE_ARGS[@]+"${WC_PROFILE_ARGS[@]}"} --sh)" && status=0 || status=$?
    if [ "$status" != "0" ]; then
        echo "wc-claude: wc-backend-env.py --sh exited ${status}; the backend" \
             "environment was not applied. Refusing to start." >&2
        exit "$status"
    fi
    eval "$sh"

    # The shared rule knows a keyless backend must not keep CLAUDE_CODE_SIMPLE;
    # it cannot know what value this shell started with. On a hot-swap onto a
    # backend that *does* have a key, restore the caller's original so the
    # session lands where a fresh launch on that backend would.
    if [ -n "${ANTHROPIC_API_KEY+x}" ] && [ "$CLAUDE_CODE_SIMPLE_WAS_SET" = "1" ]; then
        export CLAUDE_CODE_SIMPLE="$CLAUDE_CODE_SIMPLE_ORIG"
    fi
}

# The model the session will actually run with: the caller's --model if they
# gave one, otherwise the backend's own default. Sets global EFFECTIVE_MODEL.
detect_effective_model() {
    EFFECTIVE_MODEL=""
    local i j arg
    for i in $(seq 1 $#); do
        arg="${!i}"
        case "$arg" in
            --model=*) EFFECTIVE_MODEL="${arg#--model=}"; return ;;
            --model)   j=$((i + 1)); EFFECTIVE_MODEL="${!j:-}"; return ;;
        esac
    done
    [ "$MODEL" != "-" ] && EFFECTIVE_MODEL="$MODEL"
    return 0
}

# Refuse a model the resolved backend does not serve.
#
# This is the mistake the wrapper exists to prevent, and it is invisible without
# the check: a model id is only meaningful against the backend serving it.
# `vllm/Qwen3.6-35B-A3B-NVFP4` is real on the gateway and nonsense against
# api.anthropic.com; sent to the wrong one the gateway answers 429 "No
# deployments available for selected model", which reads as a capacity problem
# rather than a routing one. The shell aliases c2..c6 pinned exactly these
# model ids with no backend attached, so which one they reached depended on
# whichever machine happened to be active.
#
# Silent when the backend publishes no model list: guessing would block models
# that work, and a check that cries wolf gets switched off. WC_SKIP_MODEL_CHECK=1
# is the deliberate override, so this can never leave someone stuck.
check_model() {
    [ "${WC_SKIP_MODEL_CHECK:-0}" = "1" ] && return 0
    detect_effective_model "$@"
    [ -z "$EFFECTIVE_MODEL" ] && return 0

    if python3 "$HERE/bin/wc-backend-env.py" \
            ${WC_PROFILE_ARGS[@]+"${WC_PROFILE_ARGS[@]}"} \
            --check-model "$EFFECTIVE_MODEL" 2>/dev/null; then
        return 0
    fi

    # Not served by the backend we were going to use. If the caller named no
    # profile and exactly one backend declares this model, let the model choose
    # its backend rather than refusing.
    #
    # This is for shells that predate the alias. Their c2..c6 still expand to a
    # bare `--model azure_ai/...` with no backend named, and a running shell's
    # aliases cannot be rewritten from outside -- cweb2's parent bash has been
    # up since Aug 31. Refusing those would break a working habit to enforce a
    # rule the shell has no way to have heard about yet.
    #
    # Only when unambiguous. One backend declaring it is a fact; two would make
    # this a guess, and guessing sends a turn somewhere nobody chose.
    if [ -z "$WC_PROFILE_REQUEST" ]; then
        local inferred
        inferred="$(python3 "$HERE/bin/wc-backend-env.py" \
            --resolve-model "$EFFECTIVE_MODEL" 2>/dev/null || true)"
        if [ -n "$inferred" ]; then
            echo "wc-claude: $EFFECTIVE_MODEL is served by '$inferred', not by the" \
                 "active backend -- switching this session to it." >&2
            WC_PROFILE_REQUEST="$inferred"
            WC_PROFILE_ARGS=(--profile "$inferred")
            resolve_backend
            apply_env
            return 0
        fi
    fi

    # Nothing declares it: refuse, with the list of what the backend does serve.
    python3 "$HERE/bin/wc-backend-env.py" \
        ${WC_PROFILE_ARGS[@]+"${WC_PROFILE_ARGS[@]}"} \
        --check-model "$EFFECTIVE_MODEL" || true
    exit 1
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
    # An empty MODEL is rejected as hard as the "-" sentinel. `--model ''` is
    # not "no model": it is an option the CLI parses and refuses, so a
    # resolution that produced no model at all used to fail at launch with an
    # argument error rather than falling back to the backend's own default.
    if [ "$want_model" = 1 ] && [ "$MODEL" != "-" ] && [ -n "$MODEL" ]; then
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

# Stops supervising and hands off to a plain, unmanaged `claude`. Used only
# from inside the supervised loop, when a hot-swap restart resolves into "no
# active machine" -- reachable only via a race: two DB writes landing within
# roughly one poll interval (active -> a different active -> deactivated) can
# leave resolve_backend seeing "no active machine" by the time this process
# reacts to a signal the poller sent for a perfectly real, different change.
#
# exec discards this process's background children -- the poller would
# otherwise survive, reparented to pid 1, still polling and still holding
# this pid as a signal target it no longer owns -- and discards this
# process's own EXIT trap, so STATE_FILE would otherwise never be removed.
# Both are cleaned up by hand here rather than left to exec. apply_env is
# called once more so the machine that just went inactive does not leave its
# own credentials -- exported by this same script, earlier in this loop --
# in the child this hands off to.
#
# Not used by either startup guard, deliberately: at startup there is nothing
# of this script's own to strip yet, so calling apply_env there would instead
# unset whatever ANTHROPIC_*/CLAUDE_CODE_SIMPLE the caller's own shell had
# already set -- the exact silent credential change this feature exists to
# prevent.
handoff_unmanaged() {
    stop_poller
    rm -f "${STATE_FILE:-}"
    apply_env
    echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
    exec "$CLAUDE_BIN" "$@"
}

detect_resume_name "$@"

if [ ! -f "$DB" ]; then
    echo "wc-claude: no WebConsole database at $DB — starting claude unchanged" >&2
    exec "$CLAUDE_BIN" "$@"
fi

resolve_backend

if [ "$PROVIDER" = "-" ] || [ -z "$PROVIDER" ]; then
    # Not handoff_unmanaged: this is the very first thing the script does
    # after resolve_backend, so there is nothing of this script's own to
    # strip from the environment yet -- calling apply_env here would unset
    # whatever ANTHROPIC_*/CLAUDE_CODE_SIMPLE the caller's own shell had
    # already set, which is exactly the silent, undisclosed credential
    # change this feature exists to prevent. handoff_unmanaged's apply_env
    # call is only correct at its mid-loop call site, where it is undoing
    # this same script's own earlier export for a machine that just went
    # inactive.
    echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
    exec "$CLAUDE_BIN" "$@"
fi

apply_env
build_model_args "$@"
check_model "$@"
check_transcript_doctor

echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2

# WC_CLAUDE_DRY_RUN=1 reports what would happen and exits, so the resolution can
# be checked without starting a session or burning a turn. Secrets are reported
# as set/unset and by length, never printed.
if [ "${WC_CLAUDE_DRY_RUN:-}" = "1" ]; then
    echo "would exec: $CLAUDE_BIN ${MODEL_ARGS[*]} $*"
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
    exec "$CLAUDE_BIN" "${MODEL_ARGS[@]}" "$@"
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
    "$CLAUDE_BIN" "${MODEL_ARGS[@]}" "$@" <&0 &
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
    if [ "$PROVIDER" = "-" ] || [ -z "$PROVIDER" ]; then
        handoff_unmanaged "$@"
    fi

    apply_env
    build_model_args "$@"
    check_model "$@"
    check_transcript_doctor
    echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2
    write_backend_state "$STATE_FILE"
done
