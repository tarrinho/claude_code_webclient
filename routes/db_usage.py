# db_usage.py — Usage accounting, host statistics, bucket helpers.
#
# Extracted from db.py so the stats / admin routes do not need the full
# database module.

import datetime
import logging
import time
from typing import Any

import db

_log = logging.getLogger("wc.db.usage")


def _cutoff(days: int) -> str:
    """Return the ISO timestamp *days* before now, matching _now()'s format."""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max(0, days) * 86400)
    )


async def usage_record(
    chat_id: str,
    owner_id: str,
    model: str,
    provider: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float | None = None,
    cost_basis: str | None = None,
    duration_ms: int | None = None,
    is_error: bool = False,
    origin: str = "web",
) -> int | None:
    """Record one model's usage for a completed turn.

    Returns the row id, or None if the write failed. Accounting must never
    break a turn that has already succeeded, so failures are swallowed.
    """
    if not chat_id or not owner_id or not model:
        _log.warning(
            "usage_record_rejected: chat_id=%r owner_id=%r model=%r "
            "(all three are required to attribute a row)",
            chat_id, owner_id, model,
        )
        return None
    try:
        cur = await db.db_conn.execute(
            "INSERT INTO usage_events "
            "(chat_id, owner_id, model, provider, input_tokens, output_tokens, "
            " cache_read_tokens, cache_creation_tokens, cost_usd, cost_basis, "
            " duration_ms, is_error, created_at, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                owner_id,
                model,
                provider or "claude_code",
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(cache_read_tokens or 0),
                int(cache_creation_tokens or 0),
                cost_usd,
                cost_basis,
                duration_ms,
                1 if is_error else 0,
                db._now(),
                origin or "web",
            ),
        )
        await db.db_conn.commit()
        _log.debug(
            "usage_recorded chat_id=%s model=%s provider=%s in=%s out=%s",
            chat_id, model, provider, input_tokens, output_tokens,
        )
        return cur.lastrowid
    except Exception as exc:
        _log.error(
            "usage_record_failed: chat_id=%s model=%s provider=%s: %s",
            chat_id, model, provider, exc,
        )
        return None


USAGE_IMPORT_BATCH: int = 500


async def usage_cursor_get(session_id: str) -> int:
    """How far a transcript has been consumed for usage accounting."""
    cur = await db.db_conn.execute(
        "SELECT offset FROM usage_cursors WHERE session_id = ?", (session_id,)
    )
    row = await cur.fetchone()
    return int(row["offset"]) if row else 0


async def usage_import(
    owner_id: str, session_id: str, rows: list[dict[str, Any]], offset: int
) -> int:
    """Record usage read out of a terminal transcript. Returns rows written.

    Written with the cursor in one transaction: if the insert succeeded and the
    cursor did not, the next run would count the same turns again, and a usage
    total that drifts upward on its own is worse than one that is late.

    ``created_at`` comes from the record rather than the clock -- these turns
    already happened, and stamping them "now" would pile months of history into
    today and break every windowed query over this table.
    """
    if not rows:
        if offset:
            await db.db_conn.execute(
                "INSERT INTO usage_cursors (session_id, offset) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET offset = excluded.offset",
                (session_id, int(offset)),
            )
            await db.db_conn.commit()
        return 0
    markers = await db.routed_markers(session_id)
    written = 0
    for start in range(0, len(rows), USAGE_IMPORT_BATCH):
        batch = rows[start:start + USAGE_IMPORT_BATCH]
        last = start + USAGE_IMPORT_BATCH >= len(rows)
        checkpoint = int(offset) if last else int(batch[-1].get("offset") or offset)
        try:
            await db.db_conn.execute("BEGIN")
            for row in batch:
                routed = db.routed_owner_of(
                    markers,
                    int(row.get("offset") or 0),
                    str(row.get("timestamp") or ""),
                    str(row.get("after_prompt") or ""),
                )
                await db.db_conn.execute(
                    "INSERT INTO usage_events "
                    "(chat_id, session_id, owner_id, model, provider, input_tokens, "
                    " output_tokens, cache_read_tokens, cache_creation_tokens, "
                    " cost_usd, cost_basis, duration_ms, is_error, created_at, "
                    " origin, context_unsplit) "
                    "VALUES (?, ?, ?, ?, 'cli', ?, ?, ?, ?, ?, ?, NULL, 0, ?, "
                    " ?, ?)",
                    (
                        routed["chat_id"] if routed else "",
                        session_id,
                        owner_id,
                        row["model"],
                        int(row["input_tokens"]),
                        int(row["output_tokens"]),
                        int(row["cache_read_tokens"]),
                        int(row["cache_creation_tokens"]),
                        row.get("cost_usd"),
                        "transcript" if row.get("cost_usd") is not None else "unknown",
                        row.get("timestamp") or db._now(),
                        "web-routed" if routed else "terminal",
                        1 if row.get("context_unsplit") else 0,
                    ),
                )
            await db.db_conn.execute(
                "INSERT INTO usage_cursors (session_id, offset) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET offset = excluded.offset",
                (session_id, checkpoint),
            )
            await db.db_conn.commit()
            written += len(batch)
        except Exception:
            await db.db_conn.rollback()
            raise
    return written


async def _ensure_usage_columns() -> None:
    """Additive usage schema migrations, and a one-time origin backfill."""
    cursor = await db.db_conn.execute("PRAGMA table_info(usage_events)")
    columns = {row["name"] for row in await cursor.fetchall()}
    routed = await db.db_conn.execute("PRAGMA table_info(routed_requests)")
    routed_columns = {row["name"] for row in await routed.fetchall()}
    if routed_columns and "prompt" not in routed_columns:
        await db.db_conn.execute(
            "ALTER TABLE routed_requests ADD COLUMN prompt TEXT NOT NULL DEFAULT ''"
        )
        await db.db_conn.commit()
    migrations = {
        "origin": "ALTER TABLE usage_events ADD COLUMN origin TEXT NOT NULL DEFAULT ''",
        "context_unsplit":
            "ALTER TABLE usage_events ADD COLUMN context_unsplit "
            "INTEGER NOT NULL DEFAULT 0",
    }
    added = False
    for column, statement in migrations.items():
        if column not in columns:
            await db.db_conn.execute(statement)
            added = True
    if added:
        await db.db_conn.commit()
    await db.db_conn.execute(
        "UPDATE usage_events SET origin = "
        "CASE WHEN session_id IS NOT NULL AND TRIM(session_id) <> '' "
        "     THEN 'terminal' ELSE 'web' END "
        "WHERE origin = ''"
    )
    await db.db_conn.execute(
        "UPDATE usage_events SET context_unsplit = 1 "
        "WHERE context_unsplit = 0 AND origin = 'terminal' "
        "  AND cache_read_tokens = 0 AND cache_creation_tokens = 0 "
        "  AND input_tokens > 8000"
    )
    await db.db_conn.commit()


ROUTED_WINDOW_S: int = 6 * 3600


def normalise_prompt(text: str) -> str:
    """A prompt reduced to something comparable across the two records of it."""
    return " ".join((text or "").split())[:200].casefold()


async def routed_request_add(
    session_id: str, chat_id: str, owner_id: str, from_offset: int,
    prompt: str = "",
) -> int | None:
    """Mark that a website request was typed into *session_id*'s terminal."""
    if not session_id or not chat_id:
        return None
    try:
        cur = await db.db_conn.execute(
            "INSERT INTO routed_requests "
            "(session_id, chat_id, owner_id, from_offset, prompt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, chat_id, owner_id, int(from_offset or 0),
             (prompt or "")[:4000], db._now()),
        )
        await db.db_conn.commit()
        return cur.lastrowid
    except Exception:
        _log.warning("routed_request_not_recorded session_id=%s", session_id)
        return None


async def routed_markers(session_id: str) -> list[dict[str, Any]]:
    """Routed-request marks for a session, newest offset first."""
    try:
        cur = await db.db_conn.execute(
            "SELECT chat_id, from_offset, prompt, created_at FROM routed_requests "
            "WHERE session_id = ? ORDER BY from_offset DESC",
            (session_id,),
        )
        return [dict(row) for row in await cur.fetchall()]
    except Exception:
        return []


def routed_owner_of(
    markers: list[dict[str, Any]], offset: int, when: str, after_prompt: str = ""
) -> dict[str, Any] | None:
    """The routed request a transcript row belongs to, if any."""
    if not markers:
        return None
    wanted = normalise_prompt(after_prompt)
    if not wanted:
        return None
    for marker in markers:
        if offset and offset < marker["from_offset"]:
            continue
        if normalise_prompt(marker.get("prompt") or "") != wanted:
            continue
        try:
            asked = datetime.datetime.fromisoformat(
                str(marker["created_at"]).replace("Z", "+00:00")
            )
            wrote = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return marker
        if -60 <= (wrote - asked).total_seconds() <= ROUTED_WINDOW_S:
            return marker
        return None
    return None


async def usage_by_origin(owner_id: str, days: int | None = 30) -> list[dict[str, Any]]:
    """Totals split by where the turn came from: this website, or a terminal."""
    where = "WHERE owner_id = ?"
    params: list[Any] = [owner_id]
    if days:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        "SELECT COALESCE(NULLIF(origin, ''), 'web') AS origin, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
        "COALESCE(SUM(CASE WHEN context_unsplit = 1 "
        "                  THEN input_tokens ELSE 0 END), 0) AS unsplit_tokens, "
        "SUM(CASE WHEN context_unsplit = 1 THEN 1 ELSE 0 END) AS unsplit_requests "
        f"FROM usage_events {where} GROUP BY origin ORDER BY origin",  # nosec B608
        params,
    )
    return [dict(row) for row in await cur.fetchall()]


async def usage_by_session(
    owner_id: str, days: int | None = 30, limit: int = 15
) -> list[dict[str, Any]]:
    """Terminal usage per session, named by the conversation it belongs to."""
    where = "WHERE u.owner_id = ? AND u.origin = 'terminal'"
    params: list[Any] = [owner_id]
    if days:
        where += " AND u.created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        "SELECT u.session_id, "
        "(SELECT c.title FROM chats c WHERE c.session_id = u.session_id "
        " AND c.deleted_at IS NULL LIMIT 1) AS title, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(u.input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(u.output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read_tokens, "
        "MAX(u.context_unsplit) AS context_unsplit, "
        "MAX(u.created_at) AS last_seen "
        f"FROM usage_events u {where} "  # nosec B608
        "GROUP BY u.session_id "
        "ORDER BY SUM(u.input_tokens + u.output_tokens) DESC LIMIT ?",
        [*params, max(1, min(int(limit), 100))],
    )
    return [dict(row) for row in await cur.fetchall()]


async def usage_totals(owner_id: str, days: int | None = 30) -> list[dict[str, Any]]:
    """Per-model aggregates for *owner_id*. ``days=None`` means all time."""
    params: list[Any] = [owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        "SELECT model, provider, COUNT(*) AS requests, "
        "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "SUM(COALESCE(cost_usd, 0)) AS cost_usd, "
        "SUM(is_error) AS errors, MAX(created_at) AS last_used, "
        "MAX(CASE WHEN cost_basis = 'unknown' THEN 1 ELSE 0 END) "
        "AS cost_basis_unknown "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY model, provider ORDER BY requests DESC, model ASC",
        params,
    )
    return [dict(row) for row in await cur.fetchall()]


async def usage_overall(owner_id: str, days: int | None = 30) -> dict[str, Any]:
    """Totals across every model, so the header does not re-sum in the client."""
    params: list[Any] = [owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        "SELECT COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(is_error), 0) AS errors, "
        "COUNT(DISTINCT model) AS models "
        f"FROM usage_events WHERE {where}",  # nosec B608: clause is static
        params,
    )
    row = await cur.fetchone()
    return dict(row) if row else {}


async def usage_recent(owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent turns, with the conversation title joined in."""
    cur = await db.db_conn.execute(
        "SELECT u.created_at, u.chat_id, u.model, u.provider, u.input_tokens, "
        "u.output_tokens, u.cost_usd, u.cost_basis, u.duration_ms, u.is_error, "
        "COALESCE(c.title, 'Terminal ' || substr(u.session_id, 1, 8)) AS chat_title "
        "FROM usage_events u LEFT JOIN chats c ON c.id = u.chat_id "
        "WHERE u.owner_id = ? ORDER BY u.id DESC LIMIT ?",
        (owner_id, max(1, min(50 if limit is None else int(limit), 500))),
    )
    return [dict(row) for row in await cur.fetchall()]


_USAGE_BUCKETS: dict[str, int] = {
    "halfhour": 16,
    "hour": 13,
    "day": 10,
    "month": 7,
}
# Public name for test / route code that reads from db.USAGE_BUCKETS.
USAGE_BUCKETS = _USAGE_BUCKETS

_LOCAL_TS: str = "replace(datetime(created_at, 'localtime'), ' ', 'T')"

_SPINE_MAX: int = 5000

_BUCKET_STEP_S: dict[str, int] = {
    "halfhour": 1800,
    "hour": 3600,
    "day": 86400,
}


def bucket_spine(
    bucket: str, days: int | None, earliest: str | None = None
) -> list[str]:
    """Every bucket key across the window, in order, with none missing."""
    step = _BUCKET_STEP_S.get(bucket)
    if step is None:
        return []
    now = time.time()
    if days is not None:
        start = now - max(0, days) * 86400
    elif earliest:
        start = _epoch_of(earliest)
        if start is None:
            return []
    else:
        return []
    first = _floor_local(start, bucket)
    keys: list[str] = []
    cursor = first
    while cursor <= now + step:
        keys.append(_bucket_key(cursor, bucket))
        if len(keys) > _SPINE_MAX:
            return []
        cursor += step
    cutoff = _bucket_key(now, bucket)
    return [key for key in keys if key <= cutoff]


def _epoch_of(stamp: str) -> float | None:
    """Seconds since the epoch for a stored UTC timestamp, or None."""
    text = (stamp or "").strip().rstrip("Z")
    for shape in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.datetime.strptime(text[:19], shape).replace(
                tzinfo=datetime.UTC)
        except ValueError:
            continue
        return parsed.timestamp()
    return None


def _floor_local(epoch: float, bucket: str) -> float:
    """*epoch* floored to the start of its local bucket."""
    parts = time.localtime(epoch)
    if bucket == "day":
        floored = (*parts[:3], 0, 0, 0, *parts[6:])
    elif bucket == "hour":
        floored = (*parts[:4], 0, 0, *parts[6:])
    else:  # halfhour
        floored = (*parts[:4], 30 if parts.tm_min >= 30 else 0, 0, *parts[6:])
    return time.mktime(time.struct_time(floored))


def _bucket_key(epoch: float, bucket: str) -> str:
    """The key :func:`_bucket_expr` would produce for *epoch*, in local time."""
    parts = time.localtime(epoch)
    if bucket == "day":
        return time.strftime("%Y-%m-%d", parts)
    if bucket == "hour":
        return time.strftime("%Y-%m-%dT%H", parts)
    return time.strftime("%Y-%m-%dT%H:", parts) + (
        "30" if parts.tm_min >= 30 else "00")


def _bucket_expr(bucket: str) -> tuple[str, list[Any]]:
    """SQL mapping ``created_at`` to a local-time bucket key, and its params."""
    if bucket == "halfhour":
        halfhour = (
            f"substr({_LOCAL_TS}, 1, 14) || "
            f"CASE WHEN CAST(substr({_LOCAL_TS}, 15, 2) AS INTEGER) < 30 "
            "THEN '00' ELSE '30' END"
        )
        return (halfhour, [])
    return (
        f"substr({_LOCAL_TS}, 1, ?)",
        [_USAGE_BUCKETS.get(bucket, _USAGE_BUCKETS["day"])],
    )


async def usage_series(
    owner_id: str, days: int | None = 30, bucket: str = "day"
) -> list[dict[str, Any]]:
    """Token totals per time bucket per provider, oldest first."""
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [*expr_params, owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        f"SELECT {expr} AS bucket, provider, "  # nosec B608: expression is ours
        "COALESCE(NULLIF(origin, ''), 'web') AS origin, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
        "COALESCE(SUM(CASE WHEN context_unsplit = 1 "
        "                  THEN input_tokens ELSE 0 END), 0) AS unsplit_tokens, "
        "COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost_usd, "
        "COALESCE(SUM(is_error), 0) AS errors "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, provider, origin ORDER BY bucket ASC",
        params,
    )
    return [dict(row) for row in await cur.fetchall()]


async def usage_model_series(
    owner_id: str, days: int | None = 30, bucket: str = "day", top: int = 6
) -> list[dict[str, Any]]:
    """Token totals per time bucket per model, for the top *top* models."""
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    ranked = await db.db_conn.execute(
        "SELECT model FROM usage_events "
        f"WHERE {where} "  # nosec B608: clause is static
        "GROUP BY model ORDER BY SUM(input_tokens + output_tokens) DESC "
        "LIMIT ?",
        [*params, max(1, min(int(top), 12))],
    )
    keep = [row["model"] for row in await ranked.fetchall()]
    if not keep:
        return []
    cur = await db.db_conn.execute(
        f"SELECT {expr} AS bucket, model, "  # nosec B608: expression is ours
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, model ORDER BY bucket ASC",
        [*expr_params, *params],
    )
    kept = set(keep)
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in await cur.fetchall():
        row = dict(raw)
        if row["model"] not in kept:
            row["model"] = "Other"
        key = (row["bucket"], row["model"])
        if key in merged:
            for field in ("requests", "input_tokens", "output_tokens"):
                merged[key][field] += row[field]
        else:
            merged[key] = row
    return list(merged.values())


async def usage_prune(days: int) -> int:
    """Delete rows older than *days*. Returns the number removed."""
    if not days or days <= 0:
        return 0
    try:
        cur = await db.db_conn.execute(
            "DELETE FROM usage_events WHERE created_at < ?", (_cutoff(days),)
        )
        await db.db_conn.commit()
        return cur.rowcount or 0
    except Exception:
        return 0


# ── Host statistics ─────────────────────────────────────────────────────────────────────

SYSTEM_FIELDS: tuple[str, ...] = (
    "cpu_pct",
    "mem_pct",
    "mem_used",
    "mem_total",
    "swap_pct",
    "disk_pct",
    "disk_used",
    "disk_total",
    "load1",
    "load5",
    "load15",
    "proc_rss",
    "proc_cpu_pct",
)


async def system_sample_insert(
    values: dict[str, Any],
    *,
    host_type: str = "local",
    host_id: str = "local",
) -> None:
    """Store one host sample. Missing fields default to 0.

    ``host_type``/``host_id`` say which machine the sample describes. They
    default to the local host because that is what every caller meant before
    they existed, and the columns were added with the same defaults -- so rows
    written before this argument are already labelled correctly and nothing
    needs migrating.

    They are keyword-only on purpose. A previous version of this function took
    ``(host_type, host_id, data)`` positionally, shadowed this one, and was fed
    a single flattened dict by sysstats' background loop -- crashing on every
    interval with "type 'dict' is not supported" and stopping local sampling
    entirely (see the note in db.py). Keyword-only means a caller written
    against either signature cannot silently bind the wrong thing.
    """
    if db.db_conn is None:
        return
    columns: str = ", ".join(("created_at", "host_type", "host_id", *SYSTEM_FIELDS))
    placeholders: str = ", ".join("?" * (len(SYSTEM_FIELDS) + 3))
    await db.db_conn.execute(
        f"INSERT INTO system_samples ({columns}) "  # nosec B608: names are literals
        f"VALUES ({placeholders})",
        [
            db._now(), host_type, host_id,
            *(values.get(field, 0) or 0 for field in SYSTEM_FIELDS),
        ],
    )
    await db.db_conn.commit()


async def system_latest() -> dict[str, Any] | None:
    """The most recent sample *of this host*, or None if nothing is stored.

    Scoped to host_type='local'. Unscoped, this returned whichever row was
    newest -- and the transport poller writes one row per connected transport
    per interval, so the figure this powers was usually a transport's, and
    while the poller's keys were unmapped it was a row of zeros.
    """
    cur = await db.db_conn.execute(
        "SELECT * FROM system_samples WHERE host_type = 'local' "
        "ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def system_latest_by_host() -> list[dict[str, Any]]:
    """The newest sample for each non-local host, newest sample first.

    One grouped statement rather than a query per transport: this is read on
    page open, and the number of transports is not fixed.
    """
    cur = await db.db_conn.execute(
        "SELECT s.* FROM system_samples s "
        "JOIN (SELECT host_id, MAX(id) AS newest FROM system_samples "
        "      WHERE host_type != 'local' GROUP BY host_id) latest "
        "  ON latest.host_id = s.host_id AND latest.newest = s.id "
        "ORDER BY s.created_at DESC"
    )
    return [dict(r) for r in await cur.fetchall()]


async def system_series_by_host(
    days: int | None = 1, bucket: str = "halfhour"
) -> dict[str, list[dict[str, Any]]]:
    """Every transport's samples bucketed over time, keyed by host_id.

    One grouped query for all of them rather than one request per transport:
    the page draws a chart per transport, the number of transports is not
    fixed, and an N+1 here would make opening the Server tab cost a round
    trip per configured host.

    Shares `system_series`' bucket expression and column list deliberately --
    the charts are the same charts, so the rows have to have the same shape.
    A second, subtly different aggregation would render two graphs that look
    alike and mean different things.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [*expr_params]
    where = "host_type != 'local'"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        f"SELECT host_id, {expr} AS bucket, "  # nosec B608: expression is ours
        "COUNT(*) AS samples, "
        "ROUND(AVG(cpu_pct), 1) AS cpu_pct, "
        "ROUND(MAX(cpu_pct), 1) AS cpu_max, "
        "ROUND(AVG(mem_pct), 1) AS mem_pct, "
        "ROUND(MAX(mem_pct), 1) AS mem_max, "
        "ROUND(AVG(disk_pct), 1) AS disk_pct, "
        "ROUND(MAX(disk_pct), 1) AS disk_pct_max, "
        "ROUND(AVG(load1), 2) AS load1, "
        "ROUND(MAX(load1), 2) AS load1_max, "
        "ROUND(AVG(load5), 2) AS load5, "
        "ROUND(AVG(load15), 2) AS load15 "
        f"FROM system_samples WHERE {where} "  # nosec B608: clause is static
        "GROUP BY host_id, bucket ORDER BY host_id ASC, bucket ASC",
        params,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in await cur.fetchall():
        entry = dict(row)
        out.setdefault(entry.pop("host_id"), []).append(entry)
    return out


async def system_series(
    days: int | None = 7, bucket: str = "hour", fill: bool = False
) -> list[dict[str, Any]]:
    """This host's samples averaged per time bucket, oldest first.

    Scoped to host_type='local' for the reason system_latest is: the transport
    poller writes a row per connected transport per interval into this same
    table, so an unscoped average silently mixed four machines' figures into
    one line -- and while the poller's keys were unmapped, it averaged in
    zeros, which is what the graph gaps and the low readings were.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [*expr_params]
    where = "host_type = 'local'"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        f"SELECT {expr} AS bucket, "  # nosec B608: expression is ours
        "COUNT(*) AS samples, "
        "ROUND(AVG(cpu_pct), 1) AS cpu_pct, "
        "ROUND(MAX(cpu_pct), 1) AS cpu_max, "
        "ROUND(AVG(mem_pct), 1) AS mem_pct, "
        "ROUND(MAX(mem_pct), 1) AS mem_max, "
        "ROUND(AVG(swap_pct), 1) AS swap_pct, "
        "ROUND(AVG(disk_pct), 1) AS disk_pct, "
        "ROUND(MAX(disk_pct), 1) AS disk_pct_max, "
        "CAST(AVG(mem_used) AS INTEGER) AS mem_used, "
        "CAST(MAX(mem_total) AS INTEGER) AS mem_total, "
        "CAST(AVG(disk_used) AS INTEGER) AS disk_used, "
        "CAST(MAX(disk_total) AS INTEGER) AS disk_total, "
        "ROUND(AVG(load1), 2) AS load1, "
        "ROUND(MAX(load1), 2) AS load1_max, "
        "ROUND(AVG(load5), 2) AS load5, "
        "ROUND(AVG(load15), 2) AS load15, "
        "CAST(AVG(proc_rss) AS INTEGER) AS proc_rss, "
        "CAST(MAX(proc_rss) AS INTEGER) AS proc_rss_max, "
        "ROUND(AVG(proc_cpu_pct), 1) AS proc_cpu_pct "
        f"FROM system_samples WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket ORDER BY bucket ASC",
        params,
    )
    rows = [dict(row) for row in await cur.fetchall()]
    if not fill:
        return rows
    earliest = await _earliest("system_samples") if days is None else None
    return _on_spine(rows, bucket, days, earliest)


async def usage_earliest(owner_id: str) -> str | None:
    """The oldest usage timestamp for *owner_id*, or None."""
    cur = await db.db_conn.execute(
        "SELECT MIN(created_at) AS first FROM usage_events WHERE owner_id = ?",
        (owner_id,),
    )
    row = await cur.fetchone()
    return (row["first"] if row else None) or None


async def _earliest(table: str) -> str | None:
    """The oldest ``created_at`` in *table*, or None when it is empty."""
    cur = await db.db_conn.execute(
        f"SELECT MIN(created_at) AS first FROM {table}")  # nosec B608: fixed
    row = await cur.fetchone()
    return (row["first"] if row else None) or None


def _on_spine(
    rows: list[dict[str, Any]], bucket: str, days: int | None,
    earliest: str | None = None,
) -> list[dict[str, Any]]:
    """Place *rows* on a continuous bucket spine, missing buckets as nulls."""
    spine = bucket_spine(bucket, days, earliest)
    if not spine:
        return rows
    present = {row["bucket"]: row for row in rows}
    if not present:
        return rows
    fields = {key for row in rows for key in row}
    blank = {key: None for key in fields if key not in ("bucket", "samples")}
    filled: list[dict[str, Any]] = []
    for key in spine:
        row = present.get(key)
        filled.append(row if row else {"bucket": key, "samples": 0, **blank})
    extra = [row for key, row in present.items() if key not in set(spine)]
    if extra:
        filled.extend(extra)
        filled.sort(key=lambda row: row["bucket"])
    return filled


async def system_prune(days: int) -> int:
    """Delete samples older than *days*. Returns the number removed."""
    if not days or days <= 0:
        return 0
    try:
        cur = await db.db_conn.execute(
            "DELETE FROM system_samples WHERE created_at < ?", (_cutoff(days),)
        )
        await db.db_conn.commit()
        return cur.rowcount or 0
    except Exception:
        return 0
