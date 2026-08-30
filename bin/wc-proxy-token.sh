#!/usr/bin/env bash
# Resolve WC_PROXY_TOKEN and export it. Source this; do not execute it.
#
# The token has to be identical in three places -- whatever launches the app,
# the claude_proxy.py process, and the app itself. The app reads it from the
# database, because _load_settings_from_db() runs before config.validate() and
# so the DB wins over the environment. Anything that disagrees with the DB
# loses, which is why the order below starts there.
#
# Getting this wrong is not loud: the proxy simply refuses the handshake and
# every turn fails with "Connection lost during streaming", with no error
# naming the cause. It has happened once already.
#
# Callers must have set WC_DB_PATH and be in the project directory.

wc_resolve_proxy_token() {
    local token_file="data/proxy_token.txt"

    if [ -z "${WC_PROXY_TOKEN:-}" ] && [ -f "${WC_DB_PATH:-}" ]; then
        WC_PROXY_TOKEN="$(python3 -c "
import sqlite3, sys
try:
    con = sqlite3.connect('file:${WC_DB_PATH}?mode=ro', uri=True)
    row = con.execute(\"SELECT value FROM settings WHERE key='proxy_token'\").fetchone()
    sys.stdout.write(row[0] if row and row[0] else '')
except Exception:
    pass" 2>/dev/null)"
    fi
    if [ -z "${WC_PROXY_TOKEN:-}" ] && [ -f "$token_file" ]; then
        WC_PROXY_TOKEN="$(cat "$token_file")"
    fi
    if [ -z "${WC_PROXY_TOKEN:-}" ]; then
        WC_PROXY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    fi
    export WC_PROXY_TOKEN

    # Persist so a restart of either process agrees with the other. 077 because
    # this is a bearer credential for a socket that spawns Claude with
    # --dangerously-skip-permissions.
    mkdir -p data
    (umask 077; printf '%s' "$WC_PROXY_TOKEN" > "$token_file")
}
