# Orchestrator observability: degraded-state tracking and the human-gate rail

**Date:** 2026-09-04
**Status:** draft for review, not approved
**Author:** claude session (this checkout)
**Builds on:** `2026-08-30-orchestrator-orchestration-design.md` (SupervisorEngine,
TaskGraph), `2026-08-30-orchestrator-members-design.md` (members panel,
classify_chat sharing)
**Related, not superseded:** `2026-09-01-orchestrator-next-design.md` addresses a
different defect family — the classifier reporting a status it never actually
observed. This spec addresses a defect family that classifier work does not
touch: writes that succeed in memory but fail to persist, with nothing
observable left behind except a log line.
**Prior art:** the `spark-agentic-engineering-framework` survey (external
review, 2026-09-04) named two patterns applied here — a failure-policy table
that enumerates known failure modes rather than trusting ad hoc `except`
blocks, and a rail/gate visualization that marks exactly what a system is
blocked on rather than a flat status list.

---

## Problem

Two independent gaps, found by reading `orchestrator.py` and its usage-recording
counterpart in `routes/chats.py` end to end:

1. **Silent divergence.** Nine call sites in `orchestrator.py` (plus three more
   in `routes/chats.py` / `routes/db_usage.py`) catch every exception from a
   database write and do nothing but log it. Each catch is individually
   correct — CLAUDE.md rule 5 exists precisely because *not* catching these
   used to crash live turns — but the aggregate effect is that a orchestrator's
   or a conversation's persisted state can permanently disagree with its true
   in-memory state, and the only evidence is a log line nobody is watching in
   real time. This is not hypothetical: CLAUDE.md §5 already documents one
   instance (`orchestrator.py` never recording usage at all, for a long time)
   and the file's own comments document a second (`_set_status` writing to a
   graph node, `"orchestrator"`, that no code ever creates, so the persisted
   status could get stuck on `"planning"` forever).
2. **No visible human-gate.** The orchestrator pane shows a flat task list and a
   flat member list, each carrying its own status text. Nothing answers the
   single question the pane exists to answer — *is anything actually waiting
   on me, right now* — without reading every row. Four view states
   (`max-left`, `max-right`, `max-center`, `max-bottom`) each hide most of the
   panels, so a signal placed inside any one panel is invisible half the time.

## Non-goals

- No write-ahead log or spool file for usage events. That is a larger,
  separate durability project; this spec only makes existing failures
  visible, it does not make the underlying writes more reliable.
- No per-task `degraded` column. The orchestrator row is the only granularity
  actually rendered (pane header, sidebar badge, rail banner); a task-level
  flag would need its own UI surface nothing currently provides.
- No admin/cross-owner view of degraded rows. Stays owner-scoped like every
  other query in this codebase.
- No change to `classify_chat` or the waiting/working/updated classification
  logic. That logic is already correct for what it observes; this spec adds a
  new, narrower signal (engine-level pause, write failure) alongside it.

---

## Part A — Degraded-state tracking

### Data model

Two additive migrations, following the exact idiom `_ensure_chat_columns` and
`_ensure_supervisor_columns` already use in `db.py` (PRAGMA `table_info`, then
`ALTER TABLE` for whatever is missing, run unconditionally at every startup):

```sql
-- in _ensure_chat_columns()
ALTER TABLE chats       ADD COLUMN degraded        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chats       ADD COLUMN degraded_reason  TEXT;
ALTER TABLE chats       ADD COLUMN degraded_at      TEXT;

-- in _ensure_supervisor_columns()
ALTER TABLE supervisors ADD COLUMN degraded         INTEGER NOT NULL DEFAULT 0;
ALTER TABLE supervisors ADD COLUMN degraded_reason  TEXT;
```

`chats` gets a timestamp column and `supervisors` does not, because
`supervisors` already has `updated_at` bumped by every `supervisor_update`
call (including the one that will set `degraded`), while `chats` rows are
updated by many code paths that must not be made to also touch `updated_at`
as a side effect of marking degradation — a separate column avoids that
coupling.

`_CHAT_COLUMNS` in `routes/db_chats.py` gains the three names, so they ride
along on every existing `chat_list` / `chat_get` row exactly the way `model`
and `ai_machine_id` already do — no new endpoint. `degraded` is **not** added
to `_ALLOWED_CHAT_FIELDS`: it must never be settable through
`PATCH /api/chats/{id}`, only through the internal helper below. The
equivalent for supervisors: `supervisor_get` / `supervisor_list` in
`routes/db_supervisors.py` already `SELECT` explicit column lists, which gain
`degraded, degraded_reason`.

### Helpers

One mark/clear pair per module, matching this codebase's existing
one-function-per-concern style rather than a generic cross-table abstraction:

```python
# routes/db_chats.py
async def chat_mark_degraded(chat_id: str, reason: str) -> None: ...
async def chat_clear_degraded(chat_id: str) -> None: ...

# routes/db_supervisors.py
async def supervisor_mark_degraded(supervisor_id: str, reason: str) -> None: ...
async def supervisor_clear_degraded(supervisor_id: str) -> None: ...
```

Both `mark_*` functions themselves catch and log rather than raise — marking
degradation must never become a second thing that fails a turn, the same rule
`db.usage_record` already states for itself. `reason` is free text, timestamped
by the caller where useful (e.g. `f"usage record failed at {db._now()}: {exc}"`);
last-write-wins, no history table. This is a signal to go check logs, not a
log replacement.

### Failure-policy table

Every site identified by reading `orchestrator.py` and the usage path in full,
with the policy applied at each:

| Site | Failure | Frequency | Blast radius | Policy |
|---|---|---|---|---|
| `routes/chats.py:_record_turn_usage` — no frame reached the handler | CLI result frame carried none, or `claude_proxy` is stale | rare | cost/usage for one turn invisible; conversation content unaffected | `chat_mark_degraded(chat_id, "usage_missing")`; clear on next successful record |
| same — frame has no models | usage frame arrived empty | rare | same as above | `chat_mark_degraded(chat_id, "usage_frame_has_no_models")`; clear on next success |
| same — `db.usage_record` returns `None` | DB write failed | very rare (DB error) | one turn's cost row lost | check the return value (currently discarded) and `chat_mark_degraded` on `None`; clear on next success |
| `orchestrator.py:_record_usage` | `usage_record` raised or returned `None` | very rare | orchestrator spend under-reported | `supervisor_mark_degraded(reason="usage")`; clear on next success |
| `orchestrator.py:_persist_progress` | `db.supervisor_update` raised | rare | progress bar stale, tasks still correct | `supervisor_mark_degraded(reason="progress")`; clear on next success |
| `orchestrator.py:_set_status` | `db.supervisor_update` raised | rare, **highest severity**: this is the exact bug the file's own comments document (status stuck at `"planning"` forever) | UI shows a status that no longer reflects reality, indefinitely | `supervisor_mark_degraded(reason="status")`; clear on next success |
| `orchestrator.py:_materialise_plan` (per-task `supervisor_task_create`) | one task's DB row never created, though it runs in-memory | rare | that task's execution is invisible in the UI while it happens — same shape as bug 61-65 in the members-design doc | `supervisor_mark_degraded(reason=f"task_create:{task.id}")`; not auto-cleared (a missing row from an old plan is not fixed by a later plan's success) — cleared only by the next full successful `_materialise_plan` |
| `orchestrator.py:_execute_task` success path — message append | task succeeded but its result never reached `supervisor_messages` | rare | task shows `done` with no visible output text | `supervisor_mark_degraded(reason=f"task_message:{task_id}")`; clear on next successful append |
| `orchestrator.py:_execute_task` success path — task row update to `"done"` | in-memory graph says done, DB row does not | rare | task API/rail shows stale status forever for that task | `supervisor_mark_degraded(reason=f"task_status_done:{task_id}")`; clear on next success |
| `orchestrator.py:_execute_task` failure path — task row update to `"failed"` | same as above, failure branch | rare | same as above, failure branch | `supervisor_mark_degraded(reason=f"task_status_failed:{task_id}")`; clear on next success |
| `orchestrator.py:run_schedule_loop` background crash | unhandled exception in the scheduler | rare | whole run stuck, but already calls `_set_status("error")` right after — that call can itself silently fail (see the `_set_status` row above), which is the one case where two failures compound | no new handling beyond what `_set_status`'s own policy already covers; noted here so the compounding case is documented rather than rediscovered |

The planner-turn failure handler (`_run_planner_turn`'s own `except`, which
tries to record the failure message) is deliberately **not** given a
degraded policy: it is already a best-effort report of a different failure,
and marking degradation on a failure to report a failure adds a second
signal for one event without adding information.

### Surfacing

- `GET /api/chats` / `GET /api/chats/{id}`: `degraded` rides along
  automatically once it is a real column.
- Sidebar (`chat-list.js`): a new trailing badge, styled like the existing
  `.chat-queued` badge (independent of the existing leading-dot precedence
  chain — a chat can be `running` *and* carry a stale degraded flag from an
  earlier turn, so this is an orthogonal signal, not a replacement tier).
  Glyph: `⚠`. Title: the `degraded_reason` text, verbatim.
- `GET /api/supervisors` / `GET /api/supervisors/{id}`: same, `degraded` +
  `degraded_reason` ride along.
- Orchestrator sidebar list (`orchestrator/list.js`, `renderSupervisorList`): reuse
  the existing `.status-badge` element's title attribute to append the reason
  when `degraded` is true, rather than adding a second badge element next to
  a badge vocabulary that already has eight members.

---

## Part B — The rail and the human-gate marker

### What "blocked on a human" actually means here

Read end to end, `SupervisorEngine`'s subtasks never wait on a human: each
runs one non-interactive `-p` turn (CLAUDE.md §0) with
`--dangerously-skip-permissions`, so a task's own status vocabulary
(`pending`/`ready`/`running`/`done`/`failed`/`blocked`) never includes
"waiting for a person" — its `blocked` means a dependency failed, not that
anyone is being asked anything. The only two things that genuinely gate a
orchestrator on a human are:

1. The engine paused by a person (`status == "paused"`).
2. A watched **member** — a real, possibly-interactive conversation or
   session — whose own `classify_chat` (or `_classify_cli_session`) status is
   `"waiting"`.

The rail therefore does two distinct jobs rather than one: show the task
DAG's progress (which never blocks on a human, but is genuinely useful to see
laid out by dependency rather than as a flat list), and mark, separately and
unambiguously, whichever of the two real gates currently applies.

### The task rail

New `web/assets/orchestrator/rail.js`:

```js
export function computeLayers(tasks) { ... }   // pure, unit-testable
export function renderRail(tasks) { ... }      // DOM only
```

`computeLayers`: topological sort of `tasks` by `depends_on` into an array of
layers (array of arrays of task ids). A task with no unresolved dependency
goes in the earliest layer its dependencies allow. A dependency cycle (should
never happen — `PlanParser` excludes self-references — but must not hang the
UI if one somehow reaches here) dumps every task still unplaced after `N`
passes (`N` = task count) into one final "unresolved" layer rather than
looping.

`renderRail`: inserts a new `#task-rail` div between the "Task Tree"
`.panel-header` and the existing `#task-tree` in `orchestrator.html`. One small
square per task per layer, colored with the `.task-status-dot` classes that
already exist (`pending`/`ready`/`running`/`done`/`failed`/`blocked` —
reusing this exact palette, not inventing a second one). Layers connected by
plain CSS borders (a `::before`/`::after` line per gap), not SVG — this
matches the plain-DOM style the rest of `orchestrator/*.js` already uses and a
straight layer-to-layer connector does not need a drawing primitive.

Called from the same two places `renderTaskTree()` already is called from
(`tasks.js:loadTasks`, `stream.js:handleProgress`) — no new polling, no new
endpoint. The existing flat `#task-tree` list stays exactly as it is (click to
expand, per-task detail in `#panel-right`); the rail is additive, an overview
strip above it.

### The human-gate marker

New `updateGateMarker()` in `banners.js` (same file already owning the
topbar's unread-count badge, so the topbar's two indicators are defined
together):

```js
export function updateGateMarker(supervisorStatus, members) {
  const waitingMember = (members || []).find(m => m.status === "waiting");
  const gate = supervisorStatus === "paused"
    ? { kind: "paused" }
    : waitingMember
      ? { kind: "member", member: waitingMember }
      : null;
  // render or hide el.topbarGate accordingly
}
```

Placed in `#topbar` — the one element every `body.max-*` state leaves visible
— as a new element beside the existing `#topbar-badge`, reusing that badge's
CSS shape (`.notif-badge`) with a new modifier `.gate-badge` for its color (the
same red already used for `#pauseResumeBtn`, so "something needs you" reads
consistently across the pane). Title text names the specific gate: `"Paused"`
or the waiting member's title.

Called after `loadMembers()` resolves and after every `handleStatusUpdate` /
`handleProgress` SSE event — the two places relevant state already changes;
no new polling interval.

Click handler: `document.body.classList.remove('max-left', 'max-right',
'max-center', 'max-bottom')` (restoring all four panels regardless of which
one is currently maximized), then either focuses `#pauseResumeBtn` (paused
case) or scrolls `#membersPanel` to the specific waiting member's row and
briefly flashes it (reusing the `.status-badge.flash` animation already
defined for exactly this purpose).

---

## Testing

- `computeLayers` and the `updateGateMarker` gate-selection predicate are pure
  functions — direct unit tests with plain arrays/objects, no DOM, matching
  how `classify_chat` itself is tested (`tests/test_qa_*` style).
- Each failure-policy row gets a test that forces the underlying `db.*` call
  to raise (monkeypatching, matching the existing pattern used for
  `usage_record_failed`-style assertions), then asserts the row's `degraded`
  / `degraded_reason` afterward, then asserts a following successful call
  clears it.
- `tests/test_frontend.py`-style structural/grep assertions for: the new
  file's exports, the `#task-rail` / gate-badge markup existing in
  `orchestrator.html`, and that `degraded` is present in `_CHAT_COLUMNS` but
  absent from `_ALLOWED_CHAT_FIELDS`.

## Migration / rollout

Both `ALTER TABLE` sets are additive and idempotent, following the exact
pattern already proven safe for three prior rounds of orchestrator schema drift
(`config`/`plan`/`progress_pct`/`completed_at`, then `priority`, then the
`supervisor_tasks` description/model/result group). No backfill needed —
`degraded` defaults to `0` and every existing row is, correctly, not degraded.
