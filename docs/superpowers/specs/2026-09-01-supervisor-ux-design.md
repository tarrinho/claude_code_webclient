# Supervisor UX — live task output, and a way back from a failure

**Date:** 2026-09-01
**Status:** drafted, pending spec review
**Author:** cweb3 (Claude Opus 5), with Pedro Tarrinho

## Problem

Four UX complaints were raised against the supervisor page: task progress is
invisible, the chat looks empty, task results are hard to find, and there is no
way back from an error. Investigation narrowed that to two defects this design
addresses, and two that are already being fixed by someone else — see *What is
deliberately out of scope*.

### A run is silent from start to finish

Both turns the supervisor makes go through the blocking entry point:

```python
chunks, _sid = await runner.run_turn(...)      # supervisor.py:589 (planner)
chunks, _sid = await runner.run_turn(...)      # supervisor.py:802 (each task)
result = "".join(chunks) if chunks else ""
```

`run_turn` returns only when the turn is over. `runner.stream_turn` exists
beside it and yields events as they arrive, but the supervisor never calls it.
So a task's progress goes from 0% to 100% with nothing in between: no output, no
tool calls, no sign of life. On the observed run the planner took 19 seconds and
the task another 11, and for all 30 the page showed a spinner and an event log
holding one line. The user cannot tell a slow task from a hung one, which is the
question they are actually asking when they watch that panel.

`ProgressTracker` already records `task_start` and `task_done`, and the SSE
stream already delivers them. The gap is not the transport — it is that there is
nothing to send between the two ends of a task.

### A failed run is a dead end

There is no retry anywhere in the supervisor: no endpoint, no button, no engine
method. Grep for it finds only rate-limit `Retry-After` headers and the
conversation-level retry in `app.py`. When a run fails the supervisor sits at
`status="error"` and the only way forward is to retype the prompt — and, because
`_planner_chat_id` is regenerated per run, there is no way to resume the old
one even by hand.

The reason now reaches the chat (`Run failed: {exc}`, added by a peer this
morning), so the user can read *what* broke. They still cannot act on it.

## What is deliberately out of scope

`supervisor.py` and `web/supervisor.js` are **dirty with a live peer's
uncommitted work** as of 09:10 today (+59 and +147 lines). That work already
covers two of the four complaints:

| Complaint | Status |
|---|---|
| Chat looks empty | Being fixed by a peer — the plan and each task result are now appended as `supervisor_messages` with `{"kind": "plan"}` / `{"kind": "task_result"}` |
| Results hard to find | Partly the same change — the result is in the chat (3000 chars) and in full in `supervisor_tasks.result`, which the detail panel renders |
| Smart scroll on chat and event log | Landed in `aa3489d`, refined further in the working tree |

**This design does not touch those regions.** The empty chat I first reported
was an artefact of the running server predating the peer's edit by ten hours,
not a defect in the code on disk. Nothing here should be implemented as a
"fix" for it.

## Design

### 1. `stream_turn` gains an `owner` parameter — do this first

This is the trap. The two entry points do not have the same signature:

```python
async def run_turn(prompt, session_id, work_dir, chat_id, model=None, owner=None)
async def stream_turn(prompt, session_id, work_dir, chat_id, model=None)
#                                                                  ^ no owner
```

The supervisor must pass `owner`. Its chat ids (`subtask_<id>`,
`supervisor_<uuid>`) are labels, not rows in `chats`, and backend resolution is
keyed on a `chats` row — so without an owner the child process gets no base URL
and no API key, and every turn dies on *"Not logged in — Please run /login"*.
That failure has already been diagnosed and fixed once; the fix is the `owner`
argument at `supervisor.py:585`, and the comment there says so.

Swapping `run_turn` for `stream_turn` without adding `owner` reintroduces it
exactly. So: add `owner: str | None = None` to `stream_turn` and thread it to
`_stream_proxy` / `_stream_direct` the way `run_turn` threads it to
`_proxy_turn` / `_execute_direct`. Land and verify this on its own, before any
supervisor change, so a regression here cannot be confused with a bug in the
streaming work.

### 2. `_execute_task` consumes the stream

`_execute_task` keeps its contract — it still returns the full result string, so
`run_schedule_loop` is untouched:

```python
parts: list[str] = []
async for event in runner.stream_turn(full_prompt, f"supervisor_{task_chat_id}",
                                      work_dir, task_chat_id, model, self.owner_id):
    kind = event.get("type")
    if kind == "text":
        parts.append(event["content"])
        await self._emit_task_output(task_id, event["content"])
    elif kind == "error":
        raise runner.TurnError(event.get("error") or "stream failed", fatal=False)
result = "".join(parts)
```

The event is `{"type": "text", "content": ...}` — the shape the proxy protocol
documents at the top of `runner.py`. The stream also yields `status`,
`session_id`, `model`, `usage` and `done`, and it **reports a failure as an
`error` event rather than raising**. `run_turn` converts that into a
`TurnError` for its callers; a streaming consumer that ignores it would treat a
failed task as a successful empty one — which is the "false success" failure
mode `_run_planner_turn` already has a comment about. Hence the explicit
`elif`.

`_emit_task_output` records a `ProgressEvent(event_type="task_output")` on the
existing `ProgressTracker`, so it reaches the client over the SSE stream that is
already open and already polled. No new endpoint, no new socket.

**Coalescing, not per-token.** A token-per-event stream would mean hundreds of
SSE frames and hundreds of DB writes per task. Output is buffered and flushed on
whichever comes first: 400 characters, a newline, or 500 ms. That is frequent
enough to read as live and infrequent enough that a long task costs tens of
writes rather than thousands.

**Noted while reading this path, not fixed here:** usage is recorded by the
*caller*, via `runner.take_last_usage(chat_id)` — `app.py:1210` does it for the
chat path. The supervisor never calls it on either path, so supervisor token
spend is currently attributed to nothing. Switching to `stream_turn` neither
causes nor worsens that, and fixing it belongs with the usage work rather than
here; it is written down so the next reader does not mistake it for fallout
from this change.

**Nothing incremental is written to `supervisor_messages`.** The peer's
`{"kind": "task_result"}` message stays the single durable record of what a task
produced; live output is transport-only and is reconstructed from
`supervisor_tasks.result` on reload. Writing both would put the same text in the
chat twice, once in fragments.

### 3. The event log shows output, the detail panel shows all of it

Two small render changes, both additive:

- **Event log** gains a `task_output` case that appends the flushed chunk to the
  last line for that task rather than starting a new one, so a task's output
  reads as one growing line instead of forty stacked ones.
- **Detail panel** already renders `task.result`. It gains a live section fed by
  the same `task_output` events while the task is `running`, replaced by
  `task.result` once it is `done` — so the panel is never empty for a task that
  is visibly working.

Both are in `web/supervisor.js`, which is dirty. **Coordinate before editing**
(see *Coordination*).

### 4. Retry

`POST /api/supervisors/{id}/retry`, owner-scoped, mirroring `/send`:

- Refuses unless `status` is `error` — retrying a `running` supervisor would
  start a second engine against one graph. `409` with a reason, not a silent
  no-op.
- Re-sends the **last `user` message** for that supervisor, read back from
  `supervisor_messages`. No new prompt is invented and none is required from the
  UI.
- Clears the previous run's tasks for that supervisor before replanning.
  Retrying replans from scratch rather than resuming, because the planner is
  what failed in most observed cases and a resumed graph would inherit its
  mistake. Stated as a decision, not a limitation: resume is a larger design.
- Appends a `{"kind": "retry"}` system message, so the chat shows why a second
  identical prompt appears — otherwise the transcript reads as the user having
  sent it twice.

In the UI: an error-state supervisor row and the failure message both get a
**Retry** button. Not automatic. A run that failed for a reason the retry cannot
change (no credentials, no binary — both of which happened yesterday) would
otherwise loop, and the human deciding to press it is the check.

## Files

| File | Change |
|---|---|
| `runner.py` | `stream_turn` accepts and threads `owner` — step 1, landed alone |
| `supervisor.py` | `_execute_task` streams; `_emit_task_output`; `retry()` |
| `app.py` | `POST /api/supervisors/{id}/retry` |
| `web/supervisor.js` | `task_output` in the event log and detail panel; Retry button |
| `tests/test_qa_supervisor_streaming.py` | new |
| `tests/test_qa_supervisor_retry.py` | new |

## Verification

1. **The regression that proves step 1.** A `stream_turn` call with an owner and
   a non-conversation `chat_id` resolves a backend. Without the `owner`
   parameter this test cannot even be written — which is the point of landing it
   first.
2. `python3 -m pytest tests/ -q` green and `ruff check .` clean.
3. **Mutation-check both new suites before claiming them,** and verify each
   mutation actually changed the file before drawing a conclusion. Three
   mutations in earlier work reported "passed" while being mechanically void.
   Mutations to make: drop the coalescing flush so output never emits; make
   `retry` accept a `running` supervisor; return the wrong `user` message.
4. **Live, over HTTPS on the tailnet address:** send a task that prints
   progressively and confirm text appears in the event log and detail panel
   *before* the task finishes. This is the whole feature; a green suite does not
   demonstrate it.
5. Kill the proxy mid-task and confirm the failure reaches the chat with its
   reason, the supervisor lands on `error`, and Retry appears and works.
6. Retry a supervisor whose failure cannot be retried away (stop the proxy) and
   confirm it fails again cleanly rather than looping.
7. Reload mid-task: the detail panel repopulates from `supervisor_tasks.result`
   for finished tasks and resumes live output for the running one.
8. Headless pass with the Selenium harness: both themes, 390×680, zero console
   errors.

## Coordination — before touching anything

`supervisor.py` and `web/supervisor.js` are dirty with a peer's uncommitted work
right now, and `web/supervisor.js` is where two of these four changes land.
Sessions cweb1–cweb4 share this working tree, and two whole-file commit
accidents have already happened here between careful sessions.

So: `ListAgents`, then message each live peer naming the exact regions —
`_execute_task`'s `run_turn` call, the event-log `switch` in `handleSSEEvents`,
`renderTaskDetail`, and the supervisor-row template — and wait for
acknowledgement. `runner.py` is clean, which is a second reason step 1 goes
first: it is the one part that can start immediately.

Never `git stash` in this tree. Prefer `git commit -- <paths>`; note that a
pathspec protects against a contaminated index and a bare commit against a
contaminated working tree, and **neither is safe when both are contaminated** —
check `git diff --cached --stat` against the size of the change you meant to
make before every commit.
