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
# The host-side proxy, restarted alongside the app.
#
# It is a SECOND long-lived process running this project's code, and it was
# not restarted by any deploy until 2026-09-20. `claude_proxy.usage_frame` is
# a deliberate copy of `runner.usage_frame` -- under PROXY_ENABLED=True, which
# is the deployed default, the proxy's copy is the one that runs. So a fix to
# it took effect only when someone happened to restart the proxy by hand.
#
# That is not hypothetical. A usage-accounting fix deployed that day was live
# and still broken for nine minutes: the app had the new parser, the proxy had
# been up since 20:33 with the old one, and the first row written afterwards
# still carried a session-cumulative 110,282,269 cache-read tokens. Only a
# check against a real row caught it.
#
# Empty WC_PROXY_UNIT skips this, and a unit that is not installed is skipped
# rather than failing the deploy -- a host running PROXY_ENABLED=False has no
# proxy to restart and its deploy must not start failing because of that.
PROXY_UNIT="${WC_PROXY_UNIT:-webconsole-proxy.service}"
# Where the running instance is checked after the restart.
#
# This defaulted to empty, so every real deploy printed "no WC_HEALTH_URL set;
# skipping the live check" and activated a release without ever asking whether
# it answered -- the same shape of failure this file's header describes, where a
# session asking for a deploy is told it succeeded on the strength of nothing.
# Nothing in the repo set the variable, so the check below had never run.
#
# Derived by host name, never an address -- see bin/wc-health-url.sh for why an
# address answers 000 against Caddy's SNI-only routing, and for the measurements.
# Sourced rather than copied so this and wc-health.sh cannot drift apart.
#
# --dry-run never reaches restart_and_check, so it is unaffected. Set
# WC_SKIP_HEALTH_CHECK=1 to opt out deliberately (a throwaway WC_RELEASES_DIR,
# or a host where the tailnet name does not resolve).
# shellcheck source=bin/wc-health-url.sh
. "$(dirname "$0")/wc-health-url.sh"

if [ "${WC_SKIP_HEALTH_CHECK:-0}" = "1" ]; then
    HEALTH_URL=""
else
    HEALTH_URL="${WC_HEALTH_URL:-$(wc_health_url)}"
fi

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

    # Check that every tracked file in the specs directory is present in the
    # release archive. git archive only emits committed files, so an untracked
    # spec silently vanishes from the live copy -- this catches that before
    # the operator is told the deploy succeeded.
    local specs_in_repo specs_in_release
    specs_in_repo="$(git -C "$REPO" ls-files "docs/superpowers/specs/*.md" | wc -l)"
    specs_in_release="$(find "$dest/docs/superpowers/specs" -name '*.md' -type f 2>/dev/null | wc -l)"
    [ "$specs_in_repo" = "$specs_in_release" ] || die \
        "spec file count mismatch: repo has $specs_in_repo, release has $specs_in_release -- untracked spec files will not be live; commit them or exclude from the check"
    echo "verified: $specs_in_release spec files present in release (matches repo)"

    # Check for uncommitted files anywhere in the repo that match the specs
    # directory pattern. An untracked spec is a file that should be tracked
    # (it is in docs/superpowers/specs/) but was not committed at deploy time,
    # so the live copy is one commit behind. This catches the case where a
    # deploy runs on an intermediate commit while a session saves a new spec.
    local untracked_specs
    untracked_specs="$(git -C "$REPO" status --porcelain -- 'docs/superpowers/specs/*' 2>/dev/null)"
    if [ -n "$untracked_specs" ]; then
        echo "WARNING: untracked spec files exist and will not be live:" >&2
        echo "$untracked_specs" >&2
        echo "WARNING: consider committing them before the next deploy" >&2
    fi
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
    # The proxy first, so the app comes up against a proxy already running the
    # new code rather than reconnecting to the old one mid-deploy.
    #
    # This does not widen the blast radius: restarting $UNIT below already
    # cancels every in-flight turn (CLAUDE.md rule 9), and the proxy is what
    # those turns run through.
    #
    # Note what this does NOT fix: the proxy unit's WorkingDirectory is the
    # checkout, not $CURRENT, so it picks up the working tree as it stands at
    # this instant -- including anything a peer has saved and not committed.
    # That is the exact hazard this file's header describes, and the app is
    # protected from it by the release directory while the proxy is not.
    # Serving the proxy from a release too is a separate change.
    if [ -n "$PROXY_UNIT" ] && systemctl --user cat "$PROXY_UNIT" >/dev/null 2>&1; then
        systemctl --user restart "$PROXY_UNIT" || die \
            "restart of $PROXY_UNIT failed -- current already points at $(readlink -f "$CURRENT"), roll back with --rollback"
        echo "restarted $PROXY_UNIT"
    elif [ -n "$PROXY_UNIT" ]; then
        echo "no $PROXY_UNIT installed -- skipping (PROXY_ENABLED=False host?)"
    fi
    # If this fails, `current` has already moved. Say so: the operator is now
    # in a state where the symlink names a release that never started, and a
    # message that omits that leaves them guessing which way round things are.
    systemctl --user restart "$UNIT" || die \
        "restart of $UNIT failed -- current already points at $(readlink -f "$CURRENT"), roll back with --rollback"
    if [ -z "$HEALTH_URL" ]; then
        echo "live check skipped (WC_SKIP_HEALTH_CHECK=1) -- this release is unverified"
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
