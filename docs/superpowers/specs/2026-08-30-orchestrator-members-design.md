# Orchestrator members — supervising existing chats and agents

**Date:** 2026-08-30
**Status:** approved in outline, pending spec review
**Author:** cweb4 (Claude Opus 5), with Pedro Tarrinho

## Problem

A orchestrator cannot be pointed at work that already exists. It decomposes a
plan into a `TaskGraph` and runs each task as a throwaway headless worker:

```python
task_chat_id = f"subtask_{task_id}"
chunks, _sid = await runner.run_turn(prompt, f"supervisor_{task_chat_id}",
                                     work_dir, task_chat_id, ...)
```

Those `subtask_*` and `supervisor_*` ids are labels, not rows. Neither
`supervisors` nor `supervisor_tasks` has a `chat_id` or `session_id` column, so
there is no link anywhere between a orchestrator and a conversation or a running
agent. The request is to be able to add them.

## What was decided

Four decisions, taken in order, each narrowing the design:

1. **Watch *and* drive.** A member's status is visible and it can be sent
   prompts — not a read-only lens.
2. **A human clicks send.** The orchestrator never dispatches on its own. See
   *Why the human stays in the loop* below; this is a safety property, not a
   convenience.
3. **Two entry points.** A bulk picker inside the orchestrator, and
   `⋯ → Add to orchestrator ▸` on a conversation row.
4. **Membership is decoupled from the task graph.** Adding members builds a
   watch-and-drive surface. `PlanParser`, `TaskGraph`, `ModelRouter` and
   `SupervisorEngine` are not touched, and the headless task path keeps working
   exactly as it does now.

A fifth decision — **adopt a live agent into a conversation when it is added** —
collapses the design further, and is what makes this small. See *Members are
chats* below.

## Why the human stays in the loop

Driving a chat is ordinary: it goes through the normal turn path. Driving a
live CLI agent is not. It goes through `prompts.deliver_request`, which types
text into that agent's terminal window.

That is the mechanism `docs/threat-model.md` records as F-01 and F-02. F-01 is
fixed. **F-02 is narrowed, not closed**: the session-to-pid mapping lives in a
file that any process running as this user can write, which includes every
agent this console spawns with `--dangerously-skip-permissions`. The threat
model states plainly that the real fix is a privilege boundary, not more
validation.

Today the check on that mechanism is a human deciding to press send. A
orchestrator scheduler dispatching unattended would remove that check, at machine
speed, on prompts chosen by a model's plan. So the orchestrator prepares a prompt
and shows it; nothing leaves until the operator clicks. `locate()` reports
`shares_server_window` rather than refusing, deliberately, so the caller can
decide — and here the caller is a person looking at the screen.

This is a constraint on the design, not a phase-one limitation to be removed
later. Changing it is a new decision requiring its own threat-model pass.

## Members are chats

A live agent is **adopted on add**: the member is stored as the `chats.id` that
`POST /api/sessions/{session_id}/resume` returns.

This is the single largest simplification available. Without it there are two
member kinds with two status sources, two drive paths, and a stop that works
for one and not the other. With it there is one kind, and every existing chat
affordance — prompt, stop, read, transcript sync, per-conversation backend and
model — applies to a member for free.

Adoption is already idempotent. `handle_sessions_resume` searches the owner's
chats for one whose `session_id` matches and returns it rather than creating a
second (the fix recorded as registry entry #6), so adding the same agent twice
links nothing new.

**Accepted side effect:** supervising an agent makes a conversation for it
appear in the sidebar. This was put to the operator explicitly and accepted;
the conversation is also the thing that makes the agent's history readable in
the UI, so it is closer to a feature than a cost.

## Data model

```sql
CREATE TABLE IF NOT EXISTS supervisor_members (
    supervisor_id TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    position      INTEGER,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (supervisor_id, chat_id)
);
```

- **No `kind` column.** Adoption means every member is a chat. A column whose
  only value is `'chat'` is a branch waiting to be written.
- **Composite primary key**, so adding an existing member is a no-op rather
  than a duplicate row. `INSERT ... ON CONFLICT DO NOTHING`.
- **Many-to-many is intended.** One agent can serve two supervisors.
- **`position`** follows the pattern already established by `chats.position`:
  nullable, `position IS NULL` sorts last, so an unordered list falls back to
  recency. Reuse the ordering rule rather than inventing a second one.
- **No foreign key to `chats`.** The existing schema does not use them across
  these tables, and a deleted chat is handled by the join dropping the row —
  see *Error handling*.

## API

Three endpoints, owner-scoped throughout.

### `GET /api/supervisors/{id}/members`

Returns the same entry shape `/api/orchestrator` already produces, filtered to
this orchestrator's members:

```json
{"members": [
  {"kind": "chat", "id": "...", "title": "cweb5", "status": "waiting",
   "reason": "failed", "preview": "API Error: 500 ...", "since": "..."}
]}
```

**This is the crux of the design.** `handle_supervisor` already classifies every
chat and session as working / waiting / updated, already prefers Claude Code's
own `status` field, and already reports `reason: "failed"` from
`transcripts.last_error`. The members endpoint reuses that classifier and
filters by membership. **No new status machinery is written.** Two
classifications of "is this agent stuck" that agreed only by coincidence would
drift, which is the same argument that made `backend_kind` a single function.

### `POST /api/supervisors/{id}/members`

Body: `{"members": [{"kind": "chat", "ref_id": "..."},
{"kind": "session", "ref_id": "..."}]}`

`kind` appears in the *request* because the picker offers both, and is resolved
server-side: a `session` is adopted via the same code path as
`handle_sessions_resume` and stored as the resulting `chat_id`. Bulk, because
the picker adds several at once and a per-item request would half-apply.

### `DELETE /api/supervisors/{id}/members/{chat_id}`

Removes membership only. It never deletes the conversation — a orchestrator is a
view over work, not its owner.

## Security

Every `ref_id` is resolved against the caller before it is stored:
`db.chat_get(chat_id, owner)` for a chat, and the discovered-session check
`handle_sessions_resume` already performs for a session. Without this, a
crafted `ref_id` would pull another account's conversation into a orchestrator
the caller owns and expose its title, preview and status through the members
feed. This is the same owner-scoping rule `chat_routing` applies to a pinned
machine, and the reason registry entry #6 exists.

All three routes are mutating or owner-scoped reads and sit behind the existing
`AuthMiddleware`; the two writes are covered by `CsrfMiddleware._MUTATING`,
which includes POST and DELETE.

## UI

**Picker (bulk).** `[+ Add members]` on the orchestrator page opens a filterable
list in two groups, LIVE AGENTS and CONVERSATIONS, sourced from
`GET /api/sessions` and `GET /api/chats`. Rows already members are shown ticked
and disabled, so the dialog states the current membership rather than silently
re-adding.

**Row menu (one-off).** `⋯ → Add to orchestrator ▸` in `chat-list.js`, listing the
owner's supervisors plus `+ New…`. Follows the existing `makeDisclosure`
submenu pattern and the bare-verb menu labels already used there.

**Members panel.** One row per member: title, status badge, one-line preview,
and the controls. Polls the members endpoint on the interval the orchestrator
page already uses.

**Controls.** `[prompt]` opens a box that posts to
`POST /api/chats/{id}/messages`. `[stop]` posts to `POST /api/chats/{id}/stop`.
Both exist and are unchanged. Because every member is a chat, both work for an
adopted agent too — which is the payoff from adoption.

## Error handling

- **A member's chat is deleted.** The join returns nothing for it. The row is
  skipped in the feed and reaped on next read rather than 500-ing the panel. A
  orchestrator listing a conversation that no longer exists is the failure worth
  preventing.
- **Adoption fails** (no running session, no transcript). `handle_sessions_resume`
  answers 404; the picker reports which entries failed and adds the rest. A
  dead agent in a bulk selection must not lose the other nine.
- **The members feed fails.** The panel renders the last good list with a stale
  marker. The orchestrator page must not go blank because one poll failed —
  matching how the sidebar treats `/api/orchestrator`.
- **Sending to a member fails.** Surfaced inline on that row. Note the existing
  client bug class here: `apiFetch` resolves for 4xx, so the handler must check
  `response.ok` — this is exactly how the conversation-reorder save failed
  silently (registry entry #27).

## Testing

- **Membership:** add, add-twice-is-a-no-op, remove, remove-a-non-member,
  many-to-many across two supervisors, ordering with `position` null and set.
- **Security:** another owner's `chat_id` is refused; another owner's session id
  is refused; a member cannot be added to a orchestrator the caller does not own.
- **Adoption:** adding a session stores a `chat_id`, not a session id; adding
  the same agent twice yields one member and one chat.
- **Status reuse:** a member's status matches what `/api/orchestrator` reports for
  the same entry. This is the test that stops the two surfaces drifting — assert
  agreement, not a hardcoded string.
- **Failure surfacing:** a member whose newest turn is a synthetic API error
  shows `reason: "failed"`, reusing the `transcripts.last_error` fixtures.
- **Deleted chat:** a member row pointing at a deleted chat is skipped, not
  fatal.
- **Mutation testing is required, not optional.** Every test above must be shown
  to fail when the behaviour it covers is removed. Two tests in this repo were
  found vacuous after passing for days — one placed conversations in `C,B,A`,
  which is also their recency order, so it passed whether or not clearing did
  anything.

## Out of scope

- **Stopping a bare CLI agent.** `POST /api/chats/{id}/stop` stops a turn the
  console started. There is no way to stop a terminal agent, and adding one
  means killing a process in someone's terminal. Adopted members get `[stop]`
  for console-started turns only; the button is not offered where it would lie.
- **Assigning tasks to members.** Explicitly decided against: the task graph
  stays decoupled and the headless path is untouched.
- **Autonomous dispatch.** See *Why the human stays in the loop*.

## Open question for review

`position` is specified for manual ordering, matching `chats.position`. It may
be unnecessary — a members list is usually short, and status ordering
(failed first, then waiting, then working) may be more useful than a manual
one. It is cheap to add later and cheap to leave out now. **Recommendation:
drop it from the first version**, sort by status then recency, and add manual
ordering only if the list proves long enough to want it.
