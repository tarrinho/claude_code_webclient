# db_supervisors.py — Orchestrator orchestration persistence.
#
# Extracted from db.py so that the orchestrator route layer (routes/orchestrators.py)
# can import these helpers without pulling the full database module.

import json
import logging
from typing import Any

import db

_log = logging.getLogger("wc.db.orchestrators")


async def orchestrator_list(owner_id: str) -> list[dict[str, Any]]:
    """All orchestrators for *owner_id*, newest first."""
    cur = await db.db_conn.execute(
        "SELECT id, title, description, config, status, progress_pct, "
        "created_at, updated_at, completed_at, degraded, degraded_reason "
        "FROM orchestrators WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def orchestrator_set_planner_chat(
    orchestrator_id: str, chat_id: str
) -> None:
    """Record which synthetic chat id the planning turn ran under.

    Never raises. This is accounting, and a run must not fail because its
    bookkeeping did -- the same rule ``orchestrator_mark_degraded`` and
    ``db.usage_record`` both state for themselves. The consequence of losing
    it is a run whose planning turn is missing from its cost, which is
    reported as a partial figure rather than passed off as a total.
    """
    try:
        await db.db_conn.execute(
            "UPDATE orchestrators SET planner_chat_id = ? WHERE id = ?",
            (chat_id, orchestrator_id),
        )
        await db.db_conn.commit()
    except Exception:
        _log.exception(
            "orchestrator_set_planner_chat failed orchestrator_id=%s",
            orchestrator_id,
        )


async def orchestrator_cost(
    orchestrator_id: str, owner_id: str
) -> dict[str, Any]:
    """What one orchestrator run has spent, from the usage rows it wrote.

    The engine already records usage for every turn it spends, with
    ``origin="orchestrator"`` and the synthetic chat id as the key -- but
    nothing read it back per run, so the page that shows a orchestrator's
    progress could not say what that progress had cost. The rows were there
    the whole time.

    Two id shapes are summed: ``subtask_<task id>`` for each task in this
    run, and the planner's own id from ``planner_chat_id``. They are gathered
    in Python and passed as parameters rather than matched with a LIKE: a
    task id is user-visible text, and ``LIKE 'subtask_%'`` would also match
    another run's tasks, since the ids are globally unique but carry no
    orchestrator in them.

    ``cost_usd`` sums only rows whose provider is ``through_claude_code``,
    matching how routes/misc.py blanks cost everywhere else it is shown: the
    figure is priced with Anthropic rates and means nothing for a gateway
    backend. When rows were left out, ``cost_partial`` says so and
    ``cost_note`` says why -- a total that silently omits half a run's turns
    is worse than one labelled incomplete. Tokens are summed over every row,
    because those are counts and are meaningful whatever served them.
    """
    # Three scopes, and it is worth being precise about which one carries the
    # weight: the usage query below filters on owner_id, and
    # orchestrator_tasks_get is scoped by join, so either alone already keeps
    # one account's spend out of another's figure. This call is the third, and
    # it is defence in depth rather than the load-bearing one -- a mutation
    # that unscopes it changes no result, because there is no arrangement of
    # rows where the other two both pass and this one would have refused. It
    # stays because the alternative is a function that reads any account's
    # orchestrator row to decide what to sum.
    orchestrator = await orchestrator_get(orchestrator_id, owner_id)
    if not orchestrator:
        return _empty_cost()
    tasks = await orchestrator_tasks_get(orchestrator_id, owner_id)
    chat_ids = [f"subtask_{t['id']}" for t in tasks if t.get("id")]
    planner = orchestrator.get("planner_chat_id")
    if planner:
        chat_ids.append(planner)
    if not chat_ids:
        return _empty_cost(planner_missing=not planner)

    placeholders = ",".join("?" for _ in chat_ids)
    cur = await db.db_conn.execute(
        "SELECT provider, "  # nosec B608: placeholders are generated, not input
        "       SUM(input_tokens) AS input_tokens, "
        "       SUM(output_tokens) AS output_tokens, "
        "       SUM(cache_read_tokens) AS cache_read_tokens, "
        "       SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "       SUM(COALESCE(cost_usd, 0)) AS cost_usd, "
        "       COUNT(*) AS rows_n, "
        "       SUM(is_error) AS errors "
        "FROM usage_events "
        f"WHERE owner_id = ? AND chat_id IN ({placeholders}) "
        "GROUP BY provider",
        (owner_id, *chat_ids),
    )
    result = _empty_cost(planner_missing=not planner)
    for row in await cur.fetchall():
        result["input_tokens"] += row["input_tokens"] or 0
        result["output_tokens"] += row["output_tokens"] or 0
        result["cache_read_tokens"] += row["cache_read_tokens"] or 0
        result["cache_creation_tokens"] += row["cache_creation_tokens"] or 0
        result["turns"] += row["rows_n"] or 0
        result["errors"] += row["errors"] or 0
        if row["provider"] == "through_claude_code":
            result["cost_usd"] = (result["cost_usd"] or 0) + (row["cost_usd"] or 0)
        else:
            result["cost_partial"] = True
    if result["cost_partial"]:
        result["cost_note"] = (
            "Some turns ran on a backend where the reported cost is not "
            "meaningful, and are counted in the tokens but not the cost."
        )
    elif result["planner_missing"] and result["turns"]:
        result["cost_partial"] = True
        result["cost_note"] = (
            "The planning turn could not be attributed to this run, so its "
            "tokens and cost are not included."
        )
    return result


def _empty_cost(planner_missing: bool = False) -> dict[str, Any]:
    """A run that has spent nothing yet.

    ``cost_usd`` is 0.0 rather than None: a orchestrator with no turns has
    provably spent nothing, which is a different statement from "we cannot
    say", and the UI renders the two differently.
    """
    return {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "turns": 0,
        "errors": 0,
        "cost_partial": False,
        "cost_note": "",
        "planner_missing": planner_missing,
    }


async def orchestrator_get(orchestrator_id: str, owner_id: str) -> dict[str, Any] | None:
    """Fetch one orchestrator, owner-scoped."""
    cur = await db.db_conn.execute(
        "SELECT id, title, description, config, status, plan, progress_pct, "
        "created_at, updated_at, completed_at, degraded, degraded_reason, "
        "planner_chat_id "
        "FROM orchestrators WHERE id = ? AND owner_id = ?",
        (orchestrator_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def orchestrator_create(
    orchestrator_id: str,
    title: str,
    description: str | None,
    owner_id: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Create a new orchestrator and return its created_at timestamp."""
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO orchestrators (id, title, description, config, owner_id, status, "
        "progress_pct, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'idle', 0.0, ?, '')",
        (
            orchestrator_id,
            title,
            description,
            json.dumps(config or {}),
            owner_id,
            now,
        ),
    )
    await db.db_conn.commit()
    return now


async def orchestrator_update(
    orchestrator_id: str,
    owner_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    plan: str | None = None,
    progress_pct: float | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Update orchestrator fields; only non-None values are set.  Returns rowcount."""
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
    vals.extend([orchestrator_id, owner_id])
    sql = (
        "UPDATE orchestrators SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?"
    )
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def orchestrator_delete(orchestrator_id: str, owner_id: str) -> bool:
    """Delete a orchestrator and all its tasks/messages.  Returns rowcount."""
    try:
        await db.db_conn.execute("BEGIN")
        await db.db_conn.execute(
            "DELETE FROM orchestrator_tasks WHERE orchestrator_id = ?",
            (orchestrator_id,),
        )
        await db.db_conn.execute(
            "DELETE FROM orchestrator_messages WHERE orchestrator_id = ?",
            (orchestrator_id,),
        )
        cur = await db.db_conn.execute(
            "DELETE FROM orchestrators WHERE id = ? AND owner_id = ?",
            (orchestrator_id, owner_id),
        )
        await db.db_conn.commit()
        return cur.rowcount > 0
    except Exception:
        await db.db_conn.rollback()
        raise


async def orchestrator_tasks_get(
    orchestrator_id: str, owner_id: str
) -> list[dict[str, Any]]:
    """All tasks for one owner's orchestrator, ordered by creation.

    ``owner_id`` was accepted and never used, so ``GET /api/orchestrators/{any
    id}/tasks`` returned any account's task titles, descriptions and results --
    agent output -- to any authenticated caller.  The argument's presence is what
    made it invisible: the handler passes it, so the call reads as scoped.

    Scoped by join rather than by a check in the handler, because the handler is
    where it was already missing.
    """
    cur = await db.db_conn.execute(
        "SELECT t.id, t.orchestrator_id, t.title, t.description, t.status, "
        "       t.model, t.result, t.progress_pct, t.parent_task_id, "
        "       t.depends_on, t.created_at, t.updated_at "
        "FROM orchestrator_tasks t "
        "JOIN orchestrators s ON s.id = t.orchestrator_id AND s.owner_id = ? "
        "WHERE t.orchestrator_id = ? "
        "ORDER BY t.id ASC",
        (owner_id, orchestrator_id),
    )
    return [dict(r) for r in await cur.fetchall()]


async def orchestrator_task_create(
    orchestrator_id: str,
    task_id: str,
    title: str,
    description: str | None,
    model: str | None = None,
    parent_task_id: str | None = None,
    depends_on: list[str] | None = None,
) -> str:
    """Create a task under a orchestrator.  Returns created_at timestamp."""
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO orchestrator_tasks "
        "(id, orchestrator_id, title, description, status, model, result, "
        "progress_pct, parent_task_id, depends_on, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, '', 0.0, ?, ?, ?, ?)",
        (
            task_id,
            orchestrator_id,
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


async def orchestrator_task_update(
    orchestrator_id: str,
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
    vals.extend([task_id, orchestrator_id])
    # Scoped by ownership, not only by ids.  SQLite has no UPDATE ... JOIN, so
    # the check is a subselect.  This is the only write among the five functions
    # that took ``owner_id`` and ignored it, and its callers are all inside the
    # engine today -- but "not currently reachable from a route" is what the two
    # cross-tenant reads were until somebody added a route.
    vals.append(owner_id)
    sql = (
        "UPDATE orchestrator_tasks SET " + ", ".join(sets)
        + " WHERE id = ? AND orchestrator_id = ? AND orchestrator_id IN "
          "(SELECT id FROM orchestrators WHERE owner_id = ?)"
    )
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def orchestrator_task_get(
    orchestrator_id: str, task_id: str, owner_id: str
) -> dict[str, Any] | None:
    """Fetch one task under one owner's orchestrator.

    ``owner_id`` was accepted and unused here too.  Its only caller checks
    ownership first, so this was latent rather than reachable -- which is
    exactly the state the two reachable ones were in until a route was added.
    """
    cur = await db.db_conn.execute(
        "SELECT t.id, t.orchestrator_id, t.title, t.description, t.status, "
        "       t.model, t.result, t.progress_pct, t.parent_task_id, "
        "       t.depends_on, t.created_at, t.updated_at "
        "FROM orchestrator_tasks t "
        "JOIN orchestrators s ON s.id = t.orchestrator_id AND s.owner_id = ? "
        "WHERE t.id = ? AND t.orchestrator_id = ?",
        (owner_id, task_id, orchestrator_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def orchestrator_messages_append(
    orchestrator_id: str, role: str, content: str, metadata: dict[str, Any] | None = None
) -> int:
    """Append a orchestrator message.  Returns row id."""
    cur = await db.db_conn.execute(
        "INSERT INTO orchestrator_messages "
        "(orchestrator_id, role, content, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            orchestrator_id,
            role,
            content,
            json.dumps(metadata) if metadata else None,
            db._now(),
        ),
    )
    await db.db_conn.commit()
    return cur.lastrowid


async def orchestrator_messages_get(
    orchestrator_id: str,
    owner_id: str,
    after_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read one owner's orchestrator messages, optionally since a specific id.

    Three things were wrong here and they were wrong in a way that read
    correctly.  ``owner_id`` was accepted and never used, so any authenticated
    account could read any orchestrator's conversation by id -- and every caller
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
        "FROM orchestrator_messages m "
        "JOIN orchestrators s ON s.id = m.orchestrator_id AND s.owner_id = ? "
        "WHERE m.orchestrator_id = ?"
    )
    params: list[Any] = [owner_id, orchestrator_id]
    if after_id:
        sql += " AND m.id > ?"
        params.append(after_id)
    sql += " ORDER BY m.id ASC LIMIT ?"
    params.append(limit)
    cur = await db.db_conn.execute(sql, tuple(params))  # nosec B608: static fragments
    return [dict(r) for r in await cur.fetchall()]


async def orchestrator_members_list(
    orchestrator_id: str, owner_id: str | None = None
) -> list[dict[str, Any]]:
    """The chats a orchestrator watches, newest addition last.

    Joined against ``chats`` so a member whose conversation was deleted simply
    stops appearing.  An INNER JOIN rather than a LEFT one: a orchestrator listing
    a conversation that no longer exists is the failure worth preventing, and a
    row with a null title beside a real status reads as a bug.

    Deliberately returns no status.  That comes from the orchestrator classifier,
    which already decides working/waiting/failed for every chat and session; a
    second definition here would agree with it only by coincidence, and the two
    would drift the first time either changed.

    ``owner_id`` is optional and scopes the join when given. It is optional
    because every existing caller already passes an orchestrator id it read
    out of that owner's own ``orchestrator_list``, so requiring it would be
    churn with no bug behind it -- but the safety in those callers lives in
    the caller, and the argument lets a caller move it into the query. Compare
    ``orchestrator_tasks_get``, which was written with exactly this argument,
    ignored it, and returned any account's task titles and agent output to any
    authenticated caller for as long as it did.
    """
    scope = " AND c.owner_id = ?" if owner_id else ""
    params: list[Any] = [orchestrator_id]
    if owner_id:
        params.append(owner_id)
    cur = await db.db_conn.execute(
        "SELECT m.orchestrator_id, m.chat_id, m.added_at, "  # nosec B608: static
        "       c.title, c.session_id, c.work_dir "
        "FROM orchestrator_members m "
        "JOIN chats c ON c.id = m.chat_id AND c.deleted_at IS NULL "
        f"WHERE m.orchestrator_id = ?{scope} "
        "ORDER BY m.added_at ASC, m.chat_id ASC",
        tuple(params),
    )
    return [dict(r) for r in await cur.fetchall()]


async def orchestrator_member_add(orchestrator_id: str, chat_id: str) -> bool:
    """Add one chat to a orchestrator.  True if it was not already a member.

    The caller is responsible for having checked that *chat_id* belongs to the
    requesting owner: this layer stores what it is given, and an unchecked id
    here would pull another account's conversation into the members feed along
    with its title and preview.
    """
    cur = await db.db_conn.execute(
        "INSERT INTO orchestrator_members (orchestrator_id, chat_id, added_at) "
        "VALUES (?, ?, ?) ON CONFLICT(orchestrator_id, chat_id) DO NOTHING",
        (orchestrator_id, chat_id, db._now()),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def orchestrator_member_remove(orchestrator_id: str, chat_id: str) -> bool:
    """Drop a member.  Never deletes the conversation itself.

    A orchestrator is a view over work, not its owner -- removing a member must
    leave the conversation exactly as it was.
    """
    cur = await db.db_conn.execute(
        "DELETE FROM orchestrator_members WHERE orchestrator_id = ? AND chat_id = ?",
        (orchestrator_id, chat_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def orchestrator_progress(orchestrator_id: str, owner_id: str) -> float:
    """Mean task progress for one owner's orchestrator, 0.0 when it has no tasks.

    Has no callers at the time of writing.  Kept and scoped rather than deleted
    because the name is one somebody will reach for, and an unscoped query
    sitting under an owner-scoped signature is a trap laid for whoever does.
    One statement instead of two, so the count and the sum cannot be read from
    different states of the table.
    """
    cur = await db.db_conn.execute(
        "SELECT COUNT(*) AS total, SUM(t.progress_pct) AS sum_pct "
        "FROM orchestrator_tasks t "
        "JOIN orchestrators s ON s.id = t.orchestrator_id AND s.owner_id = ? "
        "WHERE t.orchestrator_id = ?",
        (owner_id, orchestrator_id),
    )
    row = await cur.fetchone()
    total = (row["total"] if row else 0) or 0
    if not total:
        return 0.0
    return round(float(row["sum_pct"] or 0) / total, 1)


async def orchestrator_mark_degraded(orchestrator_id: str, kind: str, detail: str) -> None:
    """Flag *orchestrator_id* as carrying a known write failure of *kind*.

    Never raises -- see chat_mark_degraded in routes/db_chats.py for the same
    reasoning; the two exist in parallel rather than as one shared function
    because there is no third caller and the two tables differ (chats also
    stamps degraded_at).
    """
    try:
        await db.db_conn.execute(
            "UPDATE orchestrators SET degraded = 1, degraded_reason = ? WHERE id = ?",
            (f"{kind}: {detail}", orchestrator_id),
        )
        await db.db_conn.commit()
    except Exception:
        _log.exception(
            "supervisor_mark_degraded failed id=%s kind=%s", orchestrator_id, kind,
        )


async def orchestrator_clear_degraded(orchestrator_id: str, kind: str) -> None:
    """Clear the flag, but only if it currently names this same *kind*.

    A clear for kind B never touches a flag currently showing kind A. But
    `degraded_reason` is a single column, not an accumulating fault log: if A
    marks, then B also marks (overwriting A's text), then B clears, the flag
    clears even though A was never resolved -- there is nothing left recording
    that A happened. Accepted tradeoff, same as chat_clear_degraded in
    routes/db_chats.py -- this is a diagnostic signal to go check logs, not a
    durable record of every failure.
    """
    try:
        await db.db_conn.execute(
            "UPDATE orchestrators SET degraded = 0, degraded_reason = NULL "
            "WHERE id = ? AND degraded_reason LIKE ?",
            (orchestrator_id, f"{kind}:%"),
        )
        await db.db_conn.commit()
    except Exception:
        _log.exception(
            "supervisor_clear_degraded failed id=%s kind=%s", orchestrator_id, kind,
        )
