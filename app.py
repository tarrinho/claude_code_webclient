#!/usr/bin/env python3
# app.py -- WebConsole FastAPI application.
#
# Chat front-end for a local Claude Code CLI. Serves HTML pages, JSON APIs,
# and SSE token streams. All routes require auth except POST /login and
# static assets.
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import config
import db
import runner

_log = logging.getLogger("wc.app")

_WEB_DIR: Final[Path] = Path(__file__).parent / "web"
_assets_dir: Final[Path] = _WEB_DIR / "assets"


# ── Middleware ────────────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, handler):
        sid = request.cookies.get("wc_session")
        request.state.session = auth.session_get(sid) if sid else None
        public_route = request.url.path == "/login" or request.url.path.startswith("/assets/")
        if not public_route and request.state.session is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Authentication required"})
            return JSONResponse(status_code=401, content={"error": "Authentication required"})
        return await handler(request)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, handler):
        response = await handler(request)
        if hasattr(response, "headers"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store, no-cache"
        return response


# ── Helpers ────────────────────────────────────────────────────────────────────────

def set_session_cookie(response: HTMLResponse | JSONResponse, sid: str) -> None:
    response.set_cookie(
        "wc_session", sid, httponly=True,
        secure=not config.COOKIE_ALLOW_INSECURE, samesite="strict",
        max_age=config.SESSION_TTL_S, path="/",
    )


def clear_session_cookie(response: HTMLResponse | JSONResponse) -> None:
    response.delete_cookie("wc_session", httponly=True,
                           secure=not config.COOKIE_ALLOW_INSECURE,
                           samesite="strict", path="/")


# ── Endpoint handlers ──────────────────────────────────────────────────────────────

async def handle_login(request: Request):
    """POST /login -- authenticates and sets session cookie."""
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return JSONResponse(status_code=400, content={"error": "Username and password required"})

    ip = request.client.host if request.client else "unknown"
    if auth.login_attempt_flood(ip):
        return JSONResponse(status_code=429, content={"error": "Too many attempts. Try again later."})

    user = await db.user_get_by_name(username)
    if not user or not auth.verify_password(password, user["password"]):
        should_backoff, wait = auth.login_record_failure(ip)
        if should_backoff:
            return JSONResponse(
                status_code=429, content={"error": "Too many attempts. Try again later."},
                headers={"Retry-After": str(wait)},
            )
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})

    sid, csrf = auth.session_new(user["name"])
    auth.login_record_success(ip)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, sid)
    resp.set_cookie("wc_csrf", csrf, httponly=True,
                    secure=not config.COOKIE_ALLOW_INSECURE, samesite="strict",
                    max_age=config.SESSION_TTL_S, path="/")
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


async def handle_chats_list(request: Request):
    """GET /api/chats -- list chats scoped to owner."""
    session = request.state.session
    chats = await db.chat_list(session["user"])
    return JSONResponse({
        "chats": [{"id": c["id"], "title": c["title"], "description": c["description"],
                   "work_dir": c["work_dir"], "created_at": c["created_at"],
                   "updated_at": c["updated_at"], "archived": bool(c["archived"]),
                   "pinned": bool(c["pinned"]), "pinned_at": c.get("pinned_at"),
                   "session_id": c.get("session_id"),
                   }
                  for c in chats],
    })


async def handle_chat_create(request: Request):
    """POST /api/chats -- create a new chat with its project directory."""
    session = request.state.session
    data = await request.json()
    title = (data.get("title") or "Untitled").strip()[:200]

    slug = db.slug_from_title(title)
    slug = db.slug_pattern(slug) or "untitled"

    date_suffix = datetime.date.today().isoformat()
    work_dir = str(Path(config.PROJECTS_ROOT).resolve() / f"{slug}-{date_suffix}")
    base = work_dir
    counter = 0
    while Path(work_dir).exists():
        counter += 1
        work_dir = f"{base}-{counter}"

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    chat_id = uuid.uuid4().hex
    now = await db.chat_create(chat_id, title, data.get("description"), work_dir, session["user"])
    _log.info("chat_created chat_id=%s work_dir=%s", chat_id, work_dir)
    return JSONResponse({"id": chat_id, "title": title, "work_dir": work_dir, "created_at": now})


async def handle_chat_get(request: Request, chat_id: str):
    """GET /api/chats/{id} -- get chat metadata and transcript."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    messages = await db.messages_get(chat_id)
    return JSONResponse({
        "chat": {**{k: chat[k] for k in (
            "id", "title", "description", "session_id", "work_dir", "archived",
            "pinned", "pinned_at",
        )}, "archived": bool(chat["archived"]), "pinned": bool(chat["pinned"])},
        "messages": [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]} for m in messages],
    })


async def handle_chat_patch(request: Request, chat_id: str):
    """PATCH /api/chats/{id} -- rename, edit description, archive, pin."""
    session = request.state.session
    data = await request.json()

    allowed = {"title", "description", "archived", "pinned"}
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
            raise HTTPException(status_code=400, detail="Description must be text or null")
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

    updated = await db.chat_update(chat_id, session["user"], **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Chat not found")
    return JSONResponse({"ok": True})


async def handle_chat_delete(request: Request, chat_id: str):
    """DELETE /api/chats/{id} -- hard delete (never rm -rf)."""
    session = request.state.session
    deleted = await db.chat_delete(chat_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found")
    _log.info("chat_deleted chat_id=%s", chat_id)
    return JSONResponse({"ok": True})


def render_chat_markdown(chat: dict, messages: list[dict]) -> str:
    """Render a chat transcript as a Markdown document."""
    created = chat.get("created_at") or "Unknown"
    try:
        created = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%-d %B %Y")
    except (AttributeError, ValueError):
        pass
    lines: list[str] = [
        f"# {chat['title']}", "",
        f"- Created: {created}",
        f"- Workspace: `{chat.get('work_dir') or 'Unknown'}`",
        f"- Session: `{chat.get('session_id') or 'None'}`", "",
    ]
    if chat.get("description"):
        lines.extend([f"> {chat['description']}", ""])
    labels = {"user": "User", "assistant": "Assistant", "system": "System"}
    for message in messages:
        role = labels.get(message.get("role"), "Message")
        lines.extend([f"## {role}", "", str(message.get("content") or ""), ""])
    return "\n".join(lines)


_render_chat_markdown = render_chat_markdown


async def handle_chat_export(request: Request, chat_id: str):
    """GET /api/chats/{id}/export -- download chat as Markdown."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    messages = await db.messages_get(chat_id)
    body = render_chat_markdown(chat, messages)
    filename = f"{db.slug_from_title(chat['title']) or 'conversation'}.md"
    return Response(body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def handle_submit_message(request: Request, chat_id: str):
    """POST /api/chats/{id}/messages -- submit a prompt, return the assistant's response."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    data = await request.json()
    prompt = (data.get("content") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    await db.messages_append(chat_id, "user", prompt)

    try:
        chunks, session_id = await runner.run_turn(
            prompt, chat["session_id"], chat["work_dir"], chat_id,
        )
    except runner.TurnError as e:
        return JSONResponse(status_code=500, content={"error": str(e), "fatal": e.fatal})

    if session_id and session_id != chat["session_id"]:
        await db.chat_set_session(chat_id, session_id)
    full_response = "".join(chunks) if chunks else ""
    if full_response:
        await db.messages_append(chat_id, "assistant", full_response)
    return JSONResponse({"response": full_response, "chunks": len(chunks)})


async def stream_handler(request: Request, chat_id: str):
    """POST /api/chats/{id}/stream -- SSE token stream with prompt in body."""
    sid = request.cookies.get("wc_session")
    session = auth.session_get(sid) if sid else None
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")

    chat = await db.chat_get(chat_id, session["user"])
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    data = await request.json()
    prompt = (data.get("content") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    await db.messages_append(chat_id, "user", prompt)

    async def event_generator():
        yield f"data: {json.dumps({'type': 'start', 'chat_id': chat_id})}\n\n"

        try:
            full_response_parts: list[str] = []

            async for event in runner.stream_turn(
                prompt,
                chat["session_id"],
                chat["work_dir"],
                chat_id,
            ):
                if event.get("type") == "session_id":
                    session_id = event.get("session_id")
                    if session_id and session_id != chat["session_id"]:
                        await db.chat_set_session(chat_id, session_id)
                elif event.get("type") == "text":
                    full_response_parts.append(event.get("content", ""))

                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)

            full_response = "".join(full_response_parts)
            if full_response:
                await db.messages_append(chat_id, "assistant", full_response)

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


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


# ── App ──────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("WebConsole starting v%s", config.VERSION)
    _log.info("PROJECTS_ROOT=%s HOST=%s PORT=%d", config.PROJECTS_ROOT, config.HOST, config.PORT)
    config.validate()
    await db.init()
    admin = await auth.bootstrap_admin()
    if admin:
        _log.info("bootstrapped admin: %s", admin)
    else:
        _log.info("admin user already exists or not configured")
    yield
    await db.close()
    _log.info("WebConsole shutting down")


app = FastAPI(title="WebConsole", version=config.VERSION, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.add_middleware(AuthMiddleware)
@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            f"<h1>Error {exc.status_code}</h1><p>{html_escape(str(exc.detail))}</p>",
            status_code=exc.status_code)
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

@app.post("/api/chats/{chat_id}/stream")
async def _api_stream(request: Request, chat_id: str):
    return await stream_handler(request, chat_id)


async def handle_sessions_list(request: Request):
    """GET /api/sessions -- list CLI sessions + Web chats for sidebar."""
    session = request.state.session
    cli_sessions = await db.read_claude_sessions()
    web_chats = await db.chat_list(session["user"])
    linked_session_ids = {chat.get("session_id") for chat in web_chats if chat.get("session_id")}
    cli_sessions = [item for item in cli_sessions if item.get("sessionId") not in linked_session_ids]
    # Merge unlinked CLI sessions with WebConsole chats.
    items = cli_sessions + [
        {"id": c["id"], "name": c["title"], "cwd": c["work_dir"],
         "kind": "web", "startedAt": c["created_at"], "updatedAt": c["updated_at"],
         "sessionId": c.get("session_id", ""), "webchat": True}
        for c in web_chats
    ]
    return JSONResponse({"sessions": items})


async def handle_sessions_resume(request: Request, session_id: str):
    """POST /api/sessions/{session_id}/resume -- open a WebConsole chat for a CLI session."""
    session = request.state.session
    # Create a new WebConsole chat linked to the CLI session
    import datetime as _dt
    from pathlib import Path as _Path
    import uuid as _uuid

    chat_id = _uuid.uuid4().hex
    title = f"CLI: {session_id[:12]}..."
    work_dir = str(_Path(config.PROJECTS_ROOT).resolve() / f"cli-import-{session_id[:8]}-{_dt.date.today().isoformat()}")
    _Path(work_dir).mkdir(parents=True, exist_ok=True)
    await db.chat_create(chat_id, title, None, work_dir, session["user"])
    # Link the CLI session ID
    await db.chat_set_session(chat_id, session_id)
    # Write session file so CLI can see it too
    try:
        db.write_claude_session_file(session_id, title, work_dir)
    except Exception:
        pass

    return JSONResponse({"id": chat_id, "title": title, "session_id": session_id})


# ── Session routes ─────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def _api_sessions_list(request: Request):
    return await handle_sessions_list(request)


@app.post("/api/sessions/{session_id}/resume")
async def _api_sessions_resume(request: Request, session_id: str):
    return await handle_sessions_resume(request, session_id)


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    uvicorn.run("app:app", host=config.LISTEN_HOST, port=config.PORT, log_level="info")