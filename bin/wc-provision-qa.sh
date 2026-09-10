#!/usr/bin/env bash
# One-time per-transport QA environment setup: a Python venv and Chromium
# under ~/wc-qa-checkout on the transport, so a later QA run only has to
# sync files and run pytest, never install anything.
#
# Usage: bin/wc-provision-qa.sh <transport-name>
#        bin/wc-provision-qa.sh --list
#
# Deliberately separate from every QA run (bin/wc-run-suite-remote.sh):
# a pip install plus a Chromium download is real time and bandwidth, worth
# paying once, not on every invocation. Run by hand, before a transport is
# usable for QA -- not triggered automatically by a sync or a run, so a
# provisioning failure (disk full, network flaky) is diagnosable on its own.
#
# Same shape as wc-deploy-proxy.sh, for the same reason: ssh_host/ssh_user/
# ssh_key_path always come from ssh_transports, never re-derived a second
# way, and this has to work on a transport nothing has connected to yet --
# it opens its own one-shot SSH connection rather than depending on
# tunnel_manager's live one.
#
# Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §1.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${WC_DB_PATH:-$PWD/data/webconsole.db}"
PY="${WC_PYTHON:-.venv/bin/python}"
REMOTE_DIR="wc-qa-checkout"

_die() { echo "wc-provision-qa: $*" >&2; exit 1; }

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

TARGET="$SSH_USER@$SSH_HOST"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -i "$SSH_KEY")

echo "wc-provision-qa: $NAME -> $TARGET"

ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<REMOTE
set -euo pipefail
mkdir -p ~/$REMOTE_DIR

if [ -x ~/$REMOTE_DIR/.venv/bin/python ]; then
  echo "  venv already present, skipping creation"
else
  echo "  creating venv"
  python3 -m venv ~/$REMOTE_DIR/.venv
fi
REMOTE

echo "  syncing requirements files"
scp "${SSH_OPTS[@]}" requirements.txt requirements-dev.txt "$TARGET:$REMOTE_DIR/"

ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<REMOTE
set -euo pipefail
cd ~/$REMOTE_DIR
echo "  installing requirements (this can take a while)"
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt

if .venv/bin/python -c 'from playwright.sync_api import sync_playwright; sync_playwright().start().chromium.executable_path' >/dev/null 2>&1; then
  echo "  chromium already present, skipping download"
else
  echo "  installing chromium (this can take a while)"
  .venv/bin/playwright install chromium
fi
REMOTE

echo "wc-provision-qa: $NAME provisioned"
