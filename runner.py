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
from collections import OrderedDict
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TypeVar

import backend_env
import config

_log: object = __import__("loguru").logger.bind(service="runner")

_K = TypeVar("_K")
_V = TypeVar("_V")


class _BoundedDict(OrderedDict[_K, _V]):
    """A dict capped at *maxsize*, evicting the least-recently-set entry.

    ``_models_by_chat``, ``_usage_by_chat`` and ``_retried_usage_by_chat`` are
    hand-off buffers: written once per turn and popped once by whichever
    caller consumes them. That pop never happens when the caller crashes or
    forgets before reaching it, and ``_skills_by_session`` is never popped at
    all -- it accumulates for the life of the process, one entry per Claude
    Code session that ever ran a turn. Both are unbounded growth under
    sustained use with no cap in the original ``dict``.

    dict.setdefault is implemented in C and does not route through a
    subclass's ``__setitem__``, so it is overridden explicitly below --
    without that, ``_skills_by_session.setdefault(...)`` (the only write site
    for that dict) would silently bypass the eviction this class exists to
    provide.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key: _K, value: _V) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)

    def setdefault(self, key: _K, default: _V) -> _V:  # type: ignore[override]
        if key in self:
            self.move_to_end(key)
            return self[key]
        self[key] = default
        return default


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
# Capped rather than plain dicts -- see _BoundedDict's docstring. 10,000 chats'
# worth of hand-off state, or session skill-sets, is generous for any single
# process's uptime and small in memory (a model name or a token-count dict per
# entry), while still bounding a leak from unlimited chat/session creation.
_MAX_RUNNER_STATE_ENTRIES = 10_000
_models_by_chat: dict[str, str] = _BoundedDict(_MAX_RUNNER_STATE_ENTRIES)
_usage_by_chat: dict[str, dict] = _BoundedDict(_MAX_RUNNER_STATE_ENTRIES)
_retried_usage_by_chat: dict[str, list[dict]] = _BoundedDict(_MAX_RUNNER_STATE_ENTRIES)
_skills_by_session: dict[str, set[str]] = _BoundedDict(_MAX_RUNNER_STATE_ENTRIES)


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


def take_retried_usage(chat_id: str) -> list[dict]:
    """Return and clear usage frames spent on attempts a retry discarded.

    A discarded attempt still cost real tokens against the backend, so its
    frame must reach the same accounting path as a kept turn -- see CLAUDE.md
    rule 5, "record failures too". It does not go through take_last_usage /
    the live 'usage' event because those carry only the kept attempt; callers
    must drain this separately and record each frame themselves (with their
    own ``origin``), same as take_last_usage.
    """
    return _retried_usage_by_chat.pop(chat_id, [])


def _is_non_answer(chunks: list[str], usage_frame: dict) -> bool:
    """Whether a completed, error-free turn's output is worth retrying.

    Both must hold: no real text content, and the reported output tokens
    (summed across models, 0 when usage is missing) below the configured
    floor. Text-empty is the primary signal -- observed on small gateway
    models asked to self-identify, which return an empty text block with a
    handful of tokens spent entirely on internal reasoning. A turn that
    produced any real text is never retried by this check, no matter its
    token count.
    """
    if "".join(chunks).strip():
        return False
    models = usage_frame.get("models") or {}
    output_tokens = sum(m.get("output_tokens", 0) for m in models.values())
    return output_tokens < config.TURN_RETRY_MIN_TOKENS


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
    """Return the proxy transport host, falling back to environment config.

    This is the global/legacy value -- what the Settings page shows
    (routes/misc.py's GET /api/settings) -- not a per-turn resolution. A turn
    itself must go through :func:`get_proxy_target`, which is scoped to the
    chat's own pinned or active machine; this function predates that and
    keeping it unscoped is what routes/misc.py's display still wants.
    """
    import db

    if db.db_conn is None:
        return config.PROXY_HOST
    return await db.setting_get("ai_machine_host") or config.PROXY_HOST


async def get_proxy_target(
    chat_id: str, owner: str | None = None
) -> tuple[str, int]:
    """Return the (host, port) a turn should connect to, following the same
    pin/active resolution as :func:`get_backend`.

    Mirrors get_backend's shape deliberately: a "proxy"-provider machine is
    the *other* half of the same routing table, and until this function
    existed it was the half nothing consulted. get_proxy_host() reads a single
    global setting with no per-machine awareness and _execute_proxy /
    _do_proxy_stream both hardcoded config.PROXY_PORT, so activating a
    "proxy" machine changed a database flag and nothing else -- no turn could
    ever reach the machine a user had just activated. The port a proxy
    machine's own row stores was written on creation and read by nothing.

    Falls back to (get_proxy_host(), config.PROXY_PORT) when no "proxy"
    machine is pinned or active, which is byte-for-byte today's behaviour for
    every deployment that only uses the single global setting.
    """
    import db

    if db.db_conn is None:
        return config.PROXY_HOST, config.PROXY_PORT

    routing = await db.chat_routing(chat_id)
    if not routing["owner"]:
        if not owner:
            return await get_proxy_host(), config.PROXY_PORT
        # Not a conversation -- same fallback get_backend uses for a caller
        # (the orchestrator) whose chat_id is a label rather than a row.
        routing = {"machine": await db.ai_machine_backend(owner)}
    machine = routing["machine"]
    if not machine:
        return await get_proxy_host(), config.PROXY_PORT

    provider = machine.get("provider", "")

    # A backend with transport_id set runs its claude process on that
    # transport's remote host -- route through the tunnel to
    # 127.0.0.1:<local_port>, same as before this was ai_machines.transport_id
    # instead of provider == "ssh_proxy".
    if machine.get("transport_id"):
        from tunnel_manager import tunnel_status

        status = await tunnel_status(machine["id"])
        if status and status.get("tunnel_up") and status.get("proxy_ok"):
            return "127.0.0.1", int(status["local_port"])
        # Tunnel down or proxy not OK — still route there so _execute_proxy
        # can report "waiting_remote" to the SSE client.
        if status and status.get("local_port"):
            return "127.0.0.1", int(status["local_port"])
        _log.warning(
            "transport_routed_backend_falling_back_to_local machine_id={} transport_id={} "
            "(tunnel not up -- turn will run on this host against the backend's own "
            "credentials instead of via the tunnel, which may be the wrong endpoint)",
            machine["id"], machine.get("transport_id"),
        )

    # keyless claude_code (the former "proxy" type): no base_url, route
    # to the machine's host as the proxy target.
    if provider == "claude_code" and machine.get("base_url") is None:
        if machine.get("host"):
            return machine["host"], int(machine.get("port") or config.PROXY_PORT)
    return await get_proxy_host(), config.PROXY_PORT


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
    # Same fallback as get_backend, and for the same caller: the orchestrator's
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
    # Type-checked, not just truthiness-checked. `if not base_url` lets a
    # non-string through -- an int is truthy -- and the next line then raises
    # AttributeError on .strip(). SQLite is dynamically typed, so a TEXT column
    # returns whatever was written to it, and a machine record written with a
    # numeric base_url took the whole turn down with a type error rather than
    # falling back to the default endpoint. The proxy path never had this
    # because it does not normalise at all.
    if not isinstance(base_url, str):
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
    all. The orchestrator engine invents ids -- ``uuid4().hex`` for a planning
    turn, ``subtask_<id>`` for each task -- so chat_routing found no row, owner
    came back None, and this returned {}. That left the child with no base URL
    and no key while CLAUDE_CODE_SIMPLE=1 also blocked the host login, so every
    orchestrator turn died on "Not logged in - Please run /login". The engine
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
    if not machine or machine.get("provider") != "claude_code":
        return {}
    backend: dict[str, str] = {"provider": "claude_code"}
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


def memory_refusal() -> str | None:
    """Why this host cannot take another turn right now, or None.

    MAX_CONCURRENT bounds how many turns run at once; it says nothing about
    whether the host has memory for even one. Measured 2026-09-08, the console's
    own turns were 3 MB of the 2520 MB that claude processes held -- so this
    limit has never been the one that mattered, and a turn admitted onto a
    swapping box is how the server itself gets OOM-killed.

    Cheap enough for the admission path: two files read, no scan. The /proc walk
    that names *who* is holding memory happens only when a refusal is being
    explained.

    Returns a string so the caller can put it straight into the event stream,
    the way a full-slots wait already reports itself (routes/chats.py). None
    means "no objection", including when the guard could not measure -- it fails
    open, deliberately, so a monitoring fault cannot stop every turn at once.

    See docs/superpowers/specs/2026-09-08-resource-guard-design.md.
    """
    try:
        import resource_guard

        verdict = resource_guard.check()
        if verdict.ok:
            return None
        return resource_guard.explain(verdict)
    except Exception:
        # Never let the guard itself be the reason a turn cannot run.
        _log.exception("resource_guard_failed — admitting the turn anyway")
        return None


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
    *,
    proxy_target: tuple[str, int] | None = None,
) -> tuple[list[str], str | None]:
    """Execute one turn over TCP to the host claude_proxy.

    Sends a single JSON message to the proxy, reads back NDJSON chunks,
    and returns (text_chunks, session_id).

    proxy_target, when given, bypasses get_proxy_target's chat/owner-based
    resolution and connects there directly. Needed by the agent-reply
    wake-up turn: that call's chat_id/owner describe the *replying* chat,
    not the *target* session's host, and get_proxy_target would otherwise
    resolve to the wrong (or merely coincidentally-right, for local-only
    traffic) proxy. See
    docs/superpowers/specs/2026-09-08-transport-aware-agent-reply-design.md.
    """
    sem = _get_sem()
    async with sem:
        return await _execute_proxy(prompt, session_id, work_dir, chat_id, model,
                                    owner, proxy_target=proxy_target)


async def _execute_proxy(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
    owner: str | None = None,
    *,
    proxy_target: tuple[str, int] | None = None,
) -> tuple[list[str], str | None]:
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

    proxy_host, proxy_port = proxy_target or await get_proxy_target(chat_id, owner)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=connect_timeout,
        )
    except (asyncio.TimeoutError, OSError, ConnectionRefusedError) as exc:
        _log.error(
            "proxy connect failed host={} port={}: {}",
            proxy_host,
            proxy_port,
            exc,
        )
        raise TurnError(
            f"Cannot connect to proxy at {proxy_host}:{proxy_port}", fatal=False
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
        # conversation with no session_id, and the orchestrator engine, which
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
    # Marks this as a spawn the console made, not a session Pedro started, so
    # `.claude/hooks/log_pt_request.py` does not record a turn's prompt as a
    # request he typed. TERM alone did not separate the two: it is in the
    # allowlist above, so a turn started from a terminal-rooted process
    # inherits it. Not set by `bin/wc-claude.sh` -- an interactive session is
    # precisely what must stay unmarked.
    env["WC_INTERNAL_SPAWN"] = "1"
    # Set before the backend is applied, so `deltas` can take it away again:
    # under CLAUDE_CODE_SIMPLE the CLI refuses to read the host's own login
    # (`claude auth status` reports loggedIn:false, authMethod:none), so a
    # keyless backend must not keep it or the subprocess has no credentials.
    env["CLAUDE_CODE_SIMPLE"] = "1"
    if isinstance(backend, dict) and backend.get("provider") == "claude_code":
        # normalise_base_url stays here rather than moving into backend_env:
        # this path accepts a bare host ("api.anthropic.com") from databases
        # written before the column was a URL, and the proxy path never did.
        # Moving it would change the proxy's behaviour as a side effect of
        # sharing the rule, which is not what sharing the rule is for.
        normalised = dict(backend)
        normalised["base_url"] = normalise_base_url(backend.get("base_url")) or ""
        return backend_env.deltas(normalised).apply_to(env)
    # Non-anthropic: the strips still apply, then the local shim's own config.
    env = backend_env.deltas(backend).apply_to(env)
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
    owner: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream a turn over TCP to the host claude_proxy."""
    sem = _get_sem()
    async with sem:
        try:
            async for event in _do_proxy_stream(
                prompt, session_id, work_dir, chat_id, model, owner
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
    owner: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Connect to proxy, send turn, yield events from the NDJSON stream."""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    proxy_host, proxy_port = await get_proxy_target(chat_id, owner)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=config.PROXY_CONNECT_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError, ConnectionRefusedError) as exc:
        _log.error("proxy connect failed: {}", exc)
        yield {
            "type": "error",
            "error": f"Cannot connect to proxy at {proxy_host}:{proxy_port}",
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
    owner: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream a turn by spawning claude subprocess directly."""
    sem = _get_sem()
    async with sem:
        async for event in _do_direct_stream(
            prompt, session_id, work_dir, chat_id, model, owner
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
    except Exception as e:
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

    _retried_usage_by_chat.pop(chat_id, None)
    max_attempts = config.TURN_RETRY_MAX + 1
    for attempt in range(1, max_attempts + 1):
        if config.PROXY_ENABLED:
            chunks, sid = await _proxy_turn(
                prompt, session_id, str(resolved), chat_id, model, owner
            )
        else:
            chunks, sid = await _execute_direct(
                prompt, session_id, str(resolved), chat_id, model, owner
            )

        if attempt == max_attempts or not _is_non_answer(
            chunks, _usage_by_chat.get(chat_id) or {}
        ):
            return chunks, sid

        frame = take_last_usage(chat_id)
        if frame:
            _retried_usage_by_chat.setdefault(chat_id, []).append(frame)
        _log.warning(
            "turn_retry chat_id=%s attempt=%d/%d reason=non_answer",
            chat_id, attempt, max_attempts,
        )
    return chunks, sid  # unreachable: loop always returns on its last iteration


async def stream_turn(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
    owner: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Yield SSE-compatible event dicts from the Claude Code subprocess/stream.

    *owner* has the same meaning as in :func:`run_turn`: the fallback identity for
    a caller whose *chat_id* is not a row in ``chats``. The orchestrator is that
    caller -- its ids are ``supervisor_<uuid>`` for the planning turn and
    ``subtask_<id>`` for each task, which are labels rather than conversations --
    so without it ``get_backend`` finds no routing row, the child process is given
    no base URL and no API key, and every turn dies on "Not logged in - Please run
    /login".

    That failure has already been diagnosed once, on the blocking path, and fixed
    by passing this argument at the ``run_turn`` call site. It was reachable here
    too: ``_do_proxy_stream`` and ``_do_direct_stream`` have both accepted
    ``owner`` and consumed it via ``get_backend`` all along, and neither of their
    callers passed it -- so the parameter existed at the bottom of both chains and
    was unreachable from the top, permanently ``None``. Threading it is this
    function's whole change; the consumers were already correct.
    """
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

    _retried_usage_by_chat.pop(chat_id, None)
    max_attempts = config.TURN_RETRY_MAX + 1
    for attempt in range(1, max_attempts + 1):
        if config.PROXY_ENABLED:
            inner = _proxy_stream_turn(
                prompt, session_id, str(resolved), chat_id, model, owner
            )
        else:
            inner = _execute_direct_stream(
                prompt, session_id, str(resolved), chat_id, model, owner
            )

        text_parts: list[str] = []
        usage_event: dict | None = None
        should_retry = False
        async for event in inner:
            etype = event.get("type")
            if etype == "usage":
                # Held back until 'done'/'error' decides whether this attempt
                # is kept -- a retried attempt's usage goes through
                # take_retried_usage instead of the live event, so a
                # streaming handler recording usage as it arrives never sees
                # a discarded attempt's frame.
                usage_event = event
                continue
            if etype == "text":
                text_parts.append(event.get("content") or "")
                yield event
                continue
            if etype == "error":
                if usage_event is not None:
                    yield usage_event
                yield event
                return
            if etype == "done":
                if attempt < max_attempts and _is_non_answer(
                    text_parts, usage_event or {}
                ):
                    should_retry = True
                    if usage_event is not None:
                        _retried_usage_by_chat.setdefault(chat_id, []).append(
                            usage_event
                        )
                    break
                if usage_event is not None:
                    yield usage_event
                yield event
                return
            yield event

        if not should_retry:
            return

        _log.warning(
            "turn_retry chat_id=%s attempt=%d/%d reason=non_answer",
            chat_id, attempt, max_attempts,
        )
        yield {
            "type": "status",
            "status": "api_retry",
            "attempt": attempt + 1,
            "max_retries": max_attempts - 1,
            "error": "Empty response, retrying",
        }
