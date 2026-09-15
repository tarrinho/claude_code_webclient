"""QA: Settings > Specs shows its specs, rather than a list of group headings.

Reported by Pedro as the specs not showing in Settings > Specs. Every spec was
in the DOM: `loadSpecs` groups them by status into `<details class="spec-group">`
elements, and `<details>` is collapsed unless `open` is set. `_makeGroup` never
set it, so the panel rendered as group headers with the specs folded behind
them.

Two things made it total rather than partial:

* `spec_status_v2` maps any keyword hit to "implementing" and, measured
  2026-09-15, returned it for all 26 specs. So there was exactly one group, and
  the panel was one collapsed line.
* The stylesheet already carried `.spec-group[open]>summary::before` to rotate
  the disclosure marker -- a rule that could never fire, because nothing ever
  set the attribute it keys on. The design expected open groups; only the JS
  disagreed.

Same shape as registry #101: two individually reasonable features -- grouping,
and groups that can collapse -- combining into a panel with nothing in it.

Asserted from source, this repo's convention for files with no JS runner (see
test_qa_specs_load_failure_surfaced.py, and test_qa_backend_status_on_first_paint.py's
docstring for why).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS_JS = ROOT / "web" / "assets" / "specs.js"
STYLES_CSS = ROOT / "web" / "assets" / "styles.css"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


class SpecGroupsStartOpenTests(unittest.TestCase):
    def setUp(self):
        js = SPECS_JS.read_text(encoding="utf-8")
        match = re.search(
            r"function _makeGroup\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
        self.assertIsNotNone(match, "_makeGroup not found in specs.js")
        self.body = _strip_comments(match.group(1))

    def test_a_group_is_created_open(self):
        """The regression: without this the gallery renders headings only."""
        self.assertRegex(
            self.body, r"\.open\s*=\s*true",
            "a spec group must be created open -- <details> is collapsed by "
            "default, so every spec ends up folded behind its group header and "
            "the panel reads as empty",
        )

    def test_it_is_still_a_details_element(self):
        """Open by default, still collapsible. The fix is the initial state,
        not the removal of the feature."""
        self.assertIn("createElement('details')", self.body)
        self.assertIn("summary", self.body)


class StylesheetAgreesTests(unittest.TestCase):
    """The CSS was already written for open groups. This pins the pair together
    so a later change cannot leave the rule orphaned again."""

    def test_the_open_marker_rule_exists(self):
        css = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn(
            ".spec-group[open]>summary::before", css,
            "the rotated disclosure marker for an open group is what tells a "
            "reader the group is expanded; it existed before the JS ever "
            "opened one",
        )
