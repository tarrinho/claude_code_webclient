# db_images.py — the generated_images table: one row per image a turn
# produced, recorded when routes/chats.py's _new_workspace_images() first
# finds it. Extracted from db.py the same way routes/db_queue.py and
# routes/db_chats.py are, so the turn-completion path does not need the
# full database module.
from __future__ import annotations

from pathlib import Path
from typing import Any

import db


async def generated_image_record(
    chat_id: str, chat_title: str, work_dir: str, owner_id: str, paths: list[str],
) -> None:
    """Record newly-discovered images for one turn.

    INSERT OR IGNORE against the (chat_id, path) unique index: the same
    image reported twice (the turn-completion path re-running for any
    reason) is a no-op, not a duplicate row.
    """
    if not paths:
        return
    now = db._now()
    await db.db_conn.executemany(
        "INSERT OR IGNORE INTO generated_images "
        "(chat_id, chat_title, work_dir, owner_id, path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(chat_id, chat_title, work_dir, owner_id, path, now) for path in paths],
    )
    await db.db_conn.commit()


def _resolved_inside(work_dir: str, path: str) -> Path | None:
    """Resolve *path* against *work_dir* and return it only if it stays
    inside -- the same resolve-then-is_relative_to containment check
    routes/images.py's handle_image_file already applies on the serve
    path. Used before both reading (_file_exists) and the destructive
    unlink in generated_image_delete, so a row with a traversal-shaped
    path can neither be reported as existing nor be used to delete a
    file outside the workspace."""
    root = Path(work_dir).resolve()
    try:
        candidate = (root / path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_relative_to(root) else None


def _file_exists(row: dict[str, Any]) -> bool:
    """Self-healing check: a row whose file is gone (deleted outside this
    feature, or a workspace removed by hand) is skipped on read rather than
    requiring a reconciliation job to keep the table honest."""
    resolved = _resolved_inside(row["work_dir"], row["path"])
    return resolved is not None and resolved.is_file()


async def generated_images_list(
    owner_id: str, limit: int = 60, before_id: int | None = None,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """One page of an owner's generated images, newest first.

    Mirrors routes/db_chats.py's messages_page: fetch limit+1 so "more
    remain" is a fact about this page, not a guess from limit alone. Rows
    whose file no longer exists are filtered out and do not count against
    the page or the has_more calculation from the caller's point of view.

    Returns (visible_rows, has_more, next_before_id). next_before_id is the
    lowest id seen in the *raw* page, before the self-healing filter --
    using the filtered rows' own ids for the cursor would stall forever if
    an entire page's files were gone (the exact case self-healing exists
    to handle): the client would receive an empty page with has_more still
    true, and have no id left to page past. next_before_id is always a
    real, advancing value as long as the raw page was non-empty.
    """
    if before_id is None:
        cur = await db.db_conn.execute(
            "SELECT id, chat_id, chat_title, work_dir, owner_id, path, created_at "
            "FROM generated_images WHERE owner_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (owner_id, limit + 1),
        )
    else:
        cur = await db.db_conn.execute(
            "SELECT id, chat_id, chat_title, work_dir, owner_id, path, created_at "
            "FROM generated_images WHERE owner_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT ?",
            (owner_id, before_id, limit + 1),
        )
    raw_rows = [dict(r) for r in await cur.fetchall()]
    has_more = len(raw_rows) > limit
    raw_page = raw_rows[:limit]
    next_before_id = raw_page[-1]["id"] if raw_page else None
    rows = [r for r in raw_page if _file_exists(r)]
    return rows, has_more, next_before_id


async def generated_image_get(image_id: int, owner_id: str) -> dict[str, Any] | None:
    """One row, owner-checked. None if it does not exist, belongs to
    someone else, or its file is gone -- all three read as "not found" to
    the caller, which is what makes the API route's 404 (never 403)
    correct without the route needing to tell those cases apart itself."""
    cur = await db.db_conn.execute(
        "SELECT id, chat_id, chat_title, work_dir, owner_id, path, created_at "
        "FROM generated_images WHERE id = ? AND owner_id = ?",
        (image_id, owner_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    row = dict(row)
    return row if _file_exists(row) else None


async def generated_image_delete(image_id: int, owner_id: str) -> bool:
    """Delete the file (if present) then the row. Returns whether a row
    was removed. A missing file is not an error -- it means there is only
    the row left to clean up, which this still does."""
    cur = await db.db_conn.execute(
        "SELECT work_dir, path FROM generated_images WHERE id = ? AND owner_id = ?",
        (image_id, owner_id),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    resolved = _resolved_inside(row["work_dir"], row["path"])
    if resolved is not None:
        try:
            resolved.unlink(missing_ok=True)
        except OSError:
            pass
    cur = await db.db_conn.execute(
        "DELETE FROM generated_images WHERE id = ? AND owner_id = ?",
        (image_id, owner_id),
    )
    await db.db_conn.commit()
    return bool(cur.rowcount)
