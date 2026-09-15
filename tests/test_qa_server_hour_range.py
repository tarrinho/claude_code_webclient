"""QA: the Server tab offers an hour, at widths its sampling can fill.

The host is sampled every ``SYSTEM_SAMPLE_S`` seconds (60 by default), and
that is what makes these widths meaningful rather than decorative: measured on
the live database, the local host produced 57 samples in an hour, so a minute
bucket is one sample wide and a five-minute bucket holds five. Transports
sample less often -- 25 to 49 in the same hour -- which is why the series is
fetched with ``fill=True``: an interval with no sample comes back null and the
chart breaks the line rather than drawing a disconnect as a flat reading.

This mirrors tests/test_qa_stats_hour_range.py for the other page. The two
share the window contract (``hours`` becomes a fractional day count) and the
bucket machinery, so the parts asserted here are the ones that are separate:
the Server tab's own selects, its own request, and ``_system_range``, which
parses and clamps independently of the usage handler.

The endpoint tests hit ``_system_range`` directly rather than the route.
``handle_system_series_get`` reads the host sample tables, and what is in
doubt is the parsing and clamping, not the aggregation those tables already
have their own tests for.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
SERVER_JS = ROOT / "web" / "assets" / "server-stats.js"
APP_JS = ROOT / "web" / "assets" / "app.js"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def _select(html: str, select_id: str) -> str:
    match = re.search(rf'<select id="{select_id}".*?</select>', html, re.DOTALL)
    assert match, f"the {select_id} select is gone"
    return match.group(0)


class ServerSelectTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")

    def test_an_hour_is_offered(self):
        self.assertIn('value="1h"', _select(self.html, "serverRange"))

    def test_the_finer_widths_are_offered(self):
        bucket = _select(self.html, "serverBucket")
        self.assertIn('value="minute"', bucket)
        self.assertIn('value="fivemin"', bucket)

    def test_the_existing_widths_survive(self):
        bucket = _select(self.html, "serverBucket")
        for value in ("halfhour", "hour", "day"):
            with self.subTest(value=value):
                self.assertIn(f'value="{value}"', bucket)

    def test_the_widths_run_finest_to_coarsest(self):
        order = re.findall(r'value="(\w+)"', _select(self.html, "serverBucket"))
        self.assertEqual(order, ["minute", "fivemin", "halfhour", "hour", "day"])


class ServerRequestTests(unittest.TestCase):
    def setUp(self):
        self.js = _strip_comments(SERVER_JS.read_text(encoding="utf-8"))

    def test_a_sub_day_range_is_sent_as_hours(self):
        """`days=1h` parses to one day on the server: a working request for
        the wrong window, which is the failure a range picker hides best."""
        self.assertIn("endsWith('h')", self.js)
        self.assertRegex(self.js, r"hours=\$\{encodeURIComponent")

    def test_a_day_range_still_travels_as_days(self):
        self.assertRegex(self.js, r"days=\$\{encodeURIComponent")

    def test_the_picker_follows_the_width_the_server_used(self):
        self.assertIn("history.bucket", self.js)
        self.assertRegex(self.js, r"bucketSelect\.value = usedBucket")

    def test_an_hour_suggests_a_width_that_fills_it(self):
        """An hour grouped by half-hours is two points, which is the same
        defect the range existed to fix."""
        js = _strip_comments(APP_JS.read_text(encoding="utf-8"))
        block = re.search(
            r"byId\('serverRange'\)\?\.addEventListener.*?\}\);", js, re.DOTALL)
        self.assertIsNotNone(block, "the serverRange handler moved")
        self.assertRegex(block.group(0), r"'1h':\s*'fivemin'")


class SystemRangeParsingTests(unittest.TestCase):
    """_system_range is the Server tab's own parser; the usage handler has a
    separate one, so neither covers the other."""

    def setUp(self):
        from routes import misc as misc_routes

        self.misc = misc_routes

    def _range(self, **params):
        return self.misc._system_range(
            SimpleNamespace(query_params=params,
                            state=SimpleNamespace(session="s")))

    def test_an_hour_becomes_a_fractional_day(self):
        days, bucket = self._range(hours="1", bucket="fivemin")
        self.assertAlmostEqual(days, 1 / 24)
        self.assertEqual(bucket, "fivemin")

    def test_the_hour_wins_over_a_days_default(self):
        days, _ = self._range(hours="1", days="30", bucket="minute")
        self.assertAlmostEqual(days, 1 / 24)

    def test_a_nonsense_hours_value_falls_back_to_one_hour(self):
        days, _ = self._range(hours="banana", bucket="fivemin")
        self.assertAlmostEqual(days, 1 / 24)

    def test_the_day_windows_are_unchanged(self):
        self.assertEqual(self._range(days="7", bucket="hour"), (7, "hour"))
        self.assertEqual(self._range(days="all", bucket="hour")[0], None)

    def test_a_width_too_fine_for_the_window_is_coarsened(self):
        """Thirty days by the minute is 43,200 intervals: an axis past its own
        cap and a payload of points no screen can show."""
        _, bucket = self._range(days="30", bucket="minute")
        self.assertNotEqual(bucket, "minute")

    def test_an_unknown_width_falls_back_rather_than_reaching_sql(self):
        _, bucket = self._range(hours="1", bucket="not-a-bucket")
        self.assertIn(bucket, ("halfhour", "minute", "fivemin", "hour", "day"))


if __name__ == "__main__":
    unittest.main()
