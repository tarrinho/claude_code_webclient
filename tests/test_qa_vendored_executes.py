"""The pinned bundles must still execute in a real browser.

A Subresource Integrity mismatch does not warn, retry, or degrade: the browser
refuses the script and the page carries on without it. For `purify.min.js`
that is worse than a missing feature -- `specs.js` calls
`window.DOMPurify.sanitize(html)` before an innerHTML assignment, so a blocked
sanitiser removes the only client-side guard on that sink while the page keeps
rendering.

The hashes themselves are checked without a browser in
tests/test_qa_vendored_integrity.py. This file answers the different question
that file cannot: whether a browser, given the attributes we actually shipped,
runs the code. A correct hash written into a tag the browser rejects for some
other reason -- a `crossorigin` mode the server will not satisfy, say -- passes
there and fails here.

Spec: docs/superpowers/specs/2026-09-25-high-assurance-development-design.md
      section 4.6, rollout step 3
"""
from __future__ import annotations

import unittest

import tests.test_frontend_browser as fb


@unittest.skipUnless(fb.DRIVER_OK, f"playwright driver unusable ({fb.DRIVER_WHY})")
@unittest.skipIf(fb.CHROMIUM is None, "no Chromium binary on PATH")
class VendoredExecutesTests(fb._BrowserFixture):

    def _load(self):
        self._login()
        self.page.goto(self.base, timeout=15_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#chatListDesktop", timeout=20_000)

    def test_the_sanitiser_is_present_on_the_page(self):
        """specs.js depends on this global existing before it sanitises."""
        self._load()
        self.assertEqual(
            self.page.evaluate("typeof window.DOMPurify?.sanitize"), "function",
            "DOMPurify did not execute -- if the integrity attribute and the "
            "bundle disagree, the browser drops the script silently")

    def test_the_chart_library_is_present_on_the_page(self):
        """supervisor-map.js draws the radial tree with it."""
        self._load()
        self.assertEqual(
            self.page.evaluate("typeof window.d3"), "object",
            "d3 did not execute -- see the note above; same failure mode")


if __name__ == "__main__":
    unittest.main()
