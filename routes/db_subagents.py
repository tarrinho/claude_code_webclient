# db_subagents.py — the chat_subagents table: one row per Task-tool subagent a
# conversation spawned, recorded when routes/chats.py's post-turn scan first
# finds it in the transcript. Extracted from db.py the same way
# routes/db_images.py is, so the turn-completion path does not need the full
# database module.
from __future__ import annotations

from typing import Any

import db


@db.write
async def subagent_record(chat_id: str, rows: list[dict[str, Any]]) -> None:
    """Record subagents discovered for one chat. Idempotent.

    INSERT OR IGNORE on (chat_id, tool_use_id), then an UPDATE for the
    status/ended_at pair. Both halves are needed and neither is sufficient:
    the insert alone could never finish a row, because a Task's `tool_result`
    arrives in a LATER transcript record than its `tool_use`, so the scan that
    first sees a subagent almost always sees it as `running`. The update alone
    would silently drop a subagent nobody had inserted yet.

    Narrowed to the running -> done direction: a row already `done` is never
    reopened, so a re-scan of an older transcript region cannot un-finish work.
    """
    if not rows:
        return
    await db.db_conn.executemany(
        "INSERT OR IGNORE INTO chat_subagents "
        "(chat_id, tool_use_id, agent_type, description, status, "
        " started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(chat_id, r["tool_use_id"], r.get("agent_type"), r.get("description"),
          r.get("status") or "running", r["started_at"], r.get("ended_at"))
         for r in rows],
    )
    await db.db_conn.executemany(
        "UPDATE chat_subagents SET status = ?, ended_at = ? "
        "WHERE chat_id = ? AND tool_use_id = ? AND status != 'done'",
        [(r.get("status") or "running", r.get("ended_at"), chat_id,
          r["tool_use_id"]) for r in rows],
    )
    await db.db_conn.commit()


async def subagents_for_chats(
    chat_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Subagents for *chat_ids*, grouped by chat, oldest first.

    One query for the whole sidebar rather than one per chat: the list endpoint
    renders every conversation, and a query per row is what makes a sidebar
    slow in proportion to how much work you have done.

    Ordered by `started_at` because spawn order is the only order that means
    anything for a subagent -- it has no recency of its own and no manual
    placement.
    """
    if not chat_ids:
        return {}
    marks = ", ".join("?" * len(chat_ids))
    cursor = await db.db_conn.execute(
        "SELECT chat_id, tool_use_id, agent_type, description, status, "
        "       started_at, ended_at "
        f"FROM chat_subagents WHERE chat_id IN ({marks}) "  # nosec B608: parameterised
        "ORDER BY started_at ASC, id ASC",
        tuple(chat_ids),
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in await cursor.fetchall():
        out.setdefault(row["chat_id"], []).append({
            "tool_use_id": row["tool_use_id"],
            "agent_type": row["agent_type"],
            "description": row["description"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        })
    return out
