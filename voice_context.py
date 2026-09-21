"""Context for a voice session: a summary of the chat it was opened from,
and a tool to read that chat's messages verbatim.

Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md

Why this is its own module rather than more of `routes/voice.py`: that file is
543 lines and is the one file CLAUDE.md singles out for care, being the single
deliberate exception to "every model call goes through the Claude Code CLI".
This module hands it two finished things -- a context string and a bound tool
-- and `voice.py`'s own responsibilities do not change.

Why summarisation runs on the CLI transport while spoken turns do not: the
exception in CLAUDE.md §0 is argued on latency grounds for a *spoken* turn.
Summarising happens once, at session open, before anyone is listening, so it
has no claim on that argument. It costs nothing either, because the
comprehension ladder's own latencies were measured through the CLI
(`bin/wc-bench.py` reports transport `cli`). Running it here also keeps the
spend inside usage accounting, which CLAUDE.md §5 requires of every caller and
which a direct API call from `voice.py` would silently skip.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

#: Spec §3. Both bounds apply, oldest dropped first. The message cap covers the
#: median chat (22 messages) whole; the character cap is NOT redundant with it,
#: because one message in this database is 256,560 characters and a
#: hundred-message window is unbounded without it.
WINDOW_MAX_MESSAGES = 100
WINDOW_MAX_CHARS = 40_000

#: Spec §4. A range can legally name 15,175 messages, so the tool's result is
#: bounded by the same pair and reports where it stopped.
FETCH_MAX_MESSAGES = 200
FETCH_MAX_CHARS = 40_000

#: Spec §3. Total across every attempt, measured from the first one's start.
SUMMARY_BUDGET_S = 15.0

#: Spec §3: a rung that answers "N/A" or "" has failed and must escalate,
#: rather than have that stored as the session's whole understanding of the
#: conversation.
MIN_SUMMARY_CHARS = 20

#: Spec §1.1. The comprehension ladder is luna -> sonnet-5 -> opus-5, but luna
#: is measured at 58.3% accuracy and 9.2s median. Walking it from luna means a
#: failure at 9.2s leaves 5.8s of the 15s budget, and sonnet needs 6.0s -- so
#: the second rung is cancelled 0.2s short and the session opens with no
#: summary, on most sessions, by default. Skipping rungs measured below this
#: threshold is what makes the budget satisfiable.
MIN_RUNG_ACCURACY = 0.75


def select_window(
    messages: list[dict[str, Any]],
    max_messages: int = WINDOW_MAX_MESSAGES,
    max_chars: int = WINDOW_MAX_CHARS,
) -> tuple[list[dict[str, Any]], bool]:
    """The tail of *messages* that fits both bounds, and whether anything was
    dropped.

    Returns (window, truncated). `truncated` is True when any message was left
    out for either reason, and the caller tells the model so -- a summary built
    from a tail that the model believes is the whole conversation is worse than
    one it knows is partial.

    Oldest-first dropping, because the recent end is the part a voice session
    is most likely to be about.
    """
    if not messages:
        return [], False
    tail = messages[-max_messages:]
    truncated = len(tail) < len(messages)
    # Walk backwards accumulating until the character bound is reached, so the
    # newest messages survive a long one near the start of the window.
    picked: list[dict[str, Any]] = []
    total = 0
    for message in reversed(tail):
        size = len(str(message.get("content") or ""))
        if picked and total + size > max_chars:
            truncated = True
            break
        picked.append(message)
        total += size
        if total >= max_chars:
            # Kept this one (it may be the only one), but nothing older fits.
            if len(picked) < len(tail):
                truncated = True
            break
    picked.reverse()
    return picked, truncated


def is_usable_summary(text: str | None) -> bool:
    """Spec §3: fewer than 20 non-whitespace characters is a failed rung."""
    if not text:
        return False
    return len("".join(text.split())) >= MIN_SUMMARY_CHARS


def eligible_rungs(
    ladder: Iterable[str],
    accuracy_by_model: dict[str, float | None],
    min_accuracy: float = MIN_RUNG_ACCURACY,
) -> list[str]:
    """The ladder with rungs too inaccurate to be worth their latency removed.

    See MIN_RUNG_ACCURACY. A model with no measured accuracy is kept rather
    than dropped: unmeasured is not the same as bad, and dropping it here would
    silently narrow the ladder on missing data -- the same conflation
    `is_ladder_eligible` exists to avoid on the routing side.

    Never returns empty while the ladder is non-empty: if every rung is below
    the threshold the whole ladder is returned unchanged, because walking a
    weak ladder beats refusing to try.
    """
    rungs = [m for m in ladder if m]
    if not rungs:
        return []
    kept = [
        m for m in rungs
        if accuracy_by_model.get(m) is None or accuracy_by_model[m] >= min_accuracy
    ]
    return kept or rungs


def summary_prompt(window: list[dict[str, Any]], truncated: bool) -> str:
    """The summarisation request. Plain text, no tool use, no preamble."""
    lines = [
        "Summarise the conversation below for someone who is about to "
        "continue it by voice and has not read it.",
        "",
        "Cover: what is being worked on, what has been decided, what is "
        "still open, and any names, paths or numbers that would be needed to "
        "carry on. Be specific -- a summary that says 'various changes were "
        "discussed' is useless to the reader.",
        "",
        "Write prose, under 200 words. Do not use headings or bullet points: "
        "this is read aloud context, not a document. Do not address the "
        "reader, and do not mention that you are summarising.",
        "",
    ]
    if truncated:
        lines.append(
            "NOTE: this is only the most recent part of a longer "
            "conversation. Say so if the beginning matters.")
        lines.append("")
    lines.append("--- conversation ---")
    for message in window:
        role = str(message.get("role") or "?")
        content = str(message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def make_fetch_tool(chat_id: str, read_messages: Callable[[str, int, int], list[dict]]):
    """A message-fetching tool hard-bound to one chat.

    `chat_id` is captured here and the returned callable takes only an id
    range, so there is no parameter through which another conversation could
    be addressed. The guarantee is structural rather than validated: the model
    cannot express the request that would read someone else's chat.

    Discarded when the session ends. Nothing registers it, so there is nothing
    to leak.
    """

    def fetch_messages(from_id: int, to_id: int) -> dict[str, Any]:
        """Return the bound chat's messages with ids in [from_id, to_id]."""
        try:
            low, high = int(from_id), int(to_id)
        except (TypeError, ValueError):
            return {"messages": [], "truncated": False,
                    "note": "from_id and to_id must be whole numbers"}
        if low > high:
            low, high = high, low
        rows = read_messages(chat_id, low, high)
        out: list[dict[str, Any]] = []
        total = 0
        truncated = False
        last_id: int | None = None
        for row in rows:
            content = str(row.get("content") or "")
            if out and (len(out) >= FETCH_MAX_MESSAGES
                        or total + len(content) > FETCH_MAX_CHARS):
                truncated = True
                break
            out.append({"id": row.get("id"), "role": row.get("role"),
                        "content": content})
            total += len(content)
            last_id = row.get("id")
        result: dict[str, Any] = {"messages": out, "truncated": truncated}
        if truncated:
            # Naming where it stopped is what lets the model ask for the next
            # span instead of believing it received the whole range.
            result["note"] = (
                f"Truncated after id {last_id}. Request from {last_id} onward "
                f"for the rest.")
        elif not out:
            # An empty range is a true answer, not an error.
            result["note"] = "No messages in that range."
        return result

    return fetch_messages


#: The schema handed to the model. No chat parameter, by construction.
FETCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_messages",
        "description": (
            "Read messages from this conversation's history, verbatim, by id "
            "range. Use this instead of guessing whenever you are unsure of a "
            "specific detail such as a number, a name, a path or a decision. "
            "The summary you were given covers only the most recent part of "
            "the conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from_id": {"type": "integer",
                            "description": "First message id, inclusive."},
                "to_id": {"type": "integer",
                          "description": "Last message id, inclusive."},
            },
            "required": ["from_id", "to_id"],
        },
    },
}


#: Status events the walk emits, in the order a successful open sees them.
#: The status line (spec §5) renders these; they are the only thing the user
#: sees of any of this, which is why the walk reports every attempt rather
#: than only its outcome.
STATUS_INITIALISING = "initialising"
STATUS_SUMMARISING = "summarising"
STATUS_FAILED = "failed"
STATUS_ESCALATING = "escalating"
STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"


async def walk_summary_ladder(
    *,
    rungs: list[str],
    run_rung,
    clock: "BudgetClock",
    expected_s: Callable[[str], float],
    emit,
) -> str | None:
    """Try each rung until one returns a usable summary. Return it, or None.

    None means the session opens degraded (spec §2): with no summary and a
    status line that says so. It is a normal outcome here, not an error --
    measured against this deployment's ladder it is expected on a real share
    of sessions, and the fetch tool is what makes it survivable.

    `run_rung(model) -> str | None` performs one attempt; anything it raises
    is a failed rung rather than a failed walk, because one model being
    unreachable must not deny the user a session.

    `expected_s(model)` is that rung's measured median latency, used to refuse
    a rung that cannot finish inside the remaining budget. Starting one anyway
    is the difference between opening degraded at 9s and opening degraded at
    15s having spent the gap on a call that was always going to be cancelled.

    `emit(state, model)` reports progress. Awaited if it returns an awaitable,
    so the caller can push straight onto an SSE stream.
    """
    async def _emit(state: str, model: str | None = None) -> None:
        result = emit(state, model)
        if hasattr(result, "__await__"):
            await result

    for index, model in enumerate(rungs):
        if clock.expired():
            break
        if not clock.allows(expected_s(model)):
            # Not attempted, and said so: a rung silently skipped for time
            # looks identical to one that was never in the ladder.
            await _emit(STATUS_FAILED, model)
            continue
        await _emit(STATUS_SUMMARISING, model)
        try:
            text = await run_rung(model)
        except Exception:
            text = None
        if is_usable_summary(text):
            return text
        await _emit(STATUS_FAILED, model)
        if index + 1 < len(rungs) and not clock.expired():
            await _emit(STATUS_ESCALATING, rungs[index + 1])
    return None


class BudgetClock:
    """Spec §3's total budget across all attempts.

    Separate from the walk so the walk can be tested without sleeping, and so
    "is there time for another rung" is one decision in one place rather than
    a subtraction repeated at each call site.
    """

    def __init__(self, budget_s: float = SUMMARY_BUDGET_S,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._budget = budget_s
        self._start = now()

    def elapsed(self) -> float:
        return self._now() - self._start

    def remaining(self) -> float:
        return max(0.0, self._budget - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def allows(self, expected_s: float) -> bool:
        """Whether a rung expected to take *expected_s* should be started.

        Refusing to start a rung that cannot finish is the difference between
        opening degraded at 9s and opening degraded at 15s having burned the
        difference on a call that was always going to be cancelled.
        """
        return self.remaining() >= expected_s
