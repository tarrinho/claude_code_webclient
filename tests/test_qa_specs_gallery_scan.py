"""QA: specs_gallery.py -- discovery, id encoding, title extraction.

Design: docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import specs_gallery


class MarkerRegexTests(unittest.TestCase):
    def test_matches_the_exact_anchored_line(self):
        text = "Produced via `/brainstorming`, 2026-09-12. More text.\n"
        self.assertIsNotNone(specs_gallery._MARKER_RE.search(text))

    def test_does_not_match_a_mid_sentence_mention(self):
        """The auto_answer.py lesson: matching phrases inside ordinary prose
        fires on unrelated text. A file that merely *mentions* brainstorming
        must not qualify."""
        text = "We talked about it during a brainstorming session once.\n"
        self.assertIsNone(specs_gallery._MARKER_RE.search(text))

    def test_matches_when_not_the_first_line(self):
        text = "# Some Title\n\nProduced via `/brainstorming`, today.\n"
        self.assertIsNotNone(specs_gallery._MARKER_RE.search(text))


class IdEncodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "real-spec.md").write_text("# Real\n")

    def test_round_trips_a_relative_path(self):
        encoded = specs_gallery.encode_id("docs/real-spec.md")
        decoded = specs_gallery.decode_id(encoded, self.root)
        self.assertEqual(decoded, self.root / "docs" / "real-spec.md")

    def test_a_path_escaping_root_is_rejected(self):
        """A manipulated id must never resolve outside root -- the same
        containment discipline routes/images.py's handle_image_file already
        applies to a client-supplied value."""
        encoded = specs_gallery.encode_id("../../../etc/passwd")
        self.assertIsNone(specs_gallery.decode_id(encoded, self.root))

    def test_a_nonexistent_but_contained_path_is_rejected(self):
        """Contained does not mean present -- a since-deleted file must read
        as not-found, not as a resolvable-but-missing path."""
        encoded = specs_gallery.encode_id("docs/gone.md")
        self.assertIsNone(specs_gallery.decode_id(encoded, self.root))

    def test_garbage_input_does_not_raise(self):
        self.assertIsNone(specs_gallery.decode_id("not-valid-base64!!", self.root))


class TitleExtractionTests(unittest.TestCase):
    def test_uses_the_first_h1(self):
        text = "Some preamble\n# The Real Title\nmore text\n"
        self.assertEqual(specs_gallery._extract_title(text, "fallback.md"), "The Real Title")

    def test_falls_back_to_filename_when_no_h1(self):
        text = "no heading here at all\n"
        self.assertEqual(specs_gallery._extract_title(text, "my-spec.md"), "my-spec.md")


class DiscoverSpecsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs" / "superpowers" / "specs").mkdir(parents=True)
        (self.root / "docs" / "superpowers" / "specs" / "2026-01-01-a-design.md").write_text(
            "# Spec A\nbody\n")
        (self.root / "AGENT-MODELS-DECISION.md").write_text(
            "# Agent Models\nProduced via `/brainstorming`, today.\n")
        (self.root / "README.md").write_text("# Read Me\nJust a readme.\n")

    def test_specs_dir_files_always_included(self):
        found = {s["path"] for s in specs_gallery.discover_specs(self.root)}
        self.assertIn("docs/superpowers/specs/2026-01-01-a-design.md", found)

    def test_marker_bearing_root_file_included(self):
        found = {s["path"] for s in specs_gallery.discover_specs(self.root)}
        self.assertIn("AGENT-MODELS-DECISION.md", found)

    def test_marker_free_root_file_excluded(self):
        found = {s["path"] for s in specs_gallery.discover_specs(self.root)}
        self.assertNotIn("README.md", found)

    def test_marker_bearing_file_inside_excluded_dir_is_not_a_spec(self):
        """I3: the same exclusion list C3 added to find_references's grep
        also has to cover this rglob walk, or a marker-bearing file sitting
        inside .git/.venv/__pycache__/node_modules/.claude (which on this
        checkout holds *other sessions'* worktrees) would surface as a spec
        of this repo."""
        hideout = self.root / ".claude" / "worktrees" / "other-session"
        hideout.mkdir(parents=True)
        (hideout / "SOMETHING.md").write_text(
            "# Other\nProduced via `/brainstorming`, today.\n")
        found = {s["path"] for s in specs_gallery.discover_specs(self.root)}
        self.assertFalse(any(".claude" in p for p in found))

    def test_sorted_by_mtime_descending(self):
        import os
        import time
        old = self.root / "docs" / "superpowers" / "specs" / "2026-01-01-a-design.md"
        new = self.root / "AGENT-MODELS-DECISION.md"
        now = time.time()
        os.utime(old, (now - 100, now - 100))
        os.utime(new, (now, now))
        found = [s["path"] for s in specs_gallery.discover_specs(self.root)]
        self.assertLess(found.index("AGENT-MODELS-DECISION.md"),
                         found.index("docs/superpowers/specs/2026-01-01-a-design.md"))
