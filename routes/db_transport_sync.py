# routes/db_transport_sync.py — approval queue + audit trail for transport_sync.py.
#
# Every sync (human-clicked or agent-requested) is one row here, unified
# rather than split by trigger -- the human-triggered path inserts at
# status='approved' and resolves immediately; the agent-triggered path
# inserts at 'pending' and waits for a human to approve or reject. See
# docs/superpowers/specs/2026-09-09-transport-project-sync-design.md.

from __future__ import annotations

from typing import Any

import db

_COLUMNS = (
    "id, transport_id, owner_id, requested_by, status, files_changed, "
    "reason, created_at, resolved_at"
)


async def sync_request_create(
    transport_id: str, owner_id: str, requested_by: str, status: str = "pending",
) -> int:
    now = db._now()
    cur = await db.db_conn.execute(
        "INSERT INTO transport_sync_requests "
        "(transport_id, owner_id, requested_by, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (transport_id, owner_id, requested_by, status, now),
    )
    await db.db_conn.commit()
    return cur.lastrowid


async def sync_request_get(request_id: int, owner_id: str) -> dict[str, Any] | None:
    cur = await db.db_conn.execute(
        f"SELECT {_COLUMNS} FROM transport_sync_requests "  # nosec B608: columns are static
        "WHERE id = ? AND owner_id = ?",
        (request_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def sync_request_list_pending(owner_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        f"SELECT {_COLUMNS} FROM transport_sync_requests "  # nosec B608: columns are static
        "WHERE owner_id = ? AND status = 'pending' ORDER BY id ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def sync_request_resolve(
    request_id: int, status: str, files_changed: int | None = None, reason: str = "",
) -> None:
    await db.db_conn.execute(
        "UPDATE transport_sync_requests "
        "SET status = ?, files_changed = ?, reason = ?, resolved_at = ? "
        "WHERE id = ?",
        (status, files_changed, reason, db._now(), request_id),
    )
    await db.db_conn.commit()
