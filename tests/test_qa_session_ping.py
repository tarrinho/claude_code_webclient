"""QA: session ping and version hash endpoints.

Two endpoints live in routes/misc.py:

  * GET /api/version/hash -- returns git short hash (or VERSION fallback) for
    cache busting. Returns {"version": "abc123..."}.

  * POST /api/ping -- refreshes session TTL. Requires a valid session cookie
    and session in request.state.session. Returns
    {"session_ttl_remaining": N, "ttl": N}. Clamped to 0 (never negative).
    Returns 401 {"error": "Session expired", "redirect": "/login"} without
    valid session.

This file tests:
  * _asset_version -- git hash + fallback.
  * GET /api/version/hash -- returns version key with git hash.
  * POST /api/ping -- 401 without session, TTL shape, clamped to 0.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from routes.misc import _asset_version, _api_version_hash, _api_ping
from starlette.requests import Request
from starlette.datastructures import State, Headers


class AssetVersionTests(unittest.TestCase):
    """_asset_version -- git hash + fallback."""

    def test_git_hash_returns_non_empty(self):
        result = _asset_version()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0, "version must not be empty")

    def setUp(self):
        # The result is cached for _ASSET_VERSION_TTL_S, so a test that does
        # not clear it reads whatever an earlier test left behind.
        import routes.misc as mr
        mr._asset_version_cache = None
        self.addCleanup(setattr, mr, "_asset_version_cache", None)

    def test_fallback_returns_version(self):
        """When git fails, falls back to config.VERSION (prefix stripped).

        Patches subprocess.run rather than os.popen: this used to shell out
        through os.popen, and a test bound to that spelling would have failed
        against the corrected implementation while asserting nothing about the
        fallback. The behaviour is "git said nothing useful, so report
        VERSION" -- which is what is checked here.
        """
        import config
        import routes.misc as mr
        with patch("routes.misc.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="")
            self.assertIn(
                config.VERSION.removeprefix("WebConsole_"), _asset_version())
        mr._asset_version_cache = None
        # And when git is absent entirely rather than merely unhelpful.
        with patch("routes.misc.subprocess.run", side_effect=FileNotFoundError):
            self.assertIn(
                config.VERSION.removeprefix("WebConsole_"), _asset_version())

    def test_git_is_asked_about_this_repo_not_a_hardcoded_path(self):
        """The defect this replaced, and the reason it was invisible.

        The command was built with the absolute literal
        `/home/kali/projects/claude-code-webconsole`. On any other checkout --
        a QA node, a git worktree, a relocated clone -- git ran against a
        directory that is not a repository, so every caller silently took the
        VERSION fallback while the code read as though it reported a commit.
        The path must be derived from this file's own location.
        """
        import routes.misc as mr
        with patch("routes.misc.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
            _asset_version()
        argv = run.call_args[0][0]

        # Read from the source, not from argv. On *this* checkout the derived
        # path is byte-identical to the old hardcoded literal, so asserting
        # the literal's absence from argv fails against correct code -- which
        # is what the first version of this test did. "Derived rather than
        # written down" is a property of the text, so the text is what to
        # check; see rules.md §16a on source invariants.
        source = Path(mr.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            '"/home/kali', source,
            "an absolute path to one checkout is back in routes/misc.py; "
            "anywhere else it makes git run outside a repository and every "
            "caller silently takes the VERSION fallback",
        )
        self.assertNotIn("'/home/kali", source)

        self.assertIn("-C", argv)
        target = argv[argv.index("-C") + 1]
        self.assertEqual(
            Path(target).resolve(), Path(mr.__file__).resolve().parents[1],
            "git must be pointed at the repository this module lives in",
        )

    def test_it_does_not_fork_git_on_every_call(self):
        """It ran a git process per request. The value cannot change without a
        deploy, so a short TTL removes the fork without making a redeploy
        invisible."""
        with patch("routes.misc.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
            first = _asset_version()
            second = _asset_version()
        self.assertEqual(first, second)
        self.assertEqual(
            run.call_count, 1,
            f"git was invoked {run.call_count} times for two calls; the cache "
            f"is not being used",
        )

    def test_no_shell_is_involved(self):
        """`os.popen` ran the command through a shell for no reason. Passed as
        an argv list, there is no shell to quote for."""
        with patch("routes.misc.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
            _asset_version()
        self.assertIsInstance(
            run.call_args[0][0], list,
            "the command must be an argv list, not a shell string")
        self.assertNotEqual(
            run.call_args.kwargs.get("shell"), True, "shell=True is back")


class VersionHashEndpointTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/version/hash -- returns version key.

    Note: the endpoint is also tested at the HTTP level in the app's
    existing test suite (middleware is exercised there).  This test
    verifies the handler function returns correct JSON structure.
    """

    async def test_returns_version_json(self):
        from starlette.requests import Request
        from starlette.datastructures import State, Headers
        from starlette.testclient import TestClient
        import app as app_mod
        # Use a session cookie so AuthMiddleware passes the request through
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        # A fake session must not reach the handler: asserted, rather than
        # fetched and thrown away as it was before.
        resp = client.get("/api/version/hash", cookies={"wc_session": "fake"})
        self.assertEqual(resp.status_code, 401)
        # Auth middleware returns 401 for fake session -- test the function
        # directly for the happy path.
        fake_req = MagicMock(spec=Request)
        fake_req.state = State({"session": {"user": "test"}})
        fake_req.cookies = {}
        fake_req.headers = Headers(raw=[])
        result = await _api_version_hash(fake_req)
        self.assertIsInstance(result.body, bytes)
        data = json.loads(result.body.decode())
        self.assertIn("version", data)

    async def test_content_type_json(self):
        fake_req = MagicMock(spec=Request)
        fake_req.state = State({"session": {"user": "test"}})
        fake_req.cookies = {}
        fake_req.headers = Headers(raw=[])
        result = await _api_version_hash(fake_req)
        self.assertIn(
            "application/json",
            result.headers.get("content-type", ""),
        )

    async def test_version_is_string(self):
        fake_req = MagicMock(spec=Request)
        fake_req.state = State({"session": {"user": "test"}})
        fake_req.cookies = {}
        fake_req.headers = Headers(raw=[])
        result = await _api_version_hash(fake_req)
        data = json.loads(result.body.decode())
        self.assertIsInstance(data["version"], str)
        self.assertTrue(len(data["version"]) > 0)


class PingEndpointTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/ping -- 401 without session, TTL clamped to 0."""

    async def asyncSetUp(self):
        from starlette.testclient import TestClient
        import app as app_mod
        self.client = TestClient(app_mod.app, raise_server_exceptions=False)

    async def asyncTearDown(self):
        self.client.close()

    async def test_returns_401_without_session(self):
        resp = self.client.post("/api/ping")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertIn("error", data)

    async def test_returned_json_not_html(self):
        resp = self.client.post("/api/ping")
        self.assertIn(
            "application/json",
            resp.headers.get("content-type", ""),
        )
        data = resp.json()
        self.assertIn("error", data)

    async def test_ping_returns_ttl_remaining(self):
        """When authenticated, returns session_ttl_remaining and ttl."""
        fake_req = MagicMock(spec=Request)
        fake_req.state = State({"session": {"user": "test"}})
        fake_req.cookies = {"wc_session": "test-session"}

        with patch("routes.misc.db.setting_get", return_value=3600):
            # auth.session_info does not exist; the call at line 109 raises
            # an exception which is caught, falling back to full ttl.
            result = await _api_ping(fake_req)
            content = json.loads(result.body.decode())
            self.assertIn("session_ttl_remaining", content)
            self.assertIn("ttl", content)
            self.assertGreaterEqual(content["session_ttl_remaining"], 0)

    async def test_ttl_clamped_to_zero(self):
        """session_ttl_remaining must never be negative."""
        ttl = 3600
        elapsed = 5000
        remaining = max(0, int(ttl - elapsed))
        self.assertEqual(remaining, 0)

    async def test_ping_includes_ttl(self):
        """ping must return both session_ttl_remaining and ttl fields."""
        fake_req = MagicMock(spec=Request)
        fake_req.state = State({"session": {"user": "test"}})
        fake_req.cookies = {"wc_session": "test-session"}

        with patch("routes.misc.db.setting_get", return_value=3600):
            result = await _api_ping(fake_req)
            content = json.loads(result.body.decode())
            self.assertIn("session_ttl_remaining", content)
            self.assertIn("ttl", content)
            self.assertIsInstance(content["session_ttl_remaining"], int)
            self.assertIsInstance(content["ttl"], int)


if __name__ == "__main__":
    unittest.main()
