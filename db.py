# db.py — SQLite bootstrap and queries for WebConsole.
#
# Single SQLite file, accessed via aiodlite (async). Schema created automatically on boot.
# All paths are resolved and validated against PROJECTS_ROOT before any filesystem op.
from __future__ import annotations

import datetime
import logging
import re
import time
from pathlib import Path
from typing import Final

import aiosqlite

import config


def __getattr__(name: str):
    """Resolve extracted-db symbols lazily (circular-import guard)."""
    _SYMBOLS: dict[str, str] = {
        # queue
        "QUEUE_MAX": "routes.db_queue",
        "last_model_used": "routes.db_queue",
        "last_models_used": "routes.db_queue",
        "queue_add": "routes.db_queue",
        "queue_counts": "routes.db_queue",
        "queue_held_counts": "routes.db_queue",
        "queue_delete": "routes.db_queue",
        "queue_hold_all": "routes.db_queue",
        "queue_list": "routes.db_queue",
        "queue_next": "routes.db_queue",
        "queue_release": "routes.db_queue",
        # orchestrators
        "orchestrator_create": "routes.db_orchestrators",
        "orchestrator_delete": "routes.db_orchestrators",
        "orchestrator_get": "routes.db_orchestrators",
        "orchestrator_list": "routes.db_orchestrators",
        "orchestrator_mark_degraded": "routes.db_orchestrators",
        "orchestrator_clear_degraded": "routes.db_orchestrators",
        "orchestrator_member_add": "routes.db_orchestrators",
        "orchestrator_member_remove": "routes.db_orchestrators",
        "orchestrator_members_list": "routes.db_orchestrators",
        "orchestrator_messages_append": "routes.db_orchestrators",
        "orchestrator_messages_get": "routes.db_orchestrators",
        "orchestrator_progress": "routes.db_orchestrators",
        "orchestrator_task_create": "routes.db_orchestrators",
        "orchestrator_task_get": "routes.db_orchestrators",
        "orchestrator_task_update": "routes.db_orchestrators",
        "orchestrator_tasks_get": "routes.db_orchestrators",
        "orchestrator_update": "routes.db_orchestrators",
        # sessions
        "read_claude_sessions": "routes.db_sessions",
        "delete_claude_session_file": "routes.db_sessions",
        "write_claude_session_file": "routes.db_sessions",
        "_TRANSCRIPT_TAIL_BYTES": "routes.db_sessions",
        "_format_timestamp": "routes.db_sessions",
        "_extract_model_from_transcript": "routes.db_sessions",
        "_lookup_session_model": "routes.db_sessions",
        "_model_cache": "routes.db_sessions",
        "_model_from_lines": "routes.db_sessions",
        "_pid_is_running": "routes.db_sessions",
        "_session_is_live": "routes.db_sessions",
        "_session_rank": "routes.db_sessions",
        "_session_transcript_paths": "routes.db_sessions",
        # chats
        "chat_list": "routes.db_chats",
        "chat_get": "routes.db_chats",
        "chat_create": "routes.db_chats",
        "chat_update": "routes.db_chats",
        "chats_reorder": "routes.db_chats",
        "chats_clear_order": "routes.db_chats",
        "chat_archive": "routes.db_chats",
        "chat_delete": "routes.db_chats",
        "chat_fork": "routes.db_chats",
        "chat_set_session": "routes.db_chats",
        "chat_set_transcript_offset": "routes.db_chats",
        "chat_set_question_ids": "routes.db_chats",
        "chat_get_question_ids": "routes.db_chats",
        "chat_auto_answer_set": "routes.db_chats",
        "chat_auto_answer_get": "routes.db_chats",
        "chat_auto_answer_recommend_get": "routes.db_chats",
        "chat_auto_answer_log_append": "routes.db_chats",
        "chat_auto_answer_log_get": "routes.db_chats",
        "chats_with_auto_answer": "routes.db_chats",
        "bump_chat_updated_at": "routes.db_chats",
        "chat_set_model": "routes.db_chats",
        "chat_set_title": "routes.db_chats",
        "chat_mark_degraded": "routes.db_chats",
        "chat_clear_degraded": "routes.db_chats",
        "_ALLOWED_CHAT_FIELDS": "routes.db_chats",
        "chat_search": "routes.db_chats",
        "messages_get": "routes.db_chats",
        "messages_last": "routes.db_chats",
        "messages_page": "routes.db_chats",
        "messages_append": "routes.db_chats",
        "messages_batch": "routes.db_chats",
        "_fts_guard": "routes.db_chats",
        "_fts_index_ids": "routes.db_chats",
        "_fts_forget_ids": "routes.db_chats",
        "_fts_rebuild": "routes.db_chats",
        "_messages_batch_lock": "routes.db_chats",
        # machines
        "ai_machine_active": "routes.db_machines",
        "ai_machines_list": "routes.db_machines",
        "ai_machine_get": "routes.db_machines",
        "ai_machine_set_ssh_host_key_fingerprint": "routes.db_machines",
        "ai_machine_create": "routes.db_machines",
        "ai_machine_update": "routes.db_machines",
        "ai_machine_clear_transport": "routes.db_machines",
        "ai_machine_activate": "routes.db_machines",
        "ai_machine_delete": "routes.db_machines",
        "ai_machine_set_models": "routes.db_machines",
        "ai_machine_api_key": "routes.db_machines",
        "ai_machine_seed_anthropic": "routes.db_machines",
        "chat_owner": "routes.db_machines",
        "ai_machine_backend": "routes.db_machines",
        "ai_machine_backend_by_id": "routes.db_machines",
        "chat_routing": "routes.db_machines",
        "chat_set_machine": "routes.db_machines",
        "parse_active_models": "routes.db_machines",
        "_BACKEND_COLUMNS": "routes.db_machines",
        # ssh transports
        "ssh_transport_create": "routes.db_transports",
        "ssh_transport_get": "routes.db_transports",
        "ssh_transports_list": "routes.db_transports",
        "ssh_transport_update": "routes.db_transports",
        "ssh_transport_delete": "routes.db_transports",
        "ssh_transport_set_host_key_fingerprint": "routes.db_transports",
        # users
        "user_get_by_name": "routes.db_users",
        "user_create": "routes.db_users",
        "setting_get": "routes.db_users",
        "setting_set": "routes.db_users",
        "api_token_create": "routes.db_users",
        "api_token_by_hash": "routes.db_users",
        "api_token_touch": "routes.db_users",
        "api_token_list": "routes.db_users",
        "api_token_revoke": "routes.db_users",
        "admin_action_record": "routes.db_users",
        # usage
        "_cutoff": "routes.db_usage",
        "usage_record": "routes.db_usage",
        "usage_cursor_get": "routes.db_usage",
        "usage_import": "routes.db_usage",
        "_ensure_usage_columns": "routes.db_usage",
        "ROUTED_WINDOW_S": "routes.db_usage",
        "normalise_prompt": "routes.db_usage",
        "routed_request_add": "routes.db_usage",
        "routed_markers": "routes.db_usage",
        "routed_owner_of": "routes.db_usage",
        "usage_by_origin": "routes.db_usage",
        "usage_by_session": "routes.db_usage",
        "usage_totals": "routes.db_usage",
        "usage_overall": "routes.db_usage",
        "usage_recent": "routes.db_usage",
        "bucket_spine": "routes.db_usage",
        "_epoch_of": "routes.db_usage",
        "_floor_local": "routes.db_usage",
        "_bucket_key": "routes.db_usage",
        "_bucket_expr": "routes.db_usage",
        "usage_series": "routes.db_usage",
        "usage_model_series": "routes.db_usage",
        "usage_prune": "routes.db_usage",
        "usage_earliest": "routes.db_usage",
        "_earliest": "routes.db_usage",
        "_on_spine": "routes.db_usage",
        "system_sample_insert": "routes.db_usage",
        "system_latest": "routes.db_usage",
        "system_series": "routes.db_usage",
        "system_prune": "routes.db_usage",
        "SYSTEM_FIELDS": "routes.db_usage",
        "USAGE_IMPORT_BATCH": "routes.db_usage",
        "_LOCAL_TS": "routes.db_usage",
        "_SPINE_MAX": "routes.db_usage",
        "_BUCKET_STEP_S": "routes.db_usage",
        "USAGE_BUCKETS": "routes.db_usage",
        "_USAGE_BUCKETS": "routes.db_usage",
        # read marks
        "read_marks_get": "routes.db_read_marks",
        "read_mark_set": "routes.db_read_marks",
        "chat_last_activity": "routes.db_read_marks",
        # backup / restore
        "db_backup": "routes.db_backup",
        "_db_backup_sync": "routes.db_backup",
        "_validate_sqlite_file": "routes.db_backup",
        "db_restore": "routes.db_backup",
    }
    if name in _SYMBOLS:
        import importlib
        mod = importlib.import_module(_SYMBOLS[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Defined here so that test patches of db._CLAUDE_SESSIONS_DIR /
# db._CLAUDE_PROJECTS_DIR propagate into the extracted module.


_CLAUDE_SESSIONS_DIR: Final[Path] = Path.home() / ".claude" / "sessions"
_CLAUDE_PROJECTS_DIR: Final[Path] = Path.home() / ".claude" / "projects"

_log = logging.getLogger("wc.db")

db_conn: aiosqlite.Connection | None = None

# How long index maintenance waits for the SQLite writer lock before giving up.
_FTS_BUSY_TIMEOUT_MS: Final[int] = 5000
# How long any writer waits for the lock before giving up. Longer than the
# 5s it used to inherit: a wait is a slow request, a timeout is a lost
# write, and the second is much worse than the first.
_BUSY_TIMEOUT_MS: Final[int] = 15000

# Every SQLite database file starts with this. Used to reject non-DB uploads.
_SQLITE_MAGIC: Final[bytes] = b"SQLite format 3\x00"

# host/port are NOT NULL and describe the transport, so an Anthropic machine
# stores the API endpoint there. It also makes the reachability test meaningful.
_ANTHROPIC_HOST: Final[str] = "api.anthropic.com"


async def init() -> None:
    """Create the database and tables. Idempotent."""
    global db_conn
    root = Path(config.PROJECTS_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)

    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db_conn = await aiosqlite.connect(str(db_path))
    db_conn.row_factory = aiosqlite.Row
    await db_conn.execute("PRAGMA journal_mode=WAL")
    await db_conn.execute("PRAGMA foreign_keys=ON")
    # Set explicitly rather than inherited. This connection was relying on
    # sqlite3's undocumented 5-second default while the two other writer
    # connections in the project set 5000ms by hand, so the shared one -- the
    # one every request goes through -- was the only writer whose patience
    # nobody had chosen. auth.py's session handle remains a separate writer, so
    # a wait is still possible even with index maintenance moved onto this one.
    await db_conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")

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
            ai_machine_id TEXT,
            voice_mode    INTEGER NOT NULL DEFAULT 0,
            -- 'normal' | 'brainstorming'. Voice-mode chats are forced
            -- to 'brainstorming' at creation and cannot be changed.
            type          TEXT NOT NULL DEFAULT 'normal',
            -- Voice conversations can link to a parent chat (handoff).
            parent_chat_id TEXT,
            -- 1 if this chat was created as a temporary voice brainstorm.
            is_temporary  INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ai_machines (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            -- 'anthropic' talks to the official API (what Claude Code uses by
            -- default); 'proxy' reaches a host running claude_proxy.py.
            provider      TEXT NOT NULL DEFAULT 'claude_code',
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
            -- May this backend be used at all. `active` above means "is the
            -- default"; these are separate questions. See _ensure_chat_columns
            -- for why the names read oddly.
            enabled       INTEGER NOT NULL DEFAULT 1,
            owner_id      TEXT NOT NULL DEFAULT 'admin',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ssh_transports (
            id                        TEXT PRIMARY KEY,
            name                      TEXT NOT NULL,
            owner_id                  TEXT NOT NULL,
            ssh_host                  TEXT NOT NULL,
            ssh_user                  TEXT NOT NULL DEFAULT 'kali',
            ssh_key_path              TEXT NOT NULL DEFAULT '',
            ssh_host_key_fingerprint  TEXT NOT NULL DEFAULT '',
            created_at                TEXT NOT NULL,
            updated_at                TEXT NOT NULL
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

        -- One row per completed voice-mode turn. Exists for two reasons:
        -- bypassing the claude CLI for voice also loses its automatic
        -- usage/cost recording (see CLAUDE.md's documented orchestrator.py
        -- lesson for what happens when a caller skips this), and it powers
        -- the "average reply time per model" annotation in the Settings
        -- dialog's voice model picker.
        CREATE TABLE IF NOT EXISTS voice_turn_timing (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            model        TEXT NOT NULL,
            ttft_ms      INTEGER NOT NULL,
            total_ms     INTEGER NOT NULL,
            recorded_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voice_turn_timing_model
            ON voice_turn_timing(model, recorded_at);

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

        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- API tokens: the authenticated way for a script to reach this server.
        -- It exists because the alternative kept being invented ad hoc -- a
        -- `/dev/*` prefix exempted from the auth middleware, and one endpoint
        -- under it that minted an admin session and returned the id to anyone
        -- who asked. A caller that cannot hold a cookie needs a credential of
        -- its own, not a hole.
        --
        -- Only a hash is stored, like `sessions` above and for the same reason:
        -- this file is copied by /api/admin/export, so a plaintext token here
        -- would make every backup a set of working keys. sha256 rather than
        -- argon2 -- the secret is 256 bits of `token_urlsafe`, so there is no
        -- guessing to slow down, and this is read on every request.
        --
        -- `id` is a public prefix, safe to log and to show in a list; the
        -- secret is shown once, at creation, and is unrecoverable afterwards.
        CREATE TABLE IF NOT EXISTS api_tokens (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            token_hash   TEXT NOT NULL UNIQUE,
            owner_id     TEXT NOT NULL,
            role         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT,           -- NULL = no expiry
            last_used_at TEXT,
            revoked_at   TEXT            -- NULL = live
        );
        CREATE INDEX IF NOT EXISTS idx_api_tokens_owner
            ON api_tokens(owner_id, revoked_at);

        -- Administrative action audit trail: each write performed by a logged-in
        -- admin is recorded here so a single-operator can review who changed what
        -- and when.  Retained for 90 days; see _admin_actions_prune().
        CREATE TABLE IF NOT EXISTS admin_actions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       TEXT NOT NULL,
            action        TEXT NOT NULL,   -- 'settings_patch', 'token_create', etc.
            detail        TEXT,           -- free-form, not security-sensitive
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admin_actions_user_time
            ON admin_actions(user_id, created_at DESC);

        -- When the user last looked at an agent, so the orchestrator can tell
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
            provider              TEXT NOT NULL DEFAULT 'claude_code',
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
            prompt      TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_routed_session
            ON routed_requests(session_id, from_offset DESC);

        -- Agent supervision: an orchestrator is an autonomous worker with its own
        -- conversation, task list, and message history.
        CREATE TABLE IF NOT EXISTS orchestrators (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            description  TEXT,
            -- JSON blob of orchestration settings (model, launcher options).
            -- Read and written by routes/db_orchestrators.py, which this table
            -- definition had fallen out of sync with -- along with plan,
            -- progress_pct and completed_at below -- so a fresh database
            -- (a new install, or any test's own throwaway one) crashed on
            -- the first orchestrator ever created with "no such column:
            -- config". The production database this shipped alongside
            -- already had all four from an earlier, richer schema and so
            -- never showed the bug; a fresh one has no such history to fall
            -- back on.
            config       TEXT NOT NULL DEFAULT '{}',
            owner_id     TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'idle',
            plan         TEXT,
            progress_pct REAL NOT NULL DEFAULT 0.0,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            completed_at TEXT
        );

        -- description, model, result, parent_task_id and depends_on are all
        -- read and written by routes/db_orchestrators.py (task_get/task_create/
        -- task_update) and were entirely absent here -- this table definition
        -- had drifted from what the code actually uses in the same way
        -- orchestrators' did. started_at, finished_at and task_list, conversely,
        -- are not referenced anywhere in the codebase and are not part of the
        -- production schema either; dropped rather than carried forward as
        -- unused columns nothing ever reads.
        CREATE TABLE IF NOT EXISTS orchestrator_tasks (
            id             TEXT PRIMARY KEY,
            orchestrator_id  TEXT NOT NULL REFERENCES orchestrators(id),
            title          TEXT NOT NULL,
            description    TEXT,
            status         TEXT NOT NULL DEFAULT 'pending',
            model          TEXT,
            result         TEXT,
            progress_pct   REAL NOT NULL DEFAULT 0.0,
            parent_task_id TEXT REFERENCES orchestrator_tasks(id),
            depends_on     TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_orch_tasks_super
            ON orchestrator_tasks(orchestrator_id);
        -- idx_orch_tasks_sup (on priority) is created after
        -- _ensure_orchestrator_columns runs, not here: CREATE TABLE IF NOT
        -- EXISTS is a no-op against a database that already had this table
        -- before `priority` was added to it, so an index referencing that
        -- column in the same script crashed db.init() outright on any such
        -- database with "no such column: priority" -- before the app ever
        -- got to serve a single request.

        -- metadata was missing entirely -- same drift shape as orchestrators
        -- and orchestrator_tasks above, and this table's own version of the
        -- crash: "table orchestrator_messages has no column named metadata"
        -- on the first message ever inserted into a fresh database.
        CREATE TABLE IF NOT EXISTS orchestrator_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            orchestrator_id TEXT NOT NULL REFERENCES orchestrators(id),
            role          TEXT NOT NULL DEFAULT 'system',
            content       TEXT NOT NULL,
            metadata      TEXT,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orch_msgs_sup ON orchestrator_messages(orchestrator_id, id);

        -- No surrogate id: the composite primary key IS the uniqueness
        -- constraint routes/db_orchestrators.py's supervisor_member_add
        -- depends on (INSERT ... ON CONFLICT(orchestrator_id, chat_id) DO
        -- NOTHING). A version of this table with a plain, non-unique index
        -- in place of the primary key shipped briefly and made every
        -- ON CONFLICT crash with "does not match any PRIMARY KEY or UNIQUE
        -- constraint" -- this production database was never actually
        -- created from that version, which is why it kept working.
        CREATE TABLE IF NOT EXISTS orchestrator_members (
            orchestrator_id TEXT NOT NULL REFERENCES orchestrators(id),
            chat_id       TEXT NOT NULL,
            added_at      TEXT NOT NULL,
            PRIMARY KEY (orchestrator_id, chat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_orch_members_sup
            ON orchestrator_members(orchestrator_id, chat_id);
        CREATE INDEX IF NOT EXISTS idx_orch_members_chat
            ON orchestrator_members(chat_id);

        -- Orchestrator progress: the latest state snapshot for each
        -- orchestrator.  A single row per orchestrator, updated after each
        -- step so the polling UI never needs to scan the full message
        -- history.
        CREATE TABLE IF NOT EXISTS orchestrator_progress (
            orchestrator_id TEXT PRIMARY KEY,
            step          TEXT NOT NULL DEFAULT '',
            details       TEXT NOT NULL DEFAULT '{}',
            updated_at    TEXT NOT NULL
        );

        -- Per-host statistics, sampled every 30 seconds by the background
        -- sysstats worker.  The orchestrator UI queries these on the stats
        -- panel so the operator can see whether the machine is saturated.
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
    await _migrate_ssh_proxy_machines_to_transports()
    await _clear_dangling_machine_pins()
    await _ensure_usage_columns()
    await _ensure_orchestrator_columns()
    await _backfill_orchestrators_from_supervisors()
    await db_conn.commit()

    # Retention pruning. Imported directly rather than via db.usage_prune /
    # db.system_prune: __getattr__ resolves those by importing routes.db_usage,
    # which itself does `import db` -- calling back through db's own
    # __getattr__ while init() is still running is the circular path that
    # broke this the first time it was extracted.
    import routes.db_usage as _usage

    await _usage.usage_prune(config.USAGE_RETENTION_DAYS)
    await _usage.system_prune(config.SYSTEM_RETENTION_DAYS)
    # Admin actions: keep 90 days of audit trail.
    await _admin_actions_prune(config.USAGE_RETENTION_DAYS)


async def close() -> None:
    global db_conn
    if db_conn:
        await db_conn.close()
        db_conn = None


async def _admin_actions_prune(keep_days: int) -> None:
    """Delete admin audit entries older than *keep_days*.

    Mirrors the pattern used by usage_prune / system_prune: a simple
    DELETE driven by a retention setting so the audit trail doesn't
    grow unbounded.
    """
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=keep_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db_conn.execute(
        "DELETE FROM admin_actions WHERE created_at < ?", (cutoff,)
    )
    await db_conn.commit()


_SUPERVISOR_BACKFILL: Final[tuple[tuple[str, str, str], ...]] = (
    # (legacy table, new table, the one column whose name changed)
    ("supervisors", "orchestrators", ""),
    ("supervisor_tasks", "orchestrator_tasks", "supervisor_id"),
    ("supervisor_messages", "orchestrator_messages", "supervisor_id"),
    ("supervisor_members", "orchestrator_members", "supervisor_id"),
    ("supervisor_progress", "orchestrator_progress", "supervisor_id"),
)


async def _backfill_orchestrators_from_supervisors() -> None:
    """Copy pre-rename `supervisor*` rows into the `orchestrator*` tables.

    The supervisor -> orchestrator rename created a second, empty set of
    tables and repointed every reader at them. It shipped no data migration,
    so every orchestrator, task, message and member that existed before the
    rename became unreachable: the rows were still on disk, and nothing read
    them any more. On this deployment that was 2 orchestrators, 3 tasks, 25
    messages and 6 members, and from the interface they had simply vanished.

    Deliberately conservative, because it runs on every startup against a
    live database:

    * per table pair, it copies only when the legacy table exists, the new
      table is **empty**, and the legacy table is not -- so it is a one-shot
      that can never double-insert, and it stops applying the moment real
      post-rename data exists;
    * columns are matched by intersecting both tables' actual `PRAGMA
      table_info`, with the single renamed foreign key mapped explicitly, so
      a schema that has drifted on either side cannot silently write a row
      into the wrong columns;
    * the legacy tables are never dropped, altered, or emptied. If anything
      about the copy turns out to be wrong, the original rows are still
      exactly where they were.

    A failure here must not stop the app from starting: an unreadable legacy
    table is a reason to serve without the old orchestrators, not a reason to
    serve nothing at all.
    """
    cursor = await db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    tables = {row["name"] for row in await cursor.fetchall()}

    for legacy, new, renamed_fk in _SUPERVISOR_BACKFILL:
        if legacy not in tables or new not in tables:
            continue
        try:
            cursor = await db_conn.execute(f"SELECT count(*) AS n FROM {new}")
            if (await cursor.fetchone())["n"]:
                continue  # already holds post-rename data; never touch it
            cursor = await db_conn.execute(f"SELECT count(*) AS n FROM {legacy}")
            if not (await cursor.fetchone())["n"]:
                continue  # nothing to carry over

            cursor = await db_conn.execute(f"PRAGMA table_info({legacy})")
            legacy_columns = [row["name"] for row in await cursor.fetchall()]
            cursor = await db_conn.execute(f"PRAGMA table_info({new})")
            new_columns = {row["name"] for row in await cursor.fetchall()}

            # `supervisor_id` -> `orchestrator_id`; everything else keeps its
            # name. Anything the new table does not have is left behind rather
            # than guessed at.
            pairs: list[tuple[str, str]] = []
            for column in legacy_columns:
                if renamed_fk and column == renamed_fk:
                    target = "orchestrator_id"
                else:
                    target = column
                if target in new_columns:
                    pairs.append((target, column))
            if not pairs:
                continue

            targets = ", ".join(target for target, _ in pairs)
            sources = ", ".join(source for _, source in pairs)
            await db_conn.execute(
                f"INSERT INTO {new} ({targets}) SELECT {sources} FROM {legacy}"
            )
            _log.info(
                "orchestrator_backfill copied %s -> %s columns=%d",
                legacy, new, len(pairs),
            )
        except Exception:
            _log.exception("orchestrator_backfill failed for %s -> %s", legacy, new)


async def _ensure_orchestrator_columns() -> None:
    """Apply additive orchestrator/orchestrator_tasks schema migrations for
    existing databases, then create the indexes that depend on them.

    Covers columns used by routes/db_orchestrators.py that the CREATE TABLE
    text had drifted out of sync with:

    * `orchestrator_tasks.priority` was added straight into the CREATE TABLE
      IF NOT EXISTS in the same executescript as the index that reads it --
      a no-op against a database that already had this table, so the index
      creation right after it crashed db.init() outright with "no such
      column: priority" on any such database, before the app ever served a
      request.
    * `orchestrators.config`/`plan`/`progress_pct`/`completed_at` and
      `orchestrator_tasks.description`/`model`/`result`/`progress_pct`/
      `parent_task_id`/`depends_on` were absent from the CREATE TABLE text
      entirely, so even a brand new database crashed on the first
      orchestrator or task ever created. This deployment's own database
      survived only because it already had all of them from an earlier,
      richer schema -- an existing database that predates that schema
      still needs the ALTERs below.

    Same check-then-ALTER pattern as _ensure_chat_columns for the same
    reason: it is idempotent and safe to run on every startup.
    """
    cursor = await db_conn.execute("PRAGMA table_info(orchestrators)")
    sup_columns = {row["name"] for row in await cursor.fetchall()}
    sup_migrations = {
        "config": "ALTER TABLE orchestrators ADD COLUMN config TEXT NOT NULL DEFAULT '{}'",
        "plan": "ALTER TABLE orchestrators ADD COLUMN plan TEXT",
        "progress_pct": (
            "ALTER TABLE orchestrators ADD COLUMN progress_pct "
            "REAL NOT NULL DEFAULT 0.0"
        ),
        "completed_at": "ALTER TABLE orchestrators ADD COLUMN completed_at TEXT",
        "degraded": "ALTER TABLE orchestrators ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0",
        "degraded_reason": "ALTER TABLE orchestrators ADD COLUMN degraded_reason TEXT",
    }
    for name, sql in sup_migrations.items():
        if name not in sup_columns:
            await db_conn.execute(sql)

    cursor = await db_conn.execute("PRAGMA table_info(orchestrator_tasks)")
    columns = {row["name"] for row in await cursor.fetchall()}
    task_migrations = {
        "description": "ALTER TABLE orchestrator_tasks ADD COLUMN description TEXT",
        "model": "ALTER TABLE orchestrator_tasks ADD COLUMN model TEXT",
        "result": "ALTER TABLE orchestrator_tasks ADD COLUMN result TEXT",
        "progress_pct": (
            "ALTER TABLE orchestrator_tasks ADD COLUMN progress_pct "
            "REAL NOT NULL DEFAULT 0.0"
        ),
        "parent_task_id": (
            "ALTER TABLE orchestrator_tasks ADD COLUMN parent_task_id TEXT "
            "REFERENCES orchestrator_tasks(id)"
        ),
        "depends_on": "ALTER TABLE orchestrator_tasks ADD COLUMN depends_on TEXT",
        "priority": (
            "ALTER TABLE orchestrator_tasks ADD COLUMN priority "
            "INTEGER NOT NULL DEFAULT 0"
        ),
    }
    for name, sql in task_migrations.items():
        if name not in columns:
            await db_conn.execute(sql)
    await db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orch_tasks_sup "
        "ON orchestrator_tasks(orchestrator_id, priority DESC)"
    )

    cursor = await db_conn.execute("PRAGMA table_info(orchestrator_messages)")
    msg_columns = {row["name"] for row in await cursor.fetchall()}
    if "metadata" not in msg_columns:
        await db_conn.execute(
            "ALTER TABLE orchestrator_messages ADD COLUMN metadata TEXT"
        )


async def _clear_dangling_machine_pins() -> None:
    """Unpin chats whose pinned backend no longer exists. Idempotent.

    `chats.ai_machine_id` pins a conversation to one backend, and there is no
    foreign key on it (this schema has none anywhere), so deleting a machine
    left every chat that named it pointing at an id that resolves to nothing.
    The frontend's `ensurePinnedModels` then asked
    `/api/models?machine_id=<gone>` every time such a conversation was opened,
    and the server answered 404 "Machine not found" -- observed live on this
    deployment: five chats pinned to one deleted machine, each open logging an
    error.

    NULL is the correct repair rather than a guess at a replacement: an empty
    pin already means "follow whichever backend is active", which is the
    picker's own default ("Follow active"). Choosing a substitute backend would
    silently route an old conversation somewhere its author never picked, and
    §0.1 of CLAUDE.md is about exactly how badly that reads when the model ids
    do not match.

    Runs every startup because it is a repair, not a one-shot: `ai_machine_delete`
    now clears these at the source, but a row written by an older build -- or by
    any future path that deletes a machine without going through it -- would
    otherwise sit dangling indefinitely.
    """
    cur = await db_conn.execute(
        "UPDATE chats SET ai_machine_id = NULL WHERE ai_machine_id IS NOT NULL "
        "AND ai_machine_id NOT IN (SELECT id FROM ai_machines)"
    )
    if cur.rowcount:
        _log.info("cleared %d dangling chat->machine pin(s)", cur.rowcount)


async def _migrate_ssh_proxy_machines_to_transports() -> None:
    """One-time, idempotent: turn every remaining provider='ssh_proxy'
    ai_machines row into an ssh_transports row plus one backend copying its
    owner's active claude_code/anthropic-compatible machine's own fields
    (model/base_url/api_key/active_models), pointed at the new transport.

    Why copy from the active machine rather than leave the new backend
    empty: both real ssh_proxy rows found in production (Kali3,
    Pentester-Kali_Mac) already declared the *same* model as the owner's
    real backend's own default, which was never actually read for anything
    except get_default_model -- the strongest available signal that the
    original intent was "run that backend, but from over there," not "this
    machine has its own separate identity." See
    docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.

    Safe to call every startup: it only ever acts on provider='ssh_proxy'
    rows, and this migration deletes every one it processes, so a second
    run finds none left and does nothing.
    """
    import uuid

    from routes.db_machines import _BACKEND_COLUMNS

    cur = await db_conn.execute(
        "SELECT id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
        "ssh_host_key_fingerprint FROM ai_machines WHERE provider = 'ssh_proxy'"
    )
    old_rows = [dict(r) for r in await cur.fetchall()]
    if not old_rows:
        return

    for old in old_rows:
        owner_id = old["owner_id"]
        # The owner's active backend, if any -- what the new machine copies.
        active_cur = await db_conn.execute(
            f"SELECT {_BACKEND_COLUMNS} FROM ai_machines "  # nosec B608: static columns
            "WHERE owner_id = ? AND active = 1 AND provider != 'ssh_proxy' LIMIT 1",
            (owner_id,),
        )
        active_row = await active_cur.fetchone()

        transport_id = uuid.uuid4().hex
        now = _now()
        await db_conn.execute(
            "INSERT INTO ssh_transports "
            "(id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
            " ssh_host_key_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transport_id, old["name"], owner_id, old["ssh_host"],
                old["ssh_user"], old["ssh_key_path"],
                old["ssh_host_key_fingerprint"], now, now,
            ),
        )

        if active_row:
            active = dict(active_row)
            new_machine_id = uuid.uuid4().hex
            await db_conn.execute(
                "INSERT INTO ai_machines "
                "(id, name, provider, host, port, api_key, model, base_url, "
                " description, active, owner_id, created_at, updated_at, "
                " transport_id, active_models) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, "
                "        (SELECT active_models FROM ai_machines WHERE id = ?))",
                (
                    new_machine_id, f"{active['name']} (via {old['name']})",
                    active["provider"], active["host"], active["port"],
                    active["api_key"], active["model"], active["base_url"],
                    None, owner_id, now, now, transport_id, active["id"],
                ),
            )
            _log.info(
                "ssh_proxy_migrated old_machine=%s -> transport=%s new_backend=%s",
                old["id"], transport_id, new_machine_id,
            )
        else:
            _log.warning(
                "ssh_proxy_migrated old_machine=%s -> transport=%s, no active "
                "backend found for owner=%s to copy -- transport created with "
                "no linked backend, add one by hand in Settings",
                old["id"], transport_id, owner_id,
            )

        await db_conn.execute("DELETE FROM ai_machines WHERE id = ?", (old["id"],))
        await db_conn.execute("DELETE FROM ssh_tunnels WHERE machine_id = ?", (old["id"],))

    await db_conn.commit()


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
        "question_ids": (
            "ALTER TABLE chats ADD COLUMN question_ids TEXT NOT NULL DEFAULT ''"
        ),
        "orchestrator": "ALTER TABLE chats ADD COLUMN orchestrator TEXT",
        # Per-chat auto-approval of permission and plan-approval prompts.
        # Default 0, and deliberately not settable globally: on, this chat
        # approves the prompts that exist to ask a person, and the answer is a
        # keystroke into a live terminal with nothing to undo.
        "auto_answer": (
            "ALTER TABLE chats ADD COLUMN auto_answer INTEGER NOT NULL DEFAULT 0"
        ),
        # The last ten answers and skips, newest first, as a JSON array. Capped
        # on write rather than pruned later, so the column cannot grow: it is
        # read with the chat, and a long-running conversation would otherwise
        # accumulate one entry per approval for ever.
        "auto_answer_log": "ALTER TABLE chats ADD COLUMN auto_answer_log TEXT",
        # A second, independent authority beyond plain approval: with this on,
        # a structured AskUserQuestion whose author marked exactly one option
        # "(Recommended)" gets that option pressed too. Meaningless with
        # auto_answer off, but stored separately rather than folded into a
        # three-valued auto_answer column -- the existing column's 0/1
        # semantics (every reader of it) stay exactly what they were.
        "auto_answer_recommend": (
            "ALTER TABLE chats ADD COLUMN auto_answer_recommend "
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "degraded": "ALTER TABLE chats ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0",
        "degraded_reason": "ALTER TABLE chats ADD COLUMN degraded_reason TEXT",
        "degraded_at": "ALTER TABLE chats ADD COLUMN degraded_at TEXT",
        "voice_mode": (
            # Set once at chat creation from the sidebar's voice button,
            # immutable after — no mid-conversation toggle. Gates whether
            # stream_handler dispatches to the direct-model-call path
            # (routes/voice.py) instead of the claude CLI.
            "ALTER TABLE chats ADD COLUMN voice_mode INTEGER NOT NULL DEFAULT 0"
        ),
        "type": (
            # Chat conversation type: 'normal' or 'brainstorming'.
            # Voice-mode chats are forced to 'brainstorming' and locked.
            "ALTER TABLE chats ADD COLUMN type TEXT NOT NULL DEFAULT 'normal'"
        ),
        "parent_chat_id": "ALTER TABLE chats ADD COLUMN parent_chat_id TEXT",
        "is_temporary": "ALTER TABLE chats ADD COLUMN is_temporary INTEGER NOT NULL DEFAULT 0",
    }
    for name, sql in migrations.items():
        if name not in columns:
            await db_conn.execute(sql)

    # Index on parent_chat_id for handoff lookup (parent → children).
    await db_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chats_parent ON chats(parent_chat_id)"
    )

    # Backfill: voice-mode chats that existed before the `type` column was
    # created default to 'normal' (the column DEFAULT).  Promote them all
    # to 'brainstorming' now — only runs once because voice_mode+normal
    # counts drop to zero after the first pass.
    try:
        cur = await db_conn.execute(
            "UPDATE chats SET type = 'brainstorming' "
            "WHERE voice_mode = 1 AND type = 'normal'"
        )
        await db_conn.commit()
        if cur.rowcount:
            _log.info(
                "type_backfill: updated %d voice-mode chats to brainstorming",
                cur.rowcount,
            )
    except Exception:
        _log.warning("type_backfill failed (non-fatal)")

    # Migrate ai_machines table for existing databases
    try:
        ma_cursor = await db_conn.execute("PRAGMA table_info(ai_machines)")
        ma_columns = {row["name"] for row in await ma_cursor.fetchall()}
    except Exception:
        ma_columns = set()
    if "owner_id" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'"
        )
    # usage_events gained cost_basis after first release.
    try:
        ue_cursor = await db_conn.execute("PRAGMA table_info(usage_events)")
        ue_columns = {row["name"] for row in await ue_cursor.fetchall()}
    except Exception:
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
            "ALTER TABLE ai_machines ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude_code'"
        )
    # Rename old provider literals to the canonical set.
    await db_conn.execute(
        "UPDATE ai_machines SET provider = 'claude_code' WHERE provider = 'anthropic'"
    )
    await db_conn.execute(
        "UPDATE ai_machines SET provider = 'claude_code' WHERE provider = 'proxy'"
    )
    try:
        rm_cursor = await db_conn.execute("PRAGMA table_info(read_marks)")
        rm_columns = {row["name"] for row in await rm_cursor.fetchall()}
    except Exception:
        rm_columns = set()
    if rm_columns and "dismissed_at" not in rm_columns:
        await db_conn.execute("ALTER TABLE read_marks ADD COLUMN dismissed_at TEXT")

    if ma_columns and "active_models" not in ma_columns:
        # '[]' means "offer everything served", which is what existing rows did.
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN active_models TEXT NOT NULL DEFAULT '[]'"
        )

    # SSH proxy: per-machine columns on ai_machines.
    if ma_columns and "ssh_host" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN ssh_host TEXT NOT NULL DEFAULT ''"
        )
    if ma_columns and "ssh_user" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN ssh_user TEXT NOT NULL DEFAULT 'kali'"
        )
    if ma_columns and "ssh_key_path" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN ssh_key_path TEXT NOT NULL DEFAULT ''"
        )
    if ma_columns and "ssh_host_key_fingerprint" not in ma_columns:
        # Trust-on-first-use pin for the remote SSH host key. paramiko's
        # AutoAddPolicy (bandit B507, CWE-295) accepted *any* host key
        # silently on every connection -- no verification at all, so a
        # MITM sitting between this host and the configured ssh_host was
        # undetectable. Blank means "not yet pinned"; connect() fills it in
        # on the first successful connection and rejects any *later*
        # connection whose key doesn't match, which is what actually
        # catches a MITM or a reinstalled host -- not blocking the first
        # connection outright, which strict verification against this
        # host's own ~/.ssh/known_hosts would have done here: nothing in
        # it mentions any of this deployment's configured ssh_proxy hosts
        # yet (checked directly), so requiring a pre-existing known_hosts
        # entry would have re-broken the very connection this session's
        # other fixes just got working.
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN ssh_host_key_fingerprint TEXT NOT NULL DEFAULT ''"
        )

    if ma_columns and "transport_id" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN transport_id TEXT"
        )

    # Whether a backend may be used at all -- a different question from
    # `active`, which on this table means "is the default".
    #
    # DEFAULT 1 is what makes this additive migration safe on a live host:
    # every backend that already exists stays usable, and a database predating
    # the column behaves exactly as it did before. There is no backfill and so
    # nothing to get wrong on a host that is mid-work.
    #
    # The column names disagree with the interface's vocabulary (Default /
    # Active / Inactive) deliberately. Renaming `active` would flip the
    # meaning of a column seven separate resolvers read, and any resolver
    # missed would then see active=1 for every backend and silently pick an
    # arbitrary one -- a routing bug with no error anywhere. The rename happens
    # in the UI, where it costs a label. See the spec's "Naming" section.
    if ma_columns and "enabled" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )

    # ssh_tunnels: one row per active ssh_proxy machine.
    try:
        st_cursor = await db_conn.execute("PRAGMA table_info(ssh_tunnels)")
        st_columns = {row["name"] for row in await st_cursor.fetchall()}
    except Exception:
        st_columns = set()

    if not st_columns:
        await db_conn.execute("""
            CREATE TABLE ssh_tunnels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id      TEXT NOT NULL DEFAULT 'admin',
                machine_id    INTEGER NOT NULL,
                local_port    INTEGER NOT NULL,
                ssh_port      INTEGER NOT NULL DEFAULT 9000,
                tunnel_up     INTEGER NOT NULL DEFAULT 0,
                proxy_ok      INTEGER NOT NULL DEFAULT 0,
                state         TEXT NOT NULL DEFAULT 'disconnected',
                concurrent_conns INTEGER NOT NULL DEFAULT 0,
                connected_at  TEXT,
                last_check    TEXT,
                error_msg     TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                UNIQUE(machine_id)
            )
        """)
    else:
        _st_fields = (
            "owner_id", "local_port", "ssh_port", "tunnel_up",
            "proxy_ok", "state", "concurrent_conns", "connected_at",
            "last_check", "error_msg", "created_at", "updated_at",
        )
        for col in _st_fields:
            if col not in st_columns:
                await db_conn.execute(
                    f"ALTER TABLE ssh_tunnels ADD COLUMN {col} TEXT"
                )  # nosec B608: column names are static literals
        # Patch non-text columns that PRAGMA defaults to TEXT.
        _int_cols = [
            ("local_port", "INTEGER"), ("ssh_port", "INTEGER"),
            ("tunnel_up", "INTEGER"), ("proxy_ok", "INTEGER"),
            ("concurrent_conns", "INTEGER"),
        ]
        for col, ctype in _int_cols:
            if col in st_columns:
                try:
                    await db_conn.execute(
                        f"ALTER TABLE ssh_tunnels MODIFY COLUMN {col} {ctype}"
                    )
                except Exception:
                    pass

    # system_samples: add host_type and host_id for remote stats.
    try:
        ss_cursor = await db_conn.execute("PRAGMA table_info(system_samples)")
        ss_columns = {row["name"] for row in await ss_cursor.fetchall()}
    except Exception:
        ss_columns = set()
    for col in ("host_type", "host_id", "data"):
        if col not in ss_columns:
            default = "'local'" if col in ("host_type", "host_id") else "'{}'"
            await db_conn.execute(
                f"ALTER TABLE system_samples ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
            )  # nosec B608: column names are static literals

    await db_conn.commit()


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


async def _reopen() -> None:
    """Reconnect and apply additive migrations. Never leaves db_conn as None."""
    await init()


# ── SSH tunnel CRUD ──────────────────────────────────────────────────


async def ssh_tunnel_get(machine_id: str) -> dict | None:
    """Return one ssh_tunnels row by machine_id or None."""
    cursor = await db_conn.execute(
        "SELECT * FROM ssh_tunnels WHERE machine_id = ?",
        (machine_id,),
    )
    # fetchall() is a coroutine too -- missing this await meant `rows` was
    # the coroutine object itself, always truthy (`not rows` never True), so
    # every call fell through to `rows[0]` and raised
    # `TypeError: 'coroutine' object is not subscriptable`. Caught by
    # tunnel_start's broad `except Exception` and logged as "will use
    # existing" -- so the ssh_tunnels row was never created, on top of (and
    # independently of) the int(machine_id) bug in the same call chain.
    rows = await cursor.fetchall()
    if not rows:
        return None
    return rows[0]


async def ssh_tunnel_list_active() -> list[dict]:
    """Return all ssh_tunnels rows with tunnel_up=1."""
    cursor = await db_conn.execute(
        "SELECT * FROM ssh_tunnels WHERE tunnel_up = 1"
    )
    # Same missing-await as ssh_tunnel_get: fetchall() is a coroutine, so
    # this returned the coroutine object itself instead of a list of rows.
    return await cursor.fetchall()


async def ssh_tunnel_create(
    machine_id: str,
    local_port: int,
    ssh_port: int = 22,
) -> int:
    """Insert a new ssh_tunnels row. Returns row id."""
    now = _now()
    cursor = await db_conn.execute(
        """INSERT INTO ssh_tunnels
               (machine_id, local_port, ssh_port, tunnel_up, proxy_ok,
                state, concurrent_conns, connected_at, last_check, error_msg,
                created_at, updated_at)
            VALUES (?, ?, ?, 0, 0, 'disconnected', 0, NULL, NULL, NULL, ?, ?)
        """,
        (machine_id, local_port, ssh_port, now, now),
    )
    await db_conn.commit()
    return cursor.lastrowid


async def ssh_tunnel_update(
    machine_id: str,
    **fields,
) -> bool:
    """Update an SSH tunnel row when every requested field is allowlisted."""
    allowed = {
        "local_port", "ssh_port", "tunnel_up", "proxy_ok", "state",
        "concurrent_conns", "connected_at", "last_check", "error_msg",
    }
    if not fields or not set(fields).issubset(allowed):
        return False
    fields = {**fields, "updated_at": _now()}
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    values = [*fields.values(), machine_id]
    cursor = await db_conn.execute(
        f"UPDATE ssh_tunnels SET {set_clause} WHERE machine_id = ?",
        values,
    )
    await db_conn.commit()
    return bool(cursor.rowcount)


async def ssh_tunnel_delete(machine_id: str) -> None:
    """Delete the ssh_tunnels row for *machine_id*."""
    await db_conn.execute(
        "DELETE FROM ssh_tunnels WHERE machine_id = ?",
        (machine_id,),
    )
    await db_conn.commit()


# system_sample_insert/system_latest/system_series/system_prune now live in
# routes.db_usage (see __getattr__ above) against the current flat-column
# schema (SYSTEM_FIELDS). A stale (host_type, host_id, data) duplicate of
# system_sample_insert used to be defined here too -- left over from the
# module split -- and it shadowed the __getattr__ forward for anyone calling
# db.system_sample_insert, since a real module-level name always wins over
# __getattr__. sysstats.py's background loop is exactly such a caller
# (`sysstats.start(db.system_sample_insert)`), so every local-host sample hit
# this dead function's 4-column INSERT with a single flattened dict where it
# expected three scalar args, and crashed on every interval:
# `sqlite3.ProgrammingError: Error binding parameter 1: type 'dict' is not
# supported`. Local system_samples rows have not been written since -- the
# stats-graph gaps this session started with (`bucket_spine`/`fill=True`)
# were the rendering half of the problem; this was the collection half.
# system_sample_list (host_type, limit) was the read-side twin, also stale,
# also unreachable through __getattr__ for the same shadowing reason, and
# had zero callers left anywhere in the codebase.
