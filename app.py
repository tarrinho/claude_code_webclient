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
import os
import re
import secrets
import socket
import uuid
from contextlib import asynccontextmanager
from html import escape as html_escape
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import ClassVar, Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import config
import db
import runner

_log = logging.getLogger("wc.app")

_WEB_DIR: Final[Path] = Path(__file__).parent / "web"
_assets_dir: Final[Path] = _WEB_DIR / "assets"

# Hostname regex: labels, full IPv4, or bracketed IPv6.
_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|"
    r"\[[0-9A-Fa-f:]+\]|[0-9A-Fa-f:]+)$"
)

# Blocked private / reserved IP ranges for SSRF protection.
_BLOCKED_NETS: Final[list[IPv4Network | IPv6Network]] = [
    ip_network("0.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("100.64.0.0/10"),  # CG-NAT
    ip_network("127.0.0.0/8"),  # Loopback
    ip_network("169.254.0.0/16"),  # Link-local
    ip_network("172.16.0.0/12"),
    ip_network("192.0.0.0/24"),  # IETF
    ip_network("192.0.2.0/24"),  # TEST-NET-1
    ip_network("192.88.99.0/24"),  # 6to4 relay
    ip_network("192.168.0.0/16"),
    ip_network("198.18.0.0/15"),
    ip_network("198.51.100.0/24"),  # TEST-NET-2
    ip_network("203.0.113.0/24"),  # TEST-NET-3
    ip_network("224.0.0.0/4"),  # Multicast
    ip_network("240.0.0.0/4"),  # Reserved
    ip_network("::1/128"),  # IPv6 loopback
    ip_network("fc00::/7"),  # ULA
    ip_network("fe80::/10"),  # IPv6 link-local
]


# ── Auth middleware ────────────────────────────────────────────────────────────────


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, handler):
        sid = request.cookies.get("wc_session")
        request.state.session = auth.session_get(sid) if sid else None
        public_route = request.url.path == "/login" or request.url.path.startswith(
            "/assets/"
        )
        if not public_route and request.state.session is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"error": "Session expired", "redirect": "/login"},
                )
            return RedirectResponse(url="/login", status_code=303)
        return await handler(request)


# ── Helpers ────────────────────────────────────────────────────────────────────────


def _is_private_ip(host: str) -> bool:
    """Return True if *host* resolves to a blocked private address."""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # strip IPv6 brackets for parsing
    try:
        addr = ip_address(host)
        for net in _BLOCKED_NETS:
            if addr in net:
                return True
        return False
    except ValueError:
        return False


def _validate_host(host: str) -> None:
    """Validate that *host* is a non-empty string that looks like a hostname or IP."""
    if not host or not isinstance(host, str) or not _HOST_PATTERN.match(host):
        raise HTTPException(
            status_code=400, detail="Enter a valid hostname or IP address"
        )


def _resolve_host(host: str) -> str:
    """Resolve *host* to an IP and check against the blocklist.

    Returns the resolved IPv4 or IPv6 string so that asyncio.open_connection
    can connect directly (avoiding a second DNS lookup).
    Raises HTTPException on private IPs.
    """
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        raise HTTPException(status_code=400, detail="DNS resolution failed")
    # Pick the first result (address family, socktype, proto, canonname, sockaddr).
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            _log.warning("blocked outbound connection to %s -> %s", host, ip)
            raise HTTPException(
                status_code=403, detail="Internal hosts are not allowed"
            )
        return ip
    raise HTTPException(status_code=400, detail="DNS resolution failed")


# ── CSRF middleware ─────────────────────────────────────────────────────────────────


class CsrfMiddleware(BaseHTTPMiddleware):
    """Validate the X-CSRF-Token header on mutating requests.

    The login endpoint sets two cookies:
      wc_session  – opaque session id
      wc_csrf     – token issued at login time

    POST /login is exempt – the session cookie is the CSRF guard.
    Every other mutating endpoint must send a matching X-CSRF-Token header.
    GET, OPTIONS, and HEAD are exempt.
    """

    _MUTATING: ClassVar[set] = {"POST", "PATCH", "DELETE"}
    _EXEMPT_PATHS: ClassVar[set] = {"/login"}

    async def dispatch(self, request: Request, handler):
        if (
            request.method in self._MUTATING
            and request.url.path not in self._EXEMPT_PATHS
        ):
            cookie_token = request.cookies.get("wc_csrf", "")
            header_token = request.headers.get("x-csrf-token", "")
            if (
                not cookie_token
                or not header_token
                or not auth._csrf_valid(cookie_token, header_token)
            ):
                return JSONResponse(
                    status_code=403,
                    content={"error": "CSRF token invalid or missing"},
                )
        return await handler(request)


# ── Security headers middleware ────────────────────────────────────────────────────


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, handler):
        response = await handler(request)
        if hasattr(response, "headers"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store, no-cache"
            # CSP – nonce is generated per-request and stored on request.state.
            nonce = getattr(request.state, "csp_nonce", "")
            if nonce:
                response.headers["Content-Security-Policy"] = (
                    f"default-src 'self'; script-src 'nonce-{nonce}'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
                    "form-action 'self'"
                )
            # HSTS – enforce HTTPS for one year, subdomains included.
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )
        return response


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

    ip = request.client.host if request.client else "unknown"
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

    sid, csrf = auth.session_new(user["name"])
    auth.login_record_success(ip)
    resp = JSONResponse({"ok": True})
    set_session_cookie(resp, sid)
    resp.set_cookie(
        "wc_csrf",
        csrf,
        httponly=True,
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


async def handle_chats_list(request: Request):
    """GET /api/chats -- list chats scoped to owner."""
    session = request.state.session
    chats = await db.chat_list(session["user"])
    return JSONResponse(
        {
            "chats": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "description": c["description"],
                    "work_dir": c["work_dir"],
                    "created_at": c["created_at"],
                    "updated_at": c["updated_at"],
                    "archived": bool(c["archived"]),
                    "pinned": bool(c["pinned"]),
                    "pinned_at": c.get("pinned_at"),
                    "session_id": c.get("session_id"),
                    "model": c.get("model") or "",
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

    Path(work_dir).mkdir(parents=True, exist_ok=True)
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
                    )
                },
                "archived": bool(chat["archived"]),
                "pinned": bool(chat["pinned"]),
            },
            "messages": [
                {
                    "role": m["role"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                }
                for m in messages
            ],
        }
    )


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


async def handle_chat_export(request: Request, chat_id: str):
    """GET /api/chats/{id}/export -- download chat as Markdown."""
    session = request.state.session
    chat = await db.chat_get(chat_id, session["user"], include_archived=True)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    messages = await db.messages_get(chat_id)
    body = render_chat_markdown(chat, messages)
    filename = f"{db.slug_from_title(chat['title']) or 'conversation'}.md"
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Safe messages for SSE errors so internal details never leak.
_SSE_INTERNAL = "An internal error occurred — see server logs."
_SSE_TIMEOUT = "The turn timed out."
_SSE_UNKNOWN = "Connection lost during streaming."


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

    try:
        chunks, session_id = await runner.run_turn(
            prompt,
            chat["session_id"],
            chat["work_dir"],
            chat_id,
            model,
        )
    except runner.TurnError as e:
        return JSONResponse(
            status_code=500, content={"error": str(e), "fatal": e.fatal}
        )

    full_response = "".join(chunks) if chunks else ""
    await db.messages_batch(
        chat_id,
        [
            ("user", prompt),
            ("assistant", full_response),
        ],
    )
    if session_id and session_id != chat["session_id"]:
        await db.chat_set_session(chat_id, session_id)
    model = runner.take_last_model(chat_id)
    if model and model != chat.get("model"):
        await db.chat_set_model(chat_id, model)
    return JSONResponse(
        {"response": full_response, "chunks": len(chunks), "model": model}
    )


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

        try:
            full_response_parts: list[str] = []
            pending_session_id = chat["session_id"]
            pending_model = chat.get("model") or ""
            completed = False
            failed = False

            async for event in runner.stream_turn(
                prompt,
                chat["session_id"],
                chat["work_dir"],
                chat_id,
                model,
            ):
                event_type = event.get("type")
                if event_type == "session_id":
                    pending_session_id = event.get("session_id") or pending_session_id
                elif event_type == "model":
                    pending_model = event.get("model") or pending_model
                elif event_type == "text":
                    full_response_parts.append(event.get("content", ""))
                elif event_type == "error":
                    failed = True
                elif event_type == "done":
                    if failed:
                        break
                    full_response = "".join(full_response_parts)
                    await db.messages_batch(
                        chat_id,
                        [
                            ("user", prompt),
                            ("assistant", full_response),
                        ],
                    )
                    if pending_session_id and pending_session_id != chat["session_id"]:
                        await db.chat_set_session(chat_id, pending_session_id)
                    if pending_model and pending_model != chat.get("model"):
                        await db.chat_set_model(chat_id, pending_model)
                    completed = True

                yield f"data: {json.dumps(event)}\n\n"
                await asyncio.sleep(0)

            if not completed and not failed:
                yield f"data: {json.dumps({'type': 'error', 'error': 'Stream ended before completion'})}\n\n"

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


# ── HTML templates ────────────────────────────────────────────────────────────────


async def handle_index(request: Request):
    try:
        return HTMLResponse((_WEB_DIR / "index.html").read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>Index template missing</h1>", status_code=500)


async def handle_login_page(request: Request):
    try:
        # Generate a CSP nonce and store it on request.state so the
        # security middleware can read it when building the response.
        # Also inject it into the HTML so the client-side script tag
        # can reference it.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        content = (_WEB_DIR / "login.html").read_text()
        content = content.replace("__CSP_NONCE__", nonce)
        return HTMLResponse(content)
    except FileNotFoundError:
        return HTMLResponse("<h1>Login template missing</h1>", status_code=500)


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
    admin = await auth.bootstrap_admin()
    if admin:
        _log.info("bootstrapped admin: %s", admin)
    else:
        _log.info("admin user already exists or not configured")
    yield
    await db.close()
    _log.info("WebConsole shutting down")


app = FastAPI(title="WebConsole", version=config.VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=[], allow_methods=["*"], allow_headers=["*"]
)
app.add_middleware(CsrfMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityMiddleware)


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    if "text/html" in request.headers.get("accept", ""):
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


@app.post("/api/chats/search")
async def _api_chat_search(request: Request):
    return await handle_chat_search(request)


@app.post("/api/chats/{chat_id}/fork")
async def _api_chat_fork(request: Request, chat_id: str):
    return await handle_chat_fork(request, chat_id)


@app.get("/api/admin/export")
async def _api_db_backup(request: Request):
    return await handle_db_backup(request)


@app.post("/api/admin/import")
async def _api_db_restore(request: Request):
    return await handle_db_restore(request)


async def handle_skills_get(request: Request):
    """GET /api/skills -- list installed skills and session activity."""
    session = request.state.session
    session_id = request.query_params.get("session_id")
    if not session_id:
        chat_id = request.query_params.get("chat_id")
        if chat_id:
            chat = await db.chat_get(chat_id, session["user"])
            session_id = chat.get("session_id") if chat else None
    skills_root = Path.home() / ".claude" / "skills"
    active = set(runner.active_skills(session_id))
    skills = []
    try:
        entries = sorted(skills_root.iterdir(), key=lambda p: p.name.lower())
    except (OSError, PermissionError):
        entries = []
    for entry in entries:
        if not entry.is_dir() or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", entry.name
        ):
            continue
        path = entry / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        description = ""
        for line in text.splitlines()[:40]:
            if line.lower().startswith("description:"):
                description = line.split(":", 1)[1].strip()[:500]
                break
        skills.append(
            {
                "name": entry.name,
                "description": description,
                "installed": True,
                "active": entry.name in active,
            }
        )
    return JSONResponse({"skills": skills, "session_id": session_id or ""})


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


async def handle_db_backup(request: Request):
    """Download a gzip-compressed database backup."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    data = await db.db_backup()
    date_str = (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    )
    fname = f'webconsole-backup-{date_str}.db.gz'
    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "content-disposition": (
                f'attachment; filename="{fname}"'
            )
        },
    )


async def handle_db_restore(request: Request):
    """POST /api/admin/import -- restore database from uploaded backup."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    file_form = await request.form()
    file = file_form.get("file")
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if len(file.filename) > 200:
        raise HTTPException(status_code=400, detail="Filename too long")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large")

    success = await db.db_restore(data)
    if not success:
        raise HTTPException(status_code=500, detail="Restore failed — invalid or corrupted backup")

    return JSONResponse({"ok": True, "message": "Database restored successfully"})


async def handle_settings_get(request: Request):
    """GET /api/settings -- return non-secret runtime and app settings."""
    host = await runner.get_proxy_host()
    try:
        session_ttl = int(await db.setting_get("session_ttl") or config.SESSION_TTL_S)
    except (TypeError, ValueError):
        session_ttl = config.SESSION_TTL_S
    try:
        turn_timeout = int(
            await db.setting_get("turn_timeout") or config.TURN_TIMEOUT_S
        )
    except (TypeError, ValueError):
        turn_timeout = config.TURN_TIMEOUT_S
    try:
        prompt_max = int(await db.setting_get("prompt_max") or config.PROMPT_MAX_CHARS)
    except (TypeError, ValueError):
        prompt_max = config.PROMPT_MAX_CHARS
    return JSONResponse(
        {
            "ai_machine_host": host,
            "ai_machine_port": config.PROXY_PORT,
            "proxy_enabled": config.PROXY_ENABLED,
            "default_model": await db.setting_get("default_model") or config.MODEL_NAME,
            "fallback_model": await db.setting_get("fallback_model") or "",
            "version": config.VERSION.removeprefix("WebConsole_"),
            "session_ttl_s": session_ttl,
            "turn_timeout_s": turn_timeout,
            "prompt_max": prompt_max,
        }
    )


async def handle_settings_patch(request: Request):
    """PATCH /api/settings -- update runtime or app settings."""
    session = request.state.session
    data = await request.json()
    if "ai_machine_host" in data:
        host = data.get("ai_machine_host")
        if not isinstance(host, str):
            raise HTTPException(status_code=400, detail="AI machine host must be text")
        host = host.strip()
        if not _HOST_PATTERN.fullmatch(host):
            raise HTTPException(
                status_code=400, detail="Enter a valid hostname or IP address"
            )
        # SSRF protection: block internal IPs before persisting.
        _validate_host(host)
        await db.setting_set("ai_machine_host", host)
        _log.info("AI machine host updated by user=%s host=%s", session["user"], host)

    # Boot secrets – these live in the DB so the app can run without .env.
    boot_secrets = {
        "session_secret": ("session_secret", "Session secret must be at least 32 characters"),
        "projects_root": ("projects_root", "Projects root path is required"),
        "proxy_token": ("proxy_token", "Proxy token must be at least 32 characters"),
        "model_base_url": ("model_base_url", "Model base URL is required"),
        "model_api_key": ("model_api_key", None),  # optional, can be empty
    }
    for key, (db_key, error) in boot_secrets.items():
        if key in data:
            value = data[key]
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=400, detail=error)
            if key in ("session_secret", "proxy_token") and value is not None:
                value = value.strip()
                if len(value) < 32:
                    raise HTTPException(status_code=400, detail=error)
            elif key == "projects_root" and value is not None:
                value = value.strip()
                if not value:
                    raise HTTPException(status_code=400, detail=error)
                # Normalize path
                value = os.path.normpath(value)
            elif key == "model_base_url" and value is not None:
                value = value.strip()
                if not value:
                    raise HTTPException(status_code=400, detail=error)
            await db.setting_set(db_key, value.strip() if value is not None else None)

    for key in ("default_model", "fallback_model"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or len(value.strip()) > 100:
                raise HTTPException(
                    status_code=400, detail=f"{key} must be text up to 100 characters"
                )
            value = value.strip()
            if value and not _MODEL_RE.fullmatch(value):
                raise HTTPException(
                    status_code=400, detail=f"{key} contains invalid characters"
                )
            await db.setting_set(key, value)
    for key, default in (
        ("session_ttl", config.SESSION_TTL_S),
        ("turn_timeout", config.TURN_TIMEOUT_S),
        ("prompt_max", config.PROMPT_MAX_CHARS),
    ):
        if key in data:
            val = data[key]
            if not isinstance(val, int) or val < 30 or val > 86400:
                raise HTTPException(status_code=400, detail=f"{key} must be 30-86400")
            await db.setting_set(key, str(val))
    return JSONResponse(
        {"ok": True, "ai_machine_host": await db.setting_get("ai_machine_host")}
    )


_MACHINE_ALLOWED_FIELDS = {
    "name",
    "host",
    "port",
    "api_key",
    "model",
    "base_url",
    "description",
}
_MACHINE_PORT_RE = re.compile(r"^(?:0|[1-9]\d{0,4})$")
# Tightened: removed '/' to prevent downstream misinterpretation.
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_HOST_PATTERN_LOCAL = _HOST_PATTERN

# Allowed URL schemes for base_url validation.
_ALLOWED_URL_SCHEMES = {"http", "https"}


def _validate_base_url(base_url: str) -> str:
    """Validate and return the host part of *base_url*.

    Returns the resolved hostname/IP for SSRF checks.
    Raises HTTPException on invalid input.
    """
    # Must have a scheme.
    if "://" not in base_url:
        raise HTTPException(status_code=400, detail="Enter a valid base URL")
    scheme, rest = base_url.split("://", 1)
    if scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise HTTPException(
            status_code=400, detail="Only http and https schemes allowed"
        )
    # Extract the host:port before any path.
    host_port = rest.split("/")[0]
    # Strip port for host-only validation.
    if ":" in host_port and not host_port.startswith("["):
        host_part = host_port.rsplit(":", 1)[0]
    else:
        host_part = host_port
    if not _HOST_PATTERN_LOCAL.fullmatch(host_part):
        raise HTTPException(status_code=400, detail="Enter a valid base URL")
    return host_part


async def handle_machines_list(request: Request):
    """GET /api/machines -- list AI machines for the current user."""
    session = request.state.session
    machines = await db.ai_machines_list(session["user"])
    # Don't leak API keys in the listing
    return JSONResponse(
        {
            "machines": [
                {k: v for k, v in m.items() if k != "api_key"} for m in machines
            ],
        }
    )


async def handle_machine_get(request: Request, machine_id: str):
    """GET /api/machines/{id} -- get AI machine details."""
    session = request.state.session
    machine = await db.ai_machine_get(machine_id, session["user"])
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    m = {k: v for k, v in machine.items() if k != "api_key"}
    m["has_api_key"] = bool(machine.get("api_key"))
    return JSONResponse({"machine": m})


async def handle_machine_create(request: Request):
    """POST /api/machines -- create a new AI machine."""
    session = request.state.session
    data = await request.json()
    name = (data.get("name") or "").strip()[:100]
    host = (data.get("host") or "").strip()
    try:
        port = int(data.get("port", 9000))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Port must be a number")
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="Port must be 1-65535")
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not host:
        raise HTTPException(status_code=400, detail="Host is required")
    if not _HOST_PATTERN_LOCAL.fullmatch(host):
        raise HTTPException(
            status_code=400, detail="Enter a valid hostname or IP address"
        )
    # SSRF: block internal IPs on creation.
    _validate_host(host)
    model = (data.get("model") or "claude-sonnet-4-20250514").strip()
    if not _MODEL_RE.fullmatch(model):
        raise HTTPException(
            status_code=400, detail="Model name contains invalid characters"
        )
    base_url = (data.get("base_url") or "").strip() or None
    if base_url:
        base_url = _validate_base_url(base_url)
    api_key = (data.get("api_key") or "").strip() or None
    description = (data.get("description") or "").strip()[:500] or None
    machine_id = uuid.uuid4().hex
    await db.ai_machine_create(
        machine_id,
        name,
        host,
        port,
        api_key,
        model,
        base_url,
        description,
        session["user"],
    )
    _log.info("ai_machine created by user=%s name=%s", session["user"], name)
    return JSONResponse(
        {
            "ok": True,
            "id": machine_id,
            "name": name,
        }
    )


async def handle_machine_patch(request: Request, machine_id: str):
    """PATCH /api/machines/{id} -- update AI machine."""
    session = request.state.session
    data = await request.json()
    if not data or not set(data).issubset(_MACHINE_ALLOWED_FIELDS):
        raise HTTPException(status_code=400, detail="No valid fields to update")
    # Validate port
    if "port" in data and data["port"] is not None:
        try:
            p = int(data["port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Port must be a number")
        if p < 1 or p > 65535:
            raise HTTPException(status_code=400, detail="Port must be 1-65535")
        data["port"] = p
    # Validate host
    if "host" in data and data["host"] is not None:
        host = data["host"].strip()
        if not host:
            raise HTTPException(status_code=400, detail="Host is required")
        if not _HOST_PATTERN_LOCAL.fullmatch(host):
            raise HTTPException(
                status_code=400, detail="Enter a valid hostname or IP address"
            )
        _validate_host(host)
        data["host"] = host
    # Validate model
    if (
        "model" in data
        and data["model"] is not None
        and not _MODEL_RE.fullmatch(data["model"])
    ):
        raise HTTPException(
            status_code=400, detail="Model name contains invalid characters"
        )
    # Validate name
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()[:100]
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        data["name"] = name
    # Validate description
    if "description" in data and data["description"] is not None:
        data["description"] = data["description"][:500]
    # Validate base_url
    if "base_url" in data and data["base_url"] is not None:
        bu = data["base_url"].strip() or None
        if bu:
            bu = _validate_base_url(bu)
        data["base_url"] = bu
    # Clear api_key if explicitly None
    if "api_key" in data and data["api_key"] is not None:
        data["api_key"] = data["api_key"].strip() or None
    updated = await db.ai_machine_update(machine_id, session["user"], **data)
    if not updated:
        raise HTTPException(status_code=404, detail="Machine not found")
    _log.info("ai_machine updated by user=%s id=%s", session["user"], machine_id)
    return JSONResponse({"ok": True})


async def handle_machine_activate(request: Request, machine_id: str):
    """POST /api/machines/{id}/activate -- activate an AI machine."""
    session = request.state.session
    exists = await db.ai_machine_get(machine_id, session["user"])
    if not exists:
        raise HTTPException(status_code=404, detail="Machine not found")
    activated = await db.ai_machine_activate(machine_id, session["user"])
    _log.info("ai_machine activated by user=%s id=%s", session["user"], machine_id)
    return JSONResponse({"ok": True, "activated": activated})


async def handle_machine_delete(request: Request, machine_id: str):
    """DELETE /api/machines/{id} -- delete AI machine."""
    session = request.state.session
    deleted = await db.ai_machine_delete(machine_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Machine not found")
    _log.info("ai_machine deleted by user=%s id=%s", session["user"], machine_id)
    return JSONResponse({"ok": True})


async def handle_machine_test(request: Request, machine_id: str):
    """POST /api/machines/{id}/test -- test connection to AI machine."""
    session = request.state.session
    machine = await db.ai_machine_get(machine_id, session["user"])
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    host = machine["host"]
    port = machine["port"]
    try:
        # Resolve and validate before connecting.
        ip = _resolve_host(host)
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=5.0,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        return JSONResponse({"ok": True, "status": "reachable"})
    except HTTPException:
        raise  # re-raise validation errors (403/400) as-is
    except asyncio.TimeoutError:
        _log.warning("machine test timeout %s:%d", host, port)
        return JSONResponse(
            {"ok": False, "status": "unreachable", "error": "Connection timed out"},
            status_code=502,
        )
    except (OSError, ConnectionRefusedError) as exc:
        _log.warning("machine test failed %s:%d: %s", host, port, exc)
        return JSONResponse(
            {"ok": False, "status": "unreachable", "error": "Connection failed"},
            status_code=502,
        )


async def handle_sessions_list(request: Request):
    """GET /api/sessions -- list CLI sessions + Web chats for sidebar."""
    session = request.state.session
    cli_sessions = await db.read_claude_sessions()
    web_chats = await db.chat_list(session["user"])
    linked_session_ids = {
        chat.get("session_id") for chat in web_chats if chat.get("session_id")
    }
    cli_sessions = [
        item for item in cli_sessions if item.get("sessionId") not in linked_session_ids
    ]
    # Merge unlinked CLI sessions with WebConsole chats.
    items = cli_sessions + [
        {
            "id": c["id"],
            "name": c["title"],
            "cwd": c["work_dir"],
            "kind": "web",
            "startedAt": c["created_at"],
            "updatedAt": c["updated_at"],
            "sessionId": c.get("session_id", ""),
            "model": c.get("model") or "",
            "webchat": True,
        }
        for c in web_chats
    ]
    return JSONResponse({"sessions": items})


# hex-only session_id pattern for path-traversal protection.
_HEX_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def _sanitize_session_id(session_id: str) -> str:
    """Return a session_id after stripping path-traversal sequences.

    We do not reject the ID on sight — the work_dir is always rooted below
    PROJECTS_ROOT (``Path.mkdir(parents=True)`` only creates inside it) and
    the chat_id is a UUID, so there is no path-traversal risk even if the
    session_id string contained ``..``.  The previous regex was too
    restrictive; we now accept any non-empty string while still rejecting
    empty/null session IDs.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="Invalid session ID")
    return session_id


async def handle_sessions_resume(request: Request, session_id: str):
    """POST /api/sessions/{session_id}/resume -- open a WebConsole chat for a CLI session."""
    session = request.state.session
    session_id = _sanitize_session_id(session_id)
    available = await db.read_claude_sessions()
    source = next(
        (item for item in available if item.get("sessionId") == session_id), None
    )
    if source is None:
        raise HTTPException(status_code=404, detail="CLI session not found")

    existing = next(
        (
            chat
            for chat in await db.chat_list(session["user"])
            if chat.get("session_id") == session_id
        ),
        None,
    )
    if existing:
        return JSONResponse(
            {
                "id": existing["id"],
                "title": existing["title"],
                "session_id": session_id,
            }
        )

    # Create a new WebConsole chat linked to the CLI session
    chat_id = uuid.uuid4().hex
    title = f"CLI: {session_id[:12]}..."
    date_suffix = datetime.datetime.now(datetime.UTC).date().isoformat()
    work_dir = str(
        Path(config.PROJECTS_ROOT).resolve()
        / f"cli-import-{session_id[:8]}-{date_suffix}"
    )
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    await db.chat_create(chat_id, title, None, work_dir, session["user"])
    # Link the CLI session ID
    await db.chat_set_session(chat_id, session_id)
    # Write session file so CLI can see it too
    try:
        db.write_claude_session_file(session_id, title, work_dir)
    except (OSError, ValueError) as exc:
        _log.warning(
            "could not write CLI session file session_id=%s: %s", session_id, exc
        )

    return JSONResponse({"id": chat_id, "title": title, "session_id": session_id})


# ── Session routes ─────────────────────────────────────────────────────────────────


@app.get("/api/skills")
async def _api_skills_get(request: Request):
    return await handle_skills_get(request)


@app.get("/api/settings")
async def _api_settings_get(request: Request):
    return await handle_settings_get(request)


@app.patch("/api/settings")
async def _api_settings_patch(request: Request):
    return await handle_settings_patch(request)


@app.get("/api/sessions")
async def _api_sessions_list(request: Request):
    return await handle_sessions_list(request)


@app.post("/api/sessions/{session_id}/resume")
async def _api_sessions_resume(request: Request, session_id: str):
    return await handle_sessions_resume(request, session_id)


# ── Machine routes ─────────────────────────────────────────────────────────────────


@app.get("/api/machines")
async def _api_machines_list(request: Request):
    return await handle_machines_list(request)


@app.post("/api/machines")
async def _api_machine_create(request: Request):
    return await handle_machine_create(request)


@app.get("/api/machines/{machine_id}")
async def _api_machine_get(request: Request, machine_id: str):
    return await handle_machine_get(request, machine_id)


@app.patch("/api/machines/{machine_id}")
async def _api_machine_patch(request: Request, machine_id: str):
    return await handle_machine_patch(request, machine_id)


@app.post("/api/machines/{machine_id}/activate")
async def _api_machine_activate(request: Request, machine_id: str):
    return await handle_machine_activate(request, machine_id)


@app.post("/api/machines/{machine_id}/test")
async def _api_machine_test(request: Request, machine_id: str):
    return await handle_machine_test(request, machine_id)


@app.delete("/api/machines/{machine_id}")
async def _api_machine_delete(request: Request, machine_id: str):
    return await handle_machine_delete(request, machine_id)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    uvicorn.run("app:app", host=config.LISTEN_HOST, port=config.PORT, log_level="info")
