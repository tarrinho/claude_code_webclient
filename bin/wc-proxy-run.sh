#!/usr/bin/env bash
# Run claude_proxy.py in the foreground, for a supervisor to own.
#
# The proxy has no supervision of its own. That has caused three separate
# "the feature does nothing" bugs, each presenting with no error anywhere:
# a token mismatch, a missing ANTHROPIC_BASE_URL, and usage accounting that
# recorded nothing. In every case the process was serving code from whenever
# it happened to be started while the file on disk had moved on.
set -euo pipefail
cd "$(dirname "$0")/.."

export WC_DB_PATH="${WC_DB_PATH:-$PWD/data/webconsole.db}"
# shellcheck source=bin/wc-proxy-token.sh
. bin/wc-proxy-token.sh
wc_resolve_proxy_token

# Resolve the Claude binary to an absolute path, because systemd --user starts
# this with a PATH that does not include ~/.local/bin -- where the CLI actually
# lives. Without it `shutil.which("claude")` in claude_proxy.py returns None and
# EVERY turn dies on "claude binary not found", which the web UI surfaces only
# as a failed turn.
#
# WC_CLAUDE_PATH rather than an `export PATH=` prefix, deliberately: the value
# is then independent of PATH ordering, and claude_proxy.py already reads it
# (`os.environ.get("WC_CLAUDE_PATH", "claude")`), so this needs no change there.
#
# This is the second time this fix has been made. The first was an
# `export PATH=...` line that lived only in the shared working tree, was never
# committed, and was silently reverted by another session's checkout -- so the
# regression lay dormant until the next proxy restart and then broke every turn
# with no error anywhere. Registry #49 and #59. Pinned by
# tests/test_qa_proxy_claude_path.py so a third loss fails the suite instead of
# the site.
# The markers are not decoration: tests/test_qa_proxy_claude_path.py lifts
# everything between them and executes it under a minimal PATH, the way systemd
# starts this. Registry #48 is the reason -- nine tests there read a shell block
# and none ran it, so a block that could never succeed passed every one.
# >>> claude-path-block
if [ -z "${WC_CLAUDE_PATH:-}" ]; then
    for _candidate in \
        "$HOME/.local/bin/claude" \
        "/usr/local/bin/claude" \
        "/usr/bin/claude"
    do
        if [ -x "$_candidate" ]; then
            WC_CLAUDE_PATH="$_candidate"
            break
        fi
    done
    # Last resort: whatever PATH can find, so a machine that installs the CLI
    # somewhere else still starts rather than refusing to run.
    : "${WC_CLAUDE_PATH:=$(command -v claude || true)}"
    export WC_CLAUDE_PATH
fi
if [ -z "${WC_CLAUDE_PATH:-}" ] || [ ! -x "$WC_CLAUDE_PATH" ]; then
    # Said loudly at startup rather than discovered one failed turn at a time.
    echo "WARNING: no executable claude binary found (WC_CLAUDE_PATH='${WC_CLAUDE_PATH:-}')." >&2
    echo "         Every turn will fail with 'claude binary not found'." >&2
fi
# <<< claude-path-block

# exec, so the supervisor watches the proxy itself rather than this wrapper --
# otherwise a crashed proxy leaves a live shell and looks healthy.
exec python3 claude_proxy.py \
    --host "${WC_PROXY_HOST:-127.0.0.1}" \
    --port "${WC_PROXY_PORT:-9000}"
