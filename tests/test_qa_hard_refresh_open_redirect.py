"""QA: /api/hard-refresh cannot be used as an open redirect.

Found in the code review of 2026-09-16. The endpoint is exempt from auth
(middleware.py) and did `RedirectResponse(url=request.query_params.get("to"))`
with no validation, so an unauthenticated request could make the trusted
console host redirect anywhere:

    https://<console>/api/hard-refresh?to=https://evil.example

That is a phishing primitive needing no account -- the victim sees the real
console hostname in the link. The only in-repo caller passes
location.pathname, so restricting this to rooted same-origin paths costs
nothing real.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from routes import misc


def _request(to=None):
    return SimpleNamespace(
        query_params={} if to is None else {"to": to},
        client=SimpleNamespace(host="203.0.113.9"),
    )


class HardRefreshOpenRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def _location(self, to=None):
        response = await misc._api_hard_refresh(_request(to))
        return response.headers["location"]

    async def test_a_rooted_path_is_kept(self):
        self.assertEqual(await self._location("/settings"), "/settings")

    async def test_the_default_is_root(self):
        self.assertEqual(await self._location(), "/")

    async def test_an_absolute_url_is_refused(self):
        self.assertEqual(await self._location("https://evil.example"), "/")

    async def test_a_protocol_relative_url_is_refused(self):
        """//host resolves off-origin while looking like a path."""
        self.assertEqual(await self._location("//evil.example"), "/")

    async def test_a_backslash_protocol_relative_url_is_refused(self):
        """Some browsers normalise /\\host to //host."""
        self.assertEqual(await self._location("/\\evil.example"), "/")

    async def test_a_bare_hostname_is_refused(self):
        self.assertEqual(await self._location("evil.example"), "/")

    async def test_the_no_store_headers_survive(self):
        """The point of the endpoint. A fix that silently dropped these would
        leave it redirecting without busting any cache."""
        response = await misc._api_hard_refresh(_request("/settings"))
        self.assertIn("no-store", response.headers["cache-control"])
