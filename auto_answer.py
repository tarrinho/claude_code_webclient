"""Answer approval and permission prompts for chats that asked to be.

Step 2 of docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md. The
storage is in db.py (`chat_auto_answer_*`, `chats_with_auto_answer`); the routes
that let a user arm this are step 3.

WHAT THIS DOES, PLAINLY. For a chat whose owner switched the knob on, it presses
a key on their behalf in answer to a prompt that exists to ask them. That is a
real transfer of authority, and the module is written to be conservative in
exactly one direction: it would rather leave a prompt unanswered than press
something it cannot justify.

Two rules do that work.

**It answers only what has a "yes".** `approval` is set by both producers of a
pending prompt -- `prompts.read_prompt` for a permission prompt read off the
terminal, and `transcripts` for a plan approval -- so the gate reads a flag the
producer already set. A structured `AskUserQuestion` has no `approval` and real
`options`: given `Delete the branch` / `Keep it` there is no affirmative answer,
only a guess, so those are left alone. Deliberately not a heuristic on the
prompt text: matching phrases against model-authored prose is what made
`_ASKS_FOR_INPUT` fire on "worth fixing" and "your call" elsewhere in this tree.

**It chooses by label, never by position.** A permission prompt offers two
affirmative answers and only one of them is the answer the operator meant:

    1. Yes
    2. Yes, and don't ask again for Bash(curl*) in this project
    3. No, and tell Claude what to do differently

Index 2 grants a standing permission for every future matching command. Since
`prompts.answer` navigates and confirms, choosing it is committed before anyone
sees it. So a broadening clause disqualifies an option, and if that leaves no
single unambiguous affirmative the watcher records a skip and moves on.

**A structured question stays somebody's decision, unless a second authority
says otherwise.** A chat can additionally arm "accept recommended" (the
`auto_answer_recommend` column). With that on, a structured `AskUserQuestion`
is no longer skipped in silence: if exactly one visible option's label ends in
"(Recommended)" -- the convention this deployment's own question-asking tools
use for the option their author judged best -- that one gets pressed, by the
same by-label-never-by-position rule as the affirmative case. Anything else
(no marked option, more than one, wording that does not end that way) is
recorded as a skip rather than guessed at.

The pending-prompt lookup and the keystroke delivery are injected rather than
imported. The canonical lookup is `routes.chats._pending_prompt`, private to a
module that was split out of app.py; injection keeps this module free of a
routes import and free of the cycle that would create, and mirrors
`sysstats.start`, which already takes its store as an argument.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

import db

_log = logging.getLogger("wc.auto_answer")

# How often to look. Each pass costs one prompt lookup per armed chat -- a
# transcript read and possibly a multiplexer snapshot -- so the cost tracks how
# many chats are armed rather than how many exist.
DEFAULT_INTERVAL_S: Final[float] = 5.0

# An option that answers "yes" and nothing more. Anchored at the start so a
# refusal that merely contains the word ("No, and tell Claude yes...") cannot
# match, and bounded at the end so "Yes, and don't ask again" is not read as a
# plain "Yes" with a suffix.
_AFFIRMATIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:yes|approve|approved|allow|continue|proceed|confirm|ok|okay)"
    r"(?:[\s,.!]+(?:please|it|that|this|proceed|continue))*[\s,.!]*$",
    re.IGNORECASE,
)

# Any of these in an option's label means it grants more than this one prompt.
# Rejected outright: the operator asked for prompts to be answered, not for the
# rules to be rewritten. "and tell claude" is here because it is the wording of
# the *refusal* option, which must never be mistaken for an approval.
_BROADENING_RE: Final[re.Pattern[str]] = re.compile(
    r"don'?t ask again|do not ask again|always|every time|"
    r"for this (?:project|session|directory)|for all|"
    r"add (?:a )?(?:permission )?rule|and tell claude",
    re.IGNORECASE,
)

# Anchored at the end, like _AFFIRMATIVE_RE is anchored at the start: the
# marker has to be what the label ends with, not merely contain, so an option
# that happens to quote the phrase mid-sentence cannot match by accident.
_RECOMMENDED_RE: Final[re.Pattern[str]] = re.compile(
    r"\(recommended\)\s*$", re.IGNORECASE,
)

_task: asyncio.Task | None = None


# ── The decision ────────────────────────────────────────────────────────────


def is_answerable(pending: dict[str, Any] | None) -> bool:
    """Whether *pending* is the kind of prompt this module may answer.

    True only for an approval or permission prompt. A structured question is
    somebody's decision to make.
    """
    return bool(pending and pending.get("approval"))


def choose_affirmative(
    options: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """The one option that plainly means "yes", or None if there is not exactly
    one.

    None is returned for no candidates *and* for several, and the second case
    matters as much as the first: two options that both look affirmative means
    the wording is not understood, and picking between them is guessing.
    """
    candidates = [
        option
        for option in options
        if isinstance(option, dict)
        and isinstance(option.get("label"), str)
        and _AFFIRMATIVE_RE.match(option["label"].strip())
        and not _BROADENING_RE.search(option["label"])
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def choose_recommended(
    options: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """The one option marked "(Recommended)", or None if there is not exactly
    one.

    Same reasoning as :func:`choose_affirmative`'s None case: two options both
    claiming the marker means something upstream is malformed, and guessing
    between them is exactly what this module exists not to do.
    """
    candidates = [
        option
        for option in options
        if isinstance(option, dict)
        and isinstance(option.get("label"), str)
        and _RECOMMENDED_RE.search(option["label"].strip())
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _prompt_text(pending: dict[str, Any]) -> str:
    questions = pending.get("questions") or []
    if questions and isinstance(questions[0], dict):
        return str(questions[0].get("question") or "")
    return ""


def _prompt_kind(pending: dict[str, Any]) -> str:
    questions = pending.get("questions") or []
    if questions and isinstance(questions[0], dict):
        return str(questions[0].get("header") or "Waiting")
    return "Waiting"


# ── One pass over one chat ──────────────────────────────────────────────────


async def consider(
    chat: dict[str, Any],
    resolve_pending: Callable[[str], Any],
    read_options: Callable[[str], Sequence[dict[str, Any]]],
    deliver: Callable[[str, int], dict[str, Any]],
) -> dict[str, Any] | None:
    """Answer *chat*'s pending prompt if it is safe to. Returns the log entry.

    None means nothing was recorded, and that is the right outcome twice over:
    no prompt is waiting, or the prompt is a structured question this chat has
    not armed "accept recommended" for. Neither is a decision, so neither
    belongs in a log the user reads to see what was decided for them.
    """
    session_id = chat.get("session_id") or ""
    if not session_id:
        return None

    pending = resolve_pending(session_id)
    if asyncio.iscoroutine(pending):
        pending = await pending
    if not pending:
        return None

    # Apply cooldown: same (chat, question) answered within COOLDOWN_S → skip.
    import hashlib
    now = time.monotonic()
    text = _prompt_text(pending)
    kind = _prompt_kind(pending)
    raw = f"{kind}|{text}"
    cooldown_key = (str(chat.get("id", "")), hashlib.md5(raw.encode()).hexdigest()[:12])
    last = _auto_answer_cooldown.get(cooldown_key)
    if last and now - last < _COOLDOWN_S:
        _log.info(
            "auto_answer_skipped: cooldown chat_id=%s elapsed=%.0fs",
            str(chat.get("id")), now - last,
        )
        return None

    if is_answerable(pending):
        chooser = choose_affirmative
        skip_reason = "no single unambiguously affirmative option; left for you"
    elif chat.get("auto_answer_recommend") and pending.get("questions"):
        chooser = choose_recommended
        skip_reason = "no single option marked \"(Recommended)\"; left for you"
    else:
        # A structured question this chat has not armed "accept recommended"
        # for. Left for the user, and not logged: it was never this module's
        # to answer, so recording it would read as a refusal.
        return None

    entry: dict[str, Any] = {
        "kind": _prompt_kind(pending),
        "prompt": _prompt_text(pending)[:400],
    }

    options = read_options(session_id)
    if asyncio.iscoroutine(options):
        options = await options
    chosen = chooser(options or [])
    if chosen is None:
        entry.update(outcome="skipped", reason=skip_reason)
        await _record(chat, entry)
        return entry

    result = deliver(session_id, int(chosen["index"]))
    if asyncio.iscoroutine(result):
        result = await result
    if not (result or {}).get("ok"):
        entry.update(
            outcome="skipped",
            reason=str((result or {}).get("reason") or "delivery failed"),
        )
        await _record(chat, entry)
        return entry

    entry.update(
        outcome="answered",
        index=int(chosen["index"]),
        label=str(result.get("label") or chosen["label"]),
    )
    _log.info(
        "auto_answered chat_id=%s owner=%s index=%s label=%s",
        chat.get("id"), chat.get("owner_id"), entry["index"], entry["label"],
    )
    await _record(chat, entry)
    return entry


async def _record(chat: dict[str, Any], entry: dict[str, Any]) -> None:
    """Append to the chat's rolling log, never letting that fail the pass.

    A prompt that was answered and not recorded is worse than one that was not
    answered: the log is the only trace the user has.
    """
    try:
        await db.chat_auto_answer_log_append(str(chat.get("id")), entry)
    except Exception:
        _log.exception("could not record auto-answer for chat %s", chat.get("id"))


# ── The loop ────────────────────────────────────────────────────────────────


def start(
    resolve_pending: Callable[[str], Any],
    read_options: Callable[[str], Sequence[dict[str, Any]]] | None = None,
    deliver: Callable[[str, int], dict[str, Any]] | None = None,
    interval_s: float | None = None,
) -> None:
    """Begin watching armed chats. Idempotent, like :func:`sysstats.start`."""
    global _task
    if _task and not _task.done():
        return  # A second lifespan must not double the poll rate.
    every = DEFAULT_INTERVAL_S if interval_s is None else interval_s
    _task = asyncio.create_task(
        _loop(resolve_pending, read_options, deliver, every)
    )


# ── Cooldown tracker ──────────────────────────────────────────────────────────

# Prevents auto-answer from hammering the same unresolved question.
# key: (chat_id, prompt_hash) → last_answer_time (monotonic)
_auto_answer_cooldown: dict[tuple[str, str], float] = {}
# After answering, skip this (chat, question) pair for COOLDOWN_S seconds.
_COOLDOWN_S: Final[float] = 300.0  # 5 minutes


def _cooldown_key(chat: dict[str, Any], pending: dict[str, Any]) -> tuple[str, str] | None:
    """Return a (chat_id, question_hash) pair, or None if unhashable."""
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return None
    text = _prompt_text(pending)
    kind = _prompt_kind(pending)
    import hashlib
    raw = f"{kind}|{text}"
    return (chat_id, hashlib.md5(raw.encode()).hexdigest()[:12])


async def _cooldown_check(key: tuple[str, str] | None) -> bool:
    """Return True if it's safe to answer (not in cooldown)."""
    if not key:
        return True
    now = time.monotonic()
    last = _auto_answer_cooldown.get(key)
    if last and now - last < _COOLDOWN_S:
        _log.info(
            "auto_answer_skipped: cooldown chat_id=%s key=%s elapsed=%.0fs",
            key[0], key[1], now - last,
        )
        return False
    _auto_answer_cooldown[key] = now
    return True


def _cooldown_cleanup() -> None:
    """Remove stale cooldown entries (older than 2x COOLDOWN_S)."""
    now = time.monotonic()
    cutoff = now - _COOLDOWN_S * 2
    stale = [k for k, v in _auto_answer_cooldown.items() if v < cutoff]
    for k in stale:
        del _auto_answer_cooldown[k]


# Periodic cleanup every 10 minutes
_cooldown_cleanup_task: asyncio.Task | None = None


async def _cooldown_loop() -> None:
    while True:
        try:
            await asyncio.sleep(600)  # 10 min
            _cooldown_cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("auto_answer cooldown cleanup failed")


async def _start_cooldown_cleanup() -> None:
    global _cooldown_cleanup_task
    if _cooldown_cleanup_task and not _cooldown_cleanup_task.done():
        return
    _cooldown_cleanup_task = asyncio.create_task(_cooldown_loop())


async def _stop_cooldown_cleanup() -> None:
    global _cooldown_cleanup_task
    if _cooldown_cleanup_task and not _cooldown_cleanup_task.done():
        _cooldown_cleanup_task.cancel()
        try:
            await _cooldown_cleanup_task
        except asyncio.CancelledError:
            pass
        _cooldown_cleanup_task = None


async def stop() -> None:
    """Cancel the watcher and wait for it. Safe if it never started.

    Held in a handle and cancelled here rather than left to the loop teardown:
    rules.md §4 names a timer nothing can stop as the failure case, and this
    project shipped one in web/supervisor.js that doubled whenever its setup ran
    twice.
    """
    global _task
    if not _task:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _task
    _task = None


def is_running() -> bool:
    return bool(_task and not _task.done())


async def _loop(
    resolve_pending: Callable[[str], Any],
    read_options: Callable[[str], Sequence[dict[str, Any]]] | None,
    deliver: Callable[[str, int], dict[str, Any]] | None,
    interval_s: float,
) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            await _pass(resolve_pending, read_options, deliver)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("auto-answer pass failed")


async def _pass(
    resolve_pending: Callable[[str], Any],
    read_options: Callable[[str], Sequence[dict[str, Any]]] | None,
    deliver: Callable[[str, int], dict[str, Any]] | None,
) -> None:
    """One sweep of every armed chat.

    Each chat is guarded separately. One session whose terminal cannot be read
    must not stop the sweep, or arming a second chat would silently disable the
    first.
    """
    for chat in await db.chats_with_auto_answer():
        try:
            await consider(
                chat,
                resolve_pending=resolve_pending,
                read_options=read_options or (lambda _sid: []),
                deliver=deliver or (lambda _sid, _i: {"ok": False,
                                                      "reason": "no delivery"}),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("auto-answer failed for chat %s", chat.get("id"))
