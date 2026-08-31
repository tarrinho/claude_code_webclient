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
import socket
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from html import escape as html_escape
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import Any, ClassVar, Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
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
import prompts
import runner
import supervisor
import sysstats
import transcripts
import turns


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
        ) or request.url.path.startswith("/dev/")
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

    # PUT was missing, so every PUT route was unguarded -- a cross-site request
    # could reorder a user's conversations, and any future PUT would have
    # inherited the same hole silently.
    _MUTATING: ClassVar[set] = {"POST", "PUT", "PATCH", "DELETE"}
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
            # SAMEORIGIN, not DENY: the console frames its own supervisor page
            # so it can sit beside the conversation list. The clickjacking
            # threat this header exists for is a *foreign* site framing us,
            # which SAMEORIGIN still refuses -- an attacker's page cannot be
            # same-origin with this one.
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
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
                "connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; "
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


def backend_kind(machine: dict | None) -> str:
    """Classify a backend as ``anthropic``, ``anthropic-compatible`` or ``proxy``.

    The stored ``provider`` column only says which wire protocol a machine
    speaks, so a self-hosted gateway reads ``anthropic`` there too. This is the
    finer distinction that actually matters: whether requests reach the official
    API. Single source of truth for both usage accounting (where it decides
    whether the CLI's cost figure is trustworthy) and the machine API, so the
    two surfaces cannot drift apart.
    """
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


# Safe messages for SSE errors so internal details never leak. The first two are
# aliases of turns.py's own constants: a turn now fails inside its task, so the
# text is chosen there, and duplicating the literals here is how a timeout ends
# up reported as a generic internal error.
_SSE_INTERNAL = turns.INTERNAL_MESSAGE
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
    if model and model != chat.get("model"):
        await db.chat_set_model(chat_id, model)
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
        if model and model != chat.get("model"):
            await db.chat_set_model(chat_id, model)
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


# Terminal sessions have no signed-in user, so their spend is attributed here.
# A fixed owner rather than whoever happens to open the tab: the byte cursor is
# per transcript, not per user, so attributing to the viewer would let the first
# person to look claim every row and leave the second an empty report. Correct
# only while this is a single-operator console -- the day a second account
# exists, this line is the bug.
CLI_USAGE_OWNER: Final[str] = "admin"


async def _import_cli_usage(owner: str = CLI_USAGE_OWNER) -> int:
    """Fold terminal-session spend into the usage table.

    Turns run in a terminal never pass through this app, so without this the
    Usage tab reports only what was typed into the website -- which on this
    machine was 6 events against roughly nine thousand real ones.

    Every assistant record in a transcript carries its model and token counts,
    so the history is recoverable after the fact. A byte cursor per transcript
    keeps a re-run from counting the same turns twice; the first import pays
    for the whole archive, later ones read only what was appended.
    """
    imported = 0
    try:
        entries = await transcripts.list_recent(limit=60)
    except OSError:
        return 0
    for entry in entries:
        session_id = entry["session_id"]
        try:
            cursor = await db.usage_cursor_get(session_id)
            rows, new_offset = await transcripts.usage_since(session_id, cursor)
            if new_offset != cursor:
                imported += await db.usage_import(owner, session_id, rows, new_offset)
        except (OSError, ValueError) as exc:
            # One unreadable transcript must not cost the whole report.
            _log.warning("cli_usage_import_failed session_id=%s: %s", session_id, exc)
    if imported:
        _log.info("cli_usage_imported owner=%s rows=%d", owner, imported)
    return imported


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
    await _import_cli_usage()
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

    by_origin = await db.usage_by_origin(owner, days)
    for row in by_origin:
        if row.get("unsplit_requests"):
            # Said in the response rather than left for the reader to infer from
            # a suspiciously large number, matching how cost is already
            # suppressed with a note for backends where it is not meaningful.
            row["unsplit_note"] = (
                "Excluded from the token total: this model reported no cache "
                "breakdown, so each turn counts the whole conversation again "
                "rather than new tokens."
            )
    totals = await db.usage_totals(owner, days)
    for row in totals:
        if row.get("provider") != "anthropic":
            row["cost_usd"] = None
            # Prefer the CLI's own assessment when it gave one: that is a
            # statement from the tool, not an inference from our base_url.
            row["cost_note"] = (
                "Claude Code reported the cost basis as unknown for this model."
                if row.get("cost_basis_unknown")
                else "Priced with Anthropic rates; not meaningful for this backend."
            )
    recent = await db.usage_recent(owner, limit)
    for row in recent:
        if row.get("provider") != "anthropic":
            row["cost_usd"] = None

    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "retention_days": config.USAGE_RETENTION_DAYS,
            "overall": await db.usage_overall(owner, days),
            # Where the turns came from, and which session spent it. Without
            # these the page reported one figure dominated by adopted agent
            # sessions and presented it as the operator's own usage.
            "by_origin": by_origin,
            "by_session": await db.usage_by_session(owner, days),
            "totals": totals,
            "recent": recent,
        }
    )


async def handle_usage_series_get(request: Request):
    """GET /api/usage/series -- usage bucketed over time, for the charts.

    Separate from /api/usage rather than folded into it: that endpoint is read
    on every visit to the Usage tab and returns a flat table, while this one is
    read only by the statistics page and scans a far wider window. Keeping them
    apart means the common request does not pay for the rare one.

    Same ownership rule as /api/usage -- the caller's own rows, readable by any
    authenticated user because it is their own data and carries no secret.
    """
    await _import_cli_usage()
    owner = request.state.session["user"]

    raw_days = request.query_params.get("days", "30")
    days: int | None
    if raw_days in ("all", "0", ""):
        days = None
    else:
        try:
            days = max(1, min(int(raw_days), 3650))
        except (TypeError, ValueError):
            days = 30

    bucket = request.query_params.get("bucket", "day")
    if bucket not in db.USAGE_BUCKETS:
        bucket = "day"

    series = await db.usage_series(owner, days, bucket)
    # Cost is only meaningful for the official API: Claude Code prices every
    # turn with Anthropic's rates, so a gateway's figure is arithmetic on the
    # wrong number. Blanked here for the same reason /api/usage blanks it.
    for row in series:
        if row.get("provider") != "anthropic":
            row["cost_usd"] = None

    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "bucket": bucket,
            "buckets": list(db.USAGE_BUCKETS),
            "retention_days": config.USAGE_RETENTION_DAYS,
            "series": series,
            "models": await db.usage_model_series(owner, days, bucket),
        }
    )


def _system_range(request: Request) -> tuple[int | None, str]:
    """Parse and clamp the days/bucket query pair shared by the system routes."""
    raw_days = request.query_params.get("days", "1")
    days: int | None
    if raw_days in ("all", "0", ""):
        days = None
    else:
        try:
            days = max(1, min(int(raw_days), 3650))
        except (TypeError, ValueError):
            days = 1
    bucket = request.query_params.get("bucket", "halfhour")
    if bucket not in db.USAGE_BUCKETS:
        bucket = "halfhour"
    return days, bucket


async def handle_system_get(request: Request):
    """GET /api/system -- a live snapshot of the host this server runs on.

    Readable by any authenticated user, matching /api/settings: it carries no
    secret, and an operator checking whether the box is struggling should not
    need an admin account to do it. Hostname and CPU model are the most
    identifying values here and both are already implicit in reaching the
    site at all.
    """
    snapshot = await sysstats.sample_async()
    snapshot["sample_interval_s"] = config.SYSTEM_SAMPLE_S
    snapshot["retention_days"] = config.SYSTEM_RETENTION_DAYS
    return JSONResponse(snapshot)


async def handle_system_series_get(request: Request):
    """GET /api/system/series -- stored host samples bucketed over time.

    Defaults to the last 24 hours in half-hour buckets rather than the 30 days
    the usage page defaults to. These two pages answer different questions: a
    token bill is read by the month, and a machine in trouble is read by the
    hour.
    """
    days, bucket = _system_range(request)
    return JSONResponse(
        {
            "days": days if days is not None else 0,
            "bucket": bucket,
            "buckets": list(db.USAGE_BUCKETS),
            "sample_interval_s": config.SYSTEM_SAMPLE_S,
            "retention_days": config.SYSTEM_RETENTION_DAYS,
            "series": await db.system_series(days, bucket),
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
                # backend_kind is derived, not stored: clients should not have to
                # reimplement the provider/base_url rule to label a backend.
                {**{k: v for k, v in m.items() if k != "api_key"},
                 "backend_kind": backend_kind(m)}
                for m in machines
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
    m["backend_kind"] = backend_kind(machine)
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


# An agent that merely finished talking is not a reason to interrupt anyone.
# The badge fires only when the last thing it said either asks for something or
# reports that it is stuck. Both lists are deliberately short: a wide net makes
# the count meaningless, and a missed alert costs one scroll of the sidebar
# while a false one costs the badge its credibility.
_ASKS_FOR_INPUT: Final[tuple[str, ...]] = (
    "let me know",
    "do you want",
    "would you like",
    "shall i",
    "should i",
    "your call",
    "yours to call",
    "say the word",
    "which would you",
    "confirm",
    "please choose",
    "waiting for your",
    "waiting on your",
    "worth doing",
    "worth fixing",
)
_REPORTS_A_BLOCKER: Final[tuple[str, ...]] = (
    "blocked",
    "cannot proceed",
    "can't proceed",
    "needs your",
    "need your",
    "waiting on you",
    "permission denied",
    "requires your",
    "i was denied",
)


def _pending_question(turns: list[dict]) -> str | None:
    """An AskUserQuestion still awaiting a reply, or None.

    The CLI records the question and its outcome as separate blocks, matched by
    id. A question with no matching answer is genuinely outstanding; once the
    answer arrives the highlight has to go, which is the whole point of pairing
    them rather than just spotting a question.
    """
    answered: set[str] = set()
    for turn in turns:
        for block in turn.get("blocks", []):
            if block.get("kind") == "answer" and block.get("id"):
                answered.add(block["id"])
    for turn in reversed(turns):
        for block in reversed(turn.get("blocks", [])):
            if block.get("kind") != "question":
                continue
            if block.get("id") in answered:
                # The newest question has been resolved, so nothing is pending.
                return None
            first = (block.get("questions") or [{}])[0]
            return str(first.get("question") or "").strip() or "A question is waiting"
    return None


def _last_thing_said(turns: list[dict]) -> str:
    """The newest assistant text in *turns*, skipping tool calls.

    A turn carrying only a tool call has role="assistant" and no text, so
    reading the last turn alone finds nothing and the agent looks silent --
    which is what stopped every terminal session from ever being surfaced.
    """
    for turn in reversed(turns):
        if turn.get("role") != "assistant":
            continue
        text = " ".join(
            (block.get("text") or "").strip()
            for block in turn.get("blocks", [])
            if block.get("kind") == "text"
        ).strip()
        if text:
            return text
    return ""


def _attention(text: str) -> str | None:
    """Why this output needs the user, or None if it is just talk.

    Returns "asks" when the agent wants something back and "blocked" when it
    reports it cannot continue. Everything else -- progress, results, a
    finished piece of work -- is left silent on purpose.
    """
    body = (text or "").strip()
    if not body:
        return None
    lowered = body.lower()
    # A trailing question is the clearest possible request for input. Only the
    # end of the message counts: a question quoted mid-explanation is not an ask.
    tail = lowered.rstrip().rstrip("`*_)\"'")
    if tail.endswith("?"):
        return "asks"
    # An agent that finishes with a colon or "…:" is inviting the user to
    # complete the thought (a choice, a confirmation, a value).
    if tail.endswith((":", "…")):
        return "asks"
    if any(phrase in lowered for phrase in _ASKS_FOR_INPUT):
        return "asks"
    if any(phrase in lowered for phrase in _REPORTS_A_BLOCKER):
        return "blocked"
    return None


def _one_line(text: str, limit: int = 120) -> str:
    """First line of *text*, collapsed, for the supervisor's preview column."""
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


# Last failure seen per session, keyed by the transcript mtime it was read at.
# A file that has not moved cannot have gained a new failure, so the tail read
# is skipped -- the busy fast path exists to make a five-second poll affordable
# and it must stay affordable.
_failure_cache: dict[str, tuple[str, str | None]] = {}


async def _session_failure(session_id: str, file_touched: str) -> str | None:
    """The newest turn's failure text for *session_id*, or None."""
    cached = _failure_cache.get(session_id)
    if cached is not None and cached[0] == file_touched:
        return cached[1]
    try:
        failure = await transcripts.last_error(session_id)
    except Exception:  # noqa: BLE001 -- the view must render without it
        # exc_info because this catches both a malformed transcript, which is
        # expected and benign, and a programming error, which is neither. A
        # NameError from a misspelled callee produces the same line as an empty
        # transcript without it -- which is exactly how the unqualified
        # _scan_questions_sync call in this file returned "no questions found"
        # on every request instead of failing.
        _log.warning("last_error failed session=%s", session_id, exc_info=True)
        return None
    _failure_cache[session_id] = (file_touched, failure)
    return failure


def classify_chat(
    chat: dict,
    last: dict,
    live_ids: set | frozenset,
    queued: dict,
    marks: dict,
    cli_status_map: dict,
    cli_dismiss_map: dict,
    cli_status_updated_map: dict,
) -> dict | None:
    """Classify one conversation as waiting, working or updated.

    Returns the entry with a "status" key set, or None when the conversation
    should not be listed at all.

    Extracted so the sidebar and the supervisor members panel share one
    definition of what "stuck" means. They had to: two implementations would
    agree only by coincidence, and would drift the first time either was
    touched -- the same argument that made backend_kind a single function
    rather than a rule reimplemented client-side.

    A pure function of what it is given, which is what makes it callable for
    one member as cheaply as for the whole sidebar. handle_supervisor does far
    more than classify -- it merges CLI sessions, reads marks and applies
    dismissals -- so calling that handler to learn one member's status would
    have paid for all of it and turned its response shape into an API nobody
    intended to depend on.
    """
    entry = {
        "kind": "chat",
        "id": chat["id"],
        "title": chat.get("title") or "Untitled",
        "preview": _one_line(last.get("preview") or ""),
        "since": last.get("created_at") or "",
    }
    # Busy outranks everything, and is asked rather than inferred. A queued
    # prompt counts as busy too: the user has already said what they want and
    # is waiting on us, not the other way round.
    if chat["id"] in live_ids or queued.get(chat["id"]):
        return {**entry, "status": "working"}
    # A turn the user stopped is not something to be summoned back to. It is
    # absent from running_ids, so it never looked busy that way -- but a cancel
    # that persisted nothing leaves the user's own prompt newest, which the
    # branch below reports as working, and it would stay that way rather than
    # clearing when the buffer is reaped.
    live = turns.get(chat["id"])
    if live is not None and live.state == "cancelled":
        return None
    if last.get("role") != "assistant":
        # The newest message is the user's own and no turn is registered. That
        # is ambiguous -- a turn that died, or one that has not started yet --
        # and the common case is the second: answering a question makes the
        # user's reply newest for the moment before the turn begins. Calling
        # that "waiting" would put the highlight back the instant it was
        # answered, which is the opposite of what was asked for.
        return {**entry, "status": "working"}
    # An ask or a blocker outranks everything: it stays listed until it is
    # actually answered, which for a conversation means the newest message
    # stops being the agent's. Opening it is not answering it -- clearing on
    # read let a question be dismissed by glancing at it.
    mark = marks.get(("chat", chat["id"]), {})
    stamp = last.get("created_at") or ""
    preview = last.get("preview") or ""
    reason = _attention(preview)
    # Questions rendered as text end with "(answer this in the terminal)", so
    # _attention() misses the trailing ? and falls through to None.
    if not reason and _QUESTION_PENDING_NOTE in preview:
        reason = "asks"
    if reason:
        # Only an explicit dismissal silences an unanswered question.
        if mark.get("dismissed_at") and stamp <= mark["dismissed_at"]:
            return None
        return {**entry, "status": "waiting", "reason": reason}
    # A web conversation linked to a CLI session that is no longer busy is not
    # "updated" -- the agent itself has stopped and is waiting. Defer to the
    # session's own status so the user sees the question that triggered it
    # rather than a truncated preview that _attention() cannot match.
    session_id = chat.get("session_id", "")
    cli_status = cli_status_map.get(session_id, "")
    if cli_status and cli_status != "busy":
        # Both marks, not just the session's. This row is presented as a
        # conversation, so dismissing it writes ("chat", chat_id) -- and this
        # branch used to consult only ("session", session_id). The dismissal
        # was recorded faithfully and then never read, so the row returned on
        # the next poll and the control looked inert. The later of the two
        # wins: either identity may silence the row the user actually sees.
        dismissed = max(
            cli_dismiss_map.get(session_id, "") or "",
            mark.get("dismissed_at") or "",
        )
        # Fall back to the conversation's own last activity when the session
        # file carries no status timestamp -- which is every non-busy session
        # on this machine, so the guard below was failing open and relisting
        # unconditionally. Requiring a timestamp that is usually absent made
        # the dismissal inert no matter which mark it consulted.
        status_updated = cli_status_updated_map.get(session_id, "") or stamp
        if not (dismissed and status_updated and status_updated <= dismissed):
            return {
                **entry,
                "status": "waiting",
                "reason": "asks",
                "reason_detail": f"session={cli_status}",
            }
    # Routine output is different: seeing it IS the whole point, so a read mark
    # retires it.
    if mark.get("read_at") and stamp <= mark["read_at"]:
        return None
    return {**entry, "status": "updated"}


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
    except Exception:  # noqa: BLE001 -- the view must render without the queue
        queued = {}
    waiting: list[dict] = []   # asked for something, or reported a blocker
    working: list[dict] = []   # mid-turn
    updated: list[dict] = []   # said something unread, but nothing is needed

    # ── Web conversations ───────────────────────────────────────────────
    chats = await db.chat_list(owner)
    activity = await db.chat_last_activity(owner)
    # Build a lookup: session_id → CLI status so the web path can defer to
    # the session's own status when a chat is linked to a running CLI session.
    try:
        cli_sessions_for_web = await db.read_claude_sessions()
    except Exception:  # noqa: BLE001
        cli_sessions_for_web = []
    _cli_status_map: dict[str, str] = {}
    for _cs in cli_sessions_for_web:
        _sid = _cs.get("sessionId", "")
        if _sid:
            _cli_status_map[_sid] = (_cs.get("status") or "").lower()
    _cli_dismiss_map: dict[str, str] = {}
    for _cs in cli_sessions_for_web:
        _sid = _cs.get("sessionId", "")
        if _sid:
            _mark = marks.get(("session", _sid), {})
            _cli_dismiss_map[_sid] = _mark.get("dismissed_at", "")
    _cli_status_updated_map: dict[str, str] = {}
    for _cs in cli_sessions_for_web:
        _sid = _cs.get("sessionId", "")
        if _sid:
            _cli_status_updated_map[_sid] = _cs.get("status_updated_at", "")
    for chat in chats:
        if chat.get("archived"):
            continue
        last = activity.get(chat["id"])
        if not last:
            continue
        entry = classify_chat(
            chat, last, live_ids, queued, marks,
            _cli_status_map, _cli_dismiss_map, _cli_status_updated_map,
        )
        if entry is None:
            continue
        {"waiting": waiting, "working": working, "updated": updated}[
            entry["status"]
        ].append(entry)

    # ── CLI / terminal sessions ─────────────────────────────────────────
    try:
        cli_sessions = await db.read_claude_sessions()
    except Exception:  # noqa: BLE001 -- the sidebar must render without them
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
        # Epoch seconds from the file, ISO from the marks: compare like for like.
        file_touched = datetime.datetime.fromtimestamp(
            meta.get("updated_at") or 0, datetime.UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        mark = marks.get(("session", session_id), {})
        seen = mark.get("read_at", "")
        status = (cli.get("status") or "").strip().lower()
        # A busy agent needs no transcript read at all, which is what keeps
        # this cheap enough to poll: it is the common case.
        if status == "busy":
            row = {
                "kind": "session", "id": session_id,
                "title": cli.get("name") or meta.get("title") or session_id,
                "preview": "", "since": file_touched, "status": "working",
            }
            # Busy is honest but incomplete: a session retrying a dead endpoint
            # reports busy the whole time, so a run going nowhere looks exactly
            # like one doing work. The failure is read from the model field --
            # the CLI writes "<synthetic>" on a failed turn -- rather than from
            # the wording, so an agent *discussing* an API error is not mistaken
            # for one suffering it. Cached against the transcript's mtime: an
            # unmoved file cannot have gained a new failure, which keeps the
            # fast path fast on the common case of a session quietly working.
            failure = await _session_failure(session_id, file_touched)
            if failure:
                dismissed = mark.get("dismissed_at") or ""
                # Retired only by an explicit dismissal, never by being read: a
                # failing endpoint does not fix itself by being looked at. It
                # also clears itself once the agent produces real output again,
                # because last_error only reports a failure that is still the
                # newest turn.
                if not (dismissed and file_touched <= dismissed):
                    waiting.append({
                        **row, "preview": _one_line(failure),
                        "status": "waiting", "reason": "failed",
                    })
                    continue
            working.append(row)
            continue
        # Without a status field, fall back to mtime as a negative filter only:
        # an untouched file certainly has nothing new. It must never decide
        # "waiting" on its own -- one cross-session message deposits dozens of
        # queue-operation and attachment records into the receiving session, so
        # with several agents talking the mtime is never still.
        if not status and seen and file_touched <= seen:
            continue
        page = await transcripts.read_turns(session_id)
        page_turns = page.get("turns") or []
        if not page_turns:
            continue
        entry = {
            "kind": "session",
            "id": session_id,
            "title": cli.get("name") or meta.get("title") or session_id,
            "preview": "",
        }
        # Claude Code reports its own state, which beats inferring one from the
        # transcript: a session at a permission prompt and one running a tool
        # look identical in the file. Any non-busy value means it has stopped
        # and is waiting on a human -- and it stays listed until it starts
        # working again, which only happens once someone answers it. A read
        # mark deliberately does not retire this.
        if status:
            spoke_at = cli.get("status_updated_at") or file_touched
            dismissed = mark.get("dismissed_at") or ""
            if dismissed and spoke_at <= dismissed:
                continue
            # A structured question outranks prose: it is an unambiguous ask,
            # and its answer block is an unambiguous resolution.
            pending = _pending_question(page_turns)
            said = pending or _last_thing_said(page_turns)
            waiting.append({
                **entry,
                "since": spoke_at,
                "preview": _one_line(said),
                "status": "waiting",
                "reason": "asks" if pending else (_attention(said) or "idle"),
            })
            continue
        last_turn = page_turns[-1]
        # A trailing tool call means the agent is still running, not that it
        # has stopped with something to say. Checking only role was wrong:
        # every tool turn carries role="assistant" too.
        last_is_speech = last_turn.get("role") == "assistant" and any(
            b.get("kind") == "text" and (b.get("text") or "").strip()
            for b in last_turn.get("blocks", [])
        )
        if not last_is_speech:
            working.append({**entry, "since": file_touched, "status": "working"})
            continue
        # The real signal: when the agent last said something, not when its
        # file was last written.
        spoke_at = str(last_turn.get("timestamp") or "") or file_touched
        if seen and spoke_at <= seen:
            continue
        # The last block of the turn, not the first: a turn often opens with a
        # sentence of narration and ends with the actual question.
        text = " ".join(
            (b.get("text") or "").strip()
            for b in last_turn.get("blocks", [])
            if b.get("kind") == "text"
        ).strip()
        pending = _pending_question(page_turns)
        if pending:
            text = pending
        reason = "asks" if pending else _attention(text)
        row = {**entry, "since": spoke_at, "preview": _one_line(text)}
        if reason:
            # Unanswered outranks read, same as for conversations.
            dismissed = mark.get("dismissed_at") or ""
            if not (dismissed and spoke_at <= dismissed):
                waiting.append({**row, "status": "waiting", "reason": reason})
        elif not (seen and spoke_at <= seen):
            updated.append({**row, "status": "updated"})

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
    except Exception:  # noqa: BLE001 -- the panel must render without the queue
        queued = {}
    activity = await db.chat_last_activity(owner)
    by_id = {c["id"]: c for c in await db.chat_list(owner)}

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
            entry = classify_chat(chat, last, live_ids, queued, marks, {}, {}, {})
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
    except Exception:  # noqa: BLE001 -- any malformed body is one 400
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


# A location hint, not a state claim. The message body is fixed at import time
# and cannot be revised when the answer arrives in a later sync, so "waiting for
# an answer" would keep asserting that forever -- including next to the
# "Declined in the terminal" message that immediately follows it. Where to
# answer stays true either way; the outcome is reported by its own message.
_QUESTION_PENDING_NOTE = "(answer this in the terminal)"


def _question_to_text(block: dict) -> str:
    """Render a question and every option as plain text for a message body.

    Message bodies are shown with textContent, not Markdown, so the shape has
    to survive as plain text.
    """
    lines: list[str] = []
    for entry in block.get("questions") or []:
        if not isinstance(entry, dict):
            continue
        header = str(entry.get("header") or "").strip()
        question = str(entry.get("question") or "").strip()
        lines.append(f"Question — {header}" if header else "Question")
        if question:
            lines.append(question)
        if entry.get("multi_select"):
            lines.append("(choose one or more)")
        for option in entry.get("options") or []:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            if not label:
                continue
            description = str(option.get("description") or "").strip()
            lines.append(f"  • {label} — {description}" if description else f"  • {label}")
        lines.append(_QUESTION_PENDING_NOTE)
    return "\n".join(lines).strip()


def _answer_to_text(block: dict) -> str:
    """Render how a question was resolved."""
    status = block.get("status") or "resolved"
    label = {"answered": "Answered", "declined": "Declined"}.get(status, "Resolved")
    text = " ".join(str(block.get("text") or "").split())
    return f"{label} in the terminal: {text}" if text else f"{label} in the terminal"


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
        kind = block.get("kind")
        # A question carries no "text" -- its content is the question and its
        # options -- so reading block["text"] dropped it from the conversation
        # entirely. A question the user has to answer is the last thing that
        # should go missing here.
        if kind == "question":
            rendered = _question_to_text(block)
            if rendered:
                parts.append(rendered)
            continue
        if kind == "answer":
            rendered = _answer_to_text(block)
            if rendered:
                parts.append(rendered)
            continue
        text = (block.get("text") or "").strip()
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
    # The 512 KB tail read misses unanswered ``AskUserQuestion`` blocks that
    # live in the older part of a large transcript.  A quick full-file scan
    # finds them and attaches them as message rows.  Filter by IDs we haven't
    # already rendered so re-importing stays idempotent.
    try:
        # transcript_path returns None when a session has no transcript on
        # disk, and _scan_questions_sync is typed for a Path: it guards
        # read_bytes with OSError, which None.read_bytes() is not. Skipping
        # here keeps that signature honest rather than teaching the scanner
        # to accept a value it says it does not take.
        scan_path = transcripts.transcript_path(session_id)
        question_blocks = await asyncio.to_thread(
            # Private, because transcripts.py exposes no public full-file scan.
            transcripts._scan_questions_sync,
            scan_path,
        ) if scan_path is not None else []
    except Exception:  # noqa: BLE001 -- a malformed transcript must not break the page
        # Logged, not silent. The call above was unqualified until now, so it
        # raised NameError on every request and this handler turned that into
        # "no questions found" -- meaning the older unanswered questions the
        # scan exists to find were the exact thing it never returned. A bare
        # pass here makes a programming error indistinguishable from a
        # transcript that legitimately had none, and one log line would have
        # found it in a single run.
        _log.warning("question_scan_failed session=%s", session_id, exc_info=True)
        question_blocks = []

    seen_ids: set[str] = await db.chat_get_question_ids(chat_id)
    filtered_questions: list[dict[str, Any]] = []
    for qb in question_blocks:
        qid = str(qb.get("id") or "")
        if (qid and qid not in seen_ids) or (not qid and qb not in filtered_questions):
            filtered_questions.append(qb)

    extra_rows: list[tuple[str, str]] = []
    for qb in filtered_questions:
        rendered = _question_to_text(qb)
        if rendered:
            extra_rows.append(("assistant", rendered))
    if extra_rows:
        rows.extend(extra_rows)
        _log.info(
            "transcript_imported_questions chat_id=%s questions=%d",
            chat_id, len(extra_rows),
        )
        await db.chat_set_question_ids(chat_id, [str(q.get("id", "")) for q in filtered_questions])
    # Record the read position even when nothing was worth importing, so the
    # sync does not re-examine the same bytes on every poll.
    await db.chat_set_transcript_offset(chat_id, int(payload.get("offset") or 0))
    if not rows:
        return 0
    await db.messages_batch(chat_id, rows)
    _log.info(
        "transcript_imported chat_id=%s session_id=%s turns=%d truncated=%s",
        chat_id, session_id, len(rows), bool(payload.get("truncated")),
    )
    return len(rows)


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

    pending = await asyncio.to_thread(transcripts.pending_question, session_id)
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

    pending = await asyncio.to_thread(transcripts.pending_question, session_id)
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

    pending = await asyncio.to_thread(transcripts.pending_question, session_id)
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


# ── Supervisor orchestration ──────────────────────────────────────────────────────
# Registry of live engine instances, keyed by supervisor_id. Engines are
# started on first use and cleaned up when their supervisor is deleted.
_supervisor_engines: dict[str, supervisor.SupervisorEngine] = {}


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
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
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


# ── Session routes ─────────────────────────────────────────────────────────────────


@app.get("/api/skills")
async def _api_skills_get(request: Request):
    return await handle_skills_get(request)


@app.get("/api/usage")
async def _api_usage_get(request: Request):
    return await handle_usage_get(request)


# Registered before no path parameter shadows it; FastAPI matches in order and
# /api/usage has no wildcard sibling, but keeping them adjacent means a future
# /api/usage/{id} cannot silently capture this one.
@app.get("/api/usage/series")
async def _api_usage_series_get(request: Request):
    return await handle_usage_series_get(request)


@app.get("/api/system")
async def _api_system_get(request: Request):
    return await handle_system_get(request)


# Same ordering care as /api/usage/series above: adjacent so a later
# /api/system/{id} cannot capture this path.
@app.get("/api/system/series")
async def _api_system_series_get(request: Request):
    return await handle_system_series_get(request)


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


@app.get("/api/supervisor")
async def _api_supervisor(request: Request):
    return await handle_supervisor(request)


@app.get("/api/supervisors/{supervisor_id}/members")
async def _api_supervisor_members_get(request: Request, supervisor_id: str):
    return await handle_supervisor_members_get(request, supervisor_id)


@app.post("/api/supervisors/{supervisor_id}/members")
async def _api_supervisor_members_add(request: Request, supervisor_id: str):
    return await handle_supervisor_members_add(request, supervisor_id)


@app.delete("/api/supervisors/{supervisor_id}/members/{chat_id}")
async def _api_supervisor_member_remove(
    request: Request, supervisor_id: str, chat_id: str
):
    return await handle_supervisor_member_remove(request, supervisor_id, chat_id)


@app.post("/api/supervisor/read")
async def _api_supervisor_read(request: Request):
    return await handle_supervisor_read(request)


@app.post("/api/supervisors/{supervisor_id}/send")
async def _api_supervisor_send(request: Request, supervisor_id: str):
    session = request.state.session
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
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
        _supervisor_engines[supervisor_id] = eng_new = supervisor.SupervisorEngine(
            supervisor_id, session["user"],
        )
    else:
        eng_new = _supervisor_engines[supervisor_id]

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


@app.post("/api/supervisors/{supervisor_id}/pause")
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


@app.post("/api/supervisors/{supervisor_id}/resume")
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


@app.get("/supervisor.js")
async def _serve_supervisor_js(request: Request):
    # application/javascript, not HTML. Browsers enforce strict MIME checking on
    # scripts, so served as text/html this file was refused outright and the
    # supervisor page rendered its markup with none of its behaviour.
    try:
        return Response(
            (_WEB_DIR / "supervisor.js").read_text(),
            media_type="application/javascript",
        )
    except FileNotFoundError:
        return Response("// supervisor.js missing", status_code=500,
                        media_type="application/javascript")


# Supervisor routes — these match the pattern from the design spec.
@app.get("/api/supervisors")
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


@app.post("/api/supervisors")
async def _api_supervisors_create(request: Request):
    session = request.state.session
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON")
    title = (body.get("title") or "New Supervisor").strip()[:200]
    description = (body.get("description") or "").strip()[:500] or None
    config_data = _validated_supervisor_config(
        body.get("config") if body.get("config") else None
    )
    sid = uuid.uuid4().hex
    await db.supervisor_create(sid, title, description, session["user"], config_data)
    eng = supervisor.SupervisorEngine(sid, session["user"])
    _supervisor_engines[sid] = eng
    return JSONResponse({"ok": True, "id": sid, "title": title, "status": "idle"})


@app.get("/api/supervisors/{supervisor_id}")
async def _api_supervisor_get(request: Request, supervisor_id: str):
    session = request.state.session
    existing = await db.supervisor_get(supervisor_id, session["user"])
    if not existing:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    return JSONResponse({"supervisor": existing})


@app.patch("/api/supervisors/{supervisor_id}")
async def _api_supervisor_patch(request: Request, supervisor_id: str):
    session = request.state.session
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
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


@app.delete("/api/supervisors/{supervisor_id}")
async def _api_supervisor_delete(request: Request, supervisor_id: str):
    session = request.state.session
    deleted = await db.supervisor_delete(supervisor_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Supervisor not found")
    _supervisor_engines.pop(supervisor_id, None)
    return JSONResponse({"ok": True})


@app.get("/api/supervisors/{supervisor_id}/stream")
async def _api_supervisor_stream(request: Request, supervisor_id: str):
    return await handle_supervisor_stream(request, supervisor_id)


@app.get("/api/supervisors/{supervisor_id}/tasks")
async def _api_supervisor_tasks_get(request: Request, supervisor_id: str):
    session = request.state.session
    return await handle_supervisor_tasks_get(supervisor_id, session["user"])


@app.get("/api/supervisors/{supervisor_id}/tasks/{task_id}/stream")
async def _api_supervisor_task_stream(request: Request, supervisor_id: str, task_id: str):
    return await handle_supervisor_task_stream(request, supervisor_id, task_id)


@app.get("/api/supervisors/{supervisor_id}/messages")
async def _api_supervisor_messages(request: Request, supervisor_id: str):
    return await handle_supervisor_messages_get(request, supervisor_id)


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

    uvicorn.run("app:app", host=config.LISTEN_HOST, port=config.PORT, log_level="info")
