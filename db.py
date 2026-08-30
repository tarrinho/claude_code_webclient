# db.py — SQLite bootstrap and queries for WebConsole.
#
# Single SQLite file, accessed via aiodlite (async). Schema created automatically on boot.
# All paths are resolved and validated against PROJECTS_ROOT before any filesystem op.
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import aiosqlite

import config

_log = logging.getLogger("wc.db")

db_conn: aiosqlite.Connection | None = None
_messages_batch_lock: asyncio.Lock | None = None

# How long index maintenance waits for the SQLite writer lock before giving up.
_FTS_BUSY_TIMEOUT_MS: Final[int] = 5000

# Every SQLite database file starts with this. Used to reject non-DB uploads.
_SQLITE_MAGIC: Final[bytes] = b"SQLite format 3\x00"

# host/port are NOT NULL and describe the transport, so an Anthropic machine
# stores the API endpoint there. It also makes the reachability test meaningful.
_ANTHROPIC_HOST: Final[str] = "api.anthropic.com"


async def init() -> None:
    """Create the database and tables. Idempotent."""
    global db_conn, _messages_batch_lock
    _messages_batch_lock = asyncio.Lock()
    root = Path(config.PROJECTS_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)

    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db_conn = await aiosqlite.connect(str(db_path))
    db_conn.row_factory = aiosqlite.Row
    await db_conn.execute("PRAGMA journal_mode=WAL")
    await db_conn.execute("PRAGMA foreign_keys=ON")

    await db_conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            description   TEXT,
            session_id    TEXT,
            work_dir      TEXT NOT NULL,
            owner_id      TEXT NOT NULL DEFAULT 'admin',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL DEFAULT '',
            archived      INTEGER NOT NULL DEFAULT 0,
            pinned        INTEGER NOT NULL DEFAULT 0,
            pinned_at     TEXT,
            -- Manual slot in the sidebar. NULL means unplaced, which
            -- keeps sorting by recency; a value pins it to that spot.
            position      INTEGER,
            deleted_at    TEXT,
            model         TEXT,
            ai_machine_id TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_machines (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            -- 'anthropic' talks to the official API (what Claude Code uses by
            -- default); 'proxy' reaches a host running claude_proxy.py.
            provider      TEXT NOT NULL DEFAULT 'proxy',
            host          TEXT NOT NULL,
            port          INTEGER NOT NULL DEFAULT 9000,
            api_key       TEXT,
            -- The default model for turns on this machine. Keep in step with
            -- config.MODEL_NAME: a retired model id here makes every turn fail
            -- on a fresh database.
            model         TEXT NOT NULL DEFAULT 'claude-sonnet-5',
            -- JSON array of model ids offered in the picker. Empty means every
            -- model the backend serves is offered, so the feature is opt-in
            -- and an untouched machine can never present an empty picker.
            active_models TEXT NOT NULL DEFAULT '[]',
            base_url      TEXT,
            description   TEXT,
            active        INTEGER NOT NULL DEFAULT 0,
            owner_id      TEXT NOT NULL DEFAULT 'admin',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT NOT NULL REFERENCES chats(id),
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

        -- FTS5 index for full-text chat search on message bodies.
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content_rowid=id
        );
        -- Manually maintained via _refresh_fts_sync() — the
        -- content=auto-trigger path is known to be broken on some
        -- SQLite 3.46.x builds (MATCH returns 0 despite entries).
        -- Manually maintained via _refresh_fts_sync() — content=auto
        -- triggers don't work on some SQLite builds.

        CREATE TABLE IF NOT EXISTS users (
            id       TEXT PRIMARY KEY,
            email    TEXT,
            name     TEXT NOT NULL,
            password TEXT NOT NULL,
            role     TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL DEFAULT ''
        );

        -- Sessions outlive a restart. Keyed by a hash of the session id:
        -- the id itself lives only in the user's cookie, so a copy of this
        -- database -- including one taken through /api/admin/export -- cannot
        -- be replayed as a login.
        -- How far each terminal transcript has been read for usage
        -- accounting. Without it an import would re-count every earlier turn
        -- on every run, and the totals would climb on their own.
        CREATE TABLE IF NOT EXISTS usage_cursors (
            session_id TEXT PRIMARY KEY,
            offset     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            sid_key    TEXT PRIMARY KEY,
            user       TEXT NOT NULL,
            role       TEXT NOT NULL,
            expiry     REAL NOT NULL,
            last       REAL NOT NULL,
            csrf       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- When the user last looked at an agent, so the supervisor can tell
        -- "produced output you have not seen" from "finished a while ago".
        -- Its own table rather than a chats column because it also has to
        -- cover CLI sessions, which are files on disk and have no chats row.
        CREATE TABLE IF NOT EXISTS read_marks (
            owner_id TEXT NOT NULL,
            kind     TEXT NOT NULL,   -- 'chat' | 'session'
            ref_id   TEXT NOT NULL,   -- chat id or Claude session id
            read_at  TEXT NOT NULL,
            -- Set only by an explicit "clear". Opening an agent marks it read,
            -- which retires routine updates but deliberately leaves an
            -- unanswered question listed; dismissing is the considered act
            -- that also silences those.
            dismissed_at TEXT,
            PRIMARY KEY (owner_id, kind, ref_id)
        );

        -- One row per model per completed turn, from Claude Code's `result`
        -- frame. provider is denormalised here so the cost-display rule
        -- survives the machine later being edited, renamed, or deleted.
        CREATE TABLE IF NOT EXISTS usage_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id               TEXT NOT NULL,
            owner_id              TEXT NOT NULL,
            model                 TEXT NOT NULL,
            provider              TEXT NOT NULL DEFAULT 'proxy',
            input_tokens          INTEGER NOT NULL DEFAULT 0,
            output_tokens         INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd              REAL,
            -- The CLI's own view of whether cost_usd means anything
            -- ('unknown' for third-party models). Explains a suppressed
            -- cost; never decides it.
            cost_basis            TEXT,
            -- Set for turns that ran in a terminal; chat_id is empty for those.
            session_id            TEXT,
            duration_ms           INTEGER,
            is_error              INTEGER NOT NULL DEFAULT 0,
            created_at            TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_owner_time
            ON usage_events(owner_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_usage_owner_model
            ON usage_events(owner_id, model);

        -- Prompts sent while that conversation already had a turn running.
        -- Persisted rather than held in memory because a queued prompt has to
        -- survive a reload: the point of the feature is that the user can walk
        -- away after sending.
        CREATE TABLE IF NOT EXISTS turn_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT NOT NULL,
            owner_id   TEXT NOT NULL,
            prompt     TEXT NOT NULL,
            model      TEXT,
            -- 'pending' is next in line; 'held' means the turn ahead of it
            -- failed, so it waits for the user to send or discard it rather
            -- than firing into a conversation that just broke.
            state      TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_queue_chat ON turn_queue(chat_id, id);

        -- A request made in the website that was typed into a live terminal
        -- instead of run here. The work happens in that terminal's process and
        -- lands in its transcript, so without this the tokens are imported as
        -- ordinary terminal usage and the person who asked disappears from the
        -- record. `from_offset` is the transcript's length at the moment of
        -- typing: everything appended after it belongs to this request.
        CREATE TABLE IF NOT EXISTS routed_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            chat_id     TEXT NOT NULL,
            owner_id    TEXT NOT NULL,
            from_offset INTEGER NOT NULL,
            -- What was asked. Attribution matches on this rather than on the
            -- byte offset alone: input typed into a busy session is queued, so
            -- the work can begin long afterwards, and everything the agent did
            -- in between belongs to whatever it was already doing.
            prompt      TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_routed_session
            ON routed_requests(session_id, from_offset);

        -- Supervisor orchestration tables (0.9.0).
        CREATE TABLE IF NOT EXISTS supervisors (
            id               TEXT PRIMARY KEY,
            title            TEXT NOT NULL DEFAULT 'New Supervisor',
            description      TEXT,
            config           TEXT NOT NULL DEFAULT '{}',  -- JSON: model routing rules, system prompt overrides
            owner_id         TEXT NOT NULL DEFAULT 'admin',
            status           TEXT NOT NULL DEFAULT 'idle',  -- idle|planning|running|paused|done|error
            plan             TEXT,  -- structured plan extracted during planning phase
            progress_pct     REAL NOT NULL DEFAULT 0.0,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL DEFAULT '',
            completed_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS supervisor_tasks (
            id              TEXT PRIMARY KEY,
            supervisor_id   TEXT NOT NULL REFERENCES supervisors(id),
            title           TEXT NOT NULL,
            description     TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',  -- pending|ready|running|done|failed|blocked
            model           TEXT,  -- assigned model (model routing result)
            result          TEXT,  -- final output/result
            progress_pct    REAL NOT NULL DEFAULT 0.0,
            parent_task_id  TEXT REFERENCES supervisor_tasks(id),
            depends_on      TEXT,  -- JSON array of task ids this task depends on
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sup_tasks_super ON supervisor_tasks(supervisor_id);

        CREATE TABLE IF NOT EXISTS supervisor_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            supervisor_id TEXT NOT NULL REFERENCES supervisors(id),
            role          TEXT NOT NULL,  -- 'user'|'supervisor'|'agent'|'system'
            content       TEXT NOT NULL,
            metadata      TEXT,  -- JSON: task_id, agent_name, etc.
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sup_msgs_super ON supervisor_messages(supervisor_id, id);

        -- Conversations and agents a supervisor watches. Separate from
        -- supervisor_tasks on purpose: a task is work the supervisor invented
        -- and runs headless, a member is work that already existed and belongs
        -- to someone. Coupling them would have made adding an agent imply
        -- handing it a task.
        --
        -- chat_id, with no "kind" column, because a live CLI agent is adopted
        -- into a conversation when it is added. That leaves one member type
        -- rather than two, so prompt, stop, transcript sync and per-conversation
        -- routing all apply to a supervised agent without a second code path.
        --
        -- The composite key is what makes adding an existing member a no-op
        -- rather than a duplicate row, and the pair is deliberately many-to-many:
        -- one agent can serve two supervisors at once.
        CREATE TABLE IF NOT EXISTS supervisor_members (
            supervisor_id TEXT NOT NULL REFERENCES supervisors(id),
            chat_id       TEXT NOT NULL,
            added_at      TEXT NOT NULL,
            PRIMARY KEY (supervisor_id, chat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sup_members_chat ON supervisor_members(chat_id);

        -- Host resource samples for the Server statistics page. No owner_id:
        -- these describe the machine, not a user, and there is exactly one
        -- machine. An autoincrement key rather than the timestamp, because two
        -- samples can share a second after a clock step and a PRIMARY KEY on
        -- created_at would make the second one an error.
        CREATE TABLE IF NOT EXISTS system_samples (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            cpu_pct       REAL NOT NULL DEFAULT 0,
            mem_pct       REAL NOT NULL DEFAULT 0,
            mem_used      INTEGER NOT NULL DEFAULT 0,
            mem_total     INTEGER NOT NULL DEFAULT 0,
            swap_pct      REAL NOT NULL DEFAULT 0,
            disk_pct      REAL NOT NULL DEFAULT 0,
            disk_used     INTEGER NOT NULL DEFAULT 0,
            disk_total    INTEGER NOT NULL DEFAULT 0,
            load1         REAL NOT NULL DEFAULT 0,
            load5         REAL NOT NULL DEFAULT 0,
            load15        REAL NOT NULL DEFAULT 0,
            proc_rss      INTEGER NOT NULL DEFAULT 0,
            proc_cpu_pct  REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_system_samples_at ON system_samples(created_at);
    """)
    await _ensure_chat_columns()
    await _ensure_usage_columns()
    await db_conn.commit()
    # Bounded growth without a scheduler: one indexed DELETE per startup.
    await usage_prune(config.USAGE_RETENTION_DAYS)
    await system_prune(config.SYSTEM_RETENTION_DAYS)


async def _ensure_chat_columns() -> None:
    """Apply additive chat schema migrations for existing databases."""
    cursor = await db_conn.execute("PRAGMA table_info(chats)")
    columns = {row["name"] for row in await cursor.fetchall()}
    migrations = {
        "pinned": "ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        "pinned_at": "ALTER TABLE chats ADD COLUMN pinned_at TEXT",
        "deleted_at": "ALTER TABLE chats ADD COLUMN deleted_at TEXT",
        "model": "ALTER TABLE chats ADD COLUMN model TEXT",
        "ai_machine_id": "ALTER TABLE chats ADD COLUMN ai_machine_id TEXT",
        "position": "ALTER TABLE chats ADD COLUMN position INTEGER",
        # Byte position already consumed from the linked CLI transcript. The
        # sync reads from here rather than re-reading the file, which matters:
        # a working transcript is tens of megabytes and this is polled.
        "transcript_offset": (
            "ALTER TABLE chats ADD COLUMN transcript_offset INTEGER NOT NULL DEFAULT 0"
        ),
        "supervisor": "ALTER TABLE chats ADD COLUMN supervisor TEXT",
    }
    for name, sql in migrations.items():
        if name not in columns:
            await db_conn.execute(sql)

    # Migrate ai_machines table for existing databases
    try:
        ma_cursor = await db_conn.execute("PRAGMA table_info(ai_machines)")
        ma_columns = {row["name"] for row in await ma_cursor.fetchall()}
    except Exception:  # noqa: BLE001 -- PRAGMA can fail on new tables
        ma_columns = set()
    if "owner_id" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'"
        )
    # usage_events gained cost_basis after first release.
    try:
        ue_cursor = await db_conn.execute("PRAGMA table_info(usage_events)")
        ue_columns = {row["name"] for row in await ue_cursor.fetchall()}
    except Exception:  # noqa: BLE001 -- PRAGMA can fail on new tables
        ue_columns = set()
    if ue_columns and "cost_basis" not in ue_columns:
        await db_conn.execute("ALTER TABLE usage_events ADD COLUMN cost_basis TEXT")
    if ue_columns and "session_id" not in ue_columns:
        # Terminal turns have no chat. usage_recent LEFT JOINs chat_id against
        # chats, so borrowing that column for a session id joins nothing and
        # renders a blank title beside real numbers.
        await db_conn.execute("ALTER TABLE usage_events ADD COLUMN session_id TEXT")

    if ma_columns and "provider" not in ma_columns:
        # Existing rows are all claude_proxy hosts -- the default matches them.
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN provider TEXT NOT NULL DEFAULT 'proxy'"
        )
    try:
        rm_cursor = await db_conn.execute("PRAGMA table_info(read_marks)")
        rm_columns = {row["name"] for row in await rm_cursor.fetchall()}
    except Exception:  # noqa: BLE001 -- PRAGMA can fail on a new table
        rm_columns = set()
    if rm_columns and "dismissed_at" not in rm_columns:
        await db_conn.execute("ALTER TABLE read_marks ADD COLUMN dismissed_at TEXT")

    if ma_columns and "active_models" not in ma_columns:
        # '[]' means "offer everything served", which is what existing rows did.
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN active_models TEXT NOT NULL DEFAULT '[]'"
        )

    await db_conn.commit()


async def close() -> None:
    global db_conn
    if db_conn:
        await db_conn.close()
        db_conn = None


# ── Chat CRUD ──────────────────────────────────────────────────────────────────────────


_CHAT_COLUMNS = (
    "id, title, description, session_id, work_dir, owner_id, created_at, "
    "updated_at, archived, pinned, pinned_at, position, deleted_at, model, ai_machine_id, "
    "transcript_offset"
)
_ALLOWED_CHAT_FIELDS = {
    "title",
    "description",
    "archived",
    "pinned",
    "pinned_at",
    "model",
    "ai_machine_id",
}


async def chat_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
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
    cur = await db_conn.execute(
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
    owner_id: str = "admin",
) -> str:
    now = _now()
    await db_conn.execute(
        "INSERT INTO chats (id, title, description, work_dir, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, title, description, work_dir, owner_id, now, now),
    )
    await db_conn.commit()
    return now


async def chat_update(chat_id: str, owner_id: str, **fields: Any) -> bool:
    if not fields or not set(fields).issubset(_ALLOWED_CHAT_FIELDS):
        return False
    if "pinned" in fields and "pinned_at" not in fields:
        fields["pinned_at"] = _now() if fields["pinned"] else None
    sets = ", ".join(k + " = ?" for k in fields)
    sets += ", updated_at = ?"
    sql = (
        "UPDATE chats SET "
        + sets
        + " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"
    )  # nosec B608: fields are allowlisted
    vals = list(fields.values()) + [_now(), chat_id, owner_id]
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
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
        await db_conn.execute("BEGIN")
        placed = 0
        for index, chat_id in enumerate(chat_ids):
            cur = await db_conn.execute(
                "UPDATE chats SET position = ? "
                "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                    (index, chat_id, owner_id),
            )
            placed += cur.rowcount
        await db_conn.commit()
    except Exception:
        await db_conn.rollback()
        raise
    return placed


async def chats_clear_order(owner_id: str) -> int:
    """Unplace every conversation, returning the list to pure recency order."""
    cur = await db_conn.execute(
        "UPDATE chats SET position = NULL "
        "WHERE owner_id = ? AND position IS NOT NULL",
        (owner_id,),
    )
    await db_conn.commit()
    return cur.rowcount


async def chat_archive(chat_id: str, owner_id: str, archived: int = 1) -> bool:
    return await chat_update(chat_id, owner_id, archived=archived)


async def chat_delete(chat_id: str, owner_id: str) -> bool:
    """Delete an owned conversation and messages while preserving its workspace."""
    cur = await db_conn.execute(
        "SELECT id FROM chats WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (chat_id, owner_id),
    )
    if await cur.fetchone() is None:
        return False
    # Capture the message ids first: once the rows are gone, an id lookup via
    # the messages table matches nothing and the index entries are orphaned.
    cur = await db_conn.execute(
        "SELECT id FROM messages WHERE chat_id = ?", (chat_id,)
    )
    msg_ids = [row["id"] for row in await cur.fetchall()]
    try:
        await db_conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db_conn.execute(
            "DELETE FROM chats WHERE id = ? AND owner_id = ?",
            (chat_id, owner_id),
        )
        await db_conn.commit()
    except Exception:
        await db_conn.rollback()
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

    new_chat_id = uuid.uuid4().hex
    now = _now()
    new_title = src["title"] + " (fork)"

    # Create a sibling workspace directory.
    work_dir = Path(src["work_dir"]).parent / f"{new_chat_id}_workspace"
    work_dir.mkdir(parents=True, exist_ok=True)

    await db_conn.execute(
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
    cur = await db_conn.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (src_chat_id,),
    )
    for row in await cur.fetchall():
        rows.append((row["role"], row["content"]))

    if rows:
        now2 = _now()
        for role, content in rows:
            await db_conn.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (new_chat_id, role, content, now2),
            )
        await db_conn.commit()
        # A fork is a bulk copy into a brand-new chat, so a per-chat rebuild
        # indexes exactly the rows just inserted.
        await _fts_rebuild(new_chat_id)

    return await chat_get(new_chat_id, owner_id)


async def chat_set_session(chat_id: str, session_id: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET session_id = ?, updated_at = ? WHERE id = ?",
        (session_id, _now(), chat_id),
    )
    await db_conn.commit()


async def chat_set_transcript_offset(chat_id: str, offset: int) -> None:
    """Record how far the linked transcript has been consumed.

    Deliberately does not touch updated_at: advancing the read position is
    bookkeeping, and letting it bump the timestamp would reorder the sidebar
    every few seconds while a chat is merely being polled.
    """
    await db_conn.execute(
        "UPDATE chats SET transcript_offset = ? WHERE id = ?",
        (int(offset), chat_id),
    )
    await db_conn.commit()


async def bump_chat_updated_at(chat_id: str) -> None:
    """Update the conversation's ``updated_at`` timestamp.

    Does not touch ``position``, so user-placed conversations keep their
    slot while the recency timestamp becomes accurate for the list view.
    """
    await db_conn.execute(
        "UPDATE chats SET updated_at = ? WHERE id = ?",
        (_now(), chat_id),
    )
    await db_conn.commit()


async def chat_set_model(chat_id: str, model: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET model = ?, updated_at = ? WHERE id = ?",
        (model, _now(), chat_id),
    )
    await db_conn.commit()


async def chat_set_title(chat_id: str, title: str) -> None:
    await db_conn.execute(
        "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), chat_id),
    )
    await db_conn.commit()


# ── Chat FTS5 Search ──────────────────────────────────────────────────────────────────

async def chat_search(owner_id: str, query: str) -> list[dict[str, Any]]:
    """Search message bodies using FTS5.

    Returns a list of unique chats that match *query*, ordered by
    FTS5 rank (best match first).  Each entry is a chat dict with an
    added ``snippet`` key showing the matching fragment.
    """
    # FTS5 MATCH query — values are parameterized, the MATCH keyword is SQL.
    rows: list[dict[str, Any]] = []
    try:
        cur = await db_conn.execute(
            "SELECT rowid FROM messages_fts "  # nosec B608: MATCH is SQL keyword
            "WHERE content MATCH ?",
            (query,),
        )
        match_ids = [row["rowid"] for row in await cur.fetchall()]
    except Exception:  # noqa: BLE001 -- FTS5 may not exist on fresh DBs
        match_ids = []

    if not match_ids:
        return []

    # Fetch full chat details for matching rows, deduplicate by chat_id.
    _placeholders = ",".join("?" for _ in match_ids)
    cur = await db_conn.execute(
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
    cur = await db_conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def messages_last(chat_id: str, count: int = 1) -> list[dict[str, Any]]:
    """Return the last *count* messages for a chat, ordered by insertion."""
    cur = await db_conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, count),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    rows.reverse()  # return in insertion order so index 0 is the oldest
    return rows


# ── FTS5 index maintenance ──────────────────────────────────────────────────────────

def _fts_connect() -> sqlite3.Connection:
    """Open the dedicated synchronous connection used for index maintenance.

    A separate connection avoids aiosqlite transaction conflicts. busy_timeout
    makes it wait for the writer lock instead of failing with "database is
    locked" the moment a turn is writing concurrently.
    """
    sync = sqlite3.connect(str(Path(config.DB_PATH)), check_same_thread=False)
    sync.execute("PRAGMA journal_mode=WAL")
    sync.execute("PRAGMA foreign_keys=ON")
    sync.execute(f"PRAGMA busy_timeout={_FTS_BUSY_TIMEOUT_MS}")
    return sync


def _fts_index_ids_sync(msg_ids: Sequence[int | None]) -> None:
    """Index exactly *msg_ids*, replacing any existing entries for them.

    Cost is proportional to len(msg_ids), not to the size of the conversation.
    Each message is indexed with its chat title prefixed so that title-based
    searches also surface through the index.
    """
    ids = [i for i in msg_ids if i is not None]
    if not ids:
        return
    sync = None
    try:
        sync = _fts_connect()
        marks = ",".join("?" for _ in ids)
        sync.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ({marks})",  # nosec B608
            ids,
        )
        rows = sync.execute(
            "SELECT m.id, m.content, c.title FROM messages m "
            "JOIN chats c ON c.id = m.chat_id "
            f"WHERE m.id IN ({marks})",  # nosec B608: generated placeholders
            ids,
        ).fetchall()
        for msg_id, content, title in rows:
            if content:
                text = f"{title} {content}" if title else content
                sync.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                    (msg_id, text),
                )
        sync.commit()
    except Exception:  # noqa: BLE001,S110 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


def _fts_forget_ids_sync(msg_ids: Sequence[int | None]) -> None:
    """Drop *msg_ids* from the index.

    Callers must capture the ids **before** deleting the message rows: a purge
    that resolves ids via the messages table after the fact matches nothing and
    leaves the entries orphaned.
    """
    ids = [i for i in msg_ids if i is not None]
    if not ids:
        return
    sync = None
    try:
        sync = _fts_connect()
        marks = ",".join("?" for _ in ids)
        sync.execute(
            f"DELETE FROM messages_fts WHERE rowid IN ({marks})",  # nosec B608
            ids,
        )
        sync.commit()
    except Exception:  # noqa: BLE001,S110 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


def _refresh_fts_sync(chat_id: str | None = None) -> None:
    """Full rebuild of the FTS5 index for one chat, or for every chat.

    Used for backfill and after a chat title changes (the title is baked into
    each indexed row). Prefer :func:`_fts_index_ids_sync` on the write path --
    this walks every message of the chat.
    """
    sync = None
    try:
        sync = _fts_connect()
        if chat_id:
            # Delete stale entries for this chat.
            sync.execute(
                "DELETE FROM messages_fts WHERE rowid IN "
                "(SELECT id FROM messages WHERE chat_id = ?)",
                (chat_id,),
            )
        else:
            sync.execute("DELETE FROM messages_fts")

        # Re-insert all message content, prefixed with chat title.
        rows = sync.execute(
            "SELECT m.id, m.content, c.title FROM messages m "
            "JOIN chats c ON c.id = m.chat_id"
            + (" AND m.chat_id = ?" if chat_id else "")
            + (" ORDER BY m.id ASC" if chat_id else ""),
            (chat_id,) if chat_id else (),
        ).fetchall()
        for msg_id, content, title in rows:
            if content:
                # Prefix with title so title searches also work.
                text = f"{title} {content}" if title else content
                sync.execute(
                    "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                    (msg_id, text),
                )
        sync.commit()
    except Exception:  # noqa: BLE001,S110 -- FTS5 may not exist, silent fail
        pass
    finally:
        if sync is not None:
            try:
                sync.close()
            except Exception:  # noqa: BLE001,S110
                pass


# ── Async wrappers: keep the blocking sqlite3 work off the event loop ──────────


async def _fts_index_ids(msg_ids: Sequence[int | None]) -> None:
    await asyncio.to_thread(_fts_index_ids_sync, list(msg_ids))


async def _fts_forget_ids(msg_ids: Sequence[int | None]) -> None:
    await asyncio.to_thread(_fts_forget_ids_sync, list(msg_ids))


async def _fts_rebuild(chat_id: str | None = None) -> None:
    await asyncio.to_thread(_refresh_fts_sync, chat_id)


async def messages_append(chat_id: str, role: str, content: str) -> int:
    cur = await db_conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, _now()),
    )
    await db_conn.commit()
    last_id = cur.lastrowid
    await _fts_index_ids([last_id])
    return last_id


async def messages_batch(chat_id: str, rows: list[tuple[str, str]]) -> list[int]:
    """Insert multiple messages atomically and return their exact IDs."""
    global _messages_batch_lock
    if not rows:
        return []
    if _messages_batch_lock is None:
        _messages_batch_lock = asyncio.Lock()
    async with _messages_batch_lock:
        ids = []
        try:
            await db_conn.execute("BEGIN")
            now = _now()
            for role, content in rows:
                cur = await db_conn.execute(
                    "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                    (chat_id, role, content, now),
                )
                ids.append(cur.lastrowid)
            await db_conn.commit()
        except Exception:
            await db_conn.rollback()
            raise
        await _fts_index_ids(ids)
        return ids


# ── Users ──────────────────────────────────────────────────────────────────────────────


async def user_get_by_name(name: str) -> dict[str, Any] | None:
    cur = await db_conn.execute(
        "SELECT id, email, name, password, role, created_at FROM users WHERE name = ?",
        (name,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def user_create(
    name: str, email: str | None, password: str, role: str = "admin"
) -> None:
    user_id = uuid.uuid4().hex
    now = _now()
    await db_conn.execute(
        "INSERT INTO users (id, name, email, password, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name, email, password, role, now),
    )
    await db_conn.commit()


# ── Application settings ────────────────────────────────────────────────────────────────


async def setting_get(key: str) -> str | None:
    cur = await db_conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


async def setting_set(key: str, value: str) -> None:
    await db_conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _now()),
    )
    await db_conn.commit()


# ── AI Machines ────────────────────────────────────────────────────────────────────────


async def ai_machine_active(owner_id: str) -> dict[str, Any] | None:
    """Return the owner's active machine without exposing its API key."""
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, active "
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machines_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        "created_at, updated_at "
        "FROM ai_machines WHERE owner_id = ? ORDER BY active DESC, name ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def ai_machine_get(id: str, owner_id: str) -> dict[str, Any] | None:
    cur = await db_conn.execute(
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        # Derived in SQL so callers can report whether a key is configured
        # without the value ever leaving this layer. Computing it from an
        # api_key this query deliberately omits made it always false.
        "CASE WHEN api_key IS NOT NULL AND TRIM(api_key) <> '' THEN 1 ELSE 0 END "
        "AS has_api_key, "
        "created_at, updated_at "
        "FROM ai_machines WHERE id = ? AND owner_id = ?",
        (id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machine_create(
    machine_id: str,
    name: str,
    host: str,
    port: int,
    api_key: str | None,
    model: str,
    base_url: str | None,
    description: str | None,
    owner_id: str,
    provider: str = "proxy",
) -> str:
    now = _now()
    await db_conn.execute(
        "INSERT INTO ai_machines "
        "(id, name, provider, host, port, api_key, model, base_url, description, active, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            machine_id,
            name,
            provider,
            host,
            port,
            api_key,
            model,
            base_url,
            description,
            owner_id,
            now,
            now,
        ),
    )
    await db_conn.commit()
    return now


async def ai_machine_update(
    machine_id: str,
    owner_id: str,
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    description: str | None = None,
    provider: str | None = None,
) -> bool:
    pairs: list[tuple[str, Any]] = [
        ("name", name),
        ("provider", provider),
        ("host", host),
        ("port", port),
        ("api_key", api_key),
        ("model", model),
        ("base_url", base_url),
        ("description", description),
    ]
    sets: list[str] = []
    vals: list[Any] = []
    for field, value in pairs:
        if value is not None:
            sets.append(f"{field} = ?")
            vals.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.extend([machine_id, owner_id])
    sql = (
        "UPDATE ai_machines SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?"
    )  # nosec B608: fields are allowlisted
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_activate(machine_id: str, owner_id: str) -> bool:
    """Deactivate all machines and activate the one requested."""
    try:
        await db_conn.execute("BEGIN")
        await db_conn.execute(
            "UPDATE ai_machines SET active = 0 WHERE owner_id = ?",
            (owner_id,),
        )
        cur = await db_conn.execute(
            "UPDATE ai_machines SET active = 1, updated_at = ? WHERE id = ? AND owner_id = ?",
            (_now(), machine_id, owner_id),
        )
        await db_conn.commit()
        return cur.rowcount > 0
    except Exception:
        await db_conn.rollback()
        raise


async def ai_machine_delete(machine_id: str, owner_id: str) -> bool:
    cur = await db_conn.execute(
        "DELETE FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    await db_conn.commit()
    return cur.rowcount > 0


async def chat_owner(chat_id: str) -> str | None:
    """Return the owner of *chat_id*, so the runner can resolve its backend."""
    cur = await db_conn.execute("SELECT owner_id FROM chats WHERE id = ?", (chat_id,))
    row = await cur.fetchone()
    return row["owner_id"] if row else None


_BACKEND_COLUMNS = (
    "id, name, provider, host, port, model, base_url, api_key"
)


async def ai_machine_backend(owner_id: str) -> dict[str, Any] | None:
    """Return the active machine *including* its API key, for the runner only.

    Every other reader goes through ``ai_machine_active``/``ai_machines_list``,
    which omit ``api_key`` so it cannot reach an API response by accident.
    """
    cur = await db_conn.execute(
        f"SELECT {_BACKEND_COLUMNS} "  # nosec B608: columns are static
        "FROM ai_machines WHERE owner_id = ? AND active = 1 LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ai_machine_backend_by_id(
    machine_id: str, owner_id: str
) -> dict[str, Any] | None:
    """Return one specific machine including its API key, for the runner only.

    Owner-scoped, so pinning a conversation to a machine id cannot reach another
    user's backend or its credential.
    """
    if not machine_id:
        return None
    cur = await db_conn.execute(
        f"SELECT {_BACKEND_COLUMNS} "  # nosec B608: columns are static
        "FROM ai_machines WHERE id = ? AND owner_id = ? LIMIT 1",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def chat_routing(chat_id: str) -> dict[str, Any]:
    """Resolve where a conversation's next turn should go.

    Returns ``{"owner", "model", "machine", "pinned"}``:

    * ``machine`` -- the conversation's own machine when ``ai_machine_id`` is
      set, otherwise the owner's active machine, which is what every
      conversation followed before pinning existed. A pin to a machine that has
      since been deleted falls back rather than failing the turn.
    * ``model`` -- the conversation's model column. This doubles as the pin and
      as the record of what actually ran: a turn writes back the model the CLI
      reports, so if a gateway substitutes a different model the conversation
      reflects the truth rather than a stale intention.
    * ``pinned`` -- whether the machine came from the pin or the active fallback.
    """
    cur = await db_conn.execute(
        "SELECT owner_id, model, ai_machine_id FROM chats WHERE id = ?", (chat_id,)
    )
    row = await cur.fetchone()
    if not row:
        return {"owner": None, "model": None, "machine": None, "pinned": False}
    owner = row["owner_id"]
    if row["ai_machine_id"]:
        machine = await ai_machine_backend_by_id(row["ai_machine_id"], owner)
        if machine:
            return {"owner": owner, "model": row["model"],
                    "machine": machine, "pinned": True}
    return {
        "owner": owner,
        "model": row["model"],
        "machine": await ai_machine_backend(owner),
        "pinned": False,
    }


async def chat_set_machine(chat_id: str, owner_id: str, machine_id: str | None) -> bool:
    """Pin a conversation to a machine, or clear the pin with None."""
    cur = await db_conn.execute(
        "UPDATE chats SET ai_machine_id = ?, updated_at = ? "
        "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
        (machine_id or None, _now(), chat_id, owner_id),
    )
    await db_conn.commit()
    return cur.rowcount > 0


async def read_marks_get(owner_id: str) -> dict[tuple[str, str], dict[str, str]]:
    """Return {(kind, ref_id): {"read_at": ..., "dismissed_at": ...}}."""
    cur = await db_conn.execute(
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
    """Record that *ref_id* has been looked at, and return the timestamp used.

    With *dismiss* the same timestamp is also written to dismissed_at, which
    additionally silences an unanswered question. Reading alone never does.
    """
    stamp = read_at or _now()
    dismissed = stamp if dismiss else None
    await db_conn.execute(
        "INSERT INTO read_marks (owner_id, kind, ref_id, read_at, dismissed_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(owner_id, kind, ref_id) DO UPDATE SET "
        "  read_at = excluded.read_at, "
        # Never unset an existing dismissal by merely reading it again.
        "  dismissed_at = COALESCE(excluded.dismissed_at, read_marks.dismissed_at)",
        (owner_id, kind, ref_id, stamp, dismissed),
    )
    await db_conn.commit()
    return stamp


async def chat_last_activity(owner_id: str) -> dict[str, dict[str, Any]]:
    """Latest message per chat: {chat_id: {role, created_at, preview}}.

    One grouped query rather than a read per conversation -- the supervisor
    polls, so this runs repeatedly.
    """
    cur = await db_conn.execute(
        "SELECT m.chat_id, m.role, m.created_at, substr(m.content, 1, 200) AS preview "
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
        }
        for r in await cur.fetchall()
    }


def parse_active_models(raw: Any) -> list[str]:
    """Decode the active_models column into a list of ids.

    Anything unreadable decodes to empty, which means "offer everything the
    backend serves" -- the safe direction, because the alternative is a picker
    with nothing in it.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        entries = raw
    else:
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(entries, list):
        return []
    seen: set[str] = set()
    models: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip() and entry.strip() not in seen:
            seen.add(entry.strip())
            models.append(entry.strip()[:200])
    return models


async def ai_machine_set_models(
    machine_id: str, owner_id: str, active: list[str], default: str | None
) -> bool:
    """Set which models a machine offers, and which one it defaults to."""
    sets = ["active_models = ?", "updated_at = ?"]
    vals: list[Any] = [json.dumps(active), _now()]
    if default:
        sets.insert(1, "model = ?")
        vals.insert(1, default)
    cur = await db_conn.execute(
        "UPDATE ai_machines SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?",
        [*vals, machine_id, owner_id],
    )  # nosec B608: column names are literals, values parameterised
    await db_conn.commit()
    return cur.rowcount > 0


async def ai_machine_api_key(machine_id: str, owner_id: str) -> str | None:
    """Return one machine's API key. Kept separate from ai_machine_get so the
    key is only ever fetched where it is deliberately needed."""
    cur = await db_conn.execute(
        "SELECT api_key FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    return row["api_key"] if row else None


async def ai_machine_seed_anthropic(owner_id: str) -> str | None:
    """Ensure the owner has an Anthropic API machine, and return its id.

    Claude Code's native backend is the official API, so every account gets an
    entry for it. Seeded inactive and with no API key: an unset key makes the
    CLI fall back to the host's own login, which is the normal setup.
    """
    cur = await db_conn.execute(
        "SELECT id FROM ai_machines WHERE owner_id = ? AND provider = 'anthropic' "
        "LIMIT 1",
        (owner_id,),
    )
    row = await cur.fetchone()
    if row:
        return row["id"]
    machine_id = uuid.uuid4().hex
    await ai_machine_create(
        machine_id,
        "Anthropic API",
        _ANTHROPIC_HOST,
        443,
        None,
        config.ANTHROPIC_MODEL,
        config.ANTHROPIC_BASE_URL,
        "Official Anthropic API — Claude Code's default backend.",
        owner_id,
        provider="anthropic",
    )
    return machine_id


# ── Usage accounting ───────────────────────────────────────────────────────────────────


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
        # Silent rejection here is how an empty Usage tab looks from the
        # outside: the turn succeeds, nothing is written, nothing is said.
        _log.warning(
            "usage_record_rejected: chat_id=%r owner_id=%r model=%r "
            "(all three are required to attribute a row)",
            chat_id, owner_id, model,
        )
        return None
    try:
        cur = await db_conn.execute(
            "INSERT INTO usage_events "
            "(chat_id, owner_id, model, provider, input_tokens, output_tokens, "
            " cache_read_tokens, cache_creation_tokens, cost_usd, cost_basis, "
            " duration_ms, is_error, created_at, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                owner_id,
                model,
                provider or "proxy",
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(cache_read_tokens or 0),
                int(cache_creation_tokens or 0),
                cost_usd,
                cost_basis,
                duration_ms,
                1 if is_error else 0,
                _now(),
                origin or "web",
            ),
        )
        await db_conn.commit()
        _log.debug(
            "usage_recorded chat_id=%s model=%s provider=%s in=%s out=%s",
            chat_id, model, provider, input_tokens, output_tokens,
        )
        return cur.lastrowid
    except Exception as exc:  # noqa: BLE001 -- never fail a turn that already succeeded
        # Swallowed so accounting cannot break a completed turn, but a write
        # that fails on every turn must not also be invisible.
        _log.error(
            "usage_record_failed: chat_id=%s model=%s provider=%s: %s",
            chat_id, model, provider, exc,
        )
        return None


# Rows per transaction when importing terminal usage.
USAGE_IMPORT_BATCH = 500


async def usage_cursor_get(session_id: str) -> int:
    """How far a transcript has been consumed for usage accounting."""
    cur = await db_conn.execute(
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
            await db_conn.execute(
                "INSERT INTO usage_cursors (session_id, offset) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET offset = excluded.offset",
                (session_id, int(offset)),
            )
            await db_conn.commit()
        return 0
    # Committed in batches: a first import can be tens of thousands of rows, and
    # holding SQLite's writer lock for all of them starves every other writer --
    # db.py opens with a 5s busy timeout, so a long hold surfaces as a failed
    # request somewhere else entirely.
    # Read once, applied per row: a request the operator made in the website but
    # which ran in this terminal must not be filed as the terminal's own work.
    markers = await routed_markers(session_id)
    written = 0
    for start in range(0, len(rows), USAGE_IMPORT_BATCH):
        batch = rows[start : start + USAGE_IMPORT_BATCH]
        last = start + USAGE_IMPORT_BATCH >= len(rows)
        # Each batch advances the cursor to its own last row, so an interruption
        # leaves the cursor exactly at what was committed: the next run resumes
        # from there, counting nothing twice and skipping nothing. Only the
        # final batch may move it past the last usage row, up to the end of the
        # data actually read.
        checkpoint = int(offset) if last else int(batch[-1].get("offset") or offset)
        try:
            await db_conn.execute("BEGIN")
            for row in batch:
                routed = routed_owner_of(
                    markers,
                    int(row.get("offset") or 0),
                    str(row.get("timestamp") or ""),
                    str(row.get("after_prompt") or ""),
                )
                await db_conn.execute(
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
                        row.get("timestamp") or _now(),
                        # Requested in the website, executed in a terminal.
                        # Neither plain label is true, so it gets its own.
                        "web-routed" if routed else "terminal",
                        1 if row.get("context_unsplit") else 0,
                    ),
                )
            await db_conn.execute(
                "INSERT INTO usage_cursors (session_id, offset) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET offset = excluded.offset",
                (session_id, checkpoint),
            )
            await db_conn.commit()
            written += len(batch)
        except Exception:
            await db_conn.rollback()
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
    """
    cursor = await db_conn.execute("PRAGMA table_info(usage_events)")
    columns = {row["name"] for row in await cursor.fetchall()}
    routed = await db_conn.execute("PRAGMA table_info(routed_requests)")
    routed_columns = {row["name"] for row in await routed.fetchall()}
    if routed_columns and "prompt" not in routed_columns:
        await db_conn.execute(
            "ALTER TABLE routed_requests ADD COLUMN prompt TEXT NOT NULL DEFAULT ''"
        )
        await db_conn.commit()
    migrations = {
        "origin": "ALTER TABLE usage_events ADD COLUMN origin TEXT NOT NULL DEFAULT ''",
        "context_unsplit":
            "ALTER TABLE usage_events ADD COLUMN context_unsplit "
            "INTEGER NOT NULL DEFAULT 0",
    }
    added = False
    for column, statement in migrations.items():
        if column not in columns:
            await db_conn.execute(statement)
            added = True
    if added:
        await db_conn.commit()
    # Backfill only rows that predate the column, using the rule that produced
    # them. Bounded by origin = '' so it runs once and never re-labels a row
    # that was written with an explicit origin.
    await db_conn.execute(
        "UPDATE usage_events SET origin = "
        "CASE WHEN session_id IS NOT NULL AND TRIM(session_id) <> '' "
        "     THEN 'terminal' ELSE 'web' END "
        "WHERE origin = ''"
    )
    # Same for the cache split: a historic row with no cache line at all and a
    # large input is context that was never broken out.
    await db_conn.execute(
        "UPDATE usage_events SET context_unsplit = 1 "
        "WHERE context_unsplit = 0 AND origin = 'terminal' "
        "  AND cache_read_tokens = 0 AND cache_creation_tokens = 0 "
        "  AND input_tokens > 8000"
    )
    await db_conn.commit()


# How long a routed request keeps claiming the turns that follow it. There is no
# end marker in a transcript -- the terminal simply carries on -- so attribution
# is bounded by time rather than left open, and work typed directly into that
# terminal an hour later is not credited to a web request.
# Backstop only, now that ownership is matched on the prompt itself: a
# session that never receives another prompt cannot leave a marker
# claiming turns indefinitely. Generous, because queued input can wait.
ROUTED_WINDOW_S: Final[int] = 6 * 3600


def normalise_prompt(text: str) -> str:
    """A prompt reduced to something comparable across the two records of it.

    The console holds what it sent; the transcript holds what the CLI received.
    Whitespace and length differ, so both sides are folded the same way and
    compared on a bounded prefix.
    """
    return " ".join((text or "").split())[:200].casefold()


async def routed_request_add(
    session_id: str, chat_id: str, owner_id: str, from_offset: int,
    prompt: str = "",
) -> int | None:
    """Mark that a website request was typed into *session_id*'s terminal."""
    if not session_id or not chat_id:
        return None
    try:
        cur = await db_conn.execute(
            "INSERT INTO routed_requests "
            "(session_id, chat_id, owner_id, from_offset, prompt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, chat_id, owner_id, int(from_offset or 0),
             (prompt or "")[:4000], _now()),
        )
        await db_conn.commit()
        return cur.lastrowid
    except Exception:  # noqa: BLE001 -- attribution must never break a request
        _log.warning("routed_request_not_recorded session_id=%s", session_id)
        return None


async def routed_markers(session_id: str) -> list[dict[str, Any]]:
    """Routed-request marks for a session, newest offset first.

    Read once per import rather than queried per row: a first import is tens of
    thousands of rows and a lookup each would be the slowest thing in it.
    """
    try:
        cur = await db_conn.execute(
            "SELECT chat_id, from_offset, prompt, created_at FROM routed_requests "
            "WHERE session_id = ? ORDER BY from_offset DESC",
            (session_id,),
        )
        return [dict(row) for row in await cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def routed_owner_of(
    markers: list[dict[str, Any]], offset: int, when: str, after_prompt: str = ""
) -> dict[str, Any] | None:
    """The routed request a transcript row belongs to, if any.

    Matched on the prompt the session was working on when the turn ran. That is
    the only signal that survives queueing: a byte offset says a turn came after
    the request was typed, which is not the same as being caused by it. Typing
    into a busy session queues the input, and the first version of this credited
    a routed request with whatever the agent happened to be doing in the
    meantime -- observed, not theoretical.

    The offset remains as a sanity check, so a marker cannot claim turns that
    predate it. The time window is now only a backstop against a session that
    never receives another prompt.
    """
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

    The distinction people actually want, and the one the page was getting
    wrong. Terminal turns are dominated by agent sessions the console adopted
    -- 175 million tokens for one of them on this machine against 271 thousand
    typed into the website the same day -- so presenting a single figure, or
    labelling the agents' work as the operator's, describes nobody's day.
    """
    where = "WHERE owner_id = ?"
    params: list[Any] = [owner_id]
    if days:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db_conn.execute(
        "SELECT COALESCE(NULLIF(origin, ''), 'web') AS origin, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
        # Reported separately rather than folded in: these rows count the whole
        # conversation on every turn, so adding them to the others produces a
        # number that means nothing.
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
    """Terminal usage per session, named by the conversation it belongs to.

    This is what makes a surprising total explainable. One bar labelled
    "terminal" hides which session spent it; named rows show at a glance that
    the consumption belongs to an agent session rather than to anything the
    operator typed.
    """
    where = "WHERE u.owner_id = ? AND u.origin = 'terminal'"
    params: list[Any] = [owner_id]
    if days:
        where += " AND u.created_at >= ?"
        params.append(_cutoff(days))
    cur = await db_conn.execute(
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
    cur = await db_conn.execute(
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
    cur = await db_conn.execute(
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
    """Most recent turns, with the conversation title joined in.

    LEFT JOIN so a deleted conversation still appears in the log rather than
    silently dropping the usage it accounted for.
    """
    cur = await db_conn.execute(
        "SELECT u.created_at, u.chat_id, u.model, u.provider, u.input_tokens, "
        "u.output_tokens, u.cost_usd, u.cost_basis, u.duration_ms, u.is_error, "
        # A terminal turn has no chat to join, so fall back to its session.
        "COALESCE(c.title, 'Terminal ' || substr(u.session_id, 1, 8)) AS chat_title "
        "FROM usage_events u LEFT JOIN chats c ON c.id = u.chat_id "
        "WHERE u.owner_id = ? ORDER BY u.id DESC LIMIT ?",
        # `limit or 50` would read 0 as "use the default", disagreeing with the
        # API layer which clamps 0 to 1. Only None means "unspecified".
        (owner_id, max(1, min(50 if limit is None else int(limit), 500))),
    )
    return [dict(row) for row in await cur.fetchall()]


# Buckets the statistics page offers, as (label, prefix length of the ISO
# timestamp). Bucketing by string prefix rather than a date function is what
# lets one query serve both timestamp shapes in this table: turns recorded by
# the site are stored as "...:15Z" and turns read out of a CLI transcript keep
# their original "...:55.776Z" milliseconds. substr() does not care; strftime()
# would have to parse, and returns NULL on the millisecond form.
USAGE_BUCKETS: Final[dict[str, int]] = {
    # Prefix widths over an ISO timestamp: 2026-08-30T11:04:11Z
    "halfhour": 16,  # not a prefix -- see _bucket_expr
    "hour": 13,
    "day": 10,
    "month": 7,
}


# Timestamps are stored in UTC, but nobody reads a chart in UTC -- an hour of
# work done at 21:00 in Lisbon was labelled 20:00, and the "today" column began
# at 01:00. Bucketing therefore groups on the local rendering of the timestamp.
#
# SQLite's 'localtime' resolves the machine's zone per timestamp, so it follows
# DST rather than baking in one offset: the same expression gives +01:00 for an
# August row and +00:00 for a January one, which a fixed offset could not do.
# It returns "2026-08-30 20:23:54"; the space becomes "T" so the keys keep the
# shape the client and the existing bucket widths already expect.
#
# Deliberately not applied to the `created_at >= ?` range filters. "The last 24
# hours" is a span measured back from now, and a span has no timezone -- only
# the labels do.
_LOCAL_TS: Final[str] = "replace(datetime(created_at, 'localtime'), ' ', 'T')"


def _bucket_expr(bucket: str) -> tuple[str, list[Any]]:
    """SQL mapping ``created_at`` to a local-time bucket key, and its params.

    Every other bucket is a prefix of the timestamp, which SQLite can take with
    a single substr. A half hour is not a prefix -- it needs the minute floored
    to 00 or 30 -- so it gets its own expression rather than bending the widths
    to fit. The key stays lexicographically sortable like the others, which is
    what lets the caller keep ``ORDER BY bucket``.

    The prefix widths are unchanged by the conversion: local and UTC renderings
    are the same length with the field boundaries in the same places.
    """
    if bucket == "halfhour":
        halfhour = (
            f"substr({_LOCAL_TS}, 1, 14) || "
            f"CASE WHEN CAST(substr({_LOCAL_TS}, 15, 2) AS INTEGER) < 30 "
            "THEN '00' ELSE '30' END"
        )
        return (halfhour, [])
    return (
        f"substr({_LOCAL_TS}, 1, ?)",
        [USAGE_BUCKETS.get(bucket, USAGE_BUCKETS["day"])],
    )


async def usage_series(
    owner_id: str, days: int | None = 30, bucket: str = "day"
) -> list[dict[str, Any]]:
    """Token totals per time bucket per provider, oldest first.

    Split by provider rather than summed because the two populations differ by
    three orders of magnitude on this machine -- 18,200 terminal turns against
    23 from the website. Stacked into one series the website's traffic is a
    flat line on the axis, which is worse than not charting it.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [*expr_params, owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db_conn.execute(
        # `origin` is carried alongside `provider` rather than replacing it, so
        # the existing series keep working. provider='cli' had been standing in
        # for "a terminal", which is true but coarse: it lumps adopted agent
        # sessions in with anything hand-typed, and those differ by three orders
        # of magnitude. `unsplit_tokens` is broken out for the same reason cost
        # is suppressed elsewhere -- rows whose model reported no cache
        # breakdown re-count the whole conversation every turn, so plotting them
        # in the same stack as the rest is not a comparison.
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
    """Token totals per time bucket per model, for the top *top* models.

    Capped because a categorical palette is only defined for a fixed number of
    slots; the rest is folded into one "Other" series by the caller rather than
    given an invented colour.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [owner_id]
    where = "owner_id = ?"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    ranked = await db_conn.execute(
        "SELECT model FROM usage_events "
        f"WHERE {where} "  # nosec B608: clause is static
        "GROUP BY model ORDER BY SUM(input_tokens + output_tokens) DESC "
        "LIMIT ?",
        [*params, max(1, min(int(top), 12))],
    )
    keep = [row["model"] for row in await ranked.fetchall()]
    if not keep:
        return []
    cur = await db_conn.execute(
        f"SELECT {expr} AS bucket, model, "  # nosec B608: expression is ours
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens "
        f"FROM usage_events WHERE {where} "  # nosec B608: clause is static
        "GROUP BY bucket, model ORDER BY bucket ASC",
        [*expr_params, *params],
    )
    kept = set(keep)
    # Everything outside the top N collapses into one "Other" series. Merged
    # rather than relabelled: two dropped models in the same bucket produce two
    # rows, and leaving both as "Other" would draw that bucket twice and make
    # the fold look like a spike.
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
        cur = await db_conn.execute(
            "DELETE FROM usage_events WHERE created_at < ?", (_cutoff(days),)
        )
        await db_conn.commit()
        return cur.rowcount or 0
    except Exception:  # noqa: BLE001 -- pruning must never block startup
        return 0


# ── Host statistics ─────────────────────────────────────────────────────────────────────
# Samples of the machine WebConsole runs on, written by the background sampler
# in sysstats.py. Not owner-scoped: there is one host and it belongs to nobody.

# Columns the sampler writes, in the order the INSERT expects them. Named once
# so the insert, the aggregate and the tests cannot drift apart.
SYSTEM_FIELDS: Final[tuple[str, ...]] = (
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


async def system_sample_insert(values: dict[str, Any]) -> None:
    """Store one host sample. Missing fields default to 0."""
    columns = ", ".join(("created_at", *SYSTEM_FIELDS))
    placeholders = ", ".join("?" * (len(SYSTEM_FIELDS) + 1))
    await db_conn.execute(
        f"INSERT INTO system_samples ({columns}) "  # nosec B608: names are literals
        f"VALUES ({placeholders})",
        [_now(), *(values.get(field, 0) or 0 for field in SYSTEM_FIELDS)],
    )
    await db_conn.commit()


async def system_latest() -> dict[str, Any] | None:
    """The most recent stored sample, or None when nothing has been sampled."""
    cur = await db_conn.execute(
        "SELECT * FROM system_samples ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def system_series(
    days: int | None = 7, bucket: str = "hour"
) -> list[dict[str, Any]]:
    """Host samples averaged per time bucket, oldest first.

    Both the average and the peak are returned for the three figures where the
    difference matters. A box that sat at 4% CPU and spiked to 100% for ninety
    seconds averages out to nothing at an hour bucket -- the average says the
    machine was idle, and the peak is the only column that remembers the spike
    happened at all.

    Bucketing goes through _bucket_expr() rather than USAGE_BUCKETS, because
    'halfhour' is not a prefix width: its entry in that dict is a sentinel, and
    reading it as a substr length silently buckets by the minute instead.
    """
    expr, expr_params = _bucket_expr(bucket)
    params: list[Any] = [*expr_params]
    where = "1=1"
    if days is not None:
        where += " AND created_at >= ?"
        params.append(_cutoff(days))
    cur = await db_conn.execute(
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
    return [dict(row) for row in await cur.fetchall()]


async def system_prune(days: int) -> int:
    """Delete samples older than *days*. Returns the number removed."""
    if not days or days <= 0:
        return 0
    try:
        cur = await db_conn.execute(
            "DELETE FROM system_samples WHERE created_at < ?", (_cutoff(days),)
        )
        await db_conn.commit()
        return cur.rowcount or 0
    except Exception:  # noqa: BLE001 -- pruning must never block startup
        return 0


# ── Queued prompts ──────────────────────────────────────────────────────────────────────
# A prompt sent while that conversation already had a turn running. Owner-scoped
# throughout: a queue entry is as private as the conversation it belongs to.

# Per-conversation cap. A queue only drains when a turn finishes cleanly, so
# without a ceiling a conversation whose turns keep failing would accumulate
# prompts indefinitely.
QUEUE_MAX: Final[int] = 5


async def queue_add(
    chat_id: str, owner_id: str, prompt: str, model: str | None = None
) -> int:
    """Append a prompt. Returns its 1-based position, or 0 if the queue is full."""
    cur = await db_conn.execute(
        "SELECT COUNT(*) AS n FROM turn_queue WHERE chat_id = ? AND owner_id = ?",
        (chat_id, owner_id),
    )
    row = await cur.fetchone()
    if (row["n"] if row else 0) >= QUEUE_MAX:
        return 0
    await db_conn.execute(
        "INSERT INTO turn_queue (chat_id, owner_id, prompt, model, state, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (chat_id, owner_id, prompt, model, _now()),
    )
    await db_conn.commit()
    return (row["n"] if row else 0) + 1


async def queue_list(chat_id: str, owner_id: str) -> list[dict[str, Any]]:
    """Every queued prompt for a conversation, oldest first."""
    cur = await db_conn.execute(
        "SELECT id, prompt, model, state, created_at FROM turn_queue "
        "WHERE chat_id = ? AND owner_id = ? ORDER BY id",
        (chat_id, owner_id),
    )
    return [dict(row) for row in await cur.fetchall()]


async def queue_counts(owner_id: str) -> dict[str, int]:
    """How many prompts each conversation has queued, for the chat list."""
    cur = await db_conn.execute(
        "SELECT chat_id, COUNT(*) AS n FROM turn_queue WHERE owner_id = ? "
        "GROUP BY chat_id",
        (owner_id,),
    )
    return {row["chat_id"]: row["n"] for row in await cur.fetchall()}


async def queue_next(chat_id: str) -> dict[str, Any] | None:
    """The oldest pending prompt for a conversation, or None.

    Deliberately not owner-scoped: the caller is the turn that just finished,
    which already established ownership, and it holds the owner to pass on.
    """
    cur = await db_conn.execute(
        "SELECT id, prompt, model FROM turn_queue "
        "WHERE chat_id = ? AND state = 'pending' ORDER BY id LIMIT 1",
        (chat_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def queue_delete(queue_id: int, owner_id: str) -> bool:
    """Remove one queued prompt. Returns whether a row was removed."""
    cur = await db_conn.execute(
        "DELETE FROM turn_queue WHERE id = ? AND owner_id = ?", (queue_id, owner_id)
    )
    await db_conn.commit()
    return bool(cur.rowcount)


async def queue_release(queue_id: int, owner_id: str) -> bool:
    """Return a held prompt to pending, so the next finish will send it."""
    cur = await db_conn.execute(
        "UPDATE turn_queue SET state = 'pending' WHERE id = ? AND owner_id = ?",
        (queue_id, owner_id),
    )
    await db_conn.commit()
    return bool(cur.rowcount)


async def queue_hold_all(chat_id: str) -> int:
    """Mark a conversation's pending prompts as held. Returns how many."""
    cur = await db_conn.execute(
        "UPDATE turn_queue SET state = 'held' WHERE chat_id = ? AND state = 'pending'",
        (chat_id,),
    )
    await db_conn.commit()
    return cur.rowcount or 0


# ── Supervisor orchestration ────────────────────────────────────────────────────────────


async def supervisor_list(owner_id: str) -> list[dict[str, Any]]:
    """All supervisors for *owner_id*, newest first."""
    cur = await db_conn.execute(
        "SELECT id, title, description, config, status, progress_pct, "
        "created_at, updated_at, completed_at "
        "FROM supervisors WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def supervisor_get(supervisor_id: str, owner_id: str) -> dict[str, Any] | None:
    """Fetch one supervisor, owner-scoped."""
    cur = await db_conn.execute(
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
    now = _now()
    await db_conn.execute(
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
    await db_conn.commit()
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
    """Update supervisor fields; only non-None values are set. Returns rowcount."""
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
    vals.append(_now())
    vals.extend([supervisor_id, owner_id])
    sql = (
        "UPDATE supervisors SET " + ", ".join(sets) + " WHERE id = ? AND owner_id = ?"
    )
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
    return cur.rowcount > 0


async def supervisor_delete(supervisor_id: str, owner_id: str) -> bool:
    """Delete a supervisor and all its tasks/messages. Returns rowcount."""
    try:
        await db_conn.execute("BEGIN")
        await db_conn.execute(
            "DELETE FROM supervisor_tasks WHERE supervisor_id = ?",
            (supervisor_id,),
        )
        await db_conn.execute(
            "DELETE FROM supervisor_messages WHERE supervisor_id = ?",
            (supervisor_id,),
        )
        cur = await db_conn.execute(
            "DELETE FROM supervisors WHERE id = ? AND owner_id = ?",
            (supervisor_id, owner_id),
        )
        await db_conn.commit()
        return cur.rowcount > 0
    except Exception:
        await db_conn.rollback()
        raise


async def supervisor_tasks_get(
    supervisor_id: str, owner_id: str
) -> list[dict[str, Any]]:
    """All tasks for a supervisor, ordered by creation."""
    cur = await db_conn.execute(
        "SELECT id, supervisor_id, title, description, status, model, result, "
        "progress_pct, parent_task_id, depends_on, created_at, updated_at "
        "FROM supervisor_tasks WHERE supervisor_id = ? "
        "ORDER BY id ASC",
        (supervisor_id,),
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
    """Create a task under a supervisor. Returns created_at timestamp."""
    now = _now()
    await db_conn.execute(
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
    await db_conn.commit()
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
    """Update a task's fields. Returns rowcount."""
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
    vals.append(_now())
    vals.extend([task_id, supervisor_id])
    sql = (
        "UPDATE supervisor_tasks SET " + ", ".join(sets)
        + " WHERE id = ? AND supervisor_id = ?"
    )
    cur = await db_conn.execute(sql, vals)
    await db_conn.commit()
    return cur.rowcount > 0


async def supervisor_task_get(
    supervisor_id: str, task_id: str, owner_id: str
) -> dict[str, Any] | None:
    """Fetch one task under a supervisor."""
    cur = await db_conn.execute(
        "SELECT id, supervisor_id, title, description, status, model, result, "
        "progress_pct, parent_task_id, depends_on, created_at, updated_at "
        "FROM supervisor_tasks WHERE id = ? AND supervisor_id = ?",
        (task_id, supervisor_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def supervisor_messages_append(
    supervisor_id: str, role: str, content: str, metadata: dict[str, Any] | None = None
) -> int:
    """Append a supervisor message. Returns row id."""
    cur = await db_conn.execute(
        "INSERT INTO supervisor_messages "
        "(supervisor_id, role, content, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            supervisor_id,
            role,
            content,
            json.dumps(metadata) if metadata else None,
            _now(),
        ),
    )
    await db_conn.commit()
    return cur.lastrowid


async def supervisor_messages_get(
    supervisor_id: str,
    owner_id: str,
    after_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read messages for a supervisor, optionally since a specific id."""
    if after_id is not None:
        cur = await db_conn.execute(
            "SELECT id, role, content, metadata, created_at "
            "FROM supervisor_messages WHERE supervisor_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (supervisor_id, limit),
        )
    else:
        cur = await db_conn.execute(
            "SELECT id, role, content, metadata, created_at "
            "FROM supervisor_messages WHERE supervisor_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (supervisor_id, limit),
        )
    return [dict(r) for r in await cur.fetchall()]


async def supervisor_members_list(supervisor_id: str) -> list[dict[str, Any]]:
    """The chats a supervisor watches, newest addition last.

    Joined against ``chats`` so a member whose conversation was deleted simply
    stops appearing. An INNER JOIN rather than a LEFT one: a supervisor listing
    a conversation that no longer exists is the failure worth preventing, and a
    row with a null title beside a real status reads as a bug.

    Deliberately returns no status. That comes from the supervisor classifier,
    which already decides working/waiting/failed for every chat and session; a
    second definition here would agree with it only by coincidence, and the two
    would drift the first time either changed.
    """
    cur = await db_conn.execute(
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
    """Add one chat to a supervisor. True if it was not already a member.

    The caller is responsible for having checked that *chat_id* belongs to the
    requesting owner: this layer stores what it is given, and an unchecked id
    here would pull another account's conversation into the members feed along
    with its title and preview.
    """
    cur = await db_conn.execute(
        "INSERT INTO supervisor_members (supervisor_id, chat_id, added_at) "
        "VALUES (?, ?, ?) ON CONFLICT(supervisor_id, chat_id) DO NOTHING",
        (supervisor_id, chat_id, _now()),
    )
    await db_conn.commit()
    return cur.rowcount > 0


async def supervisor_member_remove(supervisor_id: str, chat_id: str) -> bool:
    """Drop a member. Never deletes the conversation itself.

    A supervisor is a view over work, not its owner -- removing a member must
    leave the conversation exactly as it was.
    """
    cur = await db_conn.execute(
        "DELETE FROM supervisor_members WHERE supervisor_id = ? AND chat_id = ?",
        (supervisor_id, chat_id),
    )
    await db_conn.commit()
    return cur.rowcount > 0


async def supervisor_progress(supervisor_id: str, owner_id: str) -> float:
    """Return overall progress percentage for a supervisor's tasks."""
    # Fetch all tasks for this supervisor, compute weighted average
    cur = await db_conn.execute(
        "SELECT COUNT(*) AS total FROM supervisor_tasks WHERE supervisor_id = ?",
        (supervisor_id,),
    )
    row = await cur.fetchone()
    total = row["total"] if row else 0
    if total == 0:
        return 0.0

    cur = await db_conn.execute(
        "SELECT SUM(progress_pct) AS sum_pct FROM supervisor_tasks "
        "WHERE supervisor_id = ?",
        (supervisor_id,),
    )
    row = await cur.fetchone()
    sum_pct = row["sum_pct"] or 0
    return round(float(sum_pct) / total, 1)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug_from_title(title: str) -> str:
    """Derive a slug from a chat title: lowercase, alphanumeric-hyphens, max 40 chars."""
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())
    slug = "-".join(p for p in slug.split("-") if p)
    return slug[:40] or "untitled"


def slug_pattern(slug: str) -> str | None:
    """Validate slug characters: [a-z0-9-], 3-40 chars.

    Returns the slug if valid, None otherwise.
    Slugs must not start or end with a dash.
    """
    if not re.fullmatch(r"[a-z0-9-]{3,40}", slug):
        return None
    if slug.startswith("-") or slug.endswith("-"):
        return None
    return slug


# ── Database backup / restore ────────────────────────────────────────────────────────


def _db_backup_sync(backup_path: str) -> bytes:
    """Copy the database and return it gzip-compressed. Blocking; call off-loop."""
    import gzip as _gzip

    sync_conn = sqlite3.connect(backup_path)
    db_conn_sync = sqlite3.connect(str(config.DB_PATH))
    try:
        db_conn_sync.backup(sync_conn)
    finally:
        db_conn_sync.close()
        sync_conn.close()
    return _gzip.compress(Path(backup_path).read_bytes())


async def db_backup() -> bytes:
    """Return a gzip-compressed SQLite backup of the entire database.

    sqlite3.backup(), the file read and the gzip pass are all blocking and
    scale with database size, so they run in a worker thread. On the event
    loop they would stall every other request, including live SSE streams.
    """
    backup_path = f"{config.DB_PATH}.backup.{int(time.time())}"
    try:
        return await asyncio.to_thread(_db_backup_sync, backup_path)
    finally:
        try:
            Path(backup_path).unlink()
        except OSError:
            pass


def _validate_sqlite_file(path: Path) -> bool:
    """Return True if *path* is a sound SQLite database with our schema.

    Runs before the live file is touched, so a corrupt or unrelated upload is
    rejected rather than swapped in.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        # A valid backup of *this* app, not just any SQLite file.
        return {"chats", "messages", "users"}.issubset(names)
    except sqlite3.DatabaseError:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


async def _reopen() -> None:
    """Reconnect and apply additive migrations. Never leaves db_conn as None."""
    await init()


async def db_restore(data: bytes) -> bool:
    """Replace the current database with the provided gzip-compressed data.

    The candidate is validated before the swap (gzip, SQLite magic, integrity
    check, expected tables), the swap itself is an atomic rename, and the
    reconnect goes through :func:`init` so schema migrations are applied -- a
    restored older backup would otherwise be missing columns the app expects.
    """
    import gzip as _gzip

    try:
        decompressed = _gzip.decompress(data)
    except Exception:  # noqa: BLE001 -- silently reject bad input
        return False

    # Reject anything that is not a SQLite database outright.
    if not decompressed.startswith(_SQLITE_MAGIC):
        return False

    db_path = Path(config.DB_PATH)
    tmp_path = db_path.with_suffix(".restore.tmp")

    try:
        tmp_path.write_bytes(decompressed)
        if not await asyncio.to_thread(_validate_sqlite_file, tmp_path):
            tmp_path.unlink(missing_ok=True)
            return False

        # Close existing connection before swapping files.
        await close()

        # Drop the old write-ahead log and shared-memory sidecars. Left in
        # place, SQLite can replay the previous database's WAL over the
        # restored file and corrupt it.
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

        # Atomic rename.
        tmp_path.replace(db_path)

        await _reopen()
        return True
    except Exception:  # noqa: BLE001 -- recover best-effort on failure
        tmp_path.unlink(missing_ok=True)
        # Leaving db_conn as None would 500 every later request until restart.
        try:
            await _reopen()
        except Exception:  # noqa: BLE001 -- final fallback, DB may be unusable
            global db_conn
            db_conn = None
        return False


# ── Claude Code session files ──────────────────────────────────────────────────────

_CLAUDE_SESSIONS_DIR: Final[Path] = Path.home() / ".claude" / "sessions"


async def read_claude_sessions() -> list[dict[str, Any]]:
    """Read ~/.claude/sessions/*.json files and return a list of session dicts.

    Returns sessions that are:
    - Not the current session (by PID match)
    - Have a non-empty name or sessionId
    - Are interactive kind

    Each dict has: id, name, cwd, kind, startedAt, updatedAt, sessionId
    """
    import os as _os

    sessions: list[dict[str, Any]] = []
    try:
        if not _CLAUDE_SESSIONS_DIR.is_dir():
            return []
    except (PermissionError, OSError):
        return []

    # Current PID to filter out the running session
    current_pid = _os.getpid()

    try:
        files = sorted(_CLAUDE_SESSIONS_DIR.glob("*.json"))
    except (PermissionError, OSError):
        return []

    for fpath in files:
        try:
            content = await asyncio.to_thread(fpath.read_text)
            data = json.loads(content)
        except (json.JSONDecodeError, OSError):
            continue
        # Valid JSON that isn't an object still has no .get(): a single file
        # holding a list or a bare string raised AttributeError out of the
        # loop and failed the whole listing, rather than being skipped like
        # every other unusable file here.
        if not isinstance(data, dict):
            continue

        # Skip the active CLI process, but retain WebConsole-created session files.
        # WebConsole writes these files with its own PID so the CLI can discover
        # them; filtering by PID alone would hide the bidirectional-sync record.
        pid = data.get("pid")
        try:
            same_process = bool(pid) and int(pid) == current_pid
        except (TypeError, ValueError):
            same_process = False
        if same_process and data.get("entrypoint") != "webconsole":
            continue

        name = data.get("name", "")
        kind = data.get("kind", "")
        session_id = data.get("sessionId", "")

        # Only include interactive sessions with a name
        if kind != "interactive":
            continue
        if not name and not session_id:
            continue

        started_at = _format_timestamp(data.get("startedAt"))
        updated_at = _format_timestamp(data.get("updatedAt"))
        if not started_at:
            started_at = ""
        if not updated_at:
            updated_at = ""

        model = data.get("model", "")
        if not model and session_id:
            # Transcript reads are blocking file I/O; keep them off the loop.
            model = await asyncio.to_thread(_lookup_session_model, session_id) or ""

        sessions.append(
            {
                "id": session_id if session_id else fpath.stem,
                "name": name or "Untitled",
                "cwd": data.get("cwd", ""),
                "kind": kind,
                "startedAt": started_at,
                "updatedAt": updated_at,
                "sessionId": session_id,
                "model": model,
                "entrypoint": data.get("entrypoint", ""),
                # Claude Code reports its own state here. Observed value is
                # "busy"; older builds omit the field entirely, so absence
                # means "unknown", not "idle".
                "status": data.get("status") or "",
                "status_updated_at": _format_timestamp(data.get("statusUpdatedAt")) or "",
                "live": _pid_is_running(pid),
                "file": fpath.name,
            }
        )

    return _dedupe_sessions(sessions)


def _pid_is_running(pid: Any) -> bool:
    """Return True if *pid* names a live process.

    Signal 0 performs the permission and existence checks without delivering
    anything, which is the cheapest liveness probe available.
    """
    import os as _os

    try:
        _os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def _dedupe_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse records that describe the same CLI session.

    Resuming a CLI session writes a second file, ~/.claude/sessions/<id>.json
    with entrypoint "webconsole", so the CLI can see the link. The real CLI
    already has its own PID-named file for that same sessionId, so listing the
    directory returned both and the sidebar showed every resumed session twice
    -- once under its real name ("cweb2") and once as "CLI: b198eb69...".
    Each resume added another, permanently.

    The CLI record wins: it carries the name the user actually chose and the
    live process. A "webconsole" record survives only when no CLI record
    claims that sessionId, which is how a session whose process has exited
    stays visible and removable.
    """
    best: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for item in sessions:
        key = item.get("sessionId")
        if not key:
            unkeyed.append(item)
            continue
        current = best.get(key)
        if current is None or _session_rank(item) > _session_rank(current):
            best[key] = item
    return [*best.values(), *unkeyed]


def _session_rank(item: dict[str, Any]) -> tuple[int, int]:
    """Order candidates for the same sessionId; highest wins."""
    return (
        1 if item.get("entrypoint") == "cli" else 0,
        1 if item.get("live") else 0,
    )


def _session_is_live(session_id: str) -> bool:
    """True if any record for *session_id* has a running process behind it."""
    try:
        files = list(_CLAUDE_SESSIONS_DIR.glob("*.json"))
    except (PermissionError, OSError):
        return False
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("sessionId") == session_id and _pid_is_running(data.get("pid")):
            return True
    return False


def delete_claude_session_file(session_id: str) -> bool:
    """Remove the WebConsole shadow record for *session_id*.

    Only ever deletes a file this application wrote. The CLI's own PID-named
    files belong to Claude Code and are left alone even when the process has
    exited -- reaping those is not WebConsole's business, and a live session
    must never lose its record because someone clicked a cross in a sidebar.

    Returns True if a file was removed.
    """
    import os as _os

    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Invalid session_id (contains path separators or ..)")

    path = _CLAUDE_SESSIONS_DIR / f"{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False

    if data.get("entrypoint") != "webconsole":
        raise ValueError("Refusing to delete a session file WebConsole did not write")

    # Liveness has to be judged across every record for this sessionId, not
    # from this file's own pid. A shadow record stores the pid of whichever
    # WebConsole process wrote it, which is long dead by the time anyone looks
    # -- so trusting it alone let a running CLI session be cleared from the
    # sidebar, because the session's real liveness lives in the CLI's separate
    # PID-named file.
    if _session_is_live(session_id):
        raise ValueError("Refusing to delete a session file whose process is running")

    try:
        _os.unlink(path)
    except OSError:
        return False
    return True


def _format_timestamp(ts: float | str | None) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    if ts is None:
        return ""
    try:
        ts_num = int(ts)
        if ts_num > 1e12:  # milliseconds
            ts_num = ts_num // 1000
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_num))
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


# ── Transcript-backed model discovery ───────────────────────────────────────────

_CLAUDE_PROJECTS_DIR: Final[Path] = Path.home() / ".claude" / "projects"

# Session ids are UUID-like. Restrict the charset before interpolating one into
# a glob pattern so it cannot traverse out of the projects directory.
_SESSION_ID_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Bytes read from the end of a transcript on the fast path.
_TRANSCRIPT_TAIL_BYTES: Final[int] = 1 << 20

# In-process cache keyed by session_id; refreshed each read_claude_sessions call
_model_cache: dict[str, str] = {}


def _session_transcript_paths(session_id: str) -> list[Path]:
    """Return the transcript files belonging to *session_id*.

    Claude stores each transcript as
    ``~/.claude/projects/<project-dir>/<session_id>.jsonl`` -- one level below
    the projects root, which is why a top-level ``*.jsonl`` glob matched
    nothing. Targeting the filename also avoids reading unrelated transcripts.
    """
    if not _SESSION_ID_SAFE_RE.fullmatch(session_id):
        return []
    try:
        if not _CLAUDE_PROJECTS_DIR.is_dir():
            return []
        return sorted(_CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    except (PermissionError, OSError):
        return []


def _model_from_lines(lines: Sequence[str], session_id: str) -> str | None:
    """Scan *lines* newest-first and return the first usable model."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record: dict = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Assistant message line carries the model in message.model
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        if record.get("sessionId") != session_id:
            continue
        model = message.get("model")
        if model and model != "<synthetic>":
            return model
    return None


def _extract_model_from_transcript(session_id: str) -> str | None:
    """Return the last non-synthetic model recorded for *session_id*.

    Reads only that session's transcript, and only its tail on the fast path --
    the newest assistant message is at the end, and transcripts reach tens of
    megabytes. Falls back to a full scan when the tail holds no assistant
    message.

    Returns None when no matching transcript line is found.
    """
    for fpath in _session_transcript_paths(session_id):
        try:
            size = fpath.stat().st_size
            with fpath.open("rb") as fh:
                if size > _TRANSCRIPT_TAIL_BYTES:
                    fh.seek(size - _TRANSCRIPT_TAIL_BYTES)
                    chunk = fh.read()
                    # Drop the leading partial line left by the seek.
                    newline = chunk.find(b"\n")
                    chunk = chunk[newline + 1 :] if newline >= 0 else b""
                else:
                    chunk = fh.read()
            model = _model_from_lines(
                chunk.decode("utf-8", errors="replace").splitlines(), session_id
            )
            if model:
                return model
            if size > _TRANSCRIPT_TAIL_BYTES:
                # Tail held no assistant message; pay for the whole file once.
                text = fpath.read_text(encoding="utf-8", errors="replace")
                model = _model_from_lines(text.splitlines(), session_id)
                if model:
                    return model
        except OSError:
            continue
    return None


def _lookup_session_model(session_id: str) -> str | None:
    """Resolve the model for *session_id* (session file or transcript).

    On first call for a session, the transcript is scanned and the result is
    cached in memory for the lifetime of the process.
    """
    if session_id in _model_cache:
        return _model_cache.get(session_id) or None

    model = _extract_model_from_transcript(session_id)
    if model:
        _model_cache[session_id] = model
    else:
        _model_cache[session_id] = ""

    return model or None


def write_claude_session_file(
    session_id: str, name: str, cwd: str, model: str = ""
) -> None:
    """Write a session file to ~/.claude/sessions/<session_id>.json so CLI can pick it up.

    This enables bidirectional sync: Web sessions become visible to CLI.
    Uses atomic write (write to temp then rename) to avoid partial reads.
    Validates session_id to prevent path traversal.
    """
    import os as _os

    # ── Security: reject path-traversal sequences ─────────────────────────────
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Invalid session_id (contains path separators or ..)")

    try:
        sessions_dir = _CLAUDE_SESSIONS_DIR
        sessions_dir.mkdir(parents=True, exist_ok=True)

        now_ms = int(time.time() * 1000)
        session_data = {
            "pid": _os.getpid(),
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": now_ms,
            "procStart": now_ms,
            "version": config.VERSION,
            "peerProtocol": "http",
            "peerFeatures": {
                "supports": ["text", "markdown"],
                "toolSupport": "complete",
            },
            "kind": "interactive",
            "entrypoint": "webconsole",
            "pidDomain": str(_os.getpid()),
            "messagingSocketPath": "",
            "name": name,
            "nameSource": "webconsole",
            "nameSince": now_ms,
            "updatedAt": now_ms,
            "model": model,
        }

        # Atomic write: write to temp, rename
        tmp_path = sessions_dir / f".tmp_{session_id}.json"
        with open(tmp_path, "w") as f:
            json.dump(session_data, f, indent=2)
        _os.rename(str(tmp_path), str(sessions_dir / f"{session_id}.json"))

    except (PermissionError, OSError):
        pass  # Silently fail — non-critical
