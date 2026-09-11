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
import base64
import json
import logging
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

import db

_log = logging.getLogger("wc.transcripts")

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

# The headline is a label; these carry what the tool was actually asked to do
# and what came back. Both are capped because a transcript holds whole file
# reads and thousand-line command outputs -- the point is to show the substance
# of a call, not to replay the session's entire I/O into the browser.
_TOOL_DETAIL_MAX: Final[int] = 2000
_TOOL_RESULT_MAX: Final[int] = 2000

# Rendered in the detail line, in this order. "description" is deliberately
# absent: it is the headline already, and repeating it below said nothing.
_TOOL_DETAIL_KEYS: Final[tuple[str, ...]] = (
    "command",
    "file_path",
    "path",
    "pattern",
    "url",
    "query",
    "prompt",
    "old_string",
    "new_string",
    "content",
)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Return *text* cut to *limit*, and whether anything was removed."""
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def _tool_detail(block: dict[str, Any]) -> tuple[str, bool]:
    """The tool's actual input, newlines intact, as ``(text, truncated)``.

    Newlines are kept rather than collapsed: a multi-line shell script or patch
    is unreadable as one run-on line, and this is the field a reader opens
    precisely when the one-line headline was not enough.
    """
    payload = block.get("input")
    if not isinstance(payload, dict):
        return ("", False) if payload is None else _clip(str(payload), _TOOL_DETAIL_MAX)
    found: list[tuple[str, str]] = []
    for key in _TOOL_DETAIL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            found.append((key, value.strip()))
    if not found:
        return "", False
    # One field needs no label -- a bare command reads as a command. Several do,
    # or an Edit's two strings would run together with nothing saying which is
    # the old text and which is the new.
    if len(found) == 1:
        return _clip(found[0][1], _TOOL_DETAIL_MAX)
    return _clip("\n".join(f"{key}: {value}" for key, value in found), _TOOL_DETAIL_MAX)


def _result_text(content: Any) -> str:
    """Flatten a tool_result's content, which is a string or a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                pieces.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                pieces.append(item)
        return "\n".join(pieces)
    return "" if content is None else json.dumps(content)


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
            elif item.get("name") in _APPROVAL_TOOLS:
                # An approval prompt is a question too, even though the call
                # declares no text. Registering the id here is what lets the
                # "approved"/"rejected" result render as an answer rather than
                # as an unexplained blob of tool output.
                approval = _approval_block(item)
                if approval["id"]:
                    question_ids.add(approval["id"])
                blocks.append(approval)
                continue
            detail, clipped = _tool_detail(item)
            block: dict[str, Any] = {"kind": "tool", "text": _tool_summary(item)}
            if detail:
                # Carried alongside the headline rather than replacing it: the
                # summary stays scannable and the detail is there when the
                # summary ("Bash(Stage 15 docs + version sweep)") says nothing
                # about what actually ran.
                block["detail"] = detail
                block["detail_truncated"] = clipped
            if item.get("id"):
                block["id"] = str(item["id"])
            blocks.append(block)
        elif kind == "tool_result" and item.get("tool_use_id") in question_ids:
            # A question's result is its answer, and renders as one.
            blocks.append(_answer_block(item))
        elif kind == "tool_result":
            # Every other tool result: capped and folded away by the client, so
            # a reader can open the output of a specific call without the page
            # carrying every byte the session ever read.
            text, clipped = _clip(_result_text(item.get("content")).strip(),
                                  _TOOL_RESULT_MAX)
            if text:
                blocks.append({
                    "kind": "result",
                    "id": str(item.get("tool_use_id") or ""),
                    "text": text,
                    "truncated": clipped,
                    "error": bool(item.get("is_error")),
                })
    return blocks


_QUESTION_TOOL = "AskUserQuestion"

# Tools that stop and wait for a person to approve something, but declare
# nothing about the ask: the CLI draws the prompt itself, so the tool call
# carries an empty ``input``. AskUserQuestion was the only tool treated as a
# question, so these rendered as a bare tool name -- no ask, no options, and no
# hint that the session was blocked. They are not rare: across this machine's
# transcripts there are 60 of them against 47 AskUserQuestion, so more than half
# of everything waiting on an answer was invisible in the web chat.
#
# The header and ask are ours, not the CLI's, because the call carries no text
# to quote. They describe what approving actually does, since that is the
# decision being asked for.
_APPROVAL_TOOLS: Final[dict[str, tuple[str, str]]] = {
    "EnterPlanMode": (
        "Plan mode",
        ("Claude wants to explore and plan before changing anything. "
         "Approve entering plan mode?"),
    ),
    "ExitPlanMode": (
        "Plan ready",
        ("Claude has finished planning and wants to start making changes. "
         "Approve the plan?"),
    ),
}


def _approval_block(item: dict[str, Any]) -> dict[str, Any]:
    """Build a question block for an approval prompt that declares no options.

    Options are deliberately left empty rather than guessed. The live prompt is
    the only place the real choices exist, and the question endpoint already
    reads them off the terminal with ``prompts.visible_options``; inventing a
    plausible-looking list here would put labels in front of the user that the
    terminal never offered, and answering is by index.
    """
    header, ask = _APPROVAL_TOOLS[str(item.get("name"))]
    return {
        "kind": "question",
        "id": str(item.get("id") or ""),
        "questions": [{
            "question": ask,
            "header": header,
            "multi_select": False,
            "options": [],
        }],
        "approval": True,
    }


# Block ids already reported as malformed. The transcript is re-read on a timer,
# so without this one bad block logs on every poll: a single Qwen payload produced
# 749 identical lines in one day, which is more than every other warning in that
# log combined. The defect is worth one line, not 749.
_reported_payloads: set[str] = set()


def reset_reported_payloads() -> None:
    """Forget which blocks have been reported. For tests and process restarts."""
    _reported_payloads.clear()


def _report_once(block_id: str) -> bool:
    """True the first time *block_id* is seen, False afterwards."""
    if block_id in _reported_payloads:
        return False
    _reported_payloads.add(block_id)
    return True


def _repair_over_escaped(text: str) -> Any:
    """Undo one specific defect: a JSON string whose quotes are over-escaped.

    An OpenAI-compatible gateway sends tool arguments as a string of JSON, and
    Qwen 3.6 emitted one that is correctly quoted for 279 characters and
    backslash-escaped from there on. Replacing ``\\"`` with ``"`` recovers it.

    Returns the decoded value, or None if the repair does not yield something
    shaped like questions. Called **only** after a normal parse has failed:
    ``\\"`` is legal inside a JSON string value, so a valid payload containing
    ``"He said \\"hi\\""`` must never reach this. The shape check is the second
    guard -- a repair that produces a scalar, or that parses into nonsense, is
    rejected rather than trusted.

    Deliberately not ``codecs.decode(text, "unicode_escape")``, which also
    recovers this payload and would additionally decode every byte as latin-1,
    turning "café" into "cafÃ©" in any non-English question.
    """
    try:
        decoded = json.loads(text.replace('\\"', '"'))
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(decoded, list) and all(isinstance(x, dict) for x in decoded):
        return decoded
    if isinstance(decoded, dict):
        return [decoded]
    return None


def _questions_payload(raw: Any, block_id: str) -> tuple[list[Any], bool]:
    """Normalise a tool call's ``questions`` field to a list.

    Returns ``(entries, damaged)``. *damaged* means the field carried something
    that should have held questions but could not be read, which the caller
    surfaces as an unreadable question rather than dropping silently.

    Anthropic sends ``questions`` as a JSON array, but an OpenAI-compatible
    gateway (LiteLLM, vLLM -- anything whose tool ids look like
    ``chatcmpl-tool-*``) sends tool arguments as a *string* of JSON. Iterating
    that string yielded one character at a time, none of which is a dict, so
    every entry was skipped and the question disappeared from the UI with
    nothing logged. That is why questions showed in some conversations and not
    others: it tracked which backend served the turn, not anything about the
    question.
    """
    if isinstance(raw, list):
        return raw, False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], False
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            # Seen in the wild: a gateway emitted a string with mismatched
            # escaping, so no amount of parsing recovers the options.
            #
            # "Damaged" is claimed only for a payload that plainly *tried* to be
            # questions -- it opens like JSON and names the field. A short scrap
            # ("nope") is not evidence that anything was asked, and promoting it
            # to a visible question would invent an ask out of noise. Those keep
            # the old behaviour and fall back to the plain tool line.
            repaired = _repair_over_escaped(text)
            if repaired is not None:
                # Recovered. Not "damaged": the user gets the question and every
                # option, which is the whole point of attempting this.
                if _report_once(block_id):
                    _log.info(
                        "question_payload_repaired: id=%s (%s) — over-escaped "
                        "gateway payload recovered; options preserved",
                        block_id, exc,
                    )
                return repaired, False
            looks_structured = text.startswith(("[", "{")) and '"question"' in text
            if looks_structured and _report_once(block_id):
                _log.warning(
                    "question_payload_unparseable: id=%s (%s) — a question was "
                    "asked but its options cannot be read; shown without them",
                    block_id, exc,
                )
            elif not looks_structured and _report_once(block_id):
                _log.warning(
                    "question_payload_not_json: id=%s (%s) — questions field held "
                    "text that names no question; rendered as a plain tool call",
                    block_id, exc,
                )
            return [], looks_structured
        if isinstance(decoded, list):
            return decoded, False
        if isinstance(decoded, dict):
            # A single question sent unwrapped rather than as a one-item list.
            return [decoded], False
        # Parsed cleanly but into a scalar, which carries no question either.
        _log.warning(
            "question_payload_unexpected_type: id=%s decoded=%s",
            block_id, type(decoded).__name__,
        )
        return [], False
    if raw is None:
        return [], False
    _log.warning(
        "question_payload_unexpected_type: id=%s raw=%s",
        block_id, type(raw).__name__,
    )
    return [], True


def _question_block(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build a display block carrying a question and every option offered."""
    payload = item.get("input")
    if not isinstance(payload, dict):
        return None
    block_id = str(item.get("id") or "")
    entries, damaged = _questions_payload(payload.get("questions"), block_id)
    questions = []
    for entry in entries:
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
        # A damaged payload still means the agent stopped and asked something.
        # Returning None here is what made it vanish from the conversation, so
        # the reader saw a turn that simply ended -- with no hint that anything
        # was waiting on them. A placeholder is worse than the real question and
        # far better than silence.
        if damaged:
            return {
                "kind": "question",
                "id": block_id,
                "questions": [{
                    "question": "A question was asked, but its text and options "
                                "could not be read from the transcript.",
                    "header": "Unreadable question",
                    "multi_select": False,
                    "options": [],
                }],
                "unreadable": True,
            }
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
    elif "have been answered" in collapsed or "has approved" in collapsed:
        # "User has approved your plan" / "has approved exiting plan mode" is an
        # approval prompt's yes. Without this it fell to "resolved", which reads
        # as though nobody decided anything.
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
    turn = {
        "role": "assistant" if role == "assistant" else "user",
        "timestamp": str(record.get("timestamp") or ""),
        "model": str(message.get("model") or "") if role == "assistant" else "",
        "blocks": blocks,
        # Subagent traffic is interleaved into the same file; let the UI mark it.
        "sidechain": bool(record.get("isSidechain")),
    }
    # Tool output is replayed to the model inside a user record, so without this
    # flag every command result would render as though the operator had typed
    # it. The client uses it to attach the output to the call above instead.
    if all(b.get("kind") == "result" for b in blocks):
        turn["tool_output"] = True
    return turn


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


def transcript_size(session_id: str) -> int:
    """Current byte length of a session's transcript, or 0 if there is none.

    The mark used to attribute a routed request. A request typed into a live
    terminal produces turns in that terminal's transcript, and the byte position
    at the moment of typing is what separates "everything after this belongs to
    the request" from the agent's own earlier work.
    """
    path = transcript_path(session_id)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0



# Keyed by the string path, so two sessions never collide even though a
# transcript filename is a UUID with no realistic clash. Value is
# (size_at_last_scan, result). A transcript's unanswered questions cannot
# change without the file growing -- a new question or an answer are both
# appended records -- so a size match is a sound proxy for "nothing to find
# here that wasn't already found", not an approximation of one.
_question_scan_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}


def _scan_questions_sync(path: Path) -> list[dict[str, Any]]:
    """Quick-scan the full transcript for AskUserQuestion blocks.

    This does NOT parse turns (which is expensive) — it only looks for
    ``tool_use(name=AskUserQuestion)`` blocks and returns a minimal dict
    for each one so the import path can attach the question text to the
    message body.  Answered questions are skipped so the import stays
    idempotent (questions that have a ``tool_result`` will render as
    "Answered in the terminal" via the normal turn import of the tail).

    Every caller here polls: once per open conversation on a 5s timer, and
    again for every linked conversation on a 30s sweep. Without the cache
    below this read the whole file -- unbounded by the offset the rest of
    the import already respects, tens of megabytes on the transcripts this
    host actually has -- on every single one of those polls, whether or not
    a byte had changed since the last one. Measured on 2026-09-03: 8
    transcripts over 16 MB each, ~195 MB combined, rescanned in full by
    every open tab's sweep every 30 seconds. That is disk and CPU spent
    finding the same answer as a moment ago, not new information.
    """
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    cached = _question_scan_cache.get(key)
    if cached is not None and cached[0] == size:
        return list(cached[1])

    try:
        raw = path.read_bytes()
    except OSError:
        return []

    asked: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()

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

    # Return unanswered questions (the ones the UI is missing)
    questions: list[dict[str, Any]] = []
    for qid in reversed(list(asked.keys())):
        if qid not in answered:
            questions.append(asked[qid])
    # Cached under the size actually read, not the earlier stat() -- the file
    # can grow between the two, and keying on what was really parsed is what
    # keeps the next call's comparison honest rather than trusting a number
    # that may already be stale by the time it is stored.
    _question_scan_cache[key] = (len(raw), list(questions))
    return questions


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


# Bytes kept from just before a resume point to prove the file is still the
# one that was parsed. A size comparison alone cannot tell "appended to" from
# "rewritten to a similar length": repair_if_needed() rewrites a transcript in
# place (dropping empty-text records), and if the session then appends past
# the old offset, resuming would carry state describing records that no longer
# exist. Found by a test that regrew a truncated file *larger* than the old
# offset -- the earlier shrink-only check passed only because the first
# version of that test happened to leave it smaller.
_RESUME_ANCHOR_BYTES: Final[int] = 64


# Read size for the streaming scan below. The cold path used to slurp the
# whole file -- handle.read() on 86MB, then .decode(), then .split(b"\n") --
# several full copies, and a peak of hundreds of megabytes. On this host that
# peak is what got webconsole.service OOM-killed (SIGKILL, no traceback) while
# five claude CLIs held ~1.6GB of a 3.8GB box, and each kill cold-starts the
# caches, which spikes again: a loop. Streaming holds one block, one carried
# part-line and the anchor, whatever the file's size.
_READ_CHUNK_BYTES: Final[int] = 1 << 20


class _AppendedReader:
    """Iterate the whole records added to *path* since *start*.

    Opens and verifies eagerly so ``ok`` and ``resumed`` are known before the
    caller commits to merging anything: ``resumed`` False means the file is
    not the one the caller parsed (rewritten under it), so its accumulated
    state has to be discarded *before* the new records are merged in.

    Iteration yields complete records only. A line still being written is
    carried and left unconsumed, because a growing transcript's tail is
    routinely half-written and consuming it would drop the record it carries.
    After iteration, ``consumed`` is the record boundary to resume from and
    ``anchor`` the bytes that prove it.
    """

    def __init__(self, path: Path, start: int, anchor: bytes):
        self.consumed = 0
        self.anchor = b""
        self.resumed = False
        self.ok = False
        self._handle = None
        try:
            handle = path.open("rb")
        except OSError:
            return
        self.ok = True
        self._handle = handle
        if start:
            base = max(0, start - len(anchor))
            try:
                handle.seek(base)
                if handle.read(start - base) == anchor:
                    self.resumed = True
                    self.consumed = start
                    self.anchor = anchor
                else:
                    handle.seek(0)
            except OSError:
                self.ok = False
                handle.close()
                self._handle = None

    def __enter__(self) -> _AppendedReader:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __iter__(self):
        if self._handle is None:
            return
        carry = b""
        tail = self.anchor
        while True:
            try:
                block = self._handle.read(_READ_CHUNK_BYTES)
            except OSError:
                self.ok = False
                return
            if not block:
                return
            data = carry + block
            cut = data.rfind(b"\n")
            if cut < 0:
                # No complete record in this block; keep carrying it.
                carry = data
                continue
            complete, carry = data[:cut + 1], data[cut + 1:]
            self.consumed += len(complete)
            tail = (tail + complete)[-_RESUME_ANCHOR_BYTES:]
            self.anchor = tail
            # split drops nothing: complete ends in a newline, so the final
            # element is the empty string after it.
            for line in complete.split(b"\n")[:-1]:
                yield line


# path -> (bytes_consumed, anchor, events). bytes_consumed is a *record boundary*
# (immediately after a newline), never a raw size: the tail of a growing
# transcript is routinely a half-written line, and resuming from a raw size
# would start mid-record and drop the message that line was carrying.
#
# Measured on 2026-09-09, which is why this exists: agent_traffic() scans the
# 12 most recently modified transcripts, and those are by definition the ones
# being actively appended to -- 86, 50, 49, 48, 44, 40, 31, 13MB on this host,
# ~450MB re-read in full per call, 31.4s wall and 603MB peak RSS. Reading only
# what was appended makes the cost proportional to what actually changed. A
# plain size-keyed cache (the idiom used elsewhere in this file) would have
# been near-useless here for exactly the reason these files were selected:
# they are the ones changing.
_agent_events_cache: dict[str, tuple[int, bytes, list[dict[str, Any]]]] = {}


def _agent_events_sync(path: Path, session_id: str, title: str) -> list[dict[str, Any]]:
    """Extract messages sent to and received from other sessions.

    Reads only the bytes appended since the last call (see
    _agent_events_cache). Transcripts are append-only in normal operation, so
    everything already parsed stays true -- this reads less of the file
    without showing less of the conversation, which a tail window would not
    have managed. _read_appended is what notices the abnormal case, a file
    rewritten under us.
    """
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []

    cached = _agent_events_cache.get(key)
    if cached is not None and cached[0] == size:
        return _agent_events_copy(cached[2], title)

    start, anchor = 0, b""
    events: list[dict[str, Any]] = []
    if cached is not None and size > cached[0]:
        # Grown: keep what was already parsed and read only the new bytes.
        start, anchor, events = cached[0], cached[1], list(cached[2])

    reader = _AppendedReader(path, start, anchor)
    if not reader.ok:
        # Don't poison the cache on a transient read failure -- serve what was
        # already known and try again next call.
        reader.close()
        return _agent_events_copy(cached[2], title) if cached else []
    if not reader.resumed:
        # Rewritten under us: the parsed events describe records that may no
        # longer be in the file. Checked before merging, not after.
        events = []

    with reader:
        events.extend(_parse_agent_records(reader, session_id, title))
    if not reader.ok:
        return _agent_events_copy(cached[2], title) if cached else []
    if reader.consumed > start or not reader.resumed:
        _agent_events_cache[key] = (reader.consumed, reader.anchor, list(events))
    return _agent_events_copy(events, title)


def _agent_events_copy(
    events: list[dict[str, Any]], title: str,
) -> list[dict[str, Any]]:
    """Fresh dicts, never the cached ones. _agent_traffic_sync writes
    ``sender``/``recipient`` into what it gets back, which would otherwise
    mutate the cache in place. The title is refreshed on the way out so a
    cached parse never serves a stale one."""
    out = []
    for event in events:
        copy = dict(event)
        copy["session_title"] = title
        out.append(copy)
    return out


def _parse_agent_records(
    records: Iterable[bytes], session_id: str, title: str,
) -> list[dict[str, Any]]:
    """Pull cross-session messages out of *records*, each a whole JSONL line."""
    events: list[dict[str, Any]] = []
    for raw_line in records:
        # Was a single substring check over the whole file before this
        # streamed; per line it does the same job and skips the json.loads
        # for every record that cannot possibly carry a message, which is
        # nearly all of them.
        if _AGENT_MARKER not in raw_line and b"SendMessage" not in raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace").strip()
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


def _is_foreign_thinking(block: Any) -> bool:
    """True for a thinking block produced by a different provider.

    A thinking block carries a provider-specific ``signature``. Anthropic
    validates its own and rejects one it did not sign, with
    ``400 ... each thinking block must contain non-whitespace thinking`` -- and
    the rejection is of the whole request, so every later turn in that
    conversation fails too, permanently and invisibly.

    The discriminator is the signature, never the type: a *signed* thinking
    block is the provider's own and replays correctly, so removing those would
    throw away real reasoning for nothing. Blocks written by a gateway usually
    have no ``signature`` key at all rather than an empty one.
    """
    return (
        isinstance(block, dict)
        and block.get("type") == "thinking"
        and not str(block.get("signature") or "").strip()
    )


def _is_refused_block(block: Any) -> bool:
    """True for a content block a strict API will not accept on replay."""
    if not isinstance(block, dict):
        return False
    if _is_foreign_thinking(block):
        return True
    return (
        block.get("type") == "text"
        and not str(block.get("text") or "").strip()
    )


def _needs_repair_sync(path: Path) -> bool:
    """Cheap pre-check so a healthy transcript is never fully parsed."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if any(marker in raw for marker in _EMPTY_TEXT_MARKERS):
        return True
    # Foreign thinking cannot be found with a byte marker, because the signature
    # is normally absent rather than empty -- there is no distinctive string to
    # look for. Parsing is only reached when the file contains thinking at all,
    # so a transcript without any still costs one scan.
    #
    # This was the half the pre-check missed. `_prepare_transcript_for_backend`
    # ran, repaired the empty text blocks, reported success, and left the
    # thinking blocks that actually broke five sessions on 2026-09-01.
    if b'"thinking"' not in raw:
        return False
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, list) and any(_is_foreign_thinking(b) for b in content):
            return True
    return False


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

    def _all_content_refused(record: Any) -> bool:
        """True when nothing in the record would survive a strict replay."""
        if not isinstance(record, dict):
            return False
        content = (record.get("message") or {}).get("content")
        return (
            isinstance(content, list)
            and bool(content)
            and all(_is_refused_block(block) for block in content)
        )

    dropped: dict[str, str | None] = {
        record["uuid"]: record.get("parentUuid")
        for record in parsed
        if isinstance(record, dict) and record.get("uuid")
        and (_is_empty_text_assistant(record) or _all_content_refused(record))
    }
    # A record is usually [thinking, text]: dropping it whole to remove the
    # thinking would throw away the answer. So blocks are trimmed in place, and
    # only a record left with nothing is dropped.
    trimmable = {
        id(record) for record in parsed
        if isinstance(record, dict) and record.get("uuid") not in dropped
        and isinstance((record.get("message") or {}).get("content"), list)
        and any(_is_refused_block(b)
                for b in (record.get("message") or {}).get("content"))
    }
    if not dropped and not trimmable:
        return {"repaired": False, "removed": 0, "relinked": 0, "backup": "",
                "trimmed": 0}

    def surviving_ancestor(uuid: str | None) -> str | None:
        seen: set[str] = set()
        while uuid in dropped and uuid not in seen:
            seen.add(uuid)
            uuid = dropped[uuid]
        return uuid

    kept: list[str] = []
    relinked = 0
    trimmed = 0
    for record, line in zip(parsed, raw_lines):
        if record is None:
            kept.append(line)
            continue
        if _is_empty_text_assistant(record) or _all_content_refused(record):
            continue
        changed = False
        if id(record) in trimmable:
            content = record["message"]["content"]
            clean = [b for b in content if not _is_refused_block(b)]
            trimmed += len(content) - len(clean)
            record["message"]["content"] = clean
            changed = True
        if record.get("parentUuid") in dropped:
            record["parentUuid"] = surviving_ancestor(record["parentUuid"])
            relinked += 1
            changed = True
        # Unchanged lines are written back byte for byte; only a record this
        # pass actually modified is re-serialised.
        kept.append(json.dumps(record) if changed else line)

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
        "trimmed": trimmed,
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


# Records that state which prompt the session is working on. `attachment.prompt`
# is written when the CLI actually begins a prompt, which is what makes this
# usable where a byte offset is not: input typed into a busy session is queued,
# so work can start long after it was typed, and everything the agent does in
# between belongs to whatever it was already doing.
def _prompt_boundary(record: dict) -> str | None:
    """The prompt this record says the session is now working on, if it says one."""
    kind = record.get("type")
    if kind == "attachment":
        attachment = record.get("attachment")
        if isinstance(attachment, dict):
            text = attachment.get("prompt")
            if isinstance(text, str) and text.strip():
                return text
        return None
    if kind == "queue-operation":
        text = record.get("content")
        return text if isinstance(text, str) and text.strip() else None
    if kind == "user":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = " ".join(parts).strip()
            # A user record is also how a tool result comes back; those carry no
            # text block and must not be read as a new prompt.
            return joined or None
    return None


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
    # Whether this model reported a cache breakdown at all. When it does not,
    # `input_tokens` is the entire conversation re-read on every turn rather
    # than new spend -- a gateway session here averaged 106,769 input tokens a
    # turn with no cache line, so summing it reported a billion tokens for work
    # that mostly re-sent the same context. Detected by key presence rather than
    # by a size threshold, because the transcript states it outright.
    context_unsplit = not (
        "cache_read_input_tokens" in usage
        or "cache_creation_input_tokens" in usage
    )

    cost = record.get("costUSD")
    if not isinstance(cost, (int, float)):
        cost = None
    return {
        "model": model,
        "context_unsplit": context_unsplit,
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
        # Each row carries the byte offset just past its own line so the writer
        # can checkpoint part-way through a large import: stopping between
        # batches then leaves the cursor exactly at what was stored, which is
        # what keeps a retry from counting the same turns twice.
        cursor = offset
        current_prompt = ""
        for raw_line in consumed.split(b"\n")[:-1]:
            cursor += len(raw_line) + 1
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            boundary = _prompt_boundary(record)
            if boundary:
                current_prompt = boundary
            row = _usage_from_record(record)
            if row:
                row["offset"] = cursor
                # What the session was working on when this turn ran. Lets a
                # routed request claim its own turns and nothing else.
                row["after_prompt"] = current_prompt
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


# A failed turn is recorded as an assistant record in the model's own voice with
# model "<synthetic>" -- the CLI reporting an error, not the model speaking. The
# text is the only place the reason survives, and it always opens this way.
_SYNTHETIC_MODEL: Final[str] = "<synthetic>"
_ERROR_PREFIX: Final[str] = "api error"
# Only the tail is read. This runs against sessions the orchestrator has already
# decided are busy, on every poll, so it must not touch a 22 MB archive.
_ERROR_TAIL_BYTES: Final[int] = 64 * 1024


def _last_error_sync(path: Path) -> str | None:
    """The newest turn's error text, or None if the newest turn is not an error.

    Newest *turn*, deliberately, not "an error anywhere in the tail": a session
    that failed and then recovered has a real assistant record after the
    synthetic one, and reporting the stale failure would leave a healthy agent
    flagged until someone dismissed it by hand.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _ERROR_TAIL_BYTES:
                handle.seek(size - _ERROR_TAIL_BYTES)
                handle.readline()  # discard the partial line the seek landed in
            raw = handle.read()
    except OSError:
        return None

    for line in reversed(raw.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = ""
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "")
                    break
        elif isinstance(content, str):
            text = content
        # The newest assistant record decides, whatever it is.
        if str(message.get("model") or "") != _SYNTHETIC_MODEL:
            return None
        stripped = text.strip()
        return stripped if stripped.lower().startswith(_ERROR_PREFIX) else None
    return None


async def last_error(session_id: str) -> str | None:
    """Report a session whose newest turn is a failed one.

    The orchestrator short-circuits on Claude Code's own "busy" status and never
    reads the transcript, which is what keeps polling cheap. But a session
    retrying a failing endpoint reports busy the whole time -- "Retrying in 11s
    - attempt 9/10" is busy by every measure the status field has -- so a run
    that is going nowhere looked exactly like one doing work, and nothing was
    raised. This is the cheap tail read that tells the two apart.
    """
    path = transcript_path(session_id)
    if path is None:
        return None
    return await asyncio.to_thread(_last_error_sync, path)


# Whether the newest turn finished. Kept beside _last_error_sync because it is
# the same shape of read and shares its budget: measured on this machine, the
# newest assistant record with a stop_reason sat within the last 9 lines and
# 16 KB of transcripts that are 10-18 MB and 6,000-10,000 lines long, so 64 KB
# is a generous bound rather than a guess.
#
# A *tail peek* is not enough, which is why this reads a block rather than the
# last few records: one live session's last six records were all metadata
# (`agent-name`, `mode`, `permission-mode`, `atis-latch`) with no assistant
# record among them.
_CONCLUSION_TAIL_BYTES: Final[int] = 64 * 1024

# Claude Code's own reason for stopping. "end_turn" means it had nothing further
# to do; "tool_use" means it stopped to run something and is mid-turn.
_END_OF_TURN: Final[str] = "end_turn"


def _conclusion_sync(path: Path) -> dict[str, Any]:
    """Whether the newest turn in *path* has concluded.

    Returns ``stop_reason`` (the newest assistant record's, or None if the tail
    holds none), ``prompt_after`` (a real prompt arrived after it, so a new turn
    has already started) and ``concluded``.

    The two are both needed. A session that finished a turn and was then given
    more work still carries ``end_turn`` as its newest stop_reason -- observed
    live, on a session that flipped from idle to busy between two reads -- so
    the stop_reason alone reports a working agent as finished.
    """
    state: dict[str, Any] = {
        "stop_reason": None, "prompt_after": False, "concluded": False,
    }
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _CONCLUSION_TAIL_BYTES:
                handle.seek(size - _CONCLUSION_TAIL_BYTES)
                handle.readline()  # discard the partial line the seek landed in
            raw = handle.read()
    except OSError:
        return state

    # Forward over the tail, so "after" is a genuine ordering rather than an
    # inference from which one a reverse scan happened to meet first.
    stop_at, prompt_at = -1, -1
    for index, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "assistant":
            message = record.get("message")
            if isinstance(message, dict) and message.get("stop_reason"):
                state["stop_reason"] = message["stop_reason"]
                stop_at = index
        # _prompt_boundary, not "type == user": a tool result comes back as a
        # user record, and counting those would make every tool call look like
        # a fresh prompt and nothing would ever read as concluded.
        elif _prompt_boundary(record):
            prompt_at = index

    state["prompt_after"] = prompt_at > stop_at
    state["concluded"] = (
        state["stop_reason"] == _END_OF_TURN and not state["prompt_after"]
    )
    return state


async def turn_concluded(session_id: str) -> dict[str, Any]:
    """Whether *session_id*'s newest turn has finished.

    Corroborates the session registry's ``status`` field rather than replacing
    it: ``status`` is what Claude Code says about itself and costs no parsing,
    while this is derived from what it actually wrote. They agreed on every live
    session tested, including one that changed state mid-test.
    """
    path = transcript_path(session_id)
    if path is None:
        return {"stop_reason": None, "prompt_after": False, "concluded": False}
    return await asyncio.to_thread(_conclusion_sync, path)


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


# path -> (bytes_consumed, asked, answered). Incremental, like
# _agent_events_cache, and for the same reason: the offset is a record
# boundary rather than a raw size, because a growing transcript's last line
# is routinely half-written.
#
# This started (2026-09-09, earlier the same day) as a size-keyed cache of the
# verdict, which removed the repeat cost for an *idle* transcript and nothing
# at all for a busy one -- measured live afterwards, 10 chats were armed and
# four of their transcripts were growing every few seconds (51, 50, 42 and
# 15MB, +6-19KB per 20s), so every append invalidated the entry and the next
# poll re-read the whole file. That is 52-195MB per 20s of re-reading, and
# it is the chats someone is actively working in that lose the benefit --
# exactly the wrong way round.
#
# Keeping `asked`/`answered` instead of the verdict is what makes resuming
# possible: both are monotonic over an append-only file (a new record can add
# an ask or add an answer, never retract either), and they accumulate in file
# order, so an incremental merge reaches the same state a full scan would.
# The scan itself still covers the whole file -- a session can sit on a prompt
# for a long time with nothing appended after it, so reading *less* of the
# file would miss exactly the prompt this exists to find. What is removed is
# re-reading, not reading.
#
# Cost: 1,320ms on cweb2's 86MB transcript, run every 4s per open
# conversation (app.js QUESTION_POLL_MS) and again every 5s server-side per
# armed chat (auto_answer, via routes.chats._pending_prompt).
_pending_question_cache: dict[
    str, tuple[int, bytes, dict[str, dict[str, Any]], set[str]]
] = {}


def _question_verdict(
    asked: dict[str, dict[str, Any]], answered: set[str],
) -> dict[str, Any] | None:
    """The newest ask with no matching answer, as the API shape."""
    for qid, built in reversed(list(asked.items())):
        if qid in answered:
            continue
        first = (built.get("questions") or [{}])[0]
        # An approval prompt gets no needle. The needle confirms that the text
        # we are about to answer is the text on screen, which only works when
        # the question came with words of its own; an approval's ask is written
        # here, so matching on it would never find anything and every approval
        # would be reported unreachable -- with "not running inside screen or
        # tmux" as the reason, which would be a lie. find_target still requires
        # looks_like_a_prompt(), so keystrokes cannot land in a window that is
        # not asking anything; what is given up is only the check that it is
        # asking *this*, and pending_question already returns the newest
        # unanswered prompt.
        needle = "" if built.get("approval") else str(
            first.get("question") or "").strip()
        return {
            "id": qid,
            "questions": built.get("questions") or [],
            "approval": bool(built.get("approval")),
            "needle": needle,
        }
    return None


def pending_question(session_id: str) -> dict[str, Any] | None:
    """Return the question *session_id* is still waiting on, or None.

    A question is pending when its tool_use has no matching tool_result.
    Reads only the bytes appended since the last call and merges them into
    the running asks/answers -- see _pending_question_cache for why that
    reaches the same state as a full scan, and why reading less of the file
    would not.

    ``needle`` is the question text, used to confirm the prompt really is on
    screen before a keystroke is delivered to that window.
    """
    path = transcript_path(session_id)
    if path is None:
        return None

    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None

    cached = _pending_question_cache.get(key)
    if cached is not None and cached[0] == size:
        return _question_verdict(cached[2], cached[3])

    start, anchor = 0, b""
    asked: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()
    if cached is not None and size > cached[0]:
        # Grown: resume from the record boundary, keeping what is already
        # known. Copied rather than mutated in place so a failed read below
        # cannot leave the cache half-updated.
        start, anchor = cached[0], cached[1]
        asked, answered = dict(cached[2]), set(cached[3])

    reader = _AppendedReader(path, start, anchor)
    if not reader.ok:
        # Serve what was already known rather than poisoning the cache with a
        # verdict no bytes backed; the next call tries again.
        reader.close()
        return _question_verdict(cached[2], cached[3]) if cached else None
    if not reader.resumed:
        # The file was rewritten, so the accumulated asks and answers describe
        # records that may no longer be there. Checked before merging.
        asked, answered = {}, set()

    with reader:
        _parse_question_records(reader, asked, answered)
    if not reader.ok:
        return _question_verdict(cached[2], cached[3]) if cached else None
    if reader.consumed > start or not reader.resumed:
        _pending_question_cache[key] = (
            reader.consumed, reader.anchor, asked, answered)
    return _question_verdict(asked, answered)


def _parse_question_records(
    records: Iterable[bytes], asked: dict[str, dict[str, Any]],
    answered: set[str],
) -> None:
    """Merge the asks and answers in *records*, each a whole JSONL line, into
    the running state."""
    for line in records:
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
            elif (
                block.get("type") == "tool_use"
                and block.get("name") in _APPROVAL_TOOLS
                and block.get("id")
            ):
                asked[str(block["id"])] = _approval_block(block)
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                answered.add(str(block["tool_use_id"]))


# Auto-reply to another Claude session by writing a <cross-session-message>
# record into its transcript file.  The CLI's MCP server picks up pending
# cross-session records on its next read cycle (typically within a few
# seconds) without any additional signal.
def agent_reply_to(session_id: str, text: str) -> dict[str, Any]:
    """Inject a cross-session message into the given session's transcript.

    Returns {"ok": True, "path": "/…", "session_id": "<uuid>"} on success, or
    {"ok": False, "reason": "..."} if the session could not be located or the
    file could not be written. session_id is the resolved UUID (session_id
    here is actually the session *name*, e.g. "cweb6") -- callers that need
    to address this session directly (a wake-up turn's --resume) use it
    rather than re-resolving the name themselves.
    """
    # Locate the transcript file by scanning all active sessions and finding the
    # one whose name matches *session_id*.  _session_names_sync gives us
    # session_id -> name; we invert that map.
    by_session, _ = _session_names_sync()
    name_map: dict[str, str] = {}
    for sid, name in by_session.items():
        name_map.setdefault(name, []).append(sid)

    candidates = name_map.get(session_id)
    if not candidates:
        return {"ok": False, "reason": f"no session named '{session_id}'"}

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())
    record = json.dumps({
        "type": "user",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": (
                f'<cross-session-message from="webconsole" '
                f'from-name="auto-reply" from-mode="auto-reply">\n'
                f'{text}\n'
                f'</cross-session-message>'
            ),
        },
    })

    last_error: str | None = None
    for cand in candidates:
        path = transcript_path(cand)
        if path is None:
            continue
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(record + "\n")
            return {"ok": True, "path": str(path), "session_id": cand}
        except OSError as exc:
            last_error = str(exc)

    return {"ok": False, "reason": last_error or "no transcript file found"}


def build_remote_reply_command(remote_path: str, target: str, text: str) -> str:
    """Build the one-liner routes/chats.py runs on a remote host via
    tunnel_manager.exec_command to relay a cross-session message there.

    *target*/*text* are never interpolated into the shell string directly --
    they are JSON-encoded then base64'd, and only the base64 blob (alphabet
    A-Za-z0-9+/=, no shell metacharacters) is embedded. The remote side
    decodes it and calls this same module's agent_reply_to(), so the
    resolve+append logic -- including its session-name-to-path safety
    checks -- runs identically to the local case, just on the far host.
    See docs/superpowers/specs/2026-09-08-transport-aware-agent-reply-design.md.
    """
    payload = base64.b64encode(
        json.dumps({"to": target, "text": text}).encode("utf-8")
    ).decode("ascii")
    # $HOME is set by the SSH daemon; ~ is not expanded in non-interactive
    # exec shells.  Also add remote_path to sys.path so the `import transcripts`
    # on the far side can actually find the module (python3 -c runs from $PWD,
    # but the cd lands in remote_path which is not on sys.path by default).
    resolved_path = remote_path.replace("~/", "$HOME/", 1)
    script = (
        "import base64,json,os,sys;"
        f"path=os.path.expanduser('{remote_path}');"
        "sys.path.insert(0,path);"
        f"d=json.loads(base64.b64decode('{payload}'));"
        "print(json.dumps(transcripts.agent_reply_to(d['to'], d['text'])))"
    )
    return f"cd {resolved_path} && python3 -c \"{script}\""
