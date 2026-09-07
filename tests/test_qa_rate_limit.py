"""QA: the rate limiter must actually gate requests, not just run harmlessly.

This module was introduced as a fix (PR #1) and has two independent failure
modes, both silent under normal use:

  * A `BaseHTTPMiddleware`-based implementation consumed the request body
    stream before the handler could read it, turning every login POST (and any
    other POST with a body) into a 500 because `request.json()` raised
    `JSONDecodeError` on an empty body. A pure-ASGI implementation must inspect
    only the scope (path, client) and never touch the body.

  * The in-memory token-bucket map must auto-cleanup. Without it, the map could
    grow without bound if a single client hit many different endpoints. The
    default bucket was shared across all paths. Each (ip, path) must now get
    its own bucket, and stale entries must be pruned automatically by the
    background task.

This file tests:
  * _Bucket -- refill, consume, cap enforcement.
  * _Ratelimiter -- per-(ip, path) isolation, endpoint-specific limits.
  * RateLimitMiddleware -- skips health/assets, returns 429 via ASGI send,
    passes through requests correctly.
  * start_cleanup / stop_cleanup -- background task creates and prunes.
"""
from __future__ import annotations

import asyncio
import json
import time as _time_mod
import unittest
from typing import Any

import rate_limit


# ── Helpers ───────────────────────────────────────────────────────────────────


def _clear_buckets():
    """Clear the class-var _buckets dict so tests are isolated."""
    rate_limit._Ratelimiter._buckets.clear()


async def _echo_app(scope, receive, send):
    body_parts = []
    while True:
        msg = await receive()
        body_parts.append(msg.get("body", b""))
        if not msg.get("more_body", False):
            break
    body = b"".join(body_parts)
    headers = [(b"content-type", b"application/json")]
    response = json.dumps({
        "path": scope.get("path", "/"),
        "body_length": len(body),
        "method": scope.get("method", ""),
    }).encode()
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": response})


class BucketTests(unittest.TestCase):
    """_Bucket -- refill, consume, cap enforcement."""

    def test_consume_succeeds_when_tokens_available(self):
        bucket = rate_limit._Bucket(capacity=5, rate=0.1)
        self.assertTrue(bucket.consume())
        self.assertTrue(bucket.consume())

    def test_consume_fails_when_empty(self):
        bucket = rate_limit._Bucket(capacity=1, rate=0.001)
        self.assertTrue(bucket.consume())
        self.assertFalse(bucket.consume())

    def test_tokens_replenish_over_time(self):
        bucket = rate_limit._Bucket(capacity=2, rate=10.0)
        bucket.consume()
        bucket.consume()
        self.assertFalse(bucket.consume())
        _time_mod.sleep(0.25)
        self.assertTrue(bucket.consume())

    def test_tokens_cap_at_capacity(self):
        bucket = rate_limit._Bucket(capacity=3, rate=100.0)
        _time_mod.sleep(0.2)
        self.assertTrue(bucket.consume())
        self.assertTrue(bucket.consume())
        self.assertTrue(bucket.consume())
        self.assertFalse(bucket.consume())


class RateLimiterTests(unittest.TestCase):
    """_Ratelimiter -- per-(ip, path) isolation, endpoint limits."""

    def _fresh(self, **overrides):
        _clear_buckets()
        return rate_limit._Ratelimiter(
            capacity=overrides.get("capacity", 60),
            window_s=overrides.get("window_s", 60.0),
            endpoint_limits=overrides.get("endpoint_limits", {}),
        )

    def test_default_and_endpoint_limits_differ(self):
        limiter = self._fresh(capacity=5, endpoint_limits={"/api/chats/": (2, 1.0)})
        limiter.check("1.1.1.1", "/api/chats/")
        limiter.check("1.1.1.1", "/api/chats/")
        self.assertFalse(limiter.check("1.1.1.1", "/api/chats/"))
        self.assertTrue(limiter.check("1.1.1.1", "/other"))

    def test_different_ips_share_no_buckets(self):
        limiter = self._fresh(capacity=1)
        self.assertTrue(limiter.check("1.1.1.1", "/api/chats/"))
        self.assertFalse(limiter.check("1.1.1.1", "/api/chats/"))
        self.assertTrue(limiter.check("2.2.2.2", "/api/chats/"))

    def test_different_paths_share_no_buckets(self):
        limiter = self._fresh(capacity=1)
        self.assertTrue(limiter.check("1.1.1.1", "/api/chats/"))
        self.assertFalse(limiter.check("1.1.1.1", "/api/chats/"))
        self.assertTrue(limiter.check("1.1.1.1", "/api/settings"))

    def test_cleanup_removes_stale_buckets(self):
        limiter = self._fresh()
        limiter.check("1.1.1.1", "/a")
        limiter.check("1.1.1.1", "/b")
        self.assertEqual(len(limiter._buckets), 2)
        first_key = list(limiter._buckets.keys())[0]
        limiter._buckets[first_key].last_refill = _time_mod.monotonic() - 180
        limiter.cleanup()
        self.assertEqual(len(limiter._buckets), 1)

    def test_cleanup_removes_all_stale(self):
        limiter = self._fresh()
        limiter.check("1.1.1.1", "/a")
        for k in limiter._buckets:
            limiter._buckets[k].last_refill = _time_mod.monotonic() - 180
        limiter.cleanup()
        self.assertEqual(len(limiter._buckets), 0)


class MiddlewareTests(unittest.TestCase):
    """RateLimitMiddleware -- ASGI passthrough, 429, skip rules."""

    def _single_call(self, scope, body=b""):
        """Call the middleware once. Must have been set up before."""
        responses = []

        async def _run():
            app = rate_limit.RateLimitMiddleware(_echo_app)
            async def receive():
                return {"type": "http.request", "body": body}
            async def send(msg):
                responses.append(msg)
            await app(scope, receive, send)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
            return responses
        finally:
            loop.close()

    def _setup(self, rate_limit_cap=3):
        _clear_buckets()
        rate_limit._limiter = rate_limit._Ratelimiter(
            capacity=rate_limit_cap, window_s=60.0, endpoint_limits={}
        )

    def test_health_paths_are_skipped(self):
        self._setup()
        scope = {
            "type": "http", "method": "GET", "path": "/api/version",
            "headers": [], "client": ("1.1.1.1", 80),
        }
        resps = self._single_call(scope)
        start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
        self.assertEqual(start_msg["status"], 200)

    def test_asset_paths_are_skipped(self):
        self._setup()
        scope = {
            "type": "http", "method": "GET", "path": "/assets/style.css",
            "headers": [], "client": ("1.1.1.1", 80),
        }
        resps = self._single_call(scope)
        start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
        self.assertEqual(start_msg["status"], 200)

    def test_exhausted_limit_returns_429(self):
        self._setup(rate_limit_cap=3)
        headers = [(b"cookie", b"wc_session=existing-session")]
        scope = {
            "type": "http", "method": "GET", "path": "/api/chats/",
            "headers": headers, "client": ("1.1.1.1", 80),
        }
        for i in range(3):
            resps = self._single_call(scope)
            start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
            self.assertEqual(start_msg["status"], 200, f"request {i+1} must be 200")
        resps = self._single_call(scope)
        start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
        self.assertEqual(start_msg["status"], 429)

    def test_429_includes_retry_after(self):
        self._setup(rate_limit_cap=3)
        headers = [(b"cookie", b"wc_session=existing-session")]
        scope = {
            "type": "http", "method": "GET", "path": "/api/chats/",
            "headers": headers, "client": ("1.1.1.1", 80),
        }
        for _ in range(3):
            self._single_call(scope)
        resps = self._single_call(scope)
        start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
        self.assertEqual(start_msg["status"], 429)
        header_names = [h[0] for h in start_msg["headers"]]
        self.assertIn(b"retry-after", header_names)

    def test_unauthenticated_skipped(self):
        self._setup()
        scope = {
            "type": "http", "method": "GET", "path": "/api/chats/",
            "headers": [], "client": ("1.1.1.1", 80),
        }
        resps = self._single_call(scope)
        start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
        self.assertEqual(start_msg["status"], 200)

    def test_public_login_not_rate_limited(self):
        self._setup()
        headers = [(b"content-type", b"application/json")]
        scope = {
            "type": "http", "method": "POST", "path": "/login",
            "headers": headers, "client": ("1.1.1.1", 80),
        }
        body = b'{"username":"test","password":"test"}'
        resps = self._single_call(scope, body=body)
        start_msg = [m for m in resps if m["type"] == "http.response.start"][0]
        self.assertEqual(start_msg["status"], 200)
        body_msg = [m for m in resps if m["type"] == "http.response.body"][0]
        data = json.loads(body_msg["body"])
        self.assertEqual(data["body_length"], len(body))


class CleanupTaskTests(unittest.IsolatedAsyncioTestCase):
    """start_cleanup / stop_cleanup -- background task prunes stale buckets."""

    async def asyncSetUp(self):
        _clear_buckets()

    async def test_start_cleanup_creates_a_running_task(self):
        rate_limit.start_cleanup(interval_s=300.0)
        self.assertIsNotNone(rate_limit._cleanup_task)
        self.assertFalse(rate_limit._cleanup_task.done())

    async def test_stop_cleanup_cleans_up(self):
        rate_limit.start_cleanup(interval_s=300.0)
        self.assertIsNotNone(rate_limit._cleanup_task)
        await rate_limit.stop_cleanup()
        self.assertIsNone(rate_limit._cleanup_task)

    async def test_task_prunes_when_buckets_expire(self):
        rate_limit.start_cleanup(interval_s=0.5)
        limiter = rate_limit._limiter
        limiter.check("1.1.1.1", "/test")
        await asyncio.sleep(0.2)
        for v in limiter._buckets.values():
            v.last_refill = _time_mod.monotonic() - 300
        self.assertGreater(len(limiter._buckets), 0)
        await asyncio.sleep(0.7)
        self.assertEqual(len(limiter._buckets), 0)


if __name__ == "__main__":
    unittest.main()
