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
export WC_PORT=8080
export WC_COOKIE_ALLOW_INSECURE="0"
# transcripts.agent_traffic()'s per-file read is incremental (byte-offset,
# append-only aware -- transcripts.py's _agent_events_cache), so the 300s
# default this shipped with predates knowing that: measured live 2026-09-12,
# a warm pass costs ~0.5s against this host's real transcripts, not the
# "450MB every poll" cost the original default was chosen to avoid. 5s
# keeps the duty cycle under 10% and turns a SYNC_REQUEST marker into a
# pending row in single-digit seconds instead of up to five minutes.
export WC_SYNC_REQUEST_WATCHER="${WC_SYNC_REQUEST_WATCHER:-1}"
export WC_SYNC_REQUEST_WATCHER_INTERVAL_S="${WC_SYNC_REQUEST_WATCHER_INTERVAL_S:-5}"

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

# Reclaim the listen port from a previous instance -- by port and cmdline, never
# by pattern.
#
# This was `pkill -f "uvicorn app:app"`, which matches every uvicorn on the box
# running that app, including the throwaway servers the test suite spawns on
# random ports. launch.sh runs on every `systemctl restart`, so a single restart
# swept away every test server any session had running, and the tests reported
# it as their own servers exiting with code -15. Ten restarts while proving the
# §17 recovery cases turned a green suite into 128 failures that had nothing to
# do with the code under test.
#
# bin/wc-free-proxy-port.sh already models the right shape for the proxy port,
# and its comments warn about exactly this: killing whatever happens to match is
# a rude and occasionally destructive thing for a script to do unasked. The
# listen port never got the same treatment. Only the process actually holding
# the port we are about to bind is ours to stop, and only when its command line
# confirms what it is.
# `|| true` is load-bearing under `set -euo pipefail`: with nothing listening
# on the port -- the normal case on a clean start -- grep finds no match and
# exits 1, pipefail propagates it, and set -e kills this script silently right
# after the banner. Without it the reclaim only works when the problem it
# exists to fix is already present, and a cold start can never succeed. That
# took the site down for 43 restart attempts, each exiting 1 with no traceback
# anywhere, because the only failing path was the one that always runs.
#
# The markers below are not decoration: tests/test_qa_launch_reclaim.py lifts
# everything between them and executes it against a real listener on a spare
# port. The first version of that file only read this text, which is how a
# block that could never succeed passed nine assertions.
# >>> reclaim-block
RECLAIM_PORT="${WC_PORT:-443}"
holder="$(ss -tlnpH "sport = :${RECLAIM_PORT}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -n "${holder:-}" ] && [ "${holder}" != "$$" ]; then
    holder_cmd="$(tr '\0' ' ' < "/proc/${holder}/cmdline" 2>/dev/null || true)"
    case "$holder_cmd" in
        *"uvicorn app:app"*)
            echo "reclaiming port ${RECLAIM_PORT} from previous instance pid ${holder}" >&2
            kill "$holder" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                kill -0 "$holder" 2>/dev/null || break
                sleep 0.4
            done
            kill -9 "$holder" 2>/dev/null || true
            ;;
        *)
            echo "port ${RECLAIM_PORT} held by pid ${holder} (${holder_cmd:-unknown}); not ours, leaving it" >&2
            ;;
    esac
fi
# Wait until the kernel has fully released the port before trying to bind.
# Without this a cold-start or stale-process leaves the site down in a systemd
# restart loop (740+ attempts in the last outage).
#
# The condition tests for *output*, not exit status. `ss` returns 0 for any
# successful query whether or not anything matched, so `! ss ... >/dev/null`
# is false on a free port as well as a busy one: the loop never broke early,
# never checked anything, and simply slept ten seconds on every single start.
# A wait that cannot observe what it is waiting for is just a delay.
for _retry in 1 2 3 4 5 6 7 8 9 10; do
    if ! ss -tlnH "sport = :${RECLAIM_PORT}" 2>/dev/null | grep -q .; then
        break
    fi
    sleep 1
done
# <<< reclaim-block

# ── Resolve the Claude binary ──────────────────────────────────────────
# The main app process now spawns `claude` directly too (the machine Test
# button in routes/machines.py), not only the separate proxy process -- and
# systemd --user starts this with the same PATH that does not include
# ~/.local/bin. Without this, the Test button fails with "Could not start
# claude" on every backend, since a bare "claude" is unresolvable here. See
# bin/wc-resolve-claude-path.sh for the full history (this exact failure has
# already broken the proxy twice under a different caller).
# shellcheck source=bin/wc-resolve-claude-path.sh
. bin/wc-resolve-claude-path.sh

# ── Launch WebConsole with HTTPS ──────────────────────────────────────
# Application records go to logs/webconsole.log through logging.conf's rotating
# handler. Uvicorn's own access lines only ever reach stdout, so without this
# they die with whatever shell started the server -- which is how a backgrounded
# launch ended up with no request log at all.
mkdir -p logs

# WC_EXEC=1 replaces this shell with uvicorn rather than backgrounding it, so a
# supervisor watches the server itself. Backgrounded, the server dies with
# whatever shell started it, which is how it has gone down mid-change more than
# once. systemd captures stdout to the journal, so no redirect here.
if [ "${WC_EXEC:-0}" = "1" ]; then
    exec python3 -m uvicorn app:app \
        --host 127.0.0.1 \
        --port 8080 \
        --log-level info
fi

python3 -m uvicorn app:app \
    --host 127.0.0.1 \
    --port 8080 \
    --log-level info >> logs/uvicorn.out.log 2>&1 &

WC_PID=$!
echo "  PID      : ${WC_PID}"
echo "  URL      : https://${TAILNET_DOMAIN}"
echo ""

# ── Cleanup trap ──────────────────────────────────────────────────────
trap 'echo "Shutting down..."; kill "$WC_PID" 2>/dev/null; exit 0' INT TERM
wait