"""QA: Settings > Specs must not read a failed load as "no specs".

Reported by Pedro: "regarding the specs page, I don't see any file / spec".
specs_gallery.discover_specs()/enrich() find and render all 22 specs
correctly when called directly -- the backend is not the defect. The
frontend was: `loadSpecs()` in specs.js treated any non-200 response
(a 401 "Session expired" reproduces this: hitting /api/specs directly with
no session returns exactly that) or a thrown fetch error identically to a
genuinely empty gallery -- `if (!response.ok) return;` and a bare `catch {
return; }`, both leaving `specsList` untouched. There was no way to tell
"the request failed" from "there are truly no specs" from the UI alone.

Asserted from source, this repo's convention for files with no JS runner
(see test_qa_backend_status_on_first_paint.py's docstring) -- specs.js is
not one of the quickjs-covered files (tests/js/ serves supervisor-map.js
only).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS_JS = ROOT / "web" / "assets" / "specs.js"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


class LoadSpecsFailureIsSurfacedTests(unittest.TestCase):
    def setUp(self):
        js = SPECS_JS.read_text(encoding="utf-8")
        match = re.search(
            r"export async function loadSpecs\([^)]*\)\s*\{(.*)\n\}\s*$",
            js, re.DOTALL,
        )
        self.assertIsNotNone(match, "loadSpecs not found in specs.js")
        self.body = _strip_comments(match.group(1))

    def test_a_failed_fetch_calls_notifyResult(self):
        """The regression: a non-ok response used to just `return`, with
        nothing telling the user their request failed rather than the
        gallery being empty."""
        self.assertIn(
            "notifyResult(", self.body,
            "loadSpecs must surface a failed load through notifyResult, "
            "the same convention _deleteSpec already follows in this file",
        )

    def test_the_list_is_not_silently_left_empty_on_failure(self):
        """Guards against a fix that calls notifyResult but is placed after
        an early return, which would never execute -- must not regress
        to two paths that both silently no-op."""
        self.assertNotRegex(
            self.body, r"if\s*\(!response\.ok\)\s*\{?\s*return;\s*\}?\s*\n",
            "a bare early return on failure is exactly the silent-empty bug "
            "being fixed",
        )

    def test_the_bare_catch_no_longer_swallows_the_error_silently(self):
        """The second half of the same bug: a thrown fetch error (network
        failure, non-JSON body) took the `catch { return; }` path, equally
        silent."""
        self.assertNotRegex(
            self.body, r"catch\s*\{\s*return;\s*\}",
            "the catch block must not discard the error with no feedback",
        )
