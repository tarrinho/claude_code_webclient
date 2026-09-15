# Worktree-per-session isolation — design

Status: `[Proposed]`. Date: 2026-09-15. Supersedes nothing; it is the
precondition for evaluating an external governance harness (see §11).

## 1. The problem, measured

Five to eight `claude` sessions (`cweb1`..`cweb9`) edit **one working tree** at
`/home/kali/projects/claude-code-webconsole`, on **one branch** (`main`),
concurrently.

The cost is not theoretical and it is not small:

- **Between 7 and 11 of the 116 entries in `rules.md`'s §16 registry — 6% to 9%
  — are shared-tree collision damage.** Method, stated because the first
  version of this document got it wrong: a keyword match over the registry rows
  (peer / shared tree / `git add` / `reset --hard` / uncommitted / sweep)
  returns 23 candidates, but reading all 23 shows roughly half are unrelated
  defects that merely use one of those words — #41 is a database writer bug,
  #47 and #48 are SQLite transaction bugs, #95 is a `d3` stub `TypeError`, #100
  is a systemd unit that was never enabled. **7 are unambiguous** (#50, #52,
  #64, #66, #69, #88, #99) and **4 more are shared-*resource* rather than
  shared-*tree*** (#60, #65, #82, #103).

  The commit that introduced this document (`f47218d`) quotes the uncorrected
  "23 of 116 — 20%" figure in its message, and commit messages cannot be
  edited after pushing. **That figure is wrong; this is the corrected one.**
  The error was a count taken from a regex and never read — exactly what
  registry #101 warns about ("the count is not a usable measure here") and what
  #61 calls "an assertion that could not fail". Producing it inside a document
  whose argument rests on that registry is worth recording rather than quietly
  fixing.

  The corrected figure is smaller and the design does not depend on it: the
  load-bearing evidence is §2, not this count. What the count cannot claim any
  more is "the largest defect category" — that would need all 116 classified.
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
| Integration checkout | `/home/kali/projects/claude-code-webconsole` | integration operations only (§4.2's exempt list) | merge, release, deploy |
| Release snapshot | `~/.local/share/webconsole/releases/<sha>` | `bin/wc-deploy.sh` | what runs |

Each session works on `session/<name>` in its own worktree. The shared checkout
stops being an editing surface and becomes integration-only. `wc-release.sh`
finally receives the inputs it was written for.

### 4.2 The enforcement, which is the whole point

Per §2, a convention will be bypassed. The rule must be enforced by a mechanism
that denies the wrong action rather than describing it.

**A `PreToolUse` hook denies `Edit`/`Write`/`NotebookEdit` whose resolved
target path lies inside the integration checkout.**

The gate is on the **target of the write**, not on where the session is. That
distinction is the difference between this working and this bricking the fleet,
so it is worth stating why.

**The rejected design, and why it fails.** The obvious formulation — "deny when
the session is in the integration checkout" — cannot be evaluated here. Our
sessions launch from the *parent* of the repository:

```
SCREEN -d -m -S cweb2 bash -c cd "/home/kali/projects" && exec claude --resume ...
```

so the session's working directory is `/home/kali/projects`, which is neither
the integration checkout nor a worktree. Combined with the fail-closed rule
below, "which worktree am I in?" resolves to *undeterminable* for every session,
and the gate denies **every write everywhere** on the day it is switched on.

This is not a hypothesis. Spark hit the same wall and documented it in
`core/internal/cli/sparkdir.go`: *"neither `$SPARK_PROJECT_DIR` nor process-cwd
discovery finds the engagement, and the payload's own cwd is the only reliable
workspace signal"* — and it then has to walk *up* from that cwd to find an
initialised repo. Walking up from `/home/kali/projects` finds nothing.

Spark's own write floor takes the target-path route instead —
`relUnderRoot(in.WriteTarget, in.ProjectRoot)`, with the comment *"a target
outside the project root is none of the framework's business."* Same shape as
the rule above, arrived at independently.

Target-path gating is also **simpler**, not a workaround: it needs no
session-to-worktree attribution, no cwd, and no identity.

| Resolved target | Decision |
| --- | --- |
| inside the integration checkout, not exempt | **deny** |
| inside any `.claude/worktrees/*` | allow |
| outside the repository entirely | allow — not our business |
| unreadable / unresolvable | **deny**, and record it as a *declared unknown* |

Design constraints, each earned from a registry entry:

- **Fail-closed, and say which kind of closed.** An unresolvable target denies,
  but is logged distinctly from an ordinary deny. Borrowed from Spark's
  `UnresolvedWrite` flag, which marks "a write happened and I could not see
  where" as different from "no write happened" — the two are otherwise
  indistinguishable, and only one of them is evidence the gate has a blind spot.
- **Deny, do not warn.** A warning is prose with extra steps.
- **The exempt list is empty, and that is a decision, not an omission.**
  Revision 2 assumed one would be needed. Working through what actually writes
  in the integration checkout, nothing qualifies: a `rules.md` registry append
  belongs to the fix that prompted it, so it happens in that session's worktree
  and arrives by merge; `PT_request.md` is written by a hook, and hooks are not
  tool calls, so they never reach this gate; the release and deploy scripts act
  through git rather than through a write tool. An empty exempt list is also the
  stronger design — there is no exemption surface to widen quietly over time.
  If a genuine case appears, exempt **by path, never by caller**: identity can
  be asserted by the thing being governed.
- **Extract the target once.** Where a call carries more than one candidate path,
  resolve it a single time and share it between the exemption check and the
  floor. Two independent parses of the same input is how you get a gate whose
  two halves disagree — Spark names this as a latent inconsistency it had to fix
  in `core/loop/verdict.go`.
- **One escape hatch, evaluated first**: `WC_ALLOW_SHARED_TREE_WRITE=1`, checked
  **before** the fail-closed branch. This ordering is load-bearing and must be a
  test, not a convention: it is the only way to repair a hook that is denying
  wrongly, including repairing the hook itself. A recovery path that the fault
  can disable is not a recovery path. The variable is never set in any shell
  profile, so using it is a deliberate act visible in the transcript.

The `PreToolUse` slot is currently **free** — verified, zero registered in
either `~/.claude/settings.json` or `.claude/settings.local.json`. The only
registered hook is a user-level `UserPromptSubmit` running
`.claude/hooks/log_pt_request.py`. No conflict.

**The payload fields, so nobody has to rediscover them.** Verified against a
working `PreToolUse` implementation (Spark's `core/internal/hostio/payload.go`),
Claude Code sends snake_case:

| Field | Carries |
| --- | --- |
| `tool_name` (top level) | which tool is being called |
| `tool_input.file_path` | the target of `Edit` and `Write` |
| `tool_input.notebook_path` | the target of `NotebookEdit` |
| `tool_input.command` | the command string, for `Bash` (see §4.2.1) |
| `cwd` (top level) | the session's directory — **not** usable for the worktree question, per the rejected design above |

### 4.2.1 Shell-mediated writes, and the commands a path gate cannot see

Revision 2 gated `Edit`/`Write`/`NotebookEdit` and said nothing about `Bash`.
That was the largest hole in it. `Bash` is the most-used tool in this project,
and a gate that ignores it is bypassed by habit rather than by intent —
`sed -i`, `> file`, `tee`, `python3 -c`. A gate that announces it prevents
shared-tree writes while leaving the busiest route open is worse than no gate,
because it manufactures confidence. That is the failure §2 is written against,
so it must not be reintroduced by the fix.

Shell writes split into two kinds with genuinely different risk profiles, and
they get different rules.

**(a) Path-bearing shell writes — extract the honest forms, declare the rest.**
Redirects (`>`, `>>`), `tee`, and in-place edits (`sed -i`, `perl -i`) name
their target, so the target goes through exactly the floor in §4.2. Anything
else — `python3 -c`, `./script.sh`, `find -delete` — is **not** parsed. The
precedent is measured rather than assumed: Spark's `core/loop/shellpaths.go` is
571 lines of best-effort extraction and its own authors still call the result
*"porous… the residue is named, not closed."* Chasing a shell grammar is a
tarpit we would lose in.

So the residue is **declared, not silently allowed**: a `Bash` call carrying a
recognised interpreter head (`python`, `python3`, `node`, `ruby`, `perl -e`) is
allowed but recorded as a *declared unknown*, the same marker §4.2 defines. That
turns an accepted blind spot into a counted one — §8's instrument then answers
"how often is this actually exercised", which nobody can currently answer.

**(b) Repo-wide destructive git — deny by command shape, because no path
exists to gate on.** This is the part a target-path gate is structurally blind
to, and it is where this project's worst losses came from. `git reset --hard`
names a *commit*; `git checkout -- .` names `.`; `git stash` names nothing at
all. None of them present a target path, so no amount of extraction would ever
see them — and registry #50 and #65 are both sessions destroying a peer's
uncommitted work exactly this way, twice in one day. The reflog currently holds
**52 `reset: moving to` entries**.

Four commands are denied outright when the resolved repository is the
integration checkout:

| Denied | Why |
| --- | --- |
| `git reset --hard` | the #50/#65 destruction mode |
| `git checkout -- <path>` / `git restore <path>` | same effect, discards uncommitted work |
| `git clean -f[d]` | removes untracked peer work, unrecoverable |
| `git stash` | already banned by project convention; it has cost work here before |

**These may fail closed, where ordinary writes may not.** The asymmetry is
deliberate: a destructive git command is rare and deliberate, so denying one
wrongly costs a moment and an escape-hatch re-run. An ordinary write is
constant, so denying those wrongly stops everything — which is precisely the
trap revision 1 fell into. Frequency, not severity, decides which way a rule
fails.

`git merge` and `git commit` are **not** on the list: they are what integration
is made of, and §4.1 expects them to run in the integration checkout.

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
being untracked, cannot go stale by this route.

**One risk in sharing it was checked rather than assumed, because it would have
been silent.** If the venv contained an editable install or a `.pth` pointing at
the integration checkout, tests run inside a worktree would import *the other
tree's* code and report confidently on the wrong source — the same shape of
false green as #50 and #65, and far harder to notice. Verified on 2026-09-15:
no editable install, no `.egg-link`, the only `.pth` present is a
path-independent coverage hook, and `pyvenv.cfg` reads `home = /usr/bin`.
Sharing it is safe. The remaining accepted risk is `requirements.txt` skew
across worktrees; §9 covers the detection.

### 4.4 `bin/wc-session-worktree.sh` — the entry point

One script, because the bootstrap has a mandatory step that must not be
optional:

```
bin/wc-session-worktree.sh <session-name>
```

1. Refuse if `<session-name>` is not a known session name.
2. `git fetch origin`, then **refuse if local `main` holds commits that
   `origin/main` does not** (§9). Integration is already broken in that state
   and branching around it hides the fact; the message should name the unpushed
   commits so the fix is obvious.
3. `git worktree add .claude/worktrees/<name> -b session/<name> origin/main` —
   the base is the remote branch, never local `HEAD`.
4. Symlink `CLAUDE.md`, `.venv`, `.claude/settings.local.json`. **Abort and
   remove the worktree if any symlink fails**, rather than leave a tree that
   looks fine and is ungoverned (§4.3).
5. Print the `cd` command. Do not exec it — the same restraint
   `bin/wc-session-wake.sh` already uses.

Steps 2 and 4 are both refusals, and both are the point: this script's job is to
make the ungoverned states unreachable, not to be convenient.

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

### 5.1 Integration cadence — the largest operational risk here

Isolation converts a *correctness* problem into a *divergence* problem, and
divergence is only tolerable if something closes it regularly. At the current
rate — 341 commits in a week, 78 in the peak day, across five to eight branches
— a week of unmerged session branches would produce a merge nobody wants to
own, and the predictable outcome is that the branches get abandoned and everyone
quietly returns to committing on `main`. That is how `.worktrees/` died (§2).

Revision 2 left this open. It is settled below, because an undecided cadence is
the thing most likely to end this the way `.worktrees/` ended — not by being
rejected, but by never being operated. The three candidates:

| Trigger | Cost | Failure mode |
| --- | --- | --- |
| On every session push | merge runs constantly, conflicts surface within minutes | noisy; a session pushing WIP triggers integration of unfinished work |
| Scheduled (e.g. hourly) | predictable, batched | a conflict can sit unnoticed for the whole interval |
| Before each deploy | integration is always tied to something a human wanted | branches diverge freely between deploys, so the conflict arrives at the worst moment |

**`[Decided]` On every push, with a session branch pushed only when its work is
coherent.** `wc-release.sh` already reports conflicts attributed to the owning
session, and that attribution is worth something only while the session is
still alive and still remembers the change. Batching throws away the one
feature the existing script has that no alternative offers, in exchange for
quieter logs. The "coherent work" half is what stops that becoming noise, and
it is a judgement each session makes — the same judgement it already makes
about when to commit.

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
- **Shell writes the extractor cannot read** (§4.2.1a): `python3 -c`,
  `./script.sh`, `find -delete` and anything else that hides its target inside a
  program. These are allowed and *counted*, not blocked. A determined bypass
  therefore exists and always will — the gate is built to stop habit, not
  intent, and there is no version of this that stops someone who has decided to
  route around it. The counter in §8 is what keeps that an informed position
  rather than an assumed one.

## 7. Migration

Incremental, and reversible at every step. No flag day.

1. Land `bin/wc-session-worktree.sh` and the `PreToolUse` hook **with the hook
   in report-only mode** (logs what it would have denied, denies nothing).
2. Run report-only for 48 hours. The log is the evidence base: it shows exactly
   how many writes would have been blocked and from which sessions.
3. **Drain the in-flight work already sitting in the shared tree.** This step is
   easy to skip and cannot be: at any moment the integration checkout holds
   uncommitted edits belonging to an unknown number of sessions. Measured twice
   within two hours on 2026-09-15: **14 files / 314 insertions**, then **8 files
   / 118 insertions**. It is not a backlog to clear once — it is a *churning
   pool*, so the drain has to happen against a quiet tree or it will never
   converge.

   Nothing in this design attributes that work, and neither can git: registry
   #50 records `git log -S` returning nothing for a line that "existed only in
   the shared working tree", and declines to guess who wrote it. The options are
   to commit it wholesale with an honest message saying authorship is unknown,
   or to preserve it on a quarantine branch and let sessions reclaim what they
   recognise. **Do not `git stash`** — memory (`project_claude_code_webconsole`)
   records that as already having cost work here.
4. Move sessions one at a time. Each keeps working if it does not move.
5. Flip the hook to deny once every live session has a worktree **and** §5.1's
   cadence is decided and running.
6. Keep the escape hatch.

Step 2 is not ceremony. Every previous attempt here was adopted on conviction
and abandoned on friction; a measured blast radius before enforcement is what
distinguishes this from `.worktrees/` in §2.

## 8. Verification

- **Hook decision tests**, table-driven, in the style of
  `tests/test_qa_confirm_dialog_stacking.py` (assert the decision, not the
  prose). For write tools: target inside integration checkout → deny; same file
  path inside a session worktree → allow; target outside the repository →
  allow; **unresolvable target → deny, recorded as a declared unknown rather
  than an ordinary deny**; escape hatch set → allow. One case pins the ordering
  from §4.2 — **escape hatch set *and* target unresolvable → allow** — which
  fails if the fail-closed branch is ever moved above the hatch and takes the
  recovery path down with it. One case pins §4.2's empty exempt list by
  asserting it *is* empty, so adding an entry later arrives with a failing test
  attached.

  For `Bash` (§4.2.1), the same table covers both halves: `echo x > <integration
  path>` → deny; the same redirect into a worktree → allow; `sed -i` and `tee`
  likewise; `python3 -c ...` → allow **and counted as a declared unknown**, which
  is the case that fails if someone later "tidies" the residue into a silent
  allow. Each of the four destructive git commands → deny against the
  integration checkout, allow against a worktree, and `git merge` / `git commit`
  → allow in both, since integration is made of them.
- **Bootstrap test**: create a worktree in a temp repo, assert `CLAUDE.md` and
  `.venv` resolve, assert `data/` is absent, assert a failed symlink leaves no
  worktree behind.
- **Mutation-verify the hook**, per the standing practice in this repo: revert
  the deny branch and confirm the tests fail. A gate that cannot be shown to
  fail is not evidence.
- **The measurement that matters**, at 30 days — and the instrument matters as
  much as the number. The first version of this section proposed counting
  collision-class registry entries added after the flip. That is **confounded**:
  registry entries are written by agents at their own discretion, so the
  instrument is made of the same behaviour being measured. If collisions stop,
  entries stop; if agents merely stop *noticing* collisions, entries also stop.
  The two are indistinguishable, and one of them is this design failing
  silently.

  Use mechanical counters instead, none of which depend on anyone choosing to
  write something down:

  | Instrument | Source | Reads as |
  | --- | --- | --- |
  | conflicted merges per release run | `bin/wc-release.sh` exit 1 + its conflict report | divergence cost — expected to *rise*, and that is fine |
  | hard resets | `git reflog` entries matching `reset: moving to` | the #50/#65 destruction mode — must fall to ~0 |
  | commits touching files changed by another branch within 24h | `git log --name-only` across `session/*` | true collision surface |
  | gate denials, split by kind | the hook's own log, incl. declared unknowns | whether the rule is doing anything, and where it is blind |

  The declared-unknown count is the one to watch for a false sense of safety: a
  gate that denies nothing *and* sees nothing is indistinguishable from a gate
  that works, which is §4.2's whole reason for separating those two outcomes.

## 9. Open items

- **`[Decided]` Branch base: `origin/main`, after an explicit fetch, and the
  bootstrap **refuses** when local `main` holds unpushed commits.** The base
  must be the one state that is shared, agreed and durable. Local `main` is
  neither — it can sit ahead of the remote with commits nobody else has, which
  is observable right now rather than theoretical: this checkout was measured at
  *ahead 2* on 2026-09-15, carrying two commits from another session's work that
  no peer could see. Branching from a local `main` in that condition hands the
  new session a private base and is how the `config.py` `0.17.2 → 0.17.1`
  regression of 2026-09-12 reached `main`.

  The refusal matters more than the choice. Unpushed commits on `main` mean
  integration is *already* broken; branching quietly from `origin/main` would
  route around that and leave it broken for the next session too. Refusing
  surfaces it at the one moment somebody is paying attention.
- **`[Open]` `.venv` skew detection.** If one session changes
  `requirements.txt`, every worktree sharing the symlinked venv is affected at
  once, silently. Cheapest guard is a startup check comparing a hash of
  `requirements.txt` against one recorded in the venv.
- **`[Decided]` Exempt paths: none.** Worked through in §4.2 — a `rules.md`
  append belongs to the fix that prompted it and arrives by merge, `PT_request.md`
  is written by a hook rather than a tool call, and the release scripts act
  through git. The candidates revision 2 listed all dissolve on inspection. The
  test asserting "exempted path → allow" is replaced by one asserting the list
  is empty, so adding an entry later is a deliberate act with a failing test
  attached.
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

**It does not solve the problem in §1** — though the first version of this
section overstated that, and the accurate form is more interesting.

Spark has **no locking** (no `flock`, `sync.Mutex` or `O_EXCL` anywhere in
`core/`), and its guidance for parallel work is advice, not a mechanism:
`core/internal/cli/start.go` tells the operator that starting a run "switches
*this tree's* enforcement to it; to work both at once, use a separate worktree"
— and that line is a `print` statement.

But its **state layout already separates correctly per worktree**, which is not
nothing. `.spark/active.json` and `.spark/trace.jsonl` are gitignored, so each
worktree carries its own run state; `.spark/runs/*/` is tracked, so run history
merges through git like any other file. Two worktrees running two Spark runs
would not corrupt each other. What is absent is **enforcement and visibility** —
nothing detects a sibling worktree with an active run, and nothing surfaces one
— not structural support.

That refinement does not change the sequencing, it sharpens it. Spark's own
answer to our primary failure mode is *this document*, and its run state is
already shaped to make the combination work. **Do this first.** Afterwards a
Spark pilot is a one-command experiment inside a single session's worktree, and
its gate — which reached §4.2's target-path rule independently — could replace
the §4.2 hook rather than sit beside it.

Worth noting for that later evaluation: this design borrows two ideas from
Spark's gate on their merits alone (the declared-unknown distinction, and
extracting a target once and sharing it), so a later swap would be a narrowing
of divergence rather than a rewrite.

## 12. Revision history

Kept in the document rather than left to git, because one of the corrections
contradicts a pushed commit message that cannot be edited.

**2026-09-15, revision 2** — review of revision 1 (`f47218d`), before any code
was written against it.

| Changed | Why |
| --- | --- |
| §4.2 gates on the **target path**, not the session's worktree | As written, revision 1 would have denied every write from every session the moment enforcement was switched on: our sessions' cwd is the repo's *parent*, so "which worktree am I in" was undeterminable, and the fail-closed rule then denies everything. This is the only change that was a defect rather than a weakness. |
| §1 metric corrected from "23 of 116 (20%)" to "7–11 of 116 (6–9%)", with method shown | The original was a regex count never read back. Reading all 23 showed roughly half are unrelated defects that merely use one of the keywords. `f47218d`'s commit message still quotes the wrong figure. |
| §5.1 added: integration cadence | Unspecified in revision 1, and the largest operational risk in the design — unmerged branches are how the previous attempt at isolation died. |
| §7 step 3 added: drain in-flight work | Revision 1 moved sessions to worktrees without saying what happens to the uncommitted, unattributable work already in the shared tree. Measured at 14 files then 8 within two hours — a churning pool, not a one-off cleanup. |
| §8 measurement replaced | Revision 1 proposed counting registry entries, an instrument made of the behaviour being measured. Replaced with mechanical counters. |
| §4.3 `.venv` sharing recorded as verified | Was an assumption. Checked: no editable install, no repo-bound `.pth`. It would have been a silent false-green if wrong. |
| §11 Spark characterisation corrected | "Has nothing for concurrency" was too strong. No locking and advisory-only guidance, but the run-state layout does separate per worktree. |
| §4.1 table wording | Contradicted §4.2's exempt list. |

The pattern in three of these — a count not read back, an assumption not
checked, an instrument made of its own subject — is the one registry #61 and
#101 already name. Worth stating plainly: a design document arguing for
enforcement over good intentions is not exempt from needing its own claims
verified.

**2026-09-15, revision 3** — the implementation-readiness pass. Revision 2 was
design-complete but would have stalled an implementer on decisions it had left
open, and it had one substantive hole.

| Changed | Why |
| --- | --- |
| §4.2.1 added: shell-mediated writes and destructive git | The hole. Revision 2 gated `Edit`/`Write`/`NotebookEdit` and said nothing about `Bash` — the most-used tool here — so the gate was bypassable by habit. Split into path-bearing writes (extract the honest forms, declare the residue) and repo-wide destructive git (deny by command shape, since `git reset --hard` presents no path for any gate to see). 52 `reset: moving to` entries in the reflog say which half matters. |
| §4.2 exempt list resolved to **empty** | Revision 2 assumed a list was needed. Every candidate dissolved on inspection. An empty list has no surface to widen. |
| §9 branch base decided: `origin/main`, refusing on unpushed local commits | Blocking — it is a line in the bootstrap script. The refusal is the substantive half: unpushed commits mean integration is already broken, and branching around that hides it. |
| §5.1 cadence decided: on every push | Left open in revision 2. An undecided cadence is how the previous attempt died. |
| §4.2 payload field table added | An implementer would otherwise have rediscovered it. Verified against a working `PreToolUse` implementation. |
| §8 test table extended | Covers the shell cases, the destructive-git cases, and asserts the exempt list is empty. |

The through-line of this revision is that **frequency, not severity, decides
which way a rule fails**: destructive git may fail closed because it is rare and
deliberate, ordinary writes may not because they are constant. Revision 1's
fleet-wide deny came from getting that backwards.
