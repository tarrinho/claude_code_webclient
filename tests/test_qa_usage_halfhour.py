"""QA: 30-minute buckets, so a busy hour shows its shape.

Every other bucket is a prefix of the ISO timestamp, which is one substr. A
half hour is not a prefix -- the minute has to be floored to 00 or 30 -- so it
needs its own SQL expression. These tests pin the boundary, since an off-by-one
there silently merges two slots or splits one.

Timestamps are stored in UTC and bucketed in local time, so the expected keys
are derived from the stored value rather than written out: hardcoding them
would only pass on a machine whose clock happens to be UTC. The conversion
itself is pinned separately, under a fixed zone, in test_qa_usage_localtime.
"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db


def local_of(stamp: str) -> str:
    """The stored UTC stamp rendered in the machine's zone, as the SQL sees it."""
    # %z rather than a literal Z plus replace(): it yields an aware datetime
    # directly, which is what astimezone() needs to mean anything.
    utc = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    return utc.astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def key_of(stamp: str, bucket: str) -> str:
    """The bucket key a stored UTC stamp should land in."""
    local = local_of(stamp)
    if bucket == "halfhour":
        return local[:14] + ("00" if int(local[14:16]) < 30 else "30")
    return local[: db.USAGE_BUCKETS[bucket]]


def at(hour: int, minute: int, second: int = 0, day: int = 30) -> str:
    """The UTC stamp to store for a given *local* wall-clock time.

    Anything that reasons about slot boundaries has to be written this way
    round. A test that stores 09:29:59Z and 09:30:00Z is only straddling a
    boundary in a zone whose offset is a whole or half hour: in Kathmandu
    (+05:45) those are 15:14:59 and 15:15:00, both comfortably inside the
    15:00 slot, and the assertion fails for a reason that has nothing to do
    with the code. Building from the local side keeps the intent -- "either
    side of the half hour" -- true in every zone.
    """
    local = datetime.datetime(2026, 8, day, hour, minute, second).astimezone()
    return local.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HalfHourBucketTests(unittest.IsolatedAsyncioTestCase):
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

    async def _event(self, stamp: str, model: str = "m") -> None:
        await db.usage_record("c1", "admin", model, "cli", input_tokens=1)
        await db.db_conn.execute(
            "UPDATE usage_events SET created_at = ? "
            "WHERE id = (SELECT MAX(id) FROM usage_events)",
            (stamp,),
        )
        await db.db_conn.commit()

    async def test_halfhour_is_an_offered_bucket(self):
        self.assertIn("halfhour", db.USAGE_BUCKETS)

    async def test_the_boundary_falls_between_29_and_30(self):
        """:29:59 belongs to the first half, :30:00 to the second."""
        first, second = at(9, 29, 59), at(9, 30, 0)
        await self._event(first)
        await self._event(second)
        rows = await db.usage_series("admin", days=None, bucket="halfhour")
        self.assertEqual(
            {r["bucket"]: r["requests"] for r in rows},
            {key_of(first, "halfhour"): 1, key_of(second, "halfhour"): 1},
        )
        # And they really are two different slots, whatever the zone.
        self.assertNotEqual(key_of(first, "halfhour"), key_of(second, "halfhour"))

    async def test_an_hour_splits_into_two_slots(self):
        """A local hour, so the four fall either side of one local half-hour."""
        for minute in (1, 15, 44, 59):
            await self._event(at(9, minute))
        half = await db.usage_series("admin", days=None, bucket="halfhour")
        hour = await db.usage_series("admin", days=None, bucket="hour")
        self.assertEqual(len(half), 2)
        self.assertEqual(len(hour), 1)
        self.assertEqual(sum(r["requests"] for r in half),
                         sum(r["requests"] for r in hour))

    async def test_keys_sort_chronologically_as_strings(self):
        """The query relies on ORDER BY bucket, so the key must sort by time."""
        for stamp in ("2026-08-30T09:45:00Z", "2026-08-30T09:15:00Z",
                      "2026-08-30T10:15:00Z"):
            await self._event(stamp)
        rows = await db.usage_series("admin", days=None, bucket="halfhour")
        keys = [r["bucket"] for r in rows]
        self.assertEqual(keys, sorted(keys))

    async def test_other_buckets_are_unchanged(self):
        stamp = "2026-08-30T09:15:00Z"
        await self._event(stamp)
        for bucket in ("hour", "day", "month"):
            with self.subTest(bucket=bucket):
                rows = await db.usage_series("admin", days=None, bucket=bucket)
                self.assertEqual(rows[0]["bucket"], key_of(stamp, bucket))

    async def test_model_series_buckets_the_same_way(self):
        """Both charts share an x-axis; different bucketing would misalign them."""
        early, late = "2026-08-30T09:10:00Z", "2026-08-30T09:40:00Z"
        await self._event(early, model="a")
        await self._event(late, model="a")
        rows = await db.usage_model_series("admin", days=None, bucket="halfhour")
        self.assertEqual(
            sorted(r["bucket"] for r in rows),
            sorted({key_of(early, "halfhour"), key_of(late, "halfhour")}),
        )

    async def test_an_unknown_bucket_still_falls_back_to_day(self):
        stamp = "2026-08-30T09:15:00Z"
        await self._event(stamp)
        rows = await db.usage_series("admin", days=None, bucket="fortnight")
        self.assertEqual(rows[0]["bucket"], key_of(stamp, "day"))


class BucketExpressionTests(unittest.TestCase):
    def test_halfhour_binds_no_parameters(self):
        """It is a literal expression; the prefix buckets bind their width."""
        expr, params = db._bucket_expr("halfhour")
        self.assertEqual(params, [])
        self.assertIn("1, 14", expr)

    def test_prefix_buckets_stay_parameterised(self):
        for name in ("hour", "day", "month"):
            with self.subTest(bucket=name):
                expr, params = db._bucket_expr(name)
                self.assertEqual(params, [db.USAGE_BUCKETS[name]])
                self.assertTrue(expr.endswith(", 1, ?)"), expr)

    def test_every_bucket_groups_on_local_time(self):
        """A chart labelled in UTC reads an hour early for half the year."""
        for name in ("halfhour", "hour", "day", "month"):
            with self.subTest(bucket=name):
                expr, _ = db._bucket_expr(name)
                self.assertIn("'localtime'", expr)


if __name__ == "__main__":
    unittest.main()
