"""Read Claude Code CLI transcripts so the WebConsole can display them.

A terminal session writes an append-only JSONL transcript to
``~/.claude/projects/<project-dir>/<session-id>.jsonl``. This module turns that
file into conversation turns, and can follow one that is still being written.

Only ``user`` and ``assistant`` records are conversational. A real transcript is
mostly other things -- ``attachment``, ``file-history-snapshot``, ``mode``,
``queue-operation`` and friends -- plus ``tool_result`` blocks echoing tool
output back into the next user record. All of that is dropped here; a viewer
wants the conversation, not the machinery.

Path handling reuses ``db._session_transcript_paths``, which pins the filename
to the session id and restricts its charset, so a session id can never widen
the search or walk out of the projects directory.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Final

import db

# Initial read size for the history view. Transcripts reach tens of megabytes,
# so the first page comes from the end of the file and reports itself as
# truncated rather than loading everything.
HISTORY_TAIL_BYTES: Final[int] = 512 * 1024

# Upper bound on turns returned in one response.
MAX_TURNS: Final[int] = 500

# How often the live tail re-checks the file for new bytes.
TAIL_POLL_S: Final[float] = 1.0

# Record types that carry conversation. Everything else is session machinery.
_CONVERSATION_TYPES: Final[frozenset[str]] = frozenset({"user", "assistant"})

# Tool inputs worth showing beside the tool name, in preference order.
_TOOL_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "description",
    "command",
    "file_path",
    "pattern",
    "path",
    "url",
    "prompt",
    "query",
)

_TOOL_SUMMARY_MAX: Final[int] = 120


def _tool_summary(block: dict[str, Any]) -> str:
    """One line describing a tool call, e.g. ``Bash(git status)``."""
    name = str(block.get("name") or "tool")
    payload = block.get("input")
    if not isinstance(payload, dict):
        # input is not always an object; never assume it is.
        return name
    for key in _TOOL_SUMMARY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            if len(detail) > _TOOL_SUMMARY_MAX:
                detail = detail[: _TOOL_SUMMARY_MAX - 1].rstrip() + "…"
            return f"{name}({detail})"
    return name


def _blocks_from_content(content: Any) -> list[dict[str, str]]:
    """Normalise a record's ``message.content`` into display blocks."""
    if isinstance(content, str):
        text = content.strip()
        return [{"kind": "text", "text": text}] if text else []
    if not isinstance(content, list):
        return []

    blocks: list[dict[str, str]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            text = str(item.get("text") or "").strip()
            if text:
                blocks.append({"kind": "text", "text": text})
        elif kind == "thinking":
            text = str(item.get("thinking") or "").strip()
            if text:
                blocks.append({"kind": "thinking", "text": text})
        elif kind == "tool_use":
            blocks.append({"kind": "tool", "text": _tool_summary(item)})
        # tool_result is deliberately dropped: it is tool output replayed into
        # the next user record, and it dwarfs the conversation itself.
    return blocks


def _turn_from_record(record: Any) -> dict[str, Any] | None:
    """Return a display turn for one JSONL record, or None to skip it."""
    if not isinstance(record, dict):
        return None
    if record.get("type") not in _CONVERSATION_TYPES:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None

    blocks = _blocks_from_content(message.get("content"))
    if not blocks:
        return None

    role = message.get("role") or record.get("type")
    return {
        "role": "assistant" if role == "assistant" else "user",
        "timestamp": str(record.get("timestamp") or ""),
        "model": str(message.get("model") or "") if role == "assistant" else "",
        "blocks": blocks,
        # Subagent traffic is interleaved into the same file; let the UI mark it.
        "sidechain": bool(record.get("isSidechain")),
    }


def _turns_from_bytes(raw: bytes) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        turn = _turn_from_record(record)
        if turn:
            turns.append(turn)
    return turns


def _read_range_sync(
    path: Path, offset: int
) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Read from *offset* to EOF. Returns (turns, start, end, truncated).

    When *offset* is 0 and the file is large, this starts near the end instead
    and reports ``truncated``. *start* is the byte the returned window begins
    at, which is what ``read_before`` needs to walk further back; *end* is the
    resume point for following the file forwards.

    Reads stop on the last complete line, so a partially written final line is
    re-read next time rather than parsed in half.
    """
    truncated = False
    size = path.stat().st_size
    start = offset
    if offset <= 0 and size > HISTORY_TAIL_BYTES:
        start = size - HISTORY_TAIL_BYTES
        truncated = True
    if start >= size:
        return [], size, size, False

    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read()

    if truncated:
        # Drop the partial line the seek landed inside.
        newline = raw.find(b"\n")
        skip = (newline + 1) if newline >= 0 else len(raw)
        raw = raw[skip:]
        start += skip

    # Keep only complete lines; leave a trailing partial for the next read.
    end = raw.rfind(b"\n")
    if end < 0:
        return [], start, start, truncated
    consumed = raw[: end + 1]
    return _turns_from_bytes(consumed), start, start + len(consumed), truncated


def _read_before_sync(path: Path, before: int) -> tuple[list[dict[str, Any]], int]:
    """Read the window of turns immediately preceding byte *before*.

    Returns (turns, start). *before* is always a line boundary -- it is a
    ``start`` this module returned earlier -- so only the first line of the
    window can be partial, and that happens solely when the window does not
    reach the beginning of the file.
    """
    if before <= 0:
        return [], 0

    start = max(0, before - HISTORY_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(before - start)

    if start > 0:
        newline = raw.find(b"\n")
        skip = (newline + 1) if newline >= 0 else len(raw)
        raw = raw[skip:]
        start += skip

    return _turns_from_bytes(raw), start


def transcript_path(session_id: str) -> Path | None:
    """Locate a session's transcript, or None if there isn't exactly one."""
    paths = db._session_transcript_paths(session_id)
    return paths[0] if paths else None


async def read_turns(session_id: str, offset: int = 0) -> dict[str, Any]:
    """Read a session's conversation from *offset* to the end.

    Returns ``{turns, start, offset, truncated, at_start, found}``. ``offset``
    is the resume point for following forwards; ``start`` is the byte the
    window begins at, which ``read_before`` takes to page further back.
    """
    path = transcript_path(session_id)
    if path is None:
        return _empty(found=False)

    try:
        turns, start, end, truncated = await asyncio.to_thread(
            _read_range_sync, path, offset
        )
    except OSError:
        return _empty(found=False)

    if len(turns) > MAX_TURNS:
        turns = turns[-MAX_TURNS:]
        truncated = True
    return {
        "turns": turns,
        "start": start,
        "offset": end,
        "truncated": truncated,
        "at_start": start <= 0,
        "found": True,
    }


async def read_before(session_id: str, before: int) -> dict[str, Any]:
    """Read the window of turns preceding byte *before*, for paging backwards.

    Without this a long session could only ever be read from its tail: the
    forward offset walks towards the end of the file, never back towards the
    beginning, so on a multi-megabyte transcript most of the conversation was
    unreachable.
    """
    path = transcript_path(session_id)
    if path is None:
        return _empty(found=False)

    try:
        turns, start = await asyncio.to_thread(_read_before_sync, path, before)
    except OSError:
        return _empty(found=False)

    truncated = False
    if len(turns) > MAX_TURNS:
        # Keep the *end* of the window so it stays contiguous with what the
        # caller already has below it.
        turns = turns[-MAX_TURNS:]
        truncated = True
    return {
        "turns": turns,
        "start": start,
        "offset": max(before, start),
        "truncated": truncated,
        "at_start": start <= 0,
        "found": True,
    }


def _empty(found: bool) -> dict[str, Any]:
    return {
        "turns": [],
        "start": 0,
        "offset": 0,
        "truncated": False,
        "at_start": True,
        "found": found,
    }


def _first_prompt_sync(path: Path) -> str:
    """Best-effort title: the first line of the session's first user message."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(HISTORY_TAIL_BYTES)
    except OSError:
        return ""
    for turn in _turns_from_bytes(raw):
        if turn["role"] != "user":
            continue
        for block in turn["blocks"]:
            if block["kind"] != "text":
                continue
            first = block["text"].strip().splitlines()[0].strip()
            # Slash commands and system-injected preambles make poor titles.
            if first and not first.startswith(("<", "/")):
                return first[:100]
    return ""


def _list_sync(limit: int) -> list[dict[str, Any]]:
    root = db._CLAUDE_PROJECTS_DIR
    if not root.is_dir():
        return []
    try:
        files = [p for p in root.glob("*/*.jsonl") if p.is_file()]
    except (OSError, PermissionError):
        return []

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    listed: list[dict[str, Any]] = []
    for path in files[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        listed.append(
            {
                "session_id": path.stem,
                # The directory name is the session's cwd with "/" as "-".
                "project": path.parent.name,
                "updated_at": int(stat.st_mtime),
                "size": stat.st_size,
                "title": _first_prompt_sync(path),
            }
        )
    return listed


async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Recent transcripts, newest first, for browsing past conversations.

    The live sessions endpoint only knows about sessions that are still
    running; this is what makes finished ones reachable.
    """
    limit = max(1, min(int(limit), 200))
    return await asyncio.to_thread(_list_sync, limit)
