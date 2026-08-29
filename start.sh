#!/bin/bash
# Local development launcher. For the real deployment use launch.sh, which
# sets up TLS, the tailnet bind address and the proxy token.
#
# This script previously bound 0.0.0.0:8081 over plain HTTP without setting
# WC_COOKIE_ALLOW_INSECURE, so the session cookie was sent unprotected on
# every interface. It now binds loopback only.
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

export WC_LISTEN_HOST="${WC_LISTEN_HOST:-127.0.0.1}"
export WC_PORT="${WC_PORT:-8081}"

# Plain HTTP on loopback cannot set a Secure cookie, so opt in explicitly here
# rather than relying on a permissive default.
export WC_COOKIE_ALLOW_INSECURE="${WC_COOKIE_ALLOW_INSECURE:-1}"

if [ "$WC_LISTEN_HOST" != "127.0.0.1" ] && [ "$WC_LISTEN_HOST" != "localhost" ]; then
    echo "refusing to serve plain HTTP on ${WC_LISTEN_HOST}: use launch.sh for" >&2
    echo "anything other than loopback, or set up TLS first." >&2
    exit 1
fi

nohup python -m uvicorn app:app \
    --host "$WC_LISTEN_HOST" \
    --port "$WC_PORT" \
    > /tmp/wc_app.log 2>&1 &

echo "Started on PID=$!"
echo "Available at: http://${WC_LISTEN_HOST}:${WC_PORT}/login"
