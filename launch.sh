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

# Session secret — persist to disk so it survives restarts
SESSION_SECRET_FILE="data/session_secret.txt"
if [ -z "${WC_SESSION_SECRET:-}" ] && [ -f "$SESSION_SECRET_FILE" ]; then
    export WC_SESSION_SECRET="$(cat "$SESSION_SECRET_FILE")"
elif [ -z "${WC_SESSION_SECRET:-}" ]; then
    mkdir -p data
    export WC_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    echo -n "$WC_SESSION_SECRET" > "$SESSION_SECRET_FILE"
fi

# ── Proxy mode config ─────────────────────────────────────────────────
# The proxy token must be identical in three places: this script, the
# claude_proxy.py process, and the app. The app resolves it from the DB
# because _load_settings_from_db() runs before config.validate(), so the DB
# wins over the environment — read it from there first. Generating a fresh
# token each launch (the old behaviour) guaranteed a handshake mismatch and
# every turn failed with "Connection lost during streaming".
PROXY_TOKEN_FILE="data/proxy_token.txt"
if [ "$MODE" = "proxy" ]; then
    export WC_PROXY_ENABLED=1
    if [ -z "${WC_PROXY_TOKEN:-}" ] && [ -f "$WC_DB_PATH" ]; then
        WC_PROXY_TOKEN="$(python3 -c "
import sqlite3, sys
try:
    con = sqlite3.connect('file:$WC_DB_PATH?mode=ro', uri=True)
    row = con.execute(\"SELECT value FROM settings WHERE key='proxy_token'\").fetchone()
    sys.stdout.write(row[0] if row and row[0] else '')
except Exception:
    pass" 2>/dev/null)"
    fi
    if [ -z "${WC_PROXY_TOKEN:-}" ] && [ -f "$PROXY_TOKEN_FILE" ]; then
        WC_PROXY_TOKEN="$(cat "$PROXY_TOKEN_FILE")"
    fi
    if [ -z "${WC_PROXY_TOKEN:-}" ]; then
        WC_PROXY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    fi
    export WC_PROXY_TOKEN
    mkdir -p data
    (umask 077; printf '%s' "$WC_PROXY_TOKEN" > "$PROXY_TOKEN_FILE")

    # Warn if a proxy is already listening with a token we cannot verify.
    if ss -tln 2>/dev/null | grep -q "127.0.0.1:${WC_PROXY_PORT:-9000}"; then
        echo "NOTE: a proxy is already listening on port ${WC_PROXY_PORT:-9000}."
        echo "      If it was started with a different WC_PROXY_TOKEN, every turn"
        echo "      will fail at the handshake. Restart it with:"
        echo "        export WC_PROXY_TOKEN=\"\$(cat $PROXY_TOKEN_FILE)\""
        echo "        python3 claude_proxy.py --host 127.0.0.1 --port ${WC_PROXY_PORT:-9000}"
        echo ""
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

echo "=== $(sed -n 's/^VERSION = "WebConsole_\(.*\)"$/WebConsole v\1/p' config.py) ==="
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