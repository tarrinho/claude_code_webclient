"""Routes for /api/chats: the conversation list, messages, the turn pipeline and its SSE stream.

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
import os
import sqlite3
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import config
import db
import prompts
import runner
import transcripts
import turns
from classification import _asks_a_question
from routes.naming import generate_name as _generate_agent_name
from routes.voice import stream_voice_turn, voice_handoff as voice_handoff_fn
from shared import (
    _MODEL_RE,
    _SSE_INTERNAL,
    _question_to_text,
    _turn_to_message,
    acquire_sse_slot,
    backend_kind,
    release_sse_slot,
)

_log = logging.getLogger("wc.app")

router = APIRouter()


async def _resolve_transport_name(chat_id: str, owner: str | None) -> str:
    """Resolve a transport/agent display name for a turn.

    Returns the SSH transport host (tunnelled), the machine's alias or host,
    or ``"local"`` as the fallback.  Best-effort: failures are silent.
    """
    import db as _db

    try:
        routing = await _db.chat_routing(chat_id)
        machine = routing.get("machine") or await _db.ai_machine_backend(owner) if owner else None
        if not machine:
            return "local"
        if machine.get("transport_id"):
            from tunnel_manager import tunnel_status as _tunnel_status
            status = await _tunnel_status(machine["id"])
            if status and status.get("ssh_host"):
                return status["ssh_host"]
        alias = (machine.get("alias") or "").strip()
        if alias:
            return alias
        host = (machine.get("host") or "").strip()
        if host:
            return host
    except Exception:
        pass
    return "local"


async def _write_agent_name(session_id: str, prompt: str, chat_id: str, owner: str | None) -> None:
    """Generate and persist the human-readable agent name for *session_id*.

    Resolves the transport name, calls :func:`routes.naming.generate_name`,
    and writes the result into the session JSON so the session listing shows it.
    """
    try:
        transport_name = await _resolve_transport_name(chat_id, owner)
        name = _generate_agent_name(transport_name, prompt)
        db.write_claude_session_file(session_id, name, "")
    except Exception:
        pass  # Naming is non-critical — don't break the turn


def _transcript_mtimes_sync(session_ids: list[str]) -> dict[str, str]:
    """Last-write time of each session's transcript, as an ISO timestamp.

    Blocking: locating a transcript globs the projects directory, so callers
    run it off the event loop.
    """
    out: dict[str, str] = {}
    for session_id in session_ids:
        try:
            path = transcripts.transcript_path(session_id)
            if path is None:
                continue
            stamp = datetime.datetime.fromtimestamp(
                path.stat().st_mtime, tz=datetime.UTC
            )
        except (OSError, ValueError):
            continue
        out[session_id] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


async def _live_updated_at(chats: list[dict]) -> dict[str, str]:
    """chat_id -> the later of its stored time and its transcript's.

    A conversation linked to a terminal session only had its ``updated_at``
    advanced when the web UI touched it -- a turn sent here, or the sync that
    runs while it is the open chat. Work done in the terminal moved the
    transcript and nothing else, so the sidebar aged a conversation that was in
    active use, and went on ageing it for as long as the browser was looking
    elsewhere.

    Read rather than written: the listing reflects the transcript without a
    write per poll, and without disturbing ``position``, which is the user's
    own ordering.
    """
    linked = [c for c in chats if c.get("session_id")]
    if not linked:
        return {}
    mtimes = await asyncio.to_thread(
        _transcript_mtimes_sync, [c["session_id"] for c in linked]
    )
    live: dict[str, str] = {}
    for chat in linked:
        seen = mtimes.get(chat["session_id"])
        # Fixed-format UTC, so a lexicographic compare is a chronological one.
        if seen and seen > (chat.get("updated_at") or ""):
            live[chat["id"]] = seen
    return live


async def _busy_terminal_sessions() -> set[str]:
    """Session ids whose interactive terminal reports itself busy.

    A conversation routed to a live terminal has no LiveTurn -- the request was
    typed into a window and is out of our hands -- so `running` is false for it
    and the sidebar showed nothing at all while work was plainly happening.
    Claude Code writes a `status` field into ~/.claude/sessions/<pid>.json, which
    is the same signal the orchestrator already trusts, and "busy" is the only
    value observed. Reported separately from `running` rather than folded into
    it, because `running` also means "there is a buffer to attach to" and there
    is not one here.
    """
    try:
        sessions = await db.read_claude_sessions()
    except (OSError, ValueError):
        return set()
    return {
        item["sessionId"]
        for item in sessions
        if item.get("sessionId")
        and (item.get("status") or "").strip().lower() == "busy"
    }


async def handle_chats_list(request: Request):
    """GET /api/chats -- list chats scoped to owner."""
    session = request.state.session
    # Snapshotted first, and synchronously (an in-memory dict read, no await),
    # so the two awaits below cannot land in between it and the DB read they
    # used to follow it. They used to run first: chat_list's `updated_at` was
    # captured, then two awaits (this and queue_counts) gave a turn time to
    # finish and persist, then running_ids read *after* that already saw it
    # as no longer running -- one response reporting running=False with the
    # stale pre-turn updated_at still attached. The sidebar (chat-list.js)
    # reacts to the two independently: that combination reads as "finished"
    # for a poll or two, then "unread" once a later poll's updated_at catches
    # up -- one highlight visibly replaced by another within a few seconds.
    # Ordering this first cannot remove the race, only bite the safe side of
    # it: if a turn finishes during the awaits below, the worst case is now
    # running=True alongside an already-current updated_at, which every
    # consumer already treats as normal (still-running chats update their
    # timestamp too), not a state combination nothing was built to expect.
    running = turns.running_ids(session["user"])
    chats = await db.chat_list(session["user"])
    live_updated = await _live_updated_at(chats)
    queued = await db.queue_counts(session["user"])
    queued_held = await db.queue_held_counts(session["user"])
    busy_sessions = await _busy_terminal_sessions()
    last_models = await db.last_models_used(session["user"])
    return JSONResponse(
        {
            "chats": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "description": c["description"],
                    "work_dir": c["work_dir"],
                    "created_at": c["created_at"],
                    "updated_at": live_updated.get(c["id"], c["updated_at"]),
                    "archived": bool(c["archived"]),
                    "pinned": bool(c["pinned"]),
                    "pinned_at": c.get("pinned_at"),
                    "degraded": bool(c.get("degraded")),
                    "degraded_reason": c.get("degraded_reason"),
                    "session_id": c.get("session_id"),
                    "model": c.get("model") or "",
                    # What actually answered the last turn -- distinct from
                    # `model` above, which is the routing override and stays
                    # empty until a user sets one. This is what the sidebar's
                    # "Last model" line and tooltip should read.
                    "last_model_used": last_models.get(c["id"], ""),
                    # The sidebar populates the workspace pickers before the
                    # detail request lands, so the pin has to travel here too.
                    "ai_machine_id": c.get("ai_machine_id"),
                    "running": c["id"] in running,
                    "queued": queued.get(c["id"], 0),
                    # Of "queued", how many are held (their predecessor's turn
                    # failed, so they were not auto-sent). Split out so the
                    # sidebar badge can look different for "safely waiting"
                    # vs "needs a Send/Discard decision" -- summed together
                    # in "queued" above, a chat with 3 held prompts looked
                    # identical to one with 3 healthy ones.
                    "queued_held": queued_held.get(c["id"], 0),
                    # Work happening in a terminal this conversation is linked
                    # to. Draws the same dot; offers nothing to attach to.
                    "terminal_busy": bool(
                        c.get("session_id") and c["session_id"] in busy_sessions
                    ),
                    "voice_mode": bool(c.get("voice_mode")),
                    "parent_chat_id": c.get("parent_chat_id"),
                    "is_temporary": bool(c.get("is_temporary")),
                }
                for c in chats
            ],
        }
    )


async def handle_chat_create(request: Request):
    """POST /api/chats -- create a new chat with its project directory."""
    session = request.state.session
    data = await request.json()
    title = (data.get("title") or "Untitled").strip()[:200]

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
            "could_not_create_conversation: failed to create work_dir=%s "
            "(check permissions, disk space, and PROJECTS_ROOT=%s): %s",
            work_dir, config.PROJECTS_ROOT, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not create conversation directory — check server logs for details",
        )
    chat_id = uuid.uuid4().hex
    now = await db.chat_create(
        chat_id, title, data.get("description"), work_dir, session["user"]
    )
    _log.info("chat_created chat_id=%s work_dir=%s", chat_id, work_dir)
    voice_mode = bool(data.get("voice_mode"))
    parent_chat_id = data.get("parent_chat_id") or None
    # `is_temporary` in the request body is deliberately NOT read. Temporariness
    # is derived from having a parent -- see the `if parent_chat_id:` branch
    # below, which sets is_temporary=1 -- and that is the only way a chat
    # becomes temporary.
    #
    # It used to be read into a local that nothing then used, so the field read
    # as though it were honoured while being silently dropped: a client sending
    # `is_temporary: true` without a parent got an ordinary chat. Left
    # unhonoured rather than wired up, because a temporary chat is hidden from
    # the sidebar, and letting an unvalidated client flag hide chats is a
    # behaviour change rather than a bug fix. tests/test_qa_voice_parent_child.py
    # passes `is_temporary: True` and passes either way -- it also sends
    # parent_chat_id, which is what actually does the work.
    if voice_mode:
        voice_backend_id = (
            await db.setting_get("voice_backend_id")
            or await db.setting_get("voice_ai_machine_id")
            or config.VOICE_BACKEND_ID_DEFAULT
            or config.VOICE_AI_MACHINE_ID_DEFAULT
        )
        if voice_backend_id and not await db.ai_machine_get(voice_backend_id, session["user"]):
            raise HTTPException(status_code=404, detail="Voice backend not found")
        voice_model = await db.setting_get("voice_model") or config.VOICE_MODEL_DEFAULT
        await db.chat_update(
            chat_id, session["user"],
            voice_mode=1, model=voice_model, ai_machine_id=voice_backend_id,
            type="brainstorming",
        )
    # Temp voice chat with parent context
    if parent_chat_id:
        parent = await db.chat_get(parent_chat_id, session["user"])
        if not parent:
            raise HTTPException(status_code=404, detail="Parent chat not found")
        await db.chat_update(
            chat_id, session["user"],
            parent_chat_id=parent_chat_id,
            is_temporary=1,
        )
        # Prevent the parent's auto_answer from blocking voice turns.
        await db.chat_auto_answer_set(chat_id, session["user"], False, False)
        # Set title to match parent so the tooltip knows its source
        if title == "Untitled":
            title = (parent.get("title") or "Untitled")[:200]
            await db.chat_update(chat_id, session["user"], title=title)
    return JSONResponse(
        {"id": chat_id, "title": title, "work_dir": work_dir, "created_at": now}
    )


async def handle_chat_get(request: Request, chat_id: str):
    """GET /api/chats/{id} -- get chat metadata and transcript."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        _log.warning(
            "chat_not_found: user=%s chat_id=%s (chat may have been deleted)",
            session["user"], chat_id,
        )
        raise HTTPException(status_code=404, detail="Chat not found")

    # Paginated: a chat with thousands of turns used to send, and render,
    # every one of them on every open and every poll-driven refresh. `limit`
    # defaults to the newest 50; `before_id` (a message id, oldest one already
    # loaded) pages backward for "load more". `has_more` tells the client
    # whether that control has anything left to show.
    limit_raw = request.query_params.get("limit")
    limit = 50
    if limit_raw and limit_raw.isdigit():
        limit = min(max(int(limit_raw), 1), 200)
    before_id_raw = request.query_params.get("before_id")
    before_id = int(before_id_raw) if before_id_raw and before_id_raw.isdigit() else None
    messages, has_more = await db.messages_page(
        chat_id, limit=limit, before_id=before_id
    )
    return JSONResponse(
        {
            "chat": {
                **{
                    k: chat[k]
                    for k in (
                        "id",
                        "title",
                        "description",
                        "session_id",
                        "work_dir",
                        "archived",
                        "pinned",
                        "pinned_at",
                        "model",
                        # The conversation's pinned backend. Without it the
                        # workspace picker cannot show which backend this
                        # conversation is on when it is reopened.
                        "ai_machine_id",
                    )
                },
                # What actually answered the last turn -- distinct from
                # `model` above, the routing override, which stays empty
                # until a user pins one. Same field as the chat-list
                # response; without it here, the top-of-conversation model
                # label went back to hidden every time a chat was (re)opened,
                # since this is the endpoint that path actually reads from.
                "last_model_used": await db.last_model_used(chat_id, session["user"]),
                "archived": bool(chat["archived"]),
                "pinned": bool(chat["pinned"]),
                # Opening a conversation has to be able to tell whether a turn
                # is in flight, so the client knows to attach to /live rather
                # than showing a finished-looking conversation that is still
                # being written to. `seq` is where to attach from: the buffered
                # events are replayed only if the client asks for them.
                "running": turns.is_running(chat_id),
                "turn_seq": (turns.get(chat_id).seq if turns.get(chat_id) else 0),
                "queued": len(await db.queue_list(chat_id, session["user"])),
                # Voice conversations answer through a different turn path and
                # own the mic / live-conversation controls in the composer.
                # Without this the client could not tell one apart after a
                # reload, so every guard in the voice-* frontend modules read
                # undefined and the controls stayed hidden in the very
                # conversations they belong to.
                "voice_mode": bool(chat.get("voice_mode")),
                "degraded": bool(chat.get("degraded")),
                "degraded_reason": chat.get("degraded_reason"),
            },
            # `question` marks the rows that asked the user something, so the
            # conversation can show which ones are still owed an answer.
            #
            # It is on the message rather than the conversation because the
            # conversation-level signals cannot hold it. The question bar reads
            # the CLI transcript for an AskUserQuestion block, so a question
            # merely *written* in prose is invisible to it; and classify_chat
            # judges a conversation by its newest message, so an agent that asks
            # and then keeps working buries its own question and the highlight
            # goes out. A mark per message cannot be buried by later output.
            #
            # Decided here rather than in the browser on purpose: the same
            # judgement already backs the orchestrator panel's "?", and a second
            # copy of it in JavaScript would drift from this one.
            #
            # Assistant rows only. A user message ending in "?" is the user
            # asking Claude, which needs nothing from the user.
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                    "question": (
                        m["role"] == "assistant"
                        and _asks_a_question(m["content"] or "")
                    ),
                }
                for m in messages
            ],
            "has_more": has_more,
        }
    )


async def handle_chat_patch(request: Request, chat_id: str):
    """PATCH /api/chats/{id} -- rename, describe, archive, pin, or route.

    ``ai_machine_id`` pins the conversation to one backend and ``model`` pins
    its model, so two conversations can run on different backends and models at
    the same time. Either set to null clears the pin and the conversation
    follows the owner's active machine again.
    """
    session = request.state.session
    data = await request.json()

    allowed = {"title", "description", "archived", "pinned", "ai_machine_id", "model", "voice_mode"}
    if not data or not set(data).issubset(allowed):
        raise HTTPException(status_code=400, detail="No valid fields to update")

    fields = {}
    if "title" in data:
        if not isinstance(data["title"], str):
            raise HTTPException(status_code=400, detail="Title must be text")
        title = data["title"].strip()[:200]
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        fields["title"] = title
    if "description" in data:
        description = data["description"]
        if description is not None and not isinstance(description, str):
            raise HTTPException(
                status_code=400, detail="Description must be text or null"
            )
        fields["description"] = description[:500] if description is not None else None
    if "archived" in data:
        if not isinstance(data["archived"], bool):
            raise HTTPException(status_code=400, detail="Archived must be a boolean")
        fields["archived"] = int(data["archived"])
    if "pinned" in data:
        if not isinstance(data["pinned"], bool):
            raise HTTPException(status_code=400, detail="Pinned must be a boolean")
        fields["pinned"] = int(data["pinned"])
        fields["pinned_at"] = db._now() if data["pinned"] else None
    if "ai_machine_id" in data:
        machine_id = data["ai_machine_id"]
        if machine_id is not None and not isinstance(machine_id, str):
            raise HTTPException(
                status_code=400, detail="ai_machine_id must be text or null"
            )
        machine_id = (machine_id or "").strip() or None
        # Confirm the machine exists and belongs to this user, so a pin can
        # never route a conversation at somebody else's backend.
        if machine_id and not await db.ai_machine_get(machine_id, session["user"]):
            raise HTTPException(status_code=404, detail="Machine not found")
        fields["ai_machine_id"] = machine_id
    if "model" in data:
        model = data["model"]
        if model is not None and not isinstance(model, str):
            raise HTTPException(status_code=400, detail="Model must be text or null")
        model = (model or "").strip() or None
        if model and not _MODEL_RE.fullmatch(model):
            raise HTTPException(
                status_code=400, detail="Model name contains invalid characters"
            )
        fields["model"] = model
    if "voice_mode" in data:
        if not isinstance(data["voice_mode"], bool):
            raise HTTPException(status_code=400, detail="voice_mode must be a boolean")
        fields["voice_mode"] = int(data["voice_mode"])
        if data["voice_mode"]:
            # Enable voice: pin to the owner's configured voice machine and model.
            voice_machine_id = (
                await db.setting_get("voice_backend_id")
                or await db.setting_get("voice_ai_machine_id")
                or config.VOICE_BACKEND_ID_DEFAULT
                or config.VOICE_AI_MACHINE_ID_DEFAULT
            )
            if voice_machine_id and not await db.ai_machine_get(voice_machine_id, session["user"]):
                raise HTTPException(status_code=404, detail="Voice backend not found")
            voice_model = await db.setting_get("voice_model") or config.VOICE_MODEL_DEFAULT
            fields["ai_machine_id"] = voice_machine_id
            fields["model"] = voice_model
            # Disarm auto-answer on the way in. This is the ordering that
            # actually happened: the PUT handler's old gate stopped you arming
            # an already-voice chat, but nothing stopped you arming a normal
            # one and converting it, and the flag then rode along into a mode
            # where it does nothing. Two of seven voice chats reached that
            # state and could not take a single turn. Both halves of the knob,
            # or `accept_recommended` survives and re-arms if the chat is ever
            # switched back. Cleared only on the way in -- turning voice off
            # must not invent a value for a chat that was never armed.
            await db.chat_auto_answer_set(chat_id, session["user"], False, False)

    if "type" in data:
        if data["type"] not in ("normal", "brainstorming"):
            raise HTTPException(
                status_code=400, detail="type must be 'normal' or 'brainstorming'"
            )

    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Voice-mode chats must always be brainstorming.
    if data.get("type") == "normal" and chat.get("voice_mode"):
        raise HTTPException(
            status_code=400,
            detail="voice conversations must be brainstorming type",
        )

    # When voice_mode is turned on, force brainstorming.
    if data.get("voice_mode"):
        fields["type"] = "brainstorming"

    # When voice_mode is turned off on a brainstorming chat, allow downgrade.
    if data.get("voice_mode") is False and chat.get("type") == "brainstorming":
        fields.setdefault("type", "normal")

    updated = await db.chat_update(chat_id, session["user"], **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Return the full chat object so the caller can update the sidebar
    # row in-place without re-fetching the entire list or re-rendering the
    # conversation.  Only the fields the sidebar and workspace strips read
    # are included — anything heavier is not worth shipping here.
    return JSONResponse(
        {
            "ok": True,
            "chat": {
                "id": chat["id"],
                "title": chat.get("title", ""),
                "description": chat.get("description"),
                "voice_mode": bool(chat.get("voice_mode")),
                "pinned": bool(chat.get("pinned")),
                "archived": bool(chat.get("archived")),
                "session_id": chat.get("session_id"),
                "work_dir": chat.get("work_dir"),
                "ai_machine_id": chat.get("ai_machine_id"),
                "model": chat.get("model") or "",
            },
        }
    )


async def handle_voice_handoff(request: Request, chat_id: str):
    """POST /api/chats/{id}/voice/handoff -- summarize voice chat, append to parent, delete temp chat."""
    session = request.state.session
    result = await voice_handoff_fn(chat_id, session["user"])
    if result is None:
        raise HTTPException(status_code=400, detail="Could not generate handoff summary")
    return JSONResponse({"ok": True, "summary": result})


async def handle_chats_reorder(request: Request):
    """PUT /api/chats/order -- place conversations in an explicit order.

    Takes the whole ordered list rather than a position per conversation: one
    drag is one user action, and applying it as a cascade of individual updates
    could half-succeed and leave an order nobody chose.

    ``{"order": []}`` clears every placement and returns the list to recency.
    """
    session = request.state.session
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    order = data.get("order")
    if not isinstance(order, list):
        raise HTTPException(status_code=400, detail="order must be a list of ids")
    if len(order) > 500:
        raise HTTPException(status_code=400, detail="Too many conversations")
    if not all(isinstance(item, str) and item for item in order):
        raise HTTPException(status_code=400, detail="order must contain ids")

    if not order:
        cleared = await db.chats_clear_order(session["user"])
        _log.info("chat_order_cleared user=%s count=%d", session["user"], cleared)
        return JSONResponse({"ok": True, "placed": 0, "cleared": cleared})

    placed = await db.chats_reorder(session["user"], order)
    _log.info("chat_order_set user=%s placed=%d", session["user"], placed)
    return JSONResponse({"ok": True, "placed": placed})


async def handle_chat_delete(request: Request, chat_id: str):
    """DELETE /api/chats/{id} -- hard delete (never rm -rf)."""
    session = request.state.session
    deleted = await db.chat_delete(chat_id, session["user"])
    if not deleted:
        _log.warning(
            "chat_delete: user=%s chat_id=%s (not found or no permission)",
            session["user"], chat_id,
        )
        raise HTTPException(status_code=404, detail="Chat not found")
    _log.info("chat_deleted chat_id=%s", chat_id)
    return JSONResponse({"ok": True})


def render_chat_markdown(chat: dict, messages: list[dict]) -> str:
    """Render a chat transcript as a Markdown document."""
    created = chat.get("created_at") or "Unknown"
    try:
        created = datetime.datetime.fromisoformat(
            created.replace("Z", "+00:00")
        ).strftime("%-d %B %Y")
    except (AttributeError, ValueError):
        pass
    lines: list[str] = [
        f"# {chat['title']}",
        "",
        f"- Created: {created}",
        f"- Workspace: `{chat.get('work_dir') or 'Unknown'}`",
        f"- Session: `{chat.get('session_id') or 'None'}`",
        "",
    ]
    if chat.get("description"):
        lines.extend([f"> {chat['description']}", ""])
    labels = {"user": "User", "assistant": "Assistant", "system": "System"}
    for message in messages:
        role = labels.get(message.get("role"), "Message")
        lines.extend([f"## {role}", "", str(message.get("content") or ""), ""])
    return "\n".join(lines)


# Images a turn produced are worth seeing, and the message renderer is
# text-only, so they were unreachable from the web UI entirely. Serving them
# needs care: this is the first endpoint that reads a user-named file, in an
# application whose runner already launches Claude with
# --dangerously-skip-permissions. The boundary is therefore the narrowest one
# that still works -- a chat may read files inside its own work_dir and
# nowhere else, checked the same way runner.py checks a launch directory.
_CHAT_FILE_TYPES: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}
# Kept as a named alias for the image-specific size and route tests. The file
# boundary now serves PDFs as well, but the same containment and size rules apply.
_IMAGE_TYPES = _CHAT_FILE_TYPES


# Large enough for a screenshot, small enough that a stray path cannot stream
# a database file out through an <img> tag.
_IMAGE_MAX_BYTES: Final[int] = 12 * 1024 * 1024
_GENERATED_IMAGE_MAX: Final[int] = 12
_GENERATED_IMAGE_EXTENSIONS = frozenset(_CHAT_FILE_TYPES) - {".pdf"}
_GENERATED_IMAGE_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules"})


def _new_workspace_images(work_dir: str, since: float) -> list[str]:
    """Return newly written image paths in *work_dir*, oldest first.

    Walk only ordinary workspace directories. Hidden/dependency trees can be
    large and commonly contain unrelated assets, so they are not part of the
    generated-file boundary.
    """
    root = Path(work_dir)
    found: list[tuple[float, str]] = []
    try:
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = [
                name for name in dirs
                if not name.startswith(".") and name not in _GENERATED_IMAGE_SKIP_DIRS
            ]
            current_path = Path(current)
            for name in files:
                path = current_path / name
                if path.suffix.lower() not in _GENERATED_IMAGE_EXTENSIONS:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime <= since or not path.is_file():
                    continue
                found.append((stat.st_mtime, path.relative_to(root).as_posix()))
    except OSError:
        return []
    # Stable lexical ordering keeps the cap deterministic and matches the
    # relative-path order shown in the chat, while discovery remains limited to
    # files newer than the turn start.
    found.sort(key=lambda item: item[1])
    return [relative for _, relative in found[:_GENERATED_IMAGE_MAX]]


def _image_markdown(paths: list[str]) -> str:
    return "\n".join(f"![{Path(path).name}]({path})" for path in paths)


async def handle_chat_file(request: Request, chat_id: str):
    """GET /api/chats/{id}/file?path=... -- share an image or PDF in workspace."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    raw = (request.query_params.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="A path is required")

    root = Path(chat["work_dir"]).resolve()
    try:
        candidate = (root / raw).resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid path")
    # resolve() collapses "..", so this rejects traversal and symlinks that
    # point outside the workspace, which a bare prefix check would not.
    if not candidate.is_relative_to(root):
        _log.warning(
            "chat_file_outside_workspace: user=%s chat_id=%s path=%r",
            session["user"], chat_id, raw,
        )
        raise HTTPException(status_code=403, detail="Path is outside the workspace")

    media_type = _IMAGE_TYPES.get(candidate.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail="File type is not shared here")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if candidate.stat().st_size > _IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Shared file is too large to display")

    _log.info("chat_file_served chat_id=%s path=%s", chat_id, candidate.name)
    # PDFs are served as attachment (download) rather than inline: the workspace
    # is writable by a prompt-injected Claude, so a malicious PDF with JS or form
    # actions would be the lowest-effort exploit path. Inline PDF rendering gives
    # the browser a sandbox, but the browser's sandbox is only as good as the
    # reader — attachment is a belt-and-suspenders guarantee that no PDF code
    # ever executes in the page context.
    disposition = "attachment" if candidate.suffix.lower() == ".pdf" else "inline"
    return FileResponse(
        candidate,
        media_type=media_type,
        # nosniff is applied globally by SecurityMiddleware; the disposition
        # above is the extra layer for PDFs.
        headers={"Content-Disposition": f'{disposition}; filename="{candidate.name}"'},
    )


async def handle_chat_export(request: Request, chat_id: str):
    """GET /api/chats/{id}/export -- download chat as Markdown."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        _log.warning(
            "chat_export_not_found: user=%s chat_id=%s (deleted or no access)",
            session["user"], chat_id,
        )
        raise HTTPException(status_code=404, detail="Chat not found")
    messages = await db.messages_get(chat_id)
    body = render_chat_markdown(chat, messages)
    filename = f"{db.slug_from_title(chat['title']) or 'conversation'}.md"
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _record_turn_usage(chat_id: str, owner: str, frame: dict) -> None:
    """Persist a usage frame as one row per model.

    ``provider`` is resolved to one of three values and stored on the row,
    because the machine may later be edited or deleted:

    * ``anthropic`` -- the official API, where ``total_cost_usd`` is real.
    * ``anthropic-compatible`` -- an Anthropic-protocol gateway at a custom
      base_url (LiteLLM, a proxy, a self-hosted model). The machine's
      ``provider`` column says ``claude_code`` for these too, since that only
      describes the wire protocol, but the CLI still prices them with
      Anthropic's rates so the cost is not meaningful.
    * ``proxy`` -- a claude_proxy host.

    ``total_cost_usd`` covers the whole turn, not each model, so it is recorded
    against the first row only -- putting it on every row would multiply the
    reported spend by the number of models the turn touched.
    """
    # Both of these were bare returns. Between them they are every way the
    # Usage tab ends up empty while turns plainly succeed, so each says which
    # link of the chain gave out: the CLI's result frame -> claude_proxy's
    # usage_frame -> the runner -> here -> db.usage_record.
    if not frame:
        _log.warning(
            "usage_missing: chat_id=%s (no usage frame reached the handler; "
            "the CLI result frame carried none, or claude_proxy is running "
            "code older than its source and never emitted one)",
            chat_id,
        )
        await db.chat_mark_degraded(chat_id, "usage", "no frame reached the handler")
        return
    models = frame.get("models") or {}
    if not models:
        _log.warning(
            "usage_frame_has_no_models: chat_id=%s frame_keys=%s "
            "(a usage frame arrived but named no model, so nothing can be "
            "attributed)",
            chat_id, sorted(frame),
        )
        await db.chat_mark_degraded(chat_id, "usage", "frame carried no models")
        return
    try:
        machine = await db.ai_machine_active(owner)
    except Exception as exc:
        _log.warning(
            "usage_provider_unresolved: chat_id=%s (%s) — rows fall back to the "
            "default provider label", chat_id, exc,
        )
        machine = None
    provider = backend_kind(machine)
    cost = frame.get("cost_usd")
    any_written = False
    any_failed = False
    for index, (model, stats) in enumerate(models.items()):
        if not isinstance(stats, dict):
            continue
        row_id = await db.usage_record(
            chat_id,
            owner,
            model or "unknown",
            provider,
            input_tokens=stats.get("input_tokens", 0),
            output_tokens=stats.get("output_tokens", 0),
            cache_read_tokens=stats.get("cache_read_tokens", 0),
            cache_creation_tokens=stats.get("cache_creation_tokens", 0),
            cost_usd=cost if index == 0 else None,
            cost_basis=stats.get("cost_basis"),
            duration_ms=frame.get("duration_ms"),
            is_error=bool(frame.get("is_error")),
            # Stated, not inferred. Every web turn runs against a session-linked
            # conversation, so "has a session id" never distinguished the two.
            origin="web",
        )
        if row_id is None:
            any_failed = True
        else:
            any_written = True
    if any_failed:
        await db.chat_mark_degraded(chat_id, "usage", "usage_record returned no row id")
    elif any_written:
        await db.chat_clear_degraded(chat_id, "usage")
    _log.info(
        "usage_recorded chat_id=%s provider=%s models=%s",
        chat_id, provider, list(models),
    )


_SSE_TIMEOUT = turns.TIMEOUT_MESSAGE


_SSE_UNKNOWN = "Connection lost during streaming."


async def _route_to_live_terminal(chat: dict, prompt: str) -> dict | None:
    """Type *prompt* into the terminal running this chat's session, if any.

    A conversation linked to a live interactive session has two possible
    homes for a turn: a fresh `claude --resume` process, or the terminal the
    user is actually looking at. Spawning the second process does the work
    correctly and invisibly -- and leaves two processes appending to one
    transcript. Delivering to the live window instead means the request and
    every step of the answer appear where the user is watching, and the web
    conversation picks them up through the existing transcript sync.

    Returns None when there is no live window, so the caller falls back to the
    headless turn that has always run.
    """
    session_id = (chat.get("session_id") or "").strip()
    if not session_id:
        return None
    outcome = await asyncio.to_thread(prompts.deliver_request, session_id, prompt)
    if not outcome.get("delivered"):
        if outcome.get("target"):
            # A window was found but refused the input: worth a log, since
            # falling back silently would hide a broken multiplexer.
            _log.warning(
                "live delivery failed chat=%s session=%s reason=%s",
                chat.get("id"), session_id, outcome.get("reason"),
            )
        return None
    _log.info(
        "prompt delivered to live terminal chat=%s session=%s target=%s",
        chat.get("id"), session_id, (outcome.get("target") or {}).get("kind"),
    )
    return outcome


async def _mark_routed(chat: dict, owner: str, prompt: str) -> None:
    """Record that a website request was typed into this chat's terminal.

    Without this the turns that follow are imported as the terminal's own work
    and the person who asked disappears from the record -- which is exactly what
    "I asked in the web and it is counted as terminal" describes. The mark is the
    transcript's length at the moment of typing, so everything appended after it
    can be attributed back to the request that caused it.
    """
    session_id = (chat.get("session_id") or "").strip()
    if not session_id:
        return
    offset = await asyncio.to_thread(transcripts.transcript_size, session_id)
    await db.routed_request_add(session_id, chat["id"], owner, offset, prompt)


async def handle_submit_message(request: Request, chat_id: str):
    """POST /api/chats/{id}/messages -- submit a prompt, return the assistant's response."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        _log.warning(
            "submit_message: chat not found user=%s chat_id=%s "
            "(may have been deleted) — cannot send prompt",
            session["user"], chat_id,
        )
        raise HTTPException(status_code=404, detail="Chat not found")

    data = await request.json()
    prompt = (data.get("content") or "").strip()
    if not prompt:
        _log.warning("submit_message: empty prompt from user=%s chat_id=%s", session["user"], chat_id)
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    if len(prompt) > config.PROMPT_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Prompt is too long")
    model = data.get("model")
    if model is not None:
        if not isinstance(model, str) or len(model) > 100:
            raise HTTPException(status_code=400, detail="Model name is invalid")
        model = model.strip() or None
        if model and not _MODEL_RE.fullmatch(model):
            raise HTTPException(
                status_code=400, detail="Model name contains invalid characters"
            )

    # A conversation with a live terminal behind it gets the request typed
    # into that terminal, so the user sees it and its steps where they are
    # looking. The reply arrives in the web chat through transcript sync.
    routed = await _route_to_live_terminal(chat, prompt)
    if routed:
        session_id = chat.get("session_id")
        await _mark_routed(chat, session["user"], prompt)
        await db.messages_batch(chat_id, [("user", prompt)])
        # Same as the streaming path: the turn is also on disk, so advance
        # the sync offset to avoid a second import on the next poll.
        if session_id:
            await _skip_transcript_to_end(chat_id, session_id)
        return JSONResponse({
            "response": "",
            "delivered_to": "terminal",
            "session_id": chat.get("session_id"),
        })

    # Deliberately NOT routed through turns.py. This endpoint is synchronous by
    # contract -- it answers with the whole reply -- and the web UI does not use
    # it; the browser talks to /stream. So it keeps its own blocking runner call
    # rather than gaining a background turn it would only ever wait for.
    #
    # The consequence, stated because it is a real asymmetry: a turn started
    # here is invisible to turns.py, so it neither shows in the sidebar nor
    # queues a concurrent /stream request behind it. That is unchanged from
    # before background turns existed -- the two endpoints never coordinated --
    # but it is now the only place where they differ.
    try:
        await _prepare_transcript_for_backend(chat)
        chunks, session_id = await runner.run_turn(
            prompt,
            chat["session_id"],
            chat["work_dir"],
            chat_id,
            model,
        )
    except runner.TurnError as e:
        # Label the failure for what it is -- this previously reused
        # "could_not_create_conversation", the workspace-creation label, so log
        # searches (and a test asserting that string) matched the wrong event.
        _log.error(
            "turn_failed: chat_id=%s prompt_chars=%d work_dir=%s error=%s",
            chat_id, len(prompt), chat["work_dir"], e,
        )
        # Mask the detail, matching the SSE path -- the streaming branch already
        # refuses to hand raw exception text to the client.
        return JSONResponse(
            status_code=500, content={"error": _SSE_INTERNAL, "fatal": e.fatal}
        )

    full_response = "".join(chunks) if chunks else ""
    await db.messages_batch(
        chat_id,
        [
            ("user", prompt),
            ("assistant", full_response),
        ],
    )
    await db.bump_chat_updated_at(chat_id)
    if session_id and session_id != chat["session_id"]:
        await db.chat_set_session(chat_id, session_id)
    # Human-readable agent name — resolved from the transport/agent that handled this turn.
    if session_id:
        await _write_agent_name(session_id, prompt, chat_id, session.get("user"))
    # This turn was just stored above and the runner also appended it to the
    # CLI transcript, so step the sync past it or the next poll shows it twice.
    if session_id or chat.get("session_id"):
        await _skip_transcript_to_end(chat_id, session_id or chat["session_id"])
    model = runner.take_last_model(chat_id)
    # Deliberately not written back to the conversation. `chats.model` is the
    # user's choice, and `runner.get_default_model` reads it before the backend
    # default and the global setting -- so recording whatever happened to serve
    # a turn pinned the conversation to a model nobody picked, and it then
    # ignored the active machine for ever. Three conversations on this machine
    # are still pinned to `azure_ai/gpt-5.6-luna`, which no configured backend
    # serves, by exactly this route.
    #
    # Nothing is lost by not storing it: the served model is recorded per turn
    # in `usage_events`, it is returned in the response below, and the UI has
    # its own label for it that is not the picker.
    await _record_turn_usage(chat_id, session["user"], runner.take_last_usage(chat_id))
    # Attempts a retry discarded still spent real tokens (CLAUDE.md rule 5:
    # record failures too), and take_last_usage above only carries the kept
    # attempt.
    for frame in runner.take_retried_usage(chat_id):
        await _record_turn_usage(chat_id, session["user"], frame)
    return JSONResponse(
        {"response": full_response, "chunks": len(chunks), "model": model}
    )


async def _prepare_transcript_for_backend(chat: dict) -> None:
    """Make a conversation replayable before it runs on a strict backend.

    A gateway that streams its reply can record assistant messages whose only
    content is an empty text block. It replays those happily; the Anthropic API
    rejects the entire request with "text content blocks must be non-empty", so
    a conversation started on such a gateway fails the moment it is moved to
    Anthropic -- before the new prompt is even considered.

    Only Anthropic backends need this, and the check is a byte scan that finds
    nothing on a healthy transcript, so the common path stays cheap.
    """
    session_id = chat.get("session_id")
    if not session_id:
        return
    try:
        backend = await runner.get_backend(chat["id"])
    except Exception:
        return
    if backend.get("provider") != "claude_code":
        return
    try:
        result = await transcripts.repair_if_needed(session_id)
    except OSError as exc:
        _log.warning(
            "transcript_repair_failed session_id=%s: %s — the turn may be "
            "rejected by the API", session_id, exc,
        )
        return
    if result["repaired"]:
        _log.info(
            "transcript_repaired session_id=%s removed=%d relinked=%d backup=%s",
            session_id, result["removed"], result["relinked"], result["backup"],
        )


async def _start_turn(
    chat: dict, owner: str, prompt: str, model: str | None
) -> turns.LiveTurn:
    """Begin a background turn for *chat* and persist whatever it produces.

    Everything the old inline loop did when ``done`` arrived has to happen in
    here instead, because by then there may be no client left to do it for. That
    is the point: a turn that finishes while nobody is watching must still be
    stored, and one that fails must still leave a trace.
    """
    chat_id = chat["id"]

    turn_started_at = time.time()

    async def produce():
        if runner.slots_busy():
            # Otherwise waiting for a slot is indistinguishable from a slow
            # model: the conversation shows as running with nothing arriving.
            yield {
                "type": "status",
                "status": "waiting_for_slot",
                "error": f"Waiting for a free slot — {config.MAX_CONCURRENT} "
                         f"turns are already running.",
            }
        # Memory is the other reason a turn cannot start, and unlike a busy
        # slot it does not clear on its own. Reported the same way rather than
        # raised: the conversation stays intact and the user is told what is
        # holding the memory, which is a thing they can act on.
        refusal = runner.memory_refusal()
        if refusal:
            _log.warning("turn_refused_low_memory chat_id=%s", chat_id)
            yield {
                "type": "status",
                "status": "low_memory",
                "error": refusal,
            }
            yield {"type": "done"}
            return
        # Inside the task, not before it: a transcript repair on a large
        # conversation would otherwise delay the HTTP response.
        await _prepare_transcript_for_backend(chat)
        async for event in runner.stream_turn(
            prompt, chat["session_id"], chat["work_dir"], chat_id, model
        ):
            yield event
        # Attempts a retry discarded still spent real tokens against the
        # backend (CLAUDE.md rule 5: record failures too). They never arrived
        # as a live 'usage' event -- see runner.stream_turn -- so on_event
        # above never saw them; drain them here instead.
        for frame in runner.take_retried_usage(chat_id):
            await _record_turn_usage(chat_id, owner, frame)

    async def on_event(event: dict) -> None:
        if event.get("type") == "usage":
            # Recorded as it arrives: the tokens were spent whether or not the
            # rest of the turn completes, and whether or not anyone is watching.
            await _record_turn_usage(chat_id, owner, event)

    async def finish(
        *,
        parts: list[str],
        session_id: str | None,
        model: str,
        failed: bool,
        cancelled: bool = False,
    ) -> None:
        if cancelled:
            # Stopped on purpose. The tokens are already billed and the partial
            # answer is on the user's screen, so it is stored -- discarding it
            # left exactly the "paid for it, nothing to show" state this whole
            # change exists to remove. With no text there is nothing worth
            # keeping, and the client puts the prompt back in the composer, so
            # storing it would duplicate the moment they send again.
            partial = "".join(parts)
            if partial.strip():
                await db.messages_batch(
                    chat_id, [("user", prompt), ("assistant", partial)]
                )
                await db.bump_chat_updated_at(chat_id)
            return
        if failed:
            # Nothing is stored for a failed turn, matching the inline loop this
            # replaced. Storing the prompt looked like an improvement -- a
            # background failure leaves no trace for a user who walked away --
            # but Retry re-sends the same prompt, so it lands twice. The
            # conversation must not gain a phantom message for every failure
            # the user retried past.
            #
            # Reaffirmed 2026-09-10 after a review called this an inconsistency
            # with the `cancelled` branch above and changed it. It is not an
            # inconsistency, it is a different trade, and two tests in
            # test_app.py state it: test_failed_retry_does_not_duplicate_user_prompt
            # runs a failing attempt, retries, and asserts the conversation
            # reads exactly [("user", prompt), ("assistant", answer)] -- one
            # prompt, one answer. Keeping the partial makes that read
            # prompt/partial/prompt/answer, which is a duplicated prompt in the
            # foreground case to preserve a partial in the background one.
            # test_incomplete_stream_does_not_persist_partial_turn pins the
            # same rule for a stream that simply stops.
            #
            # The background gap is real and narrower than it looked: a user who
            # walked away loses the partial *and* has no Retry button, because
            # `retry()` sends `lastAttempt.content` from in-memory browser state
            # for the currently-open chat only. Worth solving, but not by
            # writing the pair -- that trades a clean transcript for everyone to
            # help the one case.
            return
        assistant = "".join(parts)
        images = await asyncio.to_thread(
            _new_workspace_images, chat["work_dir"], turn_started_at
        )
        if images:
            image_text = _image_markdown(images)
            assistant = f"{assistant}\n\n{image_text}" if assistant.strip() else image_text
        if assistant.strip():
            await db.messages_batch(
                chat_id, [("user", prompt), ("assistant", assistant)]
            )
        await db.bump_chat_updated_at(chat_id)
        if session_id and session_id != chat["session_id"]:
            await db.chat_set_session(chat_id, session_id)
        # Human-readable agent name — resolved from the transport/agent that handled this turn.
        if session_id:
            await _write_agent_name(session_id, prompt, chat_id, owner)
        # The served model is not written back here either -- see the blocking
        # path above. `chats.model` means "the user chose this", and routing
        # reads it before everything else.
        # Same reason as the blocking path: the runner appended this turn to the
        # CLI transcript too, so move the sync past it rather than letting the
        # next poll echo it back.
        linked = session_id or chat.get("session_id")
        if linked:
            await _skip_transcript_to_end(chat_id, linked)

    return turns.start(
        chat_id, owner, prompt, model, produce=produce, finish=finish,
        on_event=on_event,
    )


async def _launch_queued(
    chat_id: str, owner: str, prompt: str, model: str | None
) -> None:
    """Start a prompt that was waiting behind a turn.

    Registered on ``turns.launcher`` so the queue can drain from inside
    ``turns.py`` without it importing this module, which would be a cycle.
    """
    chat = await db.chat_get(chat_id, owner)
    if not chat:
        _log.warning(
            "queued_prompt_dropped chat_id=%s owner=%s (conversation is gone)",
            chat_id, owner,
        )
        return
    await _start_turn(chat, owner, prompt, model)


async def stream_handler(request: Request, chat_id: str):
    """POST /api/chats/{id}/stream -- SSE token stream with prompt in body.

    Reads ``request.state.session``, the same as every other handler in this
    file. This used to open its own cookie check instead -- a second,
    unmaintained auth path that quietly dropped the API-token login
    AuthMiddleware also supports (via ``_session_from_api_token``), so a
    caller authenticated with a bearer token could use every other endpoint
    but not this one. It also meant a change to how auth works would need to
    be made twice to actually apply everywhere.
    """
    session = request.state.session
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")

    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        _log.error(
            "stream_handler: chat not found user=%s chat_id=%s "
            "(may have been deleted, broken session reference) — "
            "reload the page or create a new conversation",
            session["user"], chat_id,
        )
        raise HTTPException(status_code=404, detail="Chat not found")

    data = await request.json()
    prompt = (data.get("content") or "").strip()
    if not prompt:
        _log.warning(
            "stream_handler: empty prompt from user=%s chat_id=%s voice_mode=%s",
            session["user"], chat_id, bool(chat.get("voice_mode")),
        )
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    if len(prompt) > config.PROMPT_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Prompt is too long")

    if chat.get("voice_mode"):
        # A stored auto_answer used to be rejected here. It made the chat
        # unusable rather than safe: arming the knob on a normal chat and then
        # switching it to voice was reachable, the flag was never cleared, and
        # every turn afterwards died on a 400 -- with the GET handler reporting
        # `enabled: False`, so the knob rendered as off while being the thing
        # blocking you. Two of seven voice chats were in that state.
        #
        # The flag is inert on this path, not dangerous: auto_answer answers
        # Claude Code CLI permission prompts, read via prompts.read_prompt and
        # the CLI's transcript. A voice turn never spawns the CLI (see
        # routes/voice.py, which drives an OpenAI-compatible client directly),
        # so there is no prompt for it to answer. Ignoring it is correct;
        # refusing the turn was not.
        #
        # Same cap every other stream respects (checked before the
        # StreamingResponse is built, for the same reason the non-voice path
        # below does it here: a rejection has to raise before the response
        # commits to a 200, or it reaches the client as a dead stream instead
        # of the 429 it should be).
        acquire_sse_slot(session["user"])

        async def voice_event_generator():
            try:
                async for frame in stream_voice_turn(chat, prompt, session["user"]):
                    yield frame
            finally:
                release_sse_slot(session["user"])

        return StreamingResponse(
            voice_event_generator(), media_type="text/event-stream"
        )

    model = data.get("model")
    if model is not None:
        if (
            not isinstance(model, str)
            or len(model) > 100
            or (model.strip() and not _MODEL_RE.fullmatch(model.strip()))
        ):
            raise HTTPException(
                status_code=400, detail="Model name contains invalid characters"
            )
        model = model.strip() or None

    # Checked (and reserved) here, before the StreamingResponse is built: the
    # response commits to a 200 status the moment its generator first yields,
    # so an over-cap rejection has to raise before that point to reach the
    # client as the 429 it is, rather than as a stream that opens then dies.
    acquire_sse_slot(session["user"])

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'chat_id': chat_id})}\n\n"

            # If a live terminal is running this conversation, the request belongs
            # there: the user watches it and every step of the answer in the window
            # they already have open, instead of a second headless process doing the
            # work invisibly against the same transcript. The reply reaches this
            # page through the existing transcript sync.
            routed = await _route_to_live_terminal(chat, prompt)
            if routed:
                session_id = chat.get("session_id")
                await _mark_routed(chat, session["user"], prompt)
                await db.messages_batch(chat_id, [("user", prompt)])
                # This turn was just stored in messages and is also written to the
                # CLI transcript, so advance the sync past it or the next poll
                # would import the same turn again.
                if session_id:
                    await _skip_transcript_to_end(chat_id, session_id)
                # Sent as `text`, which the client already renders: inventing a
                # new event type would have shown the user nothing at all, since
                # conversation.js ignores types it does not know.
                yield (
                    "data: "
                    + json.dumps({
                        "type": "text",
                        "content": "Sent to the terminal session running this "
                                   "conversation — the request and its steps appear "
                                   "there, and sync back here when the turn ends.",
                    })
                    + "\n\n"
                )
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            try:
                # The turn is started as a background task and then followed, so
                # this response is a viewer rather than the turn's owner. Closing it
                # -- by switching conversations, reloading, or locking a phone --
                # no longer shortens the turn or discards its answer.
                try:
                    await _start_turn(chat, session["user"], prompt, model)
                except turns.AlreadyRunning:
                    position = await db.queue_add(
                        chat_id, session["user"], prompt, model
                    )
                    if position:
                        yield (
                            "data: "
                            + json.dumps({"type": "queued", "position": position})
                            + "\n\n"
                        )
                    else:
                        yield (
                            "data: "
                            + json.dumps({
                                "type": "error",
                                "error": f"This conversation already has "
                                         f"{db.QUEUE_MAX} prompts waiting.",
                            })
                            + "\n\n"
                        )
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                async for event in turns.follow(chat_id):
                    if event.get("type") == "keepalive":
                        # Comment frame: keeps an intermediary from dropping a
                        # stream that is legitimately waiting on a slow model.
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
                    await asyncio.sleep(0)

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _log.exception("stream_handler timeout on %s", chat_id)
                yield f"data: {json.dumps({'type': 'error', 'error': _SSE_TIMEOUT})}\n\n"
            except (OSError, asyncio.IncompleteReadError):
                _log.exception("stream_handler I/O error on %s", chat_id)
                yield f"data: {json.dumps({'type': 'error', 'error': _SSE_UNKNOWN})}\n\n"
            except Exception:
                _log.exception("stream_handler unexpected error on %s", chat_id)
                yield f"data: {json.dumps({'type': 'error', 'error': _SSE_INTERNAL})}\n\n"
        finally:
            release_sse_slot(session["user"])

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_chat_live(request: Request, chat_id: str):
    """GET /api/chats/{id}/live?since=N -- attach to a turn already running.

    This is what makes leaving harmless. A client that switched away, reloaded,
    or had its tab suspended reconnects here, is replayed everything it missed
    from *since*, and then follows the tail. ``since`` is the last ``seq`` the
    client saw, so a reattach costs only the gap rather than the conversation.
    """
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    try:
        since = max(0, int(request.query_params.get("since", 0)))
    except (TypeError, ValueError):
        since = 0

    turn = turns.get(chat_id)
    if turn is None or turn.owner != session["user"]:
        # No live turn, and no error either: "nothing is running" is a normal
        # answer to this question, and the client uses it to settle its UI.
        return JSONResponse({"running": False, "state": "idle"})

    acquire_sse_slot(session["user"])

    async def event_generator():
        try:
            yield (
                "data: "
                + json.dumps({
                    "type": "start",
                    "chat_id": chat_id,
                    "since": since,
                    "state": turn.state,
                })
                + "\n\n"
            )
            try:
                async for event in turns.follow(chat_id, since):
                    if await request.is_disconnected():
                        return
                    if event.get("type") == "keepalive":
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("live stream failed chat_id=%s", chat_id)
                yield f"data: {json.dumps({'type': 'error', 'error': _SSE_INTERNAL})}\n\n"
        finally:
            release_sse_slot(session["user"])

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_turn_stop(request: Request, chat_id: str):
    """POST /api/chats/{id}/stop -- cancel the running turn.

    Needed because closing the stream no longer stops anything. Stop used to be
    implicit -- the browser aborted its reader and the turn died with it -- so
    once a turn outlives its viewer, an explicit request is the only way to
    distinguish "I am leaving" from "stop working".
    """
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    turn = turns.get(chat_id)
    if turn is not None and turn.owner != session["user"]:
        raise HTTPException(status_code=404, detail="Chat not found")
    stopped = await turns.cancel(chat_id)
    # A stop also abandons what was queued behind it: the user is not asking to
    # move on to the next prompt, they are asking to stop.
    held = await db.queue_hold_all(chat_id)
    if stopped:
        _log.info(
            "turn_stopped chat_id=%s user=%s held=%d",
            chat_id, session["user"], held,
        )
    return JSONResponse({"ok": True, "stopped": stopped, "held": held})


async def handle_queue_list(request: Request, chat_id: str):
    """GET /api/chats/{id}/queue -- prompts waiting behind the running turn."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return JSONResponse({
        "queue": await db.queue_list(chat_id, session["user"]),
        "max": db.QUEUE_MAX,
        "running": turns.is_running(chat_id),
    })


async def handle_queue_delete(request: Request, chat_id: str, queue_id: int):
    """DELETE /api/chats/{id}/queue/{queue_id} -- discard a queued prompt."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not await db.queue_delete(queue_id, session["user"]):
        raise HTTPException(status_code=404, detail="Queued prompt not found")
    _log.info(
        "queue_discarded chat_id=%s queue_id=%s user=%s",
        chat_id, queue_id, session["user"],
    )
    return JSONResponse({"ok": True})


async def handle_queue_release(request: Request, chat_id: str, queue_id: int):
    """POST /api/chats/{id}/queue/{queue_id}/release -- send a held prompt now.

    A prompt is held when the turn in front of it failed, so releasing it is the
    user deciding to go ahead anyway. If nothing is running it starts
    immediately; otherwise it returns to the queue and drains normally.
    """
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not await db.queue_release(queue_id, session["user"]):
        raise HTTPException(status_code=404, detail="Queued prompt not found")
    if turns.is_running(chat_id):
        return JSONResponse({"ok": True, "started": False})
    row = await db.queue_next(chat_id)
    if row is None:
        return JSONResponse({"ok": True, "started": False})
    await db.queue_delete(row["id"], session["user"])
    await _start_turn(chat, session["user"], row["prompt"], row["model"])
    return JSONResponse({"ok": True, "started": True})


@router.get("/api/chats")
async def _api_chats_list(request: Request):
    return await handle_chats_list(request)


@router.post("/api/chats")
async def _api_chat_create(request: Request):
    return await handle_chat_create(request)


@router.post("/api/chats/{chat_id}/voice/handoff")
async def _api_voice_handoff(request: Request, chat_id: str):
    return await handle_voice_handoff(request, chat_id)


# Registered before /api/chats/{chat_id} so "order" is never captured as an id.
@router.put("/api/chats/order")
async def _api_chats_reorder(request: Request):
    return await handle_chats_reorder(request)


@router.get("/api/chats/{chat_id}")
async def _api_chat_get(request: Request, chat_id: str):
    return await handle_chat_get(request, chat_id)


@router.patch("/api/chats/{chat_id}")
async def _api_chat_patch(request: Request, chat_id: str):
    return await handle_chat_patch(request, chat_id)


@router.delete("/api/chats/{chat_id}")
async def _api_chat_delete(request: Request, chat_id: str):
    return await handle_chat_delete(request, chat_id)


@router.get("/api/chats/{chat_id}/export")
async def _api_chat_export(request: Request, chat_id: str):
    return await handle_chat_export(request, chat_id)


@router.post("/api/chats/{chat_id}/messages")
async def _api_submit_message(request: Request, chat_id: str):
    return await handle_submit_message(request, chat_id)


@router.get("/api/chats/{chat_id}/file")
async def _api_chat_file(request: Request, chat_id: str):
    return await handle_chat_file(request, chat_id)


@router.get("/api/chats/{chat_id}/question")
async def _api_chat_question_get(request: Request, chat_id: str):
    return await handle_chat_question_get(request)


@router.post("/api/chats/{chat_id}/question")
async def _api_chat_question_answer(request: Request, chat_id: str):
    return await handle_chat_question_answer(request)


@router.delete("/api/chats/{chat_id}/question")
async def _api_chat_question_dismiss(request: Request, chat_id: str):
    return await handle_chat_question_dismiss(request)


@router.put("/api/chats/{chat_id}/auto-answer")
async def _api_chat_auto_answer_set(request: Request, chat_id: str):
    return await handle_chat_auto_answer_set(request, chat_id)


@router.get("/api/chats/{chat_id}/auto-answer")
async def _api_chat_auto_answer_get(request: Request, chat_id: str):
    return await handle_chat_auto_answer_get(request, chat_id)


@router.post("/api/chats/{chat_id}/agent-reply")
async def _api_chat_agent_reply(request: Request, chat_id: str):
    """Reply to an agent that messaged this session.

    Writes a <cross-session-message> record into the target session's
    transcript and fires a wake-up turn through the proxy so the CLI's
    MCP picks it up promptly. The target may be local (today's path) or
    live behind an ssh_transports connection -- see
    docs/superpowers/specs/2026-09-08-transport-aware-agent-reply-design.md.
    """
    session = request.state.session
    owner = session["user"]
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > config.PROMPT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"text too long ({len(text)} chars, max {config.PROMPT_MAX_CHARS})",
        )
    target = str(body.get("to") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="to is required")

    chat = await db.chat_get(chat_id, owner)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if not await db.agent_reply_cooldown_check(chat_id, target):
        raise HTTPException(
            status_code=429,
            detail="Already replied to this target recently — try again shortly",
        )

    # 1. Try local resolution first (today's path, unchanged).
    result = await asyncio.to_thread(transcripts.agent_reply_to, target, text)
    via = "local"
    # Mirrors get_proxy_target's own local fallback (get_proxy_host() + the
    # global port), not a bare config constant, so a deployment that
    # overrides ai_machine_host still gets that override here too.
    proxy_target: tuple[str, int] | None = (await runner.get_proxy_host(), config.PROXY_PORT)

    # 2. Not found locally -- try live transports, one at a time, stopping at
    #    the first success. Never opens a fresh SSH connection just to
    #    check whether a name exists over there (only tunnel_up=1 transports
    #    are candidates).
    if not result.get("ok"):
        proxy_target = None
        for candidate in await _find_live_transports(owner):
            cmd = transcripts.build_remote_reply_command(
                candidate["remote_path"], target, text)
            try:
                remote_result = await _remote_agent_reply(candidate["machine_id"], cmd)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "agent-reply remote exec failed transport=%s: %s",
                    candidate["transport_id"], exc,
                )
                continue
            if remote_result.get("ok"):
                result = remote_result
                via = candidate["transport_id"]
                proxy_target = ("127.0.0.1", candidate["local_port"])
                break
            # Keep the most recent remote failure reason if every candidate
            # (and the local attempt) comes up empty.
            result = remote_result

    await db.agent_reply_log_add(
        chat_id, owner, target, via, bool(result.get("ok")),
        result.get("reason", ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "unknown error"))

    # 3. Fire a wake-up turn through the proxy so the CLI's MCP processes the
    #    pending cross-session message queue. proxy_target addresses the
    #    session's own host directly -- never inferred from this chat's own
    #    backend pin, which is unrelated to where the target actually lives.
    target_session_id = result.get("session_id")
    if target_session_id and proxy_target and config.PROXY_ENABLED:
        try:
            await _fire_wake_up(
                target_session_id, chat["work_dir"], chat_id, owner, proxy_target,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "wake-up failed for agent-reply to %s: %s",
                target, exc,
            )

    return JSONResponse({
        "ok": True, "path": result.get("path", ""), "to": target, "via": via,
    })


async def _find_live_transports(owner: str) -> list[dict[str, Any]]:
    """Live (tunnel_up=1) transports for *owner*, one entry per transport.

    Resolving an agent-reply target never opens a fresh SSH connection just
    to probe whether a name exists there -- only transports whose tunnel is
    already up are candidates. Several machines can share one transport_id
    (tunnel_manager reuses the connection); each transport is tried once.
    """
    from tunnel_manager import tunnel_status

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for machine in await db.ai_machines_list(owner):
        transport_id = machine.get("transport_id")
        if not transport_id or transport_id in seen:
            continue
        status = await tunnel_status(machine["id"])
        if not status or not status.get("tunnel_up") or not status.get("local_port"):
            continue
        transport = await db.ssh_transport_get(transport_id, owner)
        if not transport:
            continue
        seen.add(transport_id)
        out.append({
            "machine_id": machine["id"],
            "transport_id": transport_id,
            "remote_path": transport.get("remote_path") or "~/wc-proxy",
            "local_port": int(status["local_port"]),
        })
    return out


async def _remote_agent_reply(
    machine_id: str, cmd: str, timeout: float = 15.0,
) -> dict[str, Any]:
    """Run *cmd* (built by transcripts.build_remote_reply_command) over the
    machine's live SSH tunnel and parse its JSON stdout.

    Remote output is data, never instruction: parsed strictly with
    json.loads, never eval/exec'd, and any unexpected shape becomes an
    error result rather than a crash.
    """
    from tunnel_manager_ssh import exec_command

    try:
        _, stdout, stderr = await asyncio.wait_for(
            exec_command(machine_id, cmd, timeout=int(timeout)), timeout=timeout,
        )
    except (RuntimeError, OSError, asyncio.TimeoutError) as exc:
        return {"ok": False, "reason": f"exec failed: {exc}"}

    def _read() -> tuple[bytes, bytes]:
        return stdout.read(), stderr.read()

    try:
        out_bytes, err_bytes = await asyncio.wait_for(
            asyncio.to_thread(_read), timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "remote command timed out"}

    out = out_bytes.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        err = err_bytes.decode("utf-8", errors="replace").strip()
        return {"ok": False, "reason": f"unexpected remote output: {(out or err or 'empty')[:200]}"}
    if not isinstance(parsed, dict):
        return {"ok": False, "reason": "unexpected remote output shape"}
    return parsed


async def _fire_wake_up(
    session_id: str,
    work_dir: str,
    chat_id: str,
    owner: str | None,
    proxy_target: tuple[str, int],
) -> None:
    """Send a minimal turn through the proxy to wake the target session.

    The CLI's MCP reads cross-session message queues on every transcript
    append. A fresh turn forces a new read, which surfaces pending
    <cross-session-message> records. proxy_target is always the resolved
    target session's own host -- never this chat's backend pin.
    """
    await runner._proxy_turn(
        " ",  # minimal whitespace-only prompt
        session_id,
        work_dir,
        chat_id,
        None,  # model — falls back to default
        owner,
        proxy_target=proxy_target,
    )


@router.get("/api/chats/{chat_id}/live")
async def _api_chat_live(request: Request, chat_id: str):
    return await handle_chat_live(request, chat_id)


@router.post("/api/chats/{chat_id}/stop")
async def _api_chat_stop(request: Request, chat_id: str):
    return await handle_turn_stop(request, chat_id)


@router.get("/api/chats/{chat_id}/queue")
async def _api_chat_queue_list(request: Request, chat_id: str):
    return await handle_queue_list(request, chat_id)


@router.delete("/api/chats/{chat_id}/queue/{queue_id}")
async def _api_chat_queue_delete(request: Request, chat_id: str, queue_id: int):
    return await handle_queue_delete(request, chat_id, queue_id)


@router.post("/api/chats/{chat_id}/queue/{queue_id}/release")
async def _api_chat_queue_release(request: Request, chat_id: str, queue_id: int):
    return await handle_queue_release(request, chat_id, queue_id)


# Registered ahead of /api/chats/{chat_id}/... so the literal wins. FastAPI
# matches in order, and "sync" would otherwise be a plausible chat_id.
@router.post("/api/chats/sync")
async def _api_chats_sync_all(request: Request):
    return await handle_chats_sync_all(request)


@router.post("/api/chats/{chat_id}/sync")
async def _api_chat_sync(request: Request, chat_id: str):
    return await handle_chat_sync(request, chat_id)


@router.post("/api/chats/{chat_id}/stream")
async def _api_stream(request: Request, chat_id: str):
    return await stream_handler(request, chat_id)


@router.post("/api/chats/search")
async def _api_chat_search(request: Request):
    return await handle_chat_search(request)


@router.post("/api/chats/{chat_id}/fork")
async def _api_chat_fork(request: Request, chat_id: str):
    return await handle_chat_fork(request, chat_id)


async def handle_chat_search(request: Request):
    """Search chat titles and message bodies using FTS5.

    POST /api/chats/search with {query: "..."}.
    """
    from routes.db_chats import _fts_validate_query

    session = request.state.session
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query is required")
    if len(query) > 200:
        query = query[:200]
    if not _fts_validate_query(query):
        raise HTTPException(
            status_code=400,
            detail="Search query contains invalid characters",
        )

    results = await db.chat_search(session["user"], query)
    return JSONResponse({"results": results, "count": len(results)})


async def handle_chat_fork(request: Request, chat_id: str):
    """Duplicate a chat's metadata and messages.

    POST /api/chats/{chat_id}/fork returns the new chat dict.
    """
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Conversation not found")

    new_chat = await db.chat_fork(chat_id, session["user"])
    if not new_chat:
        raise HTTPException(
            status_code=500,
            detail="Failed to fork conversation",
        )

    return JSONResponse({
        "id": new_chat["id"],
        "title": new_chat["title"],
        "work_dir": new_chat["work_dir"],
    })


async def _skip_transcript_to_end(chat_id: str, session_id: str) -> None:
    """Advance the read position without importing anything.

    A turn sent from WebConsole is stored in ``messages`` by the handler and
    is *also* appended to the CLI transcript, because the runner resumes the
    same session. Without this the next sync would read those bytes back and
    show every web turn a second time. Skipping past them leaves the sync
    reporting only what arrived from elsewhere -- which is the terminal.
    """
    try:
        payload = await transcripts.read_turns(session_id, 0)
    except OSError:
        return
    if payload.get("found"):
        await db.chat_set_transcript_offset(chat_id, int(payload.get("offset") or 0))


async def _pending_prompt(session_id: str) -> dict[str, Any] | None:
    """The prompt *session_id* is blocked on, from the transcript or the screen.

    The transcript is asked first because it is the richer answer: an
    AskUserQuestion call carries the question text, its header and its declared
    options as structured data, and it is durable, so it reads the same whether
    or not the session is still hosted in a multiplexer.

    The terminal is the fallback, and it exists because the transcript is not a
    complete record of what a session can be blocked on. A permission prompt --
    "Permission rule Bash(curl*) requires confirmation for this command" -- is a
    TUI interaction the CLI never writes to the JSONL. Every one of the three
    question handlers gated on the transcript alone, so all three agreed there
    was no question while the session sat blocked on one, the badge offered no
    way to answer it, and the only way out was to walk to the terminal. The
    session's own status said `waiting` the whole time.

    Order matters and not only for richness: a session can hold a recorded
    question *and* show a prompt, and the recorded one is the question the user
    was asked. Reading the screen first would answer the wrong one.
    """
    pending = await asyncio.to_thread(transcripts.pending_question, session_id)
    if pending:
        return pending
    return await asyncio.to_thread(prompts.read_prompt, session_id)


async def _pending_options(session_id: str) -> list[dict[str, Any]]:
    """The options currently visible on *session_id*'s terminal.

    For auto_answer.consider, which needs the live labels -- the same reason
    handle_chat_question_get reads them off the screen rather than the
    tool call: the terminal offers more than the call declared.
    """
    target = await asyncio.to_thread(prompts.find_target, session_id, "")
    if not target:
        return []
    snapshot = target.get("snapshot") or ""
    return prompts.visible_options(snapshot)


async def _deliver_answer(session_id: str, index: int) -> dict[str, Any]:
    """Press *index* on *session_id*'s prompt. For auto_answer.consider."""
    target = await asyncio.to_thread(prompts.find_target, session_id, "")
    if not target:
        return {"ok": False, "reason": "not running inside screen or tmux"}
    return await asyncio.to_thread(prompts.answer, target, index)


async def handle_chat_question_get(request: Request):
    """GET /api/chats/{id}/question -- the prompt this chat's session is waiting on.

    Options are read off the live terminal, not from the tool call, because the
    prompt offers more than the call declared: Claude appends its own choices,
    such as free text and "Chat about this". Showing only the declared two would
    hide answers that are genuinely available.
    """
    session = request.state.session
    chat_id = request.path_params["chat_id"]
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    session_id = chat.get("session_id")
    if not session_id:
        return JSONResponse({"pending": False, "reason": "not linked to a session"})

    pending = await _pending_prompt(session_id)
    if not pending:
        return JSONResponse({"pending": False})

    target = await asyncio.to_thread(
        prompts.find_target, session_id, pending.get("needle") or ""
    )
    if not target:
        # The question is real but unreachable: nothing hosts the session in a
        # way that accepts input. Say so rather than offering dead controls.
        return JSONResponse({
            "pending": True,
            "answerable": False,
            "reason": "This session is not running inside screen or tmux, "
                      "so it can only be answered at its own terminal.",
            **{k: v for k, v in pending.items() if k != "needle"},
        })
    snapshot = target.get("snapshot") or ""
    return JSONResponse({
        "pending": True,
        "answerable": True,
        "selected": prompts.selected_index(snapshot),
        "options": prompts.visible_options(snapshot),
        **{k: v for k, v in pending.items() if k != "needle"},
    })


async def handle_chat_question_answer(request: Request):
    """POST /api/chats/{id}/question -- choose one of the prompt's options."""
    session = request.state.session
    chat_id = request.path_params["chat_id"]
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    session_id = chat.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="Chat is not linked to a session")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    try:
        want = int(data.get("index"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="index must be a number") from None
    if not 1 <= want <= 9:
        raise HTTPException(status_code=400, detail="index out of range")

    pending = await _pending_prompt(session_id)
    if not pending:
        raise HTTPException(status_code=409, detail="No question is waiting")
    target = await asyncio.to_thread(
        prompts.find_target, session_id, pending.get("needle") or ""
    )
    if not target:
        raise HTTPException(
            status_code=409,
            detail="This session cannot be answered from here — it is not "
                   "running inside screen or tmux",
        )
    result = await asyncio.to_thread(prompts.answer, target, want)
    if not result.get("ok"):
        _log.warning(
            "question_answer_failed chat_id=%s index=%s reason=%s",
            chat_id, want, result.get("reason"),
        )
        raise HTTPException(status_code=409, detail=result.get("reason") or "Failed")
    _log.info(
        "question_answered chat_id=%s user=%s index=%s label=%s",
        chat_id, session["user"], want, result.get("label"),
    )
    return JSONResponse({"ok": True, "index": want, "label": result.get("label")})


async def handle_chat_question_dismiss(request: Request):
    """DELETE /api/chats/{id}/question -- close the prompt without answering it.

    Every option in the bar answers the question, and some questions do not
    deserve an answer: the premise is wrong, the work moved on, or it was
    already settled in the terminal. Without this the only ways out were to
    pick something the user does not mean -- which the session then acts on --
    or to leave the prompt blocking that session indefinitely.

    Escape is what the prompt itself offers, so this delivers the keystroke the
    user would have pressed at the terminal rather than inventing a channel.

    Failure is returned rather than raised, because a 409 alone cannot say
    whether a retry is safe: ``delivered`` distinguishes a key the terminal
    refused (retry) from one that went in and left the prompt open (do not --
    see :func:`prompts.dismiss`). Raising HTTPException would flatten that to a
    string and the client would have to parse prose to decide.
    """
    session = request.state.session
    chat_id = request.path_params["chat_id"]
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    session_id = chat.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="Chat is not linked to a session")

    pending = await _pending_prompt(session_id)
    if not pending:
        # Nothing to close is the state the caller asked for, so it is a
        # success. Answering has the opposite default -- a 409 there stops an
        # answer landing on whatever prompt appeared next -- but there is no
        # equivalent hazard in declining to answer a question that is gone.
        return JSONResponse({"ok": True, "already_closed": True})

    target = await asyncio.to_thread(
        prompts.find_target, session_id, pending.get("needle") or ""
    )
    if not target:
        _log.info(
            "question_dismiss_unreachable chat_id=%s user=%s",
            chat_id, session["user"],
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": "This session cannot be reached from here — it is not "
                         "running inside screen or tmux, so the prompt has to be "
                         "closed at its own terminal.",
                "delivered": False,
            },
        )

    result = await asyncio.to_thread(prompts.dismiss, target)
    if not result.get("ok"):
        _log.warning(
            "question_dismiss_failed chat_id=%s user=%s delivered=%s reason=%s",
            chat_id, session["user"], result.get("delivered"), result.get("reason"),
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": result.get("reason") or "Could not close the prompt",
                "delivered": bool(result.get("delivered")),
            },
        )
    _log.info(
        "question_dismissed chat_id=%s user=%s question_id=%s",
        chat_id, session["user"], pending.get("id"),
    )
    return JSONResponse({"ok": True, "dismissed": True})


async def handle_chat_auto_answer_set(request: Request, chat_id: str):
    """PUT /api/chats/{id}/auto-answer -- arm or disarm auto-approval.

    Owner-scoped through db.chat_auto_answer_set, which is the property that
    matters here: this route arms an automatic approver of permission prompts,
    so a write that reached another user's chat would let one user turn on
    silent approval inside a conversation that is not theirs. See
    docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md.
    """
    session = request.state.session
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    # Defaults to off, not to whatever was stored before: an older client that
    # only ever sends `enabled` should land on the unambiguous two-state
    # behaviour it asked for, not silently inherit a flag it does not know
    # exists.
    accept_recommended = data.get("accept_recommended", False)
    if not isinstance(accept_recommended, bool):
        raise HTTPException(
            status_code=400, detail="accept_recommended must be a boolean"
        )

    # A voice-mode chat used to be refused here. It is allowed now: the flag
    # does nothing on the voice path rather than anything unsafe, and refusing
    # it here never prevented the state it was guarding against -- arming a
    # normal chat and *then* switching it to voice went around this check
    # entirely, and that is how two chats ended up unable to take a turn. The
    # voice_mode branch of handle_chat_update now clears the flag instead,
    # which is the ordering that actually occurs.
    ok = await db.chat_auto_answer_set(
        chat_id, session["user"], enabled, accept_recommended,
    )
    if not ok:
        # Covers both "no such chat" and "not this user's chat" with the same
        # 404 that chat_get uses elsewhere on this page, so a cross-owner probe
        # cannot distinguish "does not exist" from "not yours".
        raise HTTPException(status_code=404, detail="Chat not found")
    _log.info(
        "auto_answer_set chat_id=%s user=%s enabled=%s accept_recommended=%s",
        chat_id, session["user"], enabled, accept_recommended,
    )
    return JSONResponse({
        "ok": True, "enabled": enabled, "accept_recommended": accept_recommended,
    })


async def handle_chat_auto_answer_get(request: Request, chat_id: str):
    """GET /api/chats/{id}/auto-answer -- current state and the last ten
    answers and skips.
    """
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    # A voice-mode chat used to get a hardcoded `enabled: False` plus a
    # `voice_mode_blocked: True` flag here. That was the reason the resulting
    # breakage was undiagnosable: a chat with a stored `true` was failing every
    # turn on a 400 naming auto-answer, while this endpoint told the UI the
    # knob was off. Nothing under web/assets ever read `voice_mode_blocked`
    # either, so the flag bought nothing and the lie cost real debugging time.
    # Every chat now reports what is actually stored.
    enabled = await db.chat_auto_answer_get(chat_id, session["user"])
    accept_recommended = await db.chat_auto_answer_recommend_get(
        chat_id, session["user"],
    )
    log = await db.chat_auto_answer_log_get(chat_id, session["user"])
    return JSONResponse({
        "enabled": enabled, "accept_recommended": accept_recommended, "log": log,
    })


async def _sync_linked_chat(chat: dict) -> list[tuple[str, str]]:
    """Import turns added to *chat*'s transcript outside WebConsole.

    Returns the rows written, newest last, or an empty list when there was
    nothing new. Split out of handle_chat_sync so the same logic serves both
    the single open conversation and the sweep over all of them -- two copies
    of a dedup rule agreeing is a coincidence, not a guarantee.

    Cheap by design: the read starts at the stored byte offset rather than
    re-parsing a transcript that routinely runs to tens of megabytes, and a
    transcript with nothing appended costs one stat.
    """
    chat_id = chat["id"]
    session_id = chat.get("session_id")
    if not session_id:
        return []

    # One importer per conversation. The open conversation polls /sync every
    # five seconds while the 30-second sweep walks every linked chat, so two
    # imports could read the same transcript_offset, both decide the same
    # bytes were new, and both insert them -- which is one of the two ways a
    # message appeared twice.
    async with _sync_lock_for(chat_id):
        return await _sync_linked_chat_locked(chat, chat_id, session_id)


# loop -> {chat_id: lock}. Created on demand; a conversation that is never
# synced never gets one.
#
# Keyed by the running loop, not by chat id alone: an asyncio.Lock binds to the
# loop that first awaits it, and awaiting it from a second loop raises
# "bound to a different event loop". A module-level dict outlives any one loop
# -- every test builds its own, and so does a restarted server inside one
# process -- so a plain {chat_id: lock} turns the first sync after a loop swap
# into a hard failure. Weak keys so a finished loop's entry goes with it.
_sync_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

# How many stored messages the dedup compares against. A web turn stores two
# rows (prompt and reply) and a transcript block can carry a few more around
# them, so this is deliberately larger than the two it has to catch and still
# small enough to be one indexed read.
_SYNC_DEDUP_WINDOW: Final[int] = 12


def _drop_already_stored(
    tail: list[dict[str, Any]], rows: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Return *rows* without the prefix already present at the end of *tail*.

    *tail* is oldest-first (``db.messages_last``). The longest suffix of the
    stored rows that equals a prefix of the imported ones is the overlap, so
    everything after it is what is genuinely new. Returns *rows* unchanged
    when nothing overlaps, and an empty list when the whole block is already
    stored.
    """
    stored = [(row["role"], row["content"]) for row in tail]
    for size in range(min(len(stored), len(rows)), 0, -1):
        if stored[-size:] == rows[:size]:
            return rows[size:]
    return rows


def _sync_lock_for(chat_id: str) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop to bind to; the caller is about to await, so this is only
        # reachable from a synchronous caller, which has no race to lose.
        return asyncio.Lock()
    per_loop = _sync_locks.get(loop)
    if per_loop is None:
        per_loop = {}
        _sync_locks[loop] = per_loop
    lock = per_loop.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[chat_id] = lock
    return lock


async def _sync_linked_chat_locked(
    chat: dict, chat_id: str, session_id: str
) -> list[tuple[str, str]]:
    """The body of _sync_linked_chat, run under this conversation's lock."""
    # A turn running here is writing the same conversation from the other end:
    # the CLI appends the prompt to the transcript as soon as it starts, and
    # `finish()` stores the pair and steps the offset past it when it ends.
    # Importing those bytes in between stores the prompt a second time, which
    # is exactly the duplicate the user sees. The turn's own bookkeeping is
    # authoritative, so the sync stands aside while it runs.
    if turns.is_running(chat_id):
        return []

    # Re-read rather than trusting the caller's snapshot: the row it came from
    # may have been listed before a finishing turn, or the importer that just
    # released this lock, advanced the offset. Reading the stale value is what
    # makes a second importer re-read bytes that are already stored.
    offset = int(chat.get("transcript_offset") or 0)
    try:
        cursor = await db.db_conn.execute(
            "SELECT transcript_offset FROM chats WHERE id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is not None and row["transcript_offset"] is not None:
            offset = int(row["transcript_offset"])
    except Exception:
        pass

    # Run the question scan *before* the dedup guard so unanswered questions
    # in already-imported chats (offset == 0) are still caught.  The scan
    # is idempotent — it only returns unanswered questions — and the dedup
    # at line 4338 prevents duplicate inserts on every poll.
    question_blocks: list[dict[str, Any]] = []
    try:
        question_blocks = await asyncio.to_thread(
            transcripts._scan_questions_sync,
            transcripts.transcript_path(session_id),
        )
    except Exception:
        pass

    # Filter questions by IDs we already rendered for this chat.  This is the
    # real dedup guard: `_scan_questions_sync` returns the same unanswered
    # questions on every poll, so we must not re-insert them.
    try:
        seen_ids: set[str] = await db.chat_get_question_ids(chat_id)
    except Exception:
        seen_ids = set()
    new_ids: list[str] = []
    filtered_questions: list[dict[str, Any]] = []
    for qb in question_blocks:
        qid = str(qb.get("id") or "")
        if qid and qid not in seen_ids:
            new_ids.append(qid)
            filtered_questions.append(qb)
        elif not qid and qb not in filtered_questions:
            # No id — fall back to content dedup against the whole set.
            filtered_questions.append(qb)

    # A chat imported before transcript_offset existed carries the column
    # default of 0 while already holding its history, so reading from the
    # start would import every turn a second time. Treat it as caught up and
    # record where it actually is.
    if offset == 0 and await db.messages_get(chat_id):
        await _skip_transcript_to_end(chat_id, session_id)
        # Even if the tail read is skipped, attach any unanswered questions.
        rows: list[tuple[str, str]] = []
        for qb in filtered_questions:
            rendered = _question_to_text(qb)
            if rendered:
                rows.append(("assistant", rendered))
        if rows:
            _log.info(
                "transcript_sync_questions chat_id=%s questions=%d",
                chat_id, len(rows),
            )
            await db.chat_set_question_ids(chat_id, new_ids)
        else:
            # No new turns and no unanswered questions: clear stale IDs so
            # the sidebar stops rendering a dead question bar.
            await db.chat_set_question_ids(chat_id, [])
        return rows

    try:
        payload = await transcripts.read_turns(session_id, offset)
    except OSError as exc:
        _log.warning("transcript_sync_failed chat_id=%s: %s", chat_id, exc)
        return []
    if not payload.get("found"):
        return []

    rows = [
        row
        for row in (_turn_to_message(turn) for turn in payload.get("turns") or [])
        if row is not None
    ]

    # Attach any unanswered questions found by the full-file scan.
    for qb in filtered_questions:
        rendered = _question_to_text(qb)
        if rendered:
            rows.append(("assistant", rendered))

    new_offset = int(payload.get("offset") or offset)
    if new_offset != offset:
        await db.chat_set_transcript_offset(chat_id, new_offset)
    if not rows:
        # No turns were read but the file still existed (e.g. offset was
        # already at EOF).  If the scan found nothing either, clear stale
        # question_ids so the sidebar stops showing a dead question bar.
        if not question_blocks:
            await db.chat_set_question_ids(chat_id, [])
        return []

    # Dedup: the /stream handler's finish() callback may have already stored
    # some of these turns. Comparing only the stored tail against rows[0] --
    # what this did before -- catches the case where the overlap starts
    # exactly at the newest stored row and misses every other one: a web turn
    # stores ("user", prompt) and ("assistant", reply) while the transcript
    # block also carries the prompt in the middle, so the prompt landed twice.
    # Match the stored tail against the imported prefix instead, and keep only
    # what is genuinely new.
    try:
        tail = await db.messages_last(chat_id, _SYNC_DEDUP_WINDOW)
    except Exception:
        tail = []
    rows = _drop_already_stored(tail, rows)
    if not rows:
        _log.info("transcript_sync_deduped chat_id=%s", chat_id)
        return []

    await db.messages_batch(chat_id, rows)
    await db.bump_chat_updated_at(chat_id)
    if new_ids:
        await db.chat_set_question_ids(chat_id, new_ids)
        _log.info(
            "transcript_sync_questions chat_id=%s questions=%d",
            chat_id, len(new_ids),
        )
    elif not question_blocks:
        # All scanned questions were already seen (or answered).  No new
        # turns either — this branch is reached when read_turns returned rows
        # that survived dedup but the question scan found nothing unanswered.
        # Clear stale IDs so the sidebar stops showing a dead question bar.
        await db.chat_set_question_ids(chat_id, [])
        _log.info(
            "transcript_sync_question_ids_cleared chat_id=%s",
            chat_id,
        )
    _log.info("transcript_synced chat_id=%s turns=%d", chat_id, len(rows))
    return rows


async def handle_chat_sync(request: Request, chat_id: str):
    """POST /api/chats/{id}/sync -- pull in turns added outside WebConsole.

    Polled while a session-linked chat is open. The work is in
    _sync_linked_chat; this only resolves and scopes the conversation.
    """
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not chat.get("session_id"):
        return JSONResponse({"messages": [], "linked": False})

    try:
        rows = await _sync_linked_chat(chat)
    except sqlite3.OperationalError as exc:
        # A locked database is a retryable condition, not a failed request.
        # This route is polled every few seconds while a linked chat is open,
        # and _sync_linked_chat's only write is bookkeeping (the transcript
        # offset). Leaving the exception to escape turned a moment of write
        # contention into a 500 with a 200-line ASGI traceback in the log and
        # a red error in the user's console, for a poll that would have
        # succeeded on its next tick.
        #
        # Nothing is lost by reporting no rows here: the offset is only
        # advanced by the write that just failed, so the next poll re-reads
        # exactly the same range. Failing closed this way is what makes it
        # safe to swallow -- a partial ingest with an un-advanced offset
        # would duplicate turns instead.
        #
        # Deliberately narrow. sync_all catches bare Exception because one
        # unreadable transcript must not cost a whole sweep; this route is
        # about a single conversation the user is looking at, so anything
        # that is not this specific retryable class still surfaces as a 500.
        _log.warning("sync_failed chat_id=%s: %s", chat_id, exc)
        return JSONResponse({"messages": [], "linked": True, "retry": True})
    return JSONResponse(
        {
            "messages": [{"role": role, "content": content} for role, content in rows],
            "linked": True,
        }
    )


async def handle_chats_sync_all(request: Request):
    """POST /api/chats/sync -- follow every linked conversation, not just the open one.

    Without this a conversation only caught up when you opened it: /sync ran
    for the conversation on screen and nothing else, so a chat whose terminal
    was busy kept its old updated_at and sat in the sidebar looking idle. The
    list was refreshed faithfully every few seconds -- it was the underlying
    rows that were stale, which is a worse failure because the page looks live.

    One request rather than one per conversation. A transcript with nothing
    appended costs a stat, so the sweep is bounded by how many conversations
    are linked, not by how much history they hold.
    """
    session = request.state.session
    chats = await db.chat_list(session["user"])
    changed: dict[str, int] = {}
    scanned = 0
    for chat in chats:
        if not chat.get("session_id"):
            continue
        scanned += 1
        try:
            rows = await _sync_linked_chat(chat)
        except Exception as exc:
            # One unreadable transcript must not cost the whole sweep; the
            # single-chat route still reports its own failures loudly.
            _log.warning("sync_all_failed chat_id=%s: %s", chat["id"], exc)
            continue
        if rows:
            changed[chat["id"]] = len(rows)
    if changed:
        _log.info(
            "sync_all user=%s scanned=%d changed=%d turns=%d",
            session["user"], scanned, len(changed), sum(changed.values()),
        )
    return JSONResponse({"scanned": scanned, "changed": changed})
