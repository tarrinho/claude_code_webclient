"""A real violation in a real browser must reach the sink.

The unit tests either side of this one each prove half of something:
tests/test_qa_security_headers.py proves the policy names the endpoint, and
tests/test_qa_csp_report.py proves the endpoint handles what arrives. Both pass
if the browser never sends anything -- a `report-uri` in a policy the browser
rejects for an unrelated syntax error, say, or an endpoint the page cannot
reach.

So this provokes an actual violation and asserts the browser posts it. It is
the "verified by injecting one" clause of the spec's own success criteria,
which exists because every false green in section 1 of that document also
looked like an absence of findings.

Spec: docs/superpowers/specs/2026-09-25-high-assurance-development-design.md
      section 4.4 and section 11, step 1
"""
from __future__ import annotations

import unittest

import tests.test_frontend_browser as fb


@unittest.skipUnless(fb.DRIVER_OK, f"playwright driver unusable ({fb.DRIVER_WHY})")
@unittest.skipIf(fb.CHROMIUM is None, "no Chromium binary on PATH")
class CspReportWiringTests(fb._BrowserFixture):

    def test_a_blocked_script_is_reported_to_the_sink(self):
        posted: list[str] = []
        self.page.on(
            "request",
            lambda req: posted.append(req.url)
            if req.method == "POST" and "/api/csp-report" in req.url else None)

        self._login()
        self.page.goto(self.base, timeout=15_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#chatListDesktop", timeout=20_000)

        # script-src 'self' must refuse this. Appending the element is enough:
        # the violation fires on the load attempt, and nothing needs the script
        # to exist at the far end.
        self.page.evaluate(
            "const s = document.createElement('script');"
            "s.src = 'https://blocked.invalid/x.js';"
            "document.body.appendChild(s);")
        # The report is posted out of band, so give the browser a moment. A
        # fixed wait is acceptable here only because the assertion is about
        # something arriving, not about how fast it arrives.
        self.page.wait_for_timeout(2_000)

        self.assertTrue(
            posted,
            "the browser blocked the script but posted no report -- the "
            "policy and the sink are not actually connected")


if __name__ == "__main__":
    unittest.main()
