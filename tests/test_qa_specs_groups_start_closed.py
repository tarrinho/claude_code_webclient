"""QA: Settings > Specs arrives showing its categories, not their contents.

Replaces test_qa_specs_groups_start_open.py, which asserted the opposite. Both
states were requested by Pedro, ten weeks apart in effect if not in date, and
the reversal is deliberate rather than a regression -- so this file carries the
whole history, because a future reader finding "groups start closed" pinned by a
test deserves to know that "groups start open" was once pinned by one too.

2026-09-15: reported as the specs not showing at all. `loadSpecs` groups specs
by status into `<details class="spec-group">`, and `<details>` is collapsed
unless `open` is set, so every spec was folded behind its header. What made it
total was that `spec_status_v2` maps any keyword hit to "implementing" and
returned it for all 26 specs -- one group, one collapsed line, an apparently
empty panel. `_makeGroup` was changed to set `open`.

2026-09-18: requested closed, to arrive at the categories and expand the one you
want. Two things make that a different proposition from the 2026-09-15 panel,
and both are asserted below rather than taken on trust:

  * the group summary carries its own count, so a collapsed group says how many
    specs are inside it;
  * statuses are set manually now, so there is more than one group to choose
    between.

The second cannot be asserted from source -- it is a fact about data, not code
-- so it is recorded here and not tested. If every spec ever collapses back to
a single status, a single closed line is what this panel will show, and that
would be worth revisiting.

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


class SpecGroupsStartClosedTests(unittest.TestCase):
    def setUp(self):
        js = SPECS_JS.read_text(encoding="utf-8")
        match = re.search(
            r"function _makeGroup\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
        self.assertIsNotNone(match, "_makeGroup not found in specs.js")
        self.body = _strip_comments(match.group(1))

    def test_a_group_is_not_created_open(self):
        """The requested behaviour. `.open = true` is the exact text this
        replaces, so it is what the assertion names."""
        self.assertNotRegex(
            self.body, r"\.open\s*=\s*true",
            "a spec group must not be created open -- the panel is meant to "
            "arrive showing its categories, with the specs behind them",
        )

    def test_it_is_still_a_details_element(self):
        """Closed by default, still expandable. The change is the initial
        state, not the removal of the feature."""
        self.assertIn("createElement('details')", self.body)
        self.assertIn("summary", self.body)


class CollapsedGroupsStaySelfDescribingTests(unittest.TestCase):
    """What makes closed usable rather than a repeat of the 2026-09-15 bug.

    A closed group is only worth arriving at if it says what is inside it.
    These two assertions are the difference between this change and that
    regression, and they belong in a test rather than in a comment because a
    later edit could quietly remove either one.
    """

    def test_the_group_label_carries_its_count(self):
        js = _strip_comments(SPECS_JS.read_text(encoding="utf-8"))
        self.assertRegex(
            js, r"_makeGroup\(\s*_groupLabel\[key\]\s*\+\s*' · '\s*\+\s*groups\[key\]\.length",
            "a collapsed group must state how many specs it holds; without the "
            "count in the summary the panel is a row of bare headings, which is "
            "what was reported as broken on 2026-09-15",
        )

    def test_the_open_marker_rule_exists(self):
        """Unchanged from the file this replaces. The rotated marker is now
        the signal that you expanded something, which matters more when closed
        is the state you start in."""
        css = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn(
            ".spec-group[open]>summary::before", css,
            "the rotated disclosure marker for an open group is what tells a "
            "reader the group is expanded",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
