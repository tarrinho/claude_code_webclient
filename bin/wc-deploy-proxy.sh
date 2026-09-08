#!/usr/bin/env bash
# Deploy claude_proxy.py to a transport host and run it under systemd --user.
#
# Usage: bin/wc-deploy-proxy.sh <transport-name>
#        bin/wc-deploy-proxy.sh --list
#
# The transport's ssh_host / ssh_user / ssh_key_path come from the
# ssh_transports table, so this script and the tunnel manager cannot disagree
# about where a transport points.
#
# WHY THIS EXISTS AS A SCRIPT rather than a handful of ad-hoc ssh/scp calls:
#
# Three earlier one-off scripts (deploy_kali3.sh, deploy_to_kali3.sh,
# start_proxy_kali3.sh, all untracked, 2026-09-05) got two things wrong that
# cost a day of "the feature does nothing and nothing is in the log":
#
#   * PORT. start_proxy_kali3.sh started the proxy on 9002 and its comment
#     claimed the tunnel forwarded there. tunnel_manager_ssh.py forwards to
#     config.PROXY_PORT, which is 9000. A proxy on the wrong port is a proxy
#     nothing ever connects to, and there is no error anywhere -- the tunnel
#     connects, the forward binds, and every turn dies at "Cannot connect".
#     This script reads the port from config.py so it cannot drift again.
#
#   * TOKEN. That script had the live proxy token as a plaintext literal.
#     Kali3's proxy then ran for days on a stale token while the database held
#     a different one, and every turn failed at the handshake. Here the token
#     is read from the database at deploy time and written to a 0600 file --
#     never into the unit, because unit files are world-readable and
#     `systemctl cat` prints them.
#
# Idempotent: re-running redeploys the current code and restarts the service.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${WC_DB_PATH:-$PWD/data/webconsole.db}"
PY="${WC_PYTHON:-.venv/bin/python}"
REMOTE_DIR="wc-proxy"
UNIT="wc-proxy.service"

_die() { echo "wc-deploy-proxy: $*" >&2; exit 1; }

_query() {
    "$PY" - "$@" <<'PY'
import sqlite3, sys
db, which, name = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
if which == "list":
    for r in con.execute("SELECT name, ssh_user, ssh_host FROM ssh_transports ORDER BY name"):
        print(f"{r['name']}\t{r['ssh_user']}@{r['ssh_host']}")
elif which == "transport":
    r = con.execute(
        "SELECT ssh_host, ssh_user, ssh_key_path FROM ssh_transports WHERE name = ?",
        (name,),
    ).fetchone()
    if not r:
        sys.exit(3)
    print("\t".join([r["ssh_host"] or "", r["ssh_user"] or "", r["ssh_key_path"] or ""]))
elif which == "token":
    r = con.execute("SELECT value FROM settings WHERE key='proxy_token'").fetchone()
    print((r["value"] if r and r["value"] else "").strip())
con.close()
PY
}

[ -f "$DB" ] || _die "no database at $DB"

if [ "${1:-}" = "--list" ] || [ $# -eq 0 ]; then
    echo "transports in $DB:"
    _query "$DB" list | sed 's/^/  /'
    [ $# -eq 0 ] && _die "name a transport (or --list)"
    exit 0
fi

NAME="$1"
if ! row="$(_query "$DB" transport "$NAME")"; then
    _die "no transport named '$NAME' (try --list)"
fi
IFS=$'\t' read -r SSH_HOST SSH_USER SSH_KEY <<<"$row"
[ -n "$SSH_HOST" ] || _die "transport '$NAME' has no ssh_host"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
[ -r "$SSH_KEY" ] || _die "ssh key not readable: $SSH_KEY"

# The port the tunnel actually forwards to -- read, never assumed.
PORT="$("$PY" -c 'import config; print(config.PROXY_PORT)')"

TOKEN="$(_query "$DB" token)"
[ "${#TOKEN}" -ge 32 ] || _die "proxy token from the database is ${#TOKEN} chars; claude_proxy refuses under 32"

TARGET="$SSH_USER@$SSH_HOST"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -i "$SSH_KEY")

echo "wc-deploy-proxy: $NAME -> $TARGET, port $PORT, token ${#TOKEN} chars (not shown)"

ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p ~/$REMOTE_DIR ~/.config/systemd/user"

# Only what the proxy imports: claude_proxy.py plus backend_env (its one local
# dependency; everything else it uses is stdlib).
scp "${SSH_OPTS[@]}" claude_proxy.py backend_env.py "$TARGET:$REMOTE_DIR/"
echo "  copied claude_proxy.py backend_env.py"

printf '%s' "$TOKEN" | ssh "${SSH_OPTS[@]}" "$TARGET" \
    "umask 077; cat > ~/$REMOTE_DIR/proxy_token.txt"
echo "  installed proxy_token.txt (0600)"

# EnvironmentFile rather than Environment=: keeps the token out of the unit,
# which is world-readable and printed by `systemctl cat`.
ssh "${SSH_OPTS[@]}" "$TARGET" "cat > ~/.config/systemd/user/$UNIT" <<UNITEOF
[Unit]
Description=WebConsole host proxy (spawns Claude Code for web turns)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/$REMOTE_DIR
Environment=WC_PROXY_LISTEN_HOST=127.0.0.1
Environment=WC_PROXY_PORT=$PORT
Environment=WC_CLAUDE_PATH=%h/.local/bin/claude
EnvironmentFile=%h/$REMOTE_DIR/proxy.env
ExecStart=/usr/bin/env python3 claude_proxy.py --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNITEOF
echo "  wrote ~/.config/systemd/user/$UNIT"

printf 'WC_PROXY_TOKEN=%s\n' "$TOKEN" | ssh "${SSH_OPTS[@]}" "$TARGET" \
    "umask 077; cat > ~/$REMOTE_DIR/proxy.env"

# Without linger a --user service dies when the last session for that user
# ends, so the proxy would vanish the moment this SSH connection closed.
ssh "${SSH_OPTS[@]}" "$TARGET" \
    "loginctl enable-linger \$USER 2>/dev/null || true
     systemctl --user daemon-reload
     systemctl --user enable --now $UNIT
     sleep 2
     systemctl --user is-active $UNIT"
echo "  service active"

echo "wc-deploy-proxy: verifying it listens on 127.0.0.1:$PORT"
ssh "${SSH_OPTS[@]}" "$TARGET" \
    "ss -tln | grep -q '127.0.0.1:$PORT' && echo '  listening on 127.0.0.1:$PORT' \
     || { echo '  NOT listening — last 20 log lines:' >&2
          systemctl --user status $UNIT --no-pager -l 2>&1 | tail -20 >&2
          exit 1; }"

echo "wc-deploy-proxy: done. The tunnel forwards a local port to 127.0.0.1:$PORT on $SSH_HOST."
