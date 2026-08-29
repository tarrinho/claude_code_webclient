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
import re
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


def _turns_with_offsets(raw: bytes) -> list[tuple[int, dict[str, Any]]]:
    """Turns paired with the byte offset, within *raw*, of the line each came from.

    The offsets are what let a capped page report where it actually begins.
    Splitting on bytes rather than decoding first keeps those offsets exact for
    multi-byte characters.
    """
    pairs: list[tuple[int, dict[str, Any]]] = []
    position = 0
    for line in raw.split(b"\n"):
        line_start = position
        position += len(line) + 1  # + the newline that was split away
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        turn = _turn_from_record(record)
        if turn:
            pairs.append((line_start, turn))
    return pairs


def _turns_from_bytes(raw: bytes) -> list[dict[str, Any]]:
    return [turn for _offset, turn in _turns_with_offsets(raw)]


def _cap(
    pairs: list[tuple[int, dict[str, Any]]], start: int, truncated: bool
) -> tuple[list[dict[str, Any]], int, bool]:
    """Keep at most MAX_TURNS, and move *start* to the first turn kept.

    Slicing the turns while leaving *start* at the beginning of the whole
    window silently strips history: the caller pages back from a byte position
    below the turns that were dropped, so nothing ever returns them. With
    400-byte records a single window holds ~1300 turns, and walking a
    2000-turn transcript recovered only half of it.
    """
    if len(pairs) <= MAX_TURNS:
        return [turn for _offset, turn in pairs], start, truncated
    kept = pairs[-MAX_TURNS:]
    return [turn for _offset, turn in kept], kept[0][0], True


def _read_range_sync(
    path: Path, offset: int
) -> tuple[list[tuple[int, dict[str, Any]]], int, int, bool]:
    """Read from *offset* to EOF. Returns (pairs, start, end, truncated).

    Each pair is (absolute byte offset of the line, turn), so a caller that
    caps the page can still say truthfully where the page begins.

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
    pairs = [(start + rel, turn) for rel, turn in _turns_with_offsets(consumed)]
    return pairs, start, start + len(consumed), truncated


def _read_before_sync(
    path: Path, before: int
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    """Read the window of turns immediately preceding byte *before*.

    Returns (pairs, start), each pair being (absolute byte offset, turn). *before* is always a line boundary -- it is a
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

    return [(start + rel, turn) for rel, turn in _turns_with_offsets(raw)], start


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
        pairs, start, end, truncated = await asyncio.to_thread(
            _read_range_sync, path, offset
        )
    except OSError:
        return _empty(found=False)

    turns, start, truncated = _cap(pairs, start, truncated)
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
        pairs, start = await asyncio.to_thread(_read_before_sync, path, before)
    except OSError:
        return _empty(found=False)

    # Keep the *end* of the window so it stays contiguous with what the caller
    # already has below it, and move start to match so the turns dropped off
    # the front are still reachable by the next page back.
    turns, start, truncated = _cap(pairs, start, False)
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


# Messages between concurrent Claude sessions. They are recorded in four
# different record shapes -- queue-operation, attachment, user and an assistant
# tool_use -- so the same message appears more than once and the conversational
# parser above deliberately drops most of them as machinery. This is a separate
# extractor rather than a mode of the other one.
_AGENT_MARKER: Final[bytes] = b"cross-session-message"
_AGENT_RE: Final[re.Pattern[str]] = re.compile(
    r'<cross-session-message\b[^>]*\bfrom-name="([^"]+)"[^>]*>(.*?)</cross-session-message>',
    re.DOTALL,
)
# Enough of the body to tell two messages apart when the same one is recorded
# by several record types.
_AGENT_DEDUPE_CHARS: Final[int] = 400


def _iter_strings(value: Any):
    """Yield every string anywhere in a nested record."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _agent_events_sync(path: Path, session_id: str, title: str) -> list[dict[str, Any]]:
    """Extract messages sent to and received from other sessions."""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if _AGENT_MARKER not in raw and b"SendMessage" not in raw:
        return []

    events: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        timestamp = str(record.get("timestamp") or "")

        # Outgoing: this session calling SendMessage.
        message = record.get("message")
        if record.get("type") == "assistant" and isinstance(message, dict):
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "SendMessage":
                    continue
                payload = block.get("input")
                if not isinstance(payload, dict):
                    continue
                events.append({
                    "direction": "out",
                    "peer": str(payload.get("to") or "?"),
                    "summary": str(payload.get("summary") or ""),
                    "text": str(payload.get("message") or ""),
                    "timestamp": timestamp,
                    "session_id": session_id,
                    "session_title": title,
                })
            continue

        # Incoming: a wrapped message, wherever in the record it happens to sit.
        for text in _iter_strings(record):
            if "<cross-session-message" not in text:
                continue
            for peer, body in _AGENT_RE.findall(text):
                events.append({
                    "direction": "in",
                    "peer": peer,
                    "summary": "",
                    "text": body.strip(),
                    "timestamp": timestamp,
                    "session_id": session_id,
                    "session_title": title,
                })
    return events


def _cwd_sync(path: Path) -> str:
    """Read the working directory a session ran in, from its own transcript.

    Needed to resume a session that is no longer running: the live registry in
    ~/.claude/sessions only describes running sessions, but every transcript
    records its cwd, so a finished conversation can still be reopened in the
    directory its files are in. The project directory name is not usable for
    this -- it is the path with separators replaced, and underscores are
    flattened to dashes too, so it cannot be reversed unambiguously.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(HISTORY_TAIL_BYTES)
    except OSError:
        return ""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("cwd"), str):
            return record["cwd"]
    return ""


async def session_cwd(session_id: str) -> str:
    """Return the directory *session_id* ran in, or "" if unknown."""
    path = transcript_path(session_id)
    if path is None:
        return ""
    return await asyncio.to_thread(_cwd_sync, path)


async def session_title(session_id: str) -> str:
    """A readable title for a session, taken from its opening prompt."""
    path = transcript_path(session_id)
    if path is None:
        return ""
    return await asyncio.to_thread(_first_prompt_sync, path)


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


def _session_names_sync() -> tuple[dict[str, str], dict[str, str]]:
    """Map session ids and socket paths to the names sessions call each other by.

    A message names its peer inconsistently: a session addressed by name
    records "cweb3", while a reply addressed back down the socket it arrived on
    records "uds:/run/user/1000/cc-socks/3194261.sock". Both denote one session,
    and the registry is what resolves them to the same name.
    """
    by_session: dict[str, str] = {}
    by_socket: dict[str, str] = {}
    directory = Path.home() / ".claude" / "sessions"
    try:
        files = sorted(directory.glob("*.json"))
    except (OSError, PermissionError):
        return by_session, by_socket
    for candidate in files:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        session_id = str(data.get("sessionId") or "")
        socket_path = str(data.get("messagingSocketPath") or "")
        if session_id:
            by_session.setdefault(session_id, name)
        if socket_path:
            by_socket[socket_path] = name
    return by_session, by_socket


def _resolve_peer(peer: str, by_socket: dict[str, str]) -> str:
    """Turn a uds: address into the name the session is known by."""
    if peer.startswith("uds:"):
        return by_socket.get(peer[len("uds:"):], peer)
    return peer


def _agent_traffic_sync(limit: int, scan_files: int) -> list[dict[str, Any]]:
    by_session, by_socket = _session_names_sync()
    entries = _list_sync(scan_files)

    events: list[dict[str, Any]] = []
    for entry in entries:
        path = _transcript_for_entry(entry)
        if path is None:
            continue
        session_id = entry["session_id"]
        # The name a session is known by, falling back to its opening prompt.
        own = by_session.get(session_id) or entry["title"] or session_id[:8]
        for event in _agent_events_sync(path, session_id, entry["title"]):
            peer = _resolve_peer(event["peer"], by_socket)
            # Recorded from one end; store it as sender -> recipient so both
            # ends collapse to the single message that actually happened.
            if event["direction"] == "out":
                event["sender"], event["recipient"] = own, peer
            else:
                event["sender"], event["recipient"] = peer, own
            events.append(event)

    # One message is written into several record types, and again into the
    # transcript at each end, so collapse on who said what to whom.
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        key = (event["sender"], event["recipient"],
               event["text"][:_AGENT_DEDUPE_CHARS])
        # Keep the earliest sighting: the sender records it before the
        # recipient does, so that timestamp is when it was actually sent.
        if key not in unique or event["timestamp"] < unique[key]["timestamp"]:
            unique[key] = event

    ordered = sorted(unique.values(), key=lambda e: e["timestamp"], reverse=True)
    return ordered[:limit]


def _transcript_for_entry(entry: dict[str, Any]) -> Path | None:
    """Rebuild the path for a listing entry."""
    path = db._CLAUDE_PROJECTS_DIR / entry["project"] / f"{entry['session_id']}.jsonl"
    return path if path.is_file() else None


async def agent_traffic(limit: int = 200, scan_files: int = 12) -> list[dict[str, Any]]:
    """Messages exchanged between concurrent sessions, newest first.

    Reads what was recorded in transcripts rather than tapping the sockets the
    sessions actually talk over: this is a log, not an interception layer, and
    it needs no knowledge of that private protocol.

    Only the most recent transcripts are scanned -- the archive runs to tens of
    megabytes and traffic older than the current run of sessions is rarely what
    anyone is looking for.
    """
    limit = max(1, min(int(limit), 1000))
    scan_files = max(1, min(int(scan_files), 60))
    return await asyncio.to_thread(_agent_traffic_sync, limit, scan_files)


async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Recent transcripts, newest first, for browsing past conversations.

    The live sessions endpoint only knows about sessions that are still
    running; this is what makes finished ones reachable.
    """
    limit = max(1, min(int(limit), 200))
    return await asyncio.to_thread(_list_sync, limit)
