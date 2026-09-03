# db_supervisors.py — Supervisor orchestration persistence.
#
# Extracted from db.py so that the supervisor route layer (routes/supervisors.py)
# can import these helpers without pulling the full database module.

import json
from typing import Any

import db


async def supervisor_list(owner_id: str) -> list[dict[str, Any]]:
    """All supervisors for *owner_id*, newest first."""
    cur = await db.db_conn.execute(
        "SELECT id, title, description, config, status, progress_pct, "
        "created_at, updated_at, completed_at "
        "FROM supervisors WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def supervisor_get(supervisor_id: str, owner_id: str) -> dict[str, Any] | None:
    """Fetch one supervisor, owner-scoped."""
    cur = await db.db_conn.execute(
        "SELECT id, title, description, config, status, plan, progress_pct, "
        "created_at, updated_at, completed_at "
        "FROM supervisors WHERE id = ? AND owner_id = ?",
        (supervisor_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def supervisor_create(
    supervisor_id: str,
    title: str,
    description: str | None,
    owner_id: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Create a new supervisor and return its created_at timestamp."""
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO supervisors (id, title, description, config, owner_id, status, "
        "progress_pct, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'idle', 0.0, ?, '')",
        (
            supervisor_id,
            title,
            description,
            json.dumps(config or {}),
            owner_id,
            now,
        ),
    )
    await db.db_conn.commit()
    return now


async def supervisor_update(
    supervisor_id: str,
    owner_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    plan: str | None = None,
    progress_pct: float | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Update supervisor fields; only non-None values are set.  Returns rowcount."""
    pairs: list[tuple[str, Any]] = [
        ("title", title),
        ("description", description),
        ("status", status),
        ("plan", plan),
        ("config", json.dumps(config) if config is not None else None),
    ]
    if progress_pct is not None:
        pairs.append(("progress_pct", float(progress_pct)))
    sets: list[str] = []
    vals: list[Any] = []
    for field, value in pairs:
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(db._now())
    vals.extend([supervisor_id, owner_id])
    sql = (
        "UPDATE supervisors SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?"
    )
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def supervisor_delete(supervisor_id: str, owner_id: str) -> bool:
    """Delete a supervisor and all its tasks/messages.  Returns rowcount."""
    try:
        await db.db_conn.execute("BEGIN")
        await db.db_conn.execute(
            "DELETE FROM supervisor_tasks WHERE supervisor_id = ?",
            (supervisor_id,),
        )
        await db.db_conn.execute(
            "DELETE FROM supervisor_messages WHERE supervisor_id = ?",
            (supervisor_id,),
        )
        cur = await db.db_conn.execute(
            "DELETE FROM supervisors WHERE id = ? AND owner_id = ?",
            (supervisor_id, owner_id),
        )
        await db.db_conn.commit()
        return cur.rowcount > 0
    except Exception:
        await db.db_conn.rollback()
        raise


async def supervisor_tasks_get(
    supervisor_id: str, owner_id: str
) -> list[dict[str, Any]]:
    """All tasks for one owner's supervisor, ordered by creation.

    ``owner_id`` was accepted and never used, so ``GET /api/supervisors/{any
    id}/tasks`` returned any account's task titles, descriptions and results --
    agent output -- to any authenticated caller.  The argument's presence is what
    made it invisible: the handler passes it, so the call reads as scoped.

    Scoped by join rather than by a check in the handler, because the handler is
    where it was already missing.
    """
    cur = await db.db_conn.execute(
        "SELECT t.id, t.supervisor_id, t.title, t.description, t.status, "
        "       t.model, t.result, t.progress_pct, t.parent_task_id, "
        "       t.depends_on, t.created_at, t.updated_at "
        "FROM supervisor_tasks t "
        "JOIN supervisors s ON s.id = t.supervisor_id AND s.owner_id = ? "
        "WHERE t.supervisor_id = ? "
        "ORDER BY t.id ASC",
        (owner_id, supervisor_id),
    )
    return [dict(r) for r in await cur.fetchall()]


async def supervisor_task_create(
    supervisor_id: str,
    task_id: str,
    title: str,
    description: str | None,
    model: str | None = None,
    parent_task_id: str | None = None,
    depends_on: list[str] | None = None,
) -> str:
    """Create a task under a supervisor.  Returns created_at timestamp."""
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO supervisor_tasks "
        "(id, supervisor_id, title, description, status, model, result, "
        "progress_pct, parent_task_id, depends_on, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, '', 0.0, ?, ?, ?, ?)",
        (
            task_id,
            supervisor_id,
            title,
            description,
            model,
            parent_task_id,
            json.dumps(depends_on or []),
            now,
            now,
        ),
    )
    await db.db_conn.commit()
    return now


async def supervisor_task_update(
    supervisor_id: str,
    task_id: str,
    owner_id: str,
    status: str | None = None,
    result: str | None = None,
    progress_pct: float | None = None,
    model: str | None = None,
) -> bool:
    """Update a task's fields.  Returns rowcount."""
    pairs: list[tuple[str, Any]] = [
        ("status", status),
        ("result", result),
        ("model", model),
    ]
    sets: list[str] = []
    vals: list[Any] = []
    for field, value in pairs:
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if progress_pct is not None:
        sets.append("progress_pct = ?")
        vals.append(float(progress_pct))
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(db._now())
    vals.extend([task_id, supervisor_id])
    # Scoped by ownership, not only by ids.  SQLite has no UPDATE ... JOIN, so
    # the check is a subselect.  This is the only write among the five functions
    # that took ``owner_id`` and ignored it, and its callers are all inside the
    # engine today -- but "not currently reachable from a route" is what the two
    # cross-tenant reads were until somebody added a route.
    vals.append(owner_id)
    sql = (
        "UPDATE supervisor_tasks SET " + ", ".join(sets)
        + " WHERE id = ? AND supervisor_id = ? AND supervisor_id IN "
          "(SELECT id FROM supervisors WHERE owner_id = ?)"
    )
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def supervisor_task_get(
    supervisor_id: str, task_id: str, owner_id: str
) -> dict[str, Any] | None:
    """Fetch one task under one owner's supervisor.

    ``owner_id`` was accepted and unused here too.  Its only caller checks
    ownership first, so this was latent rather than reachable -- which is
    exactly the state the two reachable ones were in until a route was added.
    """
    cur = await db.db_conn.execute(
        "SELECT t.id, t.supervisor_id, t.title, t.description, t.status, "
        "       t.model, t.result, t.progress_pct, t.parent_task_id, "
        "       t.depends_on, t.created_at, t.updated_at "
        "FROM supervisor_tasks t "
        "JOIN supervisors s ON s.id = t.supervisor_id AND s.owner_id = ? "
        "WHERE t.id = ? AND t.supervisor_id = ?",
        (owner_id, task_id, supervisor_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def supervisor_messages_append(
    supervisor_id: str, role: str, content: str, metadata: dict[str, Any] | None = None
) -> int:
    """Append a supervisor message.  Returns row id."""
    cur = await db.db_conn.execute(
        "INSERT INTO supervisor_messages "
        "(supervisor_id, role, content, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            supervisor_id,
            role,
            content,
            json.dumps(metadata) if metadata else None,
            db._now(),
        ),
    )
    await db.db_conn.commit()
    return cur.lastrowid


async def supervisor_messages_get(
    supervisor_id: str,
    owner_id: str,
    after_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read one owner's supervisor messages, optionally since a specific id.

    Three things were wrong here and they were wrong in a way that read
    correctly.  ``owner_id`` was accepted and never used, so any authenticated
    account could read any supervisor's conversation by id -- and every caller
    looked scoped, because the argument was right there in the call.  ``after_id``
    was accepted and never used either, so the SSE poller re-sent the same first
    hundred messages for ever.  And the ``if after_id is not None`` branch existed
    with **byte-identical** bodies on both sides, which is what made the whole
    thing look deliberate.

    Owner scoping is done here rather than by the caller on purpose: this is the
    layer that cannot be forgotten.  A handler-level check protects the handlers
    that have one.
    """
    sql = (
        "SELECT m.id, m.role, m.content, m.metadata, m.created_at "
        "FROM supervisor_messages m "
        "JOIN supervisors s ON s.id = m.supervisor_id AND s.owner_id = ? "
        "WHERE m.supervisor_id = ?"
    )
    params: list[Any] = [owner_id, supervisor_id]
    if after_id:
        sql += " AND m.id > ?"
        params.append(after_id)
    sql += " ORDER BY m.id ASC LIMIT ?"
    params.append(limit)
    cur = await db.db_conn.execute(sql, tuple(params))  # nosec B608: static fragments
    return [dict(r) for r in await cur.fetchall()]


async def supervisor_members_list(supervisor_id: str) -> list[dict[str, Any]]:
    """The chats a supervisor watches, newest addition last.

    Joined against ``chats`` so a member whose conversation was deleted simply
    stops appearing.  An INNER JOIN rather than a LEFT one: a supervisor listing
    a conversation that no longer exists is the failure worth preventing, and a
    row with a null title beside a real status reads as a bug.

    Deliberately returns no status.  That comes from the supervisor classifier,
    which already decides working/waiting/failed for every chat and session; a
    second definition here would agree with it only by coincidence, and the two
    would drift the first time either changed.
    """
    cur = await db.db_conn.execute(
        "SELECT m.supervisor_id, m.chat_id, m.added_at, "
        "       c.title, c.session_id, c.work_dir "
        "FROM supervisor_members m "
        "JOIN chats c ON c.id = m.chat_id AND c.deleted_at IS NULL "
        "WHERE m.supervisor_id = ? "
        "ORDER BY m.added_at ASC, m.chat_id ASC",
        (supervisor_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def supervisor_member_add(supervisor_id: str, chat_id: str) -> bool:
    """Add one chat to a supervisor.  True if it was not already a member.

    The caller is responsible for having checked that *chat_id* belongs to the
    requesting owner: this layer stores what it is given, and an unchecked id
    here would pull another account's conversation into the members feed along
    with its title and preview.
    """
    cur = await db.db_conn.execute(
        "INSERT INTO supervisor_members (supervisor_id, chat_id, added_at) "
        "VALUES (?, ?, ?) ON CONFLICT(supervisor_id, chat_id) DO NOTHING",
        (supervisor_id, chat_id, db._now()),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def supervisor_member_remove(supervisor_id: str, chat_id: str) -> bool:
    """Drop a member.  Never deletes the conversation itself.

    A supervisor is a view over work, not its owner -- removing a member must
    leave the conversation exactly as it was.
    """
    cur = await db.db_conn.execute(
        "DELETE FROM supervisor_members WHERE supervisor_id = ? AND chat_id = ?",
        (supervisor_id, chat_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def supervisor_progress(supervisor_id: str, owner_id: str) -> float:
    """Mean task progress for one owner's supervisor, 0.0 when it has no tasks.

    Has no callers at the time of writing.  Kept and scoped rather than deleted
    because the name is one somebody will reach for, and an unscoped query
    sitting under an owner-scoped signature is a trap laid for whoever does.
    One statement instead of two, so the count and the sum cannot be read from
    different states of the table.
    """
    cur = await db.db_conn.execute(
        "SELECT COUNT(*) AS total, SUM(t.progress_pct) AS sum_pct "
        "FROM supervisor_tasks t "
        "JOIN supervisors s ON s.id = t.supervisor_id AND s.owner_id = ? "
        "WHERE t.supervisor_id = ?",
        (owner_id, supervisor_id),
    )
    row = await cur.fetchone()
    total = (row["total"] if row else 0) or 0
    if not total:
        return 0.0
    return round(float(row["sum_pct"] or 0) / total, 1)
