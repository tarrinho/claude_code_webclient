# db_usage.py — Usage accounting, host statistics, bucket helpers.
#
# Extracted from db.py so the stats / admin routes do not need the full
# database module.

import asyncio
import datetime
import logging
import re
import time
import weakref
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
    cost_cumulative_usd: float | None = None,
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
            " duration_ms, is_error, created_at, origin, billing_route, "
            " cost_cumulative_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                cost_cumulative_usd,
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


async def usage_last_cumulative(chat_id: str, model: str) -> float | None:
    """The session-cumulative cost last recorded for this chat and model.

    `cost_usd` on a row is this turn's own spend; `cost_cumulative_usd` is the
    CLI's running session total that produced it. Subtracting the latter from
    the next frame's `total_cost_usd` is what turns a running total into a
    per-turn charge.

    Keyed on (chat_id, model) rather than on a session id because
    `usage_record` never wrote one. A chat that starts a fresh CLI session
    resets the CLI's total, which shows up as a smaller figure than the one
    stored here -- the caller treats that as a first turn, which is the same
    answer a session-keyed lookup would have given.

    Returns None when nothing was recorded yet, or on a read failure: both mean
    "no baseline", and the caller charges the frame's figure as-is rather than
    dropping the row.
    """
    if not chat_id or not model:
        return None
    try:
        cur = await db.db_conn.execute(
            "SELECT cost_cumulative_usd FROM usage_events "
            "WHERE chat_id = ? AND model = ? AND cost_cumulative_usd IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, model),
        )
        row = await cur.fetchone()
    except Exception as exc:
        _log.warning(
            "usage_cumulative_lookup_failed: chat_id=%s model=%s: %s "
            "(charging the frame's figure as-is)", chat_id, model, exc,
        )
        return None
    if row is None:
        return None
    value = row["cost_cumulative_usd"]
    return float(value) if isinstance(value, (int, float)) else None


USAGE_IMPORT_BATCH: int = 500

# Only one usage import may hold a transaction at a time.
#
# `db.db_conn` is the single connection every request in this process shares,
# and the import below opens an explicit BEGIN and then keeps awaiting inside
# it -- a to_thread call, then a row-by-row insert loop. Every one of those
# awaits hands the event loop to another request, and `_import_cli_usage` runs
# at the top of BOTH usage handlers (routes/misc.py: the report at /api/usage
# and the charts at /api/usage/series). So two overlapping requests issued two
# BEGINs on one connection, SQLite refused the second with "cannot start a
# transaction within a transaction", and the handler answered 500 -- which the
# page renders as "Could not load statistics". Measured live on 2026-09-15.
#
# A lock rather than a second connection: the transaction here is short and
# the contention is two callers, so serialising costs a wait and keeps one
# writer, whereas a second connection would put two writers on a database the
# rest of the process treats as single-writer.
#
# What this does NOT protect against, and is worth knowing before trusting it
# further: any other coroutine calling db.db_conn.commit() while this holds an
# open transaction would commit it early, because the connection is shared and
# commit() is not scoped to a caller. Nothing does that on this path today.
# Resolved per running loop rather than created once at import. A module-level
# Lock binds itself to whichever loop first acquires it, which is invisible in
# production -- one process, one loop -- and breaks the moment anything else
# runs a second loop: the test suite gives each async test its own, and the
# second test in a file failed with "is bound to a different event loop",
# turning a fix for a crash into a different crash.
_IMPORT_TX_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _import_tx_lock() -> asyncio.Lock:
    """The import lock belonging to the loop this call is running on."""
    loop = asyncio.get_running_loop()
    lock = _IMPORT_TX_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _IMPORT_TX_LOCKS[loop] = lock
    return lock


@db.write
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
    # What this session was launched to run, resolved once: a process cannot be
    # re-pointed at another model, so the value is fixed for every row in the
    # import. Recorded alongside the model the transcript reports because the
    # two disagreeing is the whole shape of the 2026-09-15 finding -- 11,022
    # web-routed rows ran on the terminal's model while the conversation asked
    # for another, and nothing in the table showed it. None when the session
    # has since exited, which is no worse than the NULL written before.
    try:
        import prompts as _prompts
        launched_model = await asyncio.to_thread(_prompts.session_model, session_id)
    except Exception:  # pragma: no cover - accounting must not break an import
        launched_model = None
    written = 0
    # The whole loop, not each batch: holding across batches also stops a
    # second caller slipping a BEGIN in between two of this import's own
    # transactions. Imports are short, and a caller that waits is strictly
    # better off than one that used to get an exception.
    async with _import_tx_lock():
        for start in range(0, len(rows), USAGE_IMPORT_BATCH):
            batch = rows[start:start + USAGE_IMPORT_BATCH]
            last = start + USAGE_IMPORT_BATCH >= len(rows)
            checkpoint = int(offset) if last else int(batch[-1].get("offset") or offset)
            try:
                before_changes = db.db_conn.total_changes
                await db.db_conn.execute("BEGIN")
                for row in batch:
                    routed = db.routed_owner_of(
                        markers,
                        int(row.get("offset") or 0),
                        str(row.get("timestamp") or ""),
                        str(row.get("after_prompt") or ""),
                    )
                    # OR IGNORE, paired with the partial unique index in
                    # _ensure_usage_uniqueness: a turn this import has already
                    # recorded is skipped rather than written twice. Without
                    # the index this clause is inert, and without this clause
                    # the index would raise IntegrityError and abort the whole
                    # batch -- losing the turns after it as well as the one
                    # already held.
                    await db.db_conn.execute(
                        "INSERT OR IGNORE INTO usage_events "
                        "(chat_id, session_id, owner_id, model, requested_model, "
                        " provider, input_tokens, "
                        " output_tokens, cache_read_tokens, cache_creation_tokens, "
                        " cost_usd, cost_basis, duration_ms, is_error, created_at, "
                        " origin, context_unsplit, source_offset) "
                        "VALUES (?, ?, ?, ?, ?, 'cli', ?, ?, ?, ?, ?, ?, NULL, 0, ?, "
                        " ?, ?, ?)",
                        (
                            routed["chat_id"] if routed else "",
                            session_id,
                            owner_id,
                            row["model"],
                            launched_model,
                            int(row["input_tokens"]),
                            int(row["output_tokens"]),
                            int(row["cache_read_tokens"]),
                            int(row["cache_creation_tokens"]),
                            row.get("cost_usd"),
                            "transcript" if row.get("cost_usd") is not None else "unknown",
                            row.get("timestamp") or db._now(),
                            "web-routed" if routed else "terminal",
                            1 if row.get("context_unsplit") else 0,
                            # The line this turn came from. None rather than 0
                            # when absent: 0 is a real offset, and the index is
                            # partial on IS NOT NULL, so a missing value must
                            # opt out of the constraint rather than collide
                            # with the first line of the file.
                            (None if row.get("offset") is None
                             else int(row["offset"])),
                        ),
                    )
                # Rows actually inserted, not rows offered. With OR IGNORE
                # above these diverge whenever a turn was already recorded,
                # and `len(batch)` would report an import that did nothing as
                # a full success -- the caller logs this number as
                # "cli_usage_imported rows=N".
                #
                # Read BEFORE the cursor upsert below, not after the commit:
                # total_changes counts every statement on the connection, and
                # the cursor write is one of them. Measuring after it reported
                # exactly one row too many per batch, which the concurrency
                # test caught as [61, 61] against an expected [60, 60].
                inserted = db.db_conn.total_changes - before_changes
                await db.db_conn.execute(
                    "INSERT INTO usage_cursors (session_id, offset) VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET offset = excluded.offset",
                    (session_id, checkpoint),
                )
                await db.db_conn.commit()
                written += inserted
            except Exception:
                await db.db_conn.rollback()
                raise
    return written


@db.write
async def _ensure_usage_uniqueness() -> None:
    """Collapse re-imported transcript turns, then make them impossible.

    Measured on this deployment 2026-09-22: 23,557 of 217,074 usage rows were
    exact duplicates -- same session, same millisecond timestamp, same model
    and all four token counts. That is 10.9% of the table, and both the turn
    count and every token total were inflated by it. The newest was two days
    old, so it was not a historical artefact.

    It is not the documented "one row per turn per model" behaviour, which
    `usage_agent_totals` describes and which would be legitimate: of the extra
    rows, *zero* carried a second model. Nor is it a batching fault -- duplicate
    pairs were never adjacent in rowid (0 of 23,448 groups) and 97.7% sat more
    than 10,000 rows apart, so the copies came from separate import passes. They
    concentrated in 12 sessions out of 2,060.

    The suspected source is that `transcripts._usage_since_sync` resumes on a
    byte offset guarded only by `offset >= size`, while the sibling read path
    carries `_RESUME_ANCHOR_BYTES` for exactly this reason: a size comparison
    cannot tell "appended to" from "rewritten to a similar length", and
    `repair_if_needed()` rewrites transcripts in place. A rewrite shifts every
    offset, the cursor lands mid-history, and turns already imported are
    imported again.

    This function does not depend on that diagnosis being right, which is why
    it exists separately from any fix to the reader. A unique index makes the
    table self-defending against *any* path that re-inserts an identical turn,
    including ones nobody has thought of. If the reader is later fixed, this
    stays correct and costs one index.

    The two halves use DIFFERENT keys on purpose, and that is the design:

    * History is matched on content, because those rows carry no
      `source_offset` and never can -- the offsets they came from were not
      recorded at the time.
    * Everything after this runs is matched on `(session_id, source_offset)`,
      the transcript line the turn came from, which names it exactly.

    A single content-based key for both would have been simpler and wrong. The
    importer can legitimately write two rows for one session at one timestamp
    with identical token counts, told apart only by which routed request they
    answer; a content key drops the second and loses real spend. That is not
    hypothetical -- it is what `tests/test_qa_usage_origin.py` builds, and the
    first version of this migration broke it.

    The delete must run before the index is created. SQLite refuses to build a
    UNIQUE index over a table that already violates it, so the other order
    would raise on every startup against a database with history, turning a
    silent over-count into a server that will not boot.

    The survivor is the lowest-id row that carries a chat_id, falling back to
    the lowest id when none does -- see the comment on the DELETE for why, and
    for the 1,403 attributions the obvious rule would have destroyed.
    Idempotent: the delete matches nothing on a second run, and the index
    creation is IF NOT EXISTS.
    """
    # History: matched on content, because these rows have no source_offset and
    # never will. Deliberately excludes chat_id and origin, and that choice was
    # measured rather than assumed. Including chat_id catches 20,821 of the
    # 23,557, leaving 2,736 groups whose copies differ in it. Those 2,736 are
    # the whole question, so they were characterised directly: every one of
    # them holds an empty chat_id alongside a real one AND spans two origins --
    # the signature of a re-import after the routed markers changed. Groups
    # holding two DIFFERENT real chat_ids, which would be distinct turns that a
    # content key wrongly collapses, number exactly zero in this database.
    # Which copy survives is not a detail, and getting it wrong destroys data
    # the row counts cannot show. The first version kept MIN(id) -- the oldest
    # row -- while the paragraph above describes these duplicates as re-imports
    # written after the routed markers changed. Those two statements point in
    # opposite directions: the re-imported copy is the one carrying the
    # corrected chat_id, and by construction it has the HIGHER id. Measured on
    # this database, MIN(id) kept an unattributed row over an attributed
    # sibling in 1,402 groups and destroyed 1,403 real chat_ids, permanently,
    # because the rows holding them were the ones deleted.
    #
    # So prefer a row that carries attribution, falling back to MIN(id) when
    # none in the group does. Keeping the whole attributed row rather than
    # patching chat_id onto the oldest one is deliberate: chat_id is not the
    # only field that differs. In all 1,402 groups `origin` differs too
    # ('terminal' against 'web-routed'), and in 1,145 so does
    # `requested_model` -- so copying one column across would leave a row
    # claiming a conversation while still labelled as untargeted terminal
    # spend, which is worse than either original and would corrupt the origin
    # breakdown. Every other column is identical, so the surviving row is a
    # real row rather than a hybrid.
    #
    # The earlier note that MIN(id) "keeps ids stable for anything that
    # recorded one" was unfounded: no table declares a foreign key to
    # usage_events, and nothing joins on its id.
    await db.db_conn.execute(
        "DELETE FROM usage_events WHERE id NOT IN ("
        "  SELECT COALESCE("
        "           MIN(CASE WHEN COALESCE(chat_id, '') <> '' THEN id END),"
        "           MIN(id))"
        "  FROM usage_events"
        "  WHERE session_id IS NOT NULL AND session_id <> ''"
        "  GROUP BY session_id, created_at, model, input_tokens,"
        "           output_tokens, cache_read_tokens, cache_creation_tokens"
        ") AND session_id IS NOT NULL AND session_id <> ''"
    )
    # Going forward: matched on identity, not content. A turn is one line of
    # one transcript, so (session_id, source_offset) names it exactly -- no
    # guessing, and no possibility of collapsing two real turns that happen to
    # agree on every count.
    #
    # The content key above must NOT be used here. `usage_import` can legally
    # write two rows for one session at one timestamp with identical token
    # counts, routed to different conversations by their `after_prompt` --
    # tests/test_qa_usage_origin.py builds exactly that, and a content-keyed
    # index silently dropped the second. Production happens to contain no such
    # pair today, which is precisely why relying on that would be a trap: the
    # constraint would hold until the first time someone sent two identical
    # prompts to one session, and then quietly lose the second turn's spend.
    #
    # Partial on IS NOT NULL so pre-column history stays unconstrained.
    await db.db_conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_import_once "
        "ON usage_events(session_id, source_offset) "
        "WHERE source_offset IS NOT NULL"
    )
    await db.db_conn.commit()


@db.write
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
        # Where this turn sat in its transcript -- the byte offset just past
        # its own line, the same value the import cursor checkpoints on. It is
        # the natural identity of an imported turn: one line, one row. Nothing
        # reads it; it exists so the unique index in
        # _ensure_usage_uniqueness can be exact rather than heuristic.
        #
        # NULL for every row that predates the column, deliberately and
        # permanently -- the offsets those rows came from were never recorded
        # and cannot be recovered. The index is partial on IS NOT NULL for
        # that reason, so history is left unconstrained and only new imports
        # are guarded.
        "source_offset":
            "ALTER TABLE usage_events ADD COLUMN source_offset INTEGER",
        # The model the work was *asked* to run on, beside `model`, which is
        # what answered. They diverge whenever a turn reaches a live terminal:
        # a running CLI process cannot be re-pointed, so a request routed into
        # one spends the turn on whatever that process launched with. Measured
        # 2026-09-15, 11,022 rows with origin='web-routed' ran on
        # vllm/Qwen3.6-35B-A3B-NVFP4 while the conversations asking for them
        # were set to Claude models, and nothing in this table could show it.
        #
        # Nullable and not backfilled, for the same reason billing_route is
        # not: NULL means "the write site did not know", which is the honest
        # value for all 166,920 rows that predate this. The column already
        # existed on this deployment's database without ever being written --
        # it is absent from db.py's CREATE TABLE, so a fresh database never
        # had it at all, which is why adding the write surfaced it here.
        "requested_model":
            "ALTER TABLE usage_events ADD COLUMN requested_model TEXT",
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


@db.write
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


# The Usage page reports two numbers for every row it shows, and these are the
# only definitions of them. Written once here because the page previously used
# three different answers -- the headline summed input+output, the origin rows
# subtracted re-read context from that, and the per-model table showed input and
# output in separate columns -- so no two sections agreed and none of them said
# which question it was answering.
#
# TOTAL is everything the model actually read and wrote. Cache reads are real
# processed context and are billed, so leaving them out understated this
# deployment by 5.1x: 13.2 billion shown against 67.2 billion moved, with 53.1
# billion of cache reads invisible.
#
# NEW is the part that was not re-read context. For a model reporting a cache
# breakdown that is input_tokens, which already excludes the cached prefix. For
# a model reporting none (`context_unsplit`), input_tokens is the whole
# conversation re-sent every turn, and how much of it was new is genuinely
# unknown -- so it is excluded from NEW rather than guessed at, and the page
# says so. Output is always new by construction.
#
# What this deliberately does NOT do is subtract unsplit input from a combined
# figure, which is what the old origin breakdown did. That cut terminal usage
# from 10.24 billion to 148 million and made it read as 12x smaller than the
# website, when measured against TOTAL it is several times larger.
_NEW_TOKENS = (
    "COALESCE(SUM(output_tokens), 0) + "
    "COALESCE(SUM(CASE WHEN context_unsplit = 1 THEN 0 "
    "                  ELSE input_tokens END), 0)"
)
_TOTAL_TOKENS = (
    "COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) + "
    "COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_creation_tokens), 0)"
)
# Input the page cannot classify: present in TOTAL, absent from NEW. The page
# prints this so a gap between the two numbers is explained rather than noticed.
_UNSPLIT_TOKENS = (
    "COALESCE(SUM(CASE WHEN context_unsplit = 1 THEN input_tokens ELSE 0 END), 0)"
)


@db.write
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
        f"{_UNSPLIT_TOKENS} AS unsplit_tokens, "
        "SUM(CASE WHEN context_unsplit = 1 THEN 1 ELSE 0 END) AS unsplit_requests, "
        f"{_NEW_TOKENS} AS new_tokens, "
        f"{_TOTAL_TOKENS} AS total_tokens "
        f"FROM usage_events {where} GROUP BY origin "  # nosec B608
        "ORDER BY total_tokens DESC",
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
        "COALESCE(SUM(u.cache_creation_tokens), 0) AS cache_creation_tokens, "
        "MAX(u.context_unsplit) AS context_unsplit, "
        "MAX(u.created_at) AS last_seen, "
        # Unqualified column names: this query has one table, so the `u` alias
        # is optional and the shared fragments stay usable everywhere.
        f"{_NEW_TOKENS} AS new_tokens, "
        f"{_TOTAL_TOKENS} AS total_tokens "
        f"FROM usage_events u {where} "  # nosec B608
        "GROUP BY u.session_id "
        # Ordered by TOTAL, matching what the page now leads with. It used to
        # order by input+output, so the list could disagree with its own
        # figures once cache reads were shown.
        "ORDER BY total_tokens DESC LIMIT ?",
        [*params, max(1, min(int(limit), 100))],
    )
    return [dict(row) for row in await cur.fetchall()]


@db.write
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
        "AS cost_basis_unknown, "
        f"{_NEW_TOKENS} AS new_tokens, "
        f"{_TOTAL_TOKENS} AS total_tokens, "
        # How much of this model's cost figure is real. cost_usd is NULL on
        # 216,730 of 217,074 rows here, so a summed total is a sample
        # presented as a total unless the page can say how big the sample is.
        "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS costed_requests "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY model, provider ORDER BY requests DESC, model ASC",
        params,
    )
    return [dict(row) for row in await cur.fetchall()]


@db.write
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
        "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
        f"{_NEW_TOKENS} AS new_tokens, "
        f"{_TOTAL_TOKENS} AS total_tokens, "
        f"{_UNSPLIT_TOKENS} AS unsplit_tokens, "
        "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS costed_requests, "
        "COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost_usd, "
        "COALESCE(SUM(is_error), 0) AS errors, "
        "COUNT(DISTINCT model) AS models "
        f"FROM usage_events WHERE {where}",  # nosec B608: clause is static
        params,
    )
    row = await cur.fetchone()
    return dict(row) if row else {}


@db.write
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


# Finest first. The value is how many characters of the local ISO timestamp
# make the key, so "minute" is 16 ("2026-09-15T14:37") and "month" is 7.
# "fivemin" and "halfhour" are not prefixes of anything -- they round the
# minute -- so they carry the same width as the minute key and get their own
# expression in _bucket_expr.
_USAGE_BUCKETS: dict[str, int] = {
    "minute": 16,
    "fivemin": 16,
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
    "minute": 60,
    "fivemin": 300,
    "halfhour": 1800,
    "hour": 3600,
    "day": 86400,
}


def clamp_bucket(bucket: str, days: float | None) -> str:
    """The requested bucket, or the finest coarser one the window can draw.

    A minute bucket over thirty days is 43,200 slots: the axis blows past
    _SPINE_MAX and comes back empty, the GROUP BY produces tens of thousands
    of rows, and the payload is megabytes of points no screen can show. The
    range picker suggests a sensible width on every range change, but the two
    controls are independent and nothing stopped the combination.

    Coarsening rather than refusing, because the honest answer to "show me
    thirty days by the minute" is the same data at a width that can be drawn,
    and the response already reports which bucket it used.
    """
    if bucket not in _USAGE_BUCKETS or days is None:
        return bucket
    order = [name for name in _USAGE_BUCKETS if name in _BUCKET_STEP_S]
    if bucket not in order:
        return bucket
    window_s = max(0.0, float(days)) * 86400
    for name in order[order.index(bucket):]:
        if window_s / _BUCKET_STEP_S[name] <= _SPINE_MAX:
            return name
    return order[-1]


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
    elif bucket == "minute":
        floored = (*parts[:5], 0, *parts[6:])
    else:  # halfhour, fivemin -- floored to the slot the minute falls in
        width = 30 if bucket == "halfhour" else 5
        floored = (*parts[:4], (parts.tm_min // width) * width, 0, *parts[6:])
    return time.mktime(time.struct_time(floored))


def _bucket_key(epoch: float, bucket: str) -> str:
    """The key :func:`_bucket_expr` would produce for *epoch*, in local time."""
    parts = time.localtime(epoch)
    if bucket == "day":
        return time.strftime("%Y-%m-%d", parts)
    if bucket == "hour":
        return time.strftime("%Y-%m-%dT%H", parts)
    if bucket == "minute":
        return time.strftime("%Y-%m-%dT%H:%M", parts)
    width = 30 if bucket == "halfhour" else 5
    return time.strftime("%Y-%m-%dT%H:", parts) + (
        f"{(parts.tm_min // width) * width:02d}")


def _bucket_expr(bucket: str) -> tuple[str, list[Any]]:
    """SQL mapping ``created_at`` to a local-time bucket key, and its params."""
    if bucket in ("halfhour", "fivemin"):
        # Rounded down to the slot, not truncated to a prefix: the minute has
        # to survive in the key so the slots of one hour sort and compare as
        # distinct values. printf keeps the two digits, because "2026-09-15T14:5"
        # and "2026-09-15T14:50" are different strings and the chart compares
        # keys as strings.
        width = 30 if bucket == "halfhour" else 5
        rounded = (
            f"substr({_LOCAL_TS}, 1, 14) || "
            f"printf('%02d', (CAST(substr({_LOCAL_TS}, 15, 2) AS INTEGER) "
            f"/ {width}) * {width})"
        )
        return (rounded, [])
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
    #
    # Rounded on the way in, because the sum is order-dependent otherwise and
    # the order is an implementation detail. The same dollars folded from
    # coarser groups gave 1.32 and from finer groups 1.3200000000000003, so
    # the payload carried float noise that varied with how the query happened
    # to group -- and it is dollars: ten decimal places is nine more than the
    # page shows and eight more than a cent.
    if row.get("cost_usd"):
        into["cost_usd"] = round(
            (into.get("cost_usd") or 0) + row["cost_usd"], 10)


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


_AGENT_KEY_EXPR = (
    "CASE WHEN session_id IS NOT NULL AND TRIM(session_id) <> '' "
    "     THEN session_id "
    "     WHEN chat_id IS NOT NULL AND TRIM(chat_id) <> '' THEN chat_id "
    "     ELSE '' END"
)


def _charted(row: dict[str, Any]) -> int:
    """What the chart draws for a row: billable input plus output.

    The same basis usage_model_series and usage_agent_series rank on. Ranking
    on raw input+output instead let one model that reports no cache breakdown
    outrank everything, so the ordering described that defect rather than the
    usage.
    """
    return int(row.get("billable_input") or 0) + int(row.get("output_tokens") or 0)


async def usage_series_bundle(
    owner_id: str,
    days: int | None = 30,
    bucket: str = "day",
    model_top: int = 12,
    agent_top: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    """All three chart series from one pass over usage_events.

    Returns ``{"series": [...], "models": [...], "agents": [...]}``, each
    identical in shape to :func:`usage_series`, :func:`usage_model_series` and
    :func:`usage_agent_series`, which remain the definition of that shape and
    the reference an equivalence test checks this against.

    Why this exists. Those three functions ran **five** full scans of this
    table between them -- one each for the route series, and a ranking scan
    plus a grouping scan for both the model and agent series -- and every one
    of them read the same rows. Nothing could make them cheap individually:
    the grouping key is a computed expression, ``datetime(created_at,
    'localtime')``, so no index can serve it and each scan ends in a temp
    B-tree (EXPLAIN QUERY PLAN: SCAN usage_events, USE TEMP B-TREE FOR GROUP
    BY). Measured on 2026-09-15 against a copy of the production database --
    169,751 rows, 126 MB -- the five came to 19.1s for daily buckets while one
    combined scan took 8.6s, a 2.2x saving, and the Python folding that
    replaces the four dropped queries costs 0.00s.

    The group count is the thing that could have made this a bad trade, since
    grouping by model and agent as well as bucket and route multiplies the
    groups. Measured rather than assumed: 1,198 groups for 30 days by day and
    2,599 by half-hour, against 169,751 rows. Groups can never exceed rows,
    and on this data they are three orders of magnitude below.

    Ranking happens here rather than in SQL for the same reason the route fold
    does: it needs ``normalise_model_id``, so ``vllm/X`` and ``nvidia/X`` rank
    as one model rather than as two series of the same weights.
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
        f"{_AGENT_KEY_EXPR} AS agent_id, "
        "COUNT(*) AS requests, "
        f"{_TOKEN_MEASURES}, "
        "COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS cost_usd, "
        "COALESCE(SUM(is_error), 0) AS errors "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, stored_route, model, agent_id ORDER BY bucket ASC",
        params,
    )
    rows = [dict(raw) for raw in await cur.fetchall()]

    # Rankings first, from the rows already in hand. Both mirror the ORDER BY
    # ... LIMIT the dropped queries used, including that an agent with neither
    # a session nor a chat id is never a candidate -- it cannot be opened, so
    # it belongs in "Other" rather than in the legend.
    model_charted: dict[str, int] = {}
    agent_charted: dict[str, int] = {}
    for row in rows:
        model_charted[normalise_model_id(row.get("model"))] = (
            model_charted.get(normalise_model_id(row.get("model")), 0)
            + _charted(row)
        )
        agent = row.get("agent_id") or ""
        if agent:
            agent_charted[agent] = agent_charted.get(agent, 0) + _charted(row)

    keep_models = {
        name for name, _ in sorted(model_charted.items(), key=lambda kv: -kv[1])
        [:max(1, min(int(model_top), 24))]
    }
    keep_agents = {
        name for name, _ in sorted(agent_charted.items(), key=lambda kv: -kv[1])
        [:max(1, min(int(agent_top), 20))]
    }

    series: dict[tuple[str, str], dict[str, Any]] = {}
    series_order: list[tuple[str, str]] = []
    models: dict[tuple[str, str], dict[str, Any]] = {}
    models_order: list[tuple[str, str]] = []
    agents: dict[tuple[str, str], dict[str, Any]] = {}
    agents_order: list[tuple[str, str]] = []

    for row in rows:
        raw_model = row.get("model")
        agent = row.get("agent_id") or ""
        # A fresh copy per destination: _fold mutates, and `requests` is added
        # into all three, so handing one dict to two folds would double-count.
        measures = {k: v for k, v in row.items()
                    if k not in ("stored_route", "model", "agent_id")}
        # The model and agent series carry requests and token measures only --
        # their queries never selected cost or errors, and folding either in
        # would add a key the charts have never seen. Errors and cost belong to
        # the route series, which is where the page reports them.
        chart_measures = {k: v for k, v in measures.items()
                          if k not in ("cost_usd", "errors")}

        route, inferred = billing_route_of(row.get("stored_route"), raw_model)
        route_row = dict(measures)
        route_row["inferred_requests"] = (
            route_row["requests"] if inferred else 0
        )
        key = (row["bucket"], route)
        if key not in series:
            series[key] = {"bucket": row["bucket"], "route": route}
            series_order.append(key)
        _fold(series[key], route_row)

        name = normalise_model_id(raw_model)
        if name not in keep_models:
            name = "Other"
        key = (row["bucket"], name)
        if key not in models:
            models[key] = {"bucket": row["bucket"], "model": name, "ids": []}
            models_order.append(key)
        entry = models[key]
        if raw_model and raw_model not in entry["ids"]:
            entry["ids"].append(raw_model)
        _fold(entry, chart_measures)

        agent_name = agent if agent in keep_agents else "Other"
        key = (row["bucket"], agent_name)
        if key not in agents:
            agents[key] = {"bucket": row["bucket"], "agent_id": agent_name}
            agents_order.append(key)
        _fold(agents[key], chart_measures)

    return {
        "series": [series[k] for k in series_order],
        "models": [models[k] for k in models_order] if model_charted else [],
        "agents": [agents[k] for k in agents_order] if agent_charted else [],
    }


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


@db.write
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
    "swap_used",
    "swap_total",
    # No "swap_max" here, and the omission is the point. A per-sample maximum
    # is not a quantity: one sample holds one swap_pct, so its own max is
    # itself. `swap_max` is a BUCKET aggregate -- `ROUND(MAX(swap_pct), 1) AS
    # swap_max` in the two series queries below -- exactly like `mem_max`,
    # which is likewise an alias and appears in neither this tuple nor the
    # table.
    #
    # It was listed here from a6c904c4 (2026-09-20) until 2026-09-21, with a
    # matching column added to `system_samples`. Nothing ever produced it:
    # `sysstats.to_row` has no line for it, and the insert below reads
    # `values.get(field, 0) or 0`, so every sample written in that window
    # stored a literal 0. The Server tab was unaffected because it reads the
    # aggregate, never the column. tests/test_qa_sysstats.py caught it the
    # day it landed -- its docstring says "a column added to system_samples
    # without a matching line here would silently store 0 for ever" -- and
    # that is precisely what happened.
    #
    # The column itself is left in place: it is NOT NULL DEFAULT 0, dropping
    # it needs a migration, and nothing reads it. Removing it from this tuple
    # is what stops the pointless write.
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


@db.write
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


@db.write
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
        "ROUND(AVG(swap_pct), 1) AS swap_pct, "
        "ROUND(MAX(swap_pct), 1) AS swap_max, "
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
        "ROUND(MAX(swap_pct), 1) AS swap_max, "
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


@db.write
async def usage_earliest(owner_id: str) -> str | None:
    """The oldest usage timestamp across every account, or None.

    Shared across every account -- see :func:`usage_by_origin`.
    """
    cur = await db.db_conn.execute(
        "SELECT MIN(created_at) AS first FROM usage_events",
    )
    row = await cur.fetchone()
    return (row["first"] if row else None) or None


@db.write
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


@db.write
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


@db.write
async def model_window_learn(model: str, window_tokens: int, source: str = "") -> bool:
    """Record what a backend said *model*'s context window is.

    Called when a turn is refused for exceeding it -- the refusal states the
    number, so this never guesses. Later readings overwrite earlier ones: a
    gateway that moves a model to different hardware changes the window, and
    the newest refusal is the current truth.
    """
    if not model or not isinstance(window_tokens, int) or window_tokens <= 0:
        return False
    try:
        await db.db_conn.execute(
            "INSERT INTO model_context_windows "
            "(model, window_tokens, learned_at, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(model) DO UPDATE SET "
            "  window_tokens = excluded.window_tokens, "
            "  learned_at = excluded.learned_at, source = excluded.source",
            (model, int(window_tokens), db._now(), source or ""),
        )
        await db.db_conn.commit()
        _log.info("model_window_learned model=%s window=%s", model, window_tokens)
        return True
    except Exception as exc:  # pragma: no cover - learning must not break a turn
        _log.warning("model_window_learn_failed model=%s: %s", model, exc)
        return False


@db.write
async def model_window_get(model: str) -> dict[str, Any] | None:
    """What is known about *model*'s window, or None if it has never refused.

    None is a real answer and callers must say so rather than substituting a
    default -- "not known yet" and "fits comfortably" are opposite claims.
    """
    if not model:
        return None
    cur = await db.db_conn.execute(
        "SELECT model, window_tokens, learned_at, source "
        "FROM model_context_windows WHERE model = ?",
        (model,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


@db.write
async def model_windows_all() -> list[dict[str, Any]]:
    """Every window learned so far, newest first."""
    cur = await db.db_conn.execute(
        "SELECT model, window_tokens, learned_at, source "
        "FROM model_context_windows ORDER BY learned_at DESC"
    )
    return [dict(r) for r in await cur.fetchall()]


@db.write
async def context_size_of(chat_id: str, session_id: str = "") -> dict[str, Any] | None:
    """How large this conversation's context was on its most recent turn.

    ``input_tokens`` alone is not the answer and reading it as one is the
    trap here. A cache-reporting model sends most of the conversation from
    cache, so the newest row for a real 411,000-token conversation reads
    ``input_tokens=26, cache_read_tokens=410,958`` -- measured 2026-09-15 on
    "local : 13 : models comparison". The comparable figure is the sum, which
    is what a model with no cache would have to accept as plain input.

    Returns None when the conversation has no usage rows yet: a conversation
    nobody has run has no measured context, and reporting zero would read as
    "empty" rather than "unknown".
    """
    if session_id:
        where, params = "session_id = ?", (session_id,)
    elif chat_id:
        where, params = "chat_id = ?", (chat_id,)
    else:
        return None
    cur = await db.db_conn.execute(
        "SELECT model, input_tokens, cache_read_tokens, cache_creation_tokens, "
        "       created_at "
        # id breaks the tie, and it is load-bearing rather than tidy: created_at
        # has one-second resolution, so two turns in the same second leave the
        # winner to whatever order SQLite happens to return -- which made this
        # report the *older* turn's size in test. id is autoincrement, so it
        # orders strictly even when the timestamps are identical.
        f"FROM usage_events WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 1",
        params,
    )
    row = await cur.fetchone()
    if row is None:
        return None
    sent = int(row["input_tokens"] or 0)
    cached = int(row["cache_read_tokens"] or 0)
    created = int(row["cache_creation_tokens"] or 0)
    return {
        "model": row["model"],
        "measured_at": row["created_at"],
        "input_tokens": sent,
        "cache_read_tokens": cached,
        "cache_creation_tokens": created,
        # What a model without caching would have to take as plain input.
        "total_tokens": sent + cached + created,
    }
