#!/usr/bin/env bash
# Restart WebConsole if it has stopped answering.
#
# systemd's Restart=always covers a process that exits. It cannot see a process
# that is alive and wedged -- still holding the port, no longer serving. This
# closes that gap, and only that gap: it never starts anything systemd has been
# told to stop, so `systemctl --user stop webconsole` stays meaningful.
set -uo pipefail
cd "$(dirname "$0")/.."

# The server binds the tailnet address, not loopback, so a check against
# 127.0.0.1 is refused every time -- which would restart a perfectly healthy
# server every 30 seconds, for ever. Ask what is actually listening rather than
# assuming; fall back to the tailnet address, then loopback.
wc_health_url() {
    local addr
    addr="$(ss -tlnH 'sport = :443' 2>/dev/null | awk '{print $4}' | sed 's/:443$//' | head -1)"
    if [ -z "$addr" ] || [ "$addr" = "*" ] || [ "$addr" = "0.0.0.0" ]; then
        addr="$(tailscale ip -4 2>/dev/null | head -1)"
    fi
    [ -n "$addr" ] || addr="127.0.0.1"
    printf 'https://%s:443/login' "$addr"
}

URL="${WC_HEALTH_URL:-$(wc_health_url)}"
ATTEMPTS="${WC_HEALTH_ATTEMPTS:-3}"
GAP="${WC_HEALTH_GAP:-3}"

log() { printf '%s wc-health: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

# Deliberately silent when systemd is not managing the server: otherwise a
# developer running launch.sh by hand gets their server restarted underneath
# them by a timer they forgot was installed.
if ! systemctl --user is-enabled --quiet webconsole.service 2>/dev/null; then
    exit 0
fi

# An intentional stop must stay stopped. Without this the timer would fight
# the operator, and "why does it keep coming back" is a miserable thing to debug.
state="$(systemctl --user show webconsole.service -p ActiveState --value 2>/dev/null || echo unknown)"
if [ "$state" = "inactive" ] || [ "$state" = "deactivating" ]; then
    exit 0
fi

# Retry before acting: a single failure during startup or a slow turn is not a
# reason to restart, and restarting mid-turn destroys work in progress.
for attempt in $(seq 1 "$ATTEMPTS"); do
    # No `|| echo 000` here: curl already writes 000 on failure, and the
    # fallback appended a second one, giving "000000" in the log.
    code="$(curl -sk -o /dev/null -m 8 -w '%{http_code}' "$URL" 2>/dev/null)"
    code="${code:-000}"
    if [ "$code" = "200" ]; then
        exit 0
    fi
    log "attempt ${attempt}/${ATTEMPTS} got HTTP ${code} from ${URL}"
    [ "$attempt" -lt "$ATTEMPTS" ] && sleep "$GAP"
done

log "unhealthy after ${ATTEMPTS} attempts; restarting webconsole.service"
systemctl --user restart webconsole.service

# The proxy is separate and can be down on its own, which is silent from the
# outside: the site answers and every turn fails at the handshake.
if ! ss -tln 2>/dev/null | grep -q "127.0.0.1:${WC_PROXY_PORT:-9000}"; then
    log "proxy not listening; restarting webconsole-proxy.service"
    systemctl --user restart webconsole-proxy.service
fi
