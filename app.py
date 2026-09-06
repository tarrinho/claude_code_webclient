# app.py -- WebConsole FastAPI application.
#
# Chat front-end for a local Claude Code CLI. Serves HTML pages, JSON APIs,
# and SSE token streams. All routes require auth except POST /login and
# static assets.
from __future__ import annotations

import configparser
import logging
import logging.config
import re
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles

import auth
import auto_answer
import config
import db
import rate_limit
import sysstats
import turns
from middleware import (  # registered below; the order of add_middleware
    AuthMiddleware,  # calls is what decides execution order, not this.
    CsrfMiddleware,
    SecurityMiddleware,
)
from net_validation import (  # re-exported: app._client_ip and
    _client_ip,
)

# `turns.launcher` is global wiring, so it stays in app.py for the same
# reason include_router and add_middleware do: a module that installs
# itself makes the wiring depend on import order.
from routes.chats import _deliver_answer, _launch_queued, _pending_options
from routes.chats import _pending_prompt as _auto_answer_pending
from routes.chats import router as chats_router
from routes.machines import router as machines_router
from routes.machines_tunnel import router as machines_tunnel_router

# _resolve_member (an orchestrators helper) adopts a session by calling the
# sessions route's handler, so this crosses prefixes. It travels to
# routes/orchestrators.py with that helper when the prefix is extracted.
from routes.misc import router as misc_router
from routes.orchestrators import router as orchestrators_router


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


turns.launcher = _launch_queued


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


async def handle_orchestrator_page(request: Request):
    try:
        return HTMLResponse((_WEB_DIR / "orchestrator.html").read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>Orchestrator template missing</h1>", status_code=500)


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
        except Exception:
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
    # Tunnel manager: background SSH tunnel lifecycle for ssh_proxy machines.
    import tunnel_manager
    # Unlike sysstats.start() just above (a sync function), tunnel_manager's
    # is `async def` -- called without await here, this only ever created a
    # coroutine object and discarded it. Its body, including the
    # `asyncio.create_task(_loop(...))` that makes the manager exist at all,
    # never ran: no reconnect scan at boot, no consumer for queue_command(),
    # nothing. Every other bug fixed in this same investigation (the request
    # body never being read, the machine_id int() cast, the un-awaited
    # ssh_tunnel_get/list_active reads) still left this as the reason a
    # queued START_TUNNEL command was accepted and then simply sat in the
    # queue forever -- confirmed live: `wc.tunnel_manager` never logged a
    # single line, and /api/tunnel/status/<id> stayed {"state": "none"}
    # indefinitely after a real start request.
    await tunnel_manager.start(db.system_sample_insert)
    # Answers permission and plan-approval prompts for chats whose owner armed
    # this. The lookups are injected from routes.chats rather than imported by
    # auto_answer, so that module carries no routes dependency and no cycle --
    # see docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md.
    auto_answer.start(_auto_answer_pending, _pending_options, _deliver_answer)
    # Rate limiter cleanup background task.
    rate_limit.start_cleanup(interval_s=300.0)
    # Auto-answer cooldown cleanup background task.
    await auto_answer._start_cooldown_cleanup()
    yield
    # Stopped before db.close(): the sampler writes through the connection.
    await rate_limit.stop_cleanup()
    await auto_answer._stop_cooldown_cleanup()
    await sysstats.stop()
    await auto_answer.stop()
    await tunnel_manager.stop()
    # Before db.close(): a turn cancelled here still runs its `finish`, which
    # needs the connection. Leaving them to be torn down with the loop instead
    # abandoned tasks mid-write.
    await turns.shutdown()
    await db.close()
    _log.info("WebConsole shutting down")


app = FastAPI(title="WebConsole", version=config.VERSION, lifespan=lifespan)
# Routes for /api/machines and /api/models. FastAPI matches in the order
# routers are included, so this line's position is the registration order.
app.include_router(machines_tunnel_router)
app.include_router(machines_router)
app.include_router(chats_router)
app.include_router(orchestrators_router)
app.include_router(misc_router)

app.add_middleware(
    CORSMiddleware, allow_origins=[], allow_methods=["*"], allow_headers=["*"]
)
app.add_middleware(CsrfMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityMiddleware)
app.add_middleware(rate_limit.RateLimitMiddleware)


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
    except Exception:
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


# ── API tokens ─────────────────────────────────────────────────────────────────
#
# The authenticated way in for a caller that cannot hold a cookie. This exists
# because the unauthenticated way kept being invented instead: a `/dev/*` prefix
# exempted from the auth middleware, with one route under it that minted an
# admin session for anybody who sent a GET. A script needs a credential, and the
# absence of one is what turns into a hole.


_MACHINE_PORT_RE = re.compile(r"^(?:0|[1-9]\d{0,4})$")


# ── Session routes ─────────────────────────────────────────────────────────────────


# Two paths, one page. "/orchestrator.html" is what index.html's iframe and its
# standalone fallback both ask for, so it cannot move; "/orchestrator" is what
# anyone types or bookmarks, and it 404'd. Served directly rather than
# redirected, matching how "/" and "/login" already serve their templates.
# Relative asset resolution is the same from both: with no trailing slash the
# base is "/", so "orchestrator.js" resolves to "/orchestrator.js" either way.
@app.get("/orchestrator")
@app.get("/orchestrator.html")
async def _serve_orchestrator_page(request: Request):
    return await handle_orchestrator_page(request)


# `_serve_orchestrator_js` was here. The orchestrator script lived in web/ rather
# than web/assets/, so nothing served it and this route existed to read the file
# and set `application/javascript` by hand -- browsers enforce strict MIME
# checking on scripts, and served as text/html it was refused outright, leaving
# the page rendering its markup with none of its behaviour.
#
# The script now lives under web/assets/orchestrator/, which StaticFiles already
# mounts, so the MIME type is correct without a route. Its old location is also
# why the section-4 timer scan globbed the wrong directory and missed a bare
# timer for a whole sweep: a file outside the directory everything else lives in
# is invisible to every check written against that directory.
#
# `_serve_orchestrator_page` below stays. StaticFiles is mounted on /assets only,
# so "/orchestrator" and "/orchestrator.html" still need a route of their own.


# ── Machine routes ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.LISTEN_HOST, port=config.PORT, log_level="info")
