"""QA: the gallery may not claim a spec is done on evidence it does not have.

`spec_status_v2` returned "implemented" as soon as `spec_implementation`
counted a single artifact. That count comes from keywords extracted from the
spec's *filename*, grepped across routes/, tests/ and web/assets/ — so a spec
was labelled implemented because a word from its title appeared somewhere in
the tree.

Measured on 2026-09-13, four specs whose own status lines say they are *not*
implemented:

    tiered-agent-delegation        "not implemented"            -> implemented (4)
    resource-guard                 "not yet implemented"        -> implemented (8)
    usage-statistics-billing-route "not yet implemented"        -> implemented (14)
    orchestrator-next              "draft for review"           -> implemented (3)

resource-guard scores 8 because the words "resource" and "guard" occur in
files, not because anything in that spec was built.

Two rules follow, and they are what these tests pin.

**Auto-detection may never return a terminal state.** Code existing near a
spec's keywords is evidence that work *started*, which is exactly
`implementing`. Whether it finished is a judgement no grep can make, so `done`
comes only from a human setting the manual override that routes/specs.py
already provides.

**The vocabulary is `ALLOWED_STATUSES`.** The auto path returned "implemented"
and "planned", neither of which is in the set the override endpoint validates
against — two vocabularies for one field, so a manual and an auto status could
not be compared or round-tripped.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import specs_gallery
from routes.db_specs import ALLOWED_STATUSES


class AutoStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.specs = self.root / "docs" / "superpowers" / "specs"
        self.specs.mkdir(parents=True)
        # The directories spec_implementation greps.
        for sub in ("routes", "tests", "web/assets"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _spec(self, name: str, body: str = "# spec\n") -> str:
        (self.specs / name).write_text(body)
        return f"docs/superpowers/specs/{name}"

    def test_evidence_of_code_means_implementing_not_done(self):
        """The defect. A keyword hit says work started, never that it finished."""
        path = self._spec("2026-01-01-widget-pipeline-design.md")
        (self.root / "routes" / "widget_pipeline.py").write_text("# widget-pipeline\n")
        status = specs_gallery.spec_status_v2(self.root, path)
        self.assertEqual(status, "implementing")
        self.assertNotIn(status, {"done", "implemented"})

    def test_auto_detection_never_returns_done(self):
        """`done` is a judgement about completeness. No grep can make it, so the
        auto path must not be able to emit it under any arrangement of files."""
        path = self._spec("2026-01-01-widget-pipeline-design.md")
        for extra in ("routes/widget_pipeline.py", "tests/test_widget_pipeline.py",
                      "web/assets/widget-pipeline.js"):
            p = self.root / extra
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("widget-pipeline\n")
        self.assertNotEqual(specs_gallery.spec_status_v2(self.root, path), "done")

    def test_every_auto_status_is_in_the_allowed_vocabulary(self):
        """The auto path returned "implemented"/"planned" while the override
        endpoint validated against {spec_only, planning, implementing, done}.
        One field, two vocabularies, so the two sources could not be compared."""
        cases = []
        # no evidence at all
        cases.append(self._spec("2026-01-01-untouched-topic-design.md"))
        # evidence present
        p = self._spec("2026-01-01-widget-pipeline-design.md")
        (self.root / "routes" / "widget_pipeline.py").write_text("widget-pipeline\n")
        cases.append(p)
        # a plan file alongside, no code
        plans = self.root / "docs" / "superpowers" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / "2026-01-01-lonely-topic-plan.md").write_text("# plan\n")
        cases.append(self._spec("2026-01-01-lonely-topic-design.md"))

        for path in cases:
            with self.subTest(spec=Path(path).name):
                self.assertIn(specs_gallery.spec_status_v2(self.root, path),
                              ALLOWED_STATUSES)

    def test_no_evidence_is_spec_only(self):
        path = self._spec("2026-01-01-untouched-topic-design.md")
        self.assertEqual(specs_gallery.spec_status_v2(self.root, path), "spec_only")


class RealSpecsTests(unittest.TestCase):
    """Against the actual repository, because the four misreports were real."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_a_spec_that_says_it_is_not_implemented_is_never_done(self):
        """These four declare themselves unimplemented in their own status
        lines. Whatever the gallery infers, it must not contradict them by
        claiming completion."""
        for name in ("2026-09-12-tiered-agent-delegation-design.md",
                     "2026-09-08-resource-guard-design.md",
                     "2026-09-10-usage-statistics-billing-route-design.md",
                     "2026-09-01-orchestrator-next-design.md"):
            path = f"docs/superpowers/specs/{name}"
            if not (self.ROOT / path).is_file():
                continue
            with self.subTest(spec=name):
                status = specs_gallery.spec_status_v2(self.ROOT, path)
                self.assertNotIn(status, {"done", "implemented"},
                                 f"{name} declares itself unimplemented")


if __name__ == "__main__":
    unittest.main()
