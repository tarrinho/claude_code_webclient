"""QA: the Statistics page offers an hour, and widths fine enough to fill it.

Asserted from source, this repo's convention for app.js and index.html: there
is no JS runner for them (the quickjs harness in tests/js/ serves
supervisor-map.js and the d3 stub only). See
test_qa_backend_status_on_first_paint.py, whose comment-stripping this mirrors.

What that buys and what it does not. These pin the wiring -- that the option
exists, that a sub-day range travels as `hours` rather than as a day count
that would floor to zero, that the suggested width for an hour is one the hour
can show, and that the picker is corrected to the width the server actually
used. They cannot prove the browser renders it. The behaviour they guard is
the request the page builds, which is where a range picker goes wrong
silently: `days=1h` would parse as 1 day on the server and quietly draw the
wrong window.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "assets" / "app.js"
INDEX = ROOT / "web" / "index.html"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def _select(html: str, select_id: str) -> str:
    match = re.search(
        rf'<select id="{select_id}".*?</select>', html, re.DOTALL)
    assert match, f"the {select_id} select is gone"
    return match.group(0)


class StatsRangeOptionsTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")

    def test_an_hour_is_offered_as_a_range(self):
        self.assertIn('value="1h"', _select(self.html, "statsRange"))

    def test_the_finer_widths_are_offered(self):
        bucket = _select(self.html, "statsBucket")
        self.assertIn('value="minute"', bucket)
        self.assertIn('value="fivemin"', bucket)

    def test_the_existing_widths_are_untouched(self):
        """The new options are additions. Losing one of these would silently
        drop a grouping people use."""
        bucket = _select(self.html, "statsBucket")
        for value in ("halfhour", "hour", "day", "month"):
            with self.subTest(value=value):
                self.assertIn(f'value="{value}"', bucket)

    def test_the_widths_run_finest_to_coarsest(self):
        """The list is read as a scale; an out-of-order entry reads as a
        mistake and makes the neighbouring choices hard to find."""
        bucket = _select(self.html, "statsBucket")
        order = re.findall(r'value="(\w+)"', bucket)
        self.assertEqual(
            order,
            ["minute", "fivemin", "halfhour", "hour", "day", "month"],
        )


class StatsRequestTests(unittest.TestCase):
    def setUp(self):
        self.js = _strip_comments(APP_JS.read_text(encoding="utf-8"))

    def _load_stats(self) -> str:
        match = re.search(
            r"async function loadStats\(.*?\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "loadStats moved or was renamed")
        return match.group(0)

    def test_a_sub_day_range_is_sent_as_hours(self):
        """`days=1h` would parse to 1 day on the server -- a working request
        for the wrong window, which is the failure worth pinning."""
        body = self._load_stats()
        self.assertIn("endsWith('h')", body)
        self.assertRegex(body, r"hours=\$\{encodeURIComponent")

    def test_a_day_range_still_travels_as_days(self):
        self.assertRegex(self._load_stats(), r"days=\$\{encodeURIComponent")

    def test_the_picker_follows_the_bucket_the_server_used(self):
        """The server coarsens a width the window cannot draw, so the control
        has to be corrected or it names a grouping nobody is looking at."""
        body = self._load_stats()
        self.assertIn("payload.bucket", body)
        self.assertRegex(body, r"bucketSelect\.value = used")


class StatsSuggestedWidthTests(unittest.TestCase):
    def setUp(self):
        self.js = _strip_comments(APP_JS.read_text(encoding="utf-8"))

    def test_an_hour_suggests_a_width_that_fills_it(self):
        """An hour grouped by half-hours is two points. The suggestion only
        fires on a range change, so an explicit choice is never overridden."""
        match = re.search(r"const suggested = \{([^}]*)\}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "the range-to-width suggestion map is gone")
        mapping = match.group(1)
        self.assertRegex(mapping, r"'1h':\s*'fivemin'")

    def test_the_existing_suggestions_are_unchanged(self):
        match = re.search(r"const suggested = \{([^}]*)\}", self.js, re.DOTALL)
        mapping = match.group(1)
        for range_value, width in (("1", "halfhour"), ("7", "hour"),
                                   ("30", "day"), ("all", "month")):
            with self.subTest(range=range_value):
                self.assertRegex(mapping, rf"'{range_value}':\s*'{width}'")


if __name__ == "__main__":
    unittest.main()
