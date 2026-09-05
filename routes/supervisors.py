"""Routes for /api/supervisors: the supervisor list, its tasks, members and streams.

Part of the 0.10.0 routes split. The cluster was measured: every function
reached from a route with this prefix, closed over its private helpers.
Registration stays in app.py -- FastAPI matches routes in the order routers are
included, so that order belongs in one readable place.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import config
import db
import supervisor
import transcripts
import turns
from classification import _classify_cli_session, _cli_maps, classify_chat
from routes.misc import handle_sessions_resume
from shared import _HEX_SESSION_ID_RE

_log = logging.getLogger("wc.app")

router = APIRouter()


async def handle_supervisor(request: Request):
    """GET /api/supervisor -- which agents are waiting on the user.

    "Waiting" is unread: the agent produced output after the last time the user
    looked at it, and is not mid-turn. Defining it that way is what makes the
    badge worth having -- "finished at some point" would mark every completed
    conversation forever and the number would be ignored within a day.

    Mid-turn now comes from the turn registry, which owns the lifecycle, rather
    than from inferring it off the last message. That inference was the best
    signal available before turns.py existed and it is wrong in both directions
    once turns run in the background: a queued prompt looks like an in-flight
    turn, and a turn whose user message has not landed yet looks like nothing
    at all. A conversation that is merely busy must never summon anyone.
    """
    session = request.state.session
    owner = session["user"]
    marks = await db.read_marks_get(owner)
    # Authoritative: a live turn object, and prompts waiting behind one.
    live_ids = turns.running_ids(owner)
    try:
        queued = await db.queue_counts(owner)
    except Exception:
        queued = {}
    waiting: list[dict] = []   # asked for something, or reported a blocker
    working: list[dict] = []   # mid-turn
    updated: list[dict] = []   # said something unread, but nothing is needed

    # ── Web conversations ───────────────────────────────────────────────
    chats = await db.chat_list(owner)
    activity = await db.chat_last_activity(owner)
    # Build a lookup: session_id → CLI status so the web path can defer to
    # the session's own status when a chat is linked to a running CLI session.
    (
        _cli_status_map,
        _cli_dismiss_map,
        _cli_status_updated_map,
        _cli_prompt_map,
    ) = await _cli_maps(marks)
    for chat in chats:
        if chat.get("archived"):
            continue
        last = activity.get(chat["id"])
        if not last:
            continue
        entry = classify_chat(
            chat, last, live_ids, queued, marks,
            _cli_status_map, _cli_dismiss_map, _cli_status_updated_map,
            _cli_prompt_map,
        )
        if entry is None:
            continue
        {"waiting": waiting, "working": working, "updated": updated}[
            entry["status"]
        ].append(entry)

    # ── CLI / terminal sessions ─────────────────────────────────────────
    try:
        cli_sessions = await db.read_claude_sessions()
    except Exception:
        cli_sessions = []
    transcripts_by_id = {t["session_id"]: t for t in await transcripts.list_recent(200)}

    # Build a set of session IDs that have a linked web conversation so the
    # CLI path does not duplicate entries already surfaced in the web path.
    _web_linked_sessions: set[str] = set()
    for chat in chats:
        if chat.get("archived"):
            continue
        sid = chat.get("session_id", "")
        if sid:
            _web_linked_sessions.add(sid)

    for cli in cli_sessions:
        session_id = cli.get("sessionId") or ""
        # A WebConsole shadow record describes a chat that is already listed.
        if not session_id or cli.get("entrypoint") == "webconsole":
            continue
        # Skip sessions already surfaced in the web path.
        if session_id in _web_linked_sessions:
            continue
        meta = transcripts_by_id.get(session_id)
        if not meta:
            continue
        entry = await _classify_cli_session(
            cli, meta, marks.get(("session", session_id), {}),
        )
        if entry is None:
            continue
        {"waiting": waiting, "working": working, "updated": updated}[
            entry["status"]
        ].append(entry)

    waiting.sort(key=lambda e: e["since"])
    updated.sort(key=lambda e: e["since"])
    return JSONResponse(
        {
            "waiting": waiting,
            "working": working,
            "updated": updated,
            # Only `waiting` is badged. The others are context, not a summons.
            "counts": {
                "waiting": len(waiting),
                "working": len(working),
                "updated": len(updated),
            },
        }
    )


# Ordered so the panel reads worst-first. A supervisor is opened to find out
# what needs attention, and a failed agent buried under six healthy ones is the
# one thing the view must not do.
_MEMBER_ORDER: Final[dict[str, int]] = {
    "waiting": 0, "working": 1, "updated": 2, "idle": 3,
}


async def handle_supervisor_members_get(request: Request, supervisor_id: str):
    """GET /api/supervisors/{id}/members -- member status, worst first.

    Status is not defined here. classify_chat is the one function that decides
    whether a conversation is waiting, working or updated, and the sidebar uses
    it too, so a member's state and the same entry in the sidebar cannot
    disagree. Calling handle_supervisor instead would have worked and been
    wrong: it merges CLI sessions, reads marks and builds a paginated response,
    so one member's status would have cost all of that and made its response
    shape an API this endpoint never meant to depend on.

    A member with nothing to say is reported "idle" rather than dropped.
    chat_last_activity only carries conversations that have some, so a quiet
    member would otherwise vanish -- and a supervisor that hides the agents you
    added to it is worse than one that says nothing.
    """
    session = request.state.session
    owner = session["user"]
    if not await db.supervisor_get(supervisor_id, owner):
        raise HTTPException(status_code=404, detail="Supervisor not found")

    rows = await db.supervisor_members_list(supervisor_id)
    if not rows:
        return JSONResponse({"members": [], "count": 0})

    marks = await db.read_marks_get(owner)
    live_ids = turns.running_ids(owner)
    try:
        queued = await db.queue_counts(owner)
    except Exception:
        queued = {}
    activity = await db.chat_last_activity(owner)
    by_id = {c["id"]: c for c in await db.chat_list(owner)}
    # The same CLI lookups the sidebar classifies with. Passing `{}, {}, {}`
    # here meant this panel could not tell that a member's linked terminal was
    # still working, so it announced running work as finished while the sidebar
    # -- same function, better inputs -- kept it quiet. Sharing the classifier
    # was not enough; the data has to be shared too.
    cli_status, cli_dismiss, cli_updated, cli_prompting = await _cli_maps(marks)

    members = []
    for row in rows:
        chat = by_id.get(row["chat_id"])
        last = activity.get(row["chat_id"])
        if chat is None:
            # The conversation is gone -- deleted, or no longer this owner's.
            # Omitted rather than rendered: there is nothing to open, and a row
            # that 404s when clicked is worse than an absent one. The membership
            # row itself is left alone, because reaping it is a write and a GET
            # that quietly deletes rows is a surprise nobody asked for.
            _log.info(
                "supervisor_member_missing supervisor_id=%s chat_id=%s",
                supervisor_id, row["chat_id"],
            )
            continue
        entry = None
        if last:
            entry = classify_chat(
                chat, last, live_ids, queued, marks,
                cli_status, cli_dismiss, cli_updated, cli_prompting,
            )
        since_ts = row["added_at"]
        if last and last.get("created_at"):
            since_ts = max(since_ts, last["created_at"]) if since_ts else last["created_at"]
        members.append(entry or {
            "kind": "chat",
            "id": row["chat_id"],
            "title": row["title"] or "Untitled",
            "preview": "",
            "since": since_ts or row["added_at"],
            # None when the member has never been spoken in. chat_last_activity
            # only carries conversations that have some, so this branch is the
            # normal case for a freshly added agent -- and it used to call .get()
            # on that None, which is the one path that reaches this fallback at
            # all. The panel raised AttributeError and 500d for every supervisor
            # with a quiet member, while the docstring above described the
            # behaviour that was intended and never written.
            "last_seen": last.get("created_at") if last else None,
            "status": "idle",
        })
    members.sort(key=lambda e: (
        # A failure outranks every other reason to be waiting: a supervisor is
        # opened to find what needs attention, and a broken agent buried under
        # six healthy ones is the one thing this view must not do.
        0 if e.get("reason") == "failed" else 1,
        _MEMBER_ORDER.get(e.get("status") or "idle", 9),
        e.get("since") or "",
    ))
    return JSONResponse({"members": members, "count": len(members)})


async def handle_supervisor_members_add(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/members -- add conversations or agents.

    Bulk, because the picker adds several at once and a request per item would
    half-apply: nine agents added and one 404 should leave nine members, not an
    unknown number.

    A session id is adopted into a conversation on the way in, so every member
    is a chat and prompt, stop and transcript sync all work without a second
    code path. Adoption is idempotent -- it returns an existing linked chat
    rather than creating a second -- so adding the same agent twice is a no-op.
    """
    session = request.state.session
    owner = session["user"]
    if not await db.supervisor_get(supervisor_id, owner):
        raise HTTPException(status_code=404, detail="Supervisor not found")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    requested = body.get("members")
    if not isinstance(requested, list) or not requested:
        raise HTTPException(status_code=400, detail="members must be a non-empty list")
    if len(requested) > 100:
        raise HTTPException(status_code=400, detail="Too many members in one request")

    added, skipped, failed = [], [], []
    for item in requested:
        if not isinstance(item, dict):
            failed.append({"ref_id": None, "error": "Each member must be an object"})
            continue
        kind = str(item.get("kind") or "chat")
        ref_id = str(item.get("ref_id") or "").strip()
        if not ref_id:
            failed.append({"ref_id": ref_id, "error": "ref_id is required"})
            continue
        try:
            chat_id = await _resolve_member(kind, ref_id, owner, request)
        except HTTPException as exc:
            # One bad entry must not lose the rest of the selection.
            failed.append({"ref_id": ref_id, "error": str(exc.detail)})
            continue
        if await db.supervisor_member_add(supervisor_id, chat_id):
            added.append(chat_id)
        else:
            skipped.append(chat_id)

    _log.info(
        "supervisor_members_added supervisor=%s added=%d already=%d failed=%d",
        supervisor_id, len(added), len(skipped), len(failed),
    )
    return JSONResponse(
        {"added": added, "already_members": skipped, "failed": failed},
        status_code=200 if added or skipped else 400,
    )


async def _resolve_member(
    kind: str, ref_id: str, owner: str, request: Request
) -> str:
    """Turn a picker selection into a chat id this owner is allowed to add.

    Owner-scoped on every path. Without it a crafted ref_id would pull another
    account's conversation into a supervisor the caller owns, exposing its
    title, preview and status through the members feed -- the same rule
    chat_routing applies to a pinned machine.
    """
    if kind == "chat":
        if not await db.chat_get(ref_id, owner, include_archived=True):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return ref_id
    if kind == "session":
        # Reuses the resume handler so a member is adopted exactly the way the
        # sidebar adopts one: same discovered-session check, same reuse of an
        # existing linked chat, same transcript import.
        adopted = json.loads(bytes((await handle_sessions_resume(request, ref_id)).body))
        chat_id = adopted.get("id")
        if not chat_id:
            raise HTTPException(status_code=404, detail="Agent could not be adopted")
        return str(chat_id)
    raise HTTPException(status_code=400, detail="kind must be 'chat' or 'session'")


async def handle_supervisor_member_remove(
    request: Request, supervisor_id: str, chat_id: str
):
    """DELETE /api/supervisors/{id}/members/{chat_id} -- stop watching it.

    Membership only. The conversation is left exactly as it was: a supervisor
    is a view over work, not its owner, and removing a member must never be a
    way to lose one.
    """
    session = request.state.session
    if not await db.supervisor_get(supervisor_id, session["user"]):
        raise HTTPException(status_code=404, detail="Supervisor not found")
    removed = await db.supervisor_member_remove(supervisor_id, chat_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Not a member")
    _log.info("supervisor_member_removed supervisor=%s chat=%s", supervisor_id, chat_id)
    return JSONResponse({"ok": True, "removed": chat_id})


async def handle_supervisor_read(request: Request):
    """POST /api/supervisor/read -- mark an agent as seen, clearing its badge."""
    session = request.state.session
    data = await request.json()
    if data.get("all"):
        # Clear everything currently listed. Deliberate, so it silences
        # unanswered questions too -- which opening one does not.
        current = json.loads((await handle_supervisor(request)).body)
        cleared = 0
        for entry in [*current.get("waiting", []), *current.get("updated", [])]:
            await db.read_mark_set(
                session["user"], entry["kind"], entry["id"], dismiss=True
            )
            cleared += 1
        _log.info("supervisor cleared by user=%s entries=%d", session["user"], cleared)
        return JSONResponse({"ok": True, "cleared": cleared})
    kind = (data.get("kind") or "").strip()
    ref_id = (data.get("id") or "").strip()
    if kind not in ("chat", "session"):
        raise HTTPException(status_code=400, detail="kind must be chat or session")
    # Same charset the transcript routes enforce: covers both a chat's uuid4
    # hex and a dashed Claude session id, and nothing usable for traversal.
    if not ref_id or not _HEX_SESSION_ID_RE.match(ref_id):
        raise HTTPException(status_code=400, detail="Invalid id")
    read_at = await db.read_mark_set(
        session["user"], kind, ref_id, dismiss=bool(data.get("dismiss"))
    )
    return JSONResponse({"ok": True, "read_at": read_at})


# ── Supervisor orchestration ──────────────────────────────────────────────────────
# Registry of live engine instances, keyed by supervisor_id. Engines are
# started on first use and cleaned up when their supervisor is deleted.
#
# Capped at _MAX_SUPERVISOR_ENGINES: without a bound, an account that only
# ever creates supervisors and never deletes them grows this dict forever --
# each entry holds a TaskGraph, a ProgressTracker and a set of background
# asyncio.Task references, so the memory is not trivial. An OrderedDict lets
# eviction take the least-recently-touched entry rather than an arbitrary one;
# "touched" means created or looked up, via _touch_engine below. A supervisor
# whose engine is evicted still has its state in the database -- eviction only
# drops the live scheduler, the same as if the process had just restarted.
_supervisor_engines: OrderedDict[str, supervisor.SupervisorEngine] = OrderedDict()
_MAX_SUPERVISOR_ENGINES: Final[int] = 200


def _touch_engine(supervisor_id: str) -> None:
    """Mark an engine as recently used, for LRU ordering."""
    if supervisor_id in _supervisor_engines:
        _supervisor_engines.move_to_end(supervisor_id)


def _register_engine(supervisor_id: str, eng: supervisor.SupervisorEngine) -> None:
    """Insert a new engine, evicting the least-recently-touched one if full."""
    _supervisor_engines[supervisor_id] = eng
    _supervisor_engines.move_to_end(supervisor_id)
    while len(_supervisor_engines) > _MAX_SUPERVISOR_ENGINES:
        evicted_id, evicted = _supervisor_engines.popitem(last=False)
        evicted.stop()
        _log.warning(
            "supervisor_engine_evicted supervisor_id=%s (registry at cap %d)",
            evicted_id, _MAX_SUPERVISOR_ENGINES,
        )


# handle_supervisor_crud and handle_supervisor_send_prompt lived here: 169
# lines implementing supervisor CRUD and prompt-send on /api/supervisor
# (singular). Neither was decorated and neither was called -- the live
# routes are /api/supervisors (plural) below, and _api_supervisor_send
# reimplements the send inline. Removed rather than wired up: two
# implementations of the same endpoints, one of them unreachable, is a
# standing invitation to fix the copy that does not run.
async def handle_supervisor_stream(request: Request, supervisor_id: str):
    """GET /api/supervisors/{id}/stream — SSE stream of supervisor progress."""
    session = request.state.session
    owner = session["user"]

    existing = await db.supervisor_get(supervisor_id, owner)
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'supervisor_id': supervisor_id})}\n\n"

            eng = _supervisor_engines.get(supervisor_id)
            last_progress = -1.0
            last_status = ""
            last_events = []
            last_message_id = 0

            while True:
                if await request.is_disconnected():
                    return

                # Read current supervisor state from DB
                current = await db.supervisor_get(supervisor_id, owner)
                if not current:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Supervisor deleted'})}\n\n"
                    return

                progress = float(current.get("progress_pct") or 0)
                status = current.get("status", "")

                # Emit on any change: progress, status, or engine events
                if progress != last_progress or status != last_status:
                    # Always send a status-only frame when status changes but
                    # progress has not — this catches planning→running,
                    # planning→done, etc. where the client would otherwise
                    # remain stuck waiting for a progress bump that never
                    # comes.  When progress also changed we bundle status
                    # into the progress frame; the duplicate key is harmless
                    # (the progress frame takes precedence).
                    if status != last_status and progress == last_progress:
                        yield f"data: {json.dumps({
                            'type': 'status',
                            'status': status,
                        })}\n\n"
                    elif progress != last_progress:
                        # Get tasks only when progress actually changed
                        tasks_data = await db.supervisor_tasks_get(
                            supervisor_id, owner,
                        )
                        yield f"data: {json.dumps({
                            'type': 'progress',
                            'progress': progress,
                            'status': status,
                            'tasks': tasks_data,
                        })}\n\n"
                    last_progress = progress
                    last_status = status

                # Check for new messages (the chat reads from this stream).
                #
                # Through db.supervisor_messages_get, not inline SQL. There were
                # two copies here -- one per branch, differing only in the
                # `id > ?` clause -- and neither filtered by owner, which made
                # three copies of "read a supervisor's messages" in the
                # repository, only one of them scoped. That is the shape F-21
                # came in: this path is safe because of the ownership check
                # above, and would stop being safe the moment somebody moved
                # the query or dropped the check. `after_id=0` means "from the
                # start", so the two branches collapse into one call.
                new_msgs = await db.supervisor_messages_get(
                    supervisor_id, owner, after_id=last_message_id, limit=10,
                )
                if new_msgs:
                    last_message_id = new_msgs[-1]["id"]
                    yield f"data: {json.dumps({
                        'type': 'messages',
                        'messages': new_msgs,
                    })}\n\n"

                # Get recent events from engine if available
                if eng:
                    events = eng.tracker.recent_events(5)
                    if events != last_events:
                        yield f"data: {json.dumps({
                            'type': 'events',
                            'events': events,
                        })}\n\n"
                        last_events = events

                # Check if done
                status = current.get("status", "")
                if status in ("done", "error"):
                    yield f"data: {json.dumps({
                        'type': 'done',
                        'status': status,
                    })}\n\n"
                    return

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("supervisor stream failed id=%s", supervisor_id)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Stream error'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_supervisor_task_stream(request: Request, supervisor_id: str, task_id: str):
    """GET /api/supervisors/{id}/tasks/{taskId}/stream — SSE stream for one task."""
    session = request.state.session
    owner = session["user"]

    # Verify ownership
    existing = await db.supervisor_get(supervisor_id, owner)
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")

    # Verify task exists
    task = await db.supervisor_task_get(supervisor_id, task_id, owner)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'task_id': task_id})}\n\n"

            # The engine lookup that used to sit here was never read -- this
            # generator polls the task row rather than the in-memory engine.
            last_status = task.get("status") or "pending"

            while True:
                if await request.is_disconnected():
                    return

                # Read current task state from DB
                current = await db.supervisor_task_get(supervisor_id, task_id, owner)
                if not current:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Task deleted'})}\n\n"
                    return

                status = current.get("status", "pending")
                progress = float(current.get("progress_pct") or 0)
                result = current.get("result")

                # Emit status change
                if status != last_status:
                    yield f"data: {json.dumps({
                        'type': 'status',
                        'task_id': task_id,
                        'status': status,
                        'progress': progress,
                    })}\n\n"
                    last_status = status

                # Emit result when available
                if status == "done" and result and result != current.get("_last_result_sent"):
                    yield f"data: {json.dumps({
                        'type': 'result',
                        'task_id': task_id,
                        'result': result[:5000],
                    })}\n\n"
                    # Mark as sent by patching temporarily
                    current["_last_result_sent"] = result

                # Check if done/failed — stop stream
                if status in ("done", "error", "failed"):
                    yield f"data: {json.dumps({
                        'type': 'done',
                        'status': status,
                    })}\n\n"
                    return

                await asyncio.sleep(1.0)

                # Re-read current state with fresh reference
                current = await db.supervisor_task_get(supervisor_id, task_id, owner)

        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("supervisor task stream failed id=%s", task_id)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Stream error'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_supervisor_tasks_get(supervisor_id: str, owner_id: str):
    """GET /api/supervisors/{id}/tasks — list tasks for a supervisor."""
    tasks = await db.supervisor_tasks_get(supervisor_id, owner_id)
    return JSONResponse({"tasks": tasks, "count": len(tasks)})


async def handle_supervisor_messages_get(request: Request, supervisor_id: str):
    """GET /api/supervisors/{id}/messages — list messages for a supervisor."""
    session = request.state.session
    owner_id = session["user"]
    try:
        after_id = int(request.query_params.get("after", "0"))
    except (TypeError, ValueError):
        after_id = 0
    messages = await db.supervisor_messages_get(supervisor_id, owner_id, after_id)
    return JSONResponse({"messages": messages, "count": len(messages)})


@router.get("/api/supervisor")
async def _api_supervisor(request: Request):
    return await handle_supervisor(request)


@router.get("/api/supervisors/{supervisor_id}/members")
async def _api_supervisor_members_get(request: Request, supervisor_id: str):
    return await handle_supervisor_members_get(request, supervisor_id)


@router.post("/api/supervisors/{supervisor_id}/members")
async def _api_supervisor_members_add(request: Request, supervisor_id: str):
    return await handle_supervisor_members_add(request, supervisor_id)


@router.delete("/api/supervisors/{supervisor_id}/members/{chat_id}")
async def _api_supervisor_member_remove(
    request: Request, supervisor_id: str, chat_id: str
):
    return await handle_supervisor_member_remove(request, supervisor_id, chat_id)


@router.post("/api/supervisor/read")
async def _api_supervisor_read(request: Request):
    return await handle_supervisor_read(request)


@router.post("/api/supervisors/{supervisor_id}/send")
async def _api_supervisor_send(request: Request, supervisor_id: str):
    session = request.state.session
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    user_prompt = (body.get("prompt") or "").strip()

    if not user_prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    # The same cap the chat endpoints enforce. This path had none, so a two
    # megabyte prompt was accepted and forwarded, while the identical text sent
    # to a conversation was refused at 8000 characters. The limit is not only
    # about resources: it is enforced before anything reaches the subprocess,
    # so an unbounded value must not be able to arrive by a second door.
    if len(user_prompt) > config.PROMPT_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Prompt is too long")

    existing = await db.supervisor_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")

    # Get or create engine
    if supervisor_id not in _supervisor_engines:
        eng_new = supervisor.SupervisorEngine(supervisor_id, session["user"])
        _register_engine(supervisor_id, eng_new)
    else:
        eng_new = _supervisor_engines[supervisor_id]
        _touch_engine(supervisor_id)

    eng = eng_new
    await eng.start_from_user_prompt(user_prompt)

    # Store user message
    await db.supervisor_messages_append(supervisor_id, "user", user_prompt)

    # Set status to planning
    await db.supervisor_update(supervisor_id, session["user"], status="planning")

    return JSONResponse({
        "ok": True,
        "supervisor_id": supervisor_id,
        "status": "planning",
    })


@router.post("/api/supervisors/{supervisor_id}/pause")
async def _api_supervisor_pause(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/pause -- pause a running supervisor."""
    session = request.state.session
    existing = await db.supervisor_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    if existing.get("status") not in ("planning", "running"):
        raise HTTPException(status_code=409, detail="Supervisor is not running")
    eng = _supervisor_engines.get(supervisor_id)
    if eng and eng.pause():
        # Remember what we were doing before the pause so resume can restore it.
        if eng:
            eng.set_status_for_pause(existing.get("status"))
        await db.supervisor_update(supervisor_id, session["user"], status="paused")
        return JSONResponse({"ok": True, "status": "paused"})
    raise HTTPException(status_code=409, detail="Supervisor is already paused")


@router.post("/api/supervisors/{supervisor_id}/resume")
async def _api_supervisor_resume(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/resume -- resume a paused supervisor."""
    session = request.state.session
    existing = await db.supervisor_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    if existing.get("status") != "paused":
        raise HTTPException(status_code=409, detail="Supervisor is not paused")
    eng = _supervisor_engines.get(supervisor_id)
    if eng and eng.resume():
        # Restore the status the engine had before it was paused.
        restore = eng._pre_pause_status or "running"
        await db.supervisor_update(supervisor_id, session["user"], status=restore)
        return JSONResponse({"ok": True, "status": restore})
    raise HTTPException(status_code=409, detail="Supervisor is not paused")


# Supervisor routes — these match the pattern from the design spec.
@router.get("/api/supervisors")
async def _api_supervisors_list(request: Request):
    session = request.state.session
    list_ = await db.supervisor_list(session["user"])
    return JSONResponse({"supervisors": list_, "count": len(list_)})


# Serialised size ceiling for a supervisor's config blob.
#
# Unlike the prompt cap next door this is not the §2 subprocess boundary --
# config is stored and read back, never passed to the CLI -- so the concern is
# unbounded input rather than execution. It still wants a limit: the value is
# json.dumps'd straight into a TEXT column, so without one a client can store
# an arbitrarily large document, and an unserialisable one reaches json.dumps
# inside the DB layer and surfaces as a 500 rather than the 400 it is.
_SUPERVISOR_CONFIG_MAX: Final[int] = 64 * 1024

# Per-owner cap on live supervisor rows. Without one, a single authenticated
# account can create supervisors without limit -- each row is small on its
# own, but every one also seeds an engine in the process-wide registry above,
# so unbounded creation is unbounded memory, not just unbounded rows.
_MAX_SUPERVISORS_PER_OWNER: Final[int] = 50


def _validated_supervisor_config(raw: Any) -> Any:
    """Return *raw* if it is storable, else raise 400 with the reason."""
    if raw is None:
        return None
    try:
        encoded = json.dumps(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="config must be JSON-serialisable"
        ) from None
    if len(encoded) > _SUPERVISOR_CONFIG_MAX:
        raise HTTPException(status_code=400, detail="config is too large")
    return raw


@router.post("/api/supervisors")
async def _api_supervisors_create(request: Request):
    session = request.state.session
    existing = await db.supervisor_list(session["user"])
    if len(existing) >= _MAX_SUPERVISORS_PER_OWNER:
        raise HTTPException(
            status_code=429,
            detail=f"Limit of {_MAX_SUPERVISORS_PER_OWNER} supervisors reached "
                   f"— delete one before creating another",
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    title = (body.get("title") or "New Supervisor").strip()[:200]
    description = (body.get("description") or "").strip()[:500] or None
    config_data = _validated_supervisor_config(
        body.get("config") if body.get("config") else None
    )
    sid = uuid.uuid4().hex
    await db.supervisor_create(sid, title, description, session["user"], config_data)
    eng = supervisor.SupervisorEngine(sid, session["user"])
    _register_engine(sid, eng)
    return JSONResponse({"ok": True, "id": sid, "title": title, "status": "idle"})


@router.get("/api/supervisors/{supervisor_id}")
async def _api_supervisor_get(request: Request, supervisor_id: str):
    session = request.state.session
    existing = await db.supervisor_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    return JSONResponse({"supervisor": existing})


@router.patch("/api/supervisors/{supervisor_id}")
async def _api_supervisor_patch(request: Request, supervisor_id: str):
    session = request.state.session
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not body:
        raise HTTPException(status_code=400, detail="No fields to update")
    existing = await db.supervisor_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    updates: dict[str, Any] = {}
    if "title" in body:
        val = str(body["title"]).strip()[:200]
        if val:
            updates["title"] = val
    if "description" in body:
        val = str(body.get("description") or "").strip()[:500] or None
        updates["description"] = val
    if "status" in body:
        val = body.get("status")
        if isinstance(val, str) and val in ("idle", "planning", "running", "paused", "done", "error"):
            updates["status"] = val
    if "config" in body:
        val = body.get("config")
        if isinstance(val, dict):
            updates["config"] = _validated_supervisor_config(val)
    if updates:
        if "status" in updates:
            status = updates.pop("status")
            await db.supervisor_update(supervisor_id, session["user"], **updates, status=status)
            eng = _supervisor_engines.get(supervisor_id)
            if status == "running" and eng and not eng._running:
                eng.spawn(eng.run_schedule_loop())
            elif status in ("idle", "done", "error") and eng:
                eng.stop()
        else:
            await db.supervisor_update(supervisor_id, session["user"], **updates)
            if "config" in updates:
                eng = _supervisor_engines.get(supervisor_id)
                if eng:
                    eng.config = updates["config"]
                    eng.router = supervisor.ModelRouter(updates["config"])
    existing = await db.supervisor_get(supervisor_id, session["user"])
    return JSONResponse({"ok": True, "supervisor": existing})


@router.delete("/api/supervisors/{supervisor_id}")
async def _api_supervisor_delete(request: Request, supervisor_id: str):
    session = request.state.session
    deleted = await db.supervisor_delete(supervisor_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    _supervisor_engines.pop(supervisor_id, None)
    return JSONResponse({"ok": True})


@router.get("/api/supervisors/{supervisor_id}/stream")
async def _api_supervisor_stream(request: Request, supervisor_id: str):
    return await handle_supervisor_stream(request, supervisor_id)


@router.get("/api/supervisors/{supervisor_id}/tasks")
async def _api_supervisor_tasks_get(request: Request, supervisor_id: str):
    session = request.state.session
    return await handle_supervisor_tasks_get(supervisor_id, session["user"])


@router.get("/api/supervisors/{supervisor_id}/tasks/{task_id}/stream")
async def _api_supervisor_task_stream(request: Request, supervisor_id: str, task_id: str):
    return await handle_supervisor_task_stream(request, supervisor_id, task_id)


@router.get("/api/supervisors/{supervisor_id}/messages")
async def _api_supervisor_messages(request: Request, supervisor_id: str):
    return await handle_supervisor_messages_get(request, supervisor_id)
