"""QA: the statistics payload is cached briefly, and keyed so it cannot leak.

The charts read stored aggregates over a window measured in days, and the page
re-requests on every range and bucket change -- four ranges against four
buckets, clicked through in a couple of seconds. Each request was a full pass
over usage_events (169,751 rows, 126 MB on this deployment), so the same scan
ran again for a payload that was already minutes stale by its own definition.

A short TTL is the whole mechanism. What these tests pin is not that it is
fast -- a timing assertion on a loaded host is a flake generator -- but the
three things that make a cache either correct or a bug:

* a hit inside the window does not query again,
* the window expires rather than pinning the page to a stale answer forever,
* and the key separates owners, ranges and buckets, because a cache that
  answers one owner's request with another's rows is a data leak rather than
  an optimisation, and one that confuses two ranges silently draws the wrong
  chart.

Deliberately not asserted: invalidation on write. There is none, by design --
a 30-second TTL converges on its own, and hooking usage inserts would put
cache bookkeeping on the turn hot path to save a wait nobody is having.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class SeriesCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-seriescache-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()

        from routes import misc as misc_routes

        self.misc = misc_routes
        # A cache shared across a process is shared across tests too, and a
        # stale entry from a neighbour would make any of these pass or fail
        # for the wrong reason.
        misc_routes._SERIES_CACHE.clear()

    async def asyncTearDown(self):
        self.misc._SERIES_CACHE.clear()
        await self.db.close()

    def _request(self, days="30", bucket="day"):
        return SimpleNamespace(
            query_params={"days": days, "bucket": bucket},
            state=SimpleNamespace(session="session-token"),
        )

    async def _call(self, days="30", bucket="day", owner="admin"):
        """The handler, with ownership and the one remaining side query stubbed
        so each test measures the bundle call and nothing else."""
        with patch.object(self.misc, "owner_of", AsyncMock(return_value=owner)), \
             patch.object(self.db, "usage_agent_names",
                          AsyncMock(return_value={})), \
             patch.object(self.db, "usage_series_bundle",
                          AsyncMock(return_value={
                              "series": [], "models": [], "agents": [],
                          })) as bundle:
            response = await self.misc.handle_usage_series_get(
                self._request(days, bucket))
        return response, bundle

    async def test_a_second_request_does_not_scan_again(self):
        _, first = await self._call()
        self.assertEqual(first.await_count, 1)
        _, second = await self._call()
        self.assertEqual(
            second.await_count, 0,
            "the repeat request went back to the database, so the cache is "
            "not being consulted",
        )

    async def test_the_cached_payload_is_the_same_answer(self):
        first, _ = await self._call()
        second, _ = await self._call()
        self.assertEqual(first.body, second.body)

    async def test_an_expired_entry_is_refetched(self):
        """Pinned by ageing the stored entry rather than by sleeping: the TTL
        is 30 seconds and a test must not take 30 seconds to prove it."""
        await self._call()
        key = self.misc._series_cache_key("admin", 30, "day")
        stored_at, payload = self.misc._SERIES_CACHE[key]
        self.misc._SERIES_CACHE[key] = (
            stored_at - self.misc._SERIES_CACHE_TTL_S - 1, payload)
        _, again = await self._call()
        self.assertEqual(
            again.await_count, 1,
            "an entry past its TTL was served anyway, so the page can be "
            "pinned to a stale answer",
        )

    async def test_a_different_bucket_is_a_different_entry(self):
        await self._call(bucket="day")
        _, other = await self._call(bucket="hour")
        self.assertEqual(
            other.await_count, 1,
            "the hourly request was answered from the daily payload, which "
            "draws the wrong chart",
        )

    async def test_a_different_range_is_a_different_entry(self):
        await self._call(days="30")
        _, other = await self._call(days="7")
        self.assertEqual(other.await_count, 1)

    async def test_another_owner_never_sees_the_cached_rows(self):
        """The one failure here that is a disclosure rather than a wrong
        picture, so it is asserted on the key and on the refetch."""
        await self._call(owner="owner-a")
        _, other = await self._call(owner="owner-b")
        self.assertEqual(
            other.await_count, 1,
            "a second owner was served the first owner's usage payload",
        )
        self.assertEqual(
            {key[1] for key in self.misc._SERIES_CACHE},
            {"owner-a", "owner-b"},
            "the cache key does not separate owners",
        )

    async def test_the_cache_stays_bounded(self):
        """A caller varying the query string must not grow this forever."""
        for n in range(self.misc._SERIES_CACHE_MAX + 12):
            await self._call(days=str((n % 3000) + 1))
        self.assertLessEqual(
            len(self.misc._SERIES_CACHE), self.misc._SERIES_CACHE_MAX)


if __name__ == "__main__":
    unittest.main()
