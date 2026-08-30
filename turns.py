"""turns.py — a Claude turn owned by the server rather than by the browser.

A turn used to live and die with the HTTP response that carried it. The client
held a ``fetch`` reader; closing it closed the SSE stream, ``claude_proxy`` saw
the disconnect and terminated the CLI, and because the assistant message was
only written when the ``done`` event arrived, the answer was thrown away. The
usage row, by contrast, is recorded as each usage event arrives -- deliberately,
since the tokens are spent either way -- so a disconnect billed the user and
returned nothing.

That is why the UI refused to let you change conversations mid-turn: the guard
was protecting the turn, not the interface.

Here a turn is an ``asyncio.Task`` with a buffer. Clients attach to the buffer
and detach from it freely; nothing they do shortens the turn. Switching
conversations, reloading, locking a phone and closing the tab are all just
followers going away.

Two hooks keep this module free of any import from ``app``, which would be a
cycle: *produce* supplies the event stream (app closes over
``runner.stream_turn``) and *finish* persists the result. ``launcher`` is set
once by app so a queued prompt can be started from here without knowing how one
is built.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import db

_log = logging.getLogger("wc.turns")

# How long a finished turn's buffer is kept. A client that reattaches after the
# turn ended still needs the tail: without this, sending a prompt and switching
# away for a moment would show an empty conversation until the next reload.
_RETAIN_S: Final[float] = 300.0

# Idle gap after which `follow` emits a keep-alive, so an intermediary does not
# drop a stream that is legitimately waiting on a slow model.
_KEEPALIVE_S: Final[float] = 15.0

# Client-facing failure text. Owned here rather than in app.py because the
# failure is now detected inside the task: app.py aliases its _SSE_* constants
# to these so the two halves cannot drift into reporting a timeout as a generic
# internal error, which is exactly what happened when the turn moved off the
# request thread.
TIMEOUT_MESSAGE: Final[str] = "The turn timed out."
INTERNAL_MESSAGE: Final[str] = "An internal error occurred — see server logs."

# Injected by app.py at import time. Takes (chat_id, owner, prompt, model) and
# builds the produce/finish pair before calling `start`, which is knowledge that
# belongs in app.py and must not be duplicated here.
launcher: Callable[[str, str, str, str | None], Awaitable[None]] | None = None

_live: dict[str, LiveTurn] = {}


class AlreadyRunning(Exception):
    """A turn is already live for this conversation."""


@dataclass
class LiveTurn:
    """One turn, its event buffer, and the followers watching it."""

    chat_id: str
    owner: str
    prompt: str
    model: str | None = None
    # Monotonic per turn. Followers resume with ?since=<seq>, so a reattaching
    # client asks for what it has not seen instead of the whole conversation.
    seq: int = 0
    # Never trimmed within a turn, only reaped whole once it finishes. A
    # deliberate trade: mid-turn eviction would create gaps a reattaching client
    # could not detect, and silently missing tokens is worse than the memory.
    # Bounded in practice by the turn timeout and the runner's stream limit.
    events: list[dict[str, Any]] = field(default_factory=list)
    state: str = "running"  # running | done | error | cancelled
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0
    task: asyncio.Task | None = None
    # Broadcast to followers. Replaced on every emit rather than cleared: with a
    # single reused Event, whichever follower ran first would clear it and the
    # rest would sleep through the wakeup.
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def running(self) -> bool:
        return self.state == "running"

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        stamped = {**event, "seq": self.seq}
        self.events.append(stamped)
        previous, self.wakeup = self.wakeup, asyncio.Event()
        previous.set()
        return stamped

    def text(self) -> str:
        """The answer so far, for callers that want the partial reply."""
        return "".join(
            event.get("content") or ""
            for event in self.events
            if event.get("type") == "text"
        )


def _reap(now: float | None = None) -> None:
    """Drop finished turns whose buffer is past its retention window."""
    now = time.monotonic() if now is None else now
    stale = [
        chat_id
        for chat_id, turn in _live.items()
        if not turn.running and turn.finished_at and now - turn.finished_at > _RETAIN_S
    ]
    for chat_id in stale:
        _live.pop(chat_id, None)


def get(chat_id: str) -> LiveTurn | None:
    return _live.get(chat_id)


def is_running(chat_id: str) -> bool:
    turn = _live.get(chat_id)
    return bool(turn and turn.running)


def running_ids(owner: str) -> set[str]:
    """Conversations with a live turn, for the sidebar indicator."""
    return {
        chat_id
        for chat_id, turn in _live.items()
        if turn.running and turn.owner == owner
    }


def start(
    chat_id: str,
    owner: str,
    prompt: str,
    model: str | None,
    produce: Callable[[], AsyncGenerator[dict[str, Any], None]],
    finish: Callable[..., Awaitable[None]],
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> LiveTurn:
    """Begin a turn in the background. Raises AlreadyRunning if one is live.

    *on_event* is awaited for each event as it arrives, which is where usage is
    recorded: that has to happen in the task rather than in a follower, because
    the tokens are spent whether or not anyone is still watching.
    """
    if is_running(chat_id):
        raise AlreadyRunning(chat_id)
    _reap()
    turn = LiveTurn(chat_id=chat_id, owner=owner, prompt=prompt, model=model)
    _live[chat_id] = turn
    turn.task = asyncio.create_task(
        _run(turn, produce, finish, on_event), name=f"turn:{chat_id}"
    )
    return turn


async def _run(
    turn: LiveTurn,
    produce: Callable[[], AsyncGenerator[dict[str, Any], None]],
    finish: Callable[..., Awaitable[None]],
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None,
) -> None:
    parts: list[str] = []
    session_id: str | None = None
    model = ""
    failed = False
    saw_done = False
    cancelled = False
    # The turn stays "running" for followers until the answer is on disk -- see
    # the settle step below for why the order matters.
    final_state = "done"
    try:
        async for event in produce():
            kind = event.get("type")
            if kind == "text":
                parts.append(event.get("content") or "")
            elif kind == "session_id":
                session_id = event.get("session_id") or session_id
            elif kind == "model":
                model = event.get("model") or model
            elif kind == "error":
                failed = True
                turn.error = event.get("error") or "The turn failed."
            if on_event is not None:
                try:
                    await on_event(event)
                except Exception:  # noqa: BLE001 -- accounting must not kill a turn
                    _log.exception("turn_on_event_failed chat_id=%s", turn.chat_id)
            if kind == "done" and failed:
                # An error frame already went out. The inline loop this replaced
                # broke here without relaying `done`, and clients still rely on
                # that: a turn reports either an answer or a failure, not both.
                saw_done = True
                break
            turn.emit(event)
            if kind == "done":
                saw_done = True
                break
        if failed:
            final_state = "error"
        elif not saw_done:
            # The producer ran out without ever saying it was finished. The
            # inline loop reported that as an error and it still should: a
            # silently truncated answer must not be stored as a whole one.
            final_state = "error"
            turn.error = "Stream ended before completion"
            turn.emit({"type": "error", "error": turn.error})
    except asyncio.CancelledError:
        cancelled = True
        final_state = "cancelled"
        turn.error = "The turn was stopped."
        turn.emit({"type": "error", "error": turn.error})
        # Uncancelled so the persistence below can await. Without this the very
        # next await re-raises and the partial answer is dropped -- while
        # on_event has already recorded its usage, which is precisely the
        # "billed and nothing returned" shape this module exists to remove.
        # The CancelledError is re-raised after settling.
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
    except (asyncio.TimeoutError, TimeoutError):
        # Distinguished from a crash so the user is told the turn ran out of
        # time rather than that something broke -- those call for different
        # responses, and one is not a bug report.
        final_state = "error"
        turn.error = TIMEOUT_MESSAGE
        _log.warning("turn_timed_out chat_id=%s", turn.chat_id)
        turn.emit({"type": "error", "error": turn.error})
    except Exception as exc:  # noqa: BLE001 -- a broken turn must still settle
        final_state = "error"
        turn.error = INTERNAL_MESSAGE
        _log.exception("turn_crashed chat_id=%s: %s", turn.chat_id, exc)
        turn.emit({"type": "error", "error": turn.error})

    # Persist BEFORE settling. A follower exits as soon as the turn stops being
    # "running", and the first thing a client does on `done` is reload the
    # conversation -- so publishing the terminal state before the write lands
    # means a race where the answer it was just shown is not yet stored.
    try:
        await finish(
            parts=parts,
            session_id=session_id,
            model=model,
            failed=final_state != "done",
            cancelled=cancelled,
        )
    except Exception:  # noqa: BLE001 -- report, but never lose turn state
        _log.exception("turn_finish_failed chat_id=%s", turn.chat_id)

    turn.state = final_state
    turn.finished_at = time.monotonic()
    # A follower waiting on the previous event would otherwise sleep until its
    # keep-alive fires before noticing the turn had settled.
    previous, turn.wakeup = turn.wakeup, asyncio.Event()
    previous.set()

    if cancelled:
        # Re-raised after settling so the task reports as cancelled, but only
        # once followers have been released and nothing is half-written.
        raise asyncio.CancelledError

    try:
        await _drain(turn)
    except Exception:  # noqa: BLE001 -- the queue must not sink a finished turn
        _log.exception("turn_drain_failed chat_id=%s", turn.chat_id)


async def _drain(turn: LiveTurn) -> None:
    """Start the next queued prompt, or hold the queue if this turn failed.

    A queued prompt is only sent on a clean finish. Firing one into a
    conversation whose previous turn just broke would compound a failure the
    user has not seen yet, so the rest of the queue is held and surfaced for an
    explicit decision instead.
    """
    if turn.state != "done":
        held = await db.queue_hold_all(turn.chat_id)
        if held:
            _log.info(
                "turn_queue_held chat_id=%s items=%d reason=%s",
                turn.chat_id, held, turn.state,
            )
        return
    if launcher is None:
        return
    row = await db.queue_next(turn.chat_id)
    if row is None:
        return
    await db.queue_delete(row["id"], turn.owner)
    _log.info("turn_queue_drain chat_id=%s queue_id=%s", turn.chat_id, row["id"])
    try:
        await launcher(turn.chat_id, turn.owner, row["prompt"], row["model"])
    except AlreadyRunning:
        # Something started a turn between the finish and here. Put it back
        # rather than dropping the user's prompt on the floor.
        await db.queue_add(turn.chat_id, turn.owner, row["prompt"], row["model"])
    except Exception:  # noqa: BLE001
        _log.exception("turn_queue_launch_failed chat_id=%s", turn.chat_id)


async def follow(
    chat_id: str, since: int = 0, keepalive: float = _KEEPALIVE_S
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield a turn's events after *since*, then follow it to completion.

    Attaching is cheap and repeatable: everything buffered is replayed first, so
    a client that reconnects mid-turn sees the answer built so far before the
    live tail. Yields ``{"type": "keepalive"}`` while idle; those carry no seq
    and are not buffered.
    """
    turn = _live.get(chat_id)
    if turn is None:
        # "Nothing is running" and "there was a turn but its buffer has been
        # reaped" are indistinguishable from here, and the client needs to tell
        # them apart: the first means settle the UI, the second means reload the
        # conversation because the answer is on disk and not on screen.
        if since:
            yield {"type": "gone"}
        return
    cursor = since
    while True:
        # Captured before reading the buffer. The other order loses an event
        # emitted between the read and the await, and the follower then sleeps
        # until its keep-alive fires.
        waiter = turn.wakeup
        for event in [e for e in turn.events if e["seq"] > cursor]:
            cursor = event["seq"]
            yield event
        if not turn.running and cursor >= turn.seq:
            return
        try:
            await asyncio.wait_for(waiter.wait(), timeout=keepalive)
        except (asyncio.TimeoutError, TimeoutError):
            yield {"type": "keepalive"}


async def cancel(chat_id: str) -> bool:
    """Stop a running turn. Returns whether there was one to stop."""
    turn = _live.get(chat_id)
    if turn is None or not turn.running or turn.task is None:
        return False
    turn.task.cancel()
    try:
        await turn.task
    except asyncio.CancelledError:
        # Expected: we asked for it. The task's own handler has already marked
        # the turn cancelled and emitted the error frame to its followers.
        pass
    except Exception:  # noqa: BLE001 -- a stop must succeed even on a bad turn
        _log.exception("turn_cancel_raised chat_id=%s", chat_id)
    return True


async def shutdown() -> None:
    """Cancel every live turn. Called from the app's lifespan.

    Without this a restart leaves orphaned tasks mid-write, and the sidebar
    would show a dot for a turn whose process is already gone.
    """
    tasks = [turn.task for turn in _live.values() if turn.running and turn.task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        _log.info("cancelled %d live turn(s) on shutdown", len(tasks))
    _live.clear()
