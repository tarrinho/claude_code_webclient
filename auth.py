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
import time

import config

_log = logging.getLogger("wc.auth")

_VALID_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_N, _R, _P = 1 << 14, 8, 1
_DKLEN = 32

# ───────────────────────────── password hashing ──────────────────────────────────────────

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, VerificationError

    _PH = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=1)

    def hash_password(pw: str) -> str:
        return _PH.hash(pw)

    def verify_password(pw: str, stored: str) -> bool:
        try:
            _PH.verify(stored, pw)
            try:
                _PH.hash_check_needed(stored)
            except Exception:
                pass  # rehash not needed
            return True
        except (VerifyMismatchError, VerificationError):
            return False
        except Exception:
            return False

except ImportError:
    # Fallback to scrypt if argon2-cffi not installed.
    def hash_password(pw: str) -> str:
        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
        b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
        return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"

    def verify_password(pw: str, stored: str) -> bool:
        try:
            scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
            if scheme != "scrypt":
                return False
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            dk = hashlib.scrypt(pw.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
            return hmac.compare_digest(dk, expected)
        except Exception:
            return False


def password_error(pw: str) -> str | None:
    if not pw or len(pw) < 8:
        return "password must be at least 8 characters"
    if len(pw) > 256:
        return "password too long"
    return None


# ───────────────────────────── session store ─────────────────────────────────────────────
_sessions: dict[str, dict] = {}


def _sweep(now: float) -> None:
    for sid in [s for s, v in _sessions.items() if v["expiry"] <= now]:
        _sessions.pop(sid, None)
    over = len(_sessions) - config.SESSION_MAX
    if over > 0:
        for sid, _v in sorted(_sessions.items(), key=lambda kv: kv[1]["expiry"])[:over]:
            _sessions.pop(sid, None)


def session_new(user: str) -> tuple[str, str]:
    """Create a session; returns (session_id, csrf_token)."""
    now = time.time()
    _sweep(now)
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    _sessions[sid] = {
        "user": user,
        "role": "admin",
        "expiry": now + config.SESSION_TTL_S,
        "last": now,
        "csrf": csrf,
    }
    return sid, csrf


def session_get(sid: str | None) -> dict | None:
    if not sid:
        return None
    s = _sessions.get(sid)
    if not s:
        return None
    now = time.time()
    if s["expiry"] <= now or (now - s["last"]) > config.SESSION_IDLE_S:
        _sessions.pop(sid, None)
        return None
    s["last"] = now
    return s


def session_drop(sid: str | None) -> None:
    if sid:
        _sessions.pop(sid, None)


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