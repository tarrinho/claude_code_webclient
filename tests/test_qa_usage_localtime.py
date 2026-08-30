"""QA: statistics are labelled in local time, not UTC.

Timestamps are stored in UTC, which is right, and were bucketed in UTC, which
was not: work done at 21:00 in Lisbon was charted at 20:00, and the "today"
column began at 01:00 rather than midnight. Portugal is UTC+1 in summer and
UTC+0 in winter, so a fixed offset would only be right for half the year --
the grouping goes through SQLite's 'localtime', which resolves the zone for
each timestamp individually.

Every test here sets TZ explicitly and restores it, so the assertions hold on a
machine whose clock is UTC as well as on the one this runs on. Without that a
UTC box would pass these no matter what the code did.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import config
import db


@contextmanager
def timezone(name: str):
    """Run the block with TZ set, including for SQLite's own localtime."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


class LocalTimeBucketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(config.PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
        await db.chat_create("c1", "C", None, config.PROJECTS_ROOT, "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _event(self, stamp: str) -> None:
        await db.usage_record("c1", "admin", "m", "cli", input_tokens=1)
        await db.db_conn.execute(
            "UPDATE usage_events SET created_at = ? "
            "WHERE id = (SELECT MAX(id) FROM usage_events)",
            (stamp,),
        )
        await db.db_conn.commit()

    async def _buckets(self, bucket: str) -> list[str]:
        rows = await db.usage_series("admin", days=None, bucket=bucket)
        return [r["bucket"] for r in rows]

    async def test_summer_in_lisbon_is_an_hour_ahead_of_utc(self):
        """20:23 UTC in August is 21:23 in Lisbon, and must chart as 21."""
        await self._event("2026-08-30T20:23:54Z")
        with timezone("Europe/Lisbon"):
            self.assertEqual(await self._buckets("hour"), ["2026-08-30T21"])

    async def test_winter_in_lisbon_matches_utc(self):
        """The same zone is UTC+0 in January: a fixed +1 would be wrong here."""
        await self._event("2026-01-15T12:00:00Z")
        with timezone("Europe/Lisbon"):
            self.assertEqual(await self._buckets("hour"), ["2026-01-15T12"])

    async def test_one_zone_does_not_decide_the_answer_for_another(self):
        """The same stored row lands in a different hour in a different zone."""
        await self._event("2026-08-30T20:23:54Z")
        with timezone("UTC"):
            self.assertEqual(await self._buckets("hour"), ["2026-08-30T20"])
        with timezone("Europe/Lisbon"):
            self.assertEqual(await self._buckets("hour"), ["2026-08-30T21"])

    async def test_late_evening_belongs_to_the_local_day(self):
        """23:30 in Lisbon is the 30th, though it is already the 31st in UTC.

        This is the half of the bug the labels do not show: with UTC grouping
        the day column changed over at 01:00 local, so an evening's work was
        filed under tomorrow.
        """
        await self._event("2026-08-30T23:30:00Z")   # 00:30 on the 31st, Lisbon
        with timezone("Europe/Lisbon"):
            self.assertEqual(await self._buckets("day"), ["2026-08-31"])
        await self._event("2026-08-30T22:30:00Z")   # 23:30 on the 30th, Lisbon
        with timezone("Europe/Lisbon"):
            self.assertEqual(await self._buckets("day"), ["2026-08-30", "2026-08-31"])

    async def test_the_half_hour_slot_follows_the_local_clock(self):
        await self._event("2026-08-30T20:05:00Z")   # 21:05 Lisbon -> 21:00 slot
        await self._event("2026-08-30T20:35:00Z")   # 21:35 Lisbon -> 21:30 slot
        with timezone("Europe/Lisbon"):
            self.assertEqual(
                await self._buckets("halfhour"),
                ["2026-08-30T21:00", "2026-08-30T21:30"],
            )

    async def test_a_month_boundary_follows_the_local_calendar(self):
        await self._event("2026-08-31T23:30:00Z")   # 00:30 on 1 Sep, Lisbon
        with timezone("Europe/Lisbon"):
            self.assertEqual(await self._buckets("month"), ["2026-09"])

    async def test_keys_keep_the_shape_the_client_expects(self):
        """The client splits on "T" and prints the parts; a space would show."""
        await self._event("2026-08-30T20:23:54Z")
        with timezone("Europe/Lisbon"):
            for bucket, width in (("halfhour", 16), ("hour", 13), ("day", 10)):
                with self.subTest(bucket=bucket):
                    key = (await self._buckets(bucket))[0]
                    self.assertEqual(len(key), width)
                    self.assertNotIn(" ", key)
                    if width > 10:
                        self.assertEqual(key[10], "T")

    async def test_the_system_series_is_labelled_the_same_way(self):
        """Both statistics pages share the convention, or they disagree."""
        await db.system_sample_insert({
            "cpu_pct": 5.0, "mem_pct": 10.0, "load": [0.1, 0.2, 0.3],
            "proc": {"rss": 1024},
        })
        await db.db_conn.execute(
            "UPDATE system_samples SET created_at = ? "
            "WHERE id = (SELECT MAX(id) FROM system_samples)",
            ("2026-08-30T20:23:54Z",),
        )
        await db.db_conn.commit()
        with timezone("Europe/Lisbon"):
            rows = await db.system_series(None, "hour")
        self.assertEqual([r["bucket"] for r in rows], ["2026-08-30T21"])


if __name__ == "__main__":
    unittest.main()
