#!/usr/bin/env bash
# wc-gc.sh — garbage-collect stale worktrees whose branches are gone.
#
# Usage: bin/wc-gc.sh
#
# Run daily via a systemd user-timer or on startup by the next session.
# Prunes the worktree metadata (git-native), then removes any worktree
# directory under .claude/worktrees/ whose branch no longer exists in
# the repository (indicating the owning session exited without cleanup).
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

# Git-native prune: removes stale entries from the worktree index.
git worktree prune

for wt_dir in .claude/worktrees/*/; do
  # Skip if the glob didn't expand (no worktrees).
  [ -d "$wt_dir" ] || continue

  branch="$(basename "$wt_dir")"
  git_branch="session/$branch"

  # If the branch is gone, the session is dead — remove worktree and branch.
  if ! git rev-parse --verify "$git_branch" >/dev/null 2>&1; then
    git worktree remove "$wt_dir" 2>/dev/null || true
    git branch -D "$git_branch" 2>/dev/null || true
  fi
done
