"""Claude Code runner — subprocess (direct) or TCP proxy modes.

When PROXY_ENABLED=True the runner connects to the host-side proxy
(claude_proxy.py) over TCP and communicates via newline-delimited JSON:

  runner → proxy stdin:    {"type":"turn","prompt":"…","session_id":"…","work_dir":"…"}
  proxy → runner stdout:   {"type":"text","content":"…"}
                         {"type":"start","chat_id":"…"}
                         {"type":"message","id":"…"}
                         {"type":"done"}

When PROXY_ENABLED=False the runner spawns `claude` directly (legacy path).
All work is gated by a concurrency semaphore so we never exceed
MAX_CONCURRENT processes or TCP slots.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import config

_log: object = __import__("loguru").logger.bind(service="runner")


class TurnError(Exception):
    """Fatal or non-fatal turn error."""

    def __init__(self, message: str, fatal: bool = True):
        self.message = message
        self.fatal = fatal
        super().__init__(message)


def _int_or_zero(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def usage_frame(obj: dict) -> dict | None:
    """Build a ``usage`` event from a Claude Code ``result`` frame.

    Mirrors claude_proxy.usage_frame — the proxy runs as a standalone host
    process and imports nothing from this package, so the parser exists on both
    sides rather than being shared. Keep the two in step.

    ``modelUsage`` is keyed by model id and preferred; its keys are camelCase
    while flat ``usage`` is snake_case. When only the flat form is present the
    model is unknown here and reported under "", for the caller to resolve.
    """
    models: dict[str, dict[str, int]] = {}
    model_usage = obj.get("modelUsage")
    if isinstance(model_usage, dict):
        for name, stats in model_usage.items():
            if not isinstance(name, str) or not isinstance(stats, dict):
                continue
            basis = stats.get("costBasis")
            models[name] = {
                "input_tokens": _int_or_zero(stats.get("inputTokens")),
                "output_tokens": _int_or_zero(stats.get("outputTokens")),
                "cache_read_tokens": _int_or_zero(stats.get("cacheReadInputTokens")),
                "cache_creation_tokens": _int_or_zero(
                    stats.get("cacheCreationInputTokens")
                ),
                # The CLI's own assessment of whether its cost figure means
                # anything. Recorded to explain a suppressed cost, never to
                # decide it -- see _usage_provider in app.py.
                "cost_basis": basis if isinstance(basis, str) else None,
            }
    if not models:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            totals = {
                "input_tokens": _int_or_zero(usage.get("input_tokens")),
                "output_tokens": _int_or_zero(usage.get("output_tokens")),
                "cache_read_tokens": _int_or_zero(usage.get("cache_read_input_tokens")),
                "cache_creation_tokens": _int_or_zero(
                    usage.get("cache_creation_input_tokens")
                ),
            }
            if any(totals.values()):
                models[""] = totals
    if not models:
        return None

    cost = obj.get("total_cost_usd")
    return {
        "type": "usage",
        "models": models,
        "cost_usd": cost if isinstance(cost, (int, float)) else None,
        "duration_ms": _int_or_zero(obj.get("duration_ms")) or None,
        "is_error": bool(obj.get("is_error")),
    }


def _normalise_cli_frame(obj: dict) -> list[dict]:
    """Translate Claude Code stream-json output into runner events."""
    frame_type = obj.get("type", "")
    subtype = obj.get("subtype", "")
    events: list[dict] = []

    if frame_type == "system":
        if subtype == "init":
            if obj.get("session_id"):
                events.append({"type": "session_id", "session_id": obj["session_id"]})
            if obj.get("model"):
                events.append({"type": "model", "model": obj["model"]})
        elif subtype == "api_retry":
            events.append(
                {
                    "type": "status",
                    "status": "api_retry",
                    "attempt": obj.get("attempt"),
                    "max_retries": obj.get("max_retries"),
                    "retry_delay_ms": obj.get("retry_delay_ms"),
                    "error": obj.get("error", "API request failed"),
                }
            )
        elif subtype == "error":
            events.append(
                {
                    "type": "error",
                    "error": obj.get("message") or obj.get("error") or str(obj),
                }
            )
    elif frame_type == "assistant":
        message = obj.get("message", {})
        if isinstance(message, dict):
            for block in message.get("content", []):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and block.get("text")
                ):
                    events.append({"type": "text", "content": block["text"]})
    elif frame_type == "message":
        for block in obj.get("content", []):
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and block.get("text")
            ):
                events.append({"type": "text", "content": block["text"]})
    elif frame_type == "result":
        if obj.get("session_id"):
            events.append({"type": "session_id", "session_id": obj["session_id"]})
        usage = usage_frame(obj)
        if usage:
            events.append(usage)
        if obj.get("is_error") or subtype not in ("", "success"):
            error = (
                obj.get("error")
                or obj.get("result")
                or obj.get("errors")
                or "Claude turn failed"
            )
            events.append(
                {
                    "type": "error",
                    "error": error if isinstance(error, str) else json.dumps(error),
                }
            )
    elif frame_type == "error" or obj.get("is_error"):
        events.append(
            {
                "type": "error",
                "error": obj.get("error") or obj.get("message") or str(obj),
            }
        )

    return events


# ── Concurrency gate (singleton, lazy init) ──────────────────────────────────

_sem: object = None
_models_by_chat: dict[str, str] = {}
_usage_by_chat: dict[str, dict] = {}
_skills_by_session: dict[str, set[str]] = {}


def take_last_model(chat_id: str) -> str:
    """Return and clear the last model reported for a blocking turn."""
    return _models_by_chat.pop(chat_id, "")


def take_last_usage(chat_id: str) -> dict:
    """Return and clear the usage reported for a blocking turn.

    Same hand-off as take_last_model: the blocking paths consume frames
    internally, so the handler collects the result afterwards. Streaming paths
    yield the usage event straight through and are recorded by the handler as
    the event arrives.
    """
    return _usage_by_chat.pop(chat_id, {})


def record_usage_frame(chat_id: str, frame: dict) -> None:
    """Stash a usage frame, resolving the model when the frame did not know it.

    A flat ``usage`` object carries no model name, so it arrives keyed by "".
    The session's model is known here, so substitute it rather than storing a
    row that cannot be attributed.
    """
    models = frame.get("models") or {}
    if "" in models:
        resolved = _models_by_chat.get(chat_id) or ""
        models = {(resolved or "unknown"): models.pop("")} | models
        frame = {**frame, "models": models}
    _usage_by_chat[chat_id] = frame


def active_skills(session_id: str | None) -> list[str]:
    """Return skills observed in a Claude session, sorted for stable API output."""
    if not session_id:
        return []
    return sorted(_skills_by_session.get(session_id, set()))


def _record_skill(session_id: str | None, name: str | None) -> None:
    if session_id and name:
        _skills_by_session.setdefault(session_id, set()).add(name)


async def get_proxy_host() -> str:
    """Return the proxy transport host, falling back to environment config."""
    import db

    if db.db_conn is None:
        return config.PROXY_HOST
    return await db.setting_get("ai_machine_host") or config.PROXY_HOST


async def get_default_model(chat_id: str | None = None, owner: str | None = None) -> str:
    """Return the model a new turn should use.

    Resolution order: the conversation's own model, then its backend's default,
    then the global setting, then environment config.

    The conversation comes first so a model chosen for one chat sticks across
    its turns without following the others. Its backend comes next -- and it is
    the conversation's backend, not merely the active one, because a model id is
    only meaningful against the backend serving it: a gateway default of
    "vllm/Qwen3.6-..." is wrong for the Anthropic endpoint and vice versa.
    """
    import db

    if db.db_conn is None:
        return config.MODEL_NAME
    if chat_id:
        routing = await db.chat_routing(chat_id)
        if (routing.get("model") or "").strip():
            return routing["model"].strip()
        machine = routing.get("machine")
        if machine and (machine.get("model") or "").strip():
            return machine["model"].strip()
    # Same fallback as get_backend, and for the same caller: the supervisor's
    # ids are not conversations, so the branch above finds nothing and the CLI
    # was left to pick its own default. A gateway that serves only one local
    # model then answered 429 "No deployments available for selected model,
    # Passed model=claude-opus-5" -- a routing failure reported as a capacity
    # one, which is the hardest kind to read.
    if owner:
        machine = await db.ai_machine_backend(owner)
        if machine and (machine.get("model") or "").strip():
            return machine["model"].strip()
    return await db.setting_get("default_model") or config.MODEL_NAME


def normalise_base_url(base_url: str | None) -> str | None:
    """Return *base_url* as an origin the Claude CLI can append paths to.

    Two shapes get corrected:

    * A bare host ("api.anthropic.com"). Databases written before the base_url
      validator was fixed hold these, because the validator used to return only
      the host and the caller persisted that. Scheme-less, the CLI would read
      it as a relative URL, so assume https.
    * A trailing "/v1". The CLI appends /v1/messages itself, so a URL copied
      from an OpenAI-style config would resolve to /v1/v1/messages and 404.
    """
    if not base_url:
        return None
    base_url = base_url.strip()
    if not base_url:
        return None
    if "://" not in base_url:
        base_url = f"https://{base_url}"
    base_url = base_url.rstrip("/").removesuffix("/v1")
    return base_url or None


async def get_backend(chat_id: str, owner: str | None = None) -> dict[str, str]:
    """Return the provider settings for the machine *chat_id* should run on.

    A conversation pinned to a machine uses that one, so two conversations can
    sit on different backends at once; an unpinned conversation follows the
    owner's active machine, as every conversation did before pinning existed.

    *owner* is the fallback for a caller whose chat_id is not a conversation at
    all. The supervisor engine invents ids -- ``uuid4().hex`` for a planning
    turn, ``subtask_<id>`` for each task -- so chat_routing found no row, owner
    came back None, and this returned {}. That left the child with no base URL
    and no key while CLAUDE_CODE_SIMPLE=1 also blocked the host login, so every
    supervisor turn died on "Not logged in - Please run /login". The engine
    could not reach any configured backend at all, which is why the feature had
    never once run a task.

    Empty when no machine applies, which leaves the CLI on its own defaults --
    the host's `claude` login against the official API.
    """
    import db

    if db.db_conn is None:
        return {}
    routing = await db.chat_routing(chat_id)
    if not routing["owner"]:
        if not owner:
            return {}
        # Not a conversation: fall back to this owner's active machine, which is
        # what an unpinned conversation would have used anyway.
        routing = {"owner": owner, "model": None,
                   "machine": await db.ai_machine_backend(owner), "pinned": False}
    machine = routing["machine"]
    if not machine or machine.get("provider") != "anthropic":
        return {}
    backend: dict[str, str] = {"provider": "anthropic"}
    base_url = normalise_base_url(machine.get("base_url"))
    if base_url:
        backend["base_url"] = base_url
    # An absent key is meaningful: the CLI then uses the host's own login
    # rather than failing, so never send an empty string.
    api_key = (machine.get("api_key") or "").strip()
    if api_key:
        backend["api_key"] = api_key
    return backend


def slots_busy() -> bool:
    """Whether every concurrent-turn slot is taken.

    A turn that has to wait for a slot looks identical to a slow one from the
    browser -- running, with nothing arriving. Background turns make that far
    easier to hit, because several conversations can be mid-turn at once, so the
    wait is now worth saying out loud instead of leaving the user to guess.
    """
    return _sem is not None and _sem.locked()


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.MAX_CONCURRENT)
    return _sem


# ──────────────────────────────────────────────────────────────────────────────
# Proxy mode — TCP connection to host-side proxy.py
# ──────────────────────────────────────────────────────────────────────────────


async def _proxy_turn(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
    owner: str | None = None,
) -> tuple[list[str], str | None]:
    """Execute one turn over TCP to the host claude_proxy.

    Sends a single JSON message to the proxy, reads back NDJSON chunks,
    and returns (text_chunks, session_id).
    """
    sem = _get_sem()
    async with sem:
        return await _execute_proxy(prompt, session_id, work_dir, chat_id, model,
                                    owner)


async def _execute_proxy(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,

    owner: str | None = None,) -> tuple[list[str], str | None]:
    """Core proxy turn: connect → send turn → read NDJSON → disconnect."""
    connect_timeout = config.PROXY_CONNECT_TIMEOUT_S
    turn_timeout = config.PROXY_TURN_TIMEOUT_S

    # The direct path logs every launch; the proxy path logged only failures,
    # so a turn that worked left no trace of having run and one that hung left
    # nothing to say where it stopped. Proxy mode is the default, which made
    # this the common case rather than the rare one.
    _log.info("proxy turn chat={} work_dir={} model={}", chat_id, work_dir, model or "default")

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    proxy_host = await get_proxy_host()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, config.PROXY_PORT),
            timeout=connect_timeout,
        )
    except (asyncio.TimeoutError, OSError, ConnectionRefusedError) as exc:
        _log.error(
            "proxy connect failed host={} port={}: {}",
            proxy_host,
            config.PROXY_PORT,
            exc,
        )
        raise TurnError(
            f"Cannot connect to proxy at {proxy_host}:{config.PROXY_PORT}", fatal=False
        )

    try:
        # Send handshake
        writer.write(
            (
                json.dumps(
                    {
                        "type": "handshake",
                        "protocol": config.PROTOCOL,
                        "token": config.PROXY_TOKEN,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()

        # Read handshake ACK from proxy
        try:
            ack_raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5)
            json.loads(ack_raw)
        except (asyncio.TimeoutError, ValueError, json.JSONDecodeError):
            _log.warning("proxy handshake ACK unexpected")

        # Build turn payload. NOTE: this frame is plaintext JSON over TCP and
        # may carry an API key, so the proxy must stay bound to loopback (or
        # WC_PROXY_LISTEN_HOST must front a tunnel) -- see claude_proxy.main().
        turn_payload = {
            "type": "turn",
            "prompt": prompt,
            "session_id": session_id,
            "work_dir": work_dir,
            "model": model or await get_default_model(chat_id, owner),
        }
        backend = await get_backend(chat_id, owner)
        if backend:
            turn_payload["backend"] = backend
        writer.write((json.dumps(turn_payload) + "\n").encode())
        await writer.drain()

        # Read response stream
        chunks: list[str] = []
        sid: str | None = session_id
        error: str | None = None

        try:
            async with asyncio.timeout(turn_timeout):
                async for raw_line in _read_lines(reader):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        _log.debug("non-JSON from proxy: {}", line[:200])
                        continue

                    msg_type = obj.get("type", "")
                    if msg_type == "session_id" and obj.get("session_id"):
                        sid = obj["session_id"]
                    elif msg_type == "model" and obj.get("model"):
                        _models_by_chat[chat_id] = obj["model"]
                    elif msg_type == "usage":
                        record_usage_frame(chat_id, obj)
                    elif msg_type == "skill":
                        _record_skill(sid, obj.get("name"))
                    elif msg_type == "text":
                        text = obj.get("content", "") or obj.get("text", "")
                        if text:
                            chunks.append(text)
                    elif msg_type == "error":
                        error = obj.get("error", str(obj))
                    elif msg_type == "done":
                        break
        except TimeoutError:
            _log.warning("proxy read timed out (turn_timeout={}s)", turn_timeout)
            raise TurnError(f"Turn timed out after {turn_timeout}s", fatal=False)

        if error:
            raise TurnError(error, fatal=False)
        return chunks, sid

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _read_lines(reader: asyncio.StreamReader):
    """Yield bytes lines from the reader until EOF/cancel."""
    buf = b""
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line
    except BrokenPipeError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Direct subprocess mode (legacy — when PROXY_ENABLED=False)
# ──────────────────────────────────────────────────────────────────────────────


# What `claude --resume` accepts: a UUID. Case-insensitive, because the
# lowercase-only version silently discarded an uppercase one.
_RESUMABLE_SESSION_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _build_cmd_direct(
    prompt: str, session_id: str | None, model: str | None = None
) -> list[str]:
    """Build the argument list for the Claude Code subprocess.

    ``-p``/``--print`` is a boolean flag and the prompt is a positional
    argument, so the prompt -- and only the prompt -- goes after the ``--``
    sentinel, where a leading dash cannot be mistaken for a CLI flag.
    Session flags stay before the sentinel so they are parsed as options.
    """
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd.extend(["--model", model])
    if session_id and _RESUMABLE_SESSION_RE.fullmatch(session_id):
        cmd.extend(["--resume", session_id])
    else:
        # A fresh session. Two callers land here legitimately: a new
        # conversation with no session_id, and the supervisor engine, which
        # passes a "supervisor_<uuid>" marker precisely because it wants a new
        # one each time.
        #
        # But an id that was supplied and rejected is a different event: it
        # means a conversation's entire history is being discarded. That used to
        # happen with no log line at all -- registry #21's symptom arriving
        # through a different door -- and the pattern was lowercase-only, so an
        # uppercase UUID was enough to trigger it. Now case-insensitive, and a
        # rejected id says so.
        if session_id and not session_id.startswith("supervisor_"):
            _log.warning(
                "session_id not resumable, starting a new session and losing "
                "history: %r", session_id,
            )
        cmd.extend(["--session-id", str(uuid.uuid4())])
    cmd.extend(["--", prompt])
    return cmd


def _build_env(backend: dict[str, str] | None = None) -> dict[str, str]:
    """Filter environment — pass model config, strip host vars.

    With an ``anthropic`` *backend* the CLI is pointed at the official API the
    way Claude Code configures itself: ANTHROPIC_BASE_URL for the endpoint and
    ANTHROPIC_API_KEY for auth. The OpenAI-compatible variables are for the
    local shim and are left out entirely in that case, so a stale OPENAI_BASE_URL
    cannot pull the turn back to the shim.
    """
    safe = {"HOME", "PATH", "SHELL", "LANG", "LC_ALL", "TERM"}
    env = {k: v for k, v in os.environ.items() if k in safe}
    env["PYTHONUNBUFFERED"] = "1"
    env["CLAUDE_CODE_SIMPLE"] = "1"
    # Opt out of experimental beta features, matching the proxy path. Set before
    # the provider branch so it applies to both, and set explicitly rather than
    # inherited because the allowlist above drops everything else.
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    if backend and backend.get("provider") == "anthropic":
        base_url = normalise_base_url(backend.get("base_url"))
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        if backend.get("api_key"):
            env["ANTHROPIC_API_KEY"] = backend["api_key"]
        else:
            # No key: the CLI has to read the host's own login, and under
            # CLAUDE_CODE_SIMPLE it refuses to -- `claude auth status` reports
            # loggedIn:false, authMethod:none with that set. Leaving it on here
            # would give the subprocess no credentials at all.
            env.pop("CLAUDE_CODE_SIMPLE", None)
        return env
    if config.MODEL_BASE_URL:
        env["OPENAI_BASE_URL"] = config.MODEL_BASE_URL
    if config.MODEL_API_KEY:
        env["OPENAI_API_KEY"] = config.MODEL_API_KEY
    env["OPENAI_MODEL_NAME"] = config.MODEL_NAME
    return env


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate subprocess on timeout/error."""
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def _execute_direct(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
    owner: str | None = None,
) -> tuple[list[str], str | None]:
    """Core subprocess execution with NDJSON parsing (blocking path)."""
    cmd = _build_cmd_direct(prompt, session_id, model)

    _log.info("launch chat={} work_dir={} cmd={}", chat_id, work_dir, cmd[:4])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        env=_build_env(await get_backend(chat_id, owner)),
    )

    chunks: list[str] = []
    sid: str | None = None

    try:
        chunks, sid = await asyncio.wait_for(
            _collect_chunks(proc, chat_id), timeout=config.TURN_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        await _kill_process(proc)
        raise TurnError(f"Turn timed out after {config.TURN_TIMEOUT_S}s", fatal=False)

    return chunks, sid


async def _collect_chunks(
    proc: asyncio.subprocess.Process, chat_id: str
) -> tuple[list[str], str | None]:
    """Read Claude NDJSON from stdout and extract text and session ID."""
    chunks: list[str] = []
    session_id_val: str | None = None
    error: str | None = None

    if proc.stdout is None:
        return chunks, session_id_val

    async for raw_line in _read_lines(proc.stdout):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            _log.debug("non-JSON line: {}", line[:200])
            continue

        for event in _normalise_cli_frame(obj):
            if event["type"] == "text":
                chunks.append(event["content"])
            elif event["type"] == "session_id":
                session_id_val = event["session_id"]
            elif event["type"] == "model":
                _models_by_chat[chat_id] = event["model"]
            elif event["type"] == "usage":
                record_usage_frame(chat_id, event)
            elif event["type"] == "error":
                error = event["error"]

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        await _kill_process(proc)

    if error:
        raise TurnError(error, fatal=False)
    return chunks, session_id_val


# ──────────────────────────────────────────────────────────────────────────────
# Proxy streaming mode — SSE via TCP
# ──────────────────────────────────────────────────────────────────────────────


async def _proxy_stream_turn(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream a turn over TCP to the host claude_proxy."""
    sem = _get_sem()
    async with sem:
        try:
            async for event in _do_proxy_stream(
                prompt, session_id, work_dir, chat_id, model
            ):
                yield event
        except TurnError as e:
            yield {"type": "error", "error": e.message}


async def _do_proxy_stream(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,

    owner: str | None = None,) -> AsyncGenerator[dict, None]:
    """Connect to proxy, send turn, yield events from the NDJSON stream."""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    proxy_host = await get_proxy_host()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, config.PROXY_PORT),
            timeout=config.PROXY_CONNECT_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError, ConnectionRefusedError) as exc:
        _log.error("proxy connect failed: {}", exc)
        yield {
            "type": "error",
            "error": f"Cannot connect to proxy at {proxy_host}:{config.PROXY_PORT}",
        }
        return

    try:
        # Send handshake
        writer.write(
            (
                json.dumps(
                    {
                        "type": "handshake",
                        "protocol": config.PROTOCOL,
                        "token": config.PROXY_TOKEN,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()

        try:
            ack_raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5)
            json.loads(ack_raw)
        except (asyncio.TimeoutError, ValueError, json.JSONDecodeError):
            _log.warning("proxy handshake ACK unexpected")

        # Send turn. The backend must travel with the streaming turn exactly as
        # it does with the blocking one: without it the proxy spawns the CLI
        # against the default Anthropic endpoint, so a chat routed to a custom
        # gateway failed on every streamed turn while the same prompt through
        # /messages succeeded.
        turn_payload: dict[str, object] = {
            "type": "turn",
            "prompt": prompt,
            "session_id": session_id,
            "work_dir": work_dir,
            "model": model or await get_default_model(chat_id, owner),
        }
        backend = await get_backend(chat_id, owner)
        if backend:
            turn_payload["backend"] = backend
        writer.write((json.dumps(turn_payload) + "\n").encode())
        await writer.drain()

        # Stream events. A clean turn must include an explicit done frame.
        completed = False
        try:
            async with asyncio.timeout(config.PROXY_TURN_TIMEOUT_S):
                async for raw_line in _read_lines(reader):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        _log.debug("non-JSON from proxy: {}", line[:200])
                        continue

                    msg_type = obj.get("type", "")
                    if msg_type == "skill":
                        _record_skill(session_id, obj.get("name"))
                        yield obj
                    elif msg_type in (
                        "text",
                        "session_id",
                        "model",
                        "status",
                        "error",
                        # Streaming records usage in the handler as the event
                        # arrives, so it is passed through rather than stashed.
                        "usage",
                    ):
                        yield obj
                    elif msg_type == "done":
                        completed = True
                        yield {"type": "done"}
                        break
            if not completed:
                yield {"type": "error", "error": "Proxy stream ended before completion"}
        except TimeoutError:
            yield {
                "type": "error",
                "error": f"Stream timed out after {config.PROXY_TURN_TIMEOUT_S}s",
            }

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Direct subprocess streaming mode (legacy)
# ──────────────────────────────────────────────────────────────────────────────


async def _execute_direct_stream(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream a turn by spawning claude subprocess directly."""
    sem = _get_sem()
    async with sem:
        async for event in _do_direct_stream(
            prompt, session_id, work_dir, chat_id, model
        ):
            yield event


async def _do_direct_stream(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
    owner: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Core subprocess streaming (behind semaphore)."""
    cmd = _build_cmd_direct(prompt, session_id, model)

    _log.info("stream chat={} work_dir={}", chat_id, work_dir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        env=_build_env(await get_backend(chat_id, owner)),
    )

    try:
        if proc.stdout is None:
            yield {"type": "error", "error": "no stdout"}
            return

        async for raw_line in _read_lines(proc.stdout):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            for event in _normalise_cli_frame(obj):
                yield event

        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            await _kill_process(proc)

        if proc.returncode:
            stderr = ""
            if proc.stderr is not None:
                stderr = (
                    (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
                )
            yield {
                "type": "error",
                "error": stderr[-2000:] or f"Claude exited with code {proc.returncode}",
            }
        else:
            yield {"type": "done"}

    except asyncio.CancelledError:
        await _kill_process(proc)
        raise
    except Exception as e:  # noqa: BLE001 -- relay subprocess failures as stream errors
        await _kill_process(proc)
        yield {"type": "error", "error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


async def run_turn(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
    owner: str | None = None,
) -> tuple[list[str], str | None]:
    """Execute one turn and return (text_chunks, session_id)."""
    if len(prompt) > config.PROMPT_MAX_CHARS:
        raise TurnError(
            f"Prompt too long: {len(prompt)} chars (max {config.PROMPT_MAX_CHARS})",
            fatal=True,
        )

    resolved = Path(work_dir).resolve()
    root = Path(config.PROJECTS_ROOT).resolve()
    if not resolved.is_relative_to(root):
        raise TurnError(
            f"Invalid work_dir: {work_dir} (escapes PROJECTS_ROOT)", fatal=True
        )
    if not resolved.is_dir():
        raise TurnError(f"Work directory does not exist: {work_dir}", fatal=True)

    if config.PROXY_ENABLED:
        return await _proxy_turn(prompt, session_id, str(resolved), chat_id, model,
                                 owner)
    return await _execute_direct(prompt, session_id, str(resolved), chat_id, model,
                                 owner)


async def stream_turn(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Yield SSE-compatible event dicts from the Claude Code subprocess/stream."""
    if len(prompt) > config.PROMPT_MAX_CHARS:
        raise TurnError(
            f"Prompt too long: {len(prompt)} chars (max {config.PROMPT_MAX_CHARS})",
            fatal=True,
        )

    resolved = Path(work_dir).resolve()
    root = Path(config.PROJECTS_ROOT).resolve()
    if not resolved.is_relative_to(root):
        raise TurnError(
            f"Invalid work_dir: {work_dir} (escapes PROJECTS_ROOT)", fatal=True
        )
    if not resolved.is_dir():
        raise TurnError(f"Work directory does not exist: {work_dir}", fatal=True)

    if config.PROXY_ENABLED:
        async for event in _proxy_stream_turn(
            prompt, session_id, str(resolved), chat_id, model
        ):
            yield event
    else:
        async for event in _execute_direct_stream(
            prompt, session_id, str(resolved), chat_id, model
        ):
            yield event
