"""Simple in-memory rate limiter (token-bucket) for the WebConsole.

Placed before Auth/CSRF so it can short-circuit before credential work.
Uses a dict keyed by (ip, path); entries expire after 2× window so the map
never grows unbounded.

Default: 60 requests per minute for any authenticated endpoint.
Chat prompt submissions get a tighter 10 per minute to curb flooding.

IMPORTANT: This is pure ASGI middleware (not BaseHTTPMiddleware). BaseHTTPMiddleware
consumes the request body before the handler can read it, which turns every
POST with a JSON body (login, chat prompts) into a 500 with JSONDecodeError.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, ClassVar, Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

import config

_log = logging.getLogger("wc.rate_limit")

# ── Token bucket ────────────────────────────────────────────────────────────


class _Bucket:
    """Per-endpoint token bucket.

    Tokens refill at a fixed rate; each request consumes one.
    """

    __slots__ = ("capacity", "rate", "tokens", "last_refill")

    def __init__(self, capacity: int, rate: float) -> None:
        self.capacity = capacity
        self.rate = rate
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self) -> bool:
        """Return True if a token was consumed; False if bucket is empty."""
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class _Ratelimiter:
    """Map of (ip, endpoint) → _Bucket."""

    _buckets: ClassVar[dict[tuple[str, str], _Bucket]] = {}

    def __init__(
        self,
        capacity: int = 60,
        window_s: float = 60.0,
        endpoint_limits: dict[str, tuple[int, float]] | None = None,
    ) -> None:
        self.default_capacity = capacity
        self.default_window = window_s
        self.default_rate = capacity / window_s
        self.endpoint_limits = endpoint_limits or {}

    def _key(self, ip: str, path: str) -> tuple[str, str]:
        return (ip, path)

    def check(self, ip: str, path: str) -> bool:
        """Return True if the request is allowed."""
        key = self._key(ip, path)
        cap, rate = self.endpoint_limits.get(
            path, (self.default_capacity, self.default_rate)
        )
        if key not in self._buckets:
            self._buckets[key] = _Bucket(cap, rate)
        return self._buckets[key].consume()

    def cleanup(self) -> None:
        """Remove stale buckets (no refill in 2× window)."""
        now = time.monotonic()
        cutoff = now - self.default_window * 2
        stale = [k for k, v in self._buckets.items() if v.last_refill < cutoff]
        for k in stale:
            del self._buckets[k]


# Global limiter instance.
_limiter = _Ratelimiter(
    capacity=config.RATE_LIMIT_MAX,
    window_s=config.RATE_LIMIT_WINDOW,
    endpoint_limits=config.RATE_LIMIT_ENDPOINTS,
)


class RateLimitMiddleware:
    """Pure ASGI rate-limit middleware.

    Uses __call__(scope, receive, send) instead of BaseHTTPMiddleware so the
    request body is never consumed before the handler sees it.

    Skips: unauthenticated /api/* endpoints that require a session (these
    already fail fast with 401), health-check endpoints, and static assets.
    Authenticated requests get rate-limited.
    """

    _HEALTH: ClassVar[set[str]] = {"/api/version", "/api/health"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "/")

        # Skip health and asset routes.
        if path in self._HEALTH or path.startswith("/assets/"):
            return await self.app(scope, receive, send)

        # Extract session from query param or cookie in the ASGI scope.
        session = None
        headers = scope.get("headers", [])
        for name, value in headers:
            if isinstance(name, bytes):
                name = name.decode()
            if name.lower() == "cookie":
                for cookie in value.decode().split(";"):
                    cookie = cookie.strip()
                    if cookie.startswith("wc_session="):
                        session = cookie.split("=", 1)[1]
                        break

        is_public_login = scope.get("method", "GET") == "POST" and path == "/login"

        if not session and not is_public_login:
            return await self.app(scope, receive, send)

        ip = "?"
        client = scope.get("client")
        if client:
            ip = client[0] if isinstance(client, tuple) else "?"

        allowed = _limiter.check(ip, path)
        if not allowed:
            _log.warning(
                "rate_limited ip=%s path=%s",
                ip, path,
            )
            body = b'{"error":"Too many requests. Try again later."}'
            headers = [
                (b"content-type", b"application/json"),
                (b"retry-after", str(int(config.RATE_LIMIT_WINDOW)).encode()),
            ]
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": headers,
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
            return

        return await self.app(scope, receive, send)


# Periodic cleanup task (called from lifespan)
_cleanup_task: ClassVar[Any] = None


def start_cleanup(interval_s: float = 300.0) -> None:
    """Start a background task that prunes stale buckets every `interval_s` seconds."""

    async def _run() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                _limiter.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("rate_limit cleanup failed")

    global _cleanup_task
    _cleanup_task = asyncio.create_task(_run())


async def stop_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
