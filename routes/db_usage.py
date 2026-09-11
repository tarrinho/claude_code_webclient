# db_usage.py — Usage accounting, host statistics, bucket helpers.
#
# Extracted from db.py so the stats / admin routes do not need the full
# database module.

import datetime
import logging
import re
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
    billing_route: str = "",
) -> int | None:
    """Record one model's usage for a completed turn.

    Returns the row id, or None if the write failed. Accounting must never
    break a turn that has already succeeded, so failures are swallowed.

    ``billing_route`` is the caller's *record* of which side of the bill this
    turn landed on -- see `billing_route_from_machine`. Callers that cannot
    know it leave it empty, and `billing_route_of` infers one at read time
    from the model id. The two are kept apart on purpose: a chart that cannot
    tell a recorded route from an inferred one cannot say how much of itself
    is a guess.
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
            " duration_ms, is_error, created_at, origin, billing_route) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                billing_route or "",
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
    """Additive usage schema migrations, and a one-time origin backfill.

    ``origin`` replaces inferring where a turn came from. The old rule was
    "session_id is set, therefore a terminal" -- true today, but a guess about
    the shape of a row rather than a statement of fact, and every web turn runs
    against a session-linked conversation, so nothing but the absence of a
    column was keeping the two apart.

    ``context_unsplit`` marks a row whose model reported no cache breakdown, so
    its ``input_tokens`` is the whole conversation re-read rather than new
    spend.

    ``billing_route`` records which side of the bill a turn landed on, and is
    the one migration here with no backfill -- see the comment on it below.

    This is the only definition. db.py held an identical copy until
    2026-09-10, and because a module-level name there beats its __getattr__
    dispatch, that copy was what ran and this one was dead.
    """
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
        # Which side of the bill a turn landed on. Deliberately NOT backfilled
        # anywhere below, unlike origin and context_unsplit: an empty string
        # means "the write site did not know", which is the truth for every row
        # that predates this column, and `billing_route_of` infers a route for
        # those from the model id at read time. Filling this column with those
        # inferences would erase the only distinction it exists to carry --
        # recorded fact against read-time guess -- and the charts label the two
        # differently.
        "billing_route":
            "ALTER TABLE usage_events ADD COLUMN billing_route "
            "TEXT NOT NULL DEFAULT ''",
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


# ── Billing route ───────────────────────────────────────────────────────────
#
# Which side of the bill a turn landed on: the Claude subscription (the CLI's
# own login against api.anthropic.com, priced by Claude Code) or an API key
# through the LiteLLM gateway. It is the split that makes the charts
# reconcilable against the gateway's own figures, and the only one that
# separates spend anybody is billed for twice over.
#
# It is not recoverable from a transcript. Checked on 2026-09-10 across 120
# transcripts, every assistant record, seven models: `service_tier` is
# "standard" on all of them, `quotaLimits` is absent from all of them, and
# `cache_read_input_tokens` is present on all of them. A gateway turn and a
# subscription turn are shape-identical in the record. So the route is
# *recorded* by the write sites that resolve a backend, and *inferred* from the
# model id for the 131k rows written before this existed.
SUBSCRIPTION = "subscription"
GATEWAY = "gateway"
UNCLASSIFIED = "unclassified"

# Model-id prefixes this deployment's gateway puts on what it serves. These are
# facts about *this* LiteLLM instance, not about gateways in general, which is
# why they live in one named table rather than scattered through a query.
_GATEWAY_PREFIXES: tuple[str, ...] = (
    "vllm/", "nvidia/", "azure_ai/", "openai/", "gemini/", "bedrock/", "Qwen/",
)
# Families the gateway serves without a prefix. `gpt-5.6-luna` and
# `gpt-5.4-mini` arrive bare, and no Anthropic subscription serves an
# OpenAI-family id.
_GATEWAY_BARE = re.compile(r"^(?:gpt-|o[0-9]|mistral|llama|qwen|deepseek)", re.I)
# The subscription's own models. Bare `claude-*` only, and the anchor is what
# does the work: a gateway-served Claude carries a vendor prefix
# (`azure_ai/claude-opus-4-8-corporate`) and so cannot match this at all. The
# prefix check above therefore runs first for readability rather than for
# correctness -- reordering the two changes no result, which was confirmed by
# mutation rather than assumed.
_SUBSCRIPTION_BARE = re.compile(r"^claude-")


def billing_route_of(stored: str | None, model: str | None) -> tuple[str, bool]:
    """Return ``(route, inferred)`` for one usage row.

    A stored value always wins: it was written by a site that had resolved the
    backend, so it is a record rather than a reading of the tea leaves. Only
    when it is empty -- every row older than the column -- does the model id
    decide, and the second element of the tuple says so, because the charts
    label an inferred series differently and that label has to be driven by
    the same call that made the decision.

    An id matching no rule is ``unclassified`` rather than being folded into
    whichever side looks more plausible. That bucket is empty against every row
    in the table today, and it is here for the model id that does not exist
    yet: a new gateway model must show up as unattributed, not silently
    inflate the subscription's line.
    """
    if stored:
        return (stored, False)
    name = (model or "").strip()
    if name.startswith(_GATEWAY_PREFIXES):
        return (GATEWAY, True)
    if _SUBSCRIPTION_BARE.match(name):
        return (SUBSCRIPTION, True)
    if _GATEWAY_BARE.match(name):
        return (GATEWAY, True)
    return (UNCLASSIFIED, True)


def billing_route_from_machine(machine: dict[str, Any] | None) -> str:
    """The route a backend record implies, or "" when it implies nothing.

    The base URL decides, and it is the only field that can: every machine on
    this host carries provider ``claude_code`` -- the official API and the
    gateway alike -- so `provider` separates nothing here. A machine with no
    base URL is the official API by definition, which is the same rule
    `backend_env.deltas` applies when it removes an inherited
    ``ANTHROPIC_BASE_URL``.

    Returns "" for an unknown backend rather than guessing, so the row is
    stored unrecorded and classified from its model id at read time like any
    other historical row.
    """
    if not machine:
        return ""
    base = str(machine.get("base_url") or "").strip().lower()
    if not base:
        return SUBSCRIPTION
    host = base.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0]
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return SUBSCRIPTION
    return GATEWAY


def normalise_model_id(model: str | None) -> str:
    """Strip a gateway's vendor prefix so one model is one series.

    `nvidia/Qwen3.6-35B-A3B-NVFP4` and `vllm/Qwen3.6-35B-A3B-NVFP4` are the
    same weights reached two ways, and charting them separately drew one model
    as two lines three orders of magnitude apart.

    Only the prefix is removed. A model the gateway *renamed* --
    `vllm/Qwen3.5-0.8` becoming `Qwen/Qwen3.5-0.8B` -- still reads as two
    models here, and deliberately so: collapsing "0.8" into "0.8B" would mean
    guessing that two ids differing in their size suffix are the same thing,
    which is exactly the kind of inference that put a wrong number on this page
    in the first place.
    """
    name = (model or "").strip()
    for prefix in _GATEWAY_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


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
    """Totals split by where the turn came from: this website, or a terminal.

    Shared across every account by design -- statistics are not per-owner
    here, so *owner_id* is accepted for a stable signature but never filters.
    """
    where = "WHERE 1=1"
    params: list[Any] = []
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



# The dashboard's observation windows are hours, not days: "1h" is one of the
# four the spec names and _cutoff's day granularity cannot express it.
def _cutoff_hours(hours: float) -> str:
    """The ISO timestamp *hours* before now, in _now()'s format."""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max(0.0, hours) * 3600)
    )


async def usage_agent_totals(
    owner_id: str, hours: float | None = 24.0
) -> dict[str, dict[str, int]]:
    """Turns and tokens per agent for the last *hours*, keyed by agent id.

    "Agent" is the same identity `usage_agent_series` uses -- session_id where
    there is one, chat_id otherwise -- because that is where the rows actually
    are, and the supervisor map's nodes are keyed the same way. Anything else
    would report zero for the terminal sessions, which are the majority.

    One grouped statement for the whole fleet rather than a query per node:
    the map polls every ten seconds and the number of agents is not fixed, so
    per-node reads would multiply with the thing being displayed.

    `hours=None` means all time, for the spec's "session" window.

    Turns are counted as rows: usage_record writes one row per turn per model,
    so a two-model turn counts twice here. That is deliberate and matches what
    the usage panel already reports -- and no cost figure is derived from it,
    which is the thing the spec rules out entirely.
    """
    where = "WHERE owner_id = ?"
    params: list[Any] = [owner_id]
    if hours:
        where += " AND created_at >= ?"
        params.append(_cutoff_hours(hours))
    cur = await db.db_conn.execute(
        "SELECT COALESCE(NULLIF(session_id, ''), chat_id) AS agent_id, "
        "COUNT(*) AS turns, "
        "COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens "
        f"FROM usage_events {where} "  # nosec B608
        "GROUP BY agent_id",
        params,
    )
    out: dict[str, dict[str, int]] = {}
    for row in await cur.fetchall():
        agent_id = row["agent_id"]
        if agent_id:
            out[agent_id] = {"turns": row["turns"] or 0,
                             "tokens": row["tokens"] or 0}
    return out

async def usage_by_session(
    owner_id: str, days: int | None = 30, limit: int = 15
) -> list[dict[str, Any]]:
    """Terminal usage per session, named by the conversation it belongs to.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    where = "WHERE u.origin = 'terminal'"
    params: list[Any] = []
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
    """Per-model aggregates across every account. ``days=None`` means all time.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    params: list[Any] = []
    where = "1=1"
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
    """Totals across every model and every account, so the header does not
    re-sum in the client.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    params: list[Any] = []
    where = "1=1"
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
    """Most recent turns across every account, with the conversation title
    joined in.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    cur = await db.db_conn.execute(
        "SELECT u.created_at, u.chat_id, u.model, u.provider, u.input_tokens, "
        "u.output_tokens, u.cost_usd, u.cost_basis, u.duration_ms, u.is_error, "
        "COALESCE(c.title, 'Terminal ' || substr(u.session_id, 1, 8)) AS chat_title "
        "FROM usage_events u LEFT JOIN chats c ON c.id = u.chat_id "
        "ORDER BY u.id DESC LIMIT ?",
        (max(1, min(50 if limit is None else int(limit), 500)),),
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


# The three measures every token chart on the Statistics page reports, defined
# once because they have to mean the same thing in all four of them.
#
# `billable_input` excludes rows flagged `context_unsplit`. Those are turns
# whose model reported no cache breakdown, so each one counts the whole
# conversation again -- 6.30B of the 7.27B tokens in this table on 2026-09-10,
# 86.6% of the headline. The Usage tab has always subtracted them; the charts
# never did, which is most of why they could not be reconciled against the
# gateway's own figures.
#
# `cache_creation` is added to billable input because that is how it is
# charged: writing the cache costs full rate, reading it does not. `cache_read`
# is therefore its own measure rather than being folded in or dropped -- it is
# 25.78B tokens that were charted nowhere at all.
_TOKEN_MEASURES = (
    "COALESCE(SUM(CASE WHEN context_unsplit = 1 THEN 0 "
    "                  ELSE input_tokens + cache_creation_tokens END), 0) "
    "  AS billable_input, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(CASE WHEN context_unsplit = 1 "
    "                  THEN input_tokens ELSE 0 END), 0) AS unsplit_tokens"
)

# Fields summed when several database rows fold into one charted series.
_FOLD_FIELDS = (
    "requests", "billable_input", "cache_read", "output_tokens",
    "unsplit_tokens", "errors", "inferred_requests",
)


def _fold(into: dict[str, Any], row: dict[str, Any]) -> None:
    """Add *row*'s measures into *into*, in place."""
    for field in _FOLD_FIELDS:
        if field in row:
            into[field] = (into.get(field) or 0) + (row.get(field) or 0)
    # Cost is summed separately: it is a float and may legitimately be absent.
    if row.get("cost_usd"):
        into["cost_usd"] = (into.get("cost_usd") or 0) + row["cost_usd"]


async def usage_series(
    owner_id: str, days: int | None = 30, bucket: str = "day"
) -> list[dict[str, Any]]:
    """Token totals per time bucket per **billing route**, oldest first.

    Grouped by route rather than by `provider`, which is the column this was
    grouped by for months and which four code paths write with four different
    meanings: the transcript importer hardcodes "cli" (131,314 of 131,557 rows
    on 2026-09-10), two web paths write raw machine-provider values
    ("anthropic", "anthropic-compatible"), and only the remainder carry a
    `shared.backend_kind` display kind. A chart grouped on that column put
    99.9% of the data in one series named after an implementation detail.

    The route cannot be resolved in SQL -- `billing_route_of` falls back to
    model-id rules for rows written before the column existed -- so the query
    groups by (bucket, stored route, model) and the fold happens here. The
    cardinality is small: 19 distinct models against a bounded bucket count.

    `inferred_requests` carries how many of a series' turns were classified
    rather than recorded, so the chart can say how much of itself is a guess.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [*expr_params]
    where = "1=1"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db.db_conn.execute(
        f"SELECT {expr} AS bucket, "  # nosec B608: expression is ours
        "COALESCE(billing_route, '') AS stored_route, model, "
        "COUNT(*) AS requests, "
        f"{_TOKEN_MEASURES}, "
        "COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost_usd, "
        "COALESCE(SUM(is_error), 0) AS errors "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, stored_route, model ORDER BY bucket ASC",
        params,
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for raw in await cur.fetchall():
        row = dict(raw)
        route, inferred = billing_route_of(row.pop("stored_route"), row.pop("model"))
        row["inferred_requests"] = row["requests"] if inferred else 0
        key = (row["bucket"], route)
        if key not in merged:
            merged[key] = {"bucket": row["bucket"], "route": route}
            order.append(key)
        _fold(merged[key], row)
    return [merged[key] for key in order]


async def usage_model_series(
    owner_id: str, days: int | None = 30, bucket: str = "day", top: int = 12
) -> list[dict[str, Any]]:
    """Token totals per bucket per model, for the top *top* models.

    Models are keyed on their normalised id, so `vllm/X` and `nvidia/X` are one
    series rather than the same weights drawn as two lines three orders of
    magnitude apart. Every raw id that folded into a series comes back in
    `ids`, because the tooltip names them and a merge nobody can see is a merge
    nobody can check.

    `top` defaults to 12 rather than 6: this table holds 19 distinct ids over
    30 days, and the old default hid 13 of them inside "Other".

    Shared across every account -- see :func:`usage_by_origin`.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = []
    where = "1=1"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    # Ranked on the same basis the chart draws, not on raw input+output: with
    # re-counted context included, one unsplit model outranked everything and
    # the ordering described the defect rather than the usage. Ranked after
    # normalising, too, or a model split across two ids could miss the cut
    # twice while its total belonged at the top.
    ranked = await db.db_conn.execute(
        "SELECT model, "
        "SUM(CASE WHEN context_unsplit = 1 THEN 0 "
        "         ELSE input_tokens + cache_creation_tokens END) "
        "  + SUM(output_tokens) AS charted "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY model",
        params,
    )
    by_name: dict[str, int] = {}
    for row in await ranked.fetchall():
        name = normalise_model_id(row["model"])
        by_name[name] = by_name.get(name, 0) + int(row["charted"] or 0)
    if not by_name:
        return []
    keep = {
        name for name, _ in
        sorted(by_name.items(), key=lambda kv: -kv[1])[:max(1, min(int(top), 24))]
    }
    cur = await db.db_conn.execute(
        f"SELECT {expr} AS bucket, model, "  # nosec B608: expression is ours
        "COUNT(*) AS requests, "
        f"{_TOKEN_MEASURES} "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, model ORDER BY bucket ASC",
        [*expr_params, *params],
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for raw in await cur.fetchall():
        row = dict(raw)
        raw_id = row.pop("model")
        name = normalise_model_id(raw_id)
        if name not in keep:
            name = "Other"
        key = (row["bucket"], name)
        if key not in merged:
            merged[key] = {"bucket": row["bucket"], "model": name, "ids": []}
            order.append(key)
        entry = merged[key]
        if raw_id and raw_id not in entry["ids"]:
            entry["ids"].append(raw_id)
        _fold(entry, row)
    return [merged[key] for key in order]


async def usage_agent_series(
    owner_id: str, days: int | None = 30, bucket: str = "day", top: int = 8
) -> list[dict[str, Any]]:
    """Token totals per bucket per agent -- a terminal session or a chat.

    Keyed on `session_id` and falling back to `chat_id`, in that order, because
    that is where the data actually is: 131,314 of 131,557 rows carry a session
    id and only 15,885 carry a chat id. A run's spend belongs to whichever
    conversation produced it, and for the overwhelming majority that is a
    terminal session rather than a web chat.

    Names are resolved by the caller, not here: this module has no business
    reading session files, and the title of a chat is one join away in a table
    this query has no reason to touch.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = []
    where = "1=1"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    key_expr = (
        "CASE WHEN session_id IS NOT NULL AND TRIM(session_id) <> '' "
        "     THEN session_id "
        "     WHEN chat_id IS NOT NULL AND TRIM(chat_id) <> '' THEN chat_id "
        "     ELSE '' END"
    )
    ranked = await db.db_conn.execute(
        f"SELECT {key_expr} AS agent_id, "  # nosec B608: expression is ours
        "SUM(CASE WHEN context_unsplit = 1 THEN 0 "
        "         ELSE input_tokens + cache_creation_tokens END) "
        "  + SUM(output_tokens) AS charted "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY agent_id HAVING agent_id <> '' "
        "ORDER BY charted DESC LIMIT ?",
        [*params, max(1, min(int(top), 20))],
    )
    keep = {row["agent_id"] for row in await ranked.fetchall()}
    if not keep:
        return []
    cur = await db.db_conn.execute(
        f"SELECT {expr} AS bucket, {key_expr} AS agent_id, "  # nosec B608: ours
        "COUNT(*) AS requests, "
        f"{_TOKEN_MEASURES} "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, agent_id ORDER BY bucket ASC",
        [*expr_params, *params],
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for raw in await cur.fetchall():
        row = dict(raw)
        agent = row.pop("agent_id") or ""
        # A row with neither id is real spend that cannot be attributed to
        # anything openable, so it joins "Other" rather than being dropped:
        # the chart's total must still add up to the page's total.
        name = agent if agent in keep else "Other"
        key = (row["bucket"], name)
        if key not in merged:
            merged[key] = {"bucket": row["bucket"], "agent_id": name}
            order.append(key)
        _fold(merged[key], row)
    return [merged[key] for key in order]


async def usage_agent_names(
    owner_id: str, agent_ids: list[str]
) -> dict[str, str]:
    """Titles for the agent ids `usage_agent_series` returned, where one exists.

    One query against `chats`, matching either the linked session id or the
    chat's own id -- the same join `usage_by_session` already uses to name a
    terminal session. Deliberately *not* `read_claude_sessions()`: that reads
    the session files and, on a host with live remote sessions, takes seconds
    per call. A chart legend is not worth a filesystem walk, and an id with no
    title renders as its first eight characters rather than blocking the page.

    Ids with no row are simply absent from the mapping; the caller decides
    what an unnamed agent looks like.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    wanted = [i for i in agent_ids if i and i != "Other"]
    if not wanted:
        return {}
    names: dict[str, str] = {}
    # Chunked: SQLite's default parameter ceiling is 999, and this list is
    # bounded by the caller's `top` today but need not stay that way.
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        cur = await db.db_conn.execute(
            "SELECT id, session_id, title FROM chats "  # nosec B608: generated
            f"WHERE deleted_at IS NULL "
            f"  AND (session_id IN ({marks}) OR id IN ({marks}))",
            [*chunk, *chunk],
        )
        for row in await cur.fetchall():
            title = (row["title"] or "").strip()
            if not title:
                continue
            if row["session_id"] in chunk:
                names.setdefault(row["session_id"], title)
            if row["id"] in chunk:
                names.setdefault(row["id"], title)
    return names


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
    # Not a measurement but a property of the host, stored per sample because
    # that is where the aggregate can reach it: load-per-core is computed
    # inside the bucketed query.
    "cores",
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
    # Host uptime at time of sample (seconds since boot). Stored per-sample
    # so the history can show the transport was offline between samples.
    "uptime_s",
)


# Load divided by the host's own core count, so four machines of different
# sizes can share one chart: a load of 8 is idle on a 16-core box and a queue
# on a 2-core one, and the raw numbers put them on incomparable scales.
#
# The denominator is MAX(cores) rather than AVG: cores is a constant property
# of the host, so any sample in the bucket carries the same value, and MAX
# ignores rows written before the column existed (which default to 0).
#
# cores = 0 means unknown -- an old row, or a transport whose `nproc` did not
# answer. Those yield NULL, not a division by zero and not a made-up 1: the
# host is left out of the load chart for that bucket rather than plotted at a
# figure nobody measured.
_LOAD_PER_CORE = (
    "CASE WHEN MAX(cores) > 0 "
    "THEN ROUND(AVG(load1) / MAX(cores), 3) ELSE NULL END AS load_per_core"
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
        "ROUND(AVG(load15), 2) AS load15, "
        f"MAX(cores) AS cores, {_LOAD_PER_CORE} "
        f"FROM system_samples WHERE {where} "  # nosec B608: clause is static
        "GROUP BY host_id, bucket ORDER BY host_id ASC, bucket ASC",
        params,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in await cur.fetchall():
        entry = dict(row)
        out.setdefault(entry.pop("host_id"), []).append(entry)
    return out


def align_hosts_to_spine(
    by_host: dict[str, list[dict[str, Any]]], spine: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Place every host's rows on the same *spine* of bucket keys.

    The charts draw one line per host on one x-axis, so the hosts have to
    agree on what the nth point means. They do not agree on their own: a
    transport is sampled only while its tunnel is up, so each one arrives with
    its own set of buckets, and drawing those directly puts a machine that was
    connected for two buckets across the full width of the chart.

    Missing buckets are filled with **nulls, not zeros**. A null breaks the
    line, which is the honest picture of a disconnect; a zero draws a
    confident reading of an idle machine that was in fact not measured. This
    is the same distinction `parse_stats` preserves at collection time and
    `_on_spine` preserves for the local series.

    Buckets a host has that the spine does not are kept and the result
    re-sorted, so a reading is never dropped for failing to line up.
    """
    if not spine or not by_host:
        return by_host
    order = {key: index for index, key in enumerate(spine)}
    aligned: dict[str, list[dict[str, Any]]] = {}
    for host_id, rows in by_host.items():
        if not rows:
            aligned[host_id] = rows
            continue
        fields = {key for row in rows for key in row}
        blank = {
            key: None for key in fields if key not in ("bucket", "samples")
        }
        present = {row["bucket"]: row for row in rows}
        filled = [
            present.get(key) or {"bucket": key, "samples": 0, **blank}
            for key in spine
        ]
        filled.extend(row for key, row in present.items() if key not in order)
        filled.sort(key=lambda row: row["bucket"])
        aligned[host_id] = filled
    return aligned


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
        "ROUND(AVG(proc_cpu_pct), 1) AS proc_cpu_pct, "
        f"MAX(cores) AS cores, {_LOAD_PER_CORE} "
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
    """The oldest usage timestamp across every account, or None.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    cur = await db.db_conn.execute(
        "SELECT MIN(created_at) AS first FROM usage_events",
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
