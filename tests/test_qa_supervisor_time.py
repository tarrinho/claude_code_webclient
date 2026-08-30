"""QA: the supervisor page's timestamps.

Every timestamp on that page rendered as the words "Invalid Date" -- the event
log, and the task Created/Updated lines with it. The zone marker was appended
unconditionally:

    new Date(iso + "Z")

but ``db._now()`` already returns ``2026-08-30T22:54:00Z`` and
``Date.toISOString()`` returns ``2026-08-30T22:54:00.000Z``, so the value became
``...00ZZ``. The surrounding ``try``/``catch`` never fired, because ``new Date``
does not throw on a value it cannot read -- it returns an Invalid Date, and
``toLocaleTimeString`` on that returns those two words rather than raising.

The function lives inside an IIFE and cannot be imported, so its source is
extracted and evaluated in a real browser engine. That is deliberate: the bug was
in how a JavaScript ``Date`` behaves, and no Python reimplementation of it could
have caught this.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SUPERVISOR_JS = Path(__file__).resolve().parent.parent / "web" / "supervisor.js"

# What the API actually hands this function, and what must come out.
#
# "Renders a time" is asserted rather than an exact string: the output is
# locale- and timezone-dependent, and pinning it would make the test fail on a
# machine set to a different locale rather than on a defect.
CASES = (
    # db._now(): the format every supervisor row is stored in.
    ("2026-08-30T22:54:00Z", "time"),
    # Date.toISOString(): what addLogEntry passes for a live event.
    ("2026-08-30T22:54:00.000Z", "time"),
    # A transcript timestamp, which keeps its milliseconds.
    ("2026-08-30T22:54:55.776Z", "time"),
    # An explicit offset, in both spellings.
    ("2026-08-30T22:54:00+01:00", "time"),
    ("2026-08-30T22:54:00+0100", "time"),
    # Naive: no zone at all. This is the shape the original "+ Z" was written
    # for, and it must keep working.
    ("2026-08-30T22:54:00", "time"),
    # Space instead of "T" -- accepted by Chrome, rejected by Safari, which is
    # what a phone runs.
    ("2026-08-30 22:54:00", "time"),
    # Absent values render as nothing, not as a date.
    ("", "empty"),
    (None, "empty"),
)


def _extract_format_time() -> str:
    """The real function's source, lifted out of the IIFE it lives in."""
    text = SUPERVISOR_JS.read_text(encoding="utf-8")
    start = text.index("function formatTime(")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("could not find the end of formatTime")


class SupervisorTimeQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _extract_format_time()

    def test_the_unconditional_zone_marker_is_gone(self):
        """The specific defect, asserted on the source.

        Cheap, and it fails even where no browser is available -- which is the
        only check in this file that does.
        """
        self.assertNotIn('new Date(iso + "Z")', self.source)
        self.assertNotIn("iso + \"Z\"", self.source)
        # The value is only given a zone when it does not already carry one.
        self.assertRegex(self.source, r"/Z\$\|\[\+-\]")

    def test_it_no_longer_relies_on_an_exception(self):
        # new Date() returns an Invalid Date instead of throwing, so a try/catch
        # around it is not a safety net. The validity has to be tested.
        self.assertIn("Number.isNaN", self.source)

    def test_every_timestamp_shape_the_api_produces_renders(self):
        driver = _browser()
        if driver is None:
            self.skipTest("chromium/selenium unavailable")
        try:
            for value, expectation in CASES:
                with self.subTest(value=value):
                    result = driver.execute_script(
                        f"{self.source}\nreturn formatTime(arguments[0]);",
                        value,
                    )
                    self.assertNotIn(
                        "Invalid", result,
                        f"{value!r} still renders as {result!r}",
                    )
                    if expectation == "empty":
                        self.assertEqual(result, "")
                    else:
                        self.assertTrue(result.strip(), f"{value!r} rendered blank")
                        # A rendered clock time, whatever the locale's shape.
                        self.assertRegex(result, r"\d")
        finally:
            driver.quit()

    def test_an_unreadable_value_shows_itself_not_the_words_invalid_date(self):
        driver = _browser()
        if driver is None:
            self.skipTest("chromium/selenium unavailable")
        try:
            result = driver.execute_script(
                f"{self.source}\nreturn formatTime('not a date at all');")
            self.assertNotIn("Invalid", result)
            self.assertIn("not a date", result)
        finally:
            driver.quit()

    def test_markup_in_an_unreadable_value_is_stripped(self):
        """The fallback is interpolated into innerHTML by both callers.

        Returning the raw value would put an API-supplied string into markup
        unescaped, so the fallback keeps only characters a timestamp needs.
        """
        driver = _browser()
        if driver is None:
            self.skipTest("chromium/selenium unavailable")
        try:
            result = driver.execute_script(
                f"{self.source}\n"
                "return formatTime('<img src=x onerror=alert(1)>');")
            for forbidden in ("<", ">", "=", "(", ")"):
                self.assertNotIn(forbidden, result, f"{forbidden!r} survived")
        finally:
            driver.quit()


class CacheBustingQA(unittest.TestCase):
    def test_the_script_is_versioned_so_a_phone_refetches_it(self):
        """A fix nobody receives is not a fix.

        The stylesheet shipped unversioned earlier this week for the same
        reason, and the symptom was indistinguishable from the bug not being
        fixed at all.
        """
        html = (SUPERVISOR_JS.parent / "supervisor.html").read_text(encoding="utf-8")
        match = re.search(r"supervisor\.js\?v=(\d+)", html)
        self.assertIsNotNone(match, "supervisor.js is loaded without a version")
        self.assertGreaterEqual(int(match.group(1)), 2)


def _browser():
    """A headless Chromium, or None if this machine has no browser."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        return None
    if not Path("/usr/bin/chromium").exists():
        return None
    options = Options()
    options.binary_location = "/usr/bin/chromium"
    for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage"):
        options.add_argument(flag)
    try:
        return webdriver.Chrome(service=Service("/usr/bin/chromedriver"),
                                options=options)
    except Exception:  # noqa: BLE001 -- absence of a browser is a skip
        return None


if __name__ == "__main__":
    unittest.main()
