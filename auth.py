# auth.py — password hashing + server-side sessions for the WebConsole.
#
# Passwords: Argon2id via argon2-cffi. Sessions: server-side in-memory, each
# revalidated against DB on every request. Cookie: HttpOnly, Secure (unless
# WC_COOKIE_ALLOW_INSECURE), SameSite=Strict.
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import os
import secrets
import sqlite3
import time
from typing import Any, Final

import config

_log = logging.getLogger("wc.auth")

# How long the one-time session migration will wait on a busy database.
#
# Kept well under app.py's per-startup-step timeout, so a contended database
# surfaces here as "migration skipped this boot" -- which is harmless, the
# migration is idempotent and retries next start -- rather than as the whole
# startup being abandoned. Python's sqlite3 default is 5.0s; this is explicit
# because the value matters relative to that other timeout, and a default that
# quietly changes underneath is exactly the kind of coupling worth naming.
_MIGRATION_TIMEOUT_S: float = 5.0

_VALID_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_N, _R, _P = 1 << 14, 8, 1
_DKLEN = 32

# ───────────────────────────── password hashing ──────────────────────────────────────────

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import (
        InvalidHashError,
        VerificationError,
        VerifyMismatchError,
    )

    _PH = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=1)

    def hash_password(pw: str) -> str:
        return _PH.hash(pw[:_MAX_PASSWORD_BYTES])

    def verify_password(pw: str, stored: str) -> bool:
        try:
            _PH.verify(stored, pw)
            return True
        except (InvalidHashError, VerifyMismatchError, VerificationError):
            return False

except ImportError:
    # Fallback to scrypt if argon2-cffi not installed.
    def hash_password(pw: str) -> str:
        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(
            pw[:_MAX_PASSWORD_BYTES].encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
        )

        def _b64(b):
            return base64.b64encode(b).decode()

        return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"

    def verify_password(pw: str, stored: str) -> bool:
        try:
            scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
            if scheme != "scrypt":
                return False
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            dk = hashlib.scrypt(
                pw.encode(),
                salt=salt,
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
            )
            return hmac.compare_digest(dk, expected)
        except (ValueError, TypeError, UnicodeError):
            return False


# Argon2 effective entropy cap: 72 bytes (same as bcrypt). Longer inputs
# are wasted work for the defender and enable slow-login DoS.
_MAX_PASSWORD_BYTES: Final[int] = 72


def password_error(pw: str) -> str | None:
    if not pw or len(pw) < 8:
        return "password must be at least 8 characters"
    if len(pw) > _MAX_PASSWORD_BYTES:
        return "password too long"
    return None


# ───────────────────────────── session store ─────────────────────────────────────────────
# Keyed by a hash of the session id, never the id itself. The id only ever
# exists in the user's cookie, so neither this dict nor the database row can be
# replayed as a credential if either is read -- which matters because the
# database is downloadable through /api/admin/export.
_sessions: dict[str, dict] = {}

# How far `last` may drift before it is written back. Sessions live in memory
# and are mirrored to SQLite so a restart does not log everyone out; persisting
# every touch would mean a write per request, so the idle clock is allowed to
# lag on disk by this much.
_LAST_PERSIST_S: Final[int] = 60
_persisted_last: dict[str, float] = {}


# How long a session write waits for the SQLite writer lock. Must equal
# db._BUSY_TIMEOUT_MS: this connection and db.py's shared one are two writers
# against a WAL file that permits one, and the shorter budget always loses.
# Declared here rather than imported because auth.py is reached from the
# middleware, and a module cycle would be a worse problem than a duplicated
# integer. `tests/test_qa_busy_timeout.py` holds the two equal.
_BUSY_TIMEOUT_MS: Final[int] = 15000


def _sid_key(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def _session_conn() -> sqlite3.Connection | None:
    """A short-lived synchronous handle, or None if the database is not ready.

    Sessions live in their own database file (SESSION_DB_PATH) so they never
    collide with the app's aiosqlite writer (registry #47).
    Calls `_ensure_session_db_file()` to guarantee the table exists before
    opening the connection — tests patch SESSION_DB_PATH to a throwaway that
    is not backed by `db.init()`.
    """
    _ensure_session_db_file()
    try:
        conn = sqlite3.connect(
            str(config.SESSION_DB_PATH),
            timeout=_BUSY_TIMEOUT_MS / 1000,
        )
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn
    except sqlite3.Error:
        return None


def _migrate_sessions_to_own_db() -> int:
    """Move sessions from DB_PATH (the old combined file) to SESSION_DB_PATH.

    Returns the number of rows migrated. Called once at startup; idempotent.
    """
    if not os.path.exists(config.SESSION_DB_PATH):
        _ensure_session_db_file()
    if not os.path.exists(config.DB_PATH):
        return 0
    # The common case is "already migrated", so ask that question read-only and
    # get out. This runs at startup, before the socket is bound, against the
    # production database that the rest of the process and any other instance
    # are also using -- opening it read-write to discover there is nothing to
    # do took a write-capable connection on the main file for no reason, and
    # any delay here is a delay in binding the port.
    #
    # `mode=ro` cannot create or upgrade the file, which is correct: if the
    # sessions table is missing entirely, sqlite3.Error is raised and caught
    # below as "nothing to migrate", which is the right answer.
    try:
        probe = sqlite3.connect(
            f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=_MIGRATION_TIMEOUT_S
        )
        try:
            src_row_count = probe.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]
        finally:
            probe.close()
    except sqlite3.Error:
        _log.warning("session_migration_probe_failed")
        return 0
    if src_row_count == 0:
        return 0
    try:
        # Only now, with real rows to move, is a writer justified.
        src = sqlite3.connect(str(config.DB_PATH), timeout=_MIGRATION_TIMEOUT_S)
        rows = src.execute("SELECT * FROM sessions").fetchall()
        if not rows:
            src.close()
            return 0
        dst = _get_session_db()
        dst.executescript(
            """CREATE TABLE IF NOT EXISTS sessions (
                sid_key    TEXT PRIMARY KEY,
                user       TEXT NOT NULL,
                role       TEXT NOT NULL,
                expiry     REAL NOT NULL,
                last       REAL NOT NULL,
                csrf       TEXT NOT NULL
            )"""
        )
        dst.executemany(
            "INSERT OR REPLACE INTO sessions VALUES "
            "(?, ?, ?, ?, ?, ?)",
            rows,
        )
        dst.commit()
        dst.close()
        src.execute("DELETE FROM sessions").fetchall()
        src.commit()
        src.close()
        return src_row_count
    except sqlite3.Error:
        _log.warning("session_migration_failed")
        return 0


def _ensure_session_db_file() -> None:
    """Create the sessions table if the DB file does not exist yet.

    If the file exists but is corrupted (e.g. left over from a crash), try
    to recreate it; the table will be rebuilt on next persist call.
    """
    _db_dir = os.path.dirname(config.SESSION_DB_PATH)
    if _db_dir:
        os.makedirs(_db_dir, exist_ok=True)
    try:
        conn = sqlite3.connect(config.SESSION_DB_PATH)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                sid_key    TEXT PRIMARY KEY,
                user       TEXT NOT NULL,
                role       TEXT NOT NULL,
                expiry     REAL NOT NULL,
                last       REAL NOT NULL,
                csrf       TEXT NOT NULL
            )"""
        )
        conn.commit()
        conn.close()
    except sqlite3.DatabaseError:
        # Corrupted file – remove and recreate
        try:
            os.remove(config.SESSION_DB_PATH)
        except OSError:
            pass
        _ensure_session_db_file()


def _get_session_db() -> sqlite3.Connection:
    """A persistent connection to the sessions database."""
    return sqlite3.connect(str(config.SESSION_DB_PATH))


def _persist(key: str, record: dict) -> None:
    conn = _session_conn()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO sessions (sid_key, user, role, expiry, last, csrf) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sid_key) DO UPDATE SET "
                "user=excluded.user, role=excluded.role, expiry=excluded.expiry, "
                "last=excluded.last, csrf=excluded.csrf",
                (key, record["user"], record["role"], record["expiry"],
                 record["last"], record["csrf"]),
            )
        _persisted_last[key] = record["last"]
    except sqlite3.Error as exc:
        # A session that cannot be written still works until the next restart;
        # losing durability must never cost the user their login.
        _log.warning("session_persist_failed: %s", exc)
    finally:
        conn.close()


def _forget(keys: list[str]) -> None:
    if not keys:
        return
    conn = _session_conn()
    if conn is None:
        return
    try:
        with conn:
            conn.executemany("DELETE FROM sessions WHERE sid_key = ?",
                             [(k,) for k in keys])
    except sqlite3.Error as exc:
        _log.warning("session_forget_failed: %s", exc)
    finally:
        for k in keys:
            _persisted_last.pop(k, None)
        conn.close()


def load_sessions() -> int:
    """Restore sessions from the database at startup. Returns how many.

    Without this every restart logged everyone out, which on a phone-first
    console means re-entering a password each time the server is bounced.
    """
    conn = _session_conn()
    if conn is None:
        return 0
    now = time.time()
    restored = 0
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sid_key, user, role, expiry, last, csrf FROM sessions"
        ).fetchall()
        stale = []
        for row in rows:
            # Apply the same expiry and idle rules a live session would face,
            # so a restart cannot resurrect a session that had already lapsed.
            if row["expiry"] <= now or (now - row["last"]) > config.SESSION_IDLE_S:
                stale.append(row["sid_key"])
                continue
            _sessions[row["sid_key"]] = {
                "user": row["user"], "role": row["role"],
                "expiry": row["expiry"], "last": row["last"], "csrf": row["csrf"],
            }
            _persisted_last[row["sid_key"]] = row["last"]
            restored += 1
        if stale:
            with conn:
                conn.executemany("DELETE FROM sessions WHERE sid_key = ?",
                                 [(k,) for k in stale])
    except sqlite3.Error as exc:
        _log.warning("session_restore_failed: %s", exc)
    finally:
        conn.close()
    return restored


def _sweep(now: float) -> None:
    dropped = [s for s, v in _sessions.items() if v["expiry"] <= now]
    for key in dropped:
        _sessions.pop(key, None)
    over = len(_sessions) - config.SESSION_MAX
    if over > 0:
        for key, _v in sorted(_sessions.items(), key=lambda kv: kv[1]["expiry"])[:over]:
            _sessions.pop(key, None)
            dropped.append(key)
    _forget(dropped)


def session_new(user: str, role: str = "admin") -> tuple[str, str]:
    """Create a session; returns (session_id, csrf_token).

    *role* must come from the user's DB record. This used to hardcode
    "admin" for every session, which made the admin checks on the
    /api/admin/* routes decorative -- any authenticated account passed them.
    The default stays "admin" only for the bootstrap admin path.
    """
    now = time.time()
    _sweep(now)
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    key = _sid_key(sid)
    record = {
        "user": user,
        "role": role,
        "expiry": now + config.SESSION_TTL_S,
        "last": now,
        "csrf": csrf,
    }
    _sessions[key] = record
    _persist(key, record)
    return sid, csrf


def session_get(sid: str | None) -> dict | None:
    if not sid:
        return None
    key = _sid_key(sid)
    s = _sessions.get(key)
    if not s:
        return None
    now = time.time()
    if s["expiry"] <= now or (now - s["last"]) > config.SESSION_IDLE_S:
        _sessions.pop(key, None)
        _forget([key])
        return None
    s["last"] = now
    # Write the idle clock back only when it has drifted, so an active session
    # costs one write a minute rather than one per request.
    if now - _persisted_last.get(key, 0.0) > _LAST_PERSIST_S:
        _persist(key, s)
    return s


def session_drop(sid: str | None) -> None:
    if not sid:
        return
    key = _sid_key(sid)
    _sessions.pop(key, None)
    _forget([key])


# Stateless CSRF token: one token per browser, stored in a secure cookie.
# The value is persisted across sessions so the server can validate it on
# mutating requests.  A new token is generated when a login succeeds.
_csrf_store: dict[str, str] = {}  # id → token  (id is the cookie value)


def csrf_generate() -> tuple[str, str]:
    """Return (cookie_value, header_value).

    The cookie_value is what the browser stores; header_value is what the
    client must send back.  They are identical — the server stores a copy
    keyed by cookie_value so that the cookie *is* the token.  This is a
    standard pattern: the client sends it back in a header.
    """
    token = secrets.token_urlsafe(32)
    _csrf_store[token] = token  # idempotent storage; key == value
    return token, token


def csrf_consume(id_value: str) -> bool:
    """Consume and validate a CSRF token.  Returns True on success."""
    return _csrf_store.pop(id_value, None) is not None


def _csrf_valid(cookie_token: str, header_token: str, sid: str | None = None) -> bool:
    """Check the CSRF header against the token bound to *sid*'s session.

    Pass *sid* (the wc_session cookie) so the token is verified against the
    requesting session. Without it this scanned every live session and accepted
    any of their tokens, so a token was never actually bound to its session.

    Comparisons use ``compare_digest`` to avoid leaking the token byte-by-byte
    through response timing.
    """
    if not cookie_token or not header_token:
        return False
    # The double-submit halves must agree before anything else.
    if not secrets.compare_digest(cookie_token, header_token):
        return False
    if sid is None:
        # No session context supplied: fall back to "is this any live token?".
        # Retained so callers that predate the sid parameter keep working.
        return any(
            secrets.compare_digest(s["csrf"], cookie_token)
            for s in _sessions.values()
        )
    session = _sessions.get(_sid_key(sid))
    if not session:
        return False
    return secrets.compare_digest(session["csrf"], cookie_token)


# ───────────────────────────── login rate-limiting ───────────────────────────────────────
_login_attempts: dict[str, list[float]] = {}


def login_attempt_flood(ip: str) -> bool:
    """Return True if the IP is rate-limited. Resets on success."""
    now = time.time()
    window = config.LOGIN_RATE_WIN
    max_attempts = config.LOGIN_RATE_MAX
    _login_attempts.setdefault(ip, [])
    attempts = [t for t in _login_attempts[ip] if now - t < window]
    _login_attempts[ip] = attempts
    return len(attempts) >= max_attempts


def login_record_success(ip: str) -> None:
    _login_attempts.pop(ip, None)


def login_record_failure(ip: str) -> tuple[bool, float]:
    """Record failure. Returns (should_backoff, wait_seconds)."""
    now = time.time()
    _login_attempts.setdefault(ip, [])
    attempts = [t for t in _login_attempts[ip] if now - t < config.LOGIN_RATE_WIN]
    attempts.append(now)
    _login_attempts[ip] = attempts
    if len(attempts) >= config.LOGIN_RATE_MAX:
        backoff = config.LOGIN_BACKOFF * (2 ** (len(attempts) - config.LOGIN_RATE_MAX))
        return True, min(backoff, 600)  # cap at 10 min
    return False, 0


# ───────────────────────────── bootstrap admin ───────────────────────────────────────────


async def bootstrap_admin() -> str | None:
    """Create first admin from env. Idempotent."""
    name = config._str("WC_ADMIN_USER", "admin")
    pw = config._str("WC_ADMIN_PASSWORD")
    if not pw or not name:
        return None
    # Check if any user exists (import here avoids circular import at top level)
    from db import user_get_by_name as _ugb

    existing = await _ugb(name)
    if existing:
        return None
    if password_error(pw):
        return None
    from db import user_create as _uc

    await _uc(name, None, hash_password(pw))
    return name


# ───────────────────────────── API tokens ────────────────────────────────────────────────
#
# A credential for callers that cannot hold a cookie: scripts, cron, another
# machine on the tailnet. It carries the same identity and role a login would,
# and is presented in a header rather than a cookie -- which is what makes it
# safe to exempt from CSRF, since a browser never attaches it on its own.
#
# The shape is deliberately boring: a public id used for display and logs, and
# a high-entropy secret the server keeps only as a hash.

# Public prefix, so a token found in a log or a shell history is recognisable
# for what it is -- and greppable when it has to be revoked.
API_TOKEN_PREFIX: Final[str] = "wct_"
# Bytes of randomness in the secret half. 32 bytes is 256 bits; the point of
# the number is that guessing is not a threat model, so the hash below does not
# need to be slow.
_API_TOKEN_BYTES: Final[int] = 32
_API_TOKEN_ID_BYTES: Final[int] = 6


def hash_api_token(secret: str) -> str:
    """The at-rest form of a token: sha256 hex of the presented string.

    Not argon2, and the difference matters in both directions. A password is
    low-entropy and chosen by a human, so it needs a slow hash. This secret is
    256 random bits, so there is nothing to slow down -- and this runs on every
    authenticated request, where argon2 would cost ~50ms per call and turn the
    credential check into the slowest thing in the stack.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_api_token() -> tuple[str, str, str]:
    """Mint a token. Returns ``(token_id, secret, token_hash)``.

    The *secret* is the only thing the caller can authenticate with and the only
    thing not stored: show it once and let it go. Its own id is embedded so the
    server can name the credential in a log line without holding the secret --
    and so a caller with the secret can say which token it is using.
    """
    token_id = API_TOKEN_PREFIX + secrets.token_hex(_API_TOKEN_ID_BYTES)
    secret = f"{token_id}.{secrets.token_urlsafe(_API_TOKEN_BYTES)}"
    return token_id, secret, hash_api_token(secret)


def api_token_id_of(secret: str) -> str:
    """The id embedded in a presented secret, or "" if it is not one of ours.

    Used only for log lines and for rejecting obviously malformed input before a
    database lookup. It is *not* an authorisation decision: the id is the public
    half, so anyone can claim any id. The hash comparison is what decides.
    """
    if not isinstance(secret, str) or not secret.startswith(API_TOKEN_PREFIX):
        return ""
    head, _, tail = secret.partition(".")
    if not tail or not _VALID_TOKEN_ID.match(head):
        return ""
    return head


_VALID_TOKEN_ID = re.compile(rf"^{re.escape(API_TOKEN_PREFIX)}[0-9a-f]{{12}}$")


def bearer_from_headers(headers: Any) -> str:
    """Extract a presented token from a request's headers, or "".

    Two spellings are accepted because both are in common use and neither is
    ambiguous: ``Authorization: Bearer <token>`` and ``X-API-Token: <token>``.
    A cookie is deliberately not one of them -- the whole reason this credential
    can skip CSRF is that a browser will never send it unprompted, and reading
    it from a cookie would quietly destroy that property.
    """
    try:
        authorization = headers.get("authorization") or ""
        direct = headers.get("x-api-token") or ""
    except (AttributeError, TypeError):
        return ""
    if authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return direct.strip()
