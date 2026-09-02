"""QA: the statistics and server charts have a continuous time axis.

Pedro asked why the graphs' time was not continuous. It was not a rendering
artefact and it was not missing data: both series queries end in
``GROUP BY bucket``, so a bucket no row falls into is not in the result at all,
and ``lineChart`` places points by index. An idle interval was therefore not
drawn as a gap -- it was absent from the axis, and its neighbours were rendered
adjacent. Measured on the live database: 41,429 usage events spanning about 166
hours, of which only 105 hours contained any usage, and five idle hours
overnight put midnight one step from 06:00.

Two fills, and they must differ. An hour with no usage genuinely is zero
tokens. An interval with no host sample is *no measurement*, so it comes back
null and the line breaks: zero-filling it would draw the machine sitting at 0%
CPU and 0% memory across precisely the windows the sampler was not running,
which is an invented reading, and the chart looks most confident exactly where
it knows least.

The load-bearing detail is the timezone. ``_bucket_expr`` buckets in *local*
time via ``datetime(created_at, 'localtime')``. A spine generated in UTC would
have labels that look right and keys that never match a row, so every real
bucket would be treated as an extra one and every series would double. That is
asserted here against SQLite itself rather than against a restatement of the
rule, because agreeing with my own reading of the expression proves nothing.
"""
from __future__ import annotations

import sqlite3
import time
import unittest

import db
from routes import misc as misc_routes


class SpineKeysMatchTheSqlTests(unittest.TestCase):
    """The keys must equal what the query groups by, or nothing lines up."""

    @staticmethod
    def _sql_bucket(stamp: str, bucket: str) -> str:
        """The bucket key SQLite produces for *stamp*, via the real expression."""
        expr, params = db._bucket_expr(bucket)
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE TABLE t (created_at TEXT)")
            con.execute("INSERT INTO t VALUES (?)", (stamp,))
            row = con.execute(
                f"SELECT {expr} AS bucket FROM t",  # nosec B608: expression is db's
                params,
            ).fetchone()
        finally:
            con.close()
        return row[0]

    def test_the_generated_key_equals_the_grouped_key(self):
        """Same instant through both paths: Python's spine and SQLite's GROUP BY.

        This is the assertion that would have caught a UTC spine. The machine
        this runs on is UTC+1 in September, so a naive implementation is off by
        one bucket at the hour and half-hour widths and silently correct at the
        day width for most of the day -- which is exactly the shape of bug that
        passes a casual test.
        """
        now = time.time()
        for bucket in ("halfhour", "hour", "day"):
            with self.subTest(bucket=bucket):
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                self.assertEqual(
                    db._bucket_key(now, bucket), self._sql_bucket(stamp, bucket),
                    f"the {bucket} spine key disagrees with the SQL it must "
                    "match, so no generated bucket would ever join a real row",
                )

    def test_it_holds_across_a_whole_day_of_instants(self):
        """One instant could agree by luck; the offset shows up at some hours."""
        base = time.time()
        for hours in range(24):
            moment = base - hours * 3600
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
            for bucket in ("hour", "day"):
                with self.subTest(hours_ago=hours, bucket=bucket):
                    self.assertEqual(
                        db._bucket_key(moment, bucket),
                        self._sql_bucket(stamp, bucket),
                    )


class SpineShapeTests(unittest.TestCase):
    """Bounds, ordering and the widths that have no fixed step."""

    def test_an_hour_spine_covers_the_window(self):
        spine = db.bucket_spine("hour", 1)
        # 24 hours inclusive of both ends.
        self.assertGreaterEqual(len(spine), 24)
        self.assertLessEqual(len(spine), 26)

    def test_it_is_sorted_and_has_no_duplicates(self):
        """`lineChart` reads the axis in order and indexes into it."""
        for bucket in ("halfhour", "hour", "day"):
            with self.subTest(bucket=bucket):
                spine = db.bucket_spine(bucket, 2)
                self.assertEqual(spine, sorted(spine))
                self.assertEqual(len(spine), len(set(spine)))

    def test_half_hours_land_only_on_00_and_30(self):
        for key in db.bucket_spine("halfhour", 1):
            self.assertIn(key[-2:], ("00", "30"), f"{key} is not a half hour")

    def test_it_does_not_run_into_the_future(self):
        """A trailing empty bucket would draw the axis past now."""
        latest = db._bucket_key(time.time(), "hour")
        self.assertLessEqual(max(db.bucket_spine("hour", 1)), latest)

    def test_months_get_no_spine(self):
        """A month is not a fixed number of seconds, and a coarse empty bucket
        is legible as a gap anyway."""
        self.assertEqual(db.bucket_spine("month", 90), [])

    def test_an_unknown_bucket_gets_no_spine(self):
        self.assertEqual(db.bucket_spine("fortnight", 30), [])

    def test_unbounded_without_a_start_gets_no_spine(self):
        """days=None means "everything", which has no start until data says."""
        self.assertEqual(db.bucket_spine("hour", None), [])

    def test_unbounded_uses_the_earliest_row(self):
        earliest = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))
        spine = db.bucket_spine("day", None, earliest)
        self.assertGreaterEqual(len(spine), 3)
        self.assertLessEqual(len(spine), 5)

    def test_an_unenumerable_window_is_dropped_not_truncated(self):
        """Ten years of half hours is 175,000 keys.

        Dropped, because half a spine mislabels the axis it exists to fix --
        the series would be plotted against a window that ends early, which is
        worse than the gap it replaces.
        """
        self.assertEqual(db.bucket_spine("halfhour", 3650), [])

    def test_a_garbage_earliest_is_refused(self):
        self.assertEqual(db.bucket_spine("day", None, "not a timestamp"), [])


class OnSpineTests(unittest.TestCase):
    """Placing real rows on the spine, and what a hole looks like."""

    def _rows(self, buckets):
        return [
            {"bucket": b, "samples": 60, "cpu_pct": 12.5, "mem_pct": 40.0}
            for b in buckets
        ]

    def test_a_missing_bucket_is_null_not_zero(self):
        """The distinction the whole change turns on.

        A zero here would draw the box idling at 0% CPU across an outage, which
        asserts a measurement that was never taken.
        """
        spine = db.bucket_spine("hour", 1)
        present = [spine[0], spine[-1]]
        filled = db._on_spine(self._rows(present), "hour", 1)
        holes = [r for r in filled if r["bucket"] not in present]
        self.assertTrue(holes, "nothing was filled, so the spine did not apply")
        for row in holes:
            self.assertIsNone(row["cpu_pct"], "a hole must not claim a reading")
            self.assertIsNone(row["mem_pct"])
            self.assertEqual(
                row["samples"], 0,
                "samples is the one honest number in a hole, and it is what "
                "marks the row as a placeholder",
            )

    def test_real_rows_keep_their_values(self):
        spine = db.bucket_spine("hour", 1)
        filled = db._on_spine(self._rows([spine[2]]), "hour", 1)
        kept = [r for r in filled if r["bucket"] == spine[2]]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["cpu_pct"], 12.5)

    def test_the_result_is_continuous_and_ordered(self):
        spine = db.bucket_spine("hour", 1)
        filled = db._on_spine(self._rows([spine[1], spine[5]]), "hour", 1)
        got = [r["bucket"] for r in filled]
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), len(spine), "the axis is not the full window")

    def test_a_placeholder_carries_every_column_a_real_row_has(self):
        """A row missing keys makes the client read undefined, not null, and
        `Number(undefined)` is NaN, which poisons the y scale."""
        spine = db.bucket_spine("hour", 1)
        filled = db._on_spine(self._rows([spine[0]]), "hour", 1)
        real = next(r for r in filled if r["samples"])
        for row in filled:
            self.assertEqual(set(row), set(real), f"{row['bucket']} is misshapen")

    def test_rows_outside_the_spine_are_kept(self):
        """A bucket holding data is evidence; a spine disagreeing with it is
        the thing to distrust. Dropping it would delete a real reading."""
        rows = self._rows(["1999-01-01T00"])
        filled = db._on_spine(rows, "hour", 1)
        self.assertIn("1999-01-01T00", [r["bucket"] for r in filled])

    def test_no_rows_stays_no_rows(self):
        """An empty table must not become a wall of null placeholders.

        "No data at all" and "measured, and it was nothing" look identical once
        the axis is drawn, and the first should render as an empty chart.
        """
        self.assertEqual(db._on_spine([], "hour", 1), [])

    def test_an_unavailable_spine_leaves_the_rows_alone(self):
        """Unfilled is still correct, just not continuous."""
        rows = self._rows(["2026-09"])
        self.assertEqual(db._on_spine(rows, "month", 90), rows)


class FillIsOptInTests(unittest.TestCase):
    """Where the spine is applied, and where it deliberately is not.

    Filling inside `system_series` unconditionally changed what every caller
    gets: two stored samples came back as 2,437 rows, and it broke six storage
    tests whose subject is the bucket expression rather than the axis. Those
    tests were right and the change was at the wrong layer -- a continuous axis
    is a presentation need, so the chart endpoint opts in and the storage read
    keeps its contract.
    """

    def test_the_storage_read_does_not_fill_by_default(self):
        import inspect
        params = inspect.signature(db.system_series).parameters
        self.assertIn("fill", params)
        self.assertIs(
            params["fill"].default, False,
            "filling by default changes every caller's result, including the "
            "storage tests whose subject is the bucketing itself",
        )

    def test_the_chart_endpoint_asks_for_the_fill(self):
        """Without this the endpoint silently returns the gappy series again,
        and nothing else in the suite would notice."""
        import inspect

        body = inspect.getsource(misc_routes.handle_system_series_get)
        self.assertIn("fill=True", body)
        self.assertIn("db.system_series", body)

    def test_the_usage_endpoint_sends_a_spine(self):
        import inspect

        body = inspect.getsource(misc_routes.handle_usage_series_get)
        self.assertIn('"spine"', body)
        self.assertIn("bucket_spine", body)

    def test_the_usage_spine_is_owner_scoped_when_unbounded(self):
        """An unbounded window starts where *this* owner's data starts, not
        where the busiest account on the machine begins."""
        import inspect

        body = inspect.getsource(misc_routes.handle_usage_series_get)
        self.assertIn("usage_earliest", body)
        self.assertIn("owner", body.split("usage_earliest", 1)[1][:40])


if __name__ == "__main__":
    unittest.main()
