#!/usr/bin/env bash
# Restart WebConsole if it has stopped answering.
#
# systemd's Restart=always covers a process that exits. It cannot see a process
# that is alive and wedged -- still holding the port, no longer serving. This
# closes that gap, and only that gap: it never starts anything systemd has been
# told to stop, so `systemctl --user stop webconsole` stays meaningful.
set -uo pipefail
cd "$(dirname "$0")/.."

# The health URL is defined once, in its own file, and sourced by both this
# script and bin/wc-deploy.sh. It used to be built here from whatever was
# listening on :443 -- an address, which Caddy's SNI-only routing answers with
# 000 forever -- and this script acted on that by restarting the service every
# ~60s. See bin/wc-health-url.sh for the measurements.
# shellcheck source=bin/wc-health-url.sh
. "$(dirname "$0")/wc-health-url.sh"

# A seam, so the restart decision can be exercised in a test without either
# restarting the live server or shadowing the real systemctl on PATH. Defaults
# to the real thing; nothing in production sets it.
SYSTEMCTL="${WC_SYSTEMCTL:-systemctl}"

URL="${WC_HEALTH_URL:-$(wc_health_url)}"
ATTEMPTS="${WC_HEALTH_ATTEMPTS:-3}"
GAP="${WC_HEALTH_GAP:-3}"

log() { printf '%s wc-health: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

# Keep ~/.local/bin/claude pointing at the routing shim.
#
# Placed before the server guards below, and that is deliberate: this concerns
# terminal sessions, not the web service. Whether webconsole is enabled,
# stopped on purpose, or mid-restart has no bearing on whether a shell about to
# start claude should reach the configured backend.
#
# Needed on a timer rather than once because the CLI self-updates and rewrites
# that path -- it moved 2.1.258 -> 2.1.259 at 00:00 on 2026-09-03 -- which would
# silently put every terminal session back to unrouted. The installer is
# idempotent and silent when the shim is already in place, so this costs one
# grep per tick.
if [ -x "$(dirname "${BASH_SOURCE[0]}")/wc-install-claude-shim.sh" ]; then
    "$(dirname "${BASH_SOURCE[0]}")/wc-install-claude-shim.sh" || \
        log "could not assert the claude shim; terminal sessions may be unrouted"
fi

# Deliberately silent when systemd is not managing the server: otherwise a
# developer running launch.sh by hand gets their server restarted underneath
# them by a timer they forgot was installed.
if ! $SYSTEMCTL --user is-enabled --quiet webconsole.service 2>/dev/null; then
    exit 0
fi

# An intentional stop must stay stopped. Without this the timer would fight
# the operator, and "why does it keep coming back" is a miserable thing to debug.
state="$($SYSTEMCTL --user show webconsole.service -p ActiveState --value 2>/dev/null || echo unknown)"
if [ "$state" = "inactive" ] || [ "$state" = "deactivating" ]; then
    exit 0
fi

# systemd is mid-restart. Restart=always already covers a process that exited;
# issuing a second restart on top of its first is how one crash became two
# stop/start cycles while proving §17 case 1.
sub="$($SYSTEMCTL --user show webconsole.service -p SubState --value 2>/dev/null || echo unknown)"
if [ "$state" = "activating" ] || [ "$sub" = "auto-restart" ] || [ "$sub" = "start" ]; then
    exit 0
fi

# A server that is still booting is not a server that has failed.
#
# Observed while proving §17 case 1: `kill -9` at 22:48:49, systemd's own
# Restart=always brought it back at 22:48:52, and this script stopped it again
# at 22:48:55 because HTTP was not answering yet. Two restarters racing, and
# the loser is the boot that never finishes. Boot here takes over ten seconds
# -- db.init, WAL recovery, TLS -- while ATTEMPTS x GAP gives up in about
# nine, so any restart at all could be followed by this one interrupting it.
#
# Skipping while the process is young costs nothing: systemd is already
# watching for a process that exits, and this check exists only for the wedged
# case, which a process too young to have served a request cannot be in.
# Fails *safe*: when the age of the process cannot be determined, grace
# applies and this script does nothing. A restarter that acts on missing
# information is worse than one that waits 30 seconds for better information.
#
# That direction matters more than it looks. The first version returned "no
# grace" when MainPID named a process that no longer existed -- which is
# exactly the state during systemd's own restart -- so the script proceeded to
# the HTTP check, found nothing answering, and issued a competing restart. It
# reproduced the very race it was added to prevent.
boot_grace() {
    local pid uptime
    pid="$($SYSTEMCTL --user show webconsole.service -p MainPID --value 2>/dev/null)"
    # No pid: systemd is between processes and already handling it.
    [ -n "$pid" ] && [ "$pid" != "0" ] || return 0
    uptime="$(python3 -c "import sysstats,sys;u=sysstats.process_uptime_s($pid);print(int(u) if u is not None else -1)" 2>/dev/null)"
    # Unreadable, or a pid that has already gone: same conclusion.
    [ -n "$uptime" ] && [ "$uptime" -ge 0 ] 2>/dev/null || return 0
    [ "$uptime" -lt "${WC_HEALTH_BOOT_GRACE:-45}" ]
}

if boot_grace; then
    exit 0
fi

# Answering HTTP is necessary and not sufficient. Registry #41: the server
# served 200 for 37 minutes while writing nothing, because its connection held
# a read transaction opened before another process wrote and so could never
# upgrade to a writer in WAL. A read-only probe cannot tell that apart from
# health -- /login returns 200 either way -- so a second check asks whether
# the write path is alive. Only run when the HTTP check passed; when it did
# not we are restarting regardless and the extra question is noise.
check_write_path() {
    [ "${WC_HEALTH_SKIP_WRITE_CHECK:-0}" = "1" ] && return 0

    local pid db server_db verdict status
    pid="$($SYSTEMCTL --user show webconsole.service -p MainPID --value 2>/dev/null)"
    [ -n "$pid" ] && [ "$pid" != "0" ] || return 0

    # The probe must be looking at the file the server is writing. If it is
    # not, it reports "unknown" for ever and this check silently never fires
    # while appearing installed -- the same shape as the bug it exists to
    # catch. Compare against the descriptors the server actually holds and
    # skip loudly on disagreement rather than restarting on a guess.
    db="$(python3 -c 'import config; print(config.DB_PATH)' 2>/dev/null)"
    server_db="$(readlink -f /proc/"$pid"/fd/* 2>/dev/null \
                 | grep -m1 'webconsole\.db$' || true)"
    if [ -n "$db" ] && [ -n "$server_db" ] && [ "$db" != "$server_db" ]; then
        log "write check skipped: probe reads ${db}, server holds ${server_db}"
        return 0
    fi

    # Exit 1 is STALE only. `warming` and `unknown` exit 0 on purpose: a
    # just-restarted server inherits rows from before the restart, so a
    # boolean check would restart it and then restart it again -- registry
    # #34, which restarted a healthy server every 30 seconds.
    verdict="$(python3 -m sysstats --pid "$pid" 2>&1)"
    status=$?
    [ "$status" -eq 0 ] && return 0

    log "write path unhealthy: ${verdict}; restarting webconsole.service"
    $SYSTEMCTL --user restart webconsole.service
    return 1
}

# Retry before acting: a single failure during startup or a slow turn is not a
# reason to restart, and restarting mid-turn destroys work in progress.
for attempt in $(seq 1 "$ATTEMPTS"); do
    # No `|| echo 000` here: curl already writes 000 on failure, and the
    # fallback appended a second one, giving "000000" in the log.
    code="$(curl -sk -o /dev/null -m 8 -w '%{http_code}' "$URL" 2>/dev/null)"
    code="${code:-000}"
    if [ "$code" = "200" ]; then
        check_write_path
        exit 0
    fi
    log "attempt ${attempt}/${ATTEMPTS} got HTTP ${code} from ${URL}"
    [ "$attempt" -lt "$ATTEMPTS" ] && sleep "$GAP"
done

# Between a process dying and systemd noticing, MainPID still names it and the
# HTTP check fails for the honest reason that nothing is listening. Restarting
# here is not wrong so much as redundant -- Restart=always is about to do it --
# and the two together turn one crash into a stop/start on top of a scheduled
# restart. Defer: this script exists for the wedged case, where the process is
# alive and answering nothing, and a process that has already gone is not it.
pid_now="$($SYSTEMCTL --user show webconsole.service -p MainPID --value 2>/dev/null)"
if [ -n "$pid_now" ] && [ "$pid_now" != "0" ] && [ ! -d "/proc/$pid_now" ]; then
    log "main process ${pid_now} has gone; leaving the restart to systemd"
    exit 0
fi

log "unhealthy after ${ATTEMPTS} attempts; restarting webconsole.service"
$SYSTEMCTL --user restart webconsole.service

# The proxy is separate and can be down on its own, which is silent from the
# outside: the site answers and every turn fails at the handshake.
if ! ss -tln 2>/dev/null | grep -q "127.0.0.1:${WC_PROXY_PORT:-9000}"; then
    log "proxy not listening; restarting webconsole-proxy.service"
    $SYSTEMCTL --user restart webconsole-proxy.service
fi
