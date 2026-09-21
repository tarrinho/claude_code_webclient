# Orchestrator: chats all the way down — design

**Status:** approved design, not yet implemented.

**Goal:** make the orchestrator something that works. A task becomes an
ordinary chat, so orchestration stops having an execution path of its own and
inherits everything the chat path has already proven — usage accounting,
transcripts, resume, standby, and the generated-images gallery.

**Diagrams** (in the gallery, Settings → Images, and on disk under
`/home/kali/projects/orchestrator-designs-2026-09-20/`):

- `orchestrator-A-APPROVED-lifecycle-and-states.svg` — this design: the run
  lifecycle, the task state machine (`pending → running → done | failed`, with
  dependents going to `blocked`), where the gallery hop happens, and the guard
  the whole thing rests on.
- `orchestrator-A-chats-all-the-way-down.svg`, `...-B-plan-as-data.svg`,
  `...-C-recipes.svg` — the three candidates this was chosen from, kept so the
  rejected options and their trade-offs remain readable rather than being
  recoverable only from a conversation.

---

## The evidence this design is answering

Measured against the production database on 2026-09-20, not asserted:

- The orchestrator has run **three times in three weeks**. `orchestrators`
  holds 3 rows: 2 `done`, 1 `error`.
- Its one real multi-task run (`fa583523`, "Main Supervisor", 2026-08-30)
  **failed both of its tasks** with `result = ''` and `progress_pct = 0.0`.
- The two runs that "succeeded" returned the machine's hostname and the word
  `ok`. Neither exercised orchestration.

So this is not a scheduler with bugs to file down. Nothing about the current
design has yet produced a useful run on this deployment.

### Why the failing run failed

`orchestrator.py:PlanParser.parse` extracts tasks from **free-text model
output** using a bespoke prose grammar:

```
<<PLAN
Task 1: Brief title - Description text {#task_id} [:model_id]
>>
```

If the planner's prose does not match the regexes, the run yields zero or
malformed tasks and the scheduler proceeds anyway — which is exactly the
`result = ''` signature above. The same code carries a documented security
note: `_MODEL_RE` extracts `[:(\S+)]`, *anything non-whitespace*, and that
value became the `--model` argv token for a child process, so a plan reading
`[:--mcp-config=/tmp/evil.json]` handed attacker-chosen argv to the CLI.

**Parsing model prose into a scheduler's argv is the defect.** This design
removes the parser rather than hardening it.

### Why images never reached the gallery

Images enter `generated_images` through exactly one call site:
one inside `_start_turn` (`routes/chats.py`, the `_new_workspace_images` /
`generated_image_record` pair at the end of the turn), which scans the chat's
`work_dir` (`_new_workspace_images`) and records what is new. The orchestrator
never goes through `_start_turn`, so anything its tasks produce is invisible to
the gallery **by construction**. This is a structural gap, not a bug, and no
amount of fixing the orchestrator's own code closes it while it keeps a
separate execution path.

---

## What already exists — and is therefore not invented here

The pieces are present and merely unconnected:

- **Child chat creation.** `POST /api/chats` already accepts `parent_chat_id`
  and creates a child chat (`routes/chats.py`, the `if parent_chat_id:` branch of the create handler). Built for voice
  handoff, so it also sets `is_temporary=1`, disables auto-answer and copies
  the parent's title — voice-specific behaviour this design does **not**
  inherit.
- **Run-to-chat membership.** `orchestrator_members` links a run to chat ids,
  and `orchestrator_member_add` is already called from
  `routes/orchestrators.py`. Today a run can only *adopt* existing chats
  (`_resolve_member`); it never creates one for a task. That is the gap.
- **The turn path.** `_start_turn` (`routes/chats.py`) runs a turn and
  then records usage, writes the transcript, and scans for images.
- **Sidebar filtering.** `web/assets/chat-list.js` already hides chats via
  `chats.filter(c => !c.is_temporary)`, so hiding task chats needs a flag, not
  new UI machinery.
- **Dependency ordering.** `orchestrator.py:TaskGraph` is sound and is kept.
- **Additive migrations.** `db._ensure_orchestrator_columns` is idempotent and
  runs every startup; the one new column follows that existing pattern.

---

## Architecture

A run is **a parent chat plus one child chat per task**.

```
Parent chat  ──plan turn──▶  proposed tasks  ──you approve/edit──▶  Run
                                                                     │
                            ┌────────────────────────────────────────┘
                            ▼        fan out, respecting depends_on
                  Task chat 1 … Task chat N   (real chats, hidden from sidebar)
                            │
                            ▼
                routes/chats.py `_start_turn`   ← unchanged, reused as-is
                            │
                usage · transcript · resume · standby · image scan
                            │
                            ▼
                        Gallery (no new code)
```

The load-bearing property: **the orchestrator no longer has an execution path
of its own.** CLAUDE.md's "change both paths or neither" (§1) stops applying to
it, and the gallery works because the chat path's scan is reached, not because
orchestration grew a copy of it.

---

## Data model

`orchestrators` remains the run record (title, status, `progress_pct`,
`config`) and gains **one** column, because the shared workspace has to live
somewhere and no existing column holds it:

| Column | Type | Meaning |
|---|---|---|
| `work_dir` | TEXT, nullable | The one directory every task chat of this run shares. NULL for the 3 legacy runs, which never had one. |

`orchestrator_tasks` gains **one** column:

| Column | Type | Meaning |
|---|---|---|
| `chat_id` | TEXT, nullable | The task's own chat. NULL means a legacy task from before this design. |

Applied through `_ensure_orchestrator_columns`, matching the additive
check-then-ALTER pattern already used there.

`status` gains one value: **`blocked`** — a task whose dependency failed. It is
distinct from `failed` (this task ran and did not succeed) and from `pending`
(waiting its turn, still expected to run).

**No backfill.** The 3 existing runs keep `chat_id = NULL`, which is exactly
how the UI distinguishes a legacy run from a new one. No history is rewritten
and nothing is destroyed.

---

## Workspace and data flow between tasks

The first version of this spec gave tasks `depends_on` and said a task runs
when its dependencies are `done` — and never said **how a dependent receives
anything**. That is not an omission that could be filled in during
implementation; the code actively prevents it. `work_dir` is *computed*, not
accepted: the create handler in `routes/chats.py` derives it from the title slug and
uniquifies it with a counter, and `work_dir` is not in
`db_chats._ALLOWED_CHAT_FIELDS`, so it cannot be set afterwards either.

Left alone, every task chat gets its own isolated directory and nothing
crosses between them. `depends_on` would be pure ordering with zero
information flow — "run the write-up after the research" while the writer
cannot read a single thing the researcher produced. That guts the feature.

**Both channels are needed, and for different cargo:**

1. **One workspace per run.** Every task chat in a run shares the run's
   `work_dir`, so file artefacts flow: task 2 reads the CSV task 1 wrote.
   **This needs no new capability**: `db.chat_create(chat_id, title,
   description, work_dir, owner_id)` already takes an explicit `work_dir`. It
   is only the HTTP create handler that computes one from the title slug, so
   the orchestrator calls the database layer directly and passes the run's
   directory. An earlier draft of this spec claimed a new parameter was
   needed; that was wrong.
2. **Predecessor results injected into the dependent's prompt.** Files carry
   artefacts; the prompt carries reasoning. A task that concluded something in
   prose leaves nothing on disk, so injection is not redundant with (1).

### What a shared workspace costs

The image scan filters `st_mtime <= since` (`_new_workspace_images` in `routes/chats.py`), so a
*sequential* task never re-records the files its predecessor wrote — they are
older than its own turn start. **Parallel tasks are different:** two tasks
starting together in one workspace will each see the other's images, and the
gallery's unique index is `(chat_id, path)`, so one file becomes two rows
under two chats.

That is accepted rather than solved. It is cosmetic, it only affects tasks
running concurrently in the same run, and the alternatives — attributing
images to the run instead of the chat, or suppressing the scan for task chats
— would each cost more than the duplicate does. Recorded here so it is a
decision rather than a surprise.

## Task source: propose, approve, run

The planning turn runs in the parent chat like any other turn and is asked for
a **JSON task list**: `title`, `prompt`, `depends_on`, and an optional `model`.

Models drift, so the parse can still fail. The design point is what failure
*costs*:

> **A human approves before anything executes, so a parse failure degrades
> instead of detonating.** Invalid JSON produces "here is what the planner
> said, write the rows yourself" — not a silent zero-task run. The current
> design has no such gate, which is why a mismatched regex became two failed
> tasks and 0% progress.

The repair is not better parsing. It is a parse whose failure mode is a visible
prompt rather than a broken run.

`model`, when present, is resolved against the **allowlist**
(`ai_machines.active_models`, per CLAUDE.md §0.1) and never used as a raw argv
token. A model id that the resolved backend does not serve is rejected at
approval time, where a person can see it, rather than mid-run where the gateway
answers 429 and it reads as a capacity problem.

### Lifecycle

1. Goal stated in the parent chat → plan turn proposes rows.
2. Rows render editable: add, edit, delete, reorder. Cycles in `depends_on` are
   rejected here, at approval, not at runtime.
3. **Run.** The scheduler creates a task chat for every row whose `depends_on`
   are `done`, and takes a turn in each through `_start_turn`.
4. On each completion the chat path records usage, writes the transcript and
   scans the workspace into the gallery. Dependents become eligible; repeat.
5. No rows left → run `done`. Progress is computed as `done / total`, not
   tracked separately, so it cannot disagree with the tasks.

---

## How the scheduler learns a task finished, and whether it failed

Both mechanisms already exist and must be **named**, not invented. Left vague,
the first is reimplemented as a polling loop and the second is got wrong in
the specific way CLAUDE.md §4 warns about.

**Completion.** `_start_turn` returns a `turns.LiveTurn`, which carries
`state` (`running | done | error | cancelled`), `finished_at`, and
`task: asyncio.Task`. The scheduler awaits that task and reads that state.
There is nothing to poll.

**Failure.** `LiveTurn.state` is the answer, and a `try`/`except` around the
turn is **not**. CLAUDE.md §4: *"a failed turn arrives as
`{"type": "error", "error": …}` in the event stream. It does not raise. Code
that only handles exceptions will treat a failed turn as a successful empty
one."* A successful-looking empty turn is precisely the signature of the
2026-08-30 run — two tasks, `result = ''`, no error anywhere.

`_start_turn` already handles the error frame correctly
(`routes/chats.py`, its `event.get("type") == "error"` branch). That is one more thing gained by reusing it instead
of re-implementing execution, and one more reason guard 1 below matters.

## Failure handling

- **A task fails** → `failed`, with its error stored. Its dependents become
  `blocked` **explicitly** — never skipped, never silently marked done.
  **Independent branches continue**, because one failed leaf must not abandon
  work that does not depend on it.
- **The run** → `degraded` when some tasks failed and others completed;
  `error` only when nothing could run at all. The existing single `error` row
  at 0% illustrates the problem: it cannot say which half broke.
- **Failures become readable.** A failed task has a real chat with a real
  transcript, so "it failed" is a conversation you can open, re-prompt or
  resume. The 2026-08-30 run left `result = ''` and nowhere to look. This is
  the largest practical gain of the whole design.
- **Retry is just another turn** in the existing chat — no new mechanism, and
  the history of what failed is preserved rather than overwritten.
- **Concurrency** is bounded by the runner's existing `MAX_CONCURRENT`
  semaphore, so a ten-task fan-out cannot stampede the host. This is not
  theoretical on a 3.73 GB box that was OOM-killed on 2026-09-20.

---

## Frontend

- **Task chats are hidden from the sidebar**, surfaced inside the run view,
  where any one can be opened to read or resume. With 102 active chats already,
  a 5-task run must not add 5 sidebar entries.
- Hiding is **not** keyed on `is_temporary` — that field carries voice-handoff
  deletion semantics this design must not inherit. It is keyed on a derived
  `is_task_chat` boolean added to the chat-list payload, computed from whether
  the chat id appears as an `orchestrator_tasks.chat_id`. **`chats` gains no
  column**: the fact is already implied by the task row, and storing it twice
  invites the two copies to disagree. One batched query per page, following
  `chat_ids_that_exist`'s precedent of answering a whole page's membership
  question in a single round trip rather than one per row. The existing
  `chat-list.js` filter is reused; only the predicate widens.
- The run view lists tasks with status, links into each task chat, and shows
  `blocked` distinctly from `failed` so a stalled dependency is legible.
- The plan-approval step is an editable table, not a text box: the structure is
  data by the time a person sees it.

### Resolved: interaction with the chat-list hierarchy design

`docs/superpowers/specs/2026-09-21-chat-list-hierarchy-design.md` decision 2
originally read "a supervisor fan-out adds small rows, not full chats", which
appeared to forbid this design's central mechanism. Raised with its author
rather than reconciled between specs; the answer is that **decision 2 governs
only in-chat `Task`-tool subagents and says nothing about orchestrator
tasks**. Corrected there in `f40dc1f3`, which rewords the decision and
retitles §4.2 "no capture, and no opinion" — that design reads and renders
`orchestrator_members` / `orchestrator_tasks` and writes nothing to them.

The two compose rather than collide: its `children` array already carries
both a `chat` kind and a `subagent` kind, so a task that is a real chat
renders as the former and needs nothing added there. **This design is
unaffected and needs no re-opening.**

### Which surface shows a task chat — proposed, awaiting Pedro

**Proposed: the family card owns the display.** Task chats hidden from the
flat root list, rendered as children inside their run's card.

**Provenance, stated because it matters:** this reached me as Pedro's ruling
*relayed by another session*, not from him directly. A peer relay is not the
operator's approval, and "some of your chats no longer appear in the sidebar"
is exactly the kind of change that should come from him rather than through a
chain of us. The hierarchy spec's author took the same view and recorded it as
an open decision rather than applying it (`7746ea6b`); this spec now matches
that treatment. An earlier version of this section asserted it as a settled
ruling, which was further than the evidence went.

If confirmed, it is what this design wants, for a reason of its own below.

No invention is needed for it. The hierarchy design's §4.3 already does
exactly this for voice children — "they become visible inside their parent's
card and nowhere else, so they never again appear as orphan rows in the flat
list" — and the ruling extends that same rule to orchestrator members. The
hiding predicate becomes **"not a root"** rather than "not present", which is
the distinction that lets this design keep task chats out of the sidebar
while the hierarchy design still nests them.

That mirror edit belongs in the hierarchy spec, not here: its composer's
precedence rule (orchestrator member → voice parent → root) is what
implements the hiding. Relayed to its author.

**Resolved by measurement — use a parent-based predicate, not
`is_temporary`.** Three facts, each checked against the tree and the live
database rather than reasoned from the names:

1. **The producer is already root-ness, not voice.** `is_temporary = 1` is
   set inside the `if parent_chat_id:` branch of the create handler, *not*
   inside `if voice_mode:`, and the comment above it says so outright:
   "Temporariness is derived from having a parent … and that is the only way
   a chat becomes temporary." Only the *consumer* comment at
   `chat-list.js:910` says voice, because voice is the only thing that
   currently makes children. That comment is what misled an earlier draft of
   this section.
2. **Switching the predicate changes nothing today.** Of the live chats:
   2 have a parent, 2 are temporary, **0** are parented-but-not-temporary and
   **0** are temporary-without-a-parent. The two sets are identical, so
   filtering on "is a root" hides exactly what `!is_temporary` hides now.
3. **This design cannot use `is_temporary` anyway.** Task 2 creates a task
   chat with `db.chat_create` plus `chat_update(parent_chat_id=…)`,
   deliberately bypassing the HTTP handler to share the run's `work_dir` —
   and `is_temporary` is **not in `_ALLOWED_CHAT_FIELDS`**, so that path
   cannot set it without widening the allowlist. The flag is reachable only
   through the create handler this design does not use.

So the predicate is `parent_chat_id IS NULL` for root-ness, leaving
`is_temporary` to mean ephemeral — which is what its producer comment already
believes it means. The cost of *not* splitting them is the one worth naming:
two unrelated concepts on one column, so the first change wanting "ephemeral"
to differ from "has a parent" has to separate them under load. Cheap now,
expensive later.

Every consumer of `is_temporary` outside tests, so the blast radius is known:
`chat-list.js:910` (this filter), `voice-tooltip.js:66` and `:84` (overlay
client state, keyed on `voice_mode` rather than this flag),
`routes/chats.py:389` (exposes it in the payload) and the column lists in
`routes/db_chats.py`. Nothing branches on it for cleanup, retention or turn
behaviour.

**Hiding a chat also hides its alerts, and that is not free.** Reported by the
session that changed the sidebar's per-row indicators (`873ecd74`,
`8a89b4aa`): the dot ladder is fed from `GET /api/orchestrator`'s `waiting`
bucket, filtered to `kind === "chat"` and `reason !== "done"`. A task chat
that asks a question lands in that bucket and would mark a row which, under
this design, **does not exist in the flat list** — so the signal is silently
lost rather than visibly broken. Either the run view surfaces waiting tasks
itself, or hiding them costs the operator the one indicator that says a task
is blocked on a question.

The same report carries a warning about how easily that feed misleads:
mapping the whole `waiting` bucket to the marker lit **61 of 64
conversations**, because 54 were in the bucket for `reason: "done"`. The
filter that makes it meaningful lives in `chat-list.js`'s `setSupervisor`,
not in the endpoint — so any second surface that consumes `waiting` inherits
the problem and has to re-apply the filter rather than assuming the endpoint
did.

---

## Components and scope

| File | Change |
|---|---|
| `orchestrator.py` | Scheduler replaces `OrchestratorEngine`; `TaskGraph` kept. ~1454 → ~250 lines |
| `routes/orchestrators.py` | Plan propose / approve / run endpoints; member-create beside member-adopt |
| `routes/db_orchestrators.py` | `chat_id` on tasks, `blocked` status, computed progress |
| `db.py` | One additive column in `_ensure_orchestrator_columns` |
| `web/orchestrator.html`, `web/assets/orchestrator.js` | Editable plan rows; task list linking into task chats |
| `web/assets/chat-list.js` | Hide task chats |

**Deleted:** `PlanParser` and its `<<PLAN>>` regexes; the `[:model]` token that
became a `--model` argv value; synthetic `supervisor_<uuid>` / `subtask_<id>`
ids; the bespoke usage-recording path built to compensate for them; the direct
`runner.run_turn` call in `_execute_task`.

---

## Testing

Weighted by what actually broke.

**Unit, no browser:**
- dependency ordering; a failed task blocks its dependents while independent
  branches continue;
- invalid plan JSON yields editable rows rather than zero tasks;
- `depends_on` cycles rejected at approval;
- a `model` outside the allowlist is rejected at approval.

**Integration, against a throwaway database:**
- a two-task run creates two chats, takes two turns, records usage for both,
  and marks the run `done`;
- a task whose workspace gains an image results in a `generated_images` row.

Never against the production database: `db.init()` migrates (CLAUDE.md §9).
Copy to a throwaway `WC_DB_PATH` with the SQLite backup API.

**Browser — one class, not the 19-minute file:**
- the plan-approval round trip;
- task chats absent from the sidebar.

A single class costs ~30s (measured: `DelegationBudgetKnobBrowserTests` is
29.41s), so "the whole file is too expensive" is not a reason to skip the class
covering what changed. Run it inside the rules.md §14 cap.

### Two architectural guards

These pin properties, not behaviour, in the spirit of
`test_qa_api_tokens.py::DevExemptionIsGoneTests` — which is what caught a real
auth-exemption defect on 2026-09-20.

1. **The orchestrator has no second execution path.** Assert task execution
   routes through `_start_turn`. This is the property that makes images and
   usage free; if it regresses, both stop silently.
2. **No prose parsed into argv.** Assert that no model id reaching `--model`
   originates from planner text, closing the `[:--mcp-config=...]` shape by
   construction rather than by vigilance.

---

## Rejected alternatives

Recorded so they are not re-proposed without their cost.

**A workspace per task, instead of per run.** More isolated, and it is what
the code does by default — which is exactly why it needs an explicit refusal.
It makes `depends_on` meaningless: a dependent cannot read what its
predecessor wrote, so dependencies degrade to ordering with no data flow. It
also leaves one directory per task on disk for ever. Isolation between tasks
of the same run is not a property anyone asked for; flow between them is the
point.

**Injecting predecessor results only, with no shared workspace.** Cheaper —
no change to chat creation — but it carries only prose. A task producing a
file, an image or a dataset has nothing to hand on, and images are the reason
this work started.

**Attributing gallery images to the run rather than the chat.** Would remove
the duplicate-row case for parallel tasks, at the cost of changing
`generated_images`' meaning for every existing row and every non-orchestrator
caller. A cosmetic duplicate is not worth a schema-wide semantic change.

**Catching exceptions around a task's turn.** The obvious way to detect
failure, and wrong: a failed turn is an event, not an exception (CLAUDE.md
§4). Reading `LiveTurn.state` is the correct mechanism.

## The risk worth stating plainly

**The gallery benefit is inherited, not implemented.** It holds only while
tasks go through `_start_turn`. If someone later "optimises" a task to call
`runner.run_turn` directly — which is what the current code does, and which
will look like a reasonable simplification — usage recording *and* image
capture both stop, with nothing raised and nothing logged.

Guard 1 exists for exactly that, and it is the single most important test in
this design.

---

## Open questions for the implementation plan, not this spec

- Whether the parent chat's own workspace should also be scanned, or only the
  task chats'. Both are defensible; the plan should pick one and say why.
- Whether a run should offer "re-run failed tasks only" in v1, or whether
  per-chat retry is sufficient. Per-chat retry needs no new mechanism, so v1
  can likely ship without the bulk action.
- Deletion semantics: whether deleting a run deletes its task chats. Existing
  precedent (ARCHITECTURE.md) is that deleting a chat deliberately leaves its
  workspace on disk, which argues for keeping task chats too.

  **The database half of this question is settled; the filesystem half is the
  whole question.** A gallery row outlives its chat: `generated_images`
  snapshots `chat_title` and `work_dir` rather than joining them live and has
  deliberately no foreign key to `chats` — the schema says why, that "an image
  must stay servable and identifiable after its chat row is gone" — and
  `chat_ids_that_exist` only chooses between linking back and saying "(chat
  deleted)", never authorising or filtering. Verified: `chat_delete` issues
  two DELETEs and nothing else, and there is no `rmtree` or `shutil` call
  anywhere in `routes/chats.py` or `routes/db_chats.py`, so deleting a chat
  today leaves its files alone.

  But a surviving row is **not** a surviving image. `_file_exists` is a
  self-healing read check that skips any row whose file has gone, naming "a
  workspace removed by hand" as the case it exists for. So the image survives
  only while the file does.

  That lands harder on this design than on the current one, and the shared
  workspace is why: **one run has one directory**, so any future "delete this
  run and its workspace" action would remove every image the run ever
  produced, in one go, while leaving every row intact and the gallery silently
  shorter. The deletion that costs images is the *workspace* one, never the
  chat one. Whatever the implementation plan decides here, it should decide it
  about the directory rather than about the chat rows.

---

# Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the orchestrator's private execution path with real chats,
so a run fans out into task chats that inherit usage, transcripts, resume and
the generated-images gallery from `_start_turn`.

**Architecture:** An orchestrator run is a parent chat plus one child chat per
task, all sharing one workspace directory. A scheduler creates each task's
chat when its `depends_on` are `done` and executes it by calling
`_start_turn`, reading `LiveTurn.state` for the outcome. The `<<PLAN>>` prose
grammar is deleted and replaced by a JSON plan a human approves before
anything runs.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, vanilla JS frontend, pytest +
Playwright.

**Spec:** this document, above.

## Global Constraints

- **Interpreter:** `.venv/bin/python -m pytest`, invoked bare. Any other
  interpreter silently skips the browser layer (rules.md #50, #65).
- **Never run `db.init()` against production.** It migrates. Use
  `WC_DB_PATH="$(bin/wc-throwaway-db.sh)/webconsole.db"`.
- **Run tests inside the cap:** `systemd-run --user --scope -q -p
  MemoryMax=500M -p MemorySwapMax=0`. Exit 137 is the cap, not a test result.
- **Shared tree.** Stage by pathspec, read `git diff --cached --name-only`
  before every commit, and never `git add -A` / `git commit -a` / `git reset
  --hard` (CLAUDE.md §11).
- **`owner_id` must be a real user UUID**, never the string `"admin"` —
  `chat_create` raises `ValueError` on it.
- **A failed turn is an event, not an exception** (CLAUDE.md §4). Read
  `LiveTurn.state`; never wrap a turn in `try`/`except` to detect failure.
- **Model ids come from the allowlist** (`ai_machines.active_models`), never
  from planner text.

## File Structure

| File | Responsibility |
|---|---|
| `orchestrator.py` | Scheduler + plan validation. `TaskGraph` kept; `PlanParser`, `ModelRouter`'s parsed-model path and the old engine deleted. |
| `routes/db_orchestrators.py` | `chat_id` on tasks, `blocked` status, computed progress. |
| `db.py` | One additive column in `_ensure_orchestrator_columns`. |
| `routes/orchestrators.py` | Plan propose / approve / run endpoints. |
| `web/orchestrator.html`, `web/assets/orchestrator.js` | Editable plan table; run view linking into task chats. |
| `web/assets/chat-list.js` | Hide non-root chats (family-card children). |
| `tests/test_qa_orchestrator_chats.py` | New: scheduler, dependencies, failure semantics. |
| `tests/test_qa_orchestrator_guards.py` | New: the two architectural guards. |

---

### Task 1: Schema — `chat_id` on tasks, `blocked` status

**Files:**
- Modify: `db.py` (`_ensure_orchestrator_columns`)
- Modify: `routes/db_orchestrators.py` (`orchestrator_task_create`, `orchestrator_task_update`)
- Test: `tests/test_qa_orchestrator_chats.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `orchestrator_tasks.chat_id` (TEXT, nullable);
  `orchestrators.work_dir` (TEXT, nullable);
  `orchestrator_task_update(..., chat_id: str | None = None)`;
  `orchestrator_update(..., work_dir: str | None = None)`;
  `"blocked"` as a valid status string.

Two columns, not one. An earlier draft of this plan added only `chat_id` and
then had Task 7 read `run["work_dir"]` — a column that does not exist. The
run's shared directory is the whole mechanism for artefact flow between
tasks, so it needs a home.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_orchestrator_chats.py
import asyncio, unittest, uuid
import db


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_tasks_table_has_a_chat_id_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(orchestrator_tasks)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertIn("chat_id", cols)

    async def test_runs_table_has_a_work_dir_column(self):
        """The run's shared directory: without it the tasks cannot pass
        files to one another, which is the point of one workspace per run."""
        cur = await db.db_conn.execute("PRAGMA table_info(orchestrators)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertIn("work_dir", cols)

    async def test_a_task_can_record_the_chat_that_runs_it(self):
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, "owner-uuid")
        await db.orchestrator_task_create(orch, "t1", "Research", None)
        await db.orchestrator_task_update(orch, "t1", "owner-uuid", chat_id="chat-abc")
        task = await db.orchestrator_task_get(orch, "t1")
        self.assertEqual(task["chat_id"], "chat-abc")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -v`
Expected: FAIL — `chat_id` not in columns.

- [ ] **Step 3: Add the column to the migration**

In `db._ensure_orchestrator_columns`, inside the existing `task_migrations`
dict (the same check-then-ALTER pattern already there):

```python
        "chat_id": "ALTER TABLE orchestrator_tasks ADD COLUMN chat_id TEXT",
```

and in the same function's `sup_migrations` dict, for the run itself:

```python
        "work_dir": "ALTER TABLE orchestrators ADD COLUMN work_dir TEXT",
```

- [ ] **Step 4: Let the update helpers write both**

Add `chat_id: str | None = None` to `orchestrator_task_update`'s signature and
`work_dir: str | None = None` to `orchestrator_update`'s, each included in its
allowlisted `SET` fields exactly as `status` and `result` already are.

The run's `work_dir` is set once, when the run is created: use the same
`PROJECTS_ROOT / <slug>-<date>` shape the chat create handler uses, so an
orchestrator workspace is indistinguishable from any other on disk.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add db.py routes/db_orchestrators.py tests/test_qa_orchestrator_chats.py
git diff --cached --name-only   # must be exactly those three
git commit -m "feat: record which chat runs an orchestrator task"
```

---

### Task 2: Create a task chat in the run's shared workspace

**Files:**
- Modify: `orchestrator.py`
- Test: `tests/test_qa_orchestrator_chats.py`

**Interfaces:**
- Consumes: Task 1's `chat_id` column.
- Produces: `async def create_task_chat(orchestrator_id: str, task_id: str,
  title: str, work_dir: str, owner_id: str, parent_chat_id: str) -> str`,
  returning the new chat id.

- [ ] **Step 1: Write the failing test**

```python
    async def test_task_chats_of_one_run_share_its_workspace(self):
        import orchestrator
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, "owner-uuid")
        await db.orchestrator_task_create(orch, "t1", "One", None)
        await db.orchestrator_task_create(orch, "t2", "Two", None)
        a = await orchestrator.create_task_chat(
            orch, "t1", "One", "/tmp/run-ws", "owner-uuid", "parent-chat")
        b = await orchestrator.create_task_chat(
            orch, "t2", "Two", "/tmp/run-ws", "owner-uuid", "parent-chat")
        self.assertNotEqual(a, b)
        chat_a = await db.chat_get(a, "owner-uuid")
        chat_b = await db.chat_get(b, "owner-uuid")
        # The point of the shared directory: artefacts flow between tasks.
        self.assertEqual(chat_a["work_dir"], "/tmp/run-ws")
        self.assertEqual(chat_b["work_dir"], "/tmp/run-ws")
        self.assertEqual(chat_a["parent_chat_id"], "parent-chat")
        # and the task now knows its chat
        self.assertEqual((await db.orchestrator_task_get(orch, "t1"))["chat_id"], a)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -k share -v`
Expected: FAIL — `orchestrator has no attribute 'create_task_chat'`.

- [ ] **Step 3: Implement it**

```python
async def create_task_chat(
    orchestrator_id: str,
    task_id: str,
    title: str,
    work_dir: str,
    owner_id: str,
    parent_chat_id: str,
) -> str:
    """Create the chat that will run one task, inside the run's workspace.

    db.chat_create is called directly rather than POSTing to /api/chats: the
    HTTP handler computes work_dir from the title slug and uniquifies it, and
    every task in a run must share ONE directory so artefacts flow between
    them. The database layer already takes work_dir as a parameter.
    """
    chat_id = uuid.uuid4().hex
    await db.chat_create(chat_id, title, None, work_dir, owner_id)
    await db.chat_update(chat_id, owner_id, parent_chat_id=parent_chat_id)
    await db.orchestrator_member_add(orchestrator_id, chat_id)
    await db.orchestrator_task_update(
        orchestrator_id, task_id, owner_id, chat_id=chat_id)
    return chat_id
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_qa_orchestrator_chats.py
git diff --cached --name-only
git commit -m "feat: create a task's chat inside the run's shared workspace"
```

---

### Task 3: Validate a JSON plan, and fail visibly

**Files:**
- Modify: `orchestrator.py`
- Test: `tests/test_qa_orchestrator_chats.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `def validate_plan(text: str, allowed_models: set[str]) ->
  tuple[list[dict], list[str]]` returning `(rows, errors)`. `rows` are dicts
  with keys `title`, `prompt`, `depends_on`, `model`. Non-empty `errors`
  means show the raw text for hand-editing; it is never a silent zero-task run.

- [ ] **Step 1: Write the failing tests**

```python
class PlanValidationTests(unittest.TestCase):
    def test_a_valid_plan_parses_to_rows(self):
        import orchestrator
        rows, errors = orchestrator.validate_plan(
            '[{"title":"A","prompt":"do a","depends_on":[]}]', set())
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["title"], "A")

    def test_invalid_json_is_an_error_not_an_empty_plan(self):
        """The 2026-08-30 signature: a bad plan produced zero tasks and ran
        anyway. Zero tasks with no error is the one outcome forbidden here."""
        import orchestrator
        rows, errors = orchestrator.validate_plan("I'll start by...", set())
        self.assertEqual(rows, [])
        self.assertTrue(errors, "a plan that cannot be read must say so")

    def test_a_model_outside_the_allowlist_is_rejected(self):
        import orchestrator
        rows, errors = orchestrator.validate_plan(
            '[{"title":"A","prompt":"x","depends_on":[],"model":"--mcp-config=/tmp/evil"}]',
            {"claude-opus-5"})
        self.assertTrue(any("model" in e for e in errors))

    def test_a_dependency_cycle_is_rejected_at_approval(self):
        import orchestrator
        rows, errors = orchestrator.validate_plan(
            '[{"title":"A","prompt":"x","depends_on":["b"],"id":"a"},'
            ' {"title":"B","prompt":"y","depends_on":["a"],"id":"b"}]', set())
        self.assertTrue(any("cycle" in e.lower() for e in errors))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -k Plan -v`
Expected: FAIL — `validate_plan` undefined.

- [ ] **Step 3: Implement it**

```python
def validate_plan(text: str, allowed_models: set[str]):
    """Turn planner output into task rows, or into errors a person can act on.

    Never returns ([], []) -- an unreadable plan yields errors, because a
    silent zero-task run is the failure this design exists to remove.
    """
    import json
    rows: list[dict] = []
    errors: list[str] = []
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return [], [f"the plan is not valid JSON: {exc}"]
    if not isinstance(parsed, list) or not parsed:
        return [], ["the plan must be a non-empty JSON array of tasks"]

    ids = {str(t.get("id") or i) for i, t in enumerate(parsed)}
    for i, task in enumerate(parsed):
        if not isinstance(task, dict):
            errors.append(f"task {i} is not an object")
            continue
        title = str(task.get("title") or "").strip()
        prompt = str(task.get("prompt") or "").strip()
        if not title or not prompt:
            errors.append(f"task {i} needs both a title and a prompt")
        deps = task.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"task {i}: depends_on must be a list")
            deps = []
        for d in deps:
            if str(d) not in ids:
                errors.append(f"task {i} depends on unknown task {d!r}")
        model = task.get("model")
        # Never a raw argv token: an allowlist membership test, so a plan
        # reading "--mcp-config=/tmp/evil" cannot reach --model.
        if model is not None and str(model) not in allowed_models:
            errors.append(f"task {i}: model {model!r} is not on the allowlist")
            model = None
        rows.append({
            "id": str(task.get("id") or i),
            "title": title, "prompt": prompt,
            "depends_on": [str(d) for d in deps], "model": model,
        })

    if _has_cycle(rows):
        errors.append("the plan has a dependency cycle")
    return (rows, errors) if not errors else ([], errors)


def _has_cycle(rows: list[dict]) -> bool:
    graph = {r["id"]: r["depends_on"] for r in rows}
    seen: set[str] = set()
    stack: set[str] = set()

    def visit(node: str) -> bool:
        if node in stack:
            return True
        if node in seen:
            return False
        seen.add(node); stack.add(node)
        for dep in graph.get(node, []):
            if visit(dep):
                return True
        stack.discard(node)
        return False

    return any(visit(n) for n in graph)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -k Plan -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_qa_orchestrator_chats.py
git diff --cached --name-only
git commit -m "feat: validate the plan as data, and refuse it visibly"
```

---

### Task 4: The scheduler — fan out, and read the outcome from LiveTurn

**Files:**
- Modify: `orchestrator.py`
- Test: `tests/test_qa_orchestrator_chats.py`

**Interfaces:**
- Consumes: `create_task_chat` (Task 2).
- Produces: `async def run_tasks(orchestrator_id: str, owner_id: str,
  parent_chat_id: str, work_dir: str) -> None`. Sets each task's status to
  `done`, `failed` or `blocked`, and the run's to `done`, `degraded` or
  `error`.

- [ ] **Step 1: Write the failing test**

```python
class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_task_blocks_dependents_and_spares_siblings(self):
        """The three-way outcome. b depends on a and must be blocked when a
        fails; c depends on nothing and must still run."""
        import orchestrator
        from unittest import mock
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, "owner-uuid")
        for tid, deps in (("a", []), ("b", ["a"]), ("c", [])):
            await db.orchestrator_task_create(
                orch, tid, tid.upper(), None, depends_on=deps)

        async def fake_turn(chat, owner, prompt, model):
            state = "error" if chat["title"] == "A" else "done"
            return mock.Mock(task=asyncio.sleep(0), state=state)

        with mock.patch("orchestrator._take_turn", side_effect=fake_turn):
            await orchestrator.run_tasks(orch, "owner-uuid", "parent", "/tmp/ws")

        status = {t["id"]: t["status"]
                  for t in await db.orchestrator_tasks_get(orch)}
        self.assertEqual(status["a"], "failed")
        self.assertEqual(status["b"], "blocked")
        self.assertEqual(status["c"], "done")
        run = await db.orchestrator_get(orch, "owner-uuid")
        self.assertEqual(run["status"], "degraded")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -k Scheduler -v`
Expected: FAIL — `run_tasks` undefined.

- [ ] **Step 3: Implement it**

```python
async def _take_turn(chat: dict, owner: str, prompt: str, model: str | None):
    """Execute one task by taking a turn in its own chat.

    This indirection exists to be patched in tests, and to be the SINGLE
    place the orchestrator touches execution -- guard 1 asserts that it
    routes through _start_turn and nothing else.
    """
    from routes.chats import _start_turn
    return await _start_turn(chat, owner, prompt, model)


async def run_tasks(orchestrator_id, owner_id, parent_chat_id, work_dir):
    tasks = {t["id"]: dict(t)
             for t in await db.orchestrator_tasks_get(orchestrator_id)}
    done: set[str] = set()
    failed: set[str] = set()

    while True:
        ready = [
            t for t in tasks.values()
            if t["status"] == "pending"
            and all(d in done for d in (t.get("depends_on") or []))
        ]
        # Anything still pending whose dependency failed is blocked, not
        # skipped and not silently done.
        for t in tasks.values():
            if t["status"] == "pending" and any(
                d in failed for d in (t.get("depends_on") or [])
            ):
                t["status"] = "blocked"
                await db.orchestrator_task_update(
                    orchestrator_id, t["id"], owner_id, status="blocked")
        if not ready:
            break

        for t in ready:
            chat_id = await create_task_chat(
                orchestrator_id, t["id"], t["title"], work_dir,
                owner_id, parent_chat_id)
            chat = await db.chat_get(chat_id, owner_id)
            prompt = await _prompt_for(orchestrator_id, t, owner_id)
            live = await _take_turn(chat, owner_id, prompt, t.get("model"))
            await live.task
            # CLAUDE.md s4: a failed turn is an EVENT. The state carries it;
            # an exception never arrives, so never look for one.
            ok = live.state == "done"
            t["status"] = "done" if ok else "failed"
            (done if ok else failed).add(t["id"])
            await db.orchestrator_task_update(
                orchestrator_id, t["id"], owner_id, status=t["status"])

    total = len(tasks)
    status = ("done" if len(done) == total
              else "error" if not done
              else "degraded")
    await db.orchestrator_update(
        orchestrator_id, owner_id, status=status,
        progress_pct=round(100.0 * len(done) / total, 1) if total else 0.0)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_qa_orchestrator_chats.py
git diff --cached --name-only
git commit -m "feat: schedule tasks by dependency and read outcomes from LiveTurn"
```

---

### Task 5: Give a dependent its predecessors' results

**Files:**
- Modify: `orchestrator.py`
- Test: `tests/test_qa_orchestrator_chats.py`

**Interfaces:**
- Consumes: Task 4's `run_tasks`.
- Produces: `async def _prompt_for(orchestrator_id: str, task: dict,
  owner_id: str) -> str` — the task's prompt, prefixed with each completed
  dependency's title and result.

- [ ] **Step 1: Write the failing test**

```python
    async def test_a_dependent_receives_its_predecessors_result(self):
        import orchestrator
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, "owner-uuid")
        await db.orchestrator_task_create(orch, "a", "Research", "find X")
        await db.orchestrator_task_create(
            orch, "b", "Write up", "write it", depends_on=["a"])
        await db.orchestrator_task_update(
            orch, "a", "owner-uuid", status="done", result="X is 42")
        task_b = await db.orchestrator_task_get(orch, "b")
        prompt = await orchestrator._prompt_for(orch, dict(task_b), "owner-uuid")
        self.assertIn("X is 42", prompt)
        self.assertIn("write it", prompt)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -k predecessors -v`
Expected: FAIL — `_prompt_for` undefined.

- [ ] **Step 3: Implement it**

```python
async def _prompt_for(orchestrator_id: str, task: dict, owner_id: str) -> str:
    """The task's own prompt, preceded by what its dependencies concluded.

    The shared workspace carries files between tasks; this carries reasoning.
    A task that concluded something in prose leaves nothing on disk, so the
    two channels are not redundant.
    """
    deps = task.get("depends_on") or []
    if not deps:
        return task.get("description") or task["title"]
    parts = []
    for dep_id in deps:
        dep = await db.orchestrator_task_get(orchestrator_id, dep_id)
        if dep and dep.get("result"):
            parts.append(f"### Result of {dep['title']}\n\n{dep['result']}")
    own = task.get("description") or task["title"]
    if not parts:
        return own
    return (
        "Earlier tasks in this run produced the following. Their files are in "
        "your working directory.\n\n" + "\n\n".join(parts) + "\n\n---\n\n" + own
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_chats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_qa_orchestrator_chats.py
git diff --cached --name-only
git commit -m "feat: pass predecessor results into a dependent task's prompt"
```

---

### Task 6: The two architectural guards

**Files:**
- Create: `tests/test_qa_orchestrator_guards.py`

**Interfaces:**
- Consumes: `orchestrator._take_turn` (Task 4).
- Produces: nothing consumed by later tasks.

These pin properties rather than behaviour, in the spirit of
`test_qa_api_tokens.py::DevExemptionIsGoneTests`. Guard 1 is the most
important test in this design: the gallery and usage benefits are inherited,
not implemented, and hold only while a task goes through `_start_turn`.

- [ ] **Step 1: Write the guards**

```python
"""The two properties this design rests on, asserted so they cannot rot."""
import inspect
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TheOrchestratorHasNoSecondExecutionPathTests(unittest.TestCase):
    def test_task_execution_routes_through_start_turn(self):
        """If this fails, usage recording AND gallery capture have both
        stopped, silently -- which is the state this design replaced."""
        import orchestrator
        src = inspect.getsource(orchestrator._take_turn)
        self.assertIn("_start_turn", src)

    def test_the_orchestrator_never_calls_the_runner_directly(self):
        source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
        for forbidden in ("runner.run_turn", "runner.stream_turn"):
            self.assertNotIn(
                forbidden, source,
                f"{forbidden} bypasses _start_turn, so the turn records no "
                "usage and its images never reach the gallery",
            )


class NoProseReachesArgvTests(unittest.TestCase):
    def test_the_plan_grammar_is_gone(self):
        source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn("<<PLAN", source)
        self.assertNotIn("_MODEL_RE", source)

    def test_a_model_from_plan_text_cannot_reach_the_cli(self):
        """`[:--mcp-config=/tmp/evil.json]` once handed attacker-chosen argv
        to the child. Membership of the allowlist is the only route now."""
        import orchestrator
        rows, errors = orchestrator.validate_plan(
            '[{"title":"A","prompt":"x","depends_on":[],'
            '"model":"--mcp-config=/tmp/evil.json"}]',
            {"claude-opus-5"},
        )
        self.assertEqual(rows, [])
        self.assertTrue(errors)
```

- [ ] **Step 2: Run them**

Run: `.venv/bin/python -m pytest tests/test_qa_orchestrator_guards.py -v`
Expected: PASS (4 tests) once Tasks 3–4 have landed.

- [ ] **Step 3: Mutation-verify each guard**

A guard that passes against both the fixed and the broken code is
decoration. Temporarily break each property and confirm the guard fails:

1. add `runner.run_turn(...)` to `orchestrator.py` → the second test fails;
2. add the string `<<PLAN` back → the third fails;
3. drop the allowlist check in `validate_plan` → the fourth fails.

Revert each immediately. Record the result in the commit message.

- [ ] **Step 4: Commit**

```bash
git add tests/test_qa_orchestrator_guards.py
git diff --cached --name-only
git commit -m "test: pin the no-second-execution-path and no-prose-argv properties"
```

---

### Task 7: Endpoints — propose, approve, run

**Files:**
- Modify: `routes/orchestrators.py`
- Test: `tests/test_qa_orchestrator_chats.py`

**Interfaces:**
- Consumes: `validate_plan` (3), `run_tasks` (4).
- Produces: `POST /api/orchestrators/{id}/plan` → `{rows, errors, raw}`;
  `POST /api/orchestrators/{id}/run` with body `{rows: [...]}` → `{ok: true}`.

- [ ] **Step 1: Write the failing test**

```python
class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreadable_plan_returns_errors_and_the_raw_text(self):
        """The approval gate's whole purpose: a bad plan is visible before
        anything runs, and the operator can still hand-write the rows."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        # ... authenticate as the test owner, create an orchestrator ...
        resp = client.post(f"/api/orchestrators/{orch}/plan",
                           json={"raw": "I'll start by researching"})
        body = resp.json()
        self.assertEqual(body["rows"], [])
        self.assertTrue(body["errors"])
        self.assertIn("I'll start by", body["raw"])
```

- [ ] **Step 2: Run it and watch it fail** — 404, no such route.

- [ ] **Step 3: Add the handlers**

```python
async def handle_orchestrator_plan(request: Request, orchestrator_id: str):
    """Validate a proposed plan. Never executes anything."""
    owner = await owner_of(request.state.session)
    data = await request.json()
    raw = str(data.get("raw") or "")
    allowed = await _allowed_models(owner)
    rows, errors = orchestrator.validate_plan(raw, allowed)
    return JSONResponse({"rows": rows, "errors": errors, "raw": raw})


async def handle_orchestrator_run(request: Request, orchestrator_id: str):
    """Persist the approved rows and start the run in the background."""
    owner = await owner_of(request.state.session)
    data = await request.json()
    rows = data.get("rows") or []
    if not rows:
        raise HTTPException(status_code=400, detail="no tasks to run")
    run = await db.orchestrator_get(orchestrator_id, owner)
    if not run:
        raise HTTPException(status_code=404, detail="orchestrator not found")
    for row in rows:
        await db.orchestrator_task_create(
            orchestrator_id, row["id"], row["title"], row["prompt"],
            model=row.get("model"), depends_on=row.get("depends_on") or [])
    asyncio.create_task(orchestrator.run_tasks(
        orchestrator_id, owner, run["planner_chat_id"], run["work_dir"]))
    return JSONResponse({"ok": True})
```

Register both beside the existing orchestrator routes.

- [ ] **Step 4: Run the tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add routes/orchestrators.py tests/test_qa_orchestrator_chats.py
git diff --cached --name-only
git commit -m "feat: propose, approve and run an orchestrator plan"
```

---

### Task 8: Frontend — plan table, run view, and hiding non-root chats

**Files:**
- Modify: `web/orchestrator.html`, `web/assets/orchestrator.js`
- Modify: `web/assets/chat-list.js`
- Test: `tests/test_frontend_browser.py` (one new class)

**Interfaces:**
- Consumes: Task 7's endpoints.
- Produces: nothing consumed by later tasks.

**Decide first, and verify rather than assume:** the spec's open question is
whether `chat-list.js`'s existing `is_temporary` filter is the right hook for
"not a root", or whether the predicate needs its own field. Read
`setSupervisor` and the filter at the top of the render path before changing
either. Per Pedro's ruling the predicate is **"not a root"**, and the
hierarchy design's composer owns nesting — so this task only *hides*; it does
not draw the family card.

- [ ] **Step 1: Write the browser test**

```python
class OrchestratorPlanBrowserTests(_BrowserFixture):
    def test_a_rejected_plan_shows_its_errors_and_stays_editable(self):
        self._login()
        self.page.goto(f"{self.base}/orchestrator", wait_until="domcontentloaded")
        self.page.click("#newRun")
        self.page.fill("#planRaw", "not json at all")
        self.page.click("#validatePlan")
        self.page.wait_for_selector(".plan-error", timeout=10_000)
        self.assertIn("JSON", self.page.inner_text(".plan-error"))
        # and the Run button must be unavailable while errors stand
        self.assertTrue(self.page.is_disabled("#runPlan"))

    def test_task_chats_do_not_appear_in_the_flat_sidebar(self):
        self._login()
        # ... create a run with one task via the API ...
        self.page.goto(self.base, wait_until="domcontentloaded")
        self.page.wait_for_selector("#chatList", timeout=10_000)
        rows = self.page.locator("#chatList .chat-row").all_inner_texts()
        self.assertNotIn("Task: One", rows)
```

- [ ] **Step 2: Run the class and watch it fail**

Run:
```bash
systemd-run --user --scope -q -p MemoryMax=500M -p MemorySwapMax=0 \
  .venv/bin/python -m pytest \
  "tests/test_frontend_browser.py::OrchestratorPlanBrowserTests" -v
```
Expected: FAIL. One class costs ~30s; do not run the whole 19-minute file.

- [ ] **Step 3: Build the plan table**

An editable table, not a text box — the structure is data by the time a
person sees it. Each row: title, prompt, `depends_on`, optional model. Errors
render as `.plan-error` and disable `#runPlan` while any remain.

- [ ] **Step 4: Switch the sidebar filter to root-ness**

In `chat-list.js` (line ~910), replace

```javascript
    const visible = chats.filter(c => !c.is_temporary);
```

with a predicate on having a parent, and correct the comment, which currently
claims a voice meaning the producer does not have:

```javascript
    // Show roots only. A chat with a parent is rendered inside its parent's
    // card, never as an orphan row here -- voice children and orchestrator
    // task chats alike. Keyed on parentage rather than is_temporary: the
    // create handler derives is_temporary FROM having a parent, so the two
    // agree today (2 of 2 live chats), but an orchestrator task chat is
    // created through db.chat_create, which cannot set is_temporary at all --
    // it is not in _ALLOWED_CHAT_FIELDS.
    const visible = chats.filter(c => !c.parent_chat_id);
```

`parent_chat_id` is already in the chat-list payload
(`routes/chats.py`'s serialiser), so no endpoint change is needed.

Verify it is behaviour-preserving before moving on: 0 live chats are
parented-but-not-temporary and 0 are temporary-without-a-parent, so no row
should change visibility.

- [ ] **Step 5: Re-run the class** — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/orchestrator.html web/assets/orchestrator.js \
        web/assets/chat-list.js tests/test_frontend_browser.py
git diff --cached --name-only
git commit -m "feat: editable plan table, and keep task chats out of the flat list"
```

---

### Task 9: Delete the old engine

**Files:**
- Modify: `orchestrator.py`

Do this **last**: the guards in Task 6 already forbid the deleted constructs
returning, so deletion is verified by tests rather than by reading.

- [ ] **Step 1: Delete `PlanParser`, its regexes, and `OrchestratorEngine`'s
      scheduler**, keeping `TaskGraph`, `ProgressEvent` and `ProgressTracker`.

- [ ] **Step 2: Delete the synthetic-id and bespoke usage-recording paths**
      (`supervisor_<uuid>`, `subtask_<id>`, `_record_usage`) — usage is now
      recorded by `_start_turn` for every task.

- [ ] **Step 3: Run the full non-browser suite**

```bash
systemd-run --user --scope -q -p MemoryMax=500M -p MemorySwapMax=0 \
  .venv/bin/python -m pytest -rs -p no:cacheprovider \
  --ignore=tests/test_frontend_browser.py
```
Expected: no new failures. Investigate every one that names `orchestrator`.

- [ ] **Step 4: Commit**

```bash
git add orchestrator.py
git diff --cached --name-only
git commit -m "refactor: delete the prose plan grammar and the old engine"
```

---

## Plan self-review

**Spec coverage.** Data model → Task 1. Shared workspace and artefact flow →
Task 2. Result injection → Task 5. Plan-as-data with a visible failure mode →
Tasks 3 and 7. Scheduler, three-way task outcome and run status → Task 4.
Both architectural guards → Task 6. Frontend and the hiding predicate → Task
8. Deletions → Task 9.

**Known gaps, deliberately left to the executor.** Task 7's test elides
authentication setup and orchestrator creation, because the surrounding
fixture differs between `TestClient` and browser tests and copying the wrong
one is worse than an explicit ellipsis. Task 8 describes the plan table's
behaviour rather than its markup, since `orchestrator.html` is another
session's active file and its conventions should be followed rather than
replaced.

**Not covered, and out of scope by the spec's own open questions:** re-running
only the failed tasks of a run, and what deleting a run does to its workspace.
The second matters more than it looks — the spec records that deleting the
*directory* is what costs the gallery its images, never deleting the chats.
