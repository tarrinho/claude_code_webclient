"""Helpers more than one route prefix needs.

Membership here is measured rather than chosen. Assigning every function in
app.py to the route prefix that reaches it leaves exactly seven names reached
from two prefixes or more; those are these. A helper reached from one prefix is
not here -- it belongs with that prefix's routes, which is the rule the file
structure design states and the reason `routes/chats.py` is described as "22
routes and their single-prefix helpers".

That rule is why this file exists at all. Without it the chats and misc routers
would both need `_turn_to_message`, and a router importing app.py while app.py
imports the router is a circular import -- which in Python fails at import time
rather than, as in the ES modules on the front end, resolving at call time.

The three transcript-rendering functions are the design's `transcript_render.py`
cluster. They are here rather than in their own file because they measure 73
lines, and splitting them out would leave the other four shared names in a
21-line module. If this file grows, it splits by subject.
"""
from __future__ import annotations

import re
from typing import Final

from fastapi import HTTPException

import config
import turns

_QUESTION_PENDING_NOTE = "(answer this in the terminal)"

# A user id is 32 hex characters (uuid4().hex). Used to tell an id from a login
# name without a database round trip on the common path.
_USER_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-fA-F]{32}")


async def owner_of(session: dict | None) -> str:
    """The real user id behind *session*, translating a legacy login name.

    ``session["user"]`` has been the user's id since login switched from
    ``user["name"]`` to ``user["id"]``. Sessions are persisted and survive a
    restart, though, so one minted before that fix carries the *name* instead
    and keeps doing so for its whole TTL.

    That mattered because ``chat_create`` rejects a name outright. The resume
    endpoint hit it as a 500 -- reported live -- and was patched with this
    translation inline; ``handle_chat_create`` was not, so POST /api/chats
    still raised ValueError on such a session. One caller defending itself
    while its neighbour does not is the shape that says the check belongs at
    the point identity enters rather than at the point a row is written.

    So this is the single place that answers "who owns this?", and it is
    deliberately *not* a validator: it translates when it can and otherwise
    returns what it was given, leaving the write layer to reject a value it
    cannot store. A lookup only happens for a value that is not already an id,
    so the normal path costs one regex.
    """
    user = ((session or {}).get("user") or "").strip()
    if not user or _USER_ID_RE.fullmatch(user):
        return user
    # Not an id: either a pre-fix session carrying a login name, or something
    # that will fail at the write, which is where it should fail.
    import db

    # No database to ask is a "cannot translate", not an error. Without this
    # the lookup raised `AttributeError: 'NoneType' object has no attribute
    # 'execute'` from db_users.py:16, so a route that resolves identity before
    # touching the database answered 500 -- which is how this helper broke
    # test_qa_layers.py's ComponentAPIQA, a suite that exercises the route
    # layer deliberately without one behind it.
    if db.db_conn is None:
        return user

    row = await db.user_get_by_name(user)
    return row["id"] if row else user

# Concurrent-SSE-connection cap, shared by every stream endpoint: chat
# /stream and /live, orchestrator /stream and task /stream, transcript
# /stream. Each open connection holds a Python generator, an event buffer,
# and -- for the chat /stream endpoint specifically -- a turn slot, for as
# long as the client keeps it open, which an authenticated caller fully
# controls. Without a cap, one account opening many connections costs file
# descriptors and memory with no other action required.
_sse_slots: dict[str, int] = {}
_MAX_SSE_PER_OWNER: Final[int] = 8


def acquire_sse_slot(owner: str) -> None:
    """Reserve one of *owner*'s limited concurrent-SSE-connection slots.

    Call before constructing the ``StreamingResponse``, not from inside its
    generator. A ``StreamingResponse`` commits to its 200 status the moment
    the generator first yields, so raising the 429 here -- before that point
    -- is what makes an over-cap request actually observable as a 429 rather
    than a stream that opens and then breaks with no diagnosable status.
    """
    current = _sse_slots.get(owner, 0)
    if current >= _MAX_SSE_PER_OWNER:
        raise HTTPException(
            status_code=429,
            detail=f"Too many open streams for this account "
                   f"(max {_MAX_SSE_PER_OWNER}) — close one and retry",
        )
    _sse_slots[owner] = current + 1


def release_sse_slot(owner: str) -> None:
    """Release a slot reserved by :func:`acquire_sse_slot`.

    Called from the generator's own ``finally``, so it runs whether the
    stream ended normally, raised, or was torn down by the client
    disconnecting (``GeneratorExit``). Floors at zero rather than going
    negative, so a call with no matching acquire -- which should not happen,
    but a release is not the place to raise over it -- is harmless.
    """
    left = _sse_slots.get(owner, 0) - 1
    if left <= 0:
        _sse_slots.pop(owner, None)
    else:
        _sse_slots[owner] = left


def backend_kind(machine: dict | None) -> str:
    """Classify a backend as ``through_claude_code``, ``direct`` or ``ssh-proxy``.

    The stored ``provider`` column encodes the wire protocol (claude_code or
    direct); an ssh transport is tracked via the ``transport_id`` column and
    overrides the display kind to ``ssh-proxy`` so the SSH route is visible
    even when the underlying provider is claude_code. This function is the
    single source of truth for both usage accounting (which gates cost figures
    on claude_code paths) and the machine API, so the two surfaces cannot
    drift apart.
    """
    if not machine:
        return "proxy"  # fallback for legacy / unknown rows
    if machine.get("transport_id"):
        return "ssh-proxy"
    prov = machine.get("provider")
    if prov == "direct":
        return "direct"
    return "through_claude_code"


# Safe messages for SSE errors so internal details never leak. The first two are
# aliases of turns.py's own constants: a turn now fails inside its task, so the
# text is chosen there, and duplicating the literals here is how a timeout ends
# up reported as a generic internal error.
_SSE_INTERNAL = turns.INTERNAL_MESSAGE
# Square brackets are allowed for the documented "[1m]" context-window suffix
# (e.g. "claude-opus-5[1m]"), which the CLI itself tells users to append. The
# value is passed to the subprocess as a single argv entry, never through a
# shell, so the brackets carry no meaning downstream.
#
# Now `config.MODEL_ID_RE`, shared with orchestrator.py, and tightened at the
# first character. The previous pattern was `^[A-Za-z0-9_.:/\[\]-]+$`, which
# accepted `-p`, `--model` and `-dangerously-skip-permissions` -- flag-shaped
# values that reach the child process as the argument to `--model`. No shell is
# involved, so this is argument injection rather than command injection, and
# whether the CLI mis-parses such a value is its business; the point is that
# nothing downstream should have to be trusted to get it right.
_MODEL_RE = config.MODEL_ID_RE
# hex-only session_id pattern for path-traversal protection.
_HEX_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def _question_to_text(block: dict) -> str:
    """Render a question and every option as plain text for a message body.

    Message bodies are shown with textContent, not Markdown, so the shape has
    to survive as plain text.
    """
    lines: list[str] = []
    for entry in block.get("questions") or []:
        if not isinstance(entry, dict):
            continue
        header = str(entry.get("header") or "").strip()
        question = str(entry.get("question") or "").strip()
        lines.append(f"Question — {header}" if header else "Question")
        if question:
            lines.append(question)
        if entry.get("multi_select"):
            lines.append("(choose one or more)")
        for option in entry.get("options") or []:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            if not label:
                continue
            description = str(option.get("description") or "").strip()
            lines.append(f"  • {label} — {description}" if description else f"  • {label}")
        lines.append(_QUESTION_PENDING_NOTE)
    return "\n".join(lines).strip()


def _answer_to_text(block: dict) -> str:
    """Render how a question was resolved."""
    status = block.get("status") or "resolved"
    label = {"answered": "Answered", "declined": "Declined"}.get(status, "Resolved")
    text = " ".join(str(block.get("text") or "").split())
    return f"{label} in the terminal: {text}" if text else f"{label} in the terminal"


def _turn_to_message(turn: dict) -> tuple[str, str] | None:
    """Flatten one transcript turn into a (role, content) message row.

    The messages table holds a role and a body, with nowhere to record that a
    turn came from a subagent, so sidechain traffic is dropped rather than
    replayed unlabelled among the user's own turns -- the transcript viewer
    already shows it marked. Thinking blocks are dropped for the same reason:
    the terminal collapses them, so replaying them inline would show more than
    the conversation the user actually saw.
    """
    if turn.get("sidechain"):
        return None
    parts: list[str] = []
    for block in turn.get("blocks") or []:
        kind = block.get("kind")
        # A question carries no "text" -- its content is the question and its
        # options -- so reading block["text"] dropped it from the conversation
        # entirely. A question the user has to answer is the last thing that
        # should go missing here.
        if kind == "question":
            rendered = _question_to_text(block)
            if rendered:
                parts.append(rendered)
            continue
        if kind == "answer":
            rendered = _answer_to_text(block)
            if rendered:
                parts.append(rendered)
            continue
        text = (block.get("text") or "").strip()
        if not text:
            continue
        if kind == "text":
            parts.append(text)
        elif kind == "tool":
            parts.append(f"`{text}`")
    if not parts:
        return None
    role = "assistant" if turn.get("role") == "assistant" else "user"
    return role, "\n\n".join(parts)
