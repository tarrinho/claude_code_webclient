#!/usr/bin/env bash
# wc-release.sh — automated release: merge session branches into a release
# staging branch, then into main.
#
# Usage: bin/wc-release.sh
#
# THIS SCRIPT DOES NOT DEPLOY. It merges session branches; putting a commit
# live is bin/wc-deploy.sh. The distinction is written here because it was
# briefly lost: this file replaced the deploy script under the same name on
# 2026-09-09, so for a day `bin/wc-release.sh` was the only release-shaped
# entry point in bin/, it answered with "Nothing to do." and exit 0, and a
# session asking for a deploy was told it had succeeded.
#
# Flow:
#   1. Ensure the `release` branch exists (based on main).
#   2. Merge each session/ branch into release.
#   3. Clean merges are committed to release automatically.
#   4. Conflicts are listed with the conflicting files and the owning session.
#      The script exits 1 and does NOT continue — a human (or the owning
#      session, if alive) must resolve the conflicts.
#   5. After all conflicts are resolved, run again: the second invocation
#      completes the release by merging release → main.
#
# Conflict resolution policy:
#   - Clean merge → automated, no human needed.
#   - Conflict detected → script lists conflicting files and session names,
#     exits 1. The owning session is expected to resolve and re-commit.
#   - Multiple sessions conflicted → consolidated single report, not one
#     message per session.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

# Ensure release branch exists and tracks main.
if ! git rev-parse --verify release >/dev/null 2>&1; then
  git branch release main
fi

# If there are leftover unstaged changes from a failed merge, abort.
if git diff --quiet >/dev/null 2>&1 && git diff --cached --quiet >/dev/null 2>&1; then
  : # clean — proceed
else
  # Only abort if we're in the middle of a merge (have a MERGE_HEAD), not
  # if there are unrelated working-tree changes (which the user may have).
  if [ -f "$repo_root/.git/MERGE_HEAD" ]; then
    git merge --abort
    echo "Aborted previous merge. Working-tree changes are safe." >&2
  fi
fi

# Merge each session branch into release.
conflicts=()
for branch in $(git branch | grep '^ session/' | sed 's/^session\///'); do
  echo "Merging session/$branch..." >&2
  if ! git merge "session/$branch" --no-edit 2>&1; then
    conflicts+=("$branch")
  fi
done

# Check for merge failures (conflicts).
if [ ${#conflicts[@]} -gt 0 ]; then
  echo "" >&2
  echo "=== CONFLICTS DETECTED ===" >&2
  for conflict_branch in "${conflicts[@]}"; do
    echo "Session: session/$conflict_branch" >&2
    echo "Conflicting file(s):" >&2
    git diff --name-only --diff-filter=U 2>/dev/null || true
    echo "" >&2
  done
  echo "Resolve conflicts, then re-run this script." >&2
  exit 1
fi

# All merges clean — now merge release into main (only if release differs).
if git merge-base --is-ancestor release main 2>/dev/null; then
  echo "release is already merged into main. Nothing to merge." >&2
  echo "NOTE: this script merges branches. It does NOT deploy." >&2
  echo "      Nothing has been put live. To deploy: bin/wc-deploy.sh" >&2
  exit 0
fi

echo "Merging release into main..." >&2
git merge release -m "release: merge from staging (all session branches clean)"

echo "" >&2
echo "Merge complete -- nothing is live yet." >&2
echo "Push with:   git push origin main" >&2
echo "Deploy with: bin/wc-deploy.sh" >&2
