#!/usr/bin/env python3
"""Host-side proxy — bridges one TCP client to Claude Code CLI.

Protocol (newline-delimited JSON):

  client → proxy : {"type": "handshake", "protocol": "webconsole-v1", "token": "..."}
  proxy → client : {"type": "ack"}
  client → proxy : {"type": "turn", "prompt": "...", "session_id": "...", "work_dir": "..."}
  proxy → client : Normalised NDJSON frames (text, message, error, done)
  proxy → client : {"type": "done"}

Claude Code's stream-json output is normalised by the proxy so the runner
doesn't need to understand Claude's internal event shapes.

Usage:  WC_PROXY_TOKEN=... python3 claude_proxy.py [--host 127.0.0.1] [--port 9000]
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import signal
import sys
import uuid

import backend_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("claude-proxy")

PROTOCOL = "webconsole-v1"

# Max bytes for a single stream-json line from Claude. The asyncio default is
# 64 KiB, which a large tool result exceeds and truncates the turn.
_STREAM_LIMIT = 16 * 1024 * 1024

# Cap concurrent Claude subprocesses here, on the server side. runner.py has its
# own semaphore, but that only bounds one client: a restarted app, a second
# instance, or any other holder of the token could otherwise spawn without limit.
_MAX_CONCURRENT = max(1, int(os.environ.get("WC_PROXY_MAX_CONCURRENT", "4")))
_slots: asyncio.Semaphore | None = None


def _get_slots() -> asyncio.Semaphore:
    """Lazily build the semaphore, so it binds to the running loop."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(_MAX_CONCURRENT)
    return _slots


async def read_lines(reader: asyncio.StreamReader):
    """Yield bytes lines until EOF or cancel."""
    buf = b""
    try:
        while True:
            chunk = await reader.read(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line
    except (asyncio.CancelledError, BrokenPipeError):
        pass


def _int_or_zero(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def usage_frame(obj: dict) -> dict | None:
    """Build a ``usage`` frame from a Claude Code ``result`` frame.

    ``modelUsage`` is preferred because it is keyed by model id, so a turn that
    touched more than one model is attributed exactly. Its keys are camelCase
    while the flat ``usage`` object is snake_case; both are normalised here.

    When only the flat form is present the model is unknown at this layer, so
    it is reported under the empty-string key and the consumer fills it in from
    the session's model. Returns None when there is nothing to record.
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
                "cache_read_tokens": _int_or_zero(
                    usage.get("cache_read_input_tokens")
                ),
                "cache_creation_tokens": _int_or_zero(
                    usage.get("cache_creation_input_tokens")
                ),
            }
            if any(totals.values()):
                models[""] = totals
    if not models:
        return None

    cost = obj.get("total_cost_usd")
    duration = obj.get("duration_ms")
    return {
        "type": "usage",
        "models": models,
        "cost_usd": cost if isinstance(cost, (int, float)) else None,
        "duration_ms": _int_or_zero(duration) or None,
        "is_error": bool(obj.get("is_error")),
    }


def normalise_claude_frame(obj: dict) -> list[dict]:
    """Normalise Claude Code stream-json frames to the webconsole protocol."""
    frame_type = obj.get("type", "")
    subtype = obj.get("subtype", "")
    frames: list[dict] = []

    if frame_type == "system":
        if subtype == "init":
            session_id = obj.get("session_id")
            if session_id:
                frames.append({"type": "session_id", "session_id": session_id})
            model = obj.get("model")
            if model:
                frames.append({"type": "model", "model": model})
        elif subtype == "api_retry":
            frames.append(
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
            error = obj.get("message") or obj.get("error") or json.dumps(obj)
            frames.append({"type": "error", "error": error})

    elif frame_type == "message":
        role = obj.get("role", "")
        frames.append({"type": "message", "role": role, "id": obj.get("id", "")})
        for block in obj.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                frames.append({"type": "text", "content": block.get("text", "")})

    elif frame_type == "tool_use":
        name = obj.get("name") or obj.get("input", {}).get("skill")
        if name:
            frames.append({"type": "skill", "name": name})

    elif frame_type == "assistant":
        msg = obj.get("message", {})
        if not isinstance(msg, dict):
            return []
        frames.append(
            {
                "type": "message",
                "role": msg.get("role", "assistant"),
                "id": msg.get("id", ""),
            }
        )
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                frames.append({"type": "text", "content": block.get("text", "")})

    elif frame_type == "result":
        session_id = obj.get("session_id")
        if session_id:
            frames.append({"type": "session_id", "session_id": session_id})
        usage = usage_frame(obj)
        if usage:
            frames.append(usage)
        else:
            # Every turn ends in a result frame, so this firing on each turn is
            # what an empty Usage tab looks like from the proxy's side.
            log.warning(
                "result frame carried no usage (keys=%s); nothing to account",
                sorted(obj),
            )
        if obj.get("is_error") or subtype not in ("", "success"):
            error = (
                obj.get("error")
                or obj.get("result")
                or obj.get("errors")
                or "Claude turn failed"
            )
            if not isinstance(error, str):
                error = json.dumps(error)
            frames.append({"type": "error", "error": error})

    elif frame_type == "error" or obj.get("is_error"):
        error = obj.get("error") or obj.get("message") or str(obj)
        frames.append({"type": "error", "error": error})

    return frames


def _fallback_dir() -> str:
    return os.environ.get(
        "WC_PROXY_FALLBACK_DIR", "/tmp"
    )  # nosec B108: configurable private fallback


def _backend_env(backend: object) -> dict[str, str]:
    """Return the child environment, applying an ``anthropic`` backend if given.

    Starts from this process's environment so the child keeps PATH, HOME and
    the host's Claude credentials -- passing a bare dict would leave `claude`
    unable to start. The API key is applied as an environment variable and
    never as an argument: /proc/<pid>/cmdline is world-readable.

    The rule itself is in :func:`backend_env.deltas`, which `runner._build_env`
    and `bin/wc-claude.sh` also use. It used to live here and be mirrored by
    hand in both of those -- the shell one said so in a comment, "Mirror
    claude_proxy._backend_env exactly, including what it removes". Registry #68
    is what the mirroring cost: this path inherited ANTHROPIC_BASE_URL from its
    shell, so switching a machine off a gateway and back to Anthropic kept
    sending turns to the gateway with nothing in the UI to say so.

    What stays here is what is specific to this path: the environment is copied
    wholesale, because a proxied child needs the host's login to be reachable.
    """
    env = backend_env.deltas(backend).apply_to(dict(os.environ))
    # Same marker `runner._build_env` sets, and it matters more here: this path
    # copies the environment wholesale, so a proxy started from a shell hands
    # the child that shell's TERM and the request log cannot tell the turn from
    # something Pedro typed. See `.claude/hooks/log_pt_request.py`. Set after
    # `deltas` so the rule that owns credentials stays the only thing in it --
    # this is a provenance flag, not backend configuration, which is also why
    # it is not in `deltas` itself: `bin/wc-claude.sh` calls that too, and an
    # interactive session must stay unmarked.
    env["WC_INTERNAL_SPAWN"] = "1"
    log.info("backend_env %s", backend_env.describe(backend))
    return env


def _safe_cwd(work_dir: str | None) -> str | None:
    """Return a cwd for the subprocess, confined to the allowed root.

    *work_dir* arrives from the client, so an unvalidated value lets the caller
    choose any directory on the host. Anything missing or outside the root
    falls back, preserving the existing behaviour for Docker-internal paths
    that legitimately do not exist here.
    """
    if not work_dir:
        return None
    fallback = _fallback_dir()
    root = os.environ.get("WC_PROXY_ALLOWED_ROOT") or os.environ.get(
        "WC_PROJECTS_ROOT", ""
    )
    if not os.path.isdir(work_dir):
        log.info("work_dir %s not found on host, falling back to %s", work_dir, fallback)
        return fallback
    if root:
        try:
            resolved = os.path.realpath(work_dir)
            root_resolved = os.path.realpath(root)
            if (
                resolved != root_resolved
                and not resolved.startswith(root_resolved + os.sep)
            ):
                log.warning(
                    "work_dir %s is outside allowed root %s, falling back to %s",
                    work_dir,
                    root_resolved,
                    fallback,
                )
                return fallback
        except OSError:
            return fallback
    return work_dir


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    claude_path: str,
    proxy_token: str,
):
    """Bound concurrent subprocesses, then serve the connection.

    The cap is enforced here rather than only in runner.py, whose semaphore
    bounds a single client: a restarted app, a second instance, or any other
    holder of the token could otherwise spawn Claude processes without limit.

    The slot covers the handshake as well as the subprocess, so an unauthenticated
    connection can occupy one for as long as the handshake timeout (10s). That is
    accepted: the proxy binds to 127.0.0.1 by default, and holding the slot for
    the whole connection is what guarantees it is always released.
    """
    slots = _get_slots()
    if slots.locked():
        log.warning(
            "at capacity (%d concurrent); queueing client %s",
            _MAX_CONCURRENT,
            writer.get_extra_info("peername"),
        )
    async with slots:
        await _handle_client(reader, writer, claude_path, proxy_token)


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    claude_path: str,
    proxy_token: str,
):
    """Handle one authenticated client connection → one Claude subprocess."""
    peer = writer.get_extra_info("peername")
    log.info("connect from %s", peer)

    # ── 1. Handshake ──────────────────────────────────────────────────────────
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10.0)
        frame = json.loads(raw)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, json.JSONDecodeError):
        log.warning("bad or missing handshake from %s", peer)
        writer.close()
        return

    supplied_token = frame.get("token", "")
    if (
        frame.get("type") != "handshake"
        or frame.get("protocol") != PROTOCOL
        or not isinstance(supplied_token, str)
        or not hmac.compare_digest(supplied_token, proxy_token)
    ):
        log.warning("bad handshake from %s", peer)
        writer.close()
        await writer.wait_closed()
        return

    # ── 2. Send ACK immediately (Claude not running yet) ─────────────────────
    writer.write((json.dumps({"type": "ack"}) + "\n").encode())
    await writer.drain()

    # ── 3. Wait for turn payload ─────────────────────────────────────────────
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=300.0)
    except asyncio.TimeoutError:
        log.warning("timeout waiting for turn from %s", peer)
        writer.close()
        return
    except (asyncio.IncompleteReadError, ConnectionError):
        log.info("client disconnected before turn from %s", peer)
        writer.close()
        return

    try:
        turn = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("bad turn from %s", peer)
        writer.close()
        return

    prompt = turn.get("prompt", "")
    session_id = turn.get("session_id")
    work_dir = turn.get("work_dir", "")
    requested_model = turn.get("model")

    log.info(
        "turn from %s: %d chars session=%s dir=%s model=%s",
        peer,
        len(prompt),
        session_id,
        work_dir,
        requested_model,
    )

    # ── 4. Launch Claude Code subprocess ─────────────────────────────────────
    claude_cmd = [
        claude_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    claude_model = requested_model or os.environ.get("WC_CLAUDE_MODEL")
    if claude_model:
        claude_cmd.extend(["--model", claude_model])
    # Session flags are real options, so they must come *before* the ``--``
    # sentinel. Placing them after it made claude treat them as prompt data
    # and silently start a fresh session, so every turn lost its history.
    if session_id and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", session_id):
        # session_id is a real Claude Code UUID — resume it
        claude_cmd.extend(["--resume", session_id])
    else:
        claude_cmd.extend(["--session-id", str(uuid.uuid4())])
    # ``-p``/``--print`` is a boolean flag and the prompt is positional, so the
    # sentinel is what keeps a prompt starting with "-" from parsing as options.
    claude_cmd.extend(["--", prompt])

    try:
        import os as _os
        import shutil as _shutil

        _resolved = (
            _shutil.which(claude_path)
            if not _os.path.isabs(claude_path)
            else claude_path
        )
        log.info(
            "spawn: claude_path=%s resolved=%s PATH=%s work_dir=%s",
            claude_path,
            _resolved,
            _os.environ.get("PATH", "?")[:80],
            work_dir,
        )

        # Validate Claude binary exists before attempting spawn
        if not _resolved or not _os.path.isfile(_resolved):
            raise FileNotFoundError(f"claude binary not found: {claude_path}")

        _cmd = [_resolved] + claude_cmd[1:]

        # Resolve work_dir: Docker-internal paths don't exist on host — fall back
        # to the configured directory. The path is client-supplied, so it is also
        # confined to an allowed root; without that the proxy would happily run
        # Claude with its cwd anywhere on the host filesystem.
        _cwd = _safe_cwd(work_dir)

        log.info("spawn cmd: %s cwd=%s", " ".join(_cmd[:3]), _cwd)
        proc = await asyncio.create_subprocess_exec(
            *_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=_cwd,
            env=_backend_env(turn.get("backend")),
            # Claude's stream-json frames routinely exceed the default 64 KiB
            # StreamReader limit (a single large tool result is enough). Hitting
            # it raises LimitOverrunError mid-turn and truncates the response.
            limit=_STREAM_LIMIT,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as _e:
        _msg = (
            "claude binary not found"
            if isinstance(_e, FileNotFoundError)
            else f"spawn failed: {_e}"
        )
        log.error("claude spawn failed: %s", _e)
        writer.write((json.dumps({"type": "error", "error": _msg}) + "\n").encode())
        writer.write((json.dumps({"type": "done"}) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    log.info("claude PID %d: %s ...", proc.pid, " ".join(claude_cmd[:3]))

    # ── 5. Relay Claude output → client ─────────────────────────────────────
    error_sent = False

    async def send_frame(frame: dict) -> bool:
        nonlocal error_sent
        if frame.get("type") == "error":
            error_sent = True
        try:
            writer.write((json.dumps(frame) + "\n").encode())
            await writer.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    async def relay_stdout():
        if proc.stdout is None:
            return
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-JSON: %s", line[:80])
                continue

            for frame in normalise_claude_frame(obj):
                if not await send_frame(frame):
                    return

    async def collect_stderr() -> str:
        if proc.stderr is None:
            return ""
        data = await proc.stderr.read()
        return data.decode("utf-8", errors="replace").strip()

    stdout_task = asyncio.create_task(relay_stdout())
    stderr_task = asyncio.create_task(collect_stderr())
    process_task = asyncio.create_task(proc.wait())
    disconnect_task = asyncio.create_task(reader.read())
    timed_out = False
    disconnected = False

    # ── 6. Wait for Claude exit, client cancellation, or timeout ─────────────
    try:
        done, _ = await asyncio.wait(
            {process_task, disconnect_task},
            timeout=300.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timed_out = True
            log.warning("claude timed out")
        elif disconnect_task in done and not process_task.done():
            disconnected = True
            log.info("client disconnected; terminating Claude PID %d", proc.pid)

        if timed_out or disconnected:
            proc.terminate()
            try:
                await asyncio.wait_for(process_task, timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                proc.kill()
                await process_task
        else:
            await process_task
    finally:
        if not disconnect_task.done():
            disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)

    await stdout_task
    stderr_text = await stderr_task

    if disconnected:
        log.info("cancelled turn for disconnected client %s", peer)
    elif timed_out:
        await send_frame({"type": "error", "error": "Claude turn timed out after 300s"})
    elif proc.returncode and not error_sent:
        detail = (
            stderr_text[-2000:]
            if stderr_text
            else f"Claude exited with code {proc.returncode}"
        )
        await send_frame({"type": "error", "error": detail})

    # ── 7. done + close ─────────────────────────────────────────────────────
    if not disconnected:
        await send_frame({"type": "done"})
    writer.close()
    try:
        await writer.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass
    log.info("closed %s", peer)


async def main():
    host = os.environ.get("WC_PROXY_LISTEN_HOST", "127.0.0.1")
    port = 9000
    claude_path = os.environ.get("WC_CLAUDE_PATH", "claude")
    proxy_token = os.environ.get("WC_PROXY_TOKEN", "")
    if len(proxy_token) < 32:
        raise RuntimeError("WC_PROXY_TOKEN must be set to at least 32 characters")

    # Parse CLI args: --host ADDRESS --port N
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--host", "-H") and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] in ("--claude", "-c") and i + 1 < len(args):
            claude_path = args[i + 1]
            i += 2
        else:
            i += 1

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGINT, stop.set_result, None)
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, claude_path, proxy_token), host, port
    )
    # Nothing supervises this process, so it happily serves code from whenever
    # it was started while the file on disk moves on. That has now silently
    # broken three separate features -- the proxy token, the backend
    # environment, and usage accounting -- each presenting as "the feature
    # does nothing" with no error anywhere. Stamping the source it is actually
    # running makes the staleness checkable from the log alone.
    try:
        import datetime as _dt

        _src_mtime = _dt.datetime.fromtimestamp(
            os.stat(__file__).st_mtime, tz=_dt.timezone.utc
        ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        _src_mtime = "unknown"
    log.info(
        "listening on %s:%d  claude=%s  protocol=%s  source_mtime=%s",
        host, port, claude_path, PROTOCOL, _src_mtime,
    )

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
