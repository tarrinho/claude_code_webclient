# db_read_marks.py — Read marks and chat_last_activity.
#
# Extracted from db.py so the orchestrator route path does not need the full
# database module.

import logging
from typing import Any

import db

_log = logging.getLogger("wc.db.read_marks")


async def read_marks_get(owner_id: str) -> dict[tuple[str, str], dict[str, str]]:
    """Return {(kind, ref_id): {"read_at": ..., "dismissed_at": ...}}."""
    cur = await db.db_conn.execute(
        "SELECT kind, ref_id, read_at, dismissed_at FROM read_marks "
        "WHERE owner_id = ?",
        (owner_id,),
    )
    return {
        (r["kind"], r["ref_id"]): {
            "read_at": r["read_at"],
            "dismissed_at": r["dismissed_at"] or "",
        }
        for r in await cur.fetchall()
    }


async def read_mark_set(
    owner_id: str,
    kind: str,
    ref_id: str,
    read_at: str | None = None,
    dismiss: bool = False,
) -> str:
    """Record that *ref_id* has been looked at, and return the timestamp used."""
    stamp = read_at or db._now()
    dismissed = stamp if dismiss else None
    await db.db_conn.execute(
        "INSERT INTO read_marks (owner_id, kind, ref_id, read_at, dismissed_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(owner_id, kind, ref_id) DO UPDATE SET "
        "  read_at = excluded.read_at, "
        "  dismissed_at = COALESCE(excluded.dismissed_at, read_marks.dismissed_at)",
        (owner_id, kind, ref_id, stamp, dismissed),
    )
    await db.db_conn.commit()
    return stamp


async def chat_last_activity(owner_id: str) -> dict[str, dict[str, Any]]:
    """Latest message per chat: {chat_id: {role, created_at, preview, tail}}.

    One grouped query rather than a read per conversation -- the orchestrator
    polls, so this runs repeatedly.
    """
    cur = await db.db_conn.execute(
        "SELECT m.chat_id, m.role, m.created_at, substr(m.content, 1, 200) AS preview, "
        "       substr(m.content, -200) AS tail "
        "FROM messages m "
        "JOIN chats c ON c.id = m.chat_id "
        "JOIN (SELECT chat_id, MAX(id) AS last_id FROM messages GROUP BY chat_id) t "
        "  ON t.chat_id = m.chat_id AND t.last_id = m.id "
        "WHERE c.owner_id = ? AND c.deleted_at IS NULL",
        (owner_id,),
    )
    return {
        r["chat_id"]: {
            "role": r["role"],
            "created_at": r["created_at"],
            "preview": r["preview"],
            "tail": r["tail"],
        }
        for r in await cur.fetchall()
    }
