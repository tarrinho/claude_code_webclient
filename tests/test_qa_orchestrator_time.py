"""QA: the orchestrator page's timestamps.

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
import threading
import unittest
from pathlib import Path

SUPERVISOR_JS = Path(__file__).resolve().parent.parent / "web" / "assets" / "orchestrator" / "main.js"

def supervisor_source() -> str:
    """Every orchestrator module, concatenated.

    The 0.10.0 split turned one file into nine, so a substring assertion that
    reads main.js alone searches a fraction of the code and fails on everything
    that moved. Globbing the directory means the next extraction needs no edit
    here, and deduplicating by resolved path means SUPERVISOR_JS pointing inside
    that directory does not read one module twice.
    """
    seen, parts = set(), []
    for path in sorted(SUPERVISOR_JS.parent.glob("*.js")):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        parts.append(path.read_text(encoding="utf-8"))
    if not parts:
        raise AssertionError(
            f"no orchestrator modules found beside {SUPERVISOR_JS} -- the split "
            "moved them somewhere this test does not know about"
        )
    return "\n".join(parts)


# What the API actually hands this function, and what must come out.
#
# "Renders a time" is asserted rather than an exact string: the output is
# locale- and timezone-dependent, and pinning it would make the test fail on a
# machine set to a different locale rather than on a defect.
CASES = (
    # db._now(): the format every orchestrator row is stored in.
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
    text = supervisor_source()
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

    def _evaluate(self, *arguments: str) -> list[str]:
        """Run the real ``formatTime`` in a real engine, once per argument.

        One browser for the whole call, in a **dedicated thread**. The thread is
        the load-bearing part: playwright's sync API refuses to start when the
        calling thread already has a running asyncio loop, and under pytest this
        thread does. Driving it from a worker keeps the sync API — which the rest
        of this suite uses — instead of splitting this one file onto the async
        API for an environmental reason.

        It also contains the hazard from registry #35. A playwright that is
        never stopped leaves its greenlet-driven loop marked as its thread's
        running loop, which once failed ~850 unrelated async tests; here that
        loop belongs to a thread that has already exited, so it cannot be
        inherited by anything. Teardown is in the worker's own ``finally``,
        innermost first, for the same reason ``addCleanup`` is LIFO.
        """
        results: list[str] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                pw = sync_playwright().start()
                try:
                    browser = pw.chromium.launch(
                        executable_path=str(CHROMIUM), args=["--no-sandbox"]
                    )
                    try:
                        page = browser.new_page()
                        for argument in arguments:
                            results.append(page.evaluate(
                                f"(arg) => {{ {self.source}\nreturn formatTime(arg); }}",
                                argument,
                            ))
                    finally:
                        browser.close()
                finally:
                    pw.stop()
            except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller
                errors.append(exc)

        thread = threading.Thread(target=work, name="formatTime-browser")
        thread.start()
        thread.join(timeout=120)
        if thread.is_alive():
            self.fail("the browser thread did not finish within 120s")
        if errors:
            raise errors[0]
        return results

    def test_every_timestamp_shape_the_api_produces_renders(self):
        if _page_unavailable():
            self.skipTest("chromium/playwright unavailable")
        # One browser for all nine shapes: launching chromium per subtest turned
        # a 4-second case into a 40-second one for no extra coverage.
        results = self._evaluate(*[value or "" for value, _ in CASES])
        for (value, expectation), result in zip(CASES, results, strict=True):
            with self.subTest(value=value):
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
                    # And it must be a *converted* time, not the input handed
                    # back. Mutation testing found this gap: restoring the
                    # original `new Date(raw + "Z")` bug while keeping the
                    # `Number.isNaN` fallback makes every valid timestamp fall
                    # into that fallback and return the raw ISO string — which
                    # contains digits, so the assertion above passed and only
                    # the source-text check noticed. Asserting the value was
                    # transformed is what makes these cases earn their runtime.
                    self.assertNotEqual(
                        result, value,
                        f"{value!r} came back unchanged, so it was not parsed",
                    )
                    self.assertNotIn(
                        "-", result,
                        f"{value!r} rendered as a date ({result!r}), not a "
                        f"clock time — the parse fell through to the fallback",
                    )

    def test_an_unreadable_value_shows_itself_not_the_words_invalid_date(self):
        if _page_unavailable():
            self.skipTest("chromium/playwright unavailable")
        result = self._evaluate("not a date at all")[0]
        self.assertNotIn("Invalid", result)
        self.assertIn("not a date", result)

    def test_markup_in_an_unreadable_value_is_stripped(self):
        """The fallback is interpolated into innerHTML by both callers.

        Returning the raw value would put an API-supplied string into markup
        unescaped, so the fallback keeps only characters a timestamp needs.
        """
        if _page_unavailable():
            self.skipTest("chromium/playwright unavailable")
        result = self._evaluate("<img src=x onerror=alert(1)>")[0]
        for forbidden in ("<", ">", "=", "(", ")"):
            self.assertNotIn(forbidden, result, f"{forbidden!r} survived")


class CacheBustingQA(unittest.TestCase):
    def test_the_script_is_versioned_so_a_phone_refetches_it(self):
        """A fix nobody receives is not a fix.

        The stylesheet shipped unversioned earlier this week for the same
        reason, and the symptom was indistinguishable from the bug not being
        fixed at all.
        """
        # Not `SUPERVISOR_JS.parent`: the script moved to
        # web/assets/orchestrator/ in 0.10.0, so its parent is the module
        # directory and the page is no longer beside it. Deriving one path from
        # another is what made this break on a move that did not touch the HTML.
        html = (Path(__file__).resolve().parent.parent / "web" /
                "orchestrator.html").read_text(encoding="utf-8")
        match = re.search(r"orchestrator/main\.js\?v=(\d+)", html)
        self.assertIsNotNone(
            match, "the orchestrator module is loaded without a version")
        self.assertGreaterEqual(int(match.group(1)), 2)


# Playwright, not selenium. These three cases were written against selenium and
# skipped on every run since, because selenium is not in `.venv` -- while
# playwright is, and is what every other browser test here uses. Three skips
# read as "not applicable on this machine", which is indistinguishable from
# "ran and passed" in an aggregate total: registry #50, in a file I wrote.
#
# The repair is the habit, not the box. Installing selenium to suit the code
# would have changed the machine to accommodate a one-off import, which is the
# choice #50 explicitly declined.
CHROMIUM = Path("/usr/bin/chromium")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover -- absence is a skip, not a failure
    sync_playwright = None


def _page_unavailable() -> bool:
    """True when this machine cannot run a browser, so the case must skip.

    Checks the driver's own executable rather than merely importing playwright.
    The Debian playwright package imports fine and then resolves its driver to
    `/usr/bin/node`, which this box does not have -- so an import-only check
    reports a browser that cannot start. That exact mismatch is what made #50's
    diagnosis wrong on the first attempt.
    """
    if sync_playwright is None or not CHROMIUM.exists():
        return True
    try:
        from playwright._impl._driver import compute_driver_executable

        driver = compute_driver_executable()
        driver = driver[0] if isinstance(driver, (list, tuple)) else driver
        return not Path(driver).exists()
    except Exception:  # noqa: BLE001 -- any resolution failure means skip
        return True




if __name__ == "__main__":
    unittest.main()
