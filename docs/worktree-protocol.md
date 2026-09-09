# Worktree Protocol

Every Claude Code session working in this repository runs inside its own
`git worktree`. There is **no shared working tree**. This eliminates the
clobber/collide problem that plagued earlier development: two sessions editing
the same file silently overwrote each other, `git stash` destroyed every
peer's in-flight work, and `git add .` committed changes the current session
had not authored.

## Why worktrees

A git worktree is a separate working directory for the same repository. All
worktrees share the same object store (`.git/objects`) but have their own:

- Working tree (uncommitted edits)
- Index (staging area)
- Branch (head)

Two sessions can edit the same file simultaneously, commit, and merge
without ever touching each other's index or working tree. The only point of
conflict is the **content** — if two sessions changed the same line, git
reports a merge conflict. No more silent overwrites, no more lost work.

## How to use a worktree

### Starting a new session

Run `bin/wc-init.sh` from inside the repository:

```bash
cd /home/kali/projects/claude-code-webconsole
bin/wc-init.sh cweb1          # creates worktree at .claude/worktrees/cweb1/
                              # on branch session/cweb1
```

The script is idempotent. If the worktree already exists it re-attaches. If
the branch is gone (zombie from a crash) it removes the old branch and
creates a new one from `release` (or `main` if no release branch exists).

### Editing files

Edit freely. Every session works on its own branch and its own working tree.
You can edit any file in your worktree without fear of clobbering another
session's in-flight changes — they live in a different directory.

**Never run `git stash`.** It operates on the current worktree only, which
is harmless, but the command implies a mindset that causes bugs on a shared
tree. On worktrees it is just confusing.

### Committing

```bash
git add <paths>                   # only your own files
git commit -- <paths>             # only your own files
```

**Never use `git add .` or a whole-tree pathspec.** Your changes are isolated
to your worktree, but `git add .` still sweeps in every file you have
modified. Commit with explicit pathspecs so the commit is auditable.

### Reading other sessions' work

```bash
git show session/<other>:<path>   # read their version of a file
git log --oneline session/<other> # see what they committed
git worktree list                 # list all worktrees
```

No need to clone or fetch — the worktrees share the object store.

### Cleaning up (garbage collection)

Dead sessions leave behind orphaned worktree directories. Run the GC
periodically (it is idempotent):

```bash
bin/wc-gc.sh
```

This prunes the git worktree index and removes any worktree whose branch no
longer exists. Install it as a systemd user-timer to run daily:

```ini
# ~/.config/systemd/user/wc-gc.service
[Service]
Type=oneshot
ExecStart=/home/kali/projects/claude-code-webconsole/bin/wc-gc.sh

# ~/.config/systemd/user/wc-gc.timer
[Unit]
Description=Daily worktree garbage collection

[Timer]
OnCalendar=daily

[Install]
WantedBy=timers.target
```

Then: `systemctl --user enable --now wc-gc.timer`

## Release workflow

### Automated merge to release staging

```bash
bin/wc-release.sh
```

Flow:
1. Ensures the `release` branch exists (based on `main`).
2. Merges each `session/<name>` branch into `release`.
3. Clean merges are committed automatically.
4. Conflicts are listed with the conflicting file(s) and the owning session.
   The script exits `1` and does NOT continue — a human must resolve.
5. After resolving, re-run. The second invocation completes the release
   by merging `release` → `main`.

### Why a release branch

Batch-merging six sessions into `main` at once guarantees conflicts. Staging
on a `release` branch means conflicts are resolved where tests can verify
partial merges. Once `release` passes full integration tests, a single
`git merge release main` produces the release commit.

### Conflict resolution policy

1. **Clean merge** — automated, no human needed.
2. **Conflict detected** — script lists conflicting files and session names,
   exits `1`. The owning session is expected to resolve and re-commit.
3. **Multiple sessions conflicted** — consolidated single report, not one
   message per session.
4. **Session is alive** — it can message the owning session directly.
5. **Session is dead** — escalate to the user: `"Conflict on X from session Y
   (dead). --ours or --theirs or manual fix?"`

## What changed from the old shared-tree model

| Old | New |
|-----|-----|
| All sessions share one working tree | Each session has its own worktree directory |
| `git add .` commits peer changes too | `git add <path>` commits only your changes |
| `git stash` destroys peer work | No stash needed — each branch is isolated |
| Two sessions editing the same file silently clobber | Each has its own working tree — no clobber at the filesystem level |
| Merge conflicts at release time are messy | Staging on `release` branch isolates conflicts |
| `git hash-object -w --path` staging trick | Normal `git add <path>` + `git commit -- <path>` |
| No cleanup of orphaned worktrees | `bin/wc-gc.sh` removes dead sessions |

## When worktrees are not enough

Worktrees solve **file-level** contention (two sessions editing the same
working tree simultaneously). They do not solve **integration** testing: if
`test_qa_voice.py` touches `app.py` AND `voice.py` and those files have
unmerged changes in different worktrees, the test may fail in both places.

**Rule:** integration tests must pass on the merged `release` branch, not in
isolated worktrees. Worktrees are for unit tests, lint, and static analysis.
