#!/usr/bin/env bash
# wc-claude-shim-do-not-exec-from-wrapper
#
# Stands where `claude` used to on PATH, so that shells started before the
# wrapper alias existed still get routed. A running shell's aliases cannot be
# changed from outside it, but bash caches the resolved *path* of a command --
# and this is that path, unchanged. cweb2's parent bash has been up since
# Aug 31; it will pick this up on its next launch without re-reading anything.
#
# Interactive sessions only. The console spawns the CLI non-interactively with
# -p/--print, and those turns are already configured by claude_proxy through the
# same backend_env.deltas rule -- sending them through the wrapper too would add
# a database read to the hot path and a second layer that can fail. Anything
# that is not a plain interactive session is passed straight through.
set -euo pipefail

WRAPPER="/home/kali/projects/claude-code-webconsole/bin/wc-claude.sh"
REAL="$(ls -1d "$HOME"/.local/share/claude/versions/* 2>/dev/null | sort -V | tail -1)"

if [ -z "$REAL" ] || [ ! -x "$REAL" ]; then
    echo "claude: no installed version found under ~/.local/share/claude/versions" >&2
    exit 127
fi

# Pass straight through when this is not a session: version/help queries and
# subcommands have their own argument grammar, and injecting --model into them
# is at best noise and at worst a parse error.
for arg in "$@"; do
    case "$arg" in
        -p|--print|--version|-v|--help|-h|mcp|doctor|update|install|config|plugin|agents|setup-token)
            exec "$REAL" "$@"
            ;;
    esac
done

# No wrapper available (moved, renamed, repo gone): run the CLI rather than
# leaving the operator with no way to start it at all. Routing is worth a lot,
# but not more than being able to work.
if [ ! -f "$WRAPPER" ]; then
    echo "claude: wrapper missing at $WRAPPER, starting unrouted" >&2
    exec "$REAL" "$@"
fi

exec bash "$WRAPPER" "$@"
