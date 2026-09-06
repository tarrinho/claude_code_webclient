"""QA: the pages the app actually serves must load with a clean browser console.

Every other browser suite in this tree drives one feature and asserts what that
feature renders. None of them asserts the *absence* of errors, so a module that
throws on import, an asset the page references but the server does not serve,
or a handler that dies on a null element is invisible to all of them: the
feature under test still passes, and the console fills up where only a human
looking at devtools would ever see it.

`_BrowserFixture` already records `pageerror` and `console.error` into
`self.errors` for exactly this reason. These tests are the ones that read it.

Two classes of failure are checked, because they fail differently:

* **Script errors** (`pageerror`, `console.error`) — a module that failed to
  parse or a handler that threw. The page may look fine and simply not work.
* **Failed subresources** — a `<script src>` or `<link href>` the server
  answers 404/500 for. Chromium reports these on the network layer, and only
  *sometimes* also as a console error, so a console-only assertion misses the
  ones that fail quietly.

Both lists are asserted empty with the offending entries in the message, so a
failure names the file rather than only the count.
"""
from __future__ import annotations

import unittest

import tests.test_frontend_browser as fb


# Console noise that is expected and is not a defect. Kept deliberately short
# and specific: a broad pattern here would silence the next real error, which
# is the whole failure mode this file exists to catch. Every entry needs a
# reason, and "it was already failing" is not one.
_ALLOWED = (
    # The app polls for a live turn before one exists; the browser logs the
    # aborted EventSource as an error even though the app handles it.
    "net::ERR_ABORTED",
)


def _real(errors) -> list[str]:
    return [e for e in errors if not any(a in e for a in _ALLOWED)]


class _ConsoleFixture(fb._BrowserFixture):
    """Adds subresource-failure capture on top of the shared fixture.

    `setUp` installs the listener *after* super().setUp() has already logged
    in, so the login page's own requests are out of scope: this asserts about
    the application shell, not the auth round trip.
    """

    __test__ = False

    def setUp(self):
        super().setUp()
        self.failed_requests: list[str] = []
        self.page.on(
            "response",
            lambda r: self.failed_requests.append(f"{r.status} {r.url}")
            if r.status >= 400
            else None,
        )
        # Errors from login itself are not what these tests are about.
        self.errors.clear()

    def assert_clean(self, where: str) -> None:
        """Report both lists in one failure.

        Asserting them one after the other hid half the evidence: a page that
        both throws and 404s stopped at the script-error assertion, and the
        console message for a failed subresource ("the server responded with
        a status of 404") does not say *which* resource -- the URL only
        exists on the network record that the second assertion holds. Naming
        the failing URL is the difference between a finding and a hunt.
        """
        script_errors = _real(self.errors)
        if not script_errors and not self.failed_requests:
            return
        report = [f"{where}: the browser reported problems."]
        if script_errors:
            report.append("script errors:")
            report += [f"  {e}" for e in script_errors]
        if self.failed_requests:
            report.append("refused requests:")
            report += [f"  {r}" for r in self.failed_requests]
        self.fail("\n".join(report))


@unittest.skipUnless(fb.DRIVER_OK, fb.DRIVER_WHY)
@unittest.skipUnless(fb.CHROMIUM, "no chromium binary")
class AppShellConsoleTests(_ConsoleFixture):
    __test__ = True

    def test_the_chat_shell_loads_without_console_errors(self):
        # Already on the shell after _login. Give the deferred module work
        # (chat list fetch, settings hydrate) a chance to throw before asking.
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_timeout(2_000)
        self.assert_clean("the chat shell")

    def test_opening_settings_does_not_throw(self):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=10_000)
        self.page.wait_for_timeout(1_000)
        self.assert_clean("the settings dialog")


@unittest.skipUnless(fb.DRIVER_OK, fb.DRIVER_WHY)
@unittest.skipUnless(fb.CHROMIUM, "no chromium binary")
class OrchestratorPageConsoleTests(_ConsoleFixture):
    __test__ = True

    def test_the_orchestrator_page_loads_without_console_errors(self):
        # domcontentloaded, not networkidle: this page holds an SSE stream
        # open and polls on a timer, so the network is never idle and that
        # wait can only time out (same reason as _BrowserFixture._login).
        self.page.goto(
            f"{self.base}/orchestrator.html", wait_until="domcontentloaded"
        )
        self.page.wait_for_selector("#task-tree", timeout=15_000)
        self.page.wait_for_timeout(2_000)
        self.assert_clean("the orchestrator page")
