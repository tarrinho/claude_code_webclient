#!/usr/bin/env bash
# Run the test suite on a remote transport instead of this host.
#
# Usage: bin/wc-run-suite-remote.sh [transport-name]
#
# Cannot open its own SSH connection to the transport: exec_command,
# open_sftp and sync_transport are all bound to the running
# webconsole.service process's own in-memory tunnel_manager._STATE, which
# a standalone script has no access to (spec §4). So this is an HTTP
# client only -- it mints a short-lived API token and POSTs to the running
# server's own /api/qa/run, printing the streamed response as it arrives.
#
# Token expiry is pinned to 1 day rather than left at wc-token.py's
# no-expiry default: a token left in a script's environment or a stray log
# line should be a bounded exposure, not a standing one (spec §7). Note
# what this does NOT do: scope the token to only this route -- no
# per-route scoping mechanism exists in this codebase today, so the token
# carries whatever role WC_QA_USER has, same as every other wc-token.py
# consumer.
#
# Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${WC_BASE_URL:-}"
if [ -z "$BASE_URL" ]; then
  # Mirrors launch.sh's own tailnet lookup exactly (launch.sh:14-15) --
  # this deployment's uvicorn binds only its Tailscale IP on 443 with a
  # Tailscale-issued cert, never a plain localhost port. A hardcoded
  # localhost:8080 default here connected to nothing: nothing listens
  # there, and Caddy (which would front that port) is not running on
  # this host.
  TAILNET_DOMAIN="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["Self"]["DNSName"])' 2>/dev/null)"
  if [ -n "$TAILNET_DOMAIN" ]; then
    BASE_URL="https://${TAILNET_DOMAIN%.}"
  fi
fi
[ -n "$BASE_URL" ] || {
  echo "wc-run-suite-remote: could not determine this server's URL (no WC_BASE_URL and tailscale lookup failed) -- set WC_BASE_URL explicitly" >&2
  exit 1
}
QA_USER="${WC_QA_USER:-admin}"

TOKEN_FILE="$(mktemp)"
CURL_CONFIG="$(mktemp)"
trap 'rm -f "$TOKEN_FILE" "$CURL_CONFIG"' EXIT

bin/wc-token.py create --user "$QA_USER" --name "qa-run-$(date +%s)" \
  --days 1 --out "$TOKEN_FILE" >&2
TOKEN="$(cat "$TOKEN_FILE")"

# Token goes to curl via a -K config file, never a -H argv argument: argv
# is world-readable through /proc/<pid>/cmdline for the run's whole
# duration -- see CLAUDE.md §0.1 for the same exposure documented for
# backend credentials elsewhere in this codebase.
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$CURL_CONFIG"

BODY='{}'
if [ $# -ge 1 ]; then
  BODY=$(.venv/bin/python -c 'import json,sys; print(json.dumps({"transport": sys.argv[1]}))' "$1")
fi

echo "wc-run-suite-remote: POSTing to $BASE_URL/api/qa/run" >&2

curl -N -sS -X POST "$BASE_URL/api/qa/run" \
  -K "$CURL_CONFIG" \
  -H "Content-Type: application/json" \
  -d "$BODY" | while IFS= read -r line; do
    case "$line" in
      data:*) echo "${line#data: }" ;;
    esac
  done

echo "wc-run-suite-remote: connection closed" >&2
echo "wc-run-suite-remote: if the run did not report run-done above, it was" >&2
echo "  interrupted (e.g. a webconsole.service restart) -- re-run to retry;" >&2
echo "  there is no resume-in-place (spec §6)." >&2
