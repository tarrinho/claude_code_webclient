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
import signal
import sys
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("claude-proxy")

PROTOCOL = "webconsole-v1"


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


async def handle_client(
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
    except (asyncio.TimeoutError, ValueError, json.JSONDecodeError):
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
    if session_id:
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

        # Resolve work_dir: Docker-internal paths don't exist on host — fall back to /tmp
        _cwd = work_dir or None
        if _cwd and not _os.path.isdir(_cwd):
            _fallback = os.environ.get(
                "WC_PROXY_FALLBACK_DIR", "/tmp"
            )  # nosec B108: configurable private fallback
            log.info(
                "work_dir %s not found on host, falling back to %s", _cwd, _fallback
            )
            _cwd = _fallback

        log.info("spawn cmd: %s cwd=%s", " ".join(_cmd[:3]), _cwd)
        proc = await asyncio.create_subprocess_exec(
            *_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=_cwd,
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
    log.info(
        "listening on %s:%d  claude=%s  protocol=%s", host, port, claude_path, PROTOCOL
    )

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
