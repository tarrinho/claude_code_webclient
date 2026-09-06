# The next orchestrator — evidence over inference

**Date:** 2026-09-01
**Status:** draft for review, not approved
**Author:** cweb3 (Claude Opus 5)
**Supersedes in scope:** `2026-09-01-orchestrator-ux-design.md` (unbuilt; folded in
as Phase 2 below)
**Builds on:** `2026-08-30-orchestrator-orchestration-design.md`,
`2026-08-30-orchestrator-members-design.md`,
`2026-09-01-orchestrator-layout-correctness-design.md`
**Prior art borrowed from:** `siteboon/claudecodeui` — its transcript renders
each tool call as a collapsible card with a separate result block, and it carries
a `Files` view beside the chat. Both are taken here, the second narrowed to a
per-task evidence view. Three further ideas from it were considered and declined;
see *What I would deliberately not build*.

---

## The thesis

Every orchestrator defect in the registry is the same defect. Not a family — the
same one, five times:

| # | What the orchestrator told the user | What it had observed |
|---|---|---|
| 61 | "this agent is blocked on a question" | that its status was not `busy` |
| 62 | (nothing — the test guarding it could not fail) | — |
| 63 | "this member's work has finished" | nothing about the terminal it was running in |
| 64 | "this agent reports it is blocked" | that the word `blocked` appeared inside `unblocked` |
| 52 | HTTP 500 | that a member had never spoken |

In each case the orchestrator asserted a state it had not established. The plan
parser has the same shape: a task is marked `done` at `orchestrator.py:846` the
moment the subprocess returns, whatever it returned, so "done" means *the process
exited*, not *the work happened*.

So the organising principle for the next orchestrator is one sentence:

> **The orchestrator never reports what it has not observed, and where it cannot
> observe, it says so.**

That is not a slogan. It decides every design question below — what to stream,
what to persist, when to say `done`, and what to do when the answer is unknown.

---

## What actually exists today, measured

Verified in this tree rather than recalled. All four of these are load-bearing
for the design.

**Execution is sequential.** `TaskGraph.get_ready_tasks()` computes the set of
independently-runnable tasks and `run_schedule_loop` then runs them one at a
time (`for tid in ready:` followed by `await self._execute_task(...)`). Zero
occurrences of `asyncio.gather`, `Semaphore`, or a spawned per-task coroutine.
**The dependency graph is therefore decoration** — it correctly identifies what
could run in parallel and nothing uses the answer. The 2026-08-30 orchestration
design listed "keep one subtask's conversation visible while others run in
parallel" as a goal; that goal is unmet.

**A run does not survive a restart.** `_supervisor_engines` is a plain dict in
`app.py`. Engines are created on demand and never rebuilt at boot. The web
application restarted at least six times today; each restart silently orphaned
any running orchestrator, leaving a row that says `running` with nothing running.

**Orchestrator token spend is attributed to nothing.** Usage is recorded by the
*caller* via `runner.take_last_usage(chat_id)` — `app.py` does it for chat turns.
`orchestrator.py` never calls it. A orchestrator fanning out ten subtasks is
invisible in the usage tables.

**Nothing checks that a task did anything.** See above: `done` on return.

Plus the two gaps from the unbuilt UX spec: no streaming (a run is silent from
0% to 100%) and no retry (a failure is a dead end).

---

## Design

Six layers, in dependency order. Each is separately landable and separately
useful — that ordering is deliberate, because the last two are the ones most
likely to be cut for time and they must not be the ones everything else waits on.

### Layer 1 — Durable run state

**Problem it solves:** a restart currently destroys a run and leaves a lie in the
database.

The engine's authoritative state moves out of the process. `supervisor_tasks`
already persists per-task status, result and progress; what is missing is enough
to *resume*:

- `supervisor_runs` — one row per run: `supervisor_id`, `prompt`, `plan`,
  `started_at`, `finished_at`, `outcome`, `budget_usd`, `spent_usd`.
- `supervisor_tasks` gains `run_id`, `attempt`, `started_at`, `finished_at`,
  `stop_reason`, `input_tokens`, `output_tokens`, `cost_usd`.

At boot, `lifespan` reconciles: any run whose row says `running` with no live
engine is either resumed (if its unfinished tasks are re-runnable) or marked
`interrupted` with the reason recorded. **Never silently left as `running`.**
`interrupted` is a first-class outcome, not an error — it is what actually
happened, and inventing either `done` or `error` would be the thesis violated at
the storage layer.

A task interrupted mid-flight is marked `interrupted` and becomes re-runnable,
not `failed`. The distinction matters to a human deciding whether to retry.

### Layer 2 — Observation

**Problem it solves:** the orchestrator cannot see what its agents are doing, so
it infers.

Three sources, in order of trust:

1. **The stream.** `_execute_task` and `_run_planner_turn` move from the blocking
   `runner.run_turn` to `runner.stream_turn`, which yields
   `{"type": "text", "content": ...}` as output arrives.

   **`stream_turn` must gain an `owner` parameter first, landed and verified on
   its own.** `run_turn` has one and `stream_turn` does not. The orchestrator's
   chat ids (`subtask_<id>`, `supervisor_<uuid>`) are labels, not rows in
   `chats`, and backend resolution is keyed on a `chats` row — so without an
   owner the child gets no base URL and no API key and every turn dies on *"Not
   logged in"*. That has already been diagnosed once and fixed by adding the
   argument at the `run_turn` call site. Swapping the call without carrying the
   argument reintroduces it exactly.

   The stream **reports failure as an `error` event rather than raising.**
   `run_turn` converts that into a `TurnError`; a streaming consumer that ignores
   it reads a failed task as a successful empty one, which is the "false success"
   mode `_run_planner_turn` already carries a comment about.

   Output is coalesced before emission — flush on 400 characters, a newline, or
   500 ms. Per-token events would mean hundreds of SSE frames and DB writes per
   task.

2. **Conclusion, not inference.** `transcripts.turn_concluded(session_id)`
   already exists and reports `stop_reason == "end_turn"` **and** whether a
   prompt arrived after it. Both halves are required: a session that finished and
   was then given more work still carries `end_turn` as its newest stop reason.
   The orchestrator uses this rather than "the subprocess returned".

3. **The session registry**, for member agents. `~/.claude/sessions/<pid>.json`
   carries `status` ∈ `busy` / `idle` / `waiting`, written by Claude Code about
   itself. `idle` means concluded; `waiting` means a person is needed; an
   unrecognised value is treated as *blocked*, because over-reporting costs a
   dismissal and under-reporting leaves an agent stuck with nobody told.

**Cost is observed too.** After each turn the engine calls
`runner.take_last_usage(chat_id)` and writes the row against the run and the
task. Without this, Layer 3's budget has nothing to spend against.

### Layer 3 — Scheduling that uses the graph

**Problem it solves:** the graph is computed and then ignored.

`run_schedule_loop` dispatches ready tasks concurrently under two limits:

- a per-orchestrator `asyncio.Semaphore(config.SUPERVISOR_MAX_PARALLEL)`,
  defaulting to **2**, and
- the existing global `runner` semaphore (`MAX_CONCURRENT`, default 3), which
  already bounds concurrent Claude processes machine-wide.

Two limits rather than one, because they answer different questions: how much of
*this* run may proceed at once, and how much of *this machine* is in use. Nesting
them is what stops one orchestrator starving every conversation on the box.

**Budget is a first-class stop condition.** `budget_usd` on the run; when spend
crosses it the scheduler stops dispatching, sets outcome `budget_exhausted`, and
says so. A orchestrator that can fan out is a orchestrator that can spend, and the
only thing worse than a run that stops is one that does not.

Failure policy is explicit and per-task, not global: a failed task marks its
dependents `blocked` (they cannot run) while independent branches continue. Today
`any_failed()` turns the whole run red at the end regardless of how much
succeeded.

### Layer 4 — Acceptance

**Problem it solves:** `done` currently means the process exited.

This is the layer that most changes what the orchestrator *is*, and it is the one I
would fight hardest to keep.

It has two halves, and shipping only the first would miss the point. A check
produces a **verdict**; Layer 6's file list produces the **evidence**. Under this
design's thesis those are different kinds of thing: a verdict is a claim about an
exit code, evidence is something the user can audit. Ship the verdict alone and
the orchestrator is once again asserting a state the user must take on trust —
which is the shape of every registry entry at the top of this document.

Each task carries an optional **acceptance check** — one of:

- `command` — a shell command that must exit 0 (`pytest tests/x.py -q`,
  `ruff check app.py`). Run in the task's `work_dir`, output captured.
- `file_exists` / `file_contains` — a path, optionally a pattern.
- `none` — explicitly none, recorded as such.

The planner is asked to propose one per task; the prompt already asks for a
structured plan, so this is a format extension (`{check: ...}`) rather than a new
mechanism. A task's terminal state becomes:

| state | meaning |
|---|---|
| `done` | the turn concluded **and** the acceptance check passed |
| `unverified` | the turn concluded, check was `none` |
| `failed` | the turn errored, or the check failed |
| `interrupted` | the run stopped underneath it |
| `blocked` | a dependency failed |

`unverified` is the important addition. It is honest about the common case, and
it makes the absence of a check visible rather than indistinguishable from a
passing one — which is the same reason a skip must be printed rather than folded
into a total.

**Do not let the model self-assess.** Asking the agent "did you succeed?" makes
the report a second opinion from the same source. A command exit code is
evidence; a sentence is not.

### Layer 5 — Intervention

**Problem it solves:** the only controls today are stop, pause and delete.

- **Retry**, per task and per run. `POST /api/supervisors/{id}/retry` (run) and
  `.../tasks/{task_id}/retry`. Refuses unless the target is `failed`,
  `interrupted` or `blocked` — `409` with a reason, never a silent no-op, because
  retrying a running orchestrator would start a second engine against one graph.
  Increments `attempt`; keeps the previous attempt's result rather than
  overwriting it, so a human can compare.
- **Never automatic.** A run that failed for a reason retry cannot change — no
  credentials, no binary; both happened today — would otherwise loop. The human
  pressing the button is the check.
- **Cancel one task** without killing the run.
- **Replan from here** — keep completed tasks, re-plan the remainder with their
  results as context. This is the answer to "the plan was wrong", which is the
  most common real failure and currently has no answer but starting over.
- **Answer a question.** The orchestration design's "orchestrator answers agent
  questions" stays a *human*-triggered action, per the members design's ruling
  that the orchestrator never dispatches into a live terminal on its own. Driving a
  live agent types into its terminal; a person deciding to press send is the
  check that mechanism still has.

### Layer 6 — The surface

**Problem it solves:** the panels cannot show what the layers below now know.

The layout work is already specified in
`2026-09-01-orchestrator-layout-correctness-design.md` and is not restated here.
What the new state requires on top:

- **Activity as structured tool cards, not a text stream.** This replaces the
  raw `task_output` line the earlier draft proposed, and it is a better answer to
  the same problem.

  A running task shows what it is *doing*, one card per tool call: the tool name
  and its principal argument as the headline (`Read app.py`, `Edit db.py`,
  `Bash pytest -q`), full parameters behind a collapsed disclosure, and the
  result as a separate labelled block with a pass/fail marker. Timestamped.

  Two reasons this beats streaming prose. It is *scannable* — a wall of narration
  answers "is it alive" but not "is it doing the right thing", and the second
  question is why the panel is open. And it needs no new data: the CLI's
  `stream-json` frames already carry `tool_use` and `tool_result` blocks, and
  `transcripts.py` already parses blocks by `kind`, so the events exist and are
  being discarded.

  Prose from the agent still appears, between the cards, as it does in a
  conversation. The cards are structure over the same stream, not a replacement
  for it.

  Borrowed from `siteboon/claudecodeui`, which renders exactly this in its
  transcript.

- **An evidence view: what the task changed.** A per-task file list — created,
  modified, deleted — with a diff on click, scoped to the task's `work_dir`.

  This is the piece that completes Layer 4 rather than decorating it. An
  acceptance check returns a verdict; a file list is the *evidence*. Under this
  design's thesis those are not interchangeable: "the check passed" is a claim
  about a command's exit code, and "these four files changed, here is the diff"
  is an observation the user can audit. A verdict without evidence is exactly the
  shape of assertion the registry entries above are all instances of.

  It also answers the question a plan-executor cannot otherwise answer — *did
  this task do the thing, or something adjacent?* — which no exit code covers.

  Derived from the `Chat | Shell | Files` switcher in `claudecodeui`, narrowed:
  scoped to one task rather than the whole project, and read-only.

- **Terminal state shown as what it is.** `unverified` must not render as `done`.
  A tick and a tick-with-a-question-mark are different claims.
- **The acceptance check and its output** in the detail panel. If a check failed,
  its output is the single most useful thing on the screen.
- **Spend against budget**, per run, next to progress. Progress without cost is
  half the picture on a fan-out.
- **`attempt` visible** where it is greater than 1, or a retried task silently
  looks like a first attempt.
- **The task's session id**, shown in full where the detail panel has room. It is
  what correlates a task with `~/.claude/sessions/<pid>.json` and with
  `--resume`, and every debugging session today began by hunting for it.
  `claudecodeui` puts the full session UUID in its header; the same argument
  applies to a task.

---

## What I would deliberately not build

Cut with reasons, because an unexplained omission gets rebuilt.

- **Automatic retry, automatic replan, automatic anything.** Every automatic
  recovery path proposed here would have looped on at least one real failure from
  today.
- **The orchestrator answering agent questions unprompted.** Ruled out in the
  members design on safety grounds and that ruling still holds.
- **Nested supervisors.** A orchestrator spawning supervisors multiplies every
  observability gap above before any of them is closed.
- **Cross-machine orchestration.** The `ai_machines` table makes this tempting.
  One machine's orchestrator is not yet trustworthy.
- **A DAG editor in the UI.** "Replan from here" covers the real need at a
  fraction of the cost.
- **Model auto-selection by regex.** `ModelRouter._score_complexity` matches
  patterns against the task title to pick a model. It is guesswork wearing the
  costume of a policy, and the same unanchored-substring class of bug that put
  `should i` inside `should include` lives in exactly this kind of list. Let the
  planner name a model explicitly or inherit the default; delete the scorer.

Three ideas from `siteboon/claudecodeui` are deliberately **not** taken into this
design, having been considered:

- **A Shell tab.** They expose an interactive shell beside the transcript. This
  console reaches a terminal already, through the linked-session and question
  paths, and those are the paths that carry the safety argument from the members
  design — a human presses send. A general shell in the browser is a second,
  unguarded route to the same capability on a box that runs Claude with
  `--dangerously-skip-permissions`. Worth wanting; wants its own security
  discussion, not a line in a orchestrator spec.
- **`@` to reference files in the composer.** A real gap and a good idea, but it
  belongs to the *conversation* composer, not the orchestrator. Filing it here
  would bundle an unrelated feature into a orchestrator review and delay both.
- **A project → sessions tree in the sidebar, and per-session turn counts.** Also
  conversation-surface work. Our chats each carry a `work_dir` so grouping by it
  is natural, and a turn count is genuinely different information from the
  waiting badge — but neither is about a orchestrator run, and this spec is already
  six layers deep.

---

## Traps carried forward

Each of these has already cost a diagnosis cycle. They are here so the next
implementer pays once.

1. **`stream_turn` has no `owner`.** Land that parameter alone, first.
2. **The stream reports errors as events, not exceptions.**
3. **A `user` record in a transcript may be a tool result, not a prompt.** Use
   `transcripts._prompt_boundary`, never `type == "user"`, or no turn ever reads
   as concluded.
4. **Session status has three values, not one.** A comment in
   `db.read_claude_sessions` said the observed value was `busy`; two call sites
   were written to that and reported finished agents as blocked. The comment now
   names the CLI version it was observed against, which is the cheapest guard
   available for a fact about someone else's tool.
5. **Shared classifier, unshared inputs.** `classify_chat` is called by the
   sidebar and the members panel. It was given empty CLI maps by one of them and
   reached a different answer from identical logic. When a decision is
   centralised, its *arguments* become the duplication to watch.
6. **Chromium harnesses need `--user-data-dir`.** Without it each launch leaks a
   ~126 MB profile; a full `/tmp` then fails every browser test in the suite and
   leaks another. This is fixed in the tree but was uncommitted as of writing.
7. **Eight sessions share this working tree.** `orchestrator.py` and
   `web/orchestrator.js` are dirty most of the time. Check `git status` before
   editing, commit with an explicit pathspec, and verify the **committed** tree
   by extract-and-test.

---

## Landing order

Each step ships alone and leaves the orchestrator better than it found it.

| # | Step | Why here |
|---|---|---|
| 0 | `stream_turn` gains `owner`, verified alone | Everything in Layer 2 depends on it, and a regression here would be blamed on the streaming work |
| 1 | Record usage per orchestrator turn | One call site; makes the run's cost visible immediately and is a prerequisite for budget |
| 2 | `supervisor_runs` + boot reconciliation | Stops the database asserting `running` about nothing. Independent of everything else |
| 3 | Stream, rendered as tool cards | The single largest perceived change; the panel stops being a spinner. Cards rather than raw text because the events are already structured and prose does not answer "is it doing the right thing" |
| 4 | Retry, per run then per task | Turns a dead end into a decision |
| 5 | Concurrency under two semaphores, plus budget | Only now is fan-out safe to enable, because 1–3 make it observable and stoppable |
| 6a | Per-task evidence view (files changed, diff on click) | Lands before the checks on purpose: it is useful on its own, and it is what makes 6b's verdict auditable rather than another assertion |
| 6b | Acceptance checks and the `unverified` state | The deepest change; wants the rest stable underneath it |
| 7 | Replan-from-here | Needs 2, 4 and 6 to be meaningful |

---

## Verification

Per step, and none of it satisfied by a green suite alone:

- **Live, over HTTPS on the tailnet address.** Send a task that prints
  progressively and confirm text appears in the event log *before* the task
  finishes. That is the whole of step 3 and no unit test demonstrates it.
- **Restart mid-run** and confirm the run is resumed or marked `interrupted` —
  never left `running`.
- **Kill the proxy mid-task** and confirm the failure reaches the chat with its
  reason, the task lands `failed`, and retry appears.
- **A task whose acceptance check fails** must land `failed` with the check's
  output visible, even though the agent reported success in prose. This is the
  thesis under test; if this case passes, Layer 4 earns its cost.
- **A task that "succeeded" while changing nothing** must be visible as such: the
  evidence view shows an empty file list beside a passing check. That combination
  is the most interesting row the orchestrator can produce, and it is invisible
  today.
- **A tool card matches the transcript.** Compare the cards rendered for a task
  against the `tool_use` blocks in its transcript on disk. A card that omits a
  call, or invents one, is worse than no cards — it would be the surface
  asserting activity it did not observe.
- **Budget exhaustion** stops dispatch and says why.
- **Mutation-test every new suite**, and verify each mutation changed *behaviour*
  and not merely the file. Mutations to make: drop the `prompt_after` half of the
  conclusion check; make the acceptance check always pass; remove the second
  semaphore; let retry accept a `running` run.
- Run as `.venv/bin/python -m pytest -rs`, and **bare**, not `pytest tests/` —
  the latter collects 2184 of 2198 tests.

---

## Open questions for Pedro

1. **Acceptance checks run shell commands** in the task's `work_dir`, proposed by
   a model. That is a new execution path, even on a box that already runs Claude
   with `--dangerously-skip-permissions`. Options: allowlist the command's first
   token; require the operator to approve a check before first use; or accept it
   as no worse than what the agent can already do. My view is the third is
   honest but should be a decision rather than a default.
2. **Default parallelism of 2.** Higher finishes sooner and makes the event log
   much harder to read. Worth trying 2 before 3.
3. **Budget default.** A number that never trips teaches people to ignore it; one
   that trips constantly gets raised without thought. I would start with no
   default and require it per run for fan-out above 1.
