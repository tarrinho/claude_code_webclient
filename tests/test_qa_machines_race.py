"""Reported live: Settings opened and the machine list rendered empty, with no
console error at all.

Root cause: loadMachines() has two independent callers with no coordination --
loadInitialData() at page boot, and loadBackends() every time the Backends tab
opens (openSettingsDialog() calls _switchTab('backends') unconditionally).
Before the fix, both callers fired their own fetch and mutated the one shared
`_machines` array in place, and a failed fetch cleared it outright
(`catch { _machines.length = 0; }`) with no regard for what the other in-flight
call was doing or had already achieved. Reproduced live against the running
service: the panel opened with a real card on screen, and a moment later the
list was empty, because a second, slower, uncoordinated call to loadMachines()
failed and wiped the array behind the render's back.

The fix has two parts, and each gets its own test here:

1. Concurrent callers now share one in-flight fetch instead of each starting
   their own -- so opening Settings while the boot-time load is still pending
   can no longer race a second request against the first at all.
2. A failed fetch leaves whatever `_machines` already held alone, rather than
   clearing it -- so a load that succeeds once cannot be erased by an
   unrelated refresh that later fails.
"""
from __future__ import annotations

import time
import unittest

from tests.test_frontend_browser import CHROMIUM, DRIVER_OK, DRIVER_WHY, _BrowserFixture


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class ConcurrentLoadsShareOneFetchTests(_BrowserFixture):
    """loadInitialData() (page boot) and loadBackends() (Settings opening)
    both call loadMachines(). Opening Settings immediately -- before the
    boot-time call has had a chance to resolve -- used to fire a second,
    independent /api/machines request that could finish out of order and
    stamp over the first one's result.
    """

    def setUp(self):
        self._machine_calls = 0
        super().setUp()

    def _login(self):
        def handler(route):
            self._machine_calls += 1
            if self._machine_calls == 1:
                # Delayed just enough to guarantee it is still in flight when
                # the test clicks Settings below -- on this fast, local,
                # in-process test server the boot-time call can otherwise
                # settle before Playwright's own click reaches the page,
                # which would make the two calls sequential rather than
                # concurrent and prove nothing about the dedup.
                time.sleep(0.3)
            route.continue_()

        self.page.route("**/api/machines", handler)
        page = self.page
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        page.click("#submitBtn")
        page.wait_for_selector("#settingsBtn", timeout=15_000)

    def test_opening_settings_right_after_login_makes_only_one_request(self):
        # No wait: Settings is opened as fast as this script can click it,
        # which is exactly the window the boot-time call was still pending in
        # when this was caught live.
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=8_000)
        self.assertEqual(
            self._machine_calls, 1,
            "two independent /api/machines requests were made -- the "
            "dedup in loadMachines() should have made the second caller "
            "share the first call's in-flight fetch instead",
        )
        self.assertEqual(self.errors, [])


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class FailedRefreshDoesNotEraseAPriorSuccessTests(_BrowserFixture):
    """A load that already succeeded once (the Backends tab has been opened
    and shows its card) must survive an unrelated refresh that fails --
    reopening the tab, or the boot-time call finishing late, must not be able
    to blank a list that was already correctly on screen.
    """

    def test_reopening_settings_after_a_failed_refresh_keeps_the_card(self):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=8_000)

        self.page.click("#settingsCancel")
        self.page.wait_for_timeout(300)

        def fail_once(route):
            route.fulfill(status=500, content_type="application/json", body="{}")
            self.page.unroute("**/api/machines", fail_once)

        self.page.route("**/api/machines", fail_once)
        self.page.click("#settingsBtn")
        self.page.wait_for_timeout(1_500)

        self.assertTrue(
            self.page.query_selector(".machine-card"),
            "a refresh that failed on reopening Settings erased a machine "
            "list that had already loaded successfully",
        )
        # Chromium logs the forced 500 itself as a console.error ("Failed to
        # load resource") -- that is this test's own fault injection making
        # noise, not a script error, so it is the one line filtered here.
        app_errors = [e for e in self.errors if "500 (Internal Server Error)" not in e]
        self.assertEqual(app_errors, [])


if __name__ == "__main__":
    unittest.main()
