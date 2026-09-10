# app.py -- WebConsole FastAPI application.
#
# Chat front-end for a local Claude Code CLI. Serves HTML pages, JSON APIs,
# and SSE token streams. All routes require auth except POST /login and
# static assets.
from __future__ import annotations

import asyncio
import configparser
import logging
import logging.config
import os
import re
import time
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
import sync_request_watcher
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
from routes.qa import router as qa_router
from routes.supervisor_map import router as supervisor_map_router
from routes.transports import router as transports_router


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


# Per-step ceiling on startup work, and it is deliberately below
# wc-health.sh's 45-second boot grace.
#
# Startup runs before uvicorn binds the socket, so a step that hangs leaves a
# process systemd considers healthy and nothing listening on the port. That is
# invisible from the outside and indistinguishable from a wedged server, so
# wc-health.sh restarts it -- killing the boot, which starts again, and hangs
# again. Measured on 2026-09-07: six consecutive boots between 12:25 and 12:31,
# a restart every ~70s, the site down the whole time. Every one of those boots
# logged "WebConsole starting" and "PROJECTS_ROOT=..." and then nothing at all,
# which named the window but not the step inside it.
#
# Two things follow from that outage, and both are the point of this block:
#
# * Failing inside the grace window means the process ends itself before the
#   health check can race it. systemd's Restart=always then does the cycling,
#   one restarter instead of two.
# * Every step is timed and logged by name, so the next occurrence says which
#   one hung instead of leaving a silent gap to bisect.
_STARTUP_STEP_TIMEOUT_S: Final[float] = float(
    os.environ.get("WC_STARTUP_STEP_TIMEOUT_S", "20")
)

# Anything slower than this is worth a WARNING even when it completes: it is
# the early warning for the hang, seen before it becomes one.
_STARTUP_STEP_SLOW_S: Final[float] = 2.0


# Background startup tasks kept referenced: asyncio only holds a weak
# reference to a running task, so a bare create_task can be collected
# mid-flight. The done-callback is what stops a crash in one of these being
# silently swallowed -- an unobserved task's exception is only reported when
# the task is garbage collected, which may be never.
_startup_tasks: list[asyncio.Task] = []


def _log_startup_task(task: asyncio.Task) -> None:
    if task.cancelled():
        _log.info("startup_task_cancelled task=%s", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        _log.error("startup_task_failed task=%s: %r", task.get_name(), exc)


async def _startup_step(name: str, awaitable):
    """Run one startup step under a timeout, recording what it cost.

    Synchronous steps must be handed over as ``asyncio.to_thread(fn)``. A bare
    blocking call cannot be timed out at all -- ``wait_for`` can only abandon a
    coroutine, never interrupt a thread already inside ``sqlite3`` -- and it
    also blocks the event loop while it runs. Abandoning it at least gets the
    process to a decision instead of hanging forever with the port unbound.
    """
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(awaitable, _STARTUP_STEP_TIMEOUT_S)
    except asyncio.TimeoutError:
        _log.error(
            "startup_step_timeout step=%s after=%.0fs — refusing to start with "
            "an unbound port; systemd will restart this process",
            name, _STARTUP_STEP_TIMEOUT_S,
        )
        raise
    elapsed = time.monotonic() - started
    if elapsed >= _STARTUP_STEP_SLOW_S:
        _log.warning("startup_step_slow step=%s took=%.1fs", name, elapsed)
    else:
        _log.info("startup_step step=%s took=%.2fs", name, elapsed)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("WebConsole starting v%s", config.VERSION)
    _log.info(
        "PROJECTS_ROOT=%s HOST=%s PORT=%d",
        config.PROJECTS_ROOT,
        config.HOST,
        config.PORT,
    )
    await _startup_step("db.init", db.init())
    # Sessions: migrate from DB_PATH to SESSION_DB_PATH if they still share
    # the old combined file (registry #47). Runs once, idempotent.
    #
    # Through a thread: it is synchronous sqlite3 against the *production*
    # database, on the event loop, before the socket is bound.
    migrated = await _startup_step(
        "migrate_sessions", asyncio.to_thread(auth._migrate_sessions_to_own_db)
    )
    if migrated:
        _log.info("migrated %d session(s) to a separate database", migrated)
    # Load DB settings into env so config.py can see them at runtime.
    await _startup_step("load_settings", _load_settings_from_db())
    config.validate()
    restored = await _startup_step(
        "load_sessions", asyncio.to_thread(auth.load_sessions)
    )
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
    # Scheduled, not awaited. Awaiting it made SSH connectivity a
    # precondition for binding the port: on 2026-09-10 startup logged
    # db.init/migrate_sessions/load_settings/load_sessions and then stopped
    # dead here, the process sat in futex_do_wait with hundreds of paramiko
    # reconnects a minute in the log, and the site served nothing for ~25
    # minutes across two releases and a rollback. Uvicorn does not serve until
    # the lifespan startup returns, so anything that can block indefinitely
    # here takes the whole console down with it -- and the transports are the
    # one thing here that talks to other machines.
    #
    # NOT the same mistake as calling it bare: that created a coroutine and
    # discarded it, so the manager never existed. create_task schedules it and
    # the reference is held below, so it runs -- just not in the critical path.
    _startup_tasks.append(asyncio.create_task(
        tunnel_manager.start(db.system_sample_insert), name="tunnel_manager.start",
    ))
    _startup_tasks[-1].add_done_callback(_log_startup_task)
    # Answers permission and plan-approval prompts for chats whose owner armed
    # this. The lookups are injected from routes.chats rather than imported by
    # auto_answer, so that module carries no routes dependency and no cycle --
    # see docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md.
    auto_answer.start(_auto_answer_pending, _pending_options, _deliver_answer)
    # Rate limiter cleanup background task.
    rate_limit.start_cleanup(interval_s=300.0)
    # Auto-answer cooldown cleanup background task.
    await auto_answer._start_cooldown_cleanup()
    # Watches incoming agent traffic for a SYNC_REQUEST marker and queues a
    # pending transport_sync_requests row for a human to approve. See
    # docs/superpowers/specs/2026-09-09-transport-project-sync-design.md.
    #
    # Off by default, and deliberately so -- see the measurement recorded at
    # config.SYNC_REQUEST_WATCHER_ENABLED. Its poll re-reads ~450MB of
    # transcripts per pass, which on this host was 417MB of disk read per 20s
    # and 76% of a core, permanently. Re-enable once
    # transcripts._agent_events_sync tail-reads. The Sync button (the other
    # half of transport sync) does not go through here and is unaffected.
    if config.SYNC_REQUEST_WATCHER_ENABLED:
        sync_request_watcher.start(config.SYNC_REQUEST_WATCHER_INTERVAL_S)
    else:
        _log.info("sync_request_watcher disabled (config.SYNC_REQUEST_WATCHER_ENABLED)")
    # In-memory cache for remote session data, refreshed in the background so
    # request handlers get instant results.
    #
    # Two things were wrong with refreshing this every 3 seconds with
    # create_task. A pass opens an SSH connection to every transport and reads
    # one file per session, so it takes far longer than 3 seconds -- and
    # create_task never waits, so passes *stacked*: four transports' worth of
    # SSH handshakes launched every 3 seconds while earlier passes were still
    # running. With 36 sessions across four transports, /login took 75 seconds
    # to answer on 2026-09-10.
    #
    # Awaited rather than spawned, so exactly one pass runs at a time, and on
    # an interval that reflects what is being watched: a list of CLI sessions
    # on other machines does not change every three seconds.
    #
    # Only starts when remote session discovery is enabled; otherwise the
    # cache is never populated at startup and read_claude_sessions() falls
    # through to the local read, keeping tests that patch a sessions dir
    # from being poisoned by the pre-warmed cache.
    import routes.db_sessions as _db_sessions
    # Skip starting the loop, but do NOT return from the lifespan here. An
    # early `yield; return` on this path -- which is the default, since
    # REMOTE_SESSIONS is off -- skipped the whole shutdown block below:
    # turns.shutdown(), db.close(), tunnel_manager.stop() and the rest never
    # ran on any normal restart. The comment on turns.shutdown() states the
    # cost precisely: a turn cancelled there still runs its `finish`, which
    # needs the connection, and leaving them to be torn down with the loop
    # abandons tasks mid-write.
    _cache_interval = config.REMOTE_SESSION_CACHE_S

    async def _cache_refresh_loop() -> None:
        while True:
            try:
                await _db_sessions.update_sessions_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One failed refresh must not end the loop: the cache would
                # then be silently frozen at whatever it last held, which
                # reads exactly like a working cache.
                _log.exception("update_sessions_cache failed; retrying next tick")
            await asyncio.sleep(_cache_interval)

    if config.REMOTE_SESSIONS:
        _startup_tasks.append(asyncio.create_task(
            _cache_refresh_loop(), name="sessions_cache_refresh"))
        _startup_tasks[-1].add_done_callback(_log_startup_task)
    else:
        _log.info(
            "remote session discovery disabled (config.REMOTE_SESSIONS); "
            "the sessions cache will not be refreshed"
        )

    yield
    # Stopped before db.close(): the sampler writes through the connection.
    await rate_limit.stop_cleanup()
    await auto_answer._stop_cooldown_cleanup()
    await sysstats.stop()
    await auto_answer.stop()
    await sync_request_watcher.stop()
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
app.include_router(supervisor_map_router)
app.include_router(misc_router)
app.include_router(transports_router)
app.include_router(qa_router)

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
        # Deliberately silent, and the one broad catch here that should stay
        # that way: this wraps the *logging* of an error response, so there is
        # nowhere left to report a failure to. Anything raised here would
        # replace the client's real 4xx/5xx with a 500 about logging.
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
