"""QA: a failed transport Check stays on screen.

Reported on 2026-09-25 in the operator's own words -- a Check on Kali3 came
back with an error and the console showed nothing. The application log for
that minute holds `transport_check name=Kali3 ready=False reachable=True`:
a real failing verdict, computed and returned, that nobody ever saw.

The readiness box lived only on the header element `_checkTransport` appended
it to, and `_refreshTunnelStatus` rebuilds every header on a 5s timer via
`_renderMachineList`. So the lines explaining the failure survived for at most
one poll interval and then vanished, leaving only a toast that had already
faded.

An earlier fix stopped the broken-badge dispatch from rebuilding the list in
the same synchronous call stack. That was real, and it covered only the
immediate path; the periodic rebuild went on discarding the box seconds later.

This drives the real page: a transport whose Check genuinely fails, then waits
out several poll ticks and asserts the explanation is still there. A unit test
over the renderer could not catch this -- the bug was never in rendering, it
was in what happened to the DOM afterwards.
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path

from tests.test_frontend_browser import (CHROMIUM, DRIVER_OK, DRIVER_WHY,
                                         _BrowserFixture)

#: Longer than two ticks of the 5s tunnel-status poll. The old code lost the
#: box on the first one, so any wait at or under 5s could pass against the bug.
POLL_TICKS_S = 12

#: Loopback with a key path that does not exist. The failure is then local,
#: immediate and deterministic: no DNS, no packet leaves the machine, and no
#: dependence on whether anything is listening on port 22. It has to be written
#: straight into the database because POST /api/transports resolves the host
#: and rejects private addresses -- which is correct, and is why a transport
#: guaranteed to fail cannot be created through the API.
BAD_HOST = "127.0.0.1"
BAD_KEY = "/nonexistent/wc-qa-key"

TRANSPORT_ID = "qa" + "0" * 30
TRANSPORT_NAME = "QA Unreachable"


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class CheckFailureStaysVisibleTests(_BrowserFixture):

    def setUp(self):
        super().setUp()  # boots the browser and logs in
        db_path = Path(self.tmp.name) / "wc.db"
        con = sqlite3.connect(str(db_path))
        try:
            # Whoever the login just authenticated as. ssh_transport_get
            # filters by owner, so a guessed id yields a 404 and a test that
            # proves nothing about rendering.
            row = con.execute("SELECT id FROM users LIMIT 1").fetchone()
            self.assertIsNotNone(row, "no user row to own the transport")
            # OR IGNORE: the fixture builds one server and one database per
            # class, so the second case in this file finds the row the first
            # one wrote.
            con.execute(
                "INSERT OR IGNORE INTO ssh_transports (id, name, owner_id, ssh_host,"
                " ssh_user, ssh_key_path, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (TRANSPORT_ID, TRANSPORT_NAME, row[0], BAD_HOST, "nobody",
                 BAD_KEY, "2026-09-26T00:00:00Z", "2026-09-26T00:00:00Z"))
            con.commit()
        finally:
            con.close()

    def _open_backends(self):
        """Settings -> Backends, with the transport's own header on screen."""
        # Verified click, retried. #settingsBtn is static markup in
        # index.html, so it is present and clickable before app.js has wired
        # its listener -- a click that lands in that window is swallowed and
        # the dialog never opens at all, for the rest of the test. Cost one
        # spurious failure here already. Clicking until the dialog actually
        # carries `.open` is the only way to know the click took.
        for attempt in range(5):
            self.page.click("#settingsBtn")
            try:
                self.page.wait_for_selector("#settingsDialog.open", timeout=3_000)
                break
            except Exception:
                if attempt == 4:
                    self.fail("the settings dialog never opened after 5 clicks")

        header = self.page.locator(
            f'.transport-group-header[data-transport-id="{TRANSPORT_ID}"]')
        try:
            header.wait_for(state="visible", timeout=45_000)
        except Exception as exc:  # pragma: no cover - diagnostic path
            # "never appeared" and "appeared late" are different faults with
            # different fixes, and the bare Playwright timeout distinguishes
            # neither. Say what the panel did render and what the server said.
            rendered = self.page.locator(".transport-group-header").all_inner_texts()
            self.fail(
                f"the transport header never appeared: {exc}\n"
                f"headers on screen: {rendered}\n"
                f"page errors: {self.errors}\n"
                f"server log:\n{self._server_log()}")
        return header

    def _click_check(self):
        """Click Check until a verdict actually lands.

        The same 5s rebuild this file is about also replaces the button: a
        click that lands while `_renderMachineList` is swapping the header out
        hits a node on its way to being detached and does nothing, and the
        test then waits out its whole timeout on a Check that was never
        requested. Re-locating the button each attempt is the point -- holding
        a stale Locator would keep clicking the same dead node.
        """
        for attempt in range(4):
            self.page.locator(
                f'.transport-group-header[data-transport-id="{TRANSPORT_ID}"]'
                f' button[aria-label="Check transport {TRANSPORT_NAME}"]'
            ).first.click()
            try:
                self.page.wait_for_selector(
                    f'.transport-group-header[data-transport-id="{TRANSPORT_ID}"]'
                    ' .transport-readiness', timeout=20_000)
                return
            except Exception:
                if attempt == 3:
                    self.fail(
                        "no readiness verdict appeared after 4 Check clicks; "
                        f"page errors: {self.errors}\n"
                        f"server log:\n{self._server_log()}")

    def test_the_failure_survives_several_poll_rebuilds(self):
        """The reported bug, restated as the property that replaces it."""
        self._open_backends()
        self._click_check()

        box = self.page.locator(
            f'.transport-group-header[data-transport-id="{TRANSPORT_ID}"]'
            ' .transport-readiness')
        first_text = box.first.inner_text().strip()
        self.assertTrue(first_text, "the readiness box rendered empty")

        # ...and is still there after the tunnel-status poll has rebuilt the
        # list more than once. This is the assertion the old code failed.
        time.sleep(POLL_TICKS_S)
        self.assertGreater(
            box.count(), 0,
            f"the readiness box disappeared within {POLL_TICKS_S}s -- the "
            "tunnel-status poll rebuilt the header and discarded the only "
            "explanation the operator had")
        self.assertTrue(
            box.first.inner_text().strip(),
            "the box survived the rebuild but lost its contents")

    def test_the_surviving_box_says_what_went_wrong(self):
        """Present is not the same as useful.

        A box that survives every rebuild and reads `✗ claude CLI:` with
        nothing after the colon is the second half of the same complaint. The
        assertion is on every rendered line, so it holds for the error line an
        unreachable host produces and for the per-check lines a reachable one
        does.
        """
        self._open_backends()
        self._click_check()
        lines = self.page.locator(
            f'.transport-group-header[data-transport-id="{TRANSPORT_ID}"]'
            ' .transport-check')
        self.assertGreater(lines.count(), 0, "a readiness box with no lines")

        for i in range(lines.count()):
            text = lines.nth(i).inner_text().strip()
            with self.subTest(line=text[:50]):
                self.assertTrue(text, "an empty check line")
                # Lines read "<mark> <name>: <detail>" -- a trailing colon
                # means the detail came out blank.
                self.assertFalse(
                    text.endswith(":"),
                    f"check line carries no detail: {text!r}")


if __name__ == "__main__":
    unittest.main()
