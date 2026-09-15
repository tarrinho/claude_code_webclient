# Worktree-per-session isolation — design

Status: `[Proposed]`. Date: 2026-09-15. Supersedes nothing; it is the
precondition for evaluating an external governance harness (see §11).

## 1. The problem, measured

Five to eight `claude` sessions (`cweb1`..`cweb9`) edit **one working tree** at
`/home/kali/projects/claude-code-webconsole`, on **one branch** (`main`),
concurrently.

The cost is not theoretical and it is not small:

- **23 of the 116 entries in `rules.md`'s §16 registry — 20% — are shared-tree
  collision damage.** Counted by matching the registry rows against
  peer/shared-tree/`git add`/`reset --hard`/uncommitted/sweep. That is the
  single largest defect category in the project's own record.
- **341 commits landed in the 7 days to 2026-09-15**, peaking at 78 in one day
  and 5 within a single 10-minute window. Throughput is high and rising, so the
  collision surface grows with it.

The registry already names the failure modes, repeatedly and in detail:

- A commit sweeping another session's uncommitted edits into itself (#50, #52,
  #60, and the `731ee3e` incident of 2026-09-13, where one commit carried a
  naming fix, an unrelated `standby_reason` schema change and 463 lines of
  `bench/tasks.py` written by three different sessions).
- `git reset --hard` destroying a peer's uncommitted work — twice in one
  session on 2026-09-12.
- Authorship becoming unrecoverable: #50 records `git log -S` returning nothing
  for a line that "existed only in the shared working tree", and the entry
  declines to guess who wrote it.

## 2. Why the existing remedies did not hold

Three remedies have been tried. Each is still in the repository. None changed
the outcome.

**Prose, twice.** #50's remedy was a documented habit. #65 records that same
habit failing again against a session that had read it, and states the
conclusion plainly:

> Habits are not enforced by being written down [...] nothing in the repository
> *makes* the wrong interpreter behave differently from the right one, and the
> wrong one fails in the reassuring direction. [...] What is *not* acceptable is
> a third prose reminder — two have now failed.

**A real mechanism, bypassed.** `bin/wc-release.sh` already implements the
entire branch-per-session workflow: it merges each `session/*` branch into a
`release` branch, auto-commits clean merges, and on conflict reports the
conflicting files *with the owning session* and exits 1. It is written,
documented and maintained.

`git branch -a | grep -c 'session/'` returns **0**. It has never had an input.
341 commits in a week went directly to `main` instead.

This is the load-bearing observation of this design: **the project's problem is
not a missing mechanism. It is that the correct path is optional and longer than
the incorrect one.** `git commit` on `main` is three words and always works.

**A worktree convention, abandoned.** `.worktrees/` exists at the repo root
(created 2026-09-03), is listed in `.gitignore`, and is empty. Separately, one
worktree *is* live today —
`.claude/worktrees/db-modularize` on branch `worktree-db-modularize` — created
through the harness's own `EnterWorktree`. So the capability is present and
occasionally used, but it is a personal choice, not the default.

## 3. What changed that makes this feasible now

The historical blocker was that the running service was bound to the working
tree, so a second tree could not exist without fighting over it. **That is no
longer true.**

`~/.config/systemd/user/webconsole.service` now reads:

```
WorkingDirectory=%h/.local/share/webconsole/releases/current
ExecStart=/usr/bin/env bash %h/.local/share/webconsole/releases/current/launch.sh proxy
```

`releases/` holds immutable per-commit snapshots (`1e65726`, `47ab45c`,
`8ab0f96`, `efb625f` at time of writing), `current` is a symlink to the live
one, and `.previous` records the rollback target. The service reads none of them
from the working tree.

**Consequence: the working tree is now purely a development surface.** Nothing
in production depends on its contents. That removes the only structural
objection to giving each session its own.

## 4. The design

### 4.1 Three surfaces, with one writer each

| Surface | Path | Writer | Purpose |
| --- | --- | --- | --- |
| Session worktree | `.claude/worktrees/<session>` | exactly one session | all editing |
| Integration checkout | `/home/kali/projects/claude-code-webconsole` | nobody, interactively | merge, release, deploy |
| Release snapshot | `~/.local/share/webconsole/releases/<sha>` | `bin/wc-deploy.sh` | what runs |

Each session works on `session/<name>` in its own worktree. The shared checkout
stops being an editing surface and becomes integration-only. `wc-release.sh`
finally receives the inputs it was written for.

### 4.2 The enforcement, which is the whole point

Per §2, a convention will be bypassed. The rule must be enforced by a mechanism
that denies the wrong action rather than describing it.

**A `PreToolUse` hook denies `Edit`/`Write`/`NotebookEdit` to a governed path
when the session's resolved worktree is the integration checkout.**

Design constraints, each earned from a registry entry:

- **Fail-closed on its own failure.** If the hook cannot determine which
  worktree it is in, it denies. An enforcement layer that fails open is #65
  again in a new costume.
- **Deny, do not warn.** A warning is prose with extra steps.
- **Exempt the integration operations by path, not by identity.** Merges,
  `rules.md` registry appends and release scripts legitimately write in the
  integration checkout. Exempt those paths explicitly; never exempt a *caller*,
  because identity can be asserted by the thing being governed.
- **One escape hatch, loud and deliberate**: an environment variable
  (`WC_ALLOW_SHARED_TREE_WRITE=1`) that is never set in any shell profile, so
  using it is a conscious act that shows up in the transcript.

The `PreToolUse` slot is currently **free** — the only registered hook is a
user-level `UserPromptSubmit` running `.claude/hooks/log_pt_request.py`. No
conflict.

### 4.3 Bootstrap: what a fresh worktree does not have

This is where a naive rollout breaks, and it breaks silently. A `git worktree
add` produces a tree containing only *tracked* files. Everything in
`.gitignore` is absent. From the current `.gitignore`, that means a new
worktree has **no**:

| Missing | Consequence if unhandled | Handling |
| --- | --- | --- |
| `/CLAUDE.md` | **the agent has no instructions at all** | symlink to integration copy |
| `.venv/` | no test runner; every verification claim becomes false (#50, #65) | symlink to integration copy |
| `/data/` | no production DB in the worktree | **leave absent, deliberately** |
| `/PT_request.md` | — | no action: the hook writes to an absolute path in the integration checkout, so one log is correct |
| `.claude/settings.local.json` | local permissions | symlink |

Two of these deserve argument rather than a table row:

**`CLAUDE.md` is the severe one.** It is gitignored (deliberately — it carries
this deployment's backend routing detail). A session started in a fresh
worktree would therefore operate with none of the rules in it, including the
rules about not breaking the model-backend path. Symlinking it is mandatory,
and the bootstrap must **refuse to create a worktree if the symlink cannot be
made**, rather than produce a tree that looks fine and is ungoverned.

**`data/` staying absent is a feature, not a gap.** CLAUDE.md §9 already says
*"Never run `db.init()` against the production database."* Today nothing
enforces that; the production DB is simply present at the path the code
defaults to. A worktree without `data/` makes the unsafe thing fail loudly and
forces the documented `WC_DB_PATH` throwaway-copy behaviour. Do not symlink it.

`.venv` is symlinked rather than copied on the precedent of registry #103,
which preserved it for the same reason: it is minutes of pip installs and,
being untracked, cannot go stale by this route. The accepted risk is
`requirements.txt` skew across worktrees; §9 covers the detection.

### 4.4 `bin/wc-session-worktree.sh` — the entry point

One script, because the bootstrap has a mandatory step that must not be
optional:

```
bin/wc-session-worktree.sh <session-name>
```

1. Refuse if `<session-name>` is not a known session name.
2. `git worktree add .claude/worktrees/<name> -b session/<name>` (from
   `origin/main`, not local `HEAD` — see §9).
3. Symlink `CLAUDE.md`, `.venv`, `.claude/settings.local.json`. **Abort and
   remove the worktree if any symlink fails.**
4. Print the `cd` command. Do not exec it — the same restraint
   `bin/wc-session-wake.sh` already uses.

## 5. What this does to the daily loop

Editing, committing and pushing happen on `session/<name>` inside the session's
own worktree. Integration is `bin/wc-release.sh`, unchanged: it merges each
`session/*` into `release`, reports conflicts against their owning session, and
on a second invocation merges `release` → `main`. Deployment is
`bin/wc-deploy.sh` from the integration checkout, unchanged.

The cross-session announce-before-editing protocol in memory
(`project_claude_code_webconsole`) **does not go away**, but its job shrinks:
from "prevent collisions" (which it demonstrably could not) to "avoid two
sessions designing the same thing", which is a coordination problem rather than
a correctness one.

## 6. What this does not solve

Stated plainly, because a design that oversells is worse than the gap:

- **Semantic conflicts.** Two sessions changing the same function in different
  worktrees still conflict; they conflict at merge time, attributed, instead of
  silently in a shared file. That is the improvement — not the absence of
  conflict.
- **The shared production DB and the shared service.** Both remain single and
  shared. §17-style deploy tests still interrupt everyone.
- **Peer sessions reading a stale tree.** A session's worktree is a snapshot;
  it will not see a peer's merged work until it rebases.

## 7. Migration

Incremental, and reversible at every step. No flag day.

1. Land `bin/wc-session-worktree.sh` and the `PreToolUse` hook **with the hook
   in report-only mode** (logs what it would have denied, denies nothing).
2. Run report-only for 48 hours. The log is the evidence base: it shows exactly
   how many writes would have been blocked and from which sessions.
3. Move sessions one at a time. Each keeps working if it does not move.
4. Flip the hook to deny once every live session has a worktree.
5. Keep the escape hatch.

Step 2 is not ceremony. Every previous attempt here was adopted on conviction
and abandoned on friction; a measured blast radius before enforcement is what
distinguishes this from `.worktrees/` in §2.

## 8. Verification

- **Hook decision tests**, table-driven, in the style of
  `tests/test_qa_confirm_dialog_stacking.py` (assert the decision, not the
  prose): governed write in integration checkout → deny; same write in a
  session worktree → allow; exempted path in integration checkout → allow;
  undeterminable worktree → deny; escape hatch set → allow.
- **Bootstrap test**: create a worktree in a temp repo, assert `CLAUDE.md` and
  `.venv` resolve, assert `data/` is absent, assert a failed symlink leaves no
  worktree behind.
- **Mutation-verify the hook**, per the standing practice in this repo: revert
  the deny branch and confirm the tests fail. A gate that cannot be shown to
  fail is not evidence.
- **The measurement that matters**, at 30 days: collision-class registry
  entries added after the flip, against the 23-in-116 baseline in §1. If that
  rate does not fall, this design failed and should be said to have failed.

## 9. Open items

- **`[Open]` Branch base.** `-b session/<name>` from `origin/main` avoids
  inheriting a local `main` that peers have already advanced past, which is how
  the `config.py` `0.17.2 → 0.17.1` regression of 2026-09-12 reached `main`.
  Needs confirming against how `wc-release.sh` expects to merge.
- **`[Open]` `.venv` skew detection.** If one session changes
  `requirements.txt`, every worktree sharing the symlinked venv is affected at
  once, silently. Cheapest guard is a startup check comparing a hash of
  `requirements.txt` against one recorded in the venv.
- **`[Open]` Which paths are exempt** in the integration checkout. `rules.md`,
  `PT_request.md` and `bin/wc-*.sh` are the obvious candidates; the list must
  be explicit and tested, not a prefix match that quietly widens.
- **`[Open]` Worktree lifecycle.** Nothing here removes a worktree when a
  session ends. `bin/wc-session-standby.sh` suspends sessions today; the two
  should probably know about each other.

## 10. Alternatives rejected

- **Filesystem-read-only integration checkout.** Blunt; breaks merges and
  releases, which legitimately write there.
- **Branch-per-session without worktrees** (one tree, many branches). Does not
  help: the collision is in the *working tree*, and `git checkout` between
  sessions would be far worse.
- **A third prose reminder.** Explicitly ruled out by #65, in the registry's
  own words.

## 11. Relationship to Spark

This design was written while evaluating Celfocus' Spark framework
(`spark-agentic-engineering-framework`, v0.3.0/0.4.0, Beta) for adoption here.

Spark is a meta-harness: a Go binary plus a Claude Code plugin that gates every
tool call at `PreToolUse` against a stage/role model, fail-closed. Its thesis is
the same conclusion §2 reaches independently — *"nothing is enforced by prompt;
the gate is a fail-closed binary beside the harness, not instructions inside
it."*

**It does not solve the problem in §1.** Spark's model is one repo, one tree,
one run, one writer: `core/internal/cli/start.go` tells the operator that
starting a run "switches *this tree's* enforcement to it; to work both at once,
use a separate worktree" — and that line is a `print` statement, not a
mechanism. There is no locking anywhere in `core/`.

So Spark's own answer to our primary failure mode is *this document*. That
makes the sequencing unambiguous: **do this first.** Afterwards, a Spark pilot
becomes a one-command experiment inside a single session's worktree, and its
gate could replace the §4.2 hook rather than sit beside it. That evaluation is
deliberately not specified here.
