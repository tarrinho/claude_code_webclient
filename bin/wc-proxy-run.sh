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

# Resolve the Claude binary to an absolute path -- see wc-resolve-claude-path.sh
# for why this is a separate sourced file rather than inline here (launch.sh
# needs the identical resolution now too, for the machine Test button).
# shellcheck source=bin/wc-resolve-claude-path.sh
. "$(dirname "$0")/wc-resolve-claude-path.sh"

# Resolve the interpreter the same way launch.sh does. claude_proxy.py imports
# only stdlib and backend_env today, so a bare python3 happens to work -- but
# that is luck, not design, and it is exactly the luck that ran out for
# launch.sh on 2026-09-12 when a new dependency landed in .venv and the service
# spent two and a half hours failing to start. The proxy is on the turn hot
# path, so the same failure here takes every turn with it, not just the web UI.
# shellcheck source=bin/wc-resolve-python.sh
. "$(dirname "$0")/wc-resolve-python.sh"

# exec, so the supervisor watches the proxy itself rather than this wrapper --
# otherwise a crashed proxy leaves a live shell and looks healthy.
exec "$WC_PYTHON" claude_proxy.py \
    --host "${WC_PROXY_HOST:-127.0.0.1}" \
    --port "${WC_PROXY_PORT:-9000}"
