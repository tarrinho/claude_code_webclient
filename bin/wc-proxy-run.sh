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

# exec, so the supervisor watches the proxy itself rather than this wrapper --
# otherwise a crashed proxy leaves a live shell and looks healthy.
exec python3 claude_proxy.py \
    --host "${WC_PROXY_HOST:-127.0.0.1}" \
    --port "${WC_PROXY_PORT:-9000}"
