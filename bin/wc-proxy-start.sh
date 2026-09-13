#!/bin/sh
# Start claude_proxy.py on a transport, under the best interpreter that host
# has. Shipped alongside claude_proxy.py by bin/wc-deploy-proxy.sh and named
# directly by the systemd unit that script writes.
#
# The unit used to say `ExecStart=/usr/bin/env python3 claude_proxy.py`. That
# is not drift -- it is generated, so provisioning a new transport or re-running
# the deploy recreated it every time. Fixing launch.sh and bin/wc-proxy-run.sh
# on 2026-09-13 left this template producing the same thing, which is the
# durable half of the bug: a call site is one mistake, a template is every
# future one.
#
# Prefers a venv and runs without one, deliberately. Transports have no venv
# today -- pentester has none -- and claude_proxy.py imports only stdlib plus
# backend_env, so a bare python3 is genuinely correct there. Refusing to start
# without a venv would take down hosts that work fine. This is CLAUDE.md §8's
# rule for the CLI applied to the interpreter: "routing is worth a lot, and not
# more than being able to work."
#
# POSIX sh, not bash: a transport is someone else's machine and bash is not
# guaranteed. No arrays, no [[ ]], no ${BASH_SOURCE}.
#
# WC_PROXY_PRINT_ONLY=1 prints the interpreter it would use and exits, so
# tests/test_qa_launch_python_path.py can execute this file rather than read
# it -- the lesson bin/wc-resolve-claude-path.sh records from Registry #48,
# where nine tests read a shell block and none ran it.
set -u

cd "$(dirname "$0")" || exit 1

WC_PROXY_PYTHON=""
for _candidate in ./.venv/bin/python ./.venv/bin/python3; do
    if [ -x "$_candidate" ]; then
        WC_PROXY_PYTHON="$_candidate"
        break
    fi
done

if [ -z "$WC_PROXY_PYTHON" ]; then
    # No venv on this host: correct for a stdlib-only proxy, and said out loud
    # so that a future dependency failing to import is one grep away from its
    # cause rather than a silent restart loop.
    WC_PROXY_PYTHON="$(command -v python3 || command -v python || true)"
    [ -n "${WC_PROXY_PRINT_ONLY:-}" ] || \
        echo "wc-proxy-start: no .venv here, using ${WC_PROXY_PYTHON:-<none>}" >&2
fi

if [ -z "$WC_PROXY_PYTHON" ]; then
    echo "wc-proxy-start: no Python interpreter found; the proxy cannot start" >&2
    exit 1
fi

if [ -n "${WC_PROXY_PRINT_ONLY:-}" ]; then
    echo "$WC_PROXY_PYTHON"
    exit 0
fi

# exec, so systemd supervises the proxy itself rather than this wrapper --
# otherwise a crashed proxy leaves a live shell and the unit looks healthy.
exec "$WC_PROXY_PYTHON" claude_proxy.py "$@"
