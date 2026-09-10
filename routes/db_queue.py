# db_queue.py — Queued-prompt persistence (turn_queue table).
#
# Extracted from db.py so that the queue-drain path does not need the full
# database module.

from typing import Any

import db

# Per-conversation cap.  A queue only drains when a turn finishes cleanly, so
# without a ceiling a conversation whose turns keep failing would accumulate
# prompts indefinitely.
QUEUE_MAX: int = 5


async def queue_add(
    chat_id: str, owner_id: str, prompt: str, model: str | None = None
) -> int:
    """Append a prompt.  Returns its 1-based position, or 0 if the queue is full."""
    cur = await db.db_conn.execute(
        "SELECT COUNT(*) AS n FROM turn_queue WHERE chat_id = ? AND owner_id = ?",
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    if (row["n"] if row else 0) >= QUEUE_MAX:
        return 0
    await db.db_conn.execute(
        "INSERT INTO turn_queue (chat_id, owner_id, prompt, model, state, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (chat_id, owner_id, prompt, model, db._now()),
    )
    await db.db_conn.commit()
    return (row["n"] if row else 0) + 1


async def queue_list(chat_id: str, owner_id: str) -> list[dict[str, Any]]:
    """Every queued prompt for a conversation, oldest first."""
    cur = await db.db_conn.execute(
        "SELECT id, prompt, model, state, created_at FROM turn_queue "
        "WHERE chat_id = ? AND owner_id = ? ORDER BY id",
        (chat_id, owner_id),
    )
    return [dict(row) for row in await cur.fetchall()]


async def queue_counts(owner_id: str) -> dict[str, int]:
    """How many prompts each conversation has queued, for the chat list."""
    cur = await db.db_conn.execute(
        "SELECT chat_id, COUNT(*) AS n FROM turn_queue WHERE owner_id = ? "
        "GROUP BY chat_id",
        (owner_id,),
    )
    return {row["chat_id"]: row["n"] for row in await cur.fetchall()}


async def queue_held_counts(owner_id: str) -> dict[str, int]:
    """Held-only counterpart to `queue_counts`.

    The sidebar badge used to render `queue_counts` alone, which sums
    pending and held rows together -- a chat with 3 prompts safely waiting
    to auto-send looked identical to one with 3 held because its last turn
    broke and needs Send/Discard. Queried separately rather than folded into
    `queue_counts` (e.g. a tuple) so a caller that only wants the total,
    like the drain path, is not paying for a state filter it does not need.
    """
    cur = await db.db_conn.execute(
        "SELECT chat_id, COUNT(*) AS n FROM turn_queue WHERE owner_id = ? "
        "AND state = 'held' GROUP BY chat_id",
        (owner_id,),
    )
    return {row["chat_id"]: row["n"] for row in await cur.fetchall()}


async def last_models_used(owner_id: str) -> dict[str, str]:
    """The model that actually served each conversation's most recent turn.

    Deliberately NOT ``chats.model``.  That column is the user's routing
    override -- ``runner.get_default_model`` reads it first, ahead of the
    backend and global defaults -- and writing the served model back into it
    would pin every chat to whatever answered its first turn, silently
    breaking "follow the backend/global default" for every conversation that
    never set one.  ``routes/chats.py``'s two ``finish()`` closures both refuse
    this write, with comments to the same effect; this is the read side that
    makes the refusal not cost the sidebar its "Last model" display.

    ``usage_events`` already carries chat_id/model/created_at per turn, so no
    schema change: the latest ``id`` per chat_id (autoincrement, so a unique
    tiebreak even when two rows share a timestamp) is that conversation's
    last-used model.  chat_id is '' for terminal turns (comment on the column)
    and excluded.
    """
    cur = await db.db_conn.execute(
        "SELECT ue.chat_id, ue.model FROM usage_events ue "
        "JOIN (SELECT chat_id, MAX(id) AS max_id FROM usage_events "
        "      WHERE owner_id = ? AND chat_id != '' GROUP BY chat_id) latest "
        "ON ue.id = latest.max_id",
        (owner_id,),
    )
    return {row["chat_id"]: row["model"] for row in await cur.fetchall()}


async def last_model_used(chat_id: str, owner_id: str) -> str:
    """Single-chat form of `last_models_used`, for GET /api/chats/{id}.

    The list endpoint batches this for every chat in one query; opening one
    chat by id only needs its own row, and running the batched query for a
    single id would scan every conversation's usage to answer about one.
    """
    cur = await db.db_conn.execute(
        "SELECT model FROM usage_events WHERE chat_id = ? AND owner_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    return row["model"] if row else ""


async def queue_next(chat_id: str) -> dict[str, Any] | None:
    """The oldest pending prompt for a conversation, or None.

    Deliberately not owner-scoped: the caller is the turn that just finished,
    which already established ownership, and it holds the owner to pass on.
    """
    cur = await db.db_conn.execute(
        "SELECT id, prompt, model FROM turn_queue "
        "WHERE chat_id = ? AND state = 'pending' ORDER BY id LIMIT 1",
        (chat_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def queue_delete(queue_id: int, owner_id: str) -> bool:
    """Remove one queued prompt.  Returns whether a row was removed."""
    cur = await db.db_conn.execute(
        "DELETE FROM turn_queue WHERE id = ? AND owner_id = ?", (queue_id, owner_id)
    )
    await db.db_conn.commit()
    return bool(cur.rowcount)


async def queue_release(queue_id: int, owner_id: str) -> bool:
    """Return a held prompt to pending, so the next finish will send it."""
    cur = await db.db_conn.execute(
        "UPDATE turn_queue SET state = 'pending' WHERE id = ? AND owner_id = ?",
        (queue_id, owner_id),
    )
    await db.db_conn.commit()
    return bool(cur.rowcount)


async def queue_hold_orphans() -> int:
    """Hold every pending prompt, across all conversations. Returns how many.

    For startup only. The queue is on disk but the turn that would drain it
    lives in memory, so a restart mid-turn leaves `pending` rows with nothing
    left to trigger them: `_drain()` runs when a turn *finishes*, and after a
    restart there is no turn to finish. Reproduced 2026-09-10 in a throwaway
    database -- two pending rows, `running_ids()` empty, `queue_counts()`
    reporting them to the sidebar as "2 queued", and no caller for
    `queue_next()`. They would execute only if the user happened to send
    another message in that same conversation.

    Held rather than launched, deliberately. Firing queued prompts at boot
    would spend a turn each with nobody watching, and it contradicts the rule
    the drain already follows: a prompt is only sent on a clean finish,
    because firing one into a conversation whose state the user has not seen
    is the wrong default. Held rows already have Send and Discard controls, so
    this turns a silent stall into a visible decision.

    This is the same failure shape as the START_TUNNEL command that "simply sat
    in the queue forever" (see the comment in app.py's lifespan): a persisted
    queue whose consumer only ran on an event that a restart had already
    destroyed.
    """
    cur = await db.db_conn.execute(
        "UPDATE turn_queue SET state = 'held' WHERE state = 'pending'"
    )
    await db.db_conn.commit()
    return cur.rowcount or 0


async def queue_hold_all(chat_id: str) -> int:
    """Mark a conversation's pending prompts as held.  Returns how many."""
    cur = await db.db_conn.execute(
        "UPDATE turn_queue SET state = 'held' WHERE chat_id = ? AND state = 'pending'",
        (chat_id,),
    )
    await db.db_conn.commit()
    return cur.rowcount or 0
