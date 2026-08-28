#!/usr/bin/env bash
# Launch WebConsole with HTTPS on the Tailscale IP using Tailscale's managed CA cert.
#
# Usage: ./launch.sh [direct|proxy]
#   direct  — WebConsole runs Claude Code locally (PROXY_ENABLED=0)
#   proxy   — WebConsole talks to host-side claude_proxy.py (default)

set -euo pipefail
cd "$(dirname "$0")"

export MODE="${1:-proxy}"

# ── Tailscale domain detection ────────────────────────────────────────
TAILNET_DOMAIN="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["Self"]["DNSName"])' 2>/dev/null)"
TAILNET_IP="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["TailscaleIPs"][0])' 2>/dev/null)"

if [ -z "${TAILNET_DOMAIN}" ] || [ -z "${TAILNET_IP}" ]; then
    echo "ERROR: Cannot determine Tailscale domain/IP."
    echo "Ensure you are connected to Tailscale."
    exit 1
fi

# ── Environment defaults ──────────────────────────────────────────────
export WC_DB_PATH="${WC_DB_PATH:-/home/kali/projects/claude-code-webconsole/data/webconsole.db}"
export WC_PROJECTS_ROOT="${WC_PROJECTS_ROOT:-/home/kali/projects/cweb3}"
export WC_LISTEN_HOST="${TAILNET_IP}"
export WC_PORT=443
export WC_COOKIE_ALLOW_INSECURE="0"

# Session secret
if [ -z "${WC_SESSION_SECRET:-}" ]; then
    export WC_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

# ── Proxy mode config ─────────────────────────────────────────────────
if [ "$MODE" = "proxy" ]; then
    export WC_PROXY_ENABLED=1
    if [ -z "${WC_PROXY_TOKEN:-}" ]; then
        export WC_PROXY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    fi
else
    export WC_PROXY_ENABLED=0
fi

# ── Cert paths ────────────────────────────────────────────────────────
CERT_DIR="$HOME/.local/share/webconsole/certs"
CERT_FILE="$CERT_DIR/fullchain.pem"
KEY_FILE="$CERT_DIR/key.pem"

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "Certificate not found. Generating with Tailscale CA..."
    sudo tailscale cert "$TAILNET_DOMAIN" \
        --cert-file "$CERT_FILE" \
        --key-file "$KEY_FILE"
    sudo chown "$USER" "$CERT_FILE" "$KEY_FILE"
fi

echo "=== WebConsole v0.3.0 ==="
echo "  listen   : https://${TAILNET_IP}:443"
echo "  tailnet  : ${TAILNET_DOMAIN}"
echo "  mode     : ${MODE}"
echo "  db       : ${WC_DB_PATH}"
echo "  cert     : ${CERT_FILE}"
echo ""

# Kill any previous instance
pkill -f "uvicorn app:app" 2>/dev/null || true
sleep 0.5

# ── Launch WebConsole with HTTPS ──────────────────────────────────────
python3 -m uvicorn app:app \
    --host "${TAILNET_IP}" \
    --port 443 \
    --ssl-certfile "$CERT_FILE" \
    --ssl-keyfile "$KEY_FILE" \
    --log-level info &

WC_PID=$!
echo "  PID      : ${WC_PID}"
echo "  URL      : https://${TAILNET_DOMAIN}"
echo ""

# ── Cleanup trap ──────────────────────────────────────────────────────
trap 'echo "Shutting down..."; kill "$WC_PID" 2>/dev/null; exit 0' INT TERM
wait