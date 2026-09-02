# app.py -- WebConsole FastAPI application.
#
# Chat front-end for a local Claude Code CLI. Serves HTML pages, JSON APIs,
# and SSE token streams. All routes require auth except POST /login and
# static assets.
from __future__ import annotations

import asyncio
import configparser
import datetime
import json
import logging
import logging.config
import re
import uuid
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

import auth
import config
import db
import prompts
import runner
import sysstats
import transcripts
import turns
from classification import (  # the callers left in this module;
    _asks_a_question,  # whole cluster moved.
    )
from middleware import (  # registered below; the order of add_middleware
    AuthMiddleware,  # calls is what decides execution order, not this.
    CsrfMiddleware,
    SecurityMiddleware,
)
from net_validation import (  # re-exported: app._client_ip and
    _client_ip,
)
from routes.machines import router as machines_router

# _resolve_member (a supervisors helper) adopts a session by calling the
# sessions route's handler, so this crosses prefixes. It travels to
# routes/supervisors.py with that helper when the prefix is extracted.
from routes.misc import router as misc_router
from routes.supervisors import router as supervisors_router
from shared import (  # helpers more than one route prefix needs;
    _MODEL_RE,
    _SSE_INTERNAL,
    _question_to_text,
    _turn_to_message,
    backend_kind,
)


# loguru keeps its own sinks, entirely separate from logging.conf, so
# runner.py's records -- turn launches, proxy connect failures, handshake
# problems and turn timeouts -- went to stderr and never reached the log file.
# Forwarding them into the stdlib logger the config already declares is
# cheaper and less risky than rewriting ten call sites, and it makes the
# wc.runner entry in logging.conf mean something again.
def _forward_loguru_to_logging() -> None:
    try:
        from loguru import logger as _loguru
    except ImportError:  # loguru is optional; runner falls back on its own
        return
    target = logging.getLogger("wc.runner")
    # loguru has levels stdlib does not (TRACE, SUCCESS); map them onto the
    # nearest stdlib level rather than dropping the record.
    levels = {
        "TRACE": logging.DEBUG, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
        "SUCCESS": logging.INFO, "WARNING": logging.WARNING,
        "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
    }

    def _sink(message) -> None:
        record = message.record
        target.log(levels.get(record["level"].name, logging.INFO), record["message"])

    # Drop loguru's default stderr sink: the stdlib config already writes to
    # the console, and leaving it would print every runner line twice.
    _loguru.remove()
    _loguru.add(_sink, level="DEBUG")


def _configure_logging() -> None:
    """Attach handlers to the wc.* loggers at import time.

    This has to run on import, not under ``__main__``: the server is started as
    ``python3 -m uvicorn app:app``, so this module is imported and the
    ``__main__`` block never executes. Without it the wc.* records went nowhere
    -- uvicorn configures only its own loggers, root was left with no handler,
    and logging's last-resort fallback emits WARNING and above. Every
    ``_log.info`` in the codebase was silently discarded, so the login,
    chat-creation and turn-timeout records §3 (A09) calls for did not exist. The
    file that looked like a log was only the shell's stdout redirect of
    uvicorn's access lines.

    ``logging.conf`` is honoured when present so the rotation policy lives in
    one place; anything else falls back to a stream handler, because losing log
    formatting must never stop the server from booting.
    """
    _forward_loguru_to_logging()
    conf = Path(__file__).parent / "logging.conf"
    if conf.is_file():
        try:
            # disable_existing_loggers would silence uvicorn's own loggers,
            # which are created before this module is imported.
            # defaults supplies the handler's filename, which the config names
            # as %(logfile)s. Done here rather than by rewriting the handler
            # afterwards because that would need a second fileConfig call, and
            # fileConfig closes every existing handler -- the failure that took
            # out 794 tests in registry #30.
            logging.config.fileConfig(
                str(conf),
                defaults={"logfile": config.LOG_FILE},
                disable_existing_loggers=False,
            )
            return
        # RuntimeError is what fileConfig raises for a file with no section
        # headers, which is the likeliest way this one gets corrupted.
        except (OSError, KeyError, ValueError, RuntimeError, configparser.Error):
            # A malformed config is worth reporting, but not worth refusing to
            # start over -- fall through to the stream handler below.
            #
            # exc_info is not optional here. This branch is the only signal
            # that the rotating file handler was never installed and the file
            # log has silently stopped existing, and it is logged through a
            # logging system that has just failed to configure. Without the
            # traceback it says that loading failed but not whether the file
            # was missing, unreadable or malformed -- at the moment the usual
            # way of finding out has stopped being written.
            logging.basicConfig(level=logging.INFO)
            logging.getLogger("wc.app").warning(
                "logging_config_failed path=%s", conf, exc_info=True
            )
            return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


_configure_logging()
_log = logging.getLogger("wc.app")

_WEB_DIR: Final[Path] = Path(__file__).parent / "web"
_assets_dir: Final[Path] = _WEB_DIR / "assets"










# ── Auth middleware ────────────────────────────────────────────────────────────────








# ── Helpers ────────────────────────────────────────────────────────────────────────












# ── CSRF middleware ─────────────────────────────────────────────────────────────────






# ── Security headers middleware ────────────────────────────────────────────────────




# ── Helpers ────────────────────────────────────────────────────────────────────────


def set_session_cookie(response: HTMLResponse | JSONResponse, sid: str) -> None:
    response.set_cookie(
        "wc_session",
        sid,
        httponly=True,
        secure=not config.COOKIE_ALLOW_INSECURE,
        samesite="strict",
        max_age=config.SESSION_TTL_S,
        path="/",
    )


def clear_session_cookie(response: HTMLResponse | JSONResponse) -> None:
    response.delete_cookie(
        "wc_session",
        httponly=True,
        secure=not config.COOKIE_ALLOW_INSECURE,
        samesite="strict",
        path="/",
    )


# ── Endpoint handlers ──────────────────────────────────────────────────────────────


async def handle_login(request: Request):
    """POST /login -- authenticates and sets session cookie."""
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return JSONResponse(
            status_code=400, content={"error": "Username and password required"}
        )

    # Attribute the attempt to the origin client, so the rate limit is per
    # client rather than per proxy when one is actually in front of us.
    ip = _client_ip(request)
    if auth.login_attempt_flood(ip):
        return JSONResponse(
            status_code=429, content={"error": "Too many attempts. Try again later."}
        )

    user = await db.user_get_by_name(username)
    if not user or not auth.verify_password(password, user["password"]):
        _log.warning("login failed user=%s ip=%s", username, ip)
        should_backoff, wait = auth.login_record_failure(ip)
        if should_backoff:
            return JSONResponse(
                status_code=429,
                content={"error": "Too many attempts. Try again later."},
                headers={"Retry-After": str(wait)},
            )
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})

    sid, csrf = auth.session_new(user["name"], user.get("role") or "user")
    auth.login_record_success(ip)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, sid)
    resp.set_cookie(
        "wc_csrf",
        csrf,
        # The one cookie that is deliberately not httponly, and §19's rule
        # ("all cookies httponly=True") reads as broken here without the
        # reason. This is the double-submit pattern: the page has to read this
        # value to echo it in X-CSRF-Token, which is exactly what a cross-site
        # request cannot do. The cookie that actually carries authority --
        # wc_session, set just above -- is httponly, so a script that reads
        # this one gains nothing it could not already send.
        httponly=False,
        secure=not config.COOKIE_ALLOW_INSECURE,
        samesite="strict",
        max_age=config.SESSION_TTL_S,
        path="/",
    )
    _log.info("login user=%s", username)
    return resp


async def handle_logout(request: Request):
    """POST /logout -- clears session cookie."""
    sid = request.cookies.get("wc_session")
    auth.session_drop(sid)
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    _log.info("logout")
    return resp


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
    is the same signal the supervisor already trusts, and "busy" is the only
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
    chats = await db.chat_list(session["user"])
    live_updated = await _live_updated_at(chats)
    # A turn now outlives the request that started it, so "is this conversation
    # busy?" is server state rather than something the open tab knows. The
    # sidebar reads it from here instead of watching its own stream, which only
    # ever saw the conversation being looked at.
    running = turns.running_ids(session["user"])
    queued = await db.queue_counts(session["user"])
    busy_sessions = await _busy_terminal_sessions()
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
                    "session_id": c.get("session_id"),
                    "model": c.get("model") or "",
                    # The sidebar populates the workspace pickers before the
                    # detail request lands, so the pin has to travel here too.
                    "ai_machine_id": c.get("ai_machine_id"),
                    "running": c["id"] in running,
                    "queued": queued.get(c["id"], 0),
                    # Work happening in a terminal this conversation is linked
                    # to. Draws the same dot; offers nothing to attach to.
                    "terminal_busy": bool(
                        c.get("session_id") and c["session_id"] in busy_sessions
                    ),
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
        raise HTTPException(status_code=500, detail="Could not create conversation directory — check server logs for details")
    chat_id = uuid.uuid4().hex
    now = await db.chat_create(
        chat_id, title, data.get("description"), work_dir, session["user"]
    )
    _log.info("chat_created chat_id=%s work_dir=%s", chat_id, work_dir)
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

    messages = await db.messages_get(chat_id)
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
            # judgement already backs the supervisor panel's "?", and a second
            # copy of it in JavaScript would drift from this one.
            #
            # Assistant rows only. A user message ending in "?" is the user
            # asking Claude, which needs nothing from the user.
            "messages": [
                {
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

    allowed = {"title", "description", "archived", "pinned", "ai_machine_id", "model"}
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

    updated = await db.chat_update(chat_id, session["user"], **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Chat not found")
    return JSONResponse({"ok": True})


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
    except Exception:  # noqa: BLE001 -- a malformed body is a client error
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


_render_chat_markdown = render_chat_markdown


# Images a turn produced are worth seeing, and the message renderer is
# text-only, so they were unreachable from the web UI entirely. Serving them
# needs care: this is the first endpoint that reads a user-named file, in an
# application whose runner already launches Claude with
# --dangerously-skip-permissions. The boundary is therefore the narrowest one
# that still works -- a chat may read files inside its own work_dir and
# nowhere else, checked the same way runner.py checks a launch directory.
_IMAGE_TYPES: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
# Large enough for a screenshot, small enough that a stray path cannot stream
# a database file out through an <img> tag.
_IMAGE_MAX_BYTES: Final[int] = 12 * 1024 * 1024


async def handle_chat_file(request: Request, chat_id: str):
    """GET /api/chats/{id}/file?path=... -- read an image from the workspace."""
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
        raise HTTPException(status_code=415, detail="Not an image this app serves")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if candidate.stat().st_size > _IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Image is too large to display")

    _log.info("chat_file_served chat_id=%s path=%s", chat_id, candidate.name)
    return FileResponse(
        candidate,
        media_type=media_type,
        # inline so the browser renders it; nosniff is applied globally.
        headers={"Content-Disposition": f'inline; filename="{candidate.name}"'},
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
      ``provider`` column says ``anthropic`` for these too, since that only
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
        return
    models = frame.get("models") or {}
    if not models:
        _log.warning(
            "usage_frame_has_no_models: chat_id=%s frame_keys=%s "
            "(a usage frame arrived but named no model, so nothing can be "
            "attributed)",
            chat_id, sorted(frame),
        )
        return
    try:
        machine = await db.ai_machine_active(owner)
    except Exception as exc:  # noqa: BLE001 -- accounting must not break a live turn
        _log.warning(
            "usage_provider_unresolved: chat_id=%s (%s) — rows fall back to the "
            "default provider label", chat_id, exc,
        )
        machine = None
    provider = backend_kind(machine)
    cost = frame.get("cost_usd")
    for index, (model, stats) in enumerate(models.items()):
        if not isinstance(stats, dict):
            continue
        await db.usage_record(
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
    except Exception:  # noqa: BLE001 -- routing must never block a turn
        return
    if backend.get("provider") != "anthropic":
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
        # Inside the task, not before it: a transcript repair on a large
        # conversation would otherwise delay the HTTP response.
        await _prepare_transcript_for_backend(chat)
        async for event in runner.stream_turn(
            prompt, chat["session_id"], chat["work_dir"], chat_id, model
        ):
            yield event

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
            return
        await db.messages_batch(
            chat_id, [("user", prompt), ("assistant", "".join(parts))]
        )
        await db.bump_chat_updated_at(chat_id)
        if session_id and session_id != chat["session_id"]:
            await db.chat_set_session(chat_id, session_id)
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


turns.launcher = _launch_queued


async def stream_handler(request: Request, chat_id: str):
    """POST /api/chats/{id}/stream -- SSE token stream with prompt in body."""
    sid = request.cookies.get("wc_session")
    session = auth.session_get(sid) if sid else None
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
            "stream_handler: empty prompt from user=%s chat_id=%s",
            session["user"], chat_id,
        )
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    if len(prompt) > config.PROMPT_MAX_CHARS:
        raise HTTPException(status_code=400, detail="Prompt is too long")
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

    async def event_generator():
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
        except Exception:  # noqa: BLE001 -- convert stream failures to SSE errors
            _log.exception("stream_handler unexpected error on %s", chat_id)
            yield f"data: {json.dumps({'type': 'error', 'error': _SSE_INTERNAL})}\n\n"

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

    async def event_generator():
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
        except Exception:  # noqa: BLE001 -- convert follow failures to SSE errors
            _log.exception("live stream failed chat_id=%s", chat_id)
            yield f"data: {json.dumps({'type': 'error', 'error': _SSE_INTERNAL})}\n\n"

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


# ── HTML templates ────────────────────────────────────────────────────────────────


async def handle_index(request: Request):
    try:
        return HTMLResponse((_WEB_DIR / "index.html").read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>Index template missing</h1>", status_code=500)


async def handle_login_page(request: Request):
    try:
        return HTMLResponse((_WEB_DIR / "login.html").read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>Login template missing</h1>", status_code=500)


async def handle_supervisor_page(request: Request):
    try:
        return HTMLResponse((_WEB_DIR / "supervisor.html").read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>Supervisor template missing</h1>", status_code=500)


# ── App ──────────────────────────────────────────────────────────────────────────


async def _load_settings_from_db() -> None:
    """Read secret settings from the DB and inject them into config.py.

    This bridges the gap between in-DB configuration and the env-driven
    config.py which reads values at import time.  Settings that exist in
    the DB take precedence over defaults; callers that *require* a value
    (session secret, proxy token) will still fail-fast in config.validate()
    if neither source has them.
    """
    # (db_key, config_attribute, type_hint)
    pairs: list[tuple[str, str, str]] = [
        ("session_secret", "SESSION_SECRET", "str"),
        ("projects_root", "PROJECTS_ROOT", "str"),
        ("proxy_token", "PROXY_TOKEN", "str"),
        ("model_base_url", "MODEL_BASE_URL", "str"),
        ("model_api_key", "MODEL_API_KEY", "str"),
        ("model_name", "MODEL_NAME", "str"),
        ("turn_timeout", "TURN_TIMEOUT_S", "int"),
        ("prompt_max", "PROMPT_MAX_CHARS", "int"),
        ("cookie_allow_insecure", "COOKIE_ALLOW_INSECURE", "bool"),
        ("ai_machine_host", "PROXY_HOST", "str"),
    ]
    for db_key, attr, typ in pairs:
        val: str | None = None
        try:
            val = await db.setting_get(db_key)
        except Exception:  # noqa: BLE001,S110 -- settings DB may not be ready at import time
            pass
        if val is not None:
            if typ == "int":
                try:
                    setattr(config, attr, int(str(val)))
                except (TypeError, ValueError):
                    pass
            elif typ == "bool":
                setattr(config, attr, str(val).lower() in ("1", "true", "yes", "on"))
            else:
                setattr(config, attr, str(val))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("WebConsole starting v%s", config.VERSION)
    _log.info(
        "PROJECTS_ROOT=%s HOST=%s PORT=%d",
        config.PROJECTS_ROOT,
        config.HOST,
        config.PORT,
    )
    await db.init()
    # Load DB settings into env so config.py can see them at runtime.
    await _load_settings_from_db()
    config.validate()
    restored = auth.load_sessions()
    if restored:
        _log.info("restored %d session(s) across the restart", restored)
    admin = await auth.bootstrap_admin()
    if admin:
        _log.info("bootstrapped admin: %s", admin)
    else:
        _log.info("admin user already exists or not configured")
    # History has to accumulate while nobody is watching, or the Server page
    # can only ever chart the moments someone had the tab open.
    sysstats.start(db.system_sample_insert)
    yield
    # Stopped before db.close(): the sampler writes through the connection.
    await sysstats.stop()
    # Before db.close(): a turn cancelled here still runs its `finish`, which
    # needs the connection. Leaving them to be torn down with the loop instead
    # abandoned tasks mid-write.
    await turns.shutdown()
    await db.close()
    _log.info("WebConsole shutting down")


app = FastAPI(title="WebConsole", version=config.VERSION, lifespan=lifespan)
# Routes for /api/machines and /api/models. FastAPI matches in the order
# routers are included, so this line's position is the registration order.
app.include_router(machines_router)
app.include_router(supervisors_router)
app.include_router(misc_router)

app.add_middleware(
    CORSMiddleware, allow_origins=[], allow_methods=["*"], allow_headers=["*"]
)
app.add_middleware(CsrfMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityMiddleware)


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    # Log before branching on Accept: the HTML branch used to return first,
    # which meant every browser-triggered error went unrecorded. Every field is
    # read defensively -- if this handler raises, the client loses the original
    # error and gets an opaque 500 instead.
    try:
        session = getattr(getattr(request, "state", None), "session", None)
        _log.error(
            "HTTP %s path=%s detail=%s ip=%s user=%s",
            exc.status_code,
            getattr(getattr(request, "url", None), "path", "?"),
            exc.detail,
            getattr(getattr(request, "client", None), "host", "?") or "?",
            session.get("user", "anonymous") if isinstance(session, dict) else "anonymous",
        )
    except Exception:  # noqa: BLE001,S110 -- logging must never mask the real error
        pass
    if "text/html" in (getattr(request, "headers", None) or {}).get("accept", ""):
        return HTMLResponse(
            f"<h1>Error {exc.status_code}</h1><p>{html_escape(str(exc.detail))}</p>",
            status_code=exc.status_code,
        )
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


app.add_route("/login", handle_login, methods=["POST"])
app.add_route("/logout", handle_logout, methods=["POST"])

if _assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

app.add_route("/", handle_index, methods=["GET"])
app.add_route("/login", handle_login_page, methods=["GET"])

# ── Route registration ───────────────────────────────────────────────────────────
# Use FastAPI decorator routes (GET/PATCH/DELETE) so path parameters like
# {chat_id} are injected by the framework.  add_route() does NOT do this.


@app.get("/api/chats")
async def _api_chats_list(request: Request):
    return await handle_chats_list(request)


@app.post("/api/chats")
async def _api_chat_create(request: Request):
    return await handle_chat_create(request)


# Registered before /api/chats/{chat_id} so "order" is never captured as an id.
@app.put("/api/chats/order")
async def _api_chats_reorder(request: Request):
    return await handle_chats_reorder(request)


@app.get("/api/chats/{chat_id}")
async def _api_chat_get(request: Request, chat_id: str):
    return await handle_chat_get(request, chat_id)


@app.patch("/api/chats/{chat_id}")
async def _api_chat_patch(request: Request, chat_id: str):
    return await handle_chat_patch(request, chat_id)


@app.delete("/api/chats/{chat_id}")
async def _api_chat_delete(request: Request, chat_id: str):
    return await handle_chat_delete(request, chat_id)


@app.get("/api/chats/{chat_id}/export")
async def _api_chat_export(request: Request, chat_id: str):
    return await handle_chat_export(request, chat_id)


@app.post("/api/chats/{chat_id}/messages")
async def _api_submit_message(request: Request, chat_id: str):
    return await handle_submit_message(request, chat_id)


@app.get("/api/chats/{chat_id}/file")
async def _api_chat_file(request: Request, chat_id: str):
    return await handle_chat_file(request, chat_id)


@app.get("/api/chats/{chat_id}/question")
async def _api_chat_question_get(request: Request, chat_id: str):
    return await handle_chat_question_get(request)


@app.post("/api/chats/{chat_id}/question")
async def _api_chat_question_answer(request: Request, chat_id: str):
    return await handle_chat_question_answer(request)


@app.delete("/api/chats/{chat_id}/question")
async def _api_chat_question_dismiss(request: Request, chat_id: str):
    return await handle_chat_question_dismiss(request)


@app.get("/api/chats/{chat_id}/live")
async def _api_chat_live(request: Request, chat_id: str):
    return await handle_chat_live(request, chat_id)


@app.post("/api/chats/{chat_id}/stop")
async def _api_chat_stop(request: Request, chat_id: str):
    return await handle_turn_stop(request, chat_id)


@app.get("/api/chats/{chat_id}/queue")
async def _api_chat_queue_list(request: Request, chat_id: str):
    return await handle_queue_list(request, chat_id)


@app.delete("/api/chats/{chat_id}/queue/{queue_id}")
async def _api_chat_queue_delete(request: Request, chat_id: str, queue_id: int):
    return await handle_queue_delete(request, chat_id, queue_id)


@app.post("/api/chats/{chat_id}/queue/{queue_id}/release")
async def _api_chat_queue_release(request: Request, chat_id: str, queue_id: int):
    return await handle_queue_release(request, chat_id, queue_id)


# Registered ahead of /api/chats/{chat_id}/... so the literal wins. FastAPI
# matches in order, and "sync" would otherwise be a plausible chat_id.
@app.post("/api/chats/sync")
async def _api_chats_sync_all(request: Request):
    return await handle_chats_sync_all(request)


@app.post("/api/chats/{chat_id}/sync")
async def _api_chat_sync(request: Request, chat_id: str):
    return await handle_chat_sync(request, chat_id)


@app.post("/api/chats/{chat_id}/stream")
async def _api_stream(request: Request, chat_id: str):
    return await stream_handler(request, chat_id)


@app.post("/api/chats/search")
async def _api_chat_search(request: Request):
    return await handle_chat_search(request)


@app.post("/api/chats/{chat_id}/fork")
async def _api_chat_fork(request: Request, chat_id: str):
    return await handle_chat_fork(request, chat_id)




















async def handle_chat_search(request: Request):
    """Search chat titles and message bodies using FTS5.

    POST /api/chats/search with {query: "..."}.
    """
    session = request.state.session
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON")

    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query is required")
    if len(query) > 200:
        query = query[:200]

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
























# ── API tokens ─────────────────────────────────────────────────────────────────
#
# The authenticated way in for a caller that cannot hold a cookie. This exists
# because the unauthenticated way kept being invented instead: a `/dev/*` prefix
# exempted from the auth middleware, with one route under it that minted an
# admin session for anybody who sent a GET. A script needs a credential, and the
# absence of one is what turns into a hole.










_MACHINE_PORT_RE = re.compile(r"^(?:0|[1-9]\d{0,4})$")









































































































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
    except Exception:  # noqa: BLE001
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

    offset = int(chat.get("transcript_offset") or 0)

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
    except Exception:  # noqa: BLE001,S110
        pass

    # Filter questions by IDs we already rendered for this chat.  This is the
    # real dedup guard: `_scan_questions_sync` returns the same unanswered
    # questions on every poll, so we must not re-insert them.
    try:
        seen_ids: set[str] = await db.chat_get_question_ids(chat_id)
    except Exception:  # noqa: BLE001
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
        return []

    # Dedup: the /stream handler's finish() callback may have already stored
    # these turns in the messages table.  Compare the last stored row against
    # the first row from the transcript; if they match, all rows are already
    # present (the transcript order is fixed, so equality at the tail means
    # the whole block is a duplicate).
    try:
        tail = await db.messages_last(chat_id, 1)
    except Exception:  # noqa: BLE001
        tail = []
    if tail and tail[0]["role"] == rows[0][0] and tail[0]["content"] == rows[0][1]:
        _log.info("transcript_sync_deduped chat_id=%s count=%d", chat_id, len(rows))
        return []

    await db.messages_batch(chat_id, rows)
    await db.bump_chat_updated_at(chat_id)
    if new_ids:
        await db.chat_set_question_ids(chat_id, new_ids)
        _log.info(
            "transcript_sync_questions chat_id=%s questions=%d",
            chat_id, len(new_ids),
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

    rows = await _sync_linked_chat(chat)
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
        except Exception as exc:  # noqa: BLE001
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














# ── Session routes ─────────────────────────────────────────────────────────────────






















































# Two paths, one page. "/supervisor.html" is what index.html's iframe and its
# standalone fallback both ask for, so it cannot move; "/supervisor" is what
# anyone types or bookmarks, and it 404'd. Served directly rather than
# redirected, matching how "/" and "/login" already serve their templates.
# Relative asset resolution is the same from both: with no trailing slash the
# base is "/", so "supervisor.js" resolves to "/supervisor.js" either way.
@app.get("/supervisor")
@app.get("/supervisor.html")
async def _serve_supervisor_page(request: Request):
    return await handle_supervisor_page(request)


# `_serve_supervisor_js` was here. The supervisor script lived in web/ rather
# than web/assets/, so nothing served it and this route existed to read the file
# and set `application/javascript` by hand -- browsers enforce strict MIME
# checking on scripts, and served as text/html it was refused outright, leaving
# the page rendering its markup with none of its behaviour.
#
# The script now lives under web/assets/supervisor/, which StaticFiles already
# mounts, so the MIME type is correct without a route. Its old location is also
# why the section-4 timer scan globbed the wrong directory and missed a bare
# timer for a whole sweep: a file outside the directory everything else lives in
# is invisible to every check written against that directory.
#
# `_serve_supervisor_page` below stays. StaticFiles is mounted on /assets only,
# so "/supervisor" and "/supervisor.html" still need a route of their own.


































# ── Machine routes ─────────────────────────────────────────────────────────────────
















if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.LISTEN_HOST, port=config.PORT, log_level="info")
