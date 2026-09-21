"""Routes for /api/supervisors: the orchestrator list, its tasks, members and streams.

Part of the 0.10.0 routes split. The cluster was measured: every function
reached from a route with this prefix, closed over its private helpers.
Registration stays in app.py -- FastAPI matches routes in the order routers are
included, so that order belongs in one readable place.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sqlite3
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import config
import db
import orchestrator
import transcripts
import turns
from classification import _classify_cli_session, _cli_maps, classify_chat
from routes.machines import known_backend_models
from routes.misc import handle_sessions_resume
from shared import _HEX_SESSION_ID_RE, owner_of

_log = logging.getLogger("wc.app")

router = APIRouter()


async def handle_supervisor(request: Request):
    """GET /api/orchestrator -- which agents are waiting on the user.

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
        # An empty dict renders as "nothing queued", which is exactly what a
        # healthy idle console looks like -- so a broken query here is
        # invisible in the UI. Degrade, but say so.
        _log.warning("supervisor: queue_counts failed", exc_info=True)
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
        # Same shape: [] means "no terminal sessions", which is a normal state.
        # This call reaches the filesystem and, when remote discovery is
        # enabled, SSH -- both of which fail in ways worth knowing about.
        _log.warning("supervisor: read_claude_sessions failed", exc_info=True)
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


# Ordered so the panel reads worst-first. A orchestrator is opened to find out
# what needs attention, and a failed agent buried under six healthy ones is the
# one thing the view must not do.
_MEMBER_ORDER: Final[dict[str, int]] = {
    "waiting": 0, "working": 1, "updated": 2, "idle": 3,
}


async def handle_orchestrator_members_get(request: Request, supervisor_id: str):
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
    member would otherwise vanish -- and a orchestrator that hides the agents you
    added to it is worse than one that says nothing.
    """
    session = request.state.session
    owner = session["user"]
    if not await db.orchestrator_get(supervisor_id, owner):
        raise HTTPException(status_code=404, detail="Orchestrator not found")

    rows = await db.orchestrator_members_list(supervisor_id)
    if not rows:
        return JSONResponse({"members": [], "count": 0})

    marks = await db.read_marks_get(owner)
    live_ids = turns.running_ids(owner)
    try:
        queued = await db.queue_counts(owner)
    except Exception:
        # As above in handle_supervisor: {} is indistinguishable from an idle
        # console, so a failing query would never surface.
        _log.warning("orchestrator members: queue_counts failed", exc_info=True)
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
            # all. The panel raised AttributeError and 500d for every orchestrator
            # with a quiet member, while the docstring above described the
            # behaviour that was intended and never written.
            "last_seen": last.get("created_at") if last else None,
            "status": "idle",
        })
    members.sort(key=lambda e: (
        # A failure outranks every other reason to be waiting: a orchestrator is
        # opened to find what needs attention, and a broken agent buried under
        # six healthy ones is the one thing this view must not do.
        0 if e.get("reason") == "failed" else 1,
        _MEMBER_ORDER.get(e.get("status") or "idle", 9),
        e.get("since") or "",
    ))
    return JSONResponse({"members": members, "count": len(members)})


async def handle_orchestrator_members_add(request: Request, supervisor_id: str):
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
    if not await db.orchestrator_get(supervisor_id, owner):
        raise HTTPException(status_code=404, detail="Orchestrator not found")
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
        if await db.orchestrator_member_add(supervisor_id, chat_id):
            added.append(chat_id)
        else:
            skipped.append(chat_id)

    _log.info(
        "supervisor_members_added orchestrator=%s added=%d already=%d failed=%d",
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
    account's conversation into a orchestrator the caller owns, exposing its
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


async def handle_orchestrator_member_remove(
    request: Request, supervisor_id: str, chat_id: str
):
    """DELETE /api/supervisors/{id}/members/{chat_id} -- stop watching it.

    Membership only. The conversation is left exactly as it was: a orchestrator
    is a view over work, not its owner, and removing a member must never be a
    way to lose one.
    """
    session = request.state.session
    if not await db.orchestrator_get(supervisor_id, session["user"]):
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    removed = await db.orchestrator_member_remove(supervisor_id, chat_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Not a member")
    _log.info("supervisor_member_removed orchestrator=%s chat=%s", supervisor_id, chat_id)
    return JSONResponse({"ok": True, "removed": chat_id})


async def handle_orchestrator_read(request: Request):
    """POST /api/orchestrator/read -- mark an agent as seen, clearing its badge."""
    session = request.state.session
    owner = await owner_of(session)

    data = await request.json()
    if data.get("all"):
        # Clear everything currently listed. Deliberate, so it silences
        # unanswered questions too -- which opening one does not.
        current = json.loads((await handle_supervisor(request)).body)
        cleared = 0
        for entry in [*current.get("waiting", []), *current.get("updated", [])]:
            await db.read_mark_set(
                owner, entry["kind"], entry["id"], dismiss=True
            )
            cleared += 1
        _log.info("orchestrator cleared by user=%s entries=%d", owner, cleared)
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
        owner, kind, ref_id, dismiss=bool(data.get("dismiss"))
    )
    return JSONResponse({"ok": True, "read_at": read_at})


# ── Orchestrator orchestration ──────────────────────────────────────────────────────
# Registry of live engine instances, keyed by supervisor_id. Engines are
# started on first use and cleaned up when their orchestrator is deleted.
#
# Capped at _MAX_ORCHESTRATOR_ENGINES: without a bound, an account that only
# ever creates supervisors and never deletes them grows this dict forever --
# each entry holds a TaskGraph, a ProgressTracker and a set of background
# asyncio.Task references, so the memory is not trivial. An OrderedDict lets
# eviction take the least-recently-touched entry rather than an arbitrary one;
# "touched" means created or looked up, via _touch_engine below. A orchestrator
# whose engine is evicted still has its state in the database -- eviction only
# drops the live scheduler, the same as if the process had just restarted.
_supervisor_engines: OrderedDict[str, orchestrator.OrchestratorEngine] = OrderedDict()
_MAX_ORCHESTRATOR_ENGINES: Final[int] = 200


def _touch_engine(supervisor_id: str) -> None:
    """Mark an engine as recently used, for LRU ordering."""
    if supervisor_id in _supervisor_engines:
        _supervisor_engines.move_to_end(supervisor_id)


def _register_engine(supervisor_id: str, eng: orchestrator.OrchestratorEngine) -> None:
    """Insert a new engine, evicting the least-recently-touched one if full."""
    _supervisor_engines[supervisor_id] = eng
    _supervisor_engines.move_to_end(supervisor_id)
    while len(_supervisor_engines) > _MAX_ORCHESTRATOR_ENGINES:
        evicted_id, evicted = _supervisor_engines.popitem(last=False)
        evicted.stop()
        _log.warning(
            "supervisor_engine_evicted supervisor_id=%s (registry at cap %d)",
            evicted_id, _MAX_ORCHESTRATOR_ENGINES,
        )


# handle_orchestrator_crud and handle_orchestrator_send_prompt lived here: 169
# lines implementing orchestrator CRUD and prompt-send on /api/orchestrator
# (singular). Neither was decorated and neither was called -- the live
# routes are /api/supervisors (plural) below, and _api_orchestrator_send
# reimplements the send inline. Removed rather than wired up: two
# implementations of the same endpoints, one of them unreachable, is a
# standing invitation to fix the copy that does not run.
async def handle_orchestrator_stream(request: Request, supervisor_id: str):
    """GET /api/supervisors/{id}/stream — SSE stream of orchestrator progress."""
    session = request.state.session
    owner = session["user"]

    existing = await db.orchestrator_get(supervisor_id, owner)
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")

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

                # Read current orchestrator state from DB
                current = await db.orchestrator_get(supervisor_id, owner)
                if not current:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Orchestrator deleted'})}\n\n"
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
                        #
                        # Run through _display_task exactly like
                        # handle_orchestrator_tasks_get does, so the ids this
                        # frame merges into state.tasks (web/assets/orchestrator
                        # /stream.js, handleProgress, keyed on t.id) match the
                        # ids /tasks already put there. Without this the raw,
                        # "{supervisor_id}:"-prefixed storage id never matches
                        # the stripped id the client already has, and every
                        # progress frame appends a duplicate task instead of
                        # updating the existing one.
                        tasks_data = [
                            _display_task(t, supervisor_id)
                            for t in await db.orchestrator_tasks_get(
                                supervisor_id, owner,
                            )
                        ]
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
                # Through db.orchestrator_messages_get, not inline SQL. There were
                # two copies here -- one per branch, differing only in the
                # `id > ?` clause -- and neither filtered by owner, which made
                # three copies of "read a orchestrator's messages" in the
                # repository, only one of them scoped. That is the shape F-21
                # came in: this path is safe because of the ownership check
                # above, and would stop being safe the moment somebody moved
                # the query or dropped the check. `after_id=0` means "from the
                # start", so the two branches collapse into one call.
                new_msgs = await db.orchestrator_messages_get(
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
            _log.exception("orchestrator stream failed id=%s", supervisor_id)
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


async def handle_orchestrator_task_stream(request: Request, supervisor_id: str, task_id: str):
    """GET /api/supervisors/{id}/tasks/{taskId}/stream — SSE stream for one task."""
    session = request.state.session
    owner = session["user"]

    # Verify ownership
    existing = await db.orchestrator_get(supervisor_id, owner)
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")

    # Verify task exists. `task_id` here is whatever the client has, and the
    # client only ever sees ids through _display_task -- stripped of the
    # "{supervisor_id}:" prefix handle_orchestrator_run stamps onto rows it
    # creates (see _display_task). Storage still has the raw, prefixed id, so
    # look that up first; fall back to the id as given for legacy-engine rows,
    # which _display_task never touches. Without this, every id /tasks hands
    # out 404s here because the raw lookup below never matches the stripped
    # one the client was given.
    stored_task_id = f"{supervisor_id}:{task_id}"
    task = await db.orchestrator_task_get(supervisor_id, stored_task_id, owner)
    if not task:
        stored_task_id = task_id
        task = await db.orchestrator_task_get(supervisor_id, stored_task_id, owner)
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
                current = await db.orchestrator_task_get(supervisor_id, stored_task_id, owner)
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
                current = await db.orchestrator_task_get(supervisor_id, stored_task_id, owner)

        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("orchestrator task stream failed id=%s", task_id)
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


async def handle_orchestrator_tasks_get(supervisor_id: str, owner_id: str):
    """GET /api/supervisors/{id}/tasks — list tasks for a orchestrator.

    Carries the run's spend alongside its tasks rather than in an endpoint of
    its own: the page that shows progress is the page that should be able to
    say what the progress cost, and it already polls this one. A second
    endpoint would be a second poll for a figure derived from the same task
    list this response is built from.
    """
    tasks = await db.orchestrator_tasks_get(supervisor_id, owner_id)
    return JSONResponse({
        "tasks": [_display_task(t, supervisor_id) for t in tasks],
        "count": len(tasks),
        "cost": await db.orchestrator_cost(supervisor_id, owner_id),
    })


def _display_task(task: dict, supervisor_id: str) -> dict:
    """Strip the ``"{supervisor_id}:"`` namespacing handle_orchestrator_run
    stamps onto a task's id and its depends_on entries (see that function),
    so what the operator sees matches the plain id they approved -- "task-1",
    not "<uuid>:task-1". The prefix was needed only to keep this run's ids
    from colliding with another run's at the storage layer; nothing outside
    that layer should ever have to look at it.

    A legacy-engine row's id (``{orchestrator_id[:8]}_{plan_id}``, a
    different separator, on purpose -- see _row_id in orchestrator.py) never
    starts with this prefix, so this is a no-op for it.
    """
    out = dict(task)
    prefix = f"{supervisor_id}:"
    raw_id = str(out.get("id") or "")
    if raw_id.startswith(prefix):
        out["id"] = raw_id[len(prefix):]
    deps = out.get("depends_on")
    if isinstance(deps, str) and deps:
        try:
            parsed = json.loads(deps)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            out["depends_on"] = json.dumps([
                d[len(prefix):] if isinstance(d, str) and d.startswith(prefix) else d
                for d in parsed
            ])
    return out


async def handle_orchestrator_messages_get(request: Request, supervisor_id: str):
    """GET /api/supervisors/{id}/messages — list messages for a orchestrator."""
    session = request.state.session
    owner_id = session["user"]
    try:
        after_id = int(request.query_params.get("after", "0"))
    except (TypeError, ValueError):
        after_id = 0
    messages = await db.orchestrator_messages_get(supervisor_id, owner_id, after_id)
    return JSONResponse({"messages": messages, "count": len(messages)})


async def _allowed_models(owner_id: str) -> set[str]:
    """The model ids a proposed or approved plan may name.

    Reuses ``known_backend_models`` (routes/machines.py): a local, no-network
    read of ``config.KNOWN_MODELS`` plus every configured machine's default
    and active models. ``owner_id`` is accepted but not used to scope the
    query -- the same shape ``known_backend_models`` already uses for the
    Backends/Models combo box -- because this set is a security allowlist
    (validate_plan never lets a plan's ``model`` field reach the CLI's
    ``--model`` flag unless it is a member) rather than a per-owner resource
    list.
    """
    known = await known_backend_models()
    return set(known.ids)


async def handle_orchestrator_plan(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/plan -- validate a proposed plan.

    Never executes anything: this is the approval gate. A plan that cannot be
    read comes back as ``rows: []`` plus ``errors``, and the raw text the
    operator typed, so they can fix it by hand rather than re-guessing what
    they wrote. On 2026-08-30 the previous orchestrator parsed a plan into
    nothing, reported nothing, and executed anyway -- burning two tasks. This
    endpoint's whole job is to make that outcome visible instead.
    """
    session = request.state.session
    owner = await owner_of(session)
    existing = await db.orchestrator_get(supervisor_id, owner)
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    raw = str(data.get("raw") or "")
    allowed = await _allowed_models(owner)
    rows, errors = orchestrator.validate_plan(raw, allowed)
    return JSONResponse({"rows": rows, "errors": errors, "raw": raw})


# Strong references for run_tasks()'s background execution, mirroring
# app.py's _startup_tasks / _log_startup_task: asyncio only holds a weak
# reference to a running task, so a bare asyncio.create_task can be
# garbage-collected mid-run with nothing logged. Discarded on completion via
# the done callback below, so this does not grow without bound.
_run_tasks_bg: set[asyncio.Task[Any]] = set()


def _log_run_tasks_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        _log.info("orchestrator_run_tasks_cancelled task=%s", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        _log.error(
            "orchestrator_run_tasks_failed task=%s", task.get_name(), exc_info=exc,
        )


async def handle_orchestrator_run(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/run -- persist the approved rows and run.

    Only rows the operator actually approved reach ``db.orchestrator_task_
    create``: nothing here re-derives a plan from stored state, so this can
    only run what was in the request body. Each row's ``model`` is checked
    against the allowlist again here, independent of ``validate_plan`` --
    the operator may have hand-edited the rows returned by ``/plan`` (that
    edit path is the whole point of returning ``raw`` and ``rows``
    separately), so a row reaching this endpoint is not guaranteed to have
    passed that check.
    """
    session = request.state.session
    owner = await owner_of(session)
    run = await db.orchestrator_get(supervisor_id, owner)
    if not run:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    rows = data.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="no tasks to run")

    allowed = await _allowed_models(owner)
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(status_code=400, detail="each row must be an object")
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            raise HTTPException(status_code=400, detail="each row needs an id")
        # Collapsed, not merely stripped -- same fix and the same reason as
        # validate_plan's: a title is spliced verbatim into a dependent
        # task's "### Result of {title}" header (orchestrator._prompt_for),
        # and a multi-line title forges a second, fake task boundary ahead
        # of the genuine one. Mutated in place so every later read of
        # row["title"] in this function -- including the create loop below
        # -- sees the collapsed form; a hand-edited /run row never went
        # through validate_plan's own gate, so this is not redundant with it.
        title = " ".join(str(row.get("title") or "").split())
        row["title"] = title
        if not title:
            raise HTTPException(status_code=400, detail="each row needs a title")
        if not str(row.get("prompt") or "").strip():
            raise HTTPException(status_code=400, detail="each row needs a prompt")
        model = row.get("model")
        # Same allowlist membership test as validate_plan, and for the same
        # reason: never a raw argv token reaching --model.
        if model is not None and str(model) not in allowed:
            raise HTTPException(
                status_code=400, detail=f"model {model!r} is not on the allowlist",
            )
        ids.append(row_id)

    # Fix round 1, MINOR 3: validate_plan already rejects a duplicate id --
    # orchestrator_tasks.id is a bare TEXT PRIMARY KEY, so without this check
    # a duplicate in a hand-edited batch reaches db.orchestrator_task_create
    # mid-loop, some rows already committed, and the operator sees an opaque
    # 500 from the IntegrityError instead of the same clean 400 /plan gives.
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="duplicate task id in rows")

    # Fix round 1, IMPORTANT 1: validate_plan rejects a dependency cycle, but
    # /run is a second, independent entry point for hand-edited rows that may
    # never have gone through /plan again. Without this, a cycle posted here
    # leaves every task in it stuck "pending" forever (run_tasks's scheduler
    # has nothing ready to run) while the orchestrator itself ends "error"
    # with nothing on either row explaining why -- confirmed by execution.
    normalised = [
        {"id": row_id, "depends_on": [str(d) for d in (row.get("depends_on") or [])]}
        for row_id, row in zip(ids, rows)
    ]
    if orchestrator._has_cycle(normalised):
        raise HTTPException(status_code=400, detail="the plan has a dependency cycle")

    if not run.get("work_dir"):
        # Every orchestrator created after this task's other half (the
        # /api/orchestrators POST handler) has one; an older row created
        # before that change would not, and run_tasks needs a real shared
        # directory to hand task chats.
        raise HTTPException(
            status_code=409, detail="orchestrator has no workspace -- recreate it",
        )

    if not run.get("planner_chat_id"):
        # Mirrors the work_dir guard just above, and for the same reason:
        # every orchestrator created after the /api/orchestrators POST
        # handler was given a parent chat has one; an older row created
        # before that change would not. Without a parent, create_task_chat
        # sets every task chat's parent_chat_id to None -- so it becomes a
        # ROOT chat, which is exactly what the hierarchy design's
        # parent_chat_id-based sidebar predicate uses to decide what to
        # show, and every task chat of the run would flood the sidebar
        # instead of nesting under a family card.
        raise HTTPException(
            status_code=409,
            detail="orchestrator has no parent conversation -- recreate it",
        )

    # CRITICAL fix: orchestrator_tasks.id is a bare TEXT PRIMARY KEY (see
    # db.py's CREATE TABLE), not a composite (orchestrator_id, id) key -- and
    # plan-local ids repeat by construction: plan.js's _nextRowId resets its
    # counter to 1 every time the plan dialog opens, and validate_plan's own
    # id fallback is the array index ("0", "1", ...) when a row carries no
    # explicit id. So the SECOND run ever created, of ANY orchestrator, hit
    # the UNIQUE constraint on a duplicate GLOBAL id -- reproduced by
    # execution: first run 200, second run 500, zero rows persisted on the
    # second (the loop below had already inserted some rows from THIS run
    # before the failing one, an opaque IntegrityError-turned-500).
    #
    # Namespaced with the orchestrator's own id, the same shape
    # orchestrator.py's `_row_id` already uses for the legacy engine's
    # `_materialise_plan` (see that function) -- a different separator
    # (":" rather than "_") so the two paths' ids are never confused with
    # each other, which fix 7's /send interlock below relies on.
    # `depends_on` values are namespaced the same way and consistently: they
    # are read back by run_tasks and compared directly against the (now
    # namespaced) `id` column, and by _prompt_for's own dependency lookups
    # (db.orchestrator_task_get keyed on the same namespaced id) -- if
    # depends_on were left bare, every dependency would silently stop
    # resolving the moment ids became namespaced, with no error raised
    # anywhere: run_tasks would simply never see any of a task's
    # dependencies as satisfied.
    def _namespaced(row_id: str) -> str:
        return f"{supervisor_id}:{row_id}"

    id_map = {row_id: _namespaced(row_id) for row_id in ids}

    for row in rows:
        row_id = str(row["id"])
        namespaced_depends_on = [
            id_map.get(str(d), str(d)) for d in (row.get("depends_on") or [])
        ]
        try:
            await db.orchestrator_task_create(
                supervisor_id, id_map[row_id], row["title"], row["prompt"],
                model=row.get("model"), depends_on=namespaced_depends_on,
            )
        except sqlite3.IntegrityError as exc:
            # A clean 400 naming the offending id, never an opaque 500 with
            # some of this run's rows already committed. The namespacing
            # above makes this vanishingly unlikely in ordinary use -- it
            # would take two /run calls racing on the exact same
            # orchestrator and row id -- but the failure mode this replaces
            # (partial write, opaque 500) is exactly what fix round 1's
            # MINOR 3 already fixed for the *duplicate-within-batch* case
            # above; this is the same fix for the *storage-layer* case.
            raise HTTPException(
                status_code=400,
                detail=f"task id {row_id!r} could not be created: {exc}",
            ) from None

    task = asyncio.create_task(
        orchestrator.run_tasks(
            supervisor_id, owner, run["planner_chat_id"], run["work_dir"],
        ),
        name=f"orchestrator-run-{supervisor_id}",
    )
    _run_tasks_bg.add(task)
    task.add_done_callback(_run_tasks_bg.discard)
    task.add_done_callback(_log_run_tasks_failure)

    return JSONResponse({"ok": True})


@router.get("/api/orchestrator")
async def _api_supervisor(request: Request):
    return await handle_supervisor(request)


@router.get("/api/orchestrators/{supervisor_id}/members")
async def _api_orchestrator_members_get(request: Request, supervisor_id: str):
    return await handle_orchestrator_members_get(request, supervisor_id)


@router.post("/api/orchestrators/{supervisor_id}/members")
async def _api_orchestrator_members_add(request: Request, supervisor_id: str):
    return await handle_orchestrator_members_add(request, supervisor_id)


@router.delete("/api/orchestrators/{supervisor_id}/members/{chat_id}")
async def _api_orchestrator_member_remove(
    request: Request, supervisor_id: str, chat_id: str
):
    return await handle_orchestrator_member_remove(request, supervisor_id, chat_id)


@router.post("/api/orchestrator/read")
async def _api_orchestrator_read(request: Request):
    return await handle_orchestrator_read(request)


async def _refuse_if_run_path_owns_this(supervisor_id: str, owner_id: str) -> None:
    """Raise 409 when *supervisor_id*'s task rows were created by /run.

    /send starts the legacy ``<<PLAN>>`` engine
    (``OrchestratorEngine.start_from_user_prompt`` ->
    ``_run_planner_turn`` -> ``_materialise_plan``), and the composer in the
    run view still posts to it -- so without this guard an operator could
    approve a plan, click Run (scheduling ``orchestrator.run_tasks`` over
    rows ``handle_orchestrator_run`` already persisted), then type into the
    composer and start a SECOND scheduler over the same
    ``orchestrator_tasks`` rows. ``/pause`` and ``/resume`` drive that same
    legacy engine and are refused here for the same reason.

    Identified by the ``"{supervisor_id}:"`` id prefix
    ``handle_orchestrator_run`` stamps on every row it creates (fix 1's
    namespacing) -- the legacy engine's own namespacing
    (``{orchestrator_id[:8]}_{plan_id}``, a different, non-colliding
    separator; see ``_row_id`` in ``orchestrator.py``) never produces that
    prefix, so the two paths' rows can never be mistaken for each other.

    This is isolation, not deletion: the legacy engine stays in place --
    other subsystems still hook it -- and is merely refused a second
    execution surface over rows the new path already owns. Deleting it is
    follow-up work (see this design's spec, amended alongside this fix).
    """
    tasks = await db.orchestrator_tasks_get(supervisor_id, owner_id)
    prefix = f"{supervisor_id}:"
    if any(str(t.get("id") or "").startswith(prefix) for t in tasks):
        raise HTTPException(
            status_code=409,
            detail="this orchestrator's tasks were created by /run; the "
                   "legacy engine cannot start a second scheduler over them",
        )


@router.post("/api/orchestrators/{supervisor_id}/send")
async def _api_orchestrator_send(request: Request, supervisor_id: str):
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

    existing = await db.orchestrator_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    await _refuse_if_run_path_owns_this(supervisor_id, session["user"])

    # Get or create engine
    if supervisor_id not in _supervisor_engines:
        eng_new = orchestrator.OrchestratorEngine(supervisor_id, session["user"])
        _register_engine(supervisor_id, eng_new)
    else:
        eng_new = _supervisor_engines[supervisor_id]
        _touch_engine(supervisor_id)

    eng = eng_new
    await eng.start_from_user_prompt(user_prompt)

    # Store user message
    await db.orchestrator_messages_append(supervisor_id, "user", user_prompt)

    # Set status to planning
    await db.orchestrator_update(supervisor_id, session["user"], status="planning")

    return JSONResponse({
        "ok": True,
        "supervisor_id": supervisor_id,
        "status": "planning",
    })


@router.post("/api/orchestrators/{supervisor_id}/pause")
async def _api_orchestrator_pause(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/pause -- pause a running orchestrator."""
    session = request.state.session
    existing = await db.orchestrator_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    await _refuse_if_run_path_owns_this(supervisor_id, session["user"])
    if existing.get("status") not in ("planning", "running"):
        raise HTTPException(status_code=409, detail="Orchestrator is not running")
    eng = _supervisor_engines.get(supervisor_id)
    if eng and eng.pause():
        # Remember what we were doing before the pause so resume can restore it.
        if eng:
            eng.set_status_for_pause(existing.get("status"))
        await db.orchestrator_update(supervisor_id, session["user"], status="paused")
        return JSONResponse({"ok": True, "status": "paused"})
    raise HTTPException(status_code=409, detail="Orchestrator is already paused")


@router.post("/api/orchestrators/{supervisor_id}/resume")
async def _api_orchestrator_resume(request: Request, supervisor_id: str):
    """POST /api/supervisors/{id}/resume -- resume a paused orchestrator."""
    session = request.state.session
    existing = await db.orchestrator_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    await _refuse_if_run_path_owns_this(supervisor_id, session["user"])
    if existing.get("status") != "paused":
        raise HTTPException(status_code=409, detail="Orchestrator is not paused")
    eng = _supervisor_engines.get(supervisor_id)
    if eng and eng.resume():
        # Restore the status the engine had before it was paused.
        restore = eng._pre_pause_status or "running"
        await db.orchestrator_update(supervisor_id, session["user"], status=restore)
        return JSONResponse({"ok": True, "status": restore})
    raise HTTPException(status_code=409, detail="Orchestrator is not paused")


# Orchestrator routes — these match the pattern from the design spec.
@router.get("/api/orchestrators")
async def _api_supervisors_list(request: Request):
    session = request.state.session
    list_ = await db.orchestrator_list(session["user"])
    return JSONResponse({"supervisors": list_, "count": len(list_)})


# Serialised size ceiling for a orchestrator's config blob.
#
# Unlike the prompt cap next door this is not the §2 subprocess boundary --
# config is stored and read back, never passed to the CLI -- so the concern is
# unbounded input rather than execution. It still wants a limit: the value is
# json.dumps'd straight into a TEXT column, so without one a client can store
# an arbitrarily large document, and an unserialisable one reaches json.dumps
# inside the DB layer and surfaces as a 500 rather than the 400 it is.
_SUPERVISOR_CONFIG_MAX: Final[int] = 64 * 1024

# Per-owner cap on live orchestrator rows. Without one, a single authenticated
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


@router.post("/api/orchestrators")
async def _api_supervisors_create(request: Request):
    session = request.state.session
    # owner_of, not the raw session["user"], because this handler is about
    # to call db.chat_create -- which raises ValueError outright for a
    # legacy session still carrying a login *name* rather than an id (see
    # owner_of's own docstring in shared.py). Every other db.* call in this
    # function tolerates that shape; chat_create does not.
    owner = await owner_of(session)
    existing = await db.orchestrator_list(owner)
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
    title = (body.get("title") or "New Orchestrator").strip()[:200]
    description = (body.get("description") or "").strip()[:500] or None
    config_data = _validated_supervisor_config(
        body.get("config") if body.get("config") else None
    )
    # A run's shared workspace, same shape as handle_chat_create's work_dir:
    # <slug>-<date>, uniquified with a counter when that path already exists.
    # Task 1 added the column but deliberately left it unpopulated -- this is
    # its first consumer (run_tasks, below, needs a directory that already
    # exists on disk), so this is where it gets written.
    #
    # Computed and created BEFORE the row is inserted, same order
    # handle_chat_create uses -- a failed mkdir must not leave an orchestrator
    # row the client never received an id for and nobody can clean up. Fix
    # round 1, MINOR 2: the row used to be inserted first, so an OSError here
    # orphaned it.
    slug = db.slug_from_title(title)
    slug = db.slug_pattern(slug) or "untitled"
    date_suffix = datetime.datetime.now(datetime.UTC).date().isoformat()
    work_dir = str(Path(config.PROJECTS_ROOT).resolve() / f"{slug}-{date_suffix}")
    base = work_dir
    counter = 0
    while Path(work_dir).exists():
        counter += 1
        work_dir = f"{base}-{counter}"
    try:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.error(
            "could_not_create_orchestrator_workspace: failed to create "
            "work_dir=%s (check permissions, disk space, and "
            "PROJECTS_ROOT=%s): %s",
            work_dir, config.PROJECTS_ROOT, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not create orchestrator workspace — check server logs for details",
        )

    sid = uuid.uuid4().hex
    await db.orchestrator_create(sid, title, description, owner, config_data)
    await db.orchestrator_update(sid, owner, work_dir=work_dir)

    # The run's parent conversation. planner_chat_id was, until now, only
    # ever written by the legacy engine's _run_planner_turn -- so on the new
    # /run path it stayed NULL for every run ever created that way.
    # create_task_chat unconditionally does `db.chat_update(chat_id, owner_id,
    # parent_chat_id=parent_chat_id)`, and that update does not skip a None,
    # so every task chat's parent_chat_id ended up NULL too -- making every
    # task chat a ROOT chat. Two consequences, both closed by giving every
    # run a real parent at creation time, in the same directory as its
    # tasks: no run had a parent conversation to show a family card under,
    # and the hierarchy design's parent_chat_id-based sidebar predicate
    # (parent_chat_id IS NULL means "show it") could never hide a task chat,
    # because none of them had a non-null parent to be hidden by.
    planner_chat_id = uuid.uuid4().hex
    await db.chat_create(planner_chat_id, title, None, work_dir, owner)
    await db.orchestrator_set_planner_chat(sid, planner_chat_id)
    await db.orchestrator_member_add(sid, planner_chat_id)

    eng = orchestrator.OrchestratorEngine(sid, owner)
    _register_engine(sid, eng)
    return JSONResponse({"ok": True, "id": sid, "title": title, "status": "idle"})


@router.get("/api/orchestrators/{supervisor_id}")
async def _api_orchestrator_get(request: Request, supervisor_id: str):
    session = request.state.session
    existing = await db.orchestrator_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    return JSONResponse({"orchestrator": existing})


@router.patch("/api/orchestrators/{supervisor_id}")
async def _api_orchestrator_patch(request: Request, supervisor_id: str):
    session = request.state.session
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not body:
        raise HTTPException(status_code=400, detail="No fields to update")
    existing = await db.orchestrator_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
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
            await db.orchestrator_update(supervisor_id, session["user"], **updates, status=status)
            eng = _supervisor_engines.get(supervisor_id)
            if status == "running" and eng and not eng._running:
                eng.spawn(eng.run_schedule_loop())
            elif status in ("idle", "done", "error") and eng:
                eng.stop()
        else:
            await db.orchestrator_update(supervisor_id, session["user"], **updates)
            if "config" in updates:
                eng = _supervisor_engines.get(supervisor_id)
                if eng:
                    eng.config = updates["config"]
                    eng.router = orchestrator.ModelRouter(updates["config"])
    existing = await db.orchestrator_get(supervisor_id, session["user"])
    return JSONResponse({"ok": True, "orchestrator": existing})


@router.delete("/api/orchestrators/{supervisor_id}")
async def _api_orchestrator_delete(request: Request, supervisor_id: str):
    session = request.state.session
    deleted = await db.orchestrator_delete(supervisor_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Orchestrator not found")
    _supervisor_engines.pop(supervisor_id, None)
    return JSONResponse({"ok": True})


@router.get("/api/orchestrators/{supervisor_id}/stream")
async def _api_orchestrator_stream(request: Request, supervisor_id: str):
    return await handle_orchestrator_stream(request, supervisor_id)


@router.get("/api/orchestrators/{supervisor_id}/tasks")
async def _api_orchestrator_tasks_get(request: Request, supervisor_id: str):
    session = request.state.session
    return await handle_orchestrator_tasks_get(supervisor_id, session["user"])


@router.get("/api/orchestrators/{supervisor_id}/tasks/{task_id}/stream")
async def _api_orchestrator_task_stream(request: Request, supervisor_id: str, task_id: str):
    return await handle_orchestrator_task_stream(request, supervisor_id, task_id)


@router.get("/api/orchestrators/{supervisor_id}/messages")
async def _api_orchestrator_messages(request: Request, supervisor_id: str):
    return await handle_orchestrator_messages_get(request, supervisor_id)


@router.post("/api/orchestrators/{supervisor_id}/plan")
async def _api_orchestrator_plan(request: Request, supervisor_id: str):
    return await handle_orchestrator_plan(request, supervisor_id)


@router.post("/api/orchestrators/{supervisor_id}/run")
async def _api_orchestrator_run(request: Request, supervisor_id: str):
    return await handle_orchestrator_run(request, supervisor_id)
