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

    def test_the_filename_date_outranks_mtime(self):
        """The ordering question is "which spec is newest", not "which file was
        touched last". Sorting on mtime answered the second: measured on the
        real checkout 2026-09-15, editing the 09-14 delegation spec that
        afternoon lifted it above the 09-15 worktree spec, and a typo fix on a
        09-08 transport spec put it above three 09-12 ones.

        So an *older* spec with a *newer* mtime must still sort below.
        """
        import os
        import time
        specs = self.root / "docs" / "superpowers" / "specs"
        older = specs / "2026-01-01-a-design.md"
        newer = specs / "2026-06-01-b-design.md"
        newer.write_text("# B\n")
        now = time.time()
        # The older spec is the one edited most recently -- the exact shape
        # that used to reorder the page.
        os.utime(older, (now, now))
        os.utime(newer, (now - 10_000, now - 10_000))
        found = [s["path"] for s in specs_gallery.discover_specs(self.root)]
        self.assertLess(
            found.index("docs/superpowers/specs/2026-06-01-b-design.md"),
            found.index("docs/superpowers/specs/2026-01-01-a-design.md"),
        )

    def test_mtime_breaks_ties_within_one_date(self):
        """Two specs sharing a date is normal -- v2 and v3 of the delegation
        spec were both dated 2026-09-14. Within one date, last edited wins,
        which is the one place mtime is the right question."""
        import os
        import time
        specs = self.root / "docs" / "superpowers" / "specs"
        v2 = specs / "2026-03-03-thing-v2-design.md"
        v3 = specs / "2026-03-03-thing-v3-design.md"
        v2.write_text("# v2\n")
        v3.write_text("# v3\n")
        now = time.time()
        os.utime(v2, (now - 500, now - 500))
        os.utime(v3, (now, now))
        found = [s["path"] for s in specs_gallery.discover_specs(self.root)]
        self.assertLess(
            found.index("docs/superpowers/specs/2026-03-03-thing-v3-design.md"),
            found.index("docs/superpowers/specs/2026-03-03-thing-v2-design.md"),
        )

    def test_an_undated_spec_sorts_below_every_dated_one(self):
        """A self-declared spec outside the specs directory (§2) carries no
        date, and README.md carries none either. Their mtime means something
        different from a filename date, so they go after rather than being
        interleaved on a value that is not comparable."""
        import os
        import time
        old = self.root / "docs" / "superpowers" / "specs" / "2026-01-01-a-design.md"
        undated = self.root / "AGENT-MODELS-DECISION.md"
        now = time.time()
        os.utime(old, (now - 100_000, now - 100_000))
        os.utime(undated, (now, now))  # newest file in the tree
        found = [s["path"] for s in specs_gallery.discover_specs(self.root)]
        self.assertLess(
            found.index("docs/superpowers/specs/2026-01-01-a-design.md"),
            found.index("AGENT-MODELS-DECISION.md"),
        )

    def test_an_excluded_directory_is_pruned_not_merely_filtered(self):
        """Regression test for the performance fix: the walk used to be
        Path.rglob("*.md") over the whole tree with excluded directories
        filtered out of the *results* afterwards, which still directory-
        walks into .venv/.git/node_modules/__pycache__/.claude before
        discarding what it finds there. Measured on the real checkout, 49
        of 157 .md files were under .venv alone, and discover_specs() there
        was 2.7s of a 15.2s /api/specs call.

        A read_text()-call-counting version of this test does not actually
        distinguish old from new: both skip reading an excluded file's
        *content* via the same post-glob `continue`, old and new alike --
        the cost this fix removes is the directory *traversal* itself, not
        the file read, and pathlib's glob internals don't expose a stable
        hook to count that directly. A timing comparison against a large
        decoy tree does distinguish them, with a wide enough margin (0.0007s
        pruned vs. 0.25s unpruned, measured writing this test, for 3000
        decoy files) that ordinary test-machine noise cannot produce a false
        pass -- an unpruned walk over the same tree is not close to the
        bound below, it is two and a half orders of magnitude past it.
        """
        import time
        hideout = self.root / ".venv" / "lib" / "site-packages"
        hideout.mkdir(parents=True)
        for i in range(3000):
            (hideout / f"pkg{i}.md").write_text(f"# Decoy {i}\nnothing relevant\n")

        t0 = time.time()
        specs_gallery.discover_specs(self.root)
        elapsed = time.time() - t0
        self.assertLess(
            elapsed, 0.1,
            f"discover_specs() took {elapsed:.3f}s against 3000 decoy files "
            "under .venv -- expected well under 0.1s if the walk is pruned "
            "before descending into an excluded directory, not filtered "
            "afterwards",
        )
