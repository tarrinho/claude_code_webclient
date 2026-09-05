# db_backup.py — Database backup and restore.
#
# Extracted from db.py so the admin routes do not need the full
# database module.

import asyncio
import gzip as _gzip
import logging
import sqlite3
import time
from pathlib import Path

import db

_log = logging.getLogger("wc.db.backup")


def _db_backup_sync(backup_path: str) -> bytes:
    """Copy the database and return it gzip-compressed. Blocking; call off-loop."""
    sync_conn = sqlite3.connect(backup_path)
    db_conn_sync = sqlite3.connect(str(db.config.DB_PATH))
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
    backup_path = f"{db.config.DB_PATH}.backup.{int(time.time())}"
    try:
        return await asyncio.to_thread(_db_backup_sync, backup_path)
    finally:
        try:
            Path(backup_path).unlink()
        except OSError:
            pass


def _validate_sqlite_file(path: Path) -> bool:
    """Return True if *path* is a sound SQLite database with our schema.

    ``PRAGMA integrity_check`` and the table-name check confirm the file is a
    coherent database with our tables, but neither rejects a file that is
    *also* a coherent database carrying something our own schema never
    creates: a trigger or a view. Our schema (see db.py's init()) has none of
    either, so their presence at all means the upload added them -- and a
    ``CREATE TRIGGER ... AFTER INSERT ON messages`` fires on the app's very
    next ordinary write, running whatever SQL the file's author wrote with the
    app's own database permissions. Rejected outright rather than inspected:
    there is no legitimate reason for one to be present, so distinguishing a
    "safe" trigger from a malicious one is a problem this file need not solve.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'trigger', 'view')"
        ).fetchall()
        names = {r[0] for r in rows if r[1] == "table"}
        if not {"chats", "messages", "users"}.issubset(names):
            return False
        unexpected = [r[0] for r in rows if r[1] in ("trigger", "view")]
        if unexpected:
            _log.warning(
                "db_restore_rejected: file carries trigger/view objects our "
                "schema never creates: %s", unexpected,
            )
            return False
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


async def db_restore(data: bytes) -> bool:
    """Replace the current database with the provided gzip-compressed data."""
    try:
        decompressed = _gzip.decompress(data)
    except Exception:
        return False

    if not decompressed.startswith(db._SQLITE_MAGIC):
        return False

    db_path = Path(db.config.DB_PATH)
    tmp_path = db_path.with_suffix(".restore.tmp")

    try:
        tmp_path.write_bytes(decompressed)
        if not await asyncio.to_thread(_validate_sqlite_file, tmp_path):
            tmp_path.unlink(missing_ok=True)
            return False

        await db.close()

        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

        tmp_path.replace(db_path)

        await db._reopen()
        return True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        try:
            await db._reopen()
        except Exception:
            global db_conn
            db_conn = None
        return False
