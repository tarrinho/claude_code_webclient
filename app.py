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
import re
import socket
import time
import urllib.error
import urllib.request
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
import transcripts

_log = logging.getLogger("wc.app")

_WEB_DIR: Final[Path] = Path(__file__).parent / "web"
_assets_dir: Final[Path] = _WEB_DIR / "assets"

# Skill discovery roots. Plain module attributes (not Final) so tests can patch
# them and never touch the real ~/.claude tree.
_USER_SKILLS_ROOT: Path = Path.home() / ".claude" / "skills"
_PLUGINS_ROOT: Path = Path.home() / ".claude" / "plugins"

# Directory names accepted as skill / plugin identifiers.
_SKILL_DIR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
# Upper bound on skills returned, so a pathological tree cannot blow up the response.
_SKILL_LIMIT: Final[int] = 500
# Longest one-line summary shown on a collapsed skill card.
_SKILL_SUMMARY_MAX: Final[int] = 120

# Hostname regex: labels, full IPv4, or bracketed IPv6.
_HOST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|"
    r"\[[0-9A-Fa-f:]+\]|[0-9A-Fa-f:]+)$"
)

# Blocked private / reserved IP ranges for SSRF protection.
_BLOCKED_NETS: Final[list[IPv4Network | IPv6Network]] = [
    ip_network("0.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("100.64.0.0/10"),  # CG-NAT (includes the Tailscale range)
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


def _parse_allow_nets() -> list[IPv4Network | IPv6Network]:
    """Ranges that stay reachable despite _BLOCKED_NETS.

    An AI machine is normally *meant* to live on the operator's own network:
    a tailnet peer (100.64.0.0/10), a LAN box (RFC1918), or the local proxy on
    loopback. Blanket-blocking those rejects the product's intended topology,
    and the endpoints that accept a host are admin-gated or authenticated, so
    the operator is not the threat here.

    What stays blocked is what an operator would never legitimately target:
    169.254.0.0/16 (link-local, including the cloud metadata endpoint),
    0.0.0.0/8, the TEST-NET and benchmark ranges, multicast, reserved space
    and IPv6 ULA / link-local. Tighten with WC_SSRF_ALLOW_NETS, which replaces
    this list wholesale.
    """
    raw = config._str(
        "WC_SSRF_ALLOW_NETS",
        "100.64.0.0/10,127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    )
    nets: list[IPv4Network | IPv6Network] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ip_network(item))
        except ValueError:
            _log.warning("ignoring invalid WC_SSRF_ALLOW_NETS entry: %s", item)
    return nets


_ALLOWED_NETS: Final[list[IPv4Network | IPv6Network]] = _parse_allow_nets()


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
                _ip = "?"
                if hasattr(request, "client") and request.client:
                    _ip = getattr(request.client, "host", "?") or "?"
                _log.warning(
                    "session_expired_or_invalid: path=%s ip=%s "
                    "(session may have expired or been revoked) — "
                    "log in again at /login",
                    request.url.path, _ip,
                )
                return JSONResponse(
                    status_code=401,
                    content={"error": "Session expired", "redirect": "/login"},
                )
            return RedirectResponse(url="/login", status_code=303)
        return await handler(request)


# ── Helpers ────────────────────────────────────────────────────────────────────────


def _trusted_proxies() -> list[IPv4Network | IPv6Network]:
    """Parse WC_TRUSTED_PROXIES into networks. Empty (the default) means none."""
    raw = config._str("WC_TRUSTED_PROXIES", "") or ""
    nets: list[IPv4Network | IPv6Network] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ip_network(part, strict=False))
        except ValueError:
            _log.warning("ignoring invalid WC_TRUSTED_PROXIES entry %r", part)
    return nets


def _client_ip(request: Request) -> str:
    """Return the client address to attribute a request to.

    Uses the forwarded header **only** when the immediate peer is a configured
    trusted proxy. Reading it unconditionally would be worse than ignoring it:
    with the app exposed directly -- which is how it runs today, uvicorn holding
    :443 itself -- any client could rotate X-Real-IP and walk straight through
    the login rate limit.
    """
    peer = getattr(getattr(request, "client", None), "host", None) or "unknown"
    trusted = _trusted_proxies()
    if not trusted or peer == "unknown":
        return peer
    try:
        peer_addr = ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_addr in net for net in trusted):
        return peer
    headers = getattr(request, "headers", None) or {}
    for header in ("x-real-ip", "x-forwarded-for"):
        value = headers.get(header) or ""
        # X-Forwarded-For is a chain; the left-most entry is the origin client.
        candidate = value.split(",")[0].strip()
        if not candidate:
            continue
        try:
            ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return peer


def _is_private_ip(host: str) -> bool:
    """Return True if *host* is a blocked address and not explicitly allowed."""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # strip IPv6 brackets for parsing
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    if any(addr in net for net in _ALLOWED_NETS):
        return False
    return any(addr in net for net in _BLOCKED_NETS)


def _validate_host(host: str) -> str:
    """Validate *host* and confirm it does not resolve to a blocked address.

    Returns the resolved IP. Every call site persists a user-supplied host
    under a comment claiming SSRF protection, but this only pattern-matched
    the string and never resolved anything, so nothing was ever blocked. It
    now applies the blocklist at the point the value enters the system.
    """
    if not host or not isinstance(host, str) or not _HOST_PATTERN.match(host):
        raise HTTPException(
            status_code=400, detail="Enter a valid hostname or IP address"
        )
    return _resolve_host(host)


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
            # Pass the session id so the token is checked against *this*
            # session rather than any live one.
            if (
                not cookie_token
                or not header_token
                or not auth._csrf_valid(
                    cookie_token, header_token, request.cookies.get("wc_session")
                )
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
            # CSP – emitted unconditionally. This used to be gated on a
            # per-request nonce that nothing ever set, so in practice no CSP
            # shipped at all. Both templates load only external scripts
            # (index.html, login.html) and carry no inline handlers, so
            # script-src 'self' covers them without any nonce plumbing.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; "
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


def _usage_provider(machine: dict | None) -> str:
    """Classify a machine for usage accounting. See _record_turn_usage."""
    if not machine or machine.get("provider") != "anthropic":
        return "proxy"
    base_url = (machine.get("base_url") or "").strip()
    if not base_url:
        # No override means the CLI's own default: the official API.
        return "anthropic"
    host = base_url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
    return "anthropic" if host in ("api.anthropic.com", "") else "anthropic-compatible"


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
    if not frame:
        return
    models = frame.get("models") or {}
    if not models:
        return
    try:
        machine = await db.ai_machine_active(owner)
    except Exception:  # noqa: BLE001 -- accounting must not break a live turn
        machine = None
    provider = _usage_provider(machine)
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
            duration_ms=frame.get("duration_ms"),
            is_error=bool(frame.get("is_error")),
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

    try:
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
    if session_id and session_id != chat["session_id"]:
        await db.chat_set_session(chat_id, session_id)
    model = runner.take_last_model(chat_id)
    if model and model != chat.get("model"):
        await db.chat_set_model(chat_id, model)
    await _record_turn_usage(chat_id, session["user"], runner.take_last_usage(chat_id))
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
                elif event_type == "usage":
                    # Recorded as it arrives: the tokens were spent whether or
                    # not the rest of the turn completes.
                    await _record_turn_usage(chat_id, session["user"], event)
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
        return HTMLResponse((_WEB_DIR / "login.html").read_text())
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


def _skill_description(text: str) -> str:
    """Pull the description out of a SKILL.md.

    Prefers the YAML frontmatter key, following wrapped continuation lines so a
    folded description is not truncated at the first newline. Falls back to a
    bare ``description:`` line anywhere near the top of the file.
    """
    lines = text.splitlines()
    body = lines
    # Restrict to the frontmatter block when one is present.
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = lines[1:index]
                break
    parts: list[str] = []
    for index, line in enumerate(body[:80]):
        if not line.lower().startswith("description:"):
            continue
        first = line.split(":", 1)[1].strip()
        # A folded / literal block scalar ("description: >-") carries no value on
        # the key line; the marker itself must not leak into the description.
        if not re.fullmatch(r"[>|][+-]?\d*", first):
            parts.append(first)
        # Consume indented / unkeyed continuation lines of a wrapped value.
        for follow in body[index + 1 :]:
            if not follow.strip():
                break
            if re.match(r"^[A-Za-z0-9_-]+\s*:", follow):
                break
            parts.append(follow.strip())
        break
    value = " ".join(part for part in parts if part).strip()
    # YAML scalars are often quoted; the quotes are not part of the value.
    for quote in ('"', "'"):
        if len(value) > 1 and value.startswith(quote) and value.endswith(quote):
            value = value[1:-1].strip()
            break
    return value[:500]


def _skill_summary(description: str) -> str:
    """Condense a description to a single short line for the collapsed card."""
    text = " ".join(description.split())
    # Descriptions are model-facing prose and often carry Markdown emphasis;
    # strip the markers so the summary reads as plain text. Underscores are only
    # treated as emphasis at word boundaries, to keep snake_case identifiers.
    text = text.replace("`", "")
    text = re.sub(r"\*{1,2}(\S(?:.*?\S)?)\*{1,2}", r"\1", text)
    text = re.sub(
        r"(?<![A-Za-z0-9_])_{1,2}(\S(?:.*?\S)?)_{1,2}(?![A-Za-z0-9_])", r"\1", text
    ).strip()
    if not text:
        return ""
    # Prefer a sentence boundary if one falls inside the budget.
    match = re.search(rf"^(.{{20,{_SKILL_SUMMARY_MAX}}}?[.!?])(?:\s|$)", text)
    if match:
        return match.group(1)
    if len(text) <= _SKILL_SUMMARY_MAX:
        return text
    clipped = text[:_SKILL_SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}…"


def _read_skill(directory: Path, name: str, source: str, source_label: str) -> dict | None:
    """Build one skill entry from a skill directory, or None if unreadable."""
    try:
        text = (directory / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    description = _skill_description(text)
    return {
        "name": name,
        "description": description,
        "summary": _skill_summary(description),
        "source": source,
        "source_label": source_label,
        "installed": True,
    }


def _iter_skill_dirs(root: Path):
    """Yield validated skill subdirectories of ``root``, sorted by name."""
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except (OSError, PermissionError):
        return
    for entry in entries:
        if not entry.is_dir() or not _SKILL_DIR_PATTERN.fullmatch(entry.name):
            continue
        yield entry


def _discover_user_skills() -> list[dict]:
    """Skills the user authored under ~/.claude/skills."""
    found = []
    for entry in _iter_skill_dirs(_USER_SKILLS_ROOT):
        skill = _read_skill(entry, entry.name, "user", "Your skills")
        if skill:
            found.append(skill)
    return found


def _discover_plugin_skills() -> list[dict]:
    """Skills provided by installed plugins.

    ``installed_plugins.json`` is the source of truth for what is actually
    installed -- walking the plugin cache directly would also surface
    marketplace checkouts and stale versions the user never installed.
    """
    manifest_path = _PLUGINS_ROOT / "installed_plugins.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return []

    try:
        plugins_root = _PLUGINS_ROOT.resolve()
    except OSError:
        return []

    found: list[dict] = []
    seen: set[str] = set()
    for key, installs in sorted(plugins.items()):
        plugin = str(key).split("@", 1)[0]
        if not _SKILL_DIR_PATTERN.fullmatch(plugin) or not isinstance(installs, list):
            continue
        for install in installs:
            if not isinstance(install, dict):
                continue
            raw_path = install.get("installPath")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            try:
                skills_root = (Path(raw_path) / "skills").resolve()
            except OSError:
                continue
            # Only read inside the plugins tree, whatever the manifest claims.
            if plugins_root not in skills_root.parents or not skills_root.is_dir():
                continue
            for entry in _iter_skill_dirs(skills_root):
                name = f"{plugin}:{entry.name}"
                if name in seen:
                    continue
                skill = _read_skill(entry, name, f"plugin:{plugin}", plugin)
                if skill:
                    seen.add(name)
                    found.append(skill)
    return found


async def handle_skills_get(request: Request):
    """GET /api/skills -- list installed skills and session activity."""
    session = request.state.session
    session_id = request.query_params.get("session_id")
    if not session_id:
        chat_id = request.query_params.get("chat_id")
        if chat_id:
            chat = await db.chat_get(chat_id, session["user"])
            session_id = chat.get("session_id") if chat else None

    skills = (_discover_user_skills() + _discover_plugin_skills())[:_SKILL_LIMIT]

    # A session records skills by bare name; match those against both the bare
    # name and the namespaced plugin name.
    active = set(runner.active_skills(session_id))
    for skill in skills:
        bare = skill["name"].split(":", 1)[-1]
        skill["active"] = skill["name"] in active or bare in active

    # Group order follows the list: user skills first, then plugins A-Z.
    sources: list[dict] = []
    by_source: dict[str, dict] = {}
    for skill in skills:
        group = by_source.get(skill["source"])
        if group is None:
            group = {
                "id": skill["source"],
                "label": skill["source_label"],
                "count": 0,
                "active_count": 0,
            }
            by_source[skill["source"]] = group
            sources.append(group)
        group["count"] += 1
        group["active_count"] += 1 if skill["active"] else 0

    return JSONResponse(
        {
            "skills": skills,
            "sources": sources,
            "total": len(skills),
            "active_count": sum(1 for skill in skills if skill["active"]),
            "session_id": session_id or "",
        }
    )


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


async def handle_usage_get(request: Request):
    """GET /api/usage -- per-model token totals and a recent-turn log.

    Scoped to the signed-in user's own rows. Like the settings GET, readable by
    any authenticated user rather than admin-only, and for the same reason: it
    is their own data and carries no secret.

    ``cost_usd`` is reported only for ``provider='anthropic'`` rows. Claude Code
    prices every turn with Anthropic's rates, so the figure is meaningless for a
    self-hosted or third-party gateway; ``cost_note`` tells the client why the
    value is absent so the UI can explain the blank rather than just show one.
    """
    session = request.state.session
    owner = session["user"]

    raw_days = request.query_params.get("days", "30")
    days: int | None
    if raw_days in ("all", "0", ""):
        days = None
    else:
        try:
            days = max(1, min(int(raw_days), 3650))
        except (TypeError, ValueError):
            days = 30
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
    except (TypeError, ValueError):
        limit = 50

    totals = await db.usage_totals(owner, days)
    for row in totals:
        if row.get("provider") != "anthropic":
            row["cost_usd"] = None
            row["cost_note"] = "Priced with Anthropic rates; not meaningful for this backend."
    recent = await db.usage_recent(owner, limit)
    for row in recent:
        if row.get("provider") != "anthropic":
            row["cost_usd"] = None

    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "retention_days": config.USAGE_RETENTION_DAYS,
            "overall": await db.usage_overall(owner, days),
            "totals": totals,
            "recent": recent,
        }
    )


async def handle_settings_get(request: Request):
    """GET /api/settings -- return non-secret runtime and app settings.

    Deliberately readable by any authenticated user, unlike the PATCH
    counterpart, which is admin-only: the frontend reads it on every page load
    to render the version and the settings form. Every value below must
    therefore stay non-sensitive -- never add a secret, key, or token here.
    """
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
            "version": config.VERSION.removeprefix("WebConsole_"),
            "session_ttl_s": session_ttl,
            "turn_timeout_s": turn_timeout,
            "prompt_max": prompt_max,
        }
    )


async def handle_settings_patch(request: Request):
    """PATCH /api/settings -- update runtime or app settings.

    Admin-only: this endpoint writes the session secret, the proxy token, the
    model API key and projects_root. projects_root is the sandbox boundary
    that runner.py validates every work_dir against, so write access here is
    equivalent to choosing where Claude may run.
    """
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
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
                value = _validate_projects_root(value)
            elif key == "model_base_url" and value is not None:
                value = value.strip()
                if not value:
                    raise HTTPException(status_code=400, detail=error)
            await db.setting_set(db_key, value.strip() if value is not None else None)

    # fallback_model was accepted and stored here but never read by anything --
    # a settings control implying a retry behaviour that did not exist. Removed
    # rather than left dead; the per-machine default is the real control now.
    for key in ("default_model",):
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
    "provider",
    "host",
    "port",
    "api_key",
    "model",
    "base_url",
    "description",
}
# Fields that must be a string (or null) when present in a machine PATCH.
_MACHINE_TEXT_FIELDS = (
    "name",
    "provider",
    "host",
    "api_key",
    "model",
    "base_url",
    "description",
)

# How a machine is reached. 'anthropic' is the official API -- what Claude Code
# talks to out of the box; 'proxy' is a host running claude_proxy.py.
_MACHINE_PROVIDERS = {"anthropic", "proxy"}
_ANTHROPIC_PORT = 443
# Required on every Anthropic API request; used for the probe and the model list.
_ANTHROPIC_API_VERSION = "2023-06-01"
# The model list comes from a user-configured endpoint, so cap what we read.
_MODELS_BODY_MAX = 1_048_576
# Opening Settings should not re-query the endpoint on every render.
_MODELS_CACHE_TTL_S = 60.0
_models_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_MACHINE_PORT_RE = re.compile(r"^(?:0|[1-9]\d{0,4})$")
# Square brackets are allowed for the documented "[1m]" context-window suffix
# (e.g. "claude-opus-5[1m]"), which the CLI itself tells users to append. The
# value is passed to the subprocess as a single argv entry, never through a
# shell, so the brackets carry no meaning downstream.
_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/\[\]-]+$")
_HOST_PATTERN_LOCAL = _HOST_PATTERN

# Allowed URL schemes for base_url validation.
_ALLOWED_URL_SCHEMES = {"http", "https"}


def _base_url_host(base_url: str) -> str:
    """Return the host portion of *base_url*, for host-level checks.

    Raises HTTPException if the URL is not a well-formed http/https URL.
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


def _validate_base_url(base_url: str) -> str:
    """Validate *base_url* and return it unchanged (minus surrounding space).

    This is a validator, not a transformer: the caller persists the value it
    passes in, so returning only the host would silently discard the scheme,
    port and path. Use :func:`_base_url_host` when the host alone is wanted.
    """
    base_url = base_url.strip()
    if len(base_url) > 500:
        raise HTTPException(status_code=400, detail="Base URL is too long")
    _base_url_host(base_url)  # raises on a malformed or non-http(s) URL
    return base_url


def _validate_projects_root(value: str) -> str:
    """Confirm a candidate projects_root is a safe workspace parent.

    runner.py validates every work_dir against this path, so an unconstrained
    value relocates the sandbox -- pointing it at "/" would let a conversation
    workspace be created anywhere and run Claude there with
    --dangerously-skip-permissions. Constrain it to a real directory under the
    server account's home, or set WC_PROJECTS_ROOT_BASE to widen that.
    """
    base = Path(config._str("WC_PROJECTS_ROOT_BASE", str(Path.home())) or "/").resolve()
    try:
        candidate = Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Projects root path is invalid")
    if not candidate.is_absolute() or not candidate.is_relative_to(base):
        raise HTTPException(
            status_code=400,
            detail=f"Projects root must be an absolute path under {base}",
        )
    if not candidate.is_dir():
        raise HTTPException(
            status_code=400, detail="Projects root must be an existing directory"
        )
    return str(candidate)


async def handle_machines_list(request: Request):
    """GET /api/machines -- list AI machines for the current user."""
    session = request.state.session
    # Claude Code's native backend should always be on offer, so materialise it
    # for accounts created before the provider column existed.
    await db.ai_machine_seed_anthropic(session["user"])
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
    # ai_machine_get does not select api_key, so this reads the flag the query
    # derives instead; deriving it from the absent column was always false.
    m["has_api_key"] = bool(machine.get("has_api_key"))
    return JSONResponse({"machine": m})


async def handle_machine_create(request: Request):
    """POST /api/machines -- create a new AI machine."""
    session = request.state.session
    data = await request.json()
    name = (data.get("name") or "").strip()[:100]
    provider = (data.get("provider") or "proxy").strip()
    if provider not in _MACHINE_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown provider")
    base_url = (data.get("base_url") or "").strip() or None
    host = (data.get("host") or "").strip()
    if provider == "anthropic":
        # The endpoint is the transport, so derive host/port from it rather
        # than asking for them twice and letting the two disagree.
        base_url = base_url or config.ANTHROPIC_BASE_URL
        host = host or _base_url_host(base_url)
        data.setdefault("port", _ANTHROPIC_PORT)
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
    default_model = (
        config.ANTHROPIC_MODEL if provider == "anthropic" else config.MODEL_NAME
    )
    model = (data.get("model") or default_model).strip()
    if not _MODEL_RE.fullmatch(model):
        raise HTTPException(
            status_code=400, detail="Model name contains invalid characters"
        )
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
        provider=provider,
    )
    _log.info(
        "ai_machine created by user=%s name=%s provider=%s",
        session["user"],
        name,
        provider,
    )
    return JSONResponse(
        {
            "ok": True,
            "id": machine_id,
            "name": name,
            "provider": provider,
        }
    )


async def handle_machine_patch(request: Request, machine_id: str):
    """PATCH /api/machines/{id} -- update AI machine."""
    session = request.state.session
    data = await request.json()
    if not data or not set(data).issubset(_MACHINE_ALLOWED_FIELDS):
        raise HTTPException(status_code=400, detail="No valid fields to update")
    # Reject non-string text fields up front: the validators below call .strip()
    # and regex methods that would otherwise raise and surface as a 500.
    for field in _MACHINE_TEXT_FIELDS:
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise HTTPException(status_code=400, detail=f"{field} must be text or null")
    # Validate port
    if "port" in data and data["port"] is not None:
        try:
            p = int(data["port"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Port must be a number")
        if p < 1 or p > 65535:
            raise HTTPException(status_code=400, detail="Port must be 1-65535")
        data["port"] = p
    # Validate provider
    if "provider" in data and data["provider"] is not None:
        prov = data["provider"].strip()
        if prov not in _MACHINE_PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        data["provider"] = prov
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


def _probe_anthropic(url: str, api_key: str | None) -> tuple[int, bytes]:
    """GET *url* and return (status, body). Runs in a worker thread.

    The body is capped: it comes from a user-configured endpoint, so an
    unbounded read would let a hostile or broken one exhaust memory.
    """
    headers = {"anthropic-version": _ANTHROPIC_API_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310: scheme checked
            return resp.status, resp.read(_MODELS_BODY_MAX)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


async def _test_anthropic_endpoint(machine: dict, api_key: str | None):
    """Probe the API itself, rather than only opening a TCP socket.

    A bare connect reports "reachable" for an endpoint that rejects every turn
    -- wrong key, wrong URL -- which reads as "this machine works". Asking
    /v1/models separates reachable, unauthenticated and broken.
    """
    base_url = runner.normalise_base_url(machine.get("base_url")) or (
        config.ANTHROPIC_BASE_URL
    )
    host = _base_url_host(base_url)
    # Same SSRF blocklist the transport path applies before connecting out.
    _resolve_host(host)
    url = f"{base_url}/v1/models"
    try:
        status, _body = await asyncio.wait_for(
            asyncio.to_thread(_probe_anthropic, url, api_key), timeout=10.0
        )
    except (asyncio.TimeoutError, TimeoutError):
        _log.warning("anthropic probe timeout %s", host)
        return JSONResponse(
            {"ok": False, "status": "unreachable", "error": "Connection timed out"},
            status_code=502,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("anthropic probe failed %s: %s", host, exc)
        return JSONResponse(
            {"ok": False, "status": "unreachable", "error": "Connection failed"},
            status_code=502,
        )
    if status == 200:
        return JSONResponse({"ok": True, "status": "reachable"})
    if status in (401, 403):
        detail = (
            "Endpoint rejected the API key"
            if api_key
            else "Endpoint requires an API key"
        )
        return JSONResponse(
            {"ok": False, "status": "auth_failed", "error": detail}, status_code=502
        )
    return JSONResponse(
        {"ok": False, "status": "error", "error": f"Endpoint returned HTTP {status}"},
        status_code=502,
    )


async def handle_machine_test(request: Request, machine_id: str):
    """POST /api/machines/{id}/test -- test connection to AI machine."""
    session = request.state.session
    machine = await db.ai_machine_get(machine_id, session["user"])
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    if machine.get("provider") == "anthropic":
        api_key = await db.ai_machine_api_key(machine_id, session["user"])
        return await _test_anthropic_endpoint(machine, api_key)
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


def _parse_model_list(body: bytes) -> list[dict[str, str]]:
    """Pull model ids out of a /v1/models response.

    Anthropic returns {"data": [{"id", "display_name", ...}]} and an
    OpenAI-compatible gateway returns {"data": [{"id", ...}]}, so the same
    shape covers both. Entries without an id are skipped rather than rendered
    as blanks.
    """
    payload = json.loads(body.decode("utf-8", errors="replace"))
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise TypeError("response has no model list")
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()[:200]
        if model_id in seen:
            continue
        seen.add(model_id)
        display = entry.get("display_name")
        models.append(
            {
                "id": model_id,
                "display_name": (
                    display.strip()[:200]
                    if isinstance(display, str) and display.strip()
                    else model_id
                ),
            }
        )
    models.sort(key=lambda m: m["id"])
    return models


def _machine_model_selection(machine: dict | None) -> dict:
    """The active/default selection to report alongside a model list."""
    if not machine:
        return {"machine_id": None, "active": [], "default": ""}
    return {
        "machine_id": machine["id"],
        # Empty means "everything served is offered" -- the UI renders that as
        # all-checked rather than none, so the feature stays opt-in.
        "active": db.parse_active_models(machine.get("active_models")),
        "default": (machine.get("model") or "").strip(),
    }


def _builtin_models(
    reason: str, endpoint: str | None = None, machine: dict | None = None
) -> JSONResponse:
    """Fall back to the ids shipped with the app, saying why.

    The page previously showed a hardcoded list with no indication that it was
    a guess, so a model the service does not serve looked identical to one it
    does. The reason is surfaced instead of hidden.
    """
    return JSONResponse(
        {
            "models": [{"id": m, "display_name": m} for m in config.KNOWN_MODELS],
            "source": "builtin",
            "endpoint": endpoint,
            "reason": reason,
            **_machine_model_selection(machine),
        }
    )


async def handle_models_list(request: Request):
    """GET /api/models -- models a machine actually serves.

    Defaults to the active machine. ``?machine_id=`` inspects another one
    without activating it, so choosing which models a backend offers does not
    require making it live first.
    """
    session = request.state.session
    machine_id = (request.query_params.get("machine_id") or "").strip()
    if machine_id:
        machine = await db.ai_machine_get(machine_id, session["user"])
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")
    else:
        machine = await db.ai_machine_active(session["user"])
    if not machine:
        return _builtin_models("No machine is active.")
    if machine.get("provider") != "anthropic":
        return _builtin_models(
            "This is a Claude Code proxy, which does not publish a model list.",
            None,
            machine,
        )
    base_url = runner.normalise_base_url(machine.get("base_url")) or (
        config.ANTHROPIC_BASE_URL
    )
    now = time.monotonic()
    cached = _models_cache.get(base_url)
    if cached and now - cached[0] < _MODELS_CACHE_TTL_S:
        return JSONResponse(
            {
                "models": cached[1],
                "source": "endpoint",
                "endpoint": base_url,
                "reason": None,
                **_machine_model_selection(machine),
            }
        )
    host = _base_url_host(base_url)
    # Same SSRF blocklist the transport path applies before connecting out.
    _resolve_host(host)
    api_key = await db.ai_machine_api_key(machine["id"], session["user"])
    # limit is Anthropic's page size; an OpenAI-compatible gateway ignores it
    # and returns everything anyway.
    url = f"{base_url}/v1/models?limit=1000"
    try:
        status, body = await asyncio.wait_for(
            asyncio.to_thread(_probe_anthropic, url, api_key), timeout=10.0
        )
    except (asyncio.TimeoutError, TimeoutError):
        _log.warning("model list timeout %s", host)
        return _builtin_models("The endpoint timed out.", base_url, machine)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("model list failed %s: %s", host, exc)
        return _builtin_models("Could not reach the endpoint.", base_url, machine)
    if status in (401, 403):
        return _builtin_models(
            "The endpoint rejected the API key."
            if api_key
            else "The endpoint requires an API key.",
            base_url,
            machine,
        )
    if status != 200:
        return _builtin_models(f"The endpoint returned HTTP {status}.", base_url, machine)
    try:
        models = _parse_model_list(body)
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        _log.warning("model list unparseable from %s", host)
        return _builtin_models("The endpoint returned an unreadable list.", base_url, machine)
    if not models:
        return _builtin_models("The endpoint listed no models.", base_url, machine)
    _models_cache[base_url] = (now, models)
    return JSONResponse(
        {
            "models": models,
            "source": "endpoint",
            "endpoint": base_url,
            "reason": None,
            **_machine_model_selection(machine),
        }
    )


async def handle_machine_models_set(request: Request, machine_id: str):
    """PUT /api/machines/{id}/models -- choose which models this machine offers.

    Deliberately its own route rather than a field on PATCH /api/machines: that
    handler rejects the whole body if any key falls outside its allowlist, and
    the shape of that allowlist is still unsettled.

    The selection only decides what the picker shows. A turn naming a model
    outside it is still executed -- an old conversation whose model was later
    deactivated must keep working, and a gateway will accept ids it does not
    advertise.
    """
    session = request.state.session
    machine = await db.ai_machine_get(machine_id, session["user"])
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found")
    data = await request.json()

    raw_active = data.get("active", [])
    if not isinstance(raw_active, list):
        raise HTTPException(status_code=400, detail="active must be a list of models")
    active: list[str] = []
    for entry in raw_active:
        if not isinstance(entry, str):
            raise HTTPException(status_code=400, detail="Model ids must be text")
        entry = entry.strip()
        if not entry:
            continue
        if not _MODEL_RE.fullmatch(entry):
            raise HTTPException(
                status_code=400, detail="Model name contains invalid characters"
            )
        if entry not in active:
            active.append(entry[:200])

    default = data.get("default")
    if default is not None and not isinstance(default, str):
        raise HTTPException(status_code=400, detail="default must be text or null")
    default = (default or "").strip()
    if default:
        if not _MODEL_RE.fullmatch(default):
            raise HTTPException(
                status_code=400, detail="Model name contains invalid characters"
            )
        # A default outside the offered set would be unreachable in the picker
        # while still being applied to every new chat.
        if active and default not in active:
            raise HTTPException(
                status_code=400, detail="The default model must be one of the active models"
            )
        default = default[:200]

    await db.ai_machine_set_models(machine_id, session["user"], active, default or None)
    _log.info(
        "machine models set by user=%s id=%s active=%d default=%s",
        session["user"],
        machine_id,
        len(active),
        default or "(unchanged)",
    )
    return JSONResponse({"ok": True, "active": active, "default": default})


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
    """Validate a session_id, rejecting anything usable for path traversal.

    An earlier version accepted any non-empty string, on the stated grounds
    that ``Path.mkdir(parents=True)`` can only create inside PROJECTS_ROOT.
    That is not true: ``handle_sessions_resume`` interpolates ``session_id[:8]``
    into a directory name, so an id of ``a/../../`` yields a work_dir that
    resolves *above* the root. A payload of only ``..`` does cancel out against
    the date suffix, which is likely why the invariant looked safe.

    Real ids are UUIDs, so the charset below is not restrictive in practice,
    and it matches the guard already enforced by
    ``db.write_claude_session_file``.
    """
    if not session_id or not _HEX_SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID")
    return session_id


def _adopt_session_cwd(source_cwd: str | None, session_id: str) -> str:
    """Pick the work_dir for a chat adopting a CLI session.

    work_dir is the directory Claude actually runs in, so a resumed
    conversation that gets a freshly minted ``cli-import-<id>`` folder keeps
    its whole history but lands somewhere empty, unable to read the files it
    was just discussing. Adopting the terminal's own cwd fixes that.

    The transcript is not the reason: a session stays anchored to wherever it
    was created and keeps appending there no matter which directory it is
    resumed from, so nothing forks either way.

    The cwd still has to sit inside PROJECTS_ROOT -- runner.py rejects
    anything outside it before spawning a turn, and this must not be the hole
    in that boundary. Anything missing or outside falls back to the old
    scratch directory, which is worse but always valid.
    """
    root = Path(config.PROJECTS_ROOT).resolve()
    candidate: Path | None = None
    if source_cwd and source_cwd.strip():
        try:
            candidate = Path(source_cwd.strip()).resolve()
        except (OSError, RuntimeError):
            candidate = None

    if candidate and candidate.is_dir() and candidate.is_relative_to(root):
        return str(candidate)

    if candidate:
        _log.info(
            "cli_session_cwd_not_adopted: cwd=%s (missing, or outside "
            "PROJECTS_ROOT=%s) — falling back to a scratch workspace",
            candidate, root,
        )
    date_suffix = datetime.datetime.now(datetime.UTC).date().isoformat()
    fallback = root / f"cli-import-{session_id[:8]}-{date_suffix}"
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


async def handle_sessions_resume(request: Request, session_id: str):
    """POST /api/sessions/{session_id}/resume -- open a WebConsole chat for a CLI session."""
    session = request.state.session
    session_id = _sanitize_session_id(session_id)
    available = await db.read_claude_sessions()
    source = next(
        (item for item in available if item.get("sessionId") == session_id), None
    )
    if source is None:
        # ~/.claude/sessions only lists sessions that are still running, so a
        # finished conversation is absent from it while its transcript lives on.
        # Fall back to the transcript, which records the cwd the session ran in,
        # so any past conversation can be reopened -- not just a live terminal.
        cwd = await transcripts.session_cwd(session_id)
        if cwd:
            source = {"sessionId": session_id, "cwd": cwd}
        else:
            _log.error(
                "cli_session_not_found: user=%s session_id=%s "
                "(no running session and no transcript on disk) — "
                "ensure claude-code is running, or that the conversation "
                "exists under the projects directory",
                session["user"], session_id,
            )
            raise HTTPException(
                status_code=404,
                detail="Conversation not found — no running session and no transcript",
            )

    existing = next(
        (
            chat
            for chat in await db.chat_list(session["user"])
            if chat.get("session_id") == session_id
        ),
        None,
    )
    if existing:
        # Backfill a chat resumed before the import existed, which would
        # otherwise stay permanently empty. Guarded on the chat having no
        # messages so a conversation continued here is never duplicated.
        if not await db.messages_get(existing["id"]):
            await _import_transcript(existing["id"], session_id)
        return JSONResponse(
            {
                "id": existing["id"],
                "title": existing["title"],
                "session_id": session_id,
            }
        )

    # Create a new WebConsole chat linked to the CLI session
    chat_id = uuid.uuid4().hex
    # Prefer a name a human would recognise: the terminal's own session name,
    # else the conversation's opening prompt. "CLI: 5bfd4035-b6d..." tells the
    # reader nothing about which conversation it is.
    title = (
        (source.get("name") or "").strip()
        or (await transcripts.session_title(session_id)).strip()
        or f"CLI: {session_id[:12]}..."
    )[:200]
    work_dir = _adopt_session_cwd(source.get("cwd"), session_id)
    await db.chat_create(chat_id, title, None, work_dir, session["user"])
    # Link the CLI session ID
    await db.chat_set_session(chat_id, session_id)
    # Seed the chat with the conversation already on disk, so it opens where
    # the terminal left off rather than blank.
    imported = await _import_transcript(chat_id, session_id)
    # Write session file so CLI can see it too
    try:
        db.write_claude_session_file(session_id, title, work_dir)
    except (OSError, ValueError) as exc:
        _log.warning(
            "could not write CLI session file session_id=%s: %s", session_id, exc
        )

    return JSONResponse(
        {
            "id": chat_id,
            "title": title,
            "session_id": session_id,
            "imported_messages": imported,
        }
    )


def _turn_to_message(turn: dict) -> tuple[str, str] | None:
    """Flatten one transcript turn into a (role, content) message row.

    The messages table holds a role and a body, with nowhere to record that a
    turn came from a subagent, so sidechain traffic is dropped rather than
    replayed unlabelled among the user's own turns -- the transcript viewer
    already shows it marked. Thinking blocks are dropped for the same reason:
    the terminal collapses them, so replaying them inline would show more than
    the conversation the user actually saw.
    """
    if turn.get("sidechain"):
        return None
    parts: list[str] = []
    for block in turn.get("blocks") or []:
        kind, text = block.get("kind"), (block.get("text") or "").strip()
        if not text:
            continue
        if kind == "text":
            parts.append(text)
        elif kind == "tool":
            parts.append(f"`{text}`")
    if not parts:
        return None
    role = "assistant" if turn.get("role") == "assistant" else "user"
    return role, "\n\n".join(parts)


async def _import_transcript(chat_id: str, session_id: str) -> int:
    """Seed a resumed chat with the conversation already on disk.

    A resumed CLI session used to open empty: the history lived only in the
    transcript viewer, so the chat gave no sense of what had been discussed.
    Importing it once at resume makes the conversation read as if it had
    always been here -- scrollable, searchable, exportable and forkable like
    any other, because it is now ordinary message rows.
    """
    try:
        payload = await transcripts.read_turns(session_id)
    except OSError as exc:
        _log.warning("transcript_import_failed session_id=%s: %s", session_id, exc)
        return 0
    if not payload.get("found"):
        return 0
    rows = [
        row
        for row in (_turn_to_message(turn) for turn in payload.get("turns") or [])
        if row is not None
    ]
    if not rows:
        return 0
    await db.messages_batch(chat_id, rows)
    _log.info(
        "transcript_imported chat_id=%s session_id=%s turns=%d truncated=%s",
        chat_id, session_id, len(rows), bool(payload.get("truncated")),
    )
    return len(rows)


async def handle_session_delete(request: Request, session_id: str):
    """DELETE /api/sessions/{session_id} -- drop a dead CLI session entry.

    Only removes the shadow record WebConsole itself wrote when the session
    was resumed. db.delete_claude_session_file refuses anything else, so a
    running session cannot be cleared out of the sidebar by accident.
    """
    session = request.state.session
    session_id = _sanitize_session_id(session_id)
    try:
        removed = await asyncio.to_thread(db.delete_claude_session_file, session_id)
    except ValueError as exc:
        _log.warning(
            "session_delete_refused: user=%s session_id=%s (%s)",
            session["user"], session_id, exc,
        )
        raise HTTPException(status_code=409, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="Session entry not found")
    _log.info("session_entry_removed session_id=%s user=%s", session_id, session["user"])
    return JSONResponse({"ok": True})


# ── Session routes ─────────────────────────────────────────────────────────────────


@app.get("/api/skills")
async def _api_skills_get(request: Request):
    return await handle_skills_get(request)


@app.get("/api/usage")
async def _api_usage_get(request: Request):
    return await handle_usage_get(request)


@app.get("/api/settings")
async def _api_settings_get(request: Request):
    return await handle_settings_get(request)


@app.patch("/api/settings")
async def _api_settings_patch(request: Request):
    return await handle_settings_patch(request)


async def handle_transcripts_list(request: Request):
    """GET /api/transcripts -- recent CLI conversations, newest first."""
    try:
        limit = int(request.query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return JSONResponse({"transcripts": await transcripts.list_recent(limit)})


async def handle_agent_traffic(request: Request):
    """GET /api/agent-traffic -- messages exchanged between concurrent sessions.

    Read out of the transcripts, not off the sockets the sessions actually talk
    over: a log rather than an interception layer, and it needs no knowledge of
    that private protocol.
    """
    try:
        limit = int(request.query_params.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    try:
        scan = int(request.query_params.get("files", 12))
    except (TypeError, ValueError):
        scan = 12
    messages = await transcripts.agent_traffic(limit=limit, scan_files=scan)
    return JSONResponse({"messages": messages, "count": len(messages)})


async def handle_transcript_get(request: Request, session_id: str):
    """GET /api/transcripts/{id} -- one conversation's history.

    Omit both parameters for the most recent page. ``before`` pages backwards
    through a long session; ``offset`` resumes forwards from a byte position.
    """
    raw_before = request.query_params.get("before")
    if raw_before is not None:
        try:
            before = max(0, int(raw_before))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="before must be a number")
        page = await transcripts.read_before(session_id, before)
    else:
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        page = await transcripts.read_turns(session_id, offset)

    if not page["found"]:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return JSONResponse(page)


async def handle_transcript_stream(request: Request, session_id: str):
    """GET /api/transcripts/{id}/stream -- follow a running session over SSE."""
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    if transcripts.transcript_path(session_id) is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    async def event_generator():
        cursor = offset
        idle = 0.0
        yield f"data: {json.dumps({'type': 'start', 'offset': cursor})}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    return
                page = await transcripts.read_turns(session_id, cursor)
                if not page["found"]:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Transcript went away'})}\n\n"
                    return
                cursor = page["offset"]
                for turn in page["turns"]:
                    yield f"data: {json.dumps({'type': 'turn', 'turn': turn, 'offset': cursor})}\n\n"
                idle = 0.0 if page["turns"] else idle + transcripts.TAIL_POLL_S
                if idle >= 15.0:
                    # Comment frame: keeps proxies from dropping an idle stream.
                    yield ": keep-alive\n\n"
                    idle = 0.0
                await asyncio.sleep(transcripts.TAIL_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- convert tail failures to SSE errors
            _log.exception("transcript stream failed session_id=%s", session_id)
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


@app.get("/api/transcripts")
async def _api_transcripts_list(request: Request):
    return await handle_transcripts_list(request)


@app.get("/api/agent-traffic")
async def _api_agent_traffic(request: Request):
    return await handle_agent_traffic(request)


@app.get("/api/transcripts/{session_id}")
async def _api_transcript_get(request: Request, session_id: str):
    return await handle_transcript_get(request, session_id)


@app.get("/api/transcripts/{session_id}/stream")
async def _api_transcript_stream(request: Request, session_id: str):
    return await handle_transcript_stream(request, session_id)


@app.get("/api/models")
async def _api_models_list(request: Request):
    return await handle_models_list(request)


@app.put("/api/machines/{machine_id}/models")
async def _api_machine_models_set(request: Request, machine_id: str):
    return await handle_machine_models_set(request, machine_id)


@app.get("/api/sessions")
async def _api_sessions_list(request: Request):
    return await handle_sessions_list(request)


@app.post("/api/sessions/{session_id}/resume")
async def _api_sessions_resume(request: Request, session_id: str):
    return await handle_sessions_resume(request, session_id)


@app.delete("/api/sessions/{session_id}")
async def _api_sessions_delete(request: Request, session_id: str):
    return await handle_session_delete(request, session_id)


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
