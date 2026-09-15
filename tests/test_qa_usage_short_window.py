"""QA: a one-hour window on the Statistics page, and the buckets it needs.

The page's shortest range was a day and its finest bucket half an hour, so
"what has this fleet been doing in the last hour" was two points. This adds an
hours-based window and the minute and five-minute buckets to draw it at.

Three things are worth stating about the design, because each is a place a
reasonable implementation would go wrong:

* **The window stays one parameter.** ``hours`` converts to a fractional
  ``days`` and everything downstream is untouched: ``_cutoff`` and
  ``bucket_spine`` already do float arithmetic on days. Two independent window
  parameters could disagree, and the one that lost would do so silently.

* **Fine buckets round, they do not truncate.** A prefix of the local
  timestamp gives minute resolution and nothing between minute and hour, so
  the five-minute and half-hour keys keep both minute digits and round the
  value down to the slot. Truncating to 15 characters would produce
  "2026-09-15T14:3", which is a ten-minute slot wearing a five-minute label
  and sorts incorrectly against "2026-09-15T14:30".

* **A bucket too fine for its window is coarsened, not served.** The range and
  bucket pickers are independent, so thirty days by the minute is reachable in
  two clicks: 43,200 slots, an axis that exceeds its own cap and returns
  empty, and a payload of points no screen can show.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class BucketKeyTests(unittest.TestCase):
    """The SQL key and the axis key must agree, or every point lands off-axis.

    Asserted together on purpose: the spine is built in Python and the bucket
    keys come out of SQLite, and the chart matches them as strings. A mismatch
    does not raise -- it silently draws an empty chart with a full axis.
    """

    def setUp(self):
        from routes import db_usage

        self.u = db_usage

    def test_the_five_minute_key_rounds_down_to_its_slot(self):
        for minute, expected in ((0, "00"), (4, "00"), (5, "05"),
                                 (29, "25"), (33, "30"), (59, "55")):
            with self.subTest(minute=minute):
                epoch = time.mktime((2026, 9, 15, 14, minute, 0, 0, 0, -1))
                self.assertEqual(
                    self.u._bucket_key(epoch, "fivemin"),
                    f"2026-09-15T14:{expected}",
                )

    def test_the_minute_key_keeps_the_minute(self):
        epoch = time.mktime((2026, 9, 15, 14, 37, 0, 0, 0, -1))
        self.assertEqual(
            self.u._bucket_key(epoch, "minute"), "2026-09-15T14:37")

    def test_the_half_hour_key_did_not_change(self):
        """The rounding was rewritten to serve both widths; the existing
        behaviour has to come out unchanged."""
        for minute, expected in ((0, "00"), (29, "00"), (30, "30"), (59, "30")):
            with self.subTest(minute=minute):
                epoch = time.mktime((2026, 9, 15, 14, minute, 0, 0, 0, -1))
                self.assertEqual(
                    self.u._bucket_key(epoch, "halfhour"),
                    f"2026-09-15T14:{expected}",
                )

    def test_a_fine_key_sorts_as_a_string(self):
        """The chart compares keys as strings, so 5 past must sort before 50
        past -- which is why the key keeps two digits rather than one."""
        keys = [
            self.u._bucket_key(
                time.mktime((2026, 9, 15, 14, m, 0, 0, 0, -1)), "fivemin")
            for m in (5, 50, 15)
        ]
        self.assertEqual(sorted(keys), sorted(keys, key=str))
        self.assertLess(keys[0], keys[2])
        self.assertLess(keys[2], keys[1])

    def test_an_hour_of_five_minute_slots_is_twelve_points(self):
        spine = self.u.bucket_spine("fivemin", 1 / 24)
        self.assertGreaterEqual(len(spine), 12)
        self.assertLessEqual(len(spine), 14)   # inclusive ends
        self.assertEqual(sorted(set(spine)), sorted(spine), "duplicate slots")

    def test_an_hour_of_minute_slots_is_sixty_points(self):
        spine = self.u.bucket_spine("minute", 1 / 24)
        self.assertGreaterEqual(len(spine), 60)
        self.assertLessEqual(len(spine), 62)


class BucketClampTests(unittest.TestCase):
    def setUp(self):
        from routes import db_usage

        self.u = db_usage

    def test_a_short_window_keeps_the_bucket_it_asked_for(self):
        self.assertEqual(self.u.clamp_bucket("minute", 1 / 24), "minute")
        self.assertEqual(self.u.clamp_bucket("fivemin", 1 / 24), "fivemin")

    def test_a_month_by_the_minute_is_coarsened(self):
        chosen = self.u.clamp_bucket("minute", 30)
        self.assertNotEqual(chosen, "minute")
        spine = self.u.bucket_spine(chosen, 30)
        self.assertTrue(
            spine, f"{chosen} still overruns its own axis cap over 30 days")

    def test_coarsening_never_goes_finer_than_asked(self):
        order = list(self.u._USAGE_BUCKETS)
        for bucket in ("minute", "fivemin", "halfhour", "hour"):
            with self.subTest(bucket=bucket):
                chosen = self.u.clamp_bucket(bucket, 30)
                self.assertGreaterEqual(
                    order.index(chosen), order.index(bucket),
                    "clamping produced a finer bucket than was requested",
                )

    def test_an_unbounded_window_is_left_alone(self):
        """days=None is "all time", whose length is not known here; the spine
        answers it from the earliest row instead."""
        self.assertEqual(self.u.clamp_bucket("minute", None), "minute")


class HourWindowEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-shortwindow-")) / "wc.db"
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
        misc_routes._SERIES_CACHE.clear()

    async def asyncTearDown(self):
        self.misc._SERIES_CACHE.clear()
        await self.db.close()

    async def _call(self, **query):
        import json

        with patch.object(self.misc, "owner_of", AsyncMock(return_value="admin")), \
             patch.object(self.db, "usage_agent_names", AsyncMock(return_value={})):
            response = await self.misc.handle_usage_series_get(
                SimpleNamespace(query_params=query,
                                state=SimpleNamespace(session="s")))
        return json.loads(response.body)

    async def test_an_hour_window_is_accepted_and_reported(self):
        payload = await self._call(hours="1", bucket="fivemin")
        self.assertEqual(payload["hours"], 1)
        self.assertEqual(payload["days"], 0)
        self.assertEqual(payload["bucket"], "fivemin")

    async def test_the_axis_covers_the_hour_at_the_chosen_width(self):
        payload = await self._call(hours="1", bucket="fivemin")
        self.assertGreaterEqual(len(payload["spine"]), 12)

    async def test_the_hour_window_wins_over_a_days_default(self):
        """Both parameters can arrive; the short one is the explicit ask."""
        payload = await self._call(hours="1", days="30", bucket="minute")
        self.assertEqual(payload["hours"], 1)
        self.assertEqual(payload["days"], 0)

    async def test_a_nonsense_hours_value_falls_back_rather_than_failing(self):
        payload = await self._call(hours="not-a-number", bucket="fivemin")
        self.assertEqual(payload["hours"], 1)

    async def test_the_response_states_the_bucket_it_actually_used(self):
        """Asked for a minute over thirty days; the page must be told which
        width it is looking at, not the one it requested."""
        payload = await self._call(days="30", bucket="minute")
        self.assertNotEqual(payload["bucket"], "minute")
        self.assertTrue(payload["spine"])

    async def test_the_new_buckets_are_advertised(self):
        payload = await self._call(hours="1", bucket="fivemin")
        for name in ("minute", "fivemin", "halfhour", "hour", "day", "month"):
            self.assertIn(name, payload["buckets"])


if __name__ == "__main__":
    unittest.main()
