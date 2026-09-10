#!/usr/bin/env bash
# Deploy a git commit, not a working tree.
#
# The problem this exists for: the systemd unit runs `launch.sh` with
# WorkingDirectory set to the repository, so production serves whatever bytes
# are on disk. With eight sessions editing one tree that is a lottery -- a
# restart to pick up a security fix also ships whoever's half-finished refactor
# happened to be saved at that moment. Twice today a restart was the correct
# action and had to be declined for exactly that reason, and §17 has been
# failing on a proxy eleven hours behind its own source file.
#
# So: export a commit to its own directory, point `current` at it, and restart.
# After that "is the fix live?" is answerable from a commit id instead of by
# exploiting the running server, and rollback is a symlink flip.
#
# WHAT THIS DOES NOT DO: build a venv per release. The interpreter and its
# packages stay shared, because a release directory is source only -- the point
# is to fix *which source* runs, not to isolate dependencies. That trade is
# deliberate: a per-release venv costs minutes per deploy and would have
# prevented none of the incidents above.
#
#   bin/wc-deploy.sh                  deploy HEAD
#   bin/wc-deploy.sh <ref>            deploy any ref
#   bin/wc-deploy.sh --list           what is deployed, and what is current
#   bin/wc-deploy.sh --rollback       point `current` at the previous release
#   bin/wc-deploy.sh --dry-run        stage and verify, change nothing live
#
# HISTORY, because the name matters here. This was bin/wc-release.sh until
# 2026-09-09, when that name was repurposed for a session-branch merge tool.
# The deploy logic was not moved -- it was dropped, and for a day nothing in
# bin/ could put a commit live while `wc-release.sh` answered every invocation
# with "Nothing to do." and exit 0. A session asking for a deploy was told it
# had succeeded. Restored here verbatim from fcb4971 under a name that says
# what it does; tests/test_qa_deploy_entrypoint.py now fails if it goes
# missing again.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RELEASES="${WC_RELEASES_DIR:-$HOME/.local/share/webconsole/releases}"
CURRENT="$RELEASES/current"
KEEP="${WC_RELEASES_KEEP:-5}"
UNIT="${WC_UNIT:-webconsole.service}"
# Where the running instance is checked. Empty skips the live check, which is
# what --dry-run and a non-default releases dir want.
HEALTH_URL="${WC_HEALTH_URL:-}"

die() { echo "wc-release: $*" >&2; exit 1; }

# --- staging -----------------------------------------------------------------

stage() {
    local ref="$1" sha dest
    sha="$(git -C "$REPO" rev-parse --short "$ref")" || die "unknown ref: $ref"
    dest="$RELEASES/$sha"

    if [ -d "$dest" ]; then
        echo "already staged: $sha"
    else
        mkdir -p "$dest" || die "cannot create $dest"
        # git archive, not cp: it emits exactly what the commit contains, so
        # untracked files and other sessions' edits cannot travel. That is the
        # entire property being bought here.
        git -C "$REPO" archive "$ref" | tar -x -C "$dest" \
            || die "export of $ref failed"
        echo "$sha" > "$dest/.release-sha"
        git -C "$REPO" log -1 --format='%H%n%ci%n%s' "$ref" > "$dest/.release-info"
        echo "staged $sha -> $dest"
    fi
    echo "$dest"
}

# Verify the artefact before anything points at it. Cheap checks only: this
# runs on every deploy and a slow gate is a gate people work around.
verify() {
    local dest="$1"
    [ -f "$dest/app.py" ] || die "no app.py in $dest -- export incomplete"
    [ -f "$dest/launch.sh" ] || die "no launch.sh in $dest"
    # Import under the shared interpreter, from the release directory, with
    # throwaway state. A release whose modules do not import is the failure
    # this catches before it becomes an outage -- registry #51 was HEAD calling
    # a function no committed file defined, which this would have caught.
    local probe
    probe="$(mktemp -d)"
    (
        cd "$dest" || exit 1
        WC_DB_PATH="$probe/db" WC_PROJECTS_ROOT="$probe/p" \
        WC_LOG_FILE="$probe/log" WC_SESSION_SECRET="verify-only-not-a-secret" \
        WC_PROXY_TOKEN="verify-only-not-a-secret" \
            "$REPO/.venv/bin/python" -c "import app, db, config, routes.supervisor_map" \
            >"$probe/out" 2>&1
    )
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "--- import failure ---" >&2
        tail -20 "$probe/out" >&2
        rm -rf -- "$probe"
        die "release $dest does not import; not activating"
    fi
    rm -rf -- "$probe"
    echo "verified: modules import from the release directory"
}

activate() {
    local dest="$1" previous=""
    [ -L "$CURRENT" ] && previous="$(readlink -f "$CURRENT")"
    [ -n "$previous" ] && echo "$previous" > "$RELEASES/.previous"
    # Atomic: a symlink swap is one rename, so there is no instant where
    # `current` points at nothing.
    ln -sfn "$dest" "$CURRENT.tmp" && mv -Tf "$CURRENT.tmp" "$CURRENT" \
        || die "could not point current at $dest"
    echo "current -> $(readlink -f "$CURRENT")"
}

restart_and_check() {
    # If this fails, `current` has already moved. Say so: the operator is now
    # in a state where the symlink names a release that never started, and a
    # message that omits that leaves them guessing which way round things are.
    systemctl --user restart "$UNIT" || die \
        "restart of $UNIT failed -- current already points at $(readlink -f "$CURRENT"), roll back with --rollback"
    if [ -z "$HEALTH_URL" ]; then
        echo "no WC_HEALTH_URL set; skipping the live check"
        return 0
    fi
    local code=""
    for _ in $(seq 1 30); do
        sleep 1
        code="$(curl -sk -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
        [ "$code" = "200" ] && break
    done
    [ "$code" = "200" ] || die "after restart, $HEALTH_URL answered '$code' -- roll back with --rollback"
    echo "live check: $HEALTH_URL -> 200"
}

prune() {
    # Newest KEEP releases plus whatever `current` and `.previous` point at.
    local keepers protect
    protect="$(readlink -f "$CURRENT" 2>/dev/null || true)
$(cat "$RELEASES/.previous" 2>/dev/null || true)"
    keepers="$(ls -1dt "$RELEASES"/*/ 2>/dev/null | head -n "$KEEP")"
    for dir in "$RELEASES"/*/; do
        dir="${dir%/}"
        case "$keepers" in *"$dir"*) continue ;; esac
        case "$protect" in *"$dir"*) continue ;; esac
        rm -rf -- "$dir" && echo "pruned $(basename "$dir")"
    done
}

# --- entry -------------------------------------------------------------------

mkdir -p "$RELEASES" || die "cannot create $RELEASES"

case "${1:-}" in
    --list)
        echo "releases dir : $RELEASES"
        # `readlink -f` resolves a *non-existent* path happily, so without the
        # -L test this printed the symlink's own path and read as though
        # something were deployed. A status line that lies is worse than none.
        if [ -L "$CURRENT" ]; then
            echo "current      : $(readlink -f "$CURRENT")"
        else
            echo "current      : (none)"
        fi
        echo "previous     : $(cat "$RELEASES/.previous" 2>/dev/null || echo '(none)')"
        for dir in "$RELEASES"/*/; do
            [ -f "${dir}.release-info" ] || continue
            printf '  %-10s %s\n' "$(basename "${dir%/}")" \
                "$(sed -n 3p "${dir}.release-info")"
        done
        ;;
    --rollback)
        prev="$(cat "$RELEASES/.previous" 2>/dev/null || true)"
        [ -n "$prev" ] && [ -d "$prev" ] || die "no previous release recorded"
        activate "$prev"
        restart_and_check
        ;;
    --dry-run)
        dest="$(stage "${2:-HEAD}" | tail -1)"
        verify "$dest"
        echo "dry run: staged and verified, nothing activated"
        ;;
    *)
        dest="$(stage "${1:-HEAD}" | tail -1)"
        verify "$dest"
        activate "$dest"
        restart_and_check
        prune
        ;;
esac
