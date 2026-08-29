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
import shutil
import time
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


def _blocks_from_content(
    content: Any, question_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """Normalise a record's ``message.content`` into display blocks.

    *question_ids* accumulates the ids of questions seen so far, so a later
    record's tool_result can be recognised as an answer to one.
    """
    question_ids = question_ids if question_ids is not None else set()
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
            if item.get("name") == _QUESTION_TOOL:
                # A question rendered as the bare string "AskUserQuestion" told
                # the reader nothing: no question text, no options, no way to
                # know an answer was wanted. Carry the whole structure instead.
                question = _question_block(item)
                if question:
                    # Register the id so this question's answer, which arrives
                    # in a later record, can be recognised.
                    if question["id"]:
                        question_ids.add(question["id"])
                    blocks.append(question)
                    continue
            blocks.append({"kind": "tool", "text": _tool_summary(item)})
        elif kind == "tool_result" and item.get("tool_use_id") in question_ids:
            # tool_result is dropped for every other tool -- it is replayed tool
            # output and dwarfs the conversation. A question's result is the
            # answer, so it is the one worth keeping.
            blocks.append(_answer_block(item))
        # tool_result is deliberately dropped: it is tool output replayed into
        # the next user record, and it dwarfs the conversation itself.
    return blocks


_QUESTION_TOOL = "AskUserQuestion"


def _question_block(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build a display block carrying a question and every option offered."""
    payload = item.get("input")
    if not isinstance(payload, dict):
        return None
    questions = []
    for entry in payload.get("questions") or []:
        if not isinstance(entry, dict):
            continue
        options = [
            {
                "label": str(option.get("label") or ""),
                "description": str(option.get("description") or ""),
            }
            for option in entry.get("options") or []
            if isinstance(option, dict) and option.get("label")
        ]
        text = str(entry.get("question") or "").strip()
        if not text and not options:
            continue
        questions.append({
            "question": text,
            "header": str(entry.get("header") or ""),
            "multi_select": bool(entry.get("multiSelect")),
            "options": options,
        })
    if not questions:
        return None
    return {
        "kind": "question",
        "id": str(item.get("id") or ""),
        "questions": questions,
    }


def _answer_block(item: dict[str, Any]) -> dict[str, Any]:
    """Build a display block for how a question was resolved.

    The CLI reports the outcome as prose, either "Your questions have been
    answered: ..." or a rejection notice, so the status is read from that rather
    than invented.
    """
    content = item.get("content")
    text = content if isinstance(content, str) else json.dumps(content)
    collapsed = " ".join(str(text).split())
    if item.get("is_error") or "was rejected" in collapsed:
        status = "declined"
    elif "have been answered" in collapsed:
        status = "answered"
    else:
        status = "resolved"
    return {
        "kind": "answer",
        "id": str(item.get("tool_use_id") or ""),
        "status": status,
        "text": collapsed[:600],
    }


def _turn_from_record(
    record: Any, question_ids: set[str] | None = None
) -> dict[str, Any] | None:
    """Return a display turn for one JSONL record, or None to skip it."""
    if not isinstance(record, dict):
        return None
    if record.get("type") not in _CONVERSATION_TYPES:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None

    blocks = _blocks_from_content(message.get("content"), question_ids)
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
    # Ids of questions seen so far, so their answers can be paired up.
    question_ids: set[str] = set()
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
        turn = _turn_from_record(record, question_ids)
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


# A gateway that streams its reply in chunks can emit a leading chunk carrying
# an empty text block. The gateway itself replays those happily; the Anthropic
# API rejects the whole request with "text content blocks must be non-empty",
# so a conversation started on such a gateway cannot later be resumed on
# Anthropic until they are removed.
_EMPTY_TEXT_MARKERS: Final[tuple[bytes, ...]] = (b'"text": ""', b'"text":""')


def _is_empty_text_assistant(record: Any) -> bool:
    """True for an assistant record whose only content is an empty text block."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and not str(content[0].get("text", "")).strip()
    )


def _needs_repair_sync(path: Path) -> bool:
    """Cheap pre-check so a healthy transcript is never fully parsed."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return any(marker in raw for marker in _EMPTY_TEXT_MARKERS)


def _repair_sync(path: Path) -> dict[str, Any]:
    """Drop empty assistant records, relinking anything that pointed at them.

    Removing them naively orphans their children: most carry a parentUuid
    chain, so each child is relinked to the nearest surviving ancestor.

    Lines that are not being changed are written back byte for byte, and the
    original is copied aside first, so a conversation cannot be damaged by a
    repair that goes wrong.
    """
    raw_lines = [
        line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    parsed: list[Any] = []
    for line in raw_lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append(None)  # keep unreadable lines exactly as they are

    dropped: dict[str, str | None] = {
        record["uuid"]: record.get("parentUuid")
        for record in parsed
        if _is_empty_text_assistant(record) and isinstance(record, dict)
        and record.get("uuid")
    }
    if not dropped:
        return {"repaired": False, "removed": 0, "relinked": 0, "backup": ""}

    def surviving_ancestor(uuid: str | None) -> str | None:
        seen: set[str] = set()
        while uuid in dropped and uuid not in seen:
            seen.add(uuid)
            uuid = dropped[uuid]
        return uuid

    kept: list[str] = []
    relinked = 0
    for record, line in zip(parsed, raw_lines):
        if record is None:
            kept.append(line)
            continue
        if _is_empty_text_assistant(record):
            continue
        if record.get("parentUuid") in dropped:
            record["parentUuid"] = surviving_ancestor(record["parentUuid"])
            relinked += 1
            kept.append(json.dumps(record))
        else:
            kept.append(line)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(f".jsonl.bak-{stamp}")
    shutil.copy2(path, backup)

    staged = path.with_suffix(".jsonl.repairing")
    staged.write_text("\n".join(kept) + "\n", encoding="utf-8")
    staged.replace(path)  # same filesystem, so the swap is atomic

    return {
        "repaired": True,
        "removed": len(dropped),
        "relinked": relinked,
        "backup": backup.name,
    }


async def repair_if_needed(session_id: str) -> dict[str, Any]:
    """Make *session_id* replayable on a strict API, if it is not already."""
    path = transcript_path(session_id)
    if path is None:
        return {"repaired": False, "removed": 0, "relinked": 0, "backup": ""}
    if not await asyncio.to_thread(_needs_repair_sync, path):
        return {"repaired": False, "removed": 0, "relinked": 0, "backup": ""}
    return await asyncio.to_thread(_repair_sync, path)


# Token accounting for turns that ran in a terminal rather than through this
# app. Every assistant record carries the model and a usage object, so a
# session's spend is recoverable without the app having been involved in it.
_USAGE_MARKER: Final[bytes] = b'"usage"'


def _usage_from_record(record: Any) -> dict[str, Any] | None:
    """Token counts for one assistant record, or None if it carries none."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    def count(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    model = str(message.get("model") or "").strip()
    # A synthetic reply is the CLI reporting an error in the model's voice; it
    # bought nothing and must not appear as spend.
    if not model or model == "<synthetic>":
        return None
    tokens = {
        "input_tokens": count("input_tokens"),
        "output_tokens": count("output_tokens"),
        "cache_read_tokens": count("cache_read_input_tokens"),
        "cache_creation_tokens": count("cache_creation_input_tokens"),
    }
    if not any(tokens.values()):
        return None

    cost = record.get("costUSD")
    if not isinstance(cost, (int, float)):
        cost = None
    return {
        "model": model,
        **tokens,
        # Carried through only when the transcript states one. A third-party
        # gateway reports no trustworthy cost, and inventing one would make the
        # total read as authoritative when it is not.
        "cost_usd": float(cost) if cost is not None else None,
        "timestamp": str(record.get("timestamp") or ""),
    }


def _usage_since_sync(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Usage rows appended after *offset*, with the new cursor position.

    A cursor is what makes this affordable and correct: the archive is tens of
    megabytes, and re-reading from the start would count every earlier turn
    again on each run.
    """
    size = path.stat().st_size
    if offset >= size:
        return [], size
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read()

    end = raw.rfind(b"\n")
    if end < 0:
        return [], offset
    consumed = raw[: end + 1]

    rows: list[dict[str, Any]] = []
    if _USAGE_MARKER in consumed:
        for line in consumed.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = _usage_from_record(record)
            if row:
                rows.append(row)
    return rows, offset + len(consumed)


async def usage_since(session_id: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Usage recorded for *session_id* after *offset*."""
    path = transcript_path(session_id)
    if path is None:
        return [], offset
    try:
        return await asyncio.to_thread(_usage_since_sync, path, offset)
    except OSError:
        return [], offset


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


def pending_question(session_id: str) -> dict[str, Any] | None:
    """Return the question *session_id* is still waiting on, or None.

    A question is pending when its tool_use has no matching tool_result. The
    whole transcript is scanned rather than a tail, because a session can sit on
    a prompt for a long time while nothing else is appended.

    ``needle`` is the question text, used to confirm the prompt really is on
    screen before a keystroke is delivered to that window.
    """
    path = transcript_path(session_id)
    if path is None:
        return None
    paths = [path]
    asked: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()
    for path in paths:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        for line in raw.split(b"\n"):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if (
                    block.get("type") == "tool_use"
                    and block.get("name") == _QUESTION_TOOL
                    and block.get("id")
                ):
                    built = _question_block(block)
                    if built:
                        asked[str(block["id"])] = built
                elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                    answered.add(str(block["tool_use_id"]))

    for qid, built in reversed(list(asked.items())):
        if qid in answered:
            continue
        first = (built.get("questions") or [{}])[0]
        return {
            "id": qid,
            "questions": built.get("questions") or [],
            "needle": str(first.get("question") or "").strip(),
        }
    return None
