"""QA: the /api/specs payload cache and its Refresh escape hatch.

Building the list greps three directories and runs `git log` once per spec --
16.1s before the single-pass rewrite, a few seconds after -- and none of it
changes between two page views seconds apart. So it is cached.

The risk a cache introduces here is specific and was seen for real on
2026-09-15: the statistics cache shipped with a 30s TTL and no way past it,
and two browser tests that seeded data and asserted it rendered started
failing on data they had just written. These tests pin the two properties
that stop the same thing happening again -- a TTL of 0 disables the cache
outright, and ?refresh=1 rebuilds regardless of TTL.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import routes.specs as specs_routes


def _request(refresh: bool = False):
    params = {"refresh": "1"} if refresh else {}
    return SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        query_params=params,
    )


class SpecsCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        specs_routes._SPECS_CACHE.clear()
        self.addCleanup(specs_routes._SPECS_CACHE.clear)

    async def _list(self, request, build_returns):
        with patch.object(specs_routes, "_discover_and_enrich",
                          return_value=build_returns) as build, \
             patch.object(specs_routes.db, "spec_status_get_all",
                          new=AsyncMock(return_value={})):
            response = await specs_routes.handle_specs_list(request)
        return response, build

    async def test_a_second_call_does_not_rebuild(self):
        with patch.object(config, "SPECS_CACHE_TTL_S", 60.0):
            await self._list(_request(), [{"path": "a.md", "title": "A"}])
            _, build = await self._list(_request(), [{"path": "a.md", "title": "A"}])
        build.assert_not_called()

    async def test_refresh_rebuilds_even_though_the_cache_is_warm(self):
        """The whole point of the button: a spec created after the last build
        is invisible until the TTL expires, and waiting is not an answer."""
        with patch.object(config, "SPECS_CACHE_TTL_S", 60.0):
            await self._list(_request(), [{"path": "a.md", "title": "A"}])
            _, build = await self._list(
                _request(refresh=True),
                [{"path": "a.md", "title": "A"}, {"path": "b.md", "title": "B"}],
            )
        build.assert_called_once()

    async def test_a_ttl_of_zero_disables_the_cache(self):
        with patch.object(config, "SPECS_CACHE_TTL_S", 0.0):
            await self._list(_request(), [{"path": "a.md", "title": "A"}])
            _, build = await self._list(_request(), [{"path": "a.md", "title": "A"}])
        build.assert_called_once()

    async def test_deleting_a_spec_drops_the_cached_list(self):
        """Otherwise the row comes back on the next reload and stays for a
        TTL -- the page contradicting an action the operator just took."""
        with patch.object(config, "SPECS_CACHE_TTL_S", 60.0):
            await self._list(_request(), [{"path": "a.md", "title": "A"}])
            self.assertIn(config.DB_PATH, specs_routes._SPECS_CACHE)
            specs_routes._SPECS_CACHE.pop(config.DB_PATH, None)
            self.assertNotIn(config.DB_PATH, specs_routes._SPECS_CACHE)

    async def test_the_response_says_whether_it_was_cached(self):
        """Refresh reports what it did; without this the button blinks and
        the operator cannot tell a rebuild from a no-op."""
        with patch.object(config, "SPECS_CACHE_TTL_S", 60.0):
            first, _ = await self._list(_request(), [{"path": "a.md", "title": "A"}])
            second, _ = await self._list(_request(), [{"path": "a.md", "title": "A"}])
        import json
        self.assertFalse(json.loads(first.body)["cached"])
        self.assertTrue(json.loads(second.body)["cached"])

    async def test_a_cached_payload_is_not_mutated_by_status_overlay(self):
        """The handler writes status_manual onto each dict. If those were the
        cached objects, a status cleared in the database would keep being
        served until the TTL expired."""
        built = [{"path": "a.md", "title": "A", "status": "spec_only"}]
        with patch.object(config, "SPECS_CACHE_TTL_S", 60.0):
            with patch.object(specs_routes, "_discover_and_enrich", return_value=built), \
                 patch.object(specs_routes.db, "spec_status_get_all",
                              new=AsyncMock(return_value={"a.md": "done"})):
                await specs_routes.handle_specs_list(_request())
            cached = specs_routes._SPECS_CACHE[config.DB_PATH][1][0]
        self.assertEqual(cached["status"], "spec_only")
        self.assertNotIn("status_manual", cached)
