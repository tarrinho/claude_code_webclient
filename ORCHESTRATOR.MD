# The Orchestrator

What it is, what it actually does when you press Send, and where each part of
it lives. Written from the code as it stands, including the parts that do not
do what their names suggest — those are called out rather than smoothed over,
because a reader who trusts a name here will make a wrong change.

## 0. Two different things are called "orchestrator"

The word is overloaded in this codebase, and the two features share almost no
code:

| Name | Endpoint | What it is |
|---|---|---|
| The **inbox** | `GET /api/orchestrator` (singular) | "Which agents are waiting on me." Classifies every conversation and terminal session as waiting / working / updated. Drives the sidebar badge. Lives in `routes/orchestrators.py::handle_supervisor`. |
| The **orchestration engine** | `/api/orchestrators/...` (plural) | The subject of this document: takes a goal, asks a model to break it into tasks, and runs those tasks itself. |

A third, separate feature — the **supervisor map** (`/api/supervisor-map`,
`web/assets/supervisor-map.js`) — draws a tree of live agents and is unrelated
to both.

Everything below is about the plural one.

## 1. In one paragraph

An orchestrator is a saved container with a title, a status, a chat log, a task
list and a set of watched members. You give it a goal in plain English. It runs
one model turn whose only job is to emit a plan of numbered tasks; it parses
that plan into a dependency graph; then it works through the graph, running one
`claude` CLI turn per task, feeding each task the results of the tasks it
depends on, and recording progress, output and token spend as it goes. You watch
it happen over a server-sent-event stream.

Every model turn — planning and each task — is the ordinary WebConsole turn
path (`runner.run_turn`), so CLAUDE.md §0 holds here as everywhere else: the
console spawns the `claude` CLI with different parameters and never calls a
model API directly.

## 2. The moving parts

| File | Responsibility |
|---|---|
| `orchestrator.py` | The engine. Plan parsing, the task graph, the scheduler, task execution, usage recording. ~1290 lines, no HTTP, no SQL of its own. |
| `routes/orchestrators.py` | The HTTP surface, the live-engine registry, and both SSE streams. |
| `routes/db_orchestrators.py` | Every read and write against the four orchestrator tables, plus the per-run cost query. |
| `db.py` | Schema for `orchestrators`, `orchestrator_tasks`, `orchestrator_messages`, `orchestrator_members`, `orchestrator_progress`. |
| `web/orchestrator.html` | The standalone page: five panels, loaded in an iframe by the console's orchestrator pane. |
| `web/assets/orchestrator/*.js` | The page's modules: `main`, `state`, `api`, `dom`, `list`, `tasks`, `rail`, `stream`, `members`, `banners`, `layout`. |
| `web/assets/orchestrator.js` | Not the page. The console-side pane opener and the "add this conversation to an orchestrator" picker. |

Design specs, for the reasoning behind individual decisions, are in
`docs/superpowers/specs/`: `2026-08-30-orchestrator-orchestration-design.md`,
`2026-08-30-orchestrator-members-design.md`,
`2026-09-01-orchestrator-ux-design.md`,
`2026-09-01-orchestrator-layout-correctness-design.md`,
`2026-09-04-orchestrator-observability-design.md`.

## 3. The full lifecycle

### 3.1 Create

`POST /api/orchestrators` writes a row with a hex uuid4 id, the title (default
"New Orchestrator", capped at 200 characters), an optional description (500),
an optional JSON `config` blob (64 KB), status `idle`, and registers a live
`OrchestratorEngine` in the process.

### 3.2 Send a goal

`POST /api/orchestrators/{id}/send` with `{"prompt": "..."}`.

* The prompt is capped at `config.PROMPT_MAX_CHARS` (8000 by default) — the
  same cap the chat endpoints enforce. This path had none once, so an
  oversized prompt was refused at one door and accepted at another.
* An engine is created if this orchestrator has none live yet, or the existing
  one is looked up and marked recently used.
* `engine.start_from_user_prompt()` spawns the planning turn as a background
  task and returns immediately. The HTTP response is `{"status": "planning"}`;
  nothing about the run has happened yet when the client gets it.
* The user's prompt is stored as an `orchestrator_messages` row with role
  `user`, and the orchestrator row's status is set to `planning`.

### 3.3 The planning turn

`_run_planner_turn` does the following, in this order, and the order matters:

1. Mints a uuid4 `plan_chat_id` and **persists it** on the orchestrator row
   (`planner_chat_id`). Persisted before the turn runs, so that if the process
   dies mid-turn the tokens it spent are still attributable to this run. Before
   this existed, the first and usually largest turn of every run was recorded
   against an id nothing could resolve.
2. Builds the prompt: `SUPERVISOR_SYSTEM_PROMPT` first, then the user's goal.
   The system prompt comes first deliberately — it is the binding constraint,
   not background.
3. Resolves an explicit model with `runner.get_default_model(owner=...)`.
   Never `None`: passing `None` let the CLI choose its own default, and a
   gateway serving one local model answered a request for `claude-opus-5` with
   `429 No deployments available` — a routing failure wearing a capacity
   failure's clothes.
4. Runs the turn through `runner.run_turn(prompt, "supervisor_<plan_chat_id>",
   work_dir, plan_chat_id, model, owner_id)`. The **owner** argument is
   load-bearing: `supervisor_<uuid>` is a label, not a row in `chats`, so
   backend resolution has nothing to key on without it, and every turn used to
   die on "Not logged in — Please run /login".
   `work_dir` is `config.PROJECTS_ROOT`, resolved.
5. Writes a human-readable session name for the turn (`routes.naming`), so the
   planning turn shows up in session lists as something other than a uuid.
6. Records usage — **before** the empty-result check below, because a turn
   that produced no text still spent tokens.
7. If the reply is empty: status `error`, run over.
8. Parses the plan (see §4), stores the cleaned reply on the orchestrator row
   as `plan` (first 20 000 characters) whatever the parser made of it, and
   appends a `orchestrator`-role message showing the task titles.
9. If the parser found **no** tasks: status `error`, plus a `system` message
   quoting what the planner actually said. This is deliberate and was a real
   bug once: an unparsable plan used to fall through to the scheduler, which
   found an empty graph, decided everything was done and reported "done" at
   0%. A goal that never ran looked exactly like one that succeeded.
10. Otherwise materialises the tasks (§5), sets status `running`, and enters
    the scheduler loop.

Any exception anywhere in that sequence sets status `error` **and** appends a
`system` message carrying the exception text, because a UI that shows only the
word "error" leaves the one useful fact readable only over SSH.

### 3.4 Execution

The scheduler (§6) runs tasks until nothing can make further progress, then
sets a final status: `done` if everything completed, `error` if any task
failed, `idle` if the loop was stopped before the graph was finished.

## 4. The plan format and its parser

The planner is instructed (`SUPERVISOR_SYSTEM_PROMPT`) to emit **only** this:

```
<<PLAN
Task 1: Brief title - Description text {#task1} [:model_id]
Task 2: Brief title - Description text {#task1}
>>
```

`PlanParser.parse` extracts it:

* **Block delimiters.** `_PLAN_START_RE` accepts `<<PLAN` alone or `<<PLAN>>`
  on its own line; the end marker is a line of `>>`. Both spellings are
  accepted because the system prompt showed one and the request text asked for
  the other, and a model obeying either produced a block the parser could not
  find. If no block is found, the whole reply is parsed as if it were one.
* **Task lines.** `^\s*(?:task|#)\s*\d+\s*[.:)]?\s+(.+?)(?:\s+-\s+(.*))?$`.
  The number is consumed rather than captured — an earlier pattern captured it
  and produced a task list of rows literally named "1" and "2". The split
  between title and description is the first ` - `; a line with no dash is all
  title rather than being dropped.
* **Dependencies.** `{#task1, #task2}` in the description. References are
  resolved positionally: `taskN` means the Nth task in this plan. Self-
  references and references to tasks that do not exist are discarded, which is
  also what keeps a trivial cycle out of the graph.
* **Model override.** `[:model_id]` in the description. Two guards:
  placeholder words copied straight out of the instructions (`model`,
  `model-name`, `model_id`, `model-id`, `modelname`) are treated as absent,
  and anything failing `config.valid_model_id` is **rejected and logged**, not
  sanitised. That second guard is a security boundary: this value comes out of
  model-authored text and becomes the argument to `--model` in a subprocess,
  so a plan reading `[:--mcp-config=/tmp/evil.json]` would otherwise hand an
  attacker-chosen argv token to the child, reachable by prompt injection into
  whatever the planner was reading. A rejected id falls back to the backend's
  own model, which is what a plan with no `[:model]` already gets.
* **Ids.** Tasks are numbered `t001`, `t002`, … within the plan.

## 5. Materialising the plan

`_materialise_plan` turns parsed tasks into graph nodes and database rows.

Task row ids are **namespaced** as `<first 8 chars of orchestrator id>_t001`.
The parser numbers from 1 within each plan, and `orchestrator_tasks.id` is a
global primary key, so without this the second orchestrator ever created hit a
UNIQUE violation, had its writes swallowed by a bare warning, and sat at 0%
with an empty task list while its work actually ran. Namespacing happens here
rather than in the parser so `{#taskN}` references stay resolvable against the
plan's own numbering.

A row that cannot be written does not stop the rest: the `try` is inside the
loop, the failure is logged with a traceback, and the orchestrator is flagged
`degraded` with kind `task_create` (see §11). The flag is cleared once for the
whole plan, not per task — an earlier plan's missing row is not repaired by a
later plan's success.

## 6. The task graph and the scheduler

`TaskGraph` holds `TaskNode`s keyed by id. A node's status is one of
`pending`, `ready`, `running`, `done`, `failed`, `blocked`.

`get_ready_tasks()` returns the tasks whose dependencies are all `done`, and
marks a task `blocked` when any dependency has `failed`. `blocked` is treated
as terminal — it has to be, because a blocked task with no dependencies of its
own would otherwise fall through and be flipped back to `ready` on the next
pass, for ever.

`all_done()` counts `done`, `blocked` **and** `failed` as finished. Counting
only the first two made a single failed task spin the scheduler at 0.5-second
intervals with nothing runnable and no terminal state — finishing
unsuccessfully is still finishing, and whether the run failed is what
`any_failed()` answers separately.

`run_schedule_loop` is:

```
while running and not all_done():
    for tid in get_ready_tasks():        # awaited one at a time
        await _execute_task(...)
    await persist_progress()
    await wait_if_paused()
    await sleep(0.5)
```

**Tasks run one at a time.** The graph is a DAG and the tasks in a layer are
independent, but the loop `await`s each execution in turn, so two ready tasks
run sequentially, not concurrently. The dependency graph currently buys
ordering and context passing, not parallelism. (`config.MAX_CONCURRENT` limits
CLI processes globally and is not what serialises them here.)

The whole loop body is wrapped: it runs as a background task nobody awaits, so
an escaping exception would surface only as asyncio's "Task exception was never
retrieved", leaving the orchestrator sitting in `running` with nothing running.
`asyncio.CancelledError` is re-raised untouched — a shutdown is not a fault.

## 7. Running one task

`_execute_task(task_id, prompt, model)`:

* If the task carries no model, one is resolved explicitly, for the same
  reason as the planning turn.
* The row is written as `running` **before** the turn starts. `updated_at` is
  stamped by that write, and the UI reads it to show elapsed time. Without it
  a task was `pending` for its whole run and only ever became visible at the
  end, as `done` or `failed` — there was no way to see that one was in flight.
* Dependency context: for each completed dependency, `Task <id> result:
  <first 500 characters>` is appended under a "Context from dependent tasks:"
  heading. That 500-character truncation is the whole of what a task learns
  from its predecessors.
* The turn runs as `runner.run_turn(full_prompt, "supervisor_subtask_<id>",
  work_dir, "subtask_<id>", model, owner_id)` — same synthetic-chat-id and
  owner rules as the planner.
* On success: result stored, progress 100, status `done`, a `task_result`
  message appended to the chat, the row updated. The message body is
  `clean_result`-ed (§8); when cleaning leaves nothing, the message says the
  task "completed with no text output — it only made tool calls" rather than
  printing a header over blank space.
* On failure: status `failed`, **usage still recorded** — a turn that ran two
  minutes and then errored has been paid for, and recording only successes
  would make the orchestrator that fails most look like the cheapest.

Each of the four persistence steps has its own `try`, its own `degraded` kind,
and never propagates: a telemetry write failing must not be reported as the
turn having failed.

## 8. Cleaning results

`clean_result` strips lines that are *only* a tool call — `Bash(...)`,
`Read(...)`, `Edit(...)` and the rest of the CLI's tool names — and collapses
runs of blank lines. Two deliberate limits:

* A line carrying prose alongside a call ("I ran Bash(x) and it failed") is
  kept; it is the answer, not machinery. An earlier version matched a tool name
  anywhere on the line and deleted ordinary sentences like "Read the config
  file (see below)".
* Fenced code blocks are left entirely alone. These results routinely contain
  code, and a snippet line reading `Read(path)` is part of the answer.

## 9. Model selection — what is real and what is not

Real:

* `[:model_id]` in a plan line, validated as in §4.
* Otherwise `runner.get_default_model(owner=...)` — the backend's own model.

**Vestigial, and worth knowing before you read too much into it:**

* `ModelRouter.assign_model` has **no callers anywhere in the codebase**. The
  engine constructs `ModelRouter()` with no rules and re-constructs it when a
  `config` PATCH arrives, but never asks it for a model.
* `DEFAULT_RULES` and the complexity-based fallback inside `assign_model` are
  therefore unreached.
* `COMPLEXITY_PATTERNS` and `PlanParser._score_complexity` do run — every
  parsed task gets a `complexity` score of 1–5 — but `_materialise_plan` never
  copies it onto the `TaskNode`, nothing persists it, and nothing reads it.

So today, model routing is: what the plan asked for, or the backend default.
The rule engine is scaffolding for a feature that was never wired up.

## 10. Pause, resume, stop

* `POST /api/orchestrators/{id}/pause` — only valid from `planning` or
  `running` (409 otherwise). Sets an event the scheduler waits on and writes
  status `paused`. The handler tells the engine what the status *was* first
  (`set_status_for_pause`) so resume can restore the right one.
* `POST /api/orchestrators/{id}/resume` — only valid from `paused`. Restores
  the remembered status.
* A paused scheduler polls with a 2-second timeout rather than blocking
  indefinitely, so it still reaches the progress-persist line every couple of
  seconds and the UI never looks frozen.
* `PATCH` with `status` also drives the engine: `running` restarts the
  scheduler loop if it is not running; `idle`, `done` or `error` stops it.
* A pause only affects work not yet dispatched. A CLI turn already in flight
  runs to completion — there is no mechanism to suspend a `claude -p`
  subprocess mid-turn.

## 11. Persistence

Five tables (`db.py`):

* **`orchestrators`** — `id`, `title`, `description`, `config` (JSON), `owner_id`,
  `status`, `plan`, `progress_pct`, `created_at`, `updated_at`, `completed_at`,
  `planner_chat_id`, plus `degraded` / `degraded_reason`.
* **`orchestrator_tasks`** — `id`, `orchestrator_id`, `title`, `description`,
  `status`, `model`, `result`, `progress_pct`, `parent_task_id`, `depends_on`,
  `created_at`, `updated_at`.
* **`orchestrator_messages`** — the chat log: `role` (`user`, `orchestrator`,
  `system`), `content`, `metadata` (JSON: `{"kind": "plan" | "task_result" |
  "error" | "plan_unparsed"}`), `created_at`.
* **`orchestrator_members`** — `(orchestrator_id, chat_id)` as the composite
  primary key. That key *is* the uniqueness constraint `member_add`'s
  `ON CONFLICT` depends on.
* **`orchestrator_progress`** — a per-orchestrator snapshot table. Nothing in
  the codebase reads or writes it today; the `orchestrator_progress()` helper
  in `routes/db_orchestrators.py` likewise has no caller.

Three quirks to know when reading rows:

* `completed_at` is selected by every read and written by nothing —
  `orchestrator_update` has no parameter for it. A finished run leaves it
  null; the time a run ended is only recoverable from its last task's
  `updated_at`.

* `depends_on` is stored as a **JSON-encoded string**, never a real array —
  every row's column is at minimum the two-character string `[]`. The frontend
  parses it in two places (`tasks.js` and `rail.js`), each keeping its own copy
  of the parse rather than importing across a module cycle.
* `degraded` is a diagnostic flag, not a fault log. `mark_degraded(kind,
  detail)` sets `degraded_reason` to `"<kind>: <detail>"`;
  `clear_degraded(kind)` only clears when the reason currently names that same
  kind. Because the column is single-valued, kind A marking, then B marking,
  then B clearing loses the record that A ever happened. Accepted: this is a
  signal to go and read the logs.

## 12. Cost accounting

The engine records usage for every turn it spends — planning and tasks alike —
one `usage_events` row per model, `origin="orchestrator"`, keyed on the
synthetic chat id. Three details that were each a bug first:

* The `provider` written is the **display kind** from `shared.backend_kind`,
  not the machine's raw provider column. Writing the raw value stored
  `claude_code`, which is not one of those kinds, so the usage API suppressed
  every orchestrator turn's cost as though it had run on a gateway.
* `cost_usd` is attached to the **first row only** of a turn. The CLI reports
  cost for the whole turn, not per model, so spreading it across each
  `modelUsage` entry bills a two-model turn twice.
* Attempts discarded by a content-quality retry are collected separately via
  `runner.take_retried_usage` and recorded too. They spent real tokens.

`db.orchestrator_cost(id, owner)` sums it back per run. It gathers the ids in
Python — `subtask_<task id>` for each task, plus `planner_chat_id` — and passes
them as parameters rather than matching `LIKE 'subtask_%'`, which would sum
another run's tasks. `cost_usd` counts only rows whose provider is
`through_claude_code`, matching how cost is blanked everywhere else it is
shown; when rows are left out, `cost_partial` is set and `cost_note` says why.
Tokens are summed over every row regardless, because counts are meaningful
whatever served them. A run with no turns reports `cost_usd: 0.0` rather than
null — provably nothing spent is a different statement from "cannot say".

The figure rides along with `GET /api/orchestrators/{id}/tasks` rather than
having an endpoint of its own: the page showing progress is the page that
should say what the progress cost, and it already polls that one.

## 13. HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/orchestrators` | List this owner's orchestrators. |
| POST | `/api/orchestrators` | Create one. 429 past 50 per owner. |
| GET | `/api/orchestrators/{id}` | One orchestrator row. |
| PATCH | `/api/orchestrators/{id}` | Update title / description / status / config. Status changes also start or stop the engine. |
| DELETE | `/api/orchestrators/{id}` | Delete, and drop the live engine. |
| POST | `/api/orchestrators/{id}/send` | Send the goal; begins planning. |
| POST | `/api/orchestrators/{id}/pause` | 409 unless planning or running. |
| POST | `/api/orchestrators/{id}/resume` | 409 unless paused. |
| GET | `/api/orchestrators/{id}/tasks` | Task list **plus** the run's cost. |
| GET | `/api/orchestrators/{id}/messages` | Chat log, `?after=<id>` for the tail. |
| GET | `/api/orchestrators/{id}/members` | Members, worst status first. |
| POST | `/api/orchestrators/{id}/members` | Add up to 100 in one call. |
| DELETE | `/api/orchestrators/{id}/members/{chat_id}` | Stop watching one. |
| GET | `/api/orchestrators/{id}/stream` | SSE: the run. |
| GET | `/api/orchestrators/{id}/tasks/{task_id}/stream` | SSE: one task. |

Every handler resolves the orchestrator with `orchestrator_get(id, owner)`
first and 404s if that returns nothing, so ownership is checked before
anything else happens.

## 14. The streams

**Run stream** (`/stream`), polling the database every 0.5 s and emitting only
on change:

| Frame | When |
|---|---|
| `start` | On connect. |
| `status` | Status changed but progress did not — this is what carries `planning → running` and `planning → done`, which the client would otherwise wait for a progress bump that never comes. |
| `progress` | Progress changed. Carries the full task list. |
| `messages` | New chat messages since the last id sent. |
| `events` | The engine's last five `ProgressEvent`s, when they differ from the last batch. Only available while the engine is live in this process. |
| `done` | Status reached `done` or `error`; the stream then closes. |
| `error` | The orchestrator was deleted, or the generator failed. |

**Task stream** (`/tasks/{task_id}/stream`) polls one task row every second and
emits `status`, `result` (first 5000 characters) and `done`.

Note both streams read the **database**, not the in-memory engine — which is
what makes them keep working for an orchestrator whose engine has been evicted
or lost to a restart. The `events` frame is the one exception, and degrades to
silence rather than to an error.

## 15. The live-engine registry

`routes/orchestrators.py` keeps an `OrderedDict` of live engines, capped at
**200** and evicted least-recently-used. Each entry holds a task graph, a
progress tracker and a set of background task references, so an unbounded dict
is unbounded memory for an account that creates orchestrators and never deletes
them.

An evicted orchestrator keeps all of its state — the database is the record.
What it loses is the running scheduler, exactly as if the process had just
restarted. **There is no rehydration:** an orchestrator whose engine is gone
does not resume on its own. `PATCH` with `status: "running"` is what starts a
scheduler loop again, and it starts from the persisted graph only insofar as
the engine object still holds one, so in practice a run interrupted by a
restart needs a fresh goal.

Background tasks are started through `engine.spawn`, which keeps a strong
reference and attaches a failure callback. The event loop holds only weak
references, so a task nobody holds can be collected mid-run — the work stops
with nothing raised and nothing logged.

## 16. The page

`web/orchestrator.html`, five panels, loaded in an iframe by the console's
orchestrator pane (`web/assets/orchestrator.js::openSupervisorPane`) so the
sidebar stays in view and leaving costs no reload.

* **Left — orchestrator list** (`list.js`). Sorted newest-first or by last
  activity; the choice is remembered in `localStorage`. Inline rename on each
  row (a rename in progress suppresses the 30 s refresh, or a poll would wipe
  a half-typed name). A status badge flashes when a status changes, and shows
  ⚠ with the reason as a tooltip when the row is `degraded`.
* **Left, lower — task rail and task tree** (`rail.js`, `tasks.js`). The rail
  is a dependency-ordered overview: `computeLayers` topologically layers tasks
  by `depends_on`, with anything unplaceable after N passes dumped into a final
  layer so a cycle cannot hang the UI. The tree below it is one row per task
  with status dot, badge, model, dependency list, progress bar and an expander.
* **Left, lowest — members** (`members.js`). See §17.
* **Centre — chat** (`list.js`, `banners.js`). The goal banner (which shrinks
  to a slim strip once messages push it up, unless the user has expanded it by
  hand), the message list, the completion banner, the composer, the overall
  progress bar and the run-cost line.
* **Right — task detail** (`tasks.js`): id, status, model, description,
  dependencies, full result, timeline.
* **Bottom — event log** (`stream.js`): every SSE event as a timestamped line.

Behaviour worth knowing:

* **Progress for a running task is an estimate, and says so.** A task's real
  `progress_pct` is 0 until the engine marks it done at 100 — a single
  `claude -p` call reports nothing until it finishes. So a running task shows
  elapsed time divided by how long *this run's own* finished tasks took,
  clamped to 99, labelled "(est.)". With no finished tasks to estimate from it
  falls back to the honest elapsed-time label rather than inventing a
  percentage. A 1-second ticker re-renders while anything is running and stops
  itself when nothing is.
* **Polling.** A 30-second timer refreshes the orchestrator list always, and
  the tasks and members when one is open. The list refresh is deliberately
  outside that guard: with nothing selected — the state the page opens in — it
  used to never refresh at all.
* **Smart scroll.** The chat and event log follow new content only while you
  are within 20 px of the bottom; otherwise a button offers to take you back,
  and an unread badge counts what arrived while you were away.
* **The gate marker** is the one topbar element every maximised-panel layout
  leaves visible, so it is where "something needs you" is shown. Exactly two
  things gate a run on a person: the engine is paused, or a watched member's
  own status is `waiting`. The task DAG never does — subtasks are one-shot
  non-interactive turns and cannot wait on anyone.
* **Keyboard.** `1`–`4` focus the task tree, chat, event log and composer, but
  only when you are not typing — without that guard every "1" typed into the
  composer threw focus at the task tree. Ctrl/Alt+Enter sends, Ctrl+N creates,
  Escape dismisses whichever banner is up.
* **Panels** resize by drag and maximise by button, with sizes kept in
  `state.panelSizes` (`layout.js`).

## 17. Members are not tasks

A **task** is work the orchestrator invented and runs headless. A **member** is
work that already existed and belongs to someone: a conversation, or a terminal
agent adopted into one on the way in (`_resolve_member` reuses the sidebar's own
adoption path, so adding the same agent twice is a no-op).

Members are watched, not driven. Their status comes from `classify_chat` — the
same function the sidebar uses, with the same CLI lookups, so a member's state
and the same entry in the sidebar cannot disagree. They are listed worst-first:
a failure outranks everything, then waiting, working, updated, idle.

Nothing here dispatches on its own, and that is the rule rather than an
omission: driving a live agent types into its terminal, so any such action has
to be a person clicking. The panel as it stands offers exactly two: add
members, and stop watching one. The orchestrator never sends a member a prompt.

Removing a member removes the membership row and nothing else — an orchestrator
is a view over work, not its owner. A member whose conversation has been
deleted is omitted from the list rather than rendered as a row that 404s, but
the membership row itself is left alone, because a GET that quietly deletes
rows is a surprise nobody asked for.

## 18. Limits

| Limit | Value | Where |
|---|---|---|
| Prompt length | `config.PROMPT_MAX_CHARS`, 8000 | `/send` |
| Orchestrators per owner | 50 | `_MAX_SUPERVISORS_PER_OWNER` |
| Live engines in the process | 200, LRU-evicted | `_MAX_ORCHESTRATOR_ENGINES` |
| `config` blob | 64 KB serialised | `_SUPERVISOR_CONFIG_MAX` |
| Members added per request | 100 | `/members` POST |
| Stored plan text | 20 000 chars | `_run_planner_turn` |
| Task result in a chat message | 3000 chars | `_execute_task` |
| Dependency context per dependency | 500 chars | `_build_dep_context` |
| Result in a task SSE frame | 5000 chars | task stream |

## 19. Failure modes, and how each one surfaces

| What happens | What you see |
|---|---|
| Planner returns nothing | Status `error`. No message body — the turn produced no text to quote. |
| Plan cannot be parsed | Status `error`, plus a `system` message quoting the first 1500 characters of what the planner said. Never reported as a completed run. |
| A task's turn raises | That task `failed`; its dependants become `blocked`; the run ends `error`. Usage is still recorded. |
| A task row cannot be written | The task still runs. The orchestrator is flagged `degraded`, with the kind and the exception in `degraded_reason`, shown as ⚠ on the list row. |
| Usage cannot be recorded | Flagged `degraded` (kind `usage`). The turn is unaffected. |
| The engine is evicted or the process restarts | Status stays whatever was last persisted. The stream keeps working (it reads the database) but nothing advances, because there is no scheduler. |
| SSE drops | The client says "Stream interrupted; reconnecting…" and lets `EventSource` reconnect on its own. It deliberately does **not** call `close()` — doing so is exactly what prevents the reconnection it announces. |

## 20. Known gaps

Recorded so nobody re-derives them from the code:

1. **No parallelism.** Independent tasks in the same layer run one after
   another (§6).
2. **The model router is dead code** (§9).
3. **No resume across a restart** (§15).
4. **`orchestrator_progress` (table and function) is unused** (§11).
5. **Dependency context is 500 characters per dependency** (§7) — a task
   depending on a long result sees only the opening of it.
6. **Progress percentage for a running task is an estimate**, because the CLI
   emits nothing mid-turn (§16).
7. **A pause cannot interrupt an in-flight turn** (§10).
8. **`completed_at` is never written** (§11).
