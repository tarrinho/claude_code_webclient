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
_skills_by_session: dict[str, set[str]] = {}


def take_last_model(chat_id: str) -> str:
    """Return and clear the last model reported for a blocking turn."""
    return _models_by_chat.pop(chat_id, "")


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


async def get_default_model() -> str:
    """Return the persisted default model, falling back to environment config."""
    import db

    if db.db_conn is None:
        return config.MODEL_NAME
    return await db.setting_get("default_model") or config.MODEL_NAME


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
) -> tuple[list[str], str | None]:
    """Execute one turn over TCP to the host claude_proxy.

    Sends a single JSON message to the proxy, reads back NDJSON chunks,
    and returns (text_chunks, session_id).
    """
    sem = _get_sem()
    async with sem:
        return await _execute_proxy(prompt, session_id, work_dir, chat_id, model)


async def _execute_proxy(
    prompt: str,
    session_id: str | None,
    work_dir: str,
    chat_id: str,
    model: str | None = None,
) -> tuple[list[str], str | None]:
    """Core proxy turn: connect → send turn → read NDJSON → disconnect."""
    connect_timeout = config.PROXY_CONNECT_TIMEOUT_S
    turn_timeout = config.PROXY_TURN_TIMEOUT_S

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
            "proxy connect failed host=%s port=%d: %s",
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

        # Build turn payload
        writer.write(
            (
                json.dumps(
                    {
                        "type": "turn",
                        "prompt": prompt,
                        "session_id": session_id,
                        "work_dir": work_dir,
                        "model": model or await get_default_model(),
                    }
                )
                + "\n"
            ).encode()
        )
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
                        _log.debug("non-JSON from proxy: %s", line[:200])
                        continue

                    msg_type = obj.get("type", "")
                    if msg_type == "session_id" and obj.get("session_id"):
                        sid = obj["session_id"]
                    elif msg_type == "model" and obj.get("model"):
                        _models_by_chat[chat_id] = obj["model"]
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
            _log.warning("proxy read timed out (turn_timeout=%ds)", turn_timeout)
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


def _build_cmd_direct(
    prompt: str, session_id: str | None, model: str | None = None
) -> list[str]:
    """Build the argument list for the Claude Code subprocess.

    User data is placed *after* ``--`` so that a prompt starting with ``--``
    or ``-`` is never mistaken for a CLI flag.
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd.extend(["--model", model])
    # Sentinel: everything after ``--`` is treated as data by claude-code.
    cmd.append("--")
    if session_id:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", str(uuid.uuid4())])
    return cmd


def _build_env() -> dict[str, str]:
    """Filter environment — pass model config, strip host vars."""
    safe = {"HOME", "PATH", "SHELL", "LANG", "LC_ALL", "TERM"}
    env = {k: v for k, v in os.environ.items() if k in safe}
    env["PYTHONUNBUFFERED"] = "1"
    env["CLAUDE_CODE_SIMPLE"] = "1"
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
) -> tuple[list[str], str | None]:
    """Core subprocess execution with NDJSON parsing (blocking path)."""
    cmd = _build_cmd_direct(prompt, session_id, model)

    _log.info("launch chat=%s work_dir=%s cmd=%s", chat_id, work_dir, cmd[:4])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        env=_build_env(),
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
            _log.debug("non-JSON line: %s", line[:200])
            continue

        for event in _normalise_cli_frame(obj):
            if event["type"] == "text":
                chunks.append(event["content"])
            elif event["type"] == "session_id":
                session_id_val = event["session_id"]
            elif event["type"] == "model":
                _models_by_chat[chat_id] = event["model"]
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
) -> AsyncGenerator[dict, None]:
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
        _log.error("proxy connect failed: %s", exc)
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

        # Send turn
        writer.write(
            (
                json.dumps(
                    {
                        "type": "turn",
                        "prompt": prompt,
                        "session_id": session_id,
                        "work_dir": work_dir,
                        "model": model or await get_default_model(),
                    }
                )
                + "\n"
            ).encode()
        )
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
                        _log.debug("non-JSON from proxy: %s", line[:200])
                        continue

                    msg_type = obj.get("type", "")
                    if msg_type == "skill":
                        _record_skill(session_id, obj.get("name"))
                        yield obj
                    elif msg_type in ("text", "session_id", "model", "status", "error"):
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
) -> AsyncGenerator[dict, None]:
    """Core subprocess streaming (behind semaphore)."""
    cmd = _build_cmd_direct(prompt, session_id, model)

    _log.info("stream chat=%s work_dir=%s", chat_id, work_dir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        env=_build_env(),
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
        return await _proxy_turn(prompt, session_id, str(resolved), chat_id, model)
    else:
        return await _execute_direct(prompt, session_id, str(resolved), chat_id, model)


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
