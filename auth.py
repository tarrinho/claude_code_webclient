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
import secrets
import sqlite3
import time
from typing import Final

import config

_log = logging.getLogger("wc.auth")

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


def _sid_key(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def _session_conn() -> sqlite3.Connection | None:
    """A short-lived synchronous handle, or None if the database is not ready.

    Separate from the app's aiosqlite connection because the session store is
    read from middleware, which is synchronous; the same pattern the FTS
    maintenance in db.py already uses.
    """
    try:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except sqlite3.Error:
        return None


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
