# routes/db_agent_reply.py — audit trail + cooldown for transcripts.agent_reply_to.
#
# Every relay attempt (local or transport-routed, successful or not) is logged
# here so a human can review what an automated relay sent on their behalf, and
# so a rapid-fire loop at the same target is capped without an in-memory dict
# that would reset on restart. See
# docs/superpowers/specs/2026-09-08-transport-aware-agent-reply-design.md.

from __future__ import annotations

import db

_COOLDOWN_S = 300.0  # 5 minutes -- same window auto_answer.py uses


async def agent_reply_cooldown_check(chat_id: str, target: str) -> bool:
    """True if a relay from *chat_id* to *target* may proceed right now.

    Reads the most recent attempt (regardless of outcome) for this pair --
    a failed attempt still counts, so a broken remote can't be hammered in
    a tight retry loop either.
    """
    cur = await db.db_conn.execute(
        "SELECT created_at FROM agent_reply_log "
        "WHERE chat_id = ? AND target = ? ORDER BY id DESC LIMIT 1",
        (chat_id, target),
    )
    row = await cur.fetchone()
    if not row:
        return True
    import datetime

    try:
        last = datetime.datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return True
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - last.replace(
        tzinfo=datetime.timezone.utc)).total_seconds()
    return elapsed >= _COOLDOWN_S


async def agent_reply_log_add(
    chat_id: str, owner_id: str, target: str, via: str, ok: bool, reason: str = "",
) -> None:
    await db.db_conn.execute(
        "INSERT INTO agent_reply_log "
        "(chat_id, owner_id, target, via, ok, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, owner_id, target, via, 1 if ok else 0, reason, db._now()),
    )
    await db.db_conn.commit()
