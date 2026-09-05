"""Routes for /api/machines and /api/models, and the handlers behind them.

First module of the routes split. The cluster was measured the same way as the
earlier extractions: every function reached from a machines or models route,
closed over its private helpers. Thirty-one names, and after the shared layer
moved to shared.py the only things it still needed from app.py were a logger and
`app` itself -- and `app` is precisely what an APIRouter replaces.

Registration stays in app.py via include_router, for the same reason middleware
registration stayed there: a module that both defines and installs its routes
makes URL resolution order depend on import order. FastAPI matches routes in the
order they are added, so that order has to be readable in one place.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import urllib.request
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import backend_env
import config
import db
import runner
from net_validation import (
    _HOST_PATTERN_LOCAL,
    _base_url_host,
    _resolve_host,
    _validate_base_url,
    _validate_host,
)
from shared import _MODEL_RE, backend_kind

_log = logging.getLogger("wc.app")

router = APIRouter()


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
_MACHINE_PROVIDERS = {"anthropic", "proxy", "ssh_proxy"}


_ANTHROPIC_PORT = 443


# Required on every Anthropic API request; used for the probe and the model list.
_ANTHROPIC_API_VERSION = "2023-06-01"


# The model list comes from a user-configured endpoint, so cap what we read.
_MODELS_BODY_MAX = 1_048_576


# Opening Settings should not re-query the endpoint on every render.
_MODELS_CACHE_TTL_S = 60.0


_models_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


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


_PROBE_PROMPT = "Reply with exactly: ok"
_PROBE_TIMEOUT_S = 20.0


def _classify_cli_error(status: int | None, message: str) -> str:
    """"auth_failed", "unreachable" or plain "error", from a frame's status/text.

    ``status`` is the HTTP-shaped ``error_status`` a retry frame carries when
    the backend answered at all. Its absence, paired with connection-shaped
    wording, is what a DNS failure or a refused connection looks like -- the
    CLI never got a response to classify, same as the old raw probe's
    ``URLError``/timeout branch.
    """
    text = (message or "").lower()
    if status in (401, 403):
        return "auth_failed"
    if any(w in text for w in ("api key", "authentic", "permissiondenied", "unauthorized")):
        return "auth_failed"
    if status is None and any(
        w in text for w in
        ("connect", "refused", "unreachable", "resolve", "enotfound", "timed out")
    ):
        return "unreachable"
    return "error"


async def _test_anthropic_endpoint(machine: dict, api_key: str | None):
    """Probe the backend through the Claude Code CLI, not a raw HTTP client.

    This used to open its own connection straight to the provider and send
    ``x-api-key`` by hand -- the one place in this codebase that talked to a
    model API directly, against CLAUDE.md's governing rule: the console never
    calls a model API, it spawns ``claude``. It also gave a wrong answer for a
    *host-login* backend (no stored key -- credentials come from the CLI's own
    OAuth login instead): with no key to send, the raw probe always got 401 and
    reported "Endpoint requires an API key", even though every real turn on
    that exact backend succeeds through the CLI's login. Reported live:
    clicking Test on the official Anthropic API machine, which runs on the
    host's OAuth login.

    So this runs one real turn with the environment ``backend_env.deltas``
    builds for *this* machine -- the same function ``claude_proxy`` and
    ``runner`` use for a real turn -- and reads the CLI's own stream for the
    verdict, rather than asking the provider anything ourselves.

    The process is killed on the first decisive frame rather than let run to
    completion. A bad key makes the CLI retry with exponential backoff --
    measured: 10 attempts, delays growing past 16s -- and waiting for that
    would turn a connectivity check into a two-minute one. The first
    ``api_retry`` frame carries the same status and reason as the last, so
    nothing is lost by stopping there.
    """
    # A test is still an admin pointing this host's own network position at a
    # base_url they configured -- same SSRF exposure the old raw probe had, so
    # the same blocklist applies before the CLI is ever started.
    base_url = runner.normalise_base_url(machine.get("base_url")) or config.ANTHROPIC_BASE_URL
    _resolve_host(_base_url_host(base_url))

    model = (machine.get("model") or "").strip() or config.MODEL_NAME
    backend = {**machine, "api_key": api_key}
    env = backend_env.deltas(backend).apply_to(os.environ.copy())
    claude_bin = os.environ.get("WC_CLAUDE_PATH", "claude")
    cmd = [
        claude_bin, "-p", _PROBE_PROMPT,
        "--output-format", "stream-json", "--verbose",
        "--model", model, "--dangerously-skip-permissions",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except OSError as exc:
        _log.error("cli_probe_spawn_failed: %s", exc)
        return JSONResponse(
            {"ok": False, "status": "error", "error": "Could not start claude"},
            status_code=502,
        )

    verdict: dict | None = None

    async def read_frames() -> None:
        nonlocal verdict
        assert proc.stdout is not None
        async for raw in proc.stdout:
            try:
                frame = json.loads(raw)
            except ValueError:
                continue  # hook noise and stray warning lines are not JSON
            ftype, subtype = frame.get("type"), frame.get("subtype")
            if ftype == "system" and subtype == "api_retry":
                error = frame.get("error") or "Request failed"
                verdict = {
                    "ok": False,
                    "status": _classify_cli_error(frame.get("error_status"), error),
                    "error": error,
                }
                return
            if ftype == "system" and subtype == "error":
                error = frame.get("message") or frame.get("error") or "CLI error"
                verdict = {"ok": False, "status": "error", "error": error}
                return
            if ftype == "result":
                if frame.get("is_error"):
                    text = str(frame.get("result") or frame.get("error") or "")
                    verdict = {
                        "ok": False,
                        "status": _classify_cli_error(None, text),
                        "error": text[:300] or "Turn failed",
                    }
                else:
                    verdict = {"ok": True, "status": "reachable"}
                return

    try:
        await asyncio.wait_for(read_frames(), timeout=_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        verdict = {
            "ok": False,
            "status": "unreachable",
            "error": f"No response from claude within {int(_PROBE_TIMEOUT_S)}s",
        }
    finally:
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()

    if verdict is None:
        verdict = {"ok": False, "status": "error", "error": "claude exited with no result"}
    return JSONResponse(verdict, status_code=200 if verdict["ok"] else 502)


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
    reason: str | None, endpoint: str | None = None, machine: dict | None = None
) -> JSONResponse:
    """Fall back to the ids shipped with the app, saying why.

    The page previously showed a hardcoded list with no indication that it was
    a guess, so a model the service does not serve looked identical to one it
    does. The reason is surfaced instead of hidden.

    ``reason=None`` means the fallback is expected rather than a fault, and the
    page shows no warning for it. Only the 401/403-without-a-stored-key case
    uses that: see the call site for why it is normal here.
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
        # A stored key that the endpoint refuses is a real misconfiguration and
        # is still reported. No stored key is not: on this deployment an
        # anthropic machine without one is the *working* configuration, because
        # the CLI authenticates with the host's own login and this probe cannot
        # -- it speaks plain HTTP and holds no OAuth token. Reporting that as
        # "the endpoint requires an API key" described the probe's limitation as
        # a fault in the backend, on a backend that was serving turns fine.
        # reason=None marks the fallback as expected, and the page stays quiet.
        return _builtin_models(
            "The endpoint rejected the API key." if api_key else None,
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


@router.get("/api/models")
async def _api_models_list(request: Request):
    return await handle_models_list(request)


@router.put("/api/machines/{machine_id}/models")
async def _api_machine_models_set(request: Request, machine_id: str):
    return await handle_machine_models_set(request, machine_id)


@router.get("/api/machines")
async def _api_machines_list(request: Request):
    return await handle_machines_list(request)


@router.post("/api/machines")
async def _api_machine_create(request: Request):
    return await handle_machine_create(request)


@router.get("/api/machines/{machine_id}")
async def _api_machine_get(request: Request, machine_id: str):
    return await handle_machine_get(request, machine_id)


@router.patch("/api/machines/{machine_id}")
async def _api_machine_patch(request: Request, machine_id: str):
    return await handle_machine_patch(request, machine_id)


@router.post("/api/machines/{machine_id}/activate")
async def _api_machine_activate(request: Request, machine_id: str):
    return await handle_machine_activate(request, machine_id)


@router.post("/api/machines/{machine_id}/test")
async def _api_machine_test(request: Request, machine_id: str):
    return await handle_machine_test(request, machine_id)


@router.delete("/api/machines/{machine_id}")
async def _api_machine_delete(request: Request, machine_id: str):
    return await handle_machine_delete(request, machine_id)
