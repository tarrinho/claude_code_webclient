"""QA: specs_gallery.py's three enrichments -- reverse-link, status badge,
git provenance. Design: docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import specs_gallery


class FindReferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_finds_a_docstring_reference(self):
        (self.root / "routes.py").write_text(
            '"""See docs/superpowers/specs/2026-01-01-a-design.md."""\n')
        refs = specs_gallery.find_references(
            self.root, "2026-01-01-a-design.md")
        self.assertIn("routes.py", refs)

    def test_a_spec_with_no_references_returns_empty(self):
        (self.root / "routes.py").write_text('"""unrelated."""\n')
        refs = specs_gallery.find_references(self.root, "no-such-spec.md")
        self.assertEqual(refs, [])


class SpecStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs" / "superpowers" / "plans").mkdir(parents=True)

    def test_matching_plan_file_yields_planned(self):
        (self.root / "docs" / "superpowers" / "plans" / "2026-01-01-a.md").write_text("plan")
        status = specs_gallery.spec_status(
            self.root, "docs/superpowers/specs/2026-01-01-a-design.md")
        self.assertEqual(status, "planned")

    def test_no_matching_plan_yields_spec_only(self):
        status = specs_gallery.spec_status(
            self.root, "docs/superpowers/specs/2026-01-01-nothing-design.md")
        self.assertEqual(status, "spec_only")


class GitProvenanceTests(unittest.TestCase):
    def test_returns_none_on_subprocess_failure(self):
        """An uncommitted file (git log finds nothing) must not raise --
        it reads as no provenance, gracefully."""
        with patch("subprocess.run", side_effect=OSError("no git")):
            result = specs_gallery.git_provenance(Path("/tmp"), "whatever.md")
        self.assertIsNone(result)

    def test_returns_none_on_empty_output(self):
        class _FakeResult:
            returncode = 0
            stdout = ""
        with patch("subprocess.run", return_value=_FakeResult()):
            result = specs_gallery.git_provenance(Path("/tmp"), "whatever.md")
        self.assertIsNone(result)

    def test_parses_author_and_date_from_real_output(self):
        class _FakeResult:
            returncode = 0
            stdout = "Jane Doe\x1f2026-01-01\n"
        with patch("subprocess.run", return_value=_FakeResult()):
            result = specs_gallery.git_provenance(Path("/tmp"), "whatever.md")
        self.assertEqual(result, {"author": "Jane Doe", "date": "2026-01-01"})


class EnrichIsolationTests(unittest.TestCase):
    def test_one_failing_enrichment_does_not_block_the_others(self):
        """The load-bearing property from spec section 5: a grep timeout
        or a git failure on one file must not fail that file's whole
        entry, let alone the rest of the listing."""
        spec = {"path": "docs/superpowers/specs/x-design.md", "title": "X", "mtime": 0.0}
        with patch("specs_gallery.find_references", side_effect=Exception("boom")):
            result = specs_gallery.enrich(Path("/tmp"), spec)
        self.assertEqual(result["referenced_by"], [])
        self.assertIn("status", result)
        self.assertIn("author", result)
