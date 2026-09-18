# Routing decision record — design

**Status:** implemented and deployed 2026-09-18, commit `ce1a813`, release
`ce1a813`. Built in a parallel session; the corrections in §1.1 (the hook
point's real name, and the unread `app.state.capability_table`) and §2 (the
`"[]"` ladder case) came back from that implementation and are folded in here.

**The suite is green, confirmed 2026-09-18.** 296 files run as one pytest process
per file, with the expected 6 skips. This change's own coverage is 21 tests and 8
subtests across §8's four classes, each verified by one of nine mutations that
turned a specific named test red.

Getting there took three false alarms, recorded because each is a pattern rather
than an incident:

- **Six delegation tests were red for an unrelated reason.** An uncommitted
  budget-enforcement change — 187 insertions across `tiered_delegation.py`,
  `delegation_startup.py`, `routes/delegation.py` and
  `tests/test_qa_delegation_routes.py` — was present in no commit and belonged to
  none of the three sessions that looked for its author. Causation was measured,
  not inferred: the same six passed against a clean `git archive` export of HEAD
  and failed in the working tree, same interpreter. It has since been committed
  as written, in `fedf31b`, on operator instruction.
- **A bisect pointed at the wrong commit.** One run per ref is three coin flips,
  not a bisect. The commit it accused touches a single markdown file, and no
  mechanism connected it to the failing picker test. The real cause was `/tmp`,
  a 1.9 GB tmpfs, at 100% from accumulated `git archive` exports.
- **Four browser files carry intra-file flake.** Each fails a *different* test on
  each whole-file run, always a Playwright `wait_for_selector` timeout, and each
  passes alone and against its parent commit. A named failure from a browser file
  is not evidence until it reproduces.

**Live state.** `delegation_routing_decision` exists in the production database
with both indexes and 0 rows — the recorder writes its first row the next time
an orchestrator materialises a plan.
**Scope:** record what the delegation classifier and ladder *would* decide for
every real orchestrator task, so that a later `learn` pass has ground truth to
argue from. Recording only. Nothing about routing behaviour changes.

---

## 1. Why this exists

The request that started this was "add a new type of task at the end of the
process which is learn, so you learn and improve on the mistakes found and
solved". Learning needs a record of decisions to learn from, and today there is
none. Four decisions were taken while designing it:

1. **Record first, propose second.** The learn pass is two mechanisms, not one.
   This spec covers only the first.
2. **A separate pass, off the critical path.** Learning never sits between a
   task and its result.
3. **All four proposal kinds are in scope eventually** — including proposing
   accuracy values directly, with the caveat in §7.
4. **Record routing decisions now**, because nothing else in the delegation
   subsystem is reachable from production yet.

Point 4 came out of a survey: `delegation_pipeline` has no importer outside
`tests/`. There is no oracle runner, no gate runner, no executor. The only
delegation code a production request can reach is the classifier and the
capability table, and the only place they are wired in is
`ModelRouter.assign_model`.

### 1.1 What the survey found next, and how it changes the hook point

The design presented in chat said the recorder would hook into
`ModelRouter.assign_model` (`orchestrator.py:277`). Checking before writing
this document showed that method is **never called in production**:

```
$ grep -rn assign_model --include=*.py .   # excluding worktrees and tests
orchestrator.py:277:    def assign_model(          # the definition
routes/delegation.py:107:    # comment referring to it
```

`ModelRouter` is instantiated (`orchestrator.py:525`,
`routes/orchestrators.py:894`) and its rules are configurable from the
orchestrator settings, but no code path invokes `assign_model`. A task's model
is whatever the plan markdown named: `PlanParser` puts it on
`ParsedTask.model`, and `OrchestratorEngine._materialise_plan`
(`orchestrator.py:743`) writes that value straight through to
`orchestrator_tasks.model` (`orchestrator.py:785` and `:798`). When the plan
names no model, the row stores `NULL` and the ladder is never consulted.

The same shape appears one level up, and it is worth recording because it is the
other half of the same illusion. `app.py:629` calls `validate_or_die()` and
stores the result:

```python
app.state.capability_table = await validate_or_die()
```

Nothing reads `app.state.capability_table`. The startup validation is real — it
refuses to boot on a bad table, which is spec 1.1 working as designed — but the
validated object it produces is then written to application state and never
consulted. So a capability-table change is validated at startup and still
reaches no routing decision.

So the delegation ladder is not merely unrecorded — it is unreachable
end-to-end. A recorder hooked into `assign_model` would have recorded zero rows
for as long as it existed, and the silence would have read as "no tasks were
routed" rather than "the hook is dead".

**The consequence for any document in this repo:** a claim that editing the
capability table changes what models live work runs on is not true today. It
describes the system this subsystem is being built toward, not the one running.

**The hook therefore moves to `OrchestratorEngine._materialise_plan`**, the
place a task's model is actually decided. The recorder computes what the delegation
subsystem *would* have chosen and stores it beside what was actually used. This
is shadow mode, and it is better than the original design for the learn pass's
purpose: it produces a labelled disagreement set — classifier verdict versus
operator's own choice — on real work, without changing a single routing
outcome.

---

## 2. What is recorded

One row per created task, written at task-creation time:

| Column | Meaning |
|---|---|
| `task_table` | `'orchestrator_tasks'`. A column, not a constant, because the table has been renamed once already (`supervisor_tasks` → `orchestrator_tasks`, `db.py:851`) and the rename map proves it can happen again. |
| `task_id` | The namespaced row id `_materialise_plan` builds (`<orchestrator[:8]>_<planId>`). Not a foreign key — see §5. |
| `task_type` | `Classification.task_type` from `delegation_classifier.classify`. |
| `score` | `Classification.score` (1–5). |
| `mutates` | `Classification.mutates`. |
| `source` | How the shadow model was arrived at: `rule` (an operator regex in `ModelRouter.rules` matched), `ladder` (the type is operational and its ladder produced a rung), `fallback` (neither — `config.ANTHROPIC_MODEL`). |
| `shadow_model` | The model the delegation subsystem would have chosen. |
| `actual_model` | What the task was actually given: `ParsedTask.model`, or `NULL` when the plan named none. |
| `ladder` | The full ladder as a JSON array of model ids. Kept because the ladder changes underneath the record every time the capability table is re-seeded, and a decision is not interpretable later without the menu it was choosing from. Three cases, and they must stay distinguishable: a consulted ladder is its rungs; an **operational type whose ladder came back empty** is `"[]"`; and `NULL` means no ladder was consulted at all — either the type is not operational, or an operator rule matched first and the ladder was never reached. Without the `"[]"` case, a fallback caused by an empty ladder and a fallback caused by a non-operational type would be one indistinguishable row, and the learn pass needs to tell them apart: the first is a capability-table defect, the second is a deliberate setting. |
| `decided_at` | Timestamp, `db._now()`. |

**Nothing else.** No prompt text, no task title, no description, no result. The
identifying columns point at `orchestrator_tasks`, which already holds all of
that; copying it would create a second store of user content with its own
retention question and its own leak surface, to no benefit.

---

## 3. Schema

```sql
CREATE TABLE IF NOT EXISTS delegation_routing_decision (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_table   TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    score        INTEGER NOT NULL,
    mutates      TEXT NOT NULL,
    source       TEXT NOT NULL,
    shadow_model TEXT NOT NULL,
    actual_model TEXT,
    ladder       TEXT,
    decided_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routing_decision_task
    ON delegation_routing_decision(task_table, task_id);
CREATE INDEX IF NOT EXISTS idx_routing_decision_type
    ON delegation_routing_decision(task_type, decided_at);
```

`id` is a surrogate key rather than `(task_table, task_id)` being primary,
because a task can be re-planned and a second decision for the same task id is
data, not a conflict to resolve.

Both indexes earn their place: the first serves "what was decided for this
task", the second serves the learn pass's own query shape, "every decision of
this type, newest first".

Accessors live in `routes/db_delegation.py` beside the other delegation tables,
and are registered in `db.py`'s dispatch map (`db.py:167-171`) the same way:

- `async def delegation_decision_record(**columns) -> int` — insert, returns
  the new `id`.
- `async def delegation_decisions_recent(task_type: str | None = None,
  limit: int = 200) -> list[dict]` — newest first.

---

## 4. Where it hooks

`OrchestratorEngine._materialise_plan`, immediately after each successful
`db.orchestrator_task_create`. After, not before: a decision record for a task
that failed to be created is a record of nothing.

The recorder itself is a new module, `delegation_recorder.py`, with one public
coroutine:

```python
async def record_decision(
    *,
    task_table: str,
    task_id: str,
    title: str,
    description: str,
    actual_model: str | None,
    router: ModelRouter | None = None,
) -> int | None
```

It returns the new row id, and it **raises rather than swallowing**. Failure
isolation belongs to the caller, because only the caller knows which task it was
recording for — and a warning that does not name the task is exactly what hid a
silent failure in this same loop once before (`orchestrator.py:755`).

It classifies `title + " " + description` (the same concatenation
`assign_model` uses, lowercased the same way), loads the capability table via
`delegation_startup.load_capability_table()`, resolves `source` and
`shadow_model` by the same precedence `assign_model` implements — operator rule,
then ladder for an operational type, then `config.ANTHROPIC_MODEL` — and writes
one row.

The precedence logic is duplicated in exactly one respect and it is deliberate:
`assign_model` returns only the model, and the recorder needs to know *which
branch produced it*. Rather than change `assign_model`'s return type — a method
with no production caller, whose signature three test files assert — the
recorder reimplements the three-branch decision and a test asserts the two
agree on a shared table of inputs (§8).

`OrchestratorEngine` has a `self.router` already (`orchestrator.py:525`), so
the operator's own rules are visible to the recorder without new plumbing.

---

## 5. Failure isolation

Recording is diagnostics. It must never be able to fail task creation.

- The whole call is wrapped in `try/except Exception` inside `_materialise_plan`,
  logging at `warning` and continuing. `_materialise_plan` already has a per-task
  `try` for exactly this reason, and the module comment at `orchestrator.py:755`
  records what a bare warning cost the last time it hid a failure — so the log
  line names the task id and the exception, not just "recording failed".
- **`task_id` is deliberately not a foreign key.** A `REFERENCES
  orchestrator_tasks(id)` would let a delete of an orchestrator's tasks fail on
  this table, which inverts the priority: the record exists to serve the task,
  never the other way round. Orphaned rows are acceptable and the learn pass
  tolerates them.
- No unique constraint, so a retry cannot raise.
- The table read (`load_capability_table`) is the one genuinely slow step. It is
  a full read of `delegation_capability` — 40 rows today — per created task. At
  the plan sizes this system produces (a handful to a few dozen tasks) that is
  acceptable; if a plan ever produces hundreds, the loader gets an engine-scoped
  cache. Not before, and not in this spec.

---

## 6. What this deliberately does not do

- **It does not change any routing outcome.** Every task still gets
  `ParsedTask.model`. This is the property the tests in §8 assert most loudly,
  because it is the one a future change is most likely to break by accident.
- **It does not wire `assign_model` into production.** Making the ladder
  actually route is a separate, larger decision with its own spec. This document
  makes that decision *measurable in advance* — after a few weeks of shadow
  rows, "what would flipping this on have changed" is a query rather than an
  argument.
- **It does not propose anything.** The proposal half of the learn pass reads
  this table; it is not designed here.
- **It does not record voice turns.** Voice has its own recording
  (`conversation_recording.py`) and does not go through the orchestrator.

---

## 7. The accuracy caveat, carried forward

One of the four approved proposal kinds is "propose accuracy values directly",
from observed production outcomes. That is legitimate but it is **not the same
measurement** as `delegation_capability.accuracy`, which comes from `bench/`
tasks with verifiers and a known-correct answer. A task that was marked done in
production was not checked against ground truth; it was checked against nobody
noticing.

Mixing the two in one column would silently degrade every ladder that reads it.
So when the proposal half is built, production-derived accuracy carries a
distinct §2.6 marker of its own — alongside the existing `†` (assumed price),
`‡` (blended from `usage_events`), `§` (CLI-transport accuracy) and `*`
(latency-only n) — and a ladder must be able to tell the two apart.

Recorded here so the constraint survives into the spec that needs it.

---

## 8. Testing

Four test classes, in `tests/test_qa_delegation_recorder.py`, following the
repo's `tests/test_qa_*.py` conventions.

**`RoutingDecisionSchemaTests`** — the table and both indexes exist after
`db.init()` against a throwaway `WC_DB_PATH`; `delegation_decision_record`
returns an id and round-trips every column including a `NULL` `actual_model`
and a `NULL` `ladder`; `delegation_decisions_recent` orders newest first and
honours its `task_type` filter and `limit`.

**`RecorderDecisionTests`** — for a fixed table and rule set, the recorder
produces the expected `(source, shadow_model, ladder)` for: an operator rule
match; an operational type with a ladder; an operational type with an empty
ladder; a non-operational type. Plus the agreement test that gives §4's
duplication its safety net — a shared table of inputs run through both
`ModelRouter.assign_model` and `record_decision`, asserting the chosen model is
identical for every row.

**`RecorderIsolationTests`** — task creation survives the recorder raising. The
mutation this guards is real: patch `record_decision` to raise, run
`_materialise_plan`, assert every task row was still created and that the warning
names the failing task id. Then the inverse, which is the one that actually
catches a regression — patch it to raise and assert the test *fails* if the
`try/except` is removed.

**`RoutingUnchangedTests`** — the property from §6. Create tasks through the
engine with the recorder active and assert every `orchestrator_tasks.model`
equals the `ParsedTask.model` that went in, including the `NULL` case. This must
be written so it can fail: a fixture whose plan names a model the ladder would
*not* have chosen, so a recorder that accidentally wrote back would be caught.

The last point is a rule for this whole file, not a stylistic note. Four
can't-fail tests were written into this subsystem during 0.19.0 and each was
caught by mutation rather than by the suite: a credential test asserting
against its own fixture, a `get_attribute("disabled")` check where `""` is
falsy, a "clean type" fixture that was not clean, and exclusions no fixture
ever reached. Every assertion here is verified by mutating the code it covers
and confirming the test goes red.

---

## 9. Open questions

- **Retention.** The table grows without bound. A row is roughly 200 bytes and
  the orchestrator produces tens of tasks a day, so this is years away from
  mattering — but the learn pass should decide a window rather than inherit
  "forever" by default.
- **Whether the operator's own choice is a label.** The disagreement set treats
  `actual_model` as the human's judgment, which is the most valuable signal
  here. It is not always deliberate: a plan may name a model because it was
  copied from another plan. The learn pass must not treat every disagreement as
  a classifier error.
