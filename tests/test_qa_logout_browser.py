"""Clicking Log out must actually end the session on the server.

This file exists because four server-side logout tests passed while the button
did nothing. `test_logout_returns_ok`, `test_logout_clears_session` and
`test_logout_cleans_cookie` in tests/test_qa_coverage.py, and
`test_logout_removes_it_from_disk` in tests/test_qa_session_durability.py, all
call the endpoint directly -- so all four exercised a handler that the browser
could never reach.

`web/assets/app.js` sent `POST /logout` with a bare `fetch`, which attaches no
`X-CSRF-Token`. `/logout` is not in `CsrfMiddleware._EXEMPT_PATHS` (only
`/login` is), so the request was answered 403 before `handle_logout` ran:
`auth.session_drop` never fired and `clear_session_cookie` never fired, leaving
the session live and the cookie set while the page navigated to /login and told
the user they were logged out. `fetch` does not reject on 403, so the
`console.error` in that call's own catch block -- written for exactly this
failure -- could not see it either.

The assertion therefore cannot be about the button, the request, or any state
the page sets optimistically. It has to be: click, then ask the server whether
the session still works. That is the only shape that fails when the request
never lands.
"""
from __future__ import annotations

import unittest

from tests.test_frontend_browser import DRIVER_OK, DRIVER_WHY, _BrowserFixture


@unittest.skipUnless(DRIVER_OK, DRIVER_WHY)
class LogoutEndsSessionBrowserTests(_BrowserFixture):

    def _session_cookie(self) -> str | None:
        for cookie in self.page.context.cookies():
            if cookie["name"] == "wc_session":
                return cookie["value"]
        return None

    def test_clicking_log_out_ends_the_session_on_the_server(self):
        """The load-bearing one. After the click, the cookie the browser held
        before it must no longer authenticate -- asked of the server, not of
        the page."""
        before = self._session_cookie()
        self.assertIsNotNone(before, "fixture should be logged in")

        self.page.click("#logoutBtn")
        self.page.wait_for_url("**/login", timeout=10_000)

        # Ask the server directly, carrying the pre-logout cookie value. A
        # fetch from the page would use whatever cookies survive; this pins the
        # exact credential that was live before the click, so a server that
        # never dropped the session answers 200 and fails this test.
        status = self.page.evaluate(
            """async ([base, sid]) => {
                document.cookie = 'wc_session=' + sid + '; path=/';
                const r = await fetch(base + '/api/chats', {
                    credentials: 'same-origin',
                    redirect: 'manual',
                });
                return r.status;
            }""",
            [self.base, before],
        )
        self.assertIn(
            status, (401, 303, 0),
            f"the pre-logout session still authenticates (status {status}); "
            "handle_logout did not run",
        )

    def test_the_logout_request_is_not_refused_for_a_missing_csrf_token(self):
        """Names the specific regression. A bare `fetch` here is answered 403,
        and because fetch does not reject on 403 nothing in the page notices --
        so this watches the response status rather than any page state."""
        statuses: list[int] = []
        self.page.on(
            "response",
            lambda r: statuses.append(r.status)
            if r.url.endswith("/logout") else None,
        )

        self.page.click("#logoutBtn")
        self.page.wait_for_url("**/login", timeout=10_000)

        self.assertTrue(statuses, "no response to POST /logout was observed")
        self.assertNotIn(
            403, statuses,
            "POST /logout was refused for a missing CSRF token -- app.js must "
            "use apiFetch, which attaches X-CSRF-Token, not a bare fetch",
        )

    def test_the_session_cookie_is_cleared_in_the_browser(self):
        """`clear_session_cookie` runs on handle_logout's response, so it is
        skipped by the same 403. Separate from the first test on purpose: the
        cookie going away is what the user can see, and the session dying is
        what protects them. Either can regress without the other."""
        self.assertIsNotNone(self._session_cookie())

        self.page.click("#logoutBtn")
        self.page.wait_for_url("**/login", timeout=10_000)

        self.assertIsNone(
            self._session_cookie(),
            "wc_session survived logout; clear_session_cookie did not run",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
