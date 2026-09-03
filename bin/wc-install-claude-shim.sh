#!/usr/bin/env bash
# Keep ~/.local/bin/claude pointing at the routing shim. Idempotent.
#
# Why this needs re-asserting rather than doing once: the CLI self-updates and
# rewrites that path. It moved from 2.1.258 to 2.1.259 at 00:00 on 2026-09-03,
# replacing the file with a fresh symlink -- which would silently remove the
# shim and put every terminal session back to unrouted, with nothing to say so.
# That is the same shape of failure the shim exists to prevent, so it cannot be
# left to be noticed.
#
# Why the shim at all: a running shell's aliases cannot be changed from outside
# it, and the shells in the screen windows predate the alias -- cweb2's parent
# bash has been up since Aug 31. But bash caches the resolved *path* of a
# command, and this is that path. Replacing what lives there routes those shells
# on their next launch without them re-reading anything.
#
# Run by bin/wc-health.sh on its 30-second timer, and safe to run by hand.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$HERE/claude-shim.sh"
TARGET="${WC_CLAUDE_SHIM_TARGET:-$HOME/.local/bin/claude}"
MARKER="wc-claude-shim-do-not-exec-from-wrapper"
VERSIONS="${WC_CLAUDE_VERSIONS_DIR:-$HOME/.local/share/claude/versions}"

quiet="${WC_SHIM_QUIET:-0}"
say() { [ "$quiet" = "1" ] || printf 'wc-install-claude-shim: %s\n' "$1"; }

if [ ! -f "$SOURCE" ]; then
    say "no shim source at $SOURCE, nothing to install"
    exit 0
fi

# Already ours: nothing to do, and say nothing on the timer path.
if [ -f "$TARGET" ] && grep -qF "$MARKER" "$TARGET" 2>/dev/null; then
    exit 0
fi

# Refuse to install if there is no real CLI to fall back to. Replacing the
# entry point when nothing can serve it would leave the operator unable to
# start claude at all, which is worse than unrouted sessions.
newest="$(ls -1d "$VERSIONS"/* 2>/dev/null | sort -V | tail -1 || true)"
if [ -z "$newest" ] || [ ! -x "$newest" ]; then
    say "no executable CLI under $VERSIONS; refusing to replace $TARGET"
    exit 0
fi

mkdir -p "$(dirname "$TARGET")"
# Written to a sibling and renamed, so there is no instant where the path does
# not exist: a shell launching claude during the swap gets the old file or the
# new one, never nothing.
tmp="$(dirname "$TARGET")/.claude-shim-installing.$$"
cp "$SOURCE" "$tmp"
chmod 755 "$tmp"
mv -f "$tmp" "$TARGET"
say "installed the routing shim at $TARGET (real CLI: $newest)"
