#!/usr/bin/env bash
# Make a throwaway copy of the production database -- and reap old ones.
#
# rules.md and CLAUDE.md §9 both say verification runs against a throwaway
# WC_DB_PATH and never the live file, because db.init() migrates and a second
# process on the live database caused registry #41: 37 minutes of a server
# that answered HTTP 200 and silently wrote nothing.
#
# That rule was prose only, so every session invented its own copy by hand.
# On 2026-09-21 /tmp was 100% full -- 1.4 MB free on a 1.9 GB tmpfs -- holding
# TEN abandoned ~185 MB copies named wcn, wcg, wck, wcb, wcs, wcval, wcverify,
# wcv3, wcv2 and tmpr7nj83bj. Chromium could not write its profile, so every
# browser test on the box failed with what looks like a browser fault.
#
# The names are the reason it accumulated: nothing could tell a throwaway copy
# from any other directory, so nothing could clean one up. This script exists
# to make the copies findable, and therefore reapable:
#
#   wc-throwaway-db.sh            create one, print its directory
#   wc-throwaway-db.sh --reap     delete stale ones (see AGE_HOURS)
#
# Uses sqlite3's online backup API rather than cp: the live database is being
# written while this runs, and a byte copy of a WAL-mode database mid-write is
# not guaranteed to be a valid database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${WC_SOURCE_DB:-$ROOT/data/webconsole.db}"
# One prefix, because the reaper below matches on it. A copy made under any
# other name is invisible to cleanup, which is the whole defect this fixes.
PREFIX="wc-throwaway-"
TMPDIR_BASE="${TMPDIR:-/tmp}"
AGE_HOURS="${WC_THROWAWAY_MAX_AGE_HOURS:-6}"

reap() {
    local freed=0 kept=0 removed=0
    shopt -s nullglob
    for dir in "$TMPDIR_BASE/$PREFIX"*; do
        [ -d "$dir" ] || continue
        # Age gate first: it is the cheap check, and a copy in active use is
        # nearly always a recent one.
        if [ -n "$(find "$dir" -maxdepth 0 -mmin "-$((AGE_HOURS * 60))" 2>/dev/null)" ]; then
            kept=$((kept + 1))
            continue
        fi
        # Then the open-file check, on the FILES rather than the directory.
        # `fuser -m` would match every process using the tmpfs mount and so
        # reports everything as busy -- it did, on the first attempt at this
        # cleanup, and would have left /tmp full.
        if fuser "$dir"/* >/dev/null 2>&1; then
            kept=$((kept + 1))
            continue
        fi
        local mb
        mb=$(du -sm "$dir" 2>/dev/null | cut -f1 || echo 0)
        rm -rf "$dir" && { freed=$((freed + mb)); removed=$((removed + 1)); }
    done
    shopt -u nullglob
    if [ "$removed" -gt 0 ]; then
        echo "wc-throwaway-db: reaped $removed copy(ies), freed ${freed} MB, kept $kept"
    fi
}

create() {
    [ -f "$SRC" ] || { echo "wc-throwaway-db: no database at $SRC" >&2; exit 1; }
    # Reap before creating: the moment you need a new copy is exactly when the
    # old ones are provably not needed, and it keeps cleanup on the path
    # everybody already walks rather than on one they have to remember.
    reap
    local dir
    dir="$(mktemp -d "$TMPDIR_BASE/${PREFIX}XXXXXX")"
    python3 - "$SRC" "$dir/webconsole.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
# Online backup API: safe against a database being written right now, which a
# plain copy of a WAL-mode file is not.
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PY
    echo "$dir"
}

case "${1:-}" in
    --reap) reap ;;
    "")     create ;;
    *)      echo "usage: wc-throwaway-db.sh [--reap]" >&2; exit 2 ;;
esac
