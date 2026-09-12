# db_chats.py — Chat CRUD, auto-answer, messages, and FTS5 search.
#
# Extracted from db.py so the chat route path does not need the full
# database module.

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Final

import db

_log = logging.getLogger("wc.db.chats")

_CHAT_COLUMNS = (
    "id, title, description, session_id, work_dir, owner_id, created_at, "
    "updated_at, archived, pinned, pinned_at, position, deleted_at, model, ai_machine_id, "
    "transcript_offset, degraded, degraded_reason, degraded_at, voice_mode, type, "
    "parent_chat_id, is_temporary"
)
_ALLOWED_CHAT_FIELDS = {
    "title",
    "description",
    "goal",
    "archived",
    "pinned",
    "pinned_at",
    "model",
    "ai_machine_id",
    "voice_mode",
    "type",
    "parent_chat_id",
    "is_temporary",
}


async def chat_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        f"SELECT {_CHAT_COLUMNS} FROM chats "  # nosec B608: columns are static
        "WHERE owner_id = ? AND deleted_at IS NULL "
        # A conversation the user has placed keeps its slot even when it
        # gets new activity -- that is the point of placing it. Unplaced
        # ones sort below by recency, exactly as before.
        "ORDER BY archived ASC, "
        "CASE WHEN archived = 0 THEN pinned ELSE 0 END DESC, "
        "position IS NULL, position ASC, "
        "CASE WHEN archived = 0 AND pinned = 1 THEN pinned_at END DESC, "
        "updated_at DESC",
        (owner_id,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def chat_get(
    chat_id: str,
    owner_id: str,
    include_archived: bool = False,
) -> dict[str, Any] | None:
    archived_filter = "" if include_archived else " AND archived = 0"
    cur = await db.db_conn.execute(
        f"SELECT {_CHAT_COLUMNS} FROM chats "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"
        + archived_filter,  # nosec B608: filter is static, values parameterized
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def chat_create(
    chat_id: str,
    title: str,
    description: str | None,
    work_dir: str,
    owner_id: str,
) -> str:
    if not owner_id or owner_id == "admin":
        raise ValueError("owner_id must be a real user UUID, not 'admin'")
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO chats (id, title, description, work_dir, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, title, description, work_dir, owner_id, now, now),
    )
    await db.db_conn.commit()
    return now


async def chat_update(chat_id: str, owner_id: str, **fields: Any) -> bool:
    if not fields or not set(fields).issubset(_ALLOWED_CHAT_FIELDS):
        return False
    if "pinned" in fields and "pinned_at" not in fields:
        fields["pinned_at"] = db._now() if fields["pinned"] else None
    sets = ", ".join(k + " = ?" for k in fields)
    sets += ", updated_at = ?"
    sql = (
        "UPDATE chats SET "
        + sets
        + " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"
    )  # nosec B608: fields are allowlisted
    vals = list(fields.values()) + [db._now(), chat_id, owner_id]
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    updated = cur.rowcount > 0
    # Each indexed row carries the chat title, so a rename makes every entry
    # for this chat stale. Rebuild them.
    if updated and "title" in fields:
        await _fts_rebuild(chat_id)
    return updated


async def chats_reorder(owner_id: str, chat_ids: list[str]) -> int:
    """Place *chat_ids* in the given order. Returns how many were placed.

    Written as one transaction: a reorder is a single user action, and applying
    half of it would leave the sidebar in an order the user never chose.

    Only the listed conversations are placed. Anything omitted keeps its
    existing position, so reordering one section cannot disturb another.
    """
    if not chat_ids:
        return 0
    try:
        # No explicit BEGIN: db_conn is shared across every writer in the
        # process, and a literal "BEGIN" raises "cannot start a transaction
        # within a transaction" if another coroutine's write already opened
        # one implicitly and has not committed yet (registry #48). The first
        # UPDATE below opens its own implicit transaction, which already
        # covers atomicity up to the commit/rollback below.
        placed = 0
        for index, chat_id in enumerate(chat_ids):
            cur = await db.db_conn.execute(
                "UPDATE chats SET position = ? "
                "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                (index, chat_id, owner_id),
            )
            placed += cur.rowcount
        await db.db_conn.commit()
    except Exception:
        await db.db_conn.rollback()
        raise
    return placed


async def chats_clear_order(owner_id: str) -> int:
    """Unplace every conversation, returning the list to pure recency order."""
    cur = await db.db_conn.execute(
        "UPDATE chats SET position = NULL "
        "WHERE owner_id = ? AND position IS NOT NULL",
        (owner_id,),
    )
    await db.db_conn.commit()
    return cur.rowcount


async def chat_archive(chat_id: str, owner_id: str, archived: int = 1) -> bool:
    return await chat_update(chat_id, owner_id, archived=archived)


async def chat_delete(chat_id: str, owner_id: str) -> bool:
    """Delete an owned conversation and messages while preserving its workspace."""
    cur = await db.db_conn.execute(
        "SELECT id FROM chats WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (chat_id, owner_id),
    )
    if await cur.fetchone() is None:
        return False
    # Capture the message ids first: once the rows are gone, an id lookup via
    # the messages table matches nothing and the index entries are orphaned.
    cur = await db.db_conn.execute(
        "SELECT id FROM messages WHERE chat_id = ?", (chat_id,)
    )
    msg_ids = [row["id"] for row in await cur.fetchall()]
    try:
        await db.db_conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.db_conn.execute(
            "DELETE FROM chats WHERE id = ? AND owner_id = ?",
            (chat_id, owner_id),
        )
        await db.db_conn.commit()
    except Exception:
        await db.db_conn.rollback()
        raise
    await _fts_forget_ids(msg_ids)
    return True


async def chat_fork(
    src_chat_id: str,
    owner_id: str,
) -> dict[str, Any] | None:
    """Duplicate a chat's metadata and messages, returning the new chat dict.

    The forked chat gets a fresh UUID, an appended title suffix, and
    an empty work_dir under the same projects root.
    """
    src = await chat_get(src_chat_id, owner_id)
    if not src:
        return None

    new_chat_id = __import__("uuid").uuid4().hex
    now = db._now()
    new_title = src["title"] + " (fork)"

    # Create a sibling workspace directory.
    work_dir = (__import__("pathlib").Path(src["work_dir"]).parent
                / f"{new_chat_id}_workspace")
    work_dir.mkdir(parents=True, exist_ok=True)

    await db.db_conn.execute(
        "INSERT INTO chats (id, title, description, work_dir, owner_id, "
        "created_at, updated_at, pinned, pinned_at, model, ai_machine_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
        (
            new_chat_id,
            new_title,
            src.get("description"),
            str(work_dir),
            owner_id,
            now,
            now,
            src.get("model"),
            src.get("ai_machine_id"),
        ),
    )

    # Copy messages in bulk.
    rows: list[tuple[str, str]] = []
    cur = await db.db_conn.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (src_chat_id,),
    )
    for row in await cur.fetchall():
        rows.append((row["role"], row["content"]))

    if rows:
        now2 = db._now()
        for role, content in rows:
            await db.db_conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (new_chat_id, role, content, now2),
            )
        await db.db_conn.commit()
        # A fork is a bulk copy into a brand-new chat, so a per-chat rebuild
        # indexes exactly the rows just inserted.
        await _fts_rebuild(new_chat_id)

    return await chat_get(new_chat_id, owner_id)


async def chat_set_session(chat_id: str, session_id: str) -> None:
    await db.db_conn.execute(
        "UPDATE chats SET session_id = ?, updated_at = ? WHERE id = ?",
        (session_id, db._now(), chat_id),
    )
    await db.db_conn.commit()


async def chat_set_transcript_offset(chat_id: str, offset: int) -> None:
    """Record how far the linked transcript has been consumed.

    Deliberately does not touch updated_at: advancing the read position is
    bookkeeping, and letting it bump the timestamp would reorder the sidebar
    every few seconds while a chat is merely being polled.
    """
    await db.db_conn.execute(
        "UPDATE chats SET transcript_offset = ? WHERE id = ?",
        (int(offset), chat_id),
    )
    await db.db_conn.commit()


async def chat_set_question_ids(chat_id: str, question_ids: list[str]) -> None:
    """Persist the set of question IDs already rendered for this chat.

    The list is stored as a JSON-encoded string so it can be read back and
    compared on the next poll without touching the message table.
    """
    await db.db_conn.execute(
        "UPDATE chats SET question_ids = ? WHERE id = ?",
        (json.dumps(question_ids), chat_id),
    )
    await db.db_conn.commit()


async def chat_get_question_ids(chat_id: str) -> list[str]:
    """Return the set of question IDs already rendered for this chat."""
    cur = await db.db_conn.execute(
        "SELECT question_ids FROM chats WHERE id = ?", (chat_id,)
    )
    data = await cur.fetchone()
    # Indexing, not .get(): the row factory is sqlite3.Row, which supports
    # subscripting and keys() but has no .get() at all -- so the defensive
    # form raised AttributeError on every call, which is stricter than the
    # thing it was defending against. The column is named in the SELECT above,
    # so it is always present.
    if not data or not data["question_ids"]:
        return []
    try:
        return json.loads(data["question_ids"])
    except (TypeError, ValueError):
        # Our own column, so this should not happen -- but one corrupt value
        # must not break the sync of every conversation, and it must not do so
        # silently either.
        _log.warning("chat %s has unreadable question_ids", chat_id)
        return []


# ── Auto-answer knob ────────────────────────────────────────────────────────
#
# Storage only. The watcher that consumes this, and the routes that set it, are
# steps 2 and 3 of
# docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md.
#
# Reads and writes of the knob are owner-scoped, unlike most of the per-chat
# helpers above, and that is not incidental: switching it on arms an automatic
# approver of permission prompts, so an unscoped write would let one user turn
# on silent approval inside another user's conversation. The log is scoped for a
# second reason -- it quotes prompt text out of somebody else's session.

_AUTO_ANSWER_LOG_MAX: int = 10


async def chat_auto_answer_set(
    chat_id: str, owner_id: str, enabled: bool, accept_recommended: bool = False,
) -> bool:
    """Arm or disarm auto-approval for one conversation. True if it landed.

    *accept_recommended* is written every call, not merged with whatever was
    there before -- the UI's three-state cycle always sends both fields
    together, so a caller that omits it (an older client, a script) gets the
    unambiguous "off" rather than an implicit carry-forward of a flag it did
    not know to ask about.
    """
    cur = await db.db_conn.execute(
        "UPDATE chats SET auto_answer = ?, auto_answer_recommend = ?, "
        "updated_at = ? "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (1 if enabled else 0, 1 if accept_recommended else 0, db._now(),
         chat_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def chat_auto_answer_get(chat_id: str, owner_id: str) -> bool:
    """Whether auto-approval is on. False for a chat that is not the owner's,
    which is the same answer as "off" on purpose: a caller that cannot set it
    should not be told what it is.
    """
    cur = await db.db_conn.execute(
        "SELECT auto_answer FROM chats "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    return bool(row and row["auto_answer"])


async def chat_auto_answer_recommend_get(chat_id: str, owner_id: str) -> bool:
    """Whether the "accept recommended" authority is armed. Same not-yours-so-
    it-reads-as-off rule as :func:`chat_auto_answer_get`.
    """
    cur = await db.db_conn.execute(
        "SELECT auto_answer_recommend FROM chats "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    return bool(row and row["auto_answer_recommend"])


async def chat_auto_answer_log_append(chat_id: str, entry: dict[str, Any]) -> None:
    """Record one answer or skip, newest first, keeping at most ten.

    Not owner-scoped, because the caller is the watcher rather than a request:
    it already resolved the chat through :func:`chats_with_auto_answer`, and
    there is no user to attribute the write to. Reading is scoped.

    Stamps ``at`` here rather than trusting the caller, so every entry has one
    and they are comparable.
    """
    cur = await db.db_conn.execute(
        "SELECT auto_answer_log FROM chats WHERE id = ?", (chat_id,)
    )
    row = await cur.fetchone()
    if not row:
        return
    existing = _auto_answer_log_decode(chat_id, row["auto_answer_log"])
    record = {"at": db._now(), **entry}
    trimmed = [record, *existing][:_AUTO_ANSWER_LOG_MAX]
    await db.db_conn.execute(
        "UPDATE chats SET auto_answer_log = ? WHERE id = ?",
        (json.dumps(trimmed), chat_id),
    )
    await db.db_conn.commit()


async def chat_auto_answer_log_get(chat_id: str, owner_id: str) -> list[dict[str, Any]]:
    """The last ten answers and skips for this chat, newest first."""
    cur = await db.db_conn.execute(
        "SELECT auto_answer_log FROM chats "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    if not row:
        return []
    return _auto_answer_log_decode(chat_id, row["auto_answer_log"])


def _auto_answer_log_decode(chat_id: str, raw: object) -> list[dict[str, Any]]:
    """Parse the stored log, treating anything unreadable as empty.

    This is our own column, so a bad value should not happen -- but a crash
    mid-write or a manual edit are both possible, and a tooltip is not worth
    failing the whole chat view over. Logged rather than swallowed, because
    silently showing no approvals for a chat that made some is the misleading
    outcome.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _log.warning("chat %s has unreadable auto_answer_log", chat_id)
        return []
    if not isinstance(parsed, list):
        _log.warning("chat %s auto_answer_log is not a list", chat_id)
        return []
    return [item for item in parsed if isinstance(item, dict)]


async def chats_with_auto_answer() -> list[dict[str, Any]]:
    """Every armed conversation the watcher should poll.

    Filtered to chats that carry a ``session_id``: answering means locating the
    session's terminal, so a chat without one can never be answered and polling
    it every tick would be pure cost. Deleted chats are excluded for the same
    reason.
    """
    cur = await db.db_conn.execute(
        "SELECT id, owner_id, session_id, title, auto_answer_recommend "
        "FROM chats "
        "WHERE auto_answer = 1 AND deleted_at IS NULL "
        "AND session_id IS NOT NULL AND session_id != '' "
        "ORDER BY id"
    )
    return [dict(row) for row in await cur.fetchall()]


async def bump_chat_updated_at(chat_id: str) -> None:
    """Update the conversation's ``updated_at`` timestamp.

    Does not touch ``position``, so user-placed conversations keep their
    slot while the recency timestamp becomes accurate for the list view.
    """
    await db.db_conn.execute(
        "UPDATE chats SET updated_at = ? WHERE id = ?",
        (db._now(), chat_id),
    )
    await db.db_conn.commit()


async def chat_set_model(chat_id: str, model: str) -> None:
    await db.db_conn.execute(
        "UPDATE chats SET model = ?, updated_at = ? WHERE id = ?",
        (model, db._now(), chat_id),
    )
    await db.db_conn.commit()


async def chat_set_title(chat_id: str, title: str) -> None:
    await db.db_conn.execute(
        "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
        (title, db._now(), chat_id),
    )
    await db.db_conn.commit()


# ── Chat FTS5 Search ──────────────────────────────────────────────────────────────────

# FTS5 query characters allowed in a bare query (no operators).
# Blocks NEAR(), phrase("..."), *, boolean operators etc. while still
# letting users search for ordinary words and spaces.
_FTSGOOD_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9 _\-\.]+$")


def _fts_validate_query(query: str) -> bool:
    """Return True if *query* is a safe FTS5 bare query."""
    return bool(query) and bool(_FTSGOOD_RE.fullmatch(query))


async def chat_search(owner_id: str, query: str) -> list[dict[str, Any]]:
    """Search message bodies using FTS5.

    Returns a list of unique chats that match *query*, ordered by
    FTS5 rank (best match first).  Each entry is a chat dict with an
    added ``snippet`` key showing the matching fragment.
    """
    # FTS5 MATCH query — values are parameterized, the MATCH keyword is SQL.
    rows: list[dict[str, Any]] = []
    try:
        cur = await db.db_conn.execute(
            "SELECT rowid FROM messages_fts "  # nosec B608: MATCH is SQL keyword
            "WHERE content MATCH ?",
            (query,),
        )
        match_ids = [row["rowid"] for row in await cur.fetchall()]
    except Exception:
        match_ids = []

    if not match_ids:
        return []

    # Fetch full chat details for matching rows, deduplicate by chat_id.
    _placeholders = ",".join("?" for _ in match_ids)
    cur = await db.db_conn.execute(
        f"SELECT c.id AS chat_id, c.title, c.description, c.session_id, "
        f"c.work_dir, c.owner_id, c.created_at, c.updated_at, "
        f"c.archived, c.pinned, c.pinned_at, c.deleted_at, c.model, "
        f"c.ai_machine_id, m.id AS msg_id, m.content AS msg_snippet "  # nosec B608: static SQL
        "FROM chats c "
        f"JOIN messages m ON m.chat_id = c.id AND m.id IN ({_placeholders}) "
        "WHERE c.owner_id = ? AND c.deleted_at IS NULL "
        "ORDER BY c.updated_at DESC",
        match_ids + [owner_id],
    )
    seen: set[str] = set()
    for row in await cur.fetchall():
        chat_id = row["chat_id"]
        if chat_id in seen:
            continue
        seen.add(chat_id)
        d = dict(row)
        d["id"] = d.pop("chat_id")  # keep key name consistent with other endpoints
        d["snippet"] = d.pop("msg_snippet", "") or ""
        rows.append(d)

    return rows


# ── Messages ───────────────────────────────────────────────────────────────────────────


async def messages_get(chat_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def messages_last(chat_id: str, count: int = 1) -> list[dict[str, Any]]:
    """Return the last *count* messages for a chat, ordered by insertion."""
    cur = await db.db_conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, count),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    rows.reverse()  # return in insertion order so index 0 is the oldest
    return rows


async def messages_page(
    chat_id: str, limit: int = 50, before_id: int | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """One page of messages, newest-first internally, returned oldest-first.

    Opening a chat used to call `messages_get` -- every message the
    conversation has ever had, every time, with no LIMIT at all. Fine for a
    ten-turn chat, expensive for a thousand-turn one: the whole row set is
    read, sent, and rendered into the DOM on every open and every poll-driven
    refresh. `before_id` pages backward (older messages, for "load more");
    omitted, this returns the newest `limit`. `limit + 1` is fetched so
    "more remain" is a fact about this page, not a guess from `limit` itself.
    """
    if before_id is None:
        cur = await db.db_conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit + 1),
        )
    else:
        cur = await db.db_conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE chat_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
            (chat_id, before_id, limit + 1),
        )
    rows = [dict(r) for r in await cur.fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()  # oldest first, matching messages_get's ordering
    return rows, has_more


# ── FTS5 index maintenance ──────────────────────────────────────────────────────────
#
# All of this runs on the shared connection. It used to open a fresh sqlite3
# connection per call, inside a worker thread, which made index maintenance a
# *second writer* against the same file: every message write was followed
# immediately by an index write from a different connection. WAL permits one
# writer at a time, so the two raced, and whichever lost waited out its busy
# timeout and reported "database is locked". With the 30-second sync sweep
# touching nine conversations, that was the single largest source of those
# errors -- 492 of 660 in one day's log.
#
# The old arrangement failed worse than it looked, because the index write
# swallowed its exception. A message whose index write lost the race was
# committed and never indexed: it existed, and search could not find it, for
# ever, with nothing logged.


async def _fts_guard(coro_fn) -> None:
    """Run index maintenance, tolerating a SQLite build without FTS5.

    Failure here is not fatal -- search degrades, nothing else does -- but on a
    shared connection it must still be rolled back. A statement that fails
    inside an implicit transaction leaves that transaction open, and every
    later write on the connection then fails too. Swallowing the error without
    the rollback would convert a missing index entry into exactly the
    site-wide "database is locked" this change exists to remove.
    """
    try:
        await coro_fn()
        await db.db_conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            await db.db_conn.rollback()


async def _fts_index_ids(msg_ids: Sequence[int | None]) -> None:
    """Index exactly *msg_ids*, replacing any existing entries for them.

    Cost is proportional to len(msg_ids), not to the size of the conversation.
    Each message is indexed with its chat title prefixed so that title-based
    searches also surface through the index.
    """
    ids = [i for i in msg_ids if i is not None]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)

    async def work() -> None:
        await db.db_conn.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ({marks})",  # nosec B608
            ids,
        )
        cursor = await db.db_conn.execute(
            "SELECT m.id, m.content, c.title FROM messages m "
            "JOIN chats c ON c.id = m.chat_id "
            f"WHERE m.id IN ({marks})",  # nosec B608: generated placeholders
            ids,
        )
        for msg_id, content, title in await cursor.fetchall():
            if content:
                text = f"{title} {content}" if title else content
                await db.db_conn.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                    (msg_id, text),
                )

    await _fts_guard(work)


async def _fts_forget_ids(msg_ids: Sequence[int | None]) -> None:
    """Drop *msg_ids* from the index.

    Callers must capture the ids **before** deleting the message rows: a purge
    that resolves ids via the messages table after the fact matches nothing and
    leaves the entries orphaned.
    """
    ids = [i for i in msg_ids if i is not None]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)

    async def work() -> None:
        await db.db_conn.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ({marks})",  # nosec B608
            ids,
        )

    await _fts_guard(work)


async def _fts_rebuild(chat_id: str | None = None) -> None:
    """Full rebuild of the index for one chat, or for every chat.

    Used for backfill and after a chat title changes, since the title is baked
    into each indexed row. Prefer :func:`_fts_index_ids` on the write path --
    this walks every message of the chat.
    """

    async def work() -> None:
        if chat_id:
            await db.db_conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN "
                "(SELECT id FROM messages WHERE chat_id = ?)",
                (chat_id,),
            )
        else:
            await db.db_conn.execute("DELETE FROM messages_fts")

        cursor = await db.db_conn.execute(
            "SELECT m.id, m.content, c.title FROM messages m "
            "JOIN chats c ON c.id = m.chat_id"
            + (" AND m.chat_id = ?" if chat_id else "")
            + (" ORDER BY m.id ASC" if chat_id else ""),
            (chat_id,) if chat_id else (),
        )
        for msg_id, content, title in await cursor.fetchall():
            if content:
                text = f"{title} {content}" if title else content
                await db.db_conn.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                    (msg_id, text),
                )

    await _fts_guard(work)


async def messages_append(chat_id: str, role: str, content: str) -> int:
    cur = await db.db_conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, db._now()),
    )
    await db.db_conn.commit()
    last_id = cur.lastrowid
    await _fts_index_ids([last_id])
    return last_id


async def messages_batch(chat_id: str, rows: list[tuple[str, str]]) -> list[int]:
    """Insert multiple messages atomically and return their exact IDs."""
    global _messages_batch_lock
    if not rows:
        return []
    if _messages_batch_lock is None:
        _messages_batch_lock = __import__("asyncio").Lock()
    async with _messages_batch_lock:
        ids = []
        try:
            # No explicit BEGIN here either -- see the comment in
            # chats_reorder (registry #48). The first INSERT below opens the
            # implicit transaction this loop needs.
            now = db._now()
            for role, content in rows:
                cur = await db.db_conn.execute(
                    "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                    (chat_id, role, content, now),
                )
                ids.append(cur.lastrowid)
            await db.db_conn.commit()
        except Exception:
            await db.db_conn.rollback()
            raise
        await _fts_index_ids(ids)
        return ids


# Re-export the lock so callers that reference db._messages_batch_lock still
# resolve it via __getattr__.  The variable itself lives here so the extraction
# is self-contained; db.py's __getattr__ proxies to it.
_messages_batch_lock: asyncio.Lock | None = None


async def chat_mark_degraded(chat_id: str, kind: str, detail: str) -> None:
    """Flag *chat_id* as carrying a known write failure of *kind*.

    Never raises: marking degradation must not become a second thing that
    fails a turn, the same rule db.usage_record already states for itself.
    """
    try:
        await db.db_conn.execute(
            "UPDATE chats SET degraded = 1, degraded_reason = ?, degraded_at = ? "
            "WHERE id = ?",
            (f"{kind}: {detail}", db._now(), chat_id),
        )
        await db.db_conn.commit()
    except Exception:
        _log.exception("chat_mark_degraded failed chat_id=%s kind=%s", chat_id, kind)


async def chat_clear_degraded(chat_id: str, kind: str) -> None:
    """Clear *chat_id*'s degraded flag, but only if it names this same *kind*.

    A clear for kind B never touches a flag currently showing kind A -- a
    clear only ever matches the reason text sitting in the column right now.

    That does not protect an EARLIER kind-A failure once kind B has also
    marked the chat: `degraded_reason` is a single free-text column, not an
    accumulating fault log, so it can only hold the most recent mark. If A
    marks, then B marks (overwriting A's text), then B clears, the flag
    clears even though A was never resolved. Accepted tradeoff -- this is a
    diagnostic signal telling an operator to go check logs, not a durable
    record of every failure that ever touched the chat.
    """
    try:
        await db.db_conn.execute(
            "UPDATE chats SET degraded = 0, degraded_reason = NULL, degraded_at = NULL "
            "WHERE id = ? AND degraded_reason LIKE ?",
            (chat_id, f"{kind}:%"),
        )
        await db.db_conn.commit()
    except Exception:
        _log.exception("chat_clear_degraded failed chat_id=%s kind=%s", chat_id, kind)


async def chats_pinned_to_machine(
    machine_id: str, owner_id: str, limit: int = 8
) -> dict[str, Any]:
    """Conversations pinned to *machine_id*, named rather than counted.

    `total` is the real count; `titles` is capped at *limit* so a refusal
    message stays readable when a popular backend has forty. A count alone
    sends the reader hunting through the sidebar for which forty, which is
    the hunt the refusal exists to save them.

    Archived conversations count. Archived is not deleted -- it can be
    restored, and it would then be pinned to a backend shelved underneath it.

    Deleted ones do not: `chat_delete` writes a `deleted_at` tombstone rather
    than removing the row, and that conversation is not coming back.
    """
    cur = await db.db_conn.execute(
        "SELECT COUNT(*) AS n FROM chats "
        "WHERE ai_machine_id = ? AND owner_id = ? AND deleted_at IS NULL",
        (machine_id, owner_id),
    )
    total = (await cur.fetchone())["n"]
    cur = await db.db_conn.execute(
        "SELECT id, title FROM chats "
        "WHERE ai_machine_id = ? AND owner_id = ? AND deleted_at IS NULL "
        "ORDER BY updated_at DESC LIMIT ?",
        (machine_id, owner_id, int(limit)),
    )
    rows = await cur.fetchall()
    return {
        "total": total,
        "titles": [r["title"] for r in rows],
        "ids": [r["id"] for r in rows],
    }


async def chats_pinned_counts(owner_id: str) -> dict[str, int]:
    """Machine id -> number of conversations pinned to it, for one owner.

    The listing counterpart to `chats_pinned_to_machine`. That one names the
    conversations for a refusal message and is asked about a single backend;
    this one answers "how many" for every backend at once, so the settings
    panel can grey out a Disable button that would be refused instead of
    letting it be clicked.

    One GROUP BY rather than a query per machine: the panel lists every backend
    the owner has, and the per-machine version would turn one page load into as
    many round trips.

    Same inclusion rule as `chats_pinned_to_machine`, and it has to stay that
    way -- archived conversations count, tombstoned ones do not. If the two
    disagree, the button and the server disagree, and the user is either
    stopped from doing something that would have worked or invited to do
    something that will not.

    Machines with no pinned conversations are absent rather than zero; callers
    read this with `.get(id, 0)`.
    """
    cur = await db.db_conn.execute(
        "SELECT ai_machine_id AS mid, COUNT(*) AS n FROM chats "
        "WHERE owner_id = ? AND deleted_at IS NULL AND ai_machine_id IS NOT NULL "
        "GROUP BY ai_machine_id",
        (owner_id,),
    )
    return {row["mid"]: row["n"] for row in await cur.fetchall()}
