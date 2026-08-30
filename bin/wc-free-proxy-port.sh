#!/usr/bin/env bash
# Free the proxy port, but only from a stale proxy of our own.
#
# A claude_proxy started by hand keeps listening after systemd takes over, so
# the unit cannot bind and restart-loops while the hand-started process quietly
# serves whatever code it was started with. That is the exact failure this
# supervision exists to prevent, so the port is reclaimed -- carefully.
#
# The check is on the holder's command line, not just the port: killing
# whatever happens to sit on 9000 would be a rude and occasionally destructive
# thing for a script to do unasked.
set -uo pipefail
PORT="${WC_PROXY_PORT:-9000}"

holder="$(ss -tlnpH "sport = :${PORT}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)"
[ -n "${holder:-}" ] || exit 0

# Our own supervised instance, already running: leave it alone.
if [ "${MAINPID:-}" = "$holder" ]; then
    exit 0
fi

cmdline="$(tr '\0' ' ' < "/proc/${holder}/cmdline" 2>/dev/null || true)"
case "$cmdline" in
    *claude_proxy.py*) ;;
    *)
        echo "port ${PORT} held by pid ${holder} (${cmdline:-unknown}); not ours, leaving it" >&2
        exit 0
        ;;
esac

echo "reclaiming port ${PORT} from stale proxy pid ${holder}" >&2
kill "$holder" 2>/dev/null || true
for _ in 1 2 3 4 5; do
    kill -0 "$holder" 2>/dev/null || exit 0
    sleep 0.4
done
# It ignored SIGTERM. The port matters more than a clean exit here: without it
# the unit simply cannot start.
kill -9 "$holder" 2>/dev/null || true
sleep 0.5
