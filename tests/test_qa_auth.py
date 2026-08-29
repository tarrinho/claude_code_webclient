"""QA coverage for the untested half of auth.py.

A name-level audit found six functions never referenced by any test file:
_sweep, csrf_generate, csrf_consume, login_attempt_flood, login_record_success
and login_record_failure. Between them they are the session-eviction policy and
the whole login rate-limit and backoff mechanism -- security controls whose
failure mode is silent, because a broken limiter still lets every legitimate
login through.

Scope note: the session-binding half of _csrf_valid is covered by
tests/test_skills.py::CsrfBindingTests (cweb3) and is not repeated here.

Covers:
* _sweep — expiry eviction, the SESSION_MAX cap, oldest-first eviction order.
* session_get — absolute expiry and idle timeout, and that it refreshes `last`.
* csrf_generate / csrf_consume — single use, unknown tokens, independence.
* login_attempt_flood — the threshold, the sliding window, per-IP isolation.
* login_record_failure — backoff onset, doubling, the 10-minute cap.
* login_record_success — clearing the counter.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import auth
import config


def _stored(sid):
    """The session record for *sid*, whatever the store keys it by.

    _sessions was re-keyed by a hash of the id, which is a good change: the raw
    id then exists only in the cookie. Reaching in by raw id broke; reaching in
    by the hash would just re-couple these tests to the next storage decision.
    """
    key = auth._sid_key(sid) if hasattr(auth, "_sid_key") else sid
    return auth._sessions[key]


def _reset_state():
    auth._sessions.clear()
    auth._csrf_store.clear()
    auth._login_attempts.clear()


class SweepTests(unittest.TestCase):
    """_sweep enforces both expiry and the hard session cap."""

    def setUp(self):
        _reset_state()

    tearDown = setUp

    def _session(self, sid: str, expiry: float, last: float | None = None):
        auth._sessions[sid] = {
            "user": "admin",
            "role": "admin",
            "csrf": "t",
            "expiry": expiry,
            "last": last if last is not None else time.time(),
        }

    def test_expired_sessions_evicted(self):
        now = time.time()
        self._session("live", now + 100)
        self._session("dead", now - 1)
        auth._sweep(now)
        self.assertIn("live", auth._sessions)
        self.assertNotIn("dead", auth._sessions)

    def test_expiry_is_inclusive(self):
        """expiry == now counts as expired, not as one last valid moment."""
        now = time.time()
        self._session("boundary", now)
        auth._sweep(now)
        self.assertNotIn("boundary", auth._sessions)

    def test_cap_evicts_down_to_session_max(self):
        now = time.time()
        with patch.object(config, "SESSION_MAX", 3):
            for index in range(6):
                self._session(f"s{index}", now + 100 + index)
            auth._sweep(now)
            self.assertEqual(len(auth._sessions), 3)

    def test_cap_evicts_the_soonest_to_expire_first(self):
        now = time.time()
        with patch.object(config, "SESSION_MAX", 2):
            self._session("oldest", now + 10)
            self._session("middle", now + 20)
            self._session("newest", now + 30)
            auth._sweep(now)
            self.assertNotIn("oldest", auth._sessions)
            self.assertIn("newest", auth._sessions)

    def test_under_the_cap_nothing_is_evicted(self):
        now = time.time()
        with patch.object(config, "SESSION_MAX", 10):
            self._session("a", now + 100)
            auth._sweep(now)
            self.assertEqual(len(auth._sessions), 1)

    def test_session_new_sweeps_expired_sessions(self):
        """A new login is what prunes the store; nothing else calls _sweep."""
        self._session("dead", time.time() - 1)
        auth.session_new("admin")
        self.assertNotIn("dead", auth._sessions)


class SessionExpiryTests(unittest.TestCase):
    """session_get enforces the absolute and idle deadlines."""

    def setUp(self):
        _reset_state()

    tearDown = setUp

    def test_none_and_unknown_ids_rejected(self):
        self.assertIsNone(auth.session_get(None))
        self.assertIsNone(auth.session_get(""))
        self.assertIsNone(auth.session_get("nope"))

    def test_live_session_returned(self):
        sid, _csrf = auth.session_new("admin")
        self.assertIsNotNone(auth.session_get(sid))

    def test_absolute_expiry_drops_the_session(self):
        sid, _csrf = auth.session_new("admin")
        _stored(sid)["expiry"] = time.time() - 1
        self.assertIsNone(auth.session_get(sid), "an expired session must be rejected")

    def test_idle_timeout_drops_the_session(self):
        sid, _csrf = auth.session_new("admin")
        _stored(sid)["last"] = time.time() - config.SESSION_IDLE_S - 1
        self.assertIsNone(auth.session_get(sid))

    def test_access_refreshes_the_idle_clock(self):
        """Otherwise an active user is logged out mid-session."""
        sid, _csrf = auth.session_new("admin")
        _stored(sid)["last"] = time.time() - config.SESSION_IDLE_S + 5
        self.assertIsNotNone(auth.session_get(sid))
        self.assertAlmostEqual(_stored(sid)["last"], time.time(), delta=2)


class CsrfStoreTests(unittest.TestCase):
    """csrf_generate / csrf_consume are a single-use token store.

    Note: app.py does not currently call either -- the live mechanism is the
    token minted by session_new. These are tested as they stand so that wiring
    them up later cannot silently change their contract.
    """

    def setUp(self):
        _reset_state()

    tearDown = setUp

    def test_generate_returns_matching_halves(self):
        cookie, header = auth.csrf_generate()
        self.assertEqual(cookie, header)
        self.assertTrue(cookie)

    def test_tokens_are_unique(self):
        tokens = {auth.csrf_generate()[0] for _ in range(20)}
        self.assertEqual(len(tokens), 20)

    def test_token_validates_once(self):
        cookie, _header = auth.csrf_generate()
        self.assertTrue(auth.csrf_consume(cookie))

    def test_token_cannot_be_replayed(self):
        """Single use is the entire point of consuming rather than checking."""
        cookie, _header = auth.csrf_generate()
        auth.csrf_consume(cookie)
        self.assertFalse(auth.csrf_consume(cookie))

    def test_unknown_token_rejected(self):
        self.assertFalse(auth.csrf_consume("never-issued"))

    def test_empty_token_rejected(self):
        self.assertFalse(auth.csrf_consume(""))

    def test_consuming_one_token_leaves_others_valid(self):
        first, _ = auth.csrf_generate()
        second, _ = auth.csrf_generate()
        auth.csrf_consume(first)
        self.assertTrue(auth.csrf_consume(second))


class LoginFloodTests(unittest.TestCase):
    """The rate limiter trips at LOGIN_RATE_MAX within LOGIN_RATE_WIN."""

    def setUp(self):
        _reset_state()

    tearDown = setUp

    def test_fresh_ip_is_not_limited(self):
        self.assertFalse(auth.login_attempt_flood("10.0.0.1"))

    def test_not_limited_below_the_threshold(self):
        with patch.object(config, "LOGIN_RATE_MAX", 3):
            for _ in range(2):
                auth.login_record_failure("10.0.0.1")
            self.assertFalse(auth.login_attempt_flood("10.0.0.1"))

    def test_limited_at_the_threshold(self):
        with patch.object(config, "LOGIN_RATE_MAX", 3):
            for _ in range(3):
                auth.login_record_failure("10.0.0.1")
            self.assertTrue(auth.login_attempt_flood("10.0.0.1"))

    def test_attempts_outside_the_window_are_forgotten(self):
        """The window slides, so an old burst must not lock an IP out forever."""
        ip = "10.0.0.1"
        with patch.object(config, "LOGIN_RATE_MAX", 3), patch.object(
            config, "LOGIN_RATE_WIN", 300
        ):
            stale = time.time() - 301
            auth._login_attempts[ip] = [stale, stale, stale]
            self.assertFalse(auth.login_attempt_flood(ip))
            self.assertEqual(auth._login_attempts[ip], [])

    def test_limiting_is_per_ip(self):
        with patch.object(config, "LOGIN_RATE_MAX", 2):
            for _ in range(2):
                auth.login_record_failure("10.0.0.1")
            self.assertTrue(auth.login_attempt_flood("10.0.0.1"))
            self.assertFalse(auth.login_attempt_flood("10.0.0.2"))

    def test_success_clears_the_counter(self):
        with patch.object(config, "LOGIN_RATE_MAX", 2):
            for _ in range(2):
                auth.login_record_failure("10.0.0.1")
            auth.login_record_success("10.0.0.1")
            self.assertFalse(auth.login_attempt_flood("10.0.0.1"))

    def test_success_for_an_unknown_ip_is_harmless(self):
        auth.login_record_success("10.0.0.9")  # must not raise


class LoginBackoffTests(unittest.TestCase):
    """login_record_failure returns a growing, capped delay past the threshold."""

    def setUp(self):
        _reset_state()

    tearDown = setUp

    def test_no_backoff_below_the_threshold(self):
        with patch.object(config, "LOGIN_RATE_MAX", 3):
            for _ in range(2):
                should_backoff, wait = auth.login_record_failure("10.0.0.1")
                self.assertFalse(should_backoff)
                self.assertEqual(wait, 0)

    def test_backoff_starts_at_the_threshold(self):
        with patch.object(config, "LOGIN_RATE_MAX", 3), patch.object(
            config, "LOGIN_BACKOFF", 30
        ):
            for _ in range(2):
                auth.login_record_failure("10.0.0.1")
            should_backoff, wait = auth.login_record_failure("10.0.0.1")
            self.assertTrue(should_backoff)
            self.assertEqual(wait, 30)

    def test_backoff_doubles_with_each_further_failure(self):
        with patch.object(config, "LOGIN_RATE_MAX", 2), patch.object(
            config, "LOGIN_BACKOFF", 10
        ):
            waits = [auth.login_record_failure("10.0.0.1")[1] for _ in range(4)]
            self.assertEqual(waits, [0, 10, 20, 40])

    def test_backoff_is_capped_at_ten_minutes(self):
        with patch.object(config, "LOGIN_RATE_MAX", 1), patch.object(
            config, "LOGIN_BACKOFF", 30
        ):
            waits = [auth.login_record_failure("10.0.0.1")[1] for _ in range(12)]
            self.assertTrue(all(wait <= 600 for wait in waits), waits)
            self.assertEqual(waits[-1], 600, "the cap should be reached and held")

    def test_backoff_counts_only_attempts_inside_the_window(self):
        ip = "10.0.0.1"
        with patch.object(config, "LOGIN_RATE_MAX", 2), patch.object(
            config, "LOGIN_BACKOFF", 10
        ), patch.object(config, "LOGIN_RATE_WIN", 300):
            stale = time.time() - 301
            auth._login_attempts[ip] = [stale, stale, stale]
            should_backoff, wait = auth.login_record_failure(ip)
            self.assertFalse(should_backoff, "stale attempts must not trigger backoff")
            self.assertEqual(wait, 0)

    def test_backoff_is_per_ip(self):
        with patch.object(config, "LOGIN_RATE_MAX", 1), patch.object(
            config, "LOGIN_BACKOFF", 10
        ):
            auth.login_record_failure("10.0.0.1")
            auth.login_record_failure("10.0.0.1")
            should_backoff, wait = auth.login_record_failure("10.0.0.2")
            self.assertTrue(should_backoff)
            self.assertEqual(wait, 10, "a second IP starts its own backoff curve")


if __name__ == "__main__":
    unittest.main()
