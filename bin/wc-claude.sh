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
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${WC_DB_PATH:-$HERE/data/webconsole.db}"

if [ ! -f "$DB" ]; then
    echo "wc-claude: no WebConsole database at $DB — starting claude unchanged" >&2
    exec claude "$@"
fi

# Read-only, always. Opening this database read-write from a second process is
# what took production's write path down for 37 minutes (registry #41): the
# server's connection could never upgrade its transaction.
#
# Tab-separated on one line so the shell can split it without eval.
read -r PROVIDER BASE_URL API_KEY MODEL NAME <<<"$(python3 - "$DB" <<'PY'
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

if row is None:
    print("- - - - -")
else:
    # The machine's own model first, then the global default -- the same order
    # runner.get_default_model uses once a chat pins nothing.
    model = (row["model"] or "").strip() or (
        (setting["value"] if setting else "") or "").strip()
    print("\t".join(clean(x) for x in (
        row["provider"], row["base_url"], row["api_key"], model, row["name"])))
PY
)"

if [ "$PROVIDER" = "-" ]; then
    echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
    exec claude "$@"
fi

# Mirror claude_proxy._backend_env exactly, including what it *removes*. A
# non-anthropic backend must not inherit Anthropic variables from this shell,
# and a machine with no base_url means "the official API" — leaving an inherited
# one there is registry #68, the bug whose workaround was to unset these by hand.
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
    # No key: the CLI must fall back to the host's own login, which it will not
    # do while CLAUDE_CODE_SIMPLE is set.
    unset ANTHROPIC_API_KEY
    unset CLAUDE_CODE_SIMPLE
fi

# --model only if you did not pass one.
want_model=1
for arg in "$@"; do
    case "$arg" in
        --model|--model=*) want_model=0 ;;
    esac
done

MODEL_ARGS=()
if [ "$want_model" = 1 ] && [ "$MODEL" != "-" ]; then
    MODEL_ARGS=(--model "$MODEL")
fi

# Switching a session between providers is what poisons a transcript: a thinking
# block carries a provider-specific signature, and the other provider rejects the
# whole conversation from then on. WebConsole repairs that before every turn; a
# terminal session has nothing doing it. So warn before resuming a session whose
# last turn ran on a different model, and say what to do about it.
for i in $(seq 1 $#); do
    if [ "${!i}" = "--resume" ]; then
        j=$((i + 1))
        target="${!j:-}"
        [ -z "$target" ] && break
        last="$(python3 - "$target" <<'PY' 2>/dev/null || true
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
                if ! "$HERE/bin/claude-transcript-doctor.py" --fix "$target" >&2; then
                    cat >&2 <<'WARN'
wc-claude: the transcript repair did not succeed. Starting anyway, but if this
           conversation was written by a different provider the API may refuse
           it. Run bin/claude-transcript-doctor.py --fix <name> by hand.
WARN
                fi
            fi
        fi
        break
    fi
done

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

exec claude "${MODEL_ARGS[@]}" "$@"
