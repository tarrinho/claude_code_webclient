"""QA: the Server statistics panel keeps itself current.

A live host reading that never changes is worse than no reading at all -- it
looks current and is not. loadServer() ran once when the tab was opened and
never again, so CPU, memory and uptime were frozen at whatever they were the
moment the panel appeared, and the history charts never gained the samples the
server had stored since.

Also pins the half-hour default on both statistics pages: the 24-hour view
exists to show evolution, and grouping a day by day is a single bar.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# startServerPolling/stopServerPolling/loadServer moved to server-stats.js in
# a later module split; concatenated here rather than moving the tests'
# lookups to a second fixture, since _body()'s "find the next function
# declaration" search works the same regardless of which file a name's own
# declaration happens to sit in. closeSettingsDialog and the tab-switch wiring
# stayed in app.js, so both still need to be searchable from one string.
APP_JS = (
    (ROOT / "web" / "assets" / "app.js").read_text()
    + (ROOT / "web" / "assets" / "server-stats.js").read_text()
)
INDEX = (ROOT / "web" / "index.html").read_text()


def _body(name: str) -> str:
    """The source of a top-level function, up to the next one."""
    start = APP_JS.index(f"function {name}(")
    rest = APP_JS[start:]
    match = re.search(r"\n(?:async )?function ", rest[1:])
    return rest[: match.start() + 1] if match else rest


class ServerPanelRefreshTests(unittest.TestCase):
    def test_a_polling_timer_exists(self):
        self.assertIn("startServerPolling", APP_JS)
        self.assertIn("stopServerPolling", APP_JS)

    def test_opening_the_tab_starts_it(self):
        switch = APP_JS[APP_JS.index("if (tab === 'server')"):]
        self.assertIn("startServerPolling()", switch[:300])

    def test_leaving_the_tab_stops_it(self):
        """Otherwise a closed panel polls /proc for the rest of the session."""
        switch = APP_JS[APP_JS.index("if (tab === 'server')"):]
        self.assertIn("stopServerPolling()", switch[:400])

    def test_closing_the_dialog_stops_it(self):
        self.assertIn("stopServerPolling", _body("closeSettingsDialog"))

    def test_the_timer_is_guarded_against_stacking(self):
        """Clicking the tab twice must not leave two intervals running."""
        body = _body("startServerPolling")
        self.assertIn("if (_serverTimer) return", body)

    def test_it_does_not_poll_a_hidden_page(self):
        body = _body("startServerPolling")
        self.assertIn("visibilityState", body)

    def test_it_does_not_poll_a_hidden_panel(self):
        body = _body("startServerPolling")
        self.assertIn("panelServer", body)

    def test_stop_clears_the_handle(self):
        """A stale handle would make the guard refuse to ever start again."""
        body = _body("stopServerPolling")
        self.assertIn("clearInterval(_serverTimer)", body)
        self.assertIn("_serverTimer = null", body)


class QuietRefreshTests(unittest.TestCase):
    """A background refresh must not make a working panel look broken."""

    def test_load_server_takes_a_quiet_flag(self):
        self.assertIn("async function loadServer(quiet = false)", APP_JS)

    def test_the_poll_asks_for_a_quiet_refresh(self):
        self.assertIn("loadServer(true)", _body("startServerPolling"))

    def test_opening_the_tab_is_not_quiet(self):
        """The first draw has nothing on screen to preserve, so it shows work."""
        switch = APP_JS[APP_JS.index("if (tab === 'server')"):]
        self.assertIn("loadServer();", switch[:300])

    def test_skeletons_are_skipped_on_a_quiet_refresh(self):
        body = _body("loadServer")
        self.assertIn("if (!quiet) {", body)
        skeleton = body.index("skill-skeleton")
        guard = body.index("if (!quiet) {")
        self.assertLess(guard, skeleton, "the skeleton must sit inside the guard")

    def test_a_failed_poll_keeps_the_last_good_reading(self):
        body = _body("loadServer")
        catch = body[body.index("} catch"):]
        self.assertIn("if (quiet) return;", catch)
        self.assertLess(catch.index("if (quiet) return;"),
                        catch.index("replaceChildren"))


class HalfHourDefaultTests(unittest.TestCase):
    """24 hours grouped by day is one bar; the point is to see the shape."""

    def test_the_server_page_defaults_to_24h_and_half_hours(self):
        self.assertIn('<option value="1" selected>Last 24 hours</option>', INDEX)
        self.assertIn('<option value="halfhour" selected>By 30 minutes</option>',
                      INDEX)

    def test_both_pages_offer_the_half_hour_slot(self):
        self.assertEqual(INDEX.count('value="halfhour"'), 2)

    def test_choosing_24_hours_switches_both_pages_to_half_hours(self):
        for control in ("statsRange", "serverRange"):
            with self.subTest(control=control):
                listener = APP_JS[APP_JS.index(f"byId('{control}')?.addEventListener"):]
                suggested = listener[:listener.index("});")]
                self.assertIn("'1': 'halfhour'", suggested)

    def test_the_client_default_bucket_is_half_hourly(self):
        self.assertIn("byId('serverBucket')?.value || 'halfhour'", APP_JS)


if __name__ == "__main__":
    unittest.main()
