#!/usr/bin/env bash
# wc-init.sh — idempotent bootstrap for a session's git worktree.
#
# Usage: bin/wc-init.sh <session_name>
#
# Creates (or re-attaches) a git worktree at .claude/worktrees/<session_name>/
# on branch session/<session_name> and cd's into it. Idempotent across all
# failure states: missing worktree, stale worktree, healthy.
#
# A session that exits without a clean `worktree remove` leaves a zombie.
# That is handled by bin/wc-gc.sh (run daily via systemd user-timer or on
# startup by the next session that runs this script).
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
session_name="${1:?Usage: bin/wc-init.sh <session_name>}"
wt_dir="$repo_root/.claude/worktrees/$session_name"
branch="session/$session_name"

if ! git worktree list 2>/dev/null | grep -q "$wt_dir"; then
  # Rebuild from either the latest release or main (no release yet).
  git worktree add -b "$branch" "$wt_dir" "$(git rev-parse --verify release >/dev/null 2>&1 && echo release || echo main)"
fi

cd "$wt_dir"
