"""QA: a spec's displayed status and the group it sits in are the same value.

Reported by Pedro: specs showing "Spec only" were filed under the implementing
category. Both halves were right about their own input and wrong together --
they read different fields for the same spec:

* the row's combo box took `_autoToManualDefault[spec.status] || 'spec_only'`
* the group took `spec.status` directly

and `_autoToManualDefault` was keyed on the **old** status vocabulary
("implemented", "planned"), which the backend stopped producing when
`spec_status_v2` moved to "implementing"/"planning"/"spec_only". So every real
status missed the map, fell through the `|| 'spec_only'` fallback, and every row
displayed "Spec only" while its heading said "Implementing". Measured
2026-09-15: 26 of 26 specs, in both directions.

Two tests, because the bug had two independent halves and either alone would
bring it back:

1. Both readers go through one function, so they cannot drift apart again.
2. The JS map covers every value the Python actually returns -- the drift that
   started it. That check is cross-language on purpose: nothing else in the
   tree notices when one side's vocabulary changes, which is exactly how this
   got shipped.

Asserted from source, the convention for this file (see
test_qa_specs_load_failure_surfaced.py).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS_JS = ROOT / "web" / "assets" / "specs.js"
SPECS_GALLERY_PY = ROOT / "specs_gallery.py"


def _strip_js_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


class OneSourceOfTruthTests(unittest.TestCase):
    def setUp(self):
        self.js = _strip_js_comments(SPECS_JS.read_text(encoding="utf-8"))

    def test_the_combo_box_uses_the_effective_status(self):
        match = re.search(
            r"function _wireStatusSelect\([^)]*\)\s*\{(.*?)\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "_wireStatusSelect not found")
        self.assertIn(
            "_effectiveStatus(spec)", match.group(1),
            "the combo box must show the effective status, not compute its own",
        )

    def test_the_grouping_uses_the_effective_status(self):
        """The half that put a 'Spec only' row under 'Implementing'."""
        self.assertIn(
            "_effectiveStatus(s)", self.js,
            "grouping must use the same effective status the row displays",
        )
        self.assertNotRegex(
            self.js, r"var key = s\.status ===",
            "grouping on the raw auto status is the defect: it ignores the "
            "override and disagrees with the row beside it",
        )

    def test_the_viewer_meta_uses_the_effective_status(self):
        """The viewer dialog shows a status line a few pixels from its own
        combo box, so reading the raw value put a contradiction inside one
        dialog."""
        match = re.search(
            r"async function _openSpec\([^)]*\)\s*\{(.*?)\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "_openSpec not found")
        self.assertIn("_statusLabel(_effectiveStatus(spec))", match.group(1))


class VocabulariesAgreeTests(unittest.TestCase):
    """The drift that caused it, pinned across the language boundary."""

    def test_the_js_map_covers_every_status_python_can_return(self):
        py = SPECS_GALLERY_PY.read_text(encoding="utf-8")
        body = re.search(
            r"def spec_status_v2\([^)]*\)[^:]*:(.*?)(?=\ndef )", py, re.DOTALL)
        self.assertIsNotNone(body, "spec_status_v2 not found in specs_gallery.py")
        source = body.group(1)

        # Strip the docstring before looking for returns. Without this the
        # search matches the prose: that docstring explains the history in
        # sentences like 'It used to return "implemented"' and 'It cannot
        # return "done"', so a naive scan reports the function can return the
        # two values it specifically documents itself as NOT returning. The
        # first version of this test did exactly that and failed against a
        # correct map.
        source = re.sub(r'"""(?:.|\n)*?"""', "", source, count=1)

        returned = set(re.findall(r"^\s+return\s+\"([a-z_]+)\"", source, re.MULTILINE))
        self.assertTrue(
            returned,
            "no literal return statements found -- if spec_status_v2 stopped "
            "returning literals this check is no longer measuring anything",
        )

        js = _strip_js_comments(SPECS_JS.read_text(encoding="utf-8"))
        block = re.search(
            r"var _autoToManualDefault = \{(.*?)\};", js, re.DOTALL)
        self.assertIsNotNone(block, "_autoToManualDefault not found in specs.js")
        mapped = set(re.findall(r"(\w+)\s*:", block.group(1)))

        missing = returned - mapped
        self.assertFalse(
            missing,
            f"spec_status_v2 can return {sorted(missing)}, which _autoToManualDefault "
            "does not map. Unmapped values fall through its `|| 'spec_only'` "
            "fallback, which is how every row came to read 'Spec only' while its "
            "group heading read 'Implementing'.",
        )
