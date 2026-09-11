#!/usr/bin/env bash
# The URL that answers "is the server up?" -- one definition, two callers.
#
# Sourced by bin/wc-health.sh (the 30s supervision timer) and bin/wc-deploy.sh
# (the post-restart gate). It lives in its own file for the reason CLAUDE.md
# gives for backend_env.deltas: the same rule written twice is two rules, and
# the copies drift. Both callers had their own idea of this URL and both were
# wrong in the same way.
#
# Ask for the host *name*, never an address. Caddy fronts :443 with a single
# named site block (`kali-2.tail850c40.ts.net { ... }`), so it routes on SNI and
# Host: a request to https://<tailnet-ip>/login matches no site and gets no
# reply at all. Measured 2026-09-11 against the live server -- IP form 000, name
# form 200, app form 200 -- after the health timer spent an afternoon restarting
# webconsole.service every ~60s on the strength of its own unreachable URL. A
# health check that cannot reach a healthy server is worse than no health check,
# because this one acts on the answer.
#
# The name comes from tailscale's own record, with the trailing root dot
# stripped (DNSName is reported as "kali-2.tail850c40.ts.net.").
#
# The fallback is the app on loopback rather than another address: uvicorn binds
# 127.0.0.1:8080 behind Caddy, so it is reachable, and it is the more honest
# question for wc-health.sh to ask -- that script restarts webconsole.service,
# and the app is the thing such a restart fixes. A Caddy that is down is not
# something restarting the app will mend.
wc_health_url() {
    local domain
    domain="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' \
        2>/dev/null)"
    if [ -n "$domain" ]; then
        printf 'https://%s/login' "$domain"
        return
    fi
    printf 'http://127.0.0.1:%s/login' "${WC_PORT:-8080}"
}
