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
    """Return True if *path* is a sound SQLite database with our schema."""
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
        return {"chats", "messages", "users"}.issubset(names)
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
    except Exception:  # noqa: BLE001 -- silently reject bad input
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
    except Exception:  # noqa: BLE001 -- recover best-effort on failure
        tmp_path.unlink(missing_ok=True)
        try:
            await db._reopen()
        except Exception:  # noqa: BLE001 -- final fallback, DB may be unusable
            global db_conn
            db_conn = None
        return False
