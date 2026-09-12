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

    def test_a_hit_inside_an_excluded_dir_does_not_count(self):
        """Regression test for C3/I3: the grep used to walk the entire repo
        tree, including .git/.venv/__pycache__/node_modules/.claude -- on
        the real checkout that took over 10s per call and hit its own
        timeout, silently returning [], indistinguishable from "genuinely no
        references". A file whose *only* mention sits inside an excluded
        directory must not produce a hit -- if it did, --exclude-dir was
        not actually applied."""
        for excluded_dir in ("__pycache__", ".git"):
            hideout = self.root / excluded_dir
            hideout.mkdir()
            (hideout / "stale.py").write_text(
                '"""See docs/superpowers/specs/2026-01-01-a-design.md."""\n')
        refs = specs_gallery.find_references(self.root, "2026-01-01-a-design.md")
        self.assertEqual(refs, [])


class FindAllReferencesTests(unittest.TestCase):
    """find_all_references() must agree with calling find_references() once
    per name -- it exists to make the same answer cheaper to compute
    (measured 2026-09-12: 15.2s for 23 specs down to 1.5s), not a different
    answer. See its docstring for why (grep -H across every hit file in one
    process, rather than one grep -l per spec)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_finds_references_for_multiple_names_in_one_call(self):
        (self.root / "routes.py").write_text(
            '"""See docs/superpowers/specs/2026-01-01-a-design.md."""\n')
        (self.root / "other.py").write_text(
            '"""See docs/superpowers/specs/2026-01-02-b-design.md."""\n')
        refs = specs_gallery.find_all_references(
            self.root, ["2026-01-01-a-design.md", "2026-01-02-b-design.md"])
        self.assertIn("routes.py", refs["2026-01-01-a-design.md"])
        self.assertIn("other.py", refs["2026-01-02-b-design.md"])
        # Neither file mentions the other spec.
        self.assertNotIn("other.py", refs["2026-01-01-a-design.md"])
        self.assertNotIn("routes.py", refs["2026-01-02-b-design.md"])

    def test_one_file_referencing_two_specs_is_attributed_to_both(self):
        (self.root / "routes.py").write_text(
            "# See 2026-01-01-a-design.md and 2026-01-02-b-design.md\n")
        refs = specs_gallery.find_all_references(
            self.root, ["2026-01-01-a-design.md", "2026-01-02-b-design.md"])
        self.assertIn("routes.py", refs["2026-01-01-a-design.md"])
        self.assertIn("routes.py", refs["2026-01-02-b-design.md"])

    def test_a_name_with_no_references_maps_to_an_empty_list(self):
        (self.root / "routes.py").write_text('"""unrelated."""\n')
        refs = specs_gallery.find_all_references(self.root, ["no-such-spec.md"])
        self.assertEqual(refs["no-such-spec.md"], [])

    def test_self_reference_is_excluded(self):
        """Same exclusion find_references() applies: a spec file
        mentioning its own filename (e.g. in its own frontmatter) is not a
        reference to itself."""
        (self.root / "2026-01-01-a-design.md").write_text(
            "# A\nSee also 2026-01-01-a-design.md for background.\n")
        refs = specs_gallery.find_all_references(
            self.root, ["2026-01-01-a-design.md"])
        self.assertNotIn("2026-01-01-a-design.md", refs["2026-01-01-a-design.md"])

    def test_an_excluded_directory_does_not_count(self):
        hideout = self.root / ".venv"
        hideout.mkdir()
        (hideout / "stale.py").write_text(
            '"""See 2026-01-01-a-design.md."""\n')
        refs = specs_gallery.find_all_references(self.root, ["2026-01-01-a-design.md"])
        self.assertEqual(refs["2026-01-01-a-design.md"], [])

    def test_empty_filename_list_returns_empty_dict(self):
        self.assertEqual(specs_gallery.find_all_references(self.root, []), {})

    def test_agrees_with_calling_find_references_once_per_name(self):
        """The property that actually matters: same input, same answer as
        the slower one-call-per-name version, for a case with real overlap
        and real gaps."""
        (self.root / "a.py").write_text("2026-01-01-a-design.md\n")
        (self.root / "b.py").write_text(
            "2026-01-01-a-design.md and 2026-01-02-b-design.md\n")
        (self.root / "c.py").write_text("nothing relevant here\n")
        names = ["2026-01-01-a-design.md", "2026-01-02-b-design.md", "2026-01-03-c-design.md"]
        bulk = specs_gallery.find_all_references(self.root, names)
        individually = {
            name: specs_gallery.find_references(self.root, name) for name in names
        }
        self.assertEqual(
            {k: sorted(v) for k, v in bulk.items()},
            {k: sorted(v) for k, v in individually.items()},
        )


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
