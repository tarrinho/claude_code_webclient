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

`orchestrators` is unchanged and remains the run record (title, status,
`progress_pct`, `config`).

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
   `work_dir`, so file artefacts flow: task 2 reads the CSV task 1 wrote. This
   requires a new capability — either `POST /api/chats` accepts an explicit
   `work_dir`, or `chat_update` allows it — and that is a real cost this spec
   previously hid.
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

The paragraph below is kept as written because the smaller half is still a
live coordination question.

### Which surface shows a task chat — and one signal that can be lost

`docs/superpowers/specs/2026-09-21-chat-list-hierarchy-design.md` (approved
with Pedro on 2026-09-21, the day after this one) renders chat relationships
as family cards in the sidebar, and its decision 2 states that *"a supervisor
fan-out adds small rows, not full chats"* — a subagent being a display-only
child node rather than an openable conversation.

**If that governs orchestrator tasks, this design does not work.** A task
being a real chat is not a presentation choice here; it is the mechanism.
Usage, transcripts, resume and the gallery are inherited precisely because a
task goes through `_start_turn`. Display-only rows inherit none of it.

It may be a wording collision rather than a disagreement: that spec's
decision 1 lists "subagents spawned in a chat" and "orchestrator and its
members" as *separate* relationship kinds, and in-chat Task-tool subagents
genuinely are display-only today — they leave no record anywhere. The
question is which of those decision 2 means.

Which surface shows task chats is reconcilable — hidden from the flat list,
shown as children of their run — but only if one surface owns it, or both
designs will build it.

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
| `routes/chats.py` | accept an explicit `work_dir` on create, so a run's tasks can share one (see "Workspace and data flow") |
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
