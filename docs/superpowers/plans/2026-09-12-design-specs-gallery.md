# Design Specs Gallery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only (plus admin-gated delete) Settings tab that browses every design spec in the repo — `docs/superpowers/specs/*.md` plus any file elsewhere that self-declares as a spec.

**Architecture:** A pure filesystem-scan module (`specs_gallery.py`, no database table — specs are shared, git-tracked files with no per-owner concept), a thin HTTP layer (`routes/specs.py`) mirroring `routes/images.py`'s shape, and a Settings tab (`web/assets/specs.js`) mirroring `web/assets/images.js`'s shape.

**Tech Stack:** FastAPI routes, Python stdlib (`re`, `pathlib`, `subprocess` for git), the `Markdown` package (promoted from dev-only to production), vanilla JS (no new frontend dependency).

**Spec:** `docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md`

## Global Constraints

- No new database table — filesystem scan only (spec §1).
- Inclusion rule: `docs/superpowers/specs/*.md` always counts; anywhere else, a file counts only if a line matches `^Produced via \`/brainstorming\`` (multiline-anchored, not a substring search) (spec §2).
- Sort order: file mtime, descending (spec §3/§4).
- `id` is an encoding of the file's relative path, never the raw path — every route must resolve it through the same path-containment check before touching the filesystem (spec §3).
- `DELETE` is admin-only, unlinks the file only, and must never invoke any git command (spec §3, §5).
- Markdown rendering is server-side via the `Markdown` package; malformed input falls back to escaped plain text, never a 500 (spec §3, §5).
- Every per-file enrichment (reverse-link grep, plan-status check, git provenance) is isolated — one file's enrichment failure must never fail the whole listing (spec §4, §5).
- Tests run via `.venv/bin/python -m pytest`, invoked bare (never `pytest tests/`); no writes to the production database; throwaway paths for anything filesystem-backed (spec §6).

---

### Task 1: Core scan module — discovery, `id` encoding, title extraction

**Files:**
- Create: `specs_gallery.py`
- Test: `tests/test_qa_specs_gallery_scan.py`

**Interfaces:**
- Consumes: nothing from other tasks (this is the foundation).
- Produces:
  - `discover_specs(root: Path) -> list[dict]` — each dict has keys
    `path` (str, relative to `root`), `title` (str), `mtime` (float).
  - `encode_id(relative_path: str) -> str`
  - `decode_id(encoded: str, root: Path) -> Path | None` — returns the
    resolved absolute path if it exists and stays inside `root`, else
    `None`. Later tasks (2, 4) rely on this exact signature for the
    path-containment gate.
  - `_MARKER_RE: re.Pattern` — the anchored marker regex, exported so
    Task 1's tests and any future caller can reuse the exact pattern
    rather than a second copy.

- [ ] **Step 1: Write the failing test for the marker regex**

```python
# tests/test_qa_specs_gallery_scan.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_scan.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'specs_gallery'`

- [ ] **Step 3: Write the marker regex**

```python
# specs_gallery.py
"""specs_gallery.py -- discovers, enriches, and serves this repo's design
specs for the Settings > Specs gallery.

No database table: specs are shared, git-tracked markdown files with no
per-owner concept, unlike generated_images (routes/db_images.py). See
docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Final

# Anchored to the start of a line (MULTILINE), not a bare substring search --
# auto_answer.py's own docstring states the reason for this discipline on
# every marker-based trigger in this codebase: "matching phrases against
# model-authored prose is what made [it] fire on unrelated text elsewhere in
# this tree." A file that only *mentions* brainstorming in ordinary prose
# must not count as a spec.
_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^Produced via `/brainstorming`", re.MULTILINE
)

_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"^#\s+(.+)$", re.MULTILINE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_scan.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Write failing tests for `encode_id`/`decode_id`**

```python
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
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_scan.py::IdEncodingTests -v`
Expected: FAIL with `AttributeError: module 'specs_gallery' has no attribute 'encode_id'`

- [ ] **Step 7: Implement `encode_id`/`decode_id`**

```python
def encode_id(relative_path: str) -> str:
    """URL-safe, reversible encoding of a path relative to the specs root.
    Never the raw path itself: a client-supplied id must be decoded and
    contained-checked before it ever touches the filesystem."""
    return base64.urlsafe_b64encode(relative_path.encode("utf-8")).decode("ascii")


def decode_id(encoded: str, root: Path) -> Path | None:
    """Resolve *encoded* back to an absolute path, or None if it is
    malformed, escapes *root*, or does not exist. All three read as
    "not found" to callers -- same convention db_images.py already uses,
    so a route never has to tell them apart itself."""
    try:
        relative = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    root = root.resolve()
    try:
        candidate = (root / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None
```

- [ ] **Step 8: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_scan.py::IdEncodingTests -v`
Expected: PASS (4 passed)

- [ ] **Step 9: Write failing tests for title extraction and discovery**

```python
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
```

- [ ] **Step 10: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_scan.py -v`
Expected: FAIL (`_extract_title`/`discover_specs` not defined)

- [ ] **Step 11: Implement title extraction and discovery**

```python
def _extract_title(text: str, fallback_name: str) -> str:
    m = _TITLE_RE.search(text)
    return m.group(1).strip() if m else fallback_name


def discover_specs(root: Path) -> list[dict[str, Any]]:
    """Every markdown file this repo counts as a spec, per the design's
    two-part rule: docs/superpowers/specs/*.md always, anywhere else only
    with the exact anchored marker. Re-scans every call -- no cache. The
    file count here is small (dozens), and a cached list is exactly the
    "list went stale" class of bug this project has already hit twice with
    cached status elsewhere.
    """
    root = root.resolve()
    specs_dir = root / "docs" / "superpowers" / "specs"
    found: list[dict[str, Any]] = []

    if specs_dir.is_dir():
        for path in specs_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = str(path.relative_to(root))
            found.append({
                "path": rel,
                "title": _extract_title(text, path.name),
                "mtime": path.stat().st_mtime,
            })

    for path in root.rglob("*.md"):
        if path.is_relative_to(specs_dir):
            continue  # already covered above
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _MARKER_RE.search(text):
            continue
        rel = str(path.relative_to(root))
        found.append({
            "path": rel,
            "title": _extract_title(text, path.name),
            "mtime": path.stat().st_mtime,
        })

    found.sort(key=lambda s: s["mtime"], reverse=True)
    return found
```

- [ ] **Step 12: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_scan.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 13: Commit**

```bash
git add specs_gallery.py tests/test_qa_specs_gallery_scan.py
git commit -m "feat: add specs_gallery core scan module (discovery, id encoding, titles)"
```

---

### Task 2: Enrichments — reverse-link, status badge, git provenance

**Files:**
- Modify: `specs_gallery.py`
- Test: `tests/test_qa_specs_gallery_enrich.py`

**Interfaces:**
- Consumes: `discover_specs` output shape (`{"path", "title", "mtime"}`) from Task 1.
- Produces:
  - `find_references(repo_root: Path, filename: str) -> list[str]`
  - `spec_status(repo_root: Path, spec_path: str) -> str` — `"planned"` or `"spec_only"`
  - `git_provenance(repo_root: Path, spec_path: str) -> dict[str, str] | None` — `{"author": str, "date": str}` or `None`
  - `enrich(repo_root: Path, spec: dict) -> dict` — takes one `discover_specs` entry and returns it with `referenced_by`, `status`, `author`, `date` added. Each enrichment isolated in its own `try/except Exception`, per spec §5: one enrichment's failure must not affect the others or fail the whole entry.

- [ ] **Step 1: Write failing tests for `find_references`**

```python
# tests/test_qa_specs_gallery_enrich.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::FindReferencesTests -v`
Expected: FAIL (`find_references` not defined)

- [ ] **Step 3: Implement `find_references`**

```python
def find_references(repo_root: Path, filename: str) -> list[str]:
    """Every file (relative path) that mentions *filename* -- a spec
    already gets referenced back from code today (routes/transports.py's
    own docstring names its design doc). Read-only, never raises: grep
    exiting non-zero (no matches) is a normal, empty result, not a failure.
    """
    try:
        result = subprocess.run(
            ["grep", "-rl", "--", filename, str(repo_root)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode not in (0, 1):  # 1 == no matches, still fine
        return []
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    return [str(Path(h).relative_to(repo_root)) for h in hits
            if Path(h).name != filename]
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::FindReferencesTests -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for `spec_status`**

```python
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
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::SpecStatusTests -v`
Expected: FAIL (`spec_status` not defined)

- [ ] **Step 7: Implement `spec_status`**

```python
def spec_status(repo_root: Path, spec_path: str) -> str:
    """"planned" if docs/superpowers/plans/ has a same-dated file, else
    "spec_only". Matches on the date prefix a spec's own filename carries
    (YYYY-MM-DD-<topic>-design.md); a spec with no date prefix (a
    self-declared root file) always reads as spec_only -- there is no
    reliable date to match a plan against.
    """
    plans_dir = repo_root / "docs" / "superpowers" / "plans"
    if not plans_dir.is_dir():
        return "spec_only"
    name = Path(spec_path).name
    prefix = name[:10]  # "YYYY-MM-DD"
    if len(prefix) != 10 or prefix[4] != "-" or prefix[7] != "-":
        return "spec_only"
    for plan in plans_dir.glob(f"{prefix}-*.md"):
        return "planned"
    return "spec_only"
```

- [ ] **Step 8: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::SpecStatusTests -v`
Expected: PASS

- [ ] **Step 9: Write failing tests for `git_provenance`**

```python
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
```

- [ ] **Step 10: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::GitProvenanceTests -v`
Expected: FAIL (`git_provenance` not defined)

- [ ] **Step 11: Implement `git_provenance`**

```python
def git_provenance(repo_root: Path, spec_path: str) -> dict[str, str] | None:
    """{"author", "date"} for the commit that introduced *spec_path*, or
    None if the file has no git history yet (freshly created, uncommitted)
    or git itself is unavailable. \\x1f (unit separator) as the field
    delimiter rather than a space or comma: author names can contain both.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--format=%an\x1f%as", "-1", "--", spec_path],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.strip().splitlines()[0]
    if "\x1f" not in line:
        return None
    author, date = line.split("\x1f", 1)
    return {"author": author, "date": date}
```

- [ ] **Step 12: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::GitProvenanceTests -v`
Expected: PASS

- [ ] **Step 13: Write failing tests for `enrich`'s isolation guarantee**

```python
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
```

- [ ] **Step 14: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py::EnrichIsolationTests -v`
Expected: FAIL (`enrich` not defined)

- [ ] **Step 15: Implement `enrich`**

```python
def enrich(repo_root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Adds referenced_by/status/author/date to one discover_specs() entry.
    Each enrichment is isolated: one failing must never affect the others
    or fail the whole entry (spec section 5)."""
    out = dict(spec)

    try:
        out["referenced_by"] = find_references(repo_root, Path(spec["path"]).name)
    except Exception:
        out["referenced_by"] = []

    try:
        out["status"] = spec_status(repo_root, spec["path"])
    except Exception:
        out["status"] = "spec_only"

    try:
        provenance = git_provenance(repo_root, spec["path"])
    except Exception:
        provenance = None
    out["author"] = provenance["author"] if provenance else None
    out["date"] = provenance["date"] if provenance else None

    return out
```

- [ ] **Step 16: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_enrich.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 17: Commit**

```bash
git add specs_gallery.py tests/test_qa_specs_gallery_enrich.py
git commit -m "feat: add specs_gallery enrichments (reverse-link, status, provenance)"
```

---

### Task 3: Markdown rendering

**Files:**
- Modify: `specs_gallery.py`
- Modify: `requirements.txt`
- Test: `tests/test_qa_specs_gallery_render.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks.
- Produces: `render_markdown(text: str) -> str` (returns HTML).

- [ ] **Step 1: Promote the `Markdown` dependency to production**

Check `requirements-dev.txt` for the current pinned version first:

Run: `grep -i markdown requirements-dev.txt`

Add to `requirements.txt`, matching the file's existing convention (exact
version, comment explaining why):

```
# requirements.txt -- append near the end, matching the existing style of
# one entry per line with a justifying comment.
Markdown==3.7
# Required by specs_gallery.render_markdown for the Settings > Specs
# gallery -- was dev-only until this feature needed it at runtime.
```

- [ ] **Step 2: Install it into the project's venv**

Run: `.venv/bin/pip install "Markdown==3.7"`
Expected: installs cleanly (it is likely already present from
`requirements-dev.txt`, so this may report "already satisfied")

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_qa_specs_gallery_render.py
"""QA: specs_gallery.render_markdown -- server-side rendering with a
plain-text fallback on malformed input. Design:
docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import specs_gallery


class RenderMarkdownTests(unittest.TestCase):
    def test_renders_a_heading(self):
        html = specs_gallery.render_markdown("# Title\n\nbody text\n")
        self.assertIn("<h1", html)
        self.assertIn("Title", html)

    def test_renders_a_code_block(self):
        html = specs_gallery.render_markdown("```\ncode here\n```\n")
        self.assertIn("<pre", html)

    def test_falls_back_to_escaped_text_on_render_failure(self):
        """Malformed input must never surface as a 500 -- escaped plain
        text instead, per spec section 5."""
        with patch("markdown.markdown", side_effect=Exception("boom")):
            html = specs_gallery.render_markdown("<script>evil()</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
```

- [ ] **Step 4: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_render.py -v`
Expected: FAIL (`render_markdown` not defined)

- [ ] **Step 5: Implement `render_markdown`**

```python
# Add near the top of specs_gallery.py, alongside the other imports:
import html as _html
import markdown as _markdown


def render_markdown(text: str) -> str:
    """Server-side render via the Markdown package. Falls back to escaped
    plain text on any rendering failure -- malformed input must never
    surface as a 500 (spec section 5). Specs are written by agents/humans
    working this repo, not untrusted external input, but escaping the
    fallback path costs nothing and closes the obvious XSS case regardless.
    """
    try:
        return _markdown.markdown(text, extensions=["fenced_code", "tables"])
    except Exception:
        return f"<pre>{_html.escape(text)}</pre>"
```

- [ ] **Step 6: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_gallery_render.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Commit**

```bash
git add specs_gallery.py requirements.txt tests/test_qa_specs_gallery_render.py
git commit -m "feat: add specs_gallery markdown rendering, promote Markdown to production"
```

---

### Task 4: HTTP layer — `routes/specs.py` + wiring into `app.py`

**Files:**
- Create: `routes/specs.py`
- Modify: `app.py` (add import near line 59, `app.include_router(specs_router)` near line 597)
- Test: `tests/test_qa_specs_routes.py`

**Interfaces:**
- Consumes from Tasks 1-3: `specs_gallery.discover_specs`, `specs_gallery.enrich`,
  `specs_gallery.decode_id`, `specs_gallery.render_markdown`.
- Produces: `router` (an `APIRouter`), registered routes
  `GET /api/specs`, `GET /api/specs/{spec_id}/content`, `DELETE /api/specs/{spec_id}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_qa_specs_routes.py
"""QA: routes/specs.py -- list/content/delete for the design specs gallery.
Design: docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import routes.specs as specs_routes
import specs_gallery


def _request(role="user"):
    return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": role}))


class ListRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_returns_enriched_entries(self):
        with patch.object(specs_gallery, "discover_specs", return_value=[
                {"path": "docs/superpowers/specs/x-design.md", "title": "X", "mtime": 1.0}]), \
             patch.object(specs_gallery, "enrich", side_effect=lambda root, s: {**s, "referenced_by": [], "status": "spec_only", "author": None, "date": None}):
            resp = await specs_routes.handle_specs_list(_request())
        body = resp.body
        self.assertIn(b"docs/superpowers/specs/x-design.md", body)


class ContentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_id_is_404(self):
        with patch.object(specs_gallery, "decode_id", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await specs_routes.handle_spec_content(_request(), "bogus")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_known_id_returns_rendered_html(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.md"
        path.write_text("# Hello\n")
        with patch.object(specs_gallery, "decode_id", return_value=path), \
             patch.object(specs_gallery, "render_markdown", return_value="<h1>Hello</h1>"):
            resp = await specs_routes.handle_spec_content(_request(), "whatever")
        self.assertIn(b"<h1>Hello</h1>", resp.body)


class DeleteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_gets_403(self):
        with self.assertRaises(HTTPException) as ctx:
            await specs_routes.handle_spec_delete(_request(role="user"), "whatever")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_admin_deletes_and_never_touches_git(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.md"
        path.write_text("# Hello\n")
        with patch.object(specs_gallery, "decode_id", return_value=path), \
             patch("subprocess.run") as mock_run:
            resp = await specs_routes.handle_spec_delete(_request(role="admin"), "whatever")
        mock_run.assert_not_called()
        self.assertFalse(path.exists())
        self.assertIn(b'"ok":true', resp.body.replace(b" ", b""))

    async def test_admin_delete_of_already_gone_file_is_idempotent(self):
        with patch.object(specs_gallery, "decode_id", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                await specs_routes.handle_spec_delete(_request(role="admin"), "whatever")
        self.assertEqual(ctx.exception.status_code, 404)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_routes.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'routes.specs'`)

- [ ] **Step 3: Implement `routes/specs.py`**

```python
"""Routes for /api/specs: the design-specs gallery.

List, view, and admin-gated delete for this repo's design specs
(specs_gallery.py). No database table -- specs are shared, git-tracked
files with no per-owner concept, unlike routes/images.py's generated_images.
See docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import specs_gallery

_log = logging.getLogger("wc.app")

router = APIRouter()

# The repo root specs are scanned from. Two directories up from this file
# (routes/specs.py -> routes/ -> repo root), matching how routes/db_backup.py
# and others already locate project-root paths relative to their own file.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


async def handle_specs_list(request: Request):
    """GET /api/specs -- every spec, enriched, newest (by mtime) first."""
    specs = [specs_gallery.enrich(_REPO_ROOT, s)
             for s in specs_gallery.discover_specs(_REPO_ROOT)]
    return JSONResponse({"specs": specs})


async def handle_spec_content(request: Request, spec_id: str):
    """GET /api/specs/{id}/content -- one spec's content, rendered."""
    path = specs_gallery.decode_id(spec_id, _REPO_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="Spec not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    return HTMLResponse(specs_gallery.render_markdown(text))


async def handle_spec_delete(request: Request, spec_id: str):
    """DELETE /api/specs/{id} -- admin-only. Unlinks the file. Never runs
    a git command: the removal sits as an uncommitted working-tree change
    for a human/agent to commit deliberately, same as any other edit --
    auto-committing from a UI click in this shared, multi-session tree
    would be exactly the kind of autonomous git action that has caused
    collisions this project has already documented (spec section 5)."""
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    path = specs_gallery.decode_id(spec_id, _REPO_ROOT)
    if path is None:
        raise HTTPException(status_code=404, detail="Spec not found")
    path.unlink(missing_ok=True)
    _log.info("spec_deleted user=%s path=%s", session["user"], path)
    return JSONResponse({"ok": True})


@router.get("/api/specs")
async def _api_specs_list(request: Request):
    return await handle_specs_list(request)


@router.get("/api/specs/{spec_id}/content")
async def _api_spec_content(request: Request, spec_id: str):
    return await handle_spec_content(request, spec_id)


@router.delete("/api/specs/{spec_id}")
async def _api_spec_delete(request: Request, spec_id: str):
    return await handle_spec_delete(request, spec_id)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_specs_routes.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Wire the router into `app.py`**

Modify `app.py` line 59 area (alongside the other route imports):

```python
from routes.specs import router as specs_router
```

Modify `app.py` line 597 area (alongside `app.include_router(images_router)`):

```python
app.include_router(specs_router)
```

- [ ] **Step 6: Verify the app still boots**

Run: `.venv/bin/python -c "import ast; ast.parse(open('app.py').read())"`
Expected: no output (syntax OK)

- [ ] **Step 7: Commit**

```bash
git add routes/specs.py app.py tests/test_qa_specs_routes.py
git commit -m "feat: add routes/specs.py (list/content/delete), wire into app.py"
```

---

### Task 5: Frontend — Specs tab

**Files:**
- Create: `web/assets/specs.js`
- Modify: `web/index.html` (add tab button near line 227, panel near line 358)
- Modify: `web/assets/app.js` (import near line 14, tab map near line 442-449, tab-switch dispatch near line 478)
- Modify: `web/index.html`'s existing `<script>` version query strings and `web/assets/app.js`'s import version query strings for `specs.js`, per this project's cache-busting convention (bump the shared number the same way the images tab's own addition did)

**Interfaces:**
- Consumes: `GET /api/specs`, `GET /api/specs/{id}/content`, `DELETE /api/specs/{id}` from Task 4;
  `_showConfirmDialog(title, message, onYes)` from `web/assets/machines.js` (existing).
- Produces: `loadSpecs(force: boolean)` (exported, called from `app.js`'s tab-switch dispatch, same shape as `loadImages`).

- [ ] **Step 1: Add the tab button and panel markup to `web/index.html`**

Alongside the existing tab button (near line 227):

```html
<button class="settings-tab" id="tabSpecs" role="tab" tabindex="-1" aria-selected="false" data-tab="specs" aria-controls="panelSpecs">Specs</button>
```

Alongside the existing Images panel (near line 358):

```html
<!-- Specs tab: every design spec in the repo, browsable and (admin-only)
     deletable. See docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md. -->
<div class="settings-panel" id="panelSpecs" role="tabpanel" aria-labelledby="tabSpecs" tabindex="0" hidden>
  <div class="skills-header">
    <span class="skills-count" id="specsCount" role="status" aria-live="polite"></span>
  </div>
  <div class="specs-list" id="specsList"></div>
</div>
```

- [ ] **Step 2: Write `web/assets/specs.js`**

```javascript
// specs.js — Settings > Specs: every design spec in the repo, browsable.
//
// No pagination (unlike images.js) -- a few dozen small markdown files is
// cheap to list in full every time; server-side re-scan avoids any cache
// going stale, per specs_gallery.py's own discover_specs() docstring.
import {apiFetch} from './api.js?v=2741508';
import {_showConfirmDialog} from './machines.js?v=3055851';

const byId = id => document.getElementById(id);

function _statusLabel(status) {
  return status === 'planned' ? 'Planned' : 'Spec only';
}

function _row(spec, isAdmin) {
  const row = document.createElement('div');
  row.className = 'spec-row';
  row.dataset.specId = spec.id;

  const title = document.createElement('button');
  title.type = 'button';
  title.className = 'spec-row-title';
  title.textContent = spec.title;
  title.addEventListener('click', () => _openSpec(spec));
  row.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'spec-row-meta';
  const parts = [_statusLabel(spec.status)];
  if (spec.author) parts.push(`${spec.author}${spec.date ? ` · ${spec.date}` : ''}`);
  if (spec.referenced_by && spec.referenced_by.length) {
    parts.push(`referenced by ${spec.referenced_by.length} file${spec.referenced_by.length === 1 ? '' : 's'}`);
  }
  meta.textContent = parts.join(' · ');
  row.appendChild(meta);

  if (isAdmin) {
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'spec-row-delete';
    del.textContent = '×';
    del.setAttribute('aria-label', `Delete ${spec.title}`);
    del.addEventListener('click', event => {
      event.stopPropagation();
      _showConfirmDialog(
        'Delete this spec?',
        `This removes the file from disk. It is not committed to git automatically -- delete ${spec.title}?`,
        () => _deleteSpec(spec.id, row),
      );
    });
    row.appendChild(del);
  }

  return row;
}

async function _openSpec(spec) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(spec.id)}/content`);
    if (!response.ok) return;
    const html = await response.text();
    const win = window.open('', '_blank');
    if (win) {
      win.document.title = spec.title;
      win.document.body.innerHTML = html;
    }
  } catch {
    // Silent: same fallback stance as images.js -- a failed open leaves
    // the list intact rather than surfacing a broken viewer.
  }
}

async function _deleteSpec(specId, row) {
  try {
    const response = await apiFetch(`/api/specs/${encodeURIComponent(specId)}`, {method: 'DELETE'});
    if (response.ok) {
      row.remove();
      _updateCount();
    }
  } catch {
    // The row staying put on a failed delete is the correct fallback --
    // no silent "it worked" when it did not.
  }
}

function _updateCount() {
  const countEl = byId('specsCount');
  const list = byId('specsList');
  if (!countEl || !list) return;
  const total = list.children.length;
  countEl.textContent = `${total} spec${total === 1 ? '' : 's'}`;
}

/** Load the full list. force=true (Settings tab just opened) always
 *  refetches -- the server itself never caches (specs_gallery.discover_specs
 *  re-scans every call), so a stale in-memory render is the only staleness
 *  risk left, and this closes it. */
export async function loadSpecs(force = false) {
  const list = byId('specsList');
  if (!list) return;
  if (!force && list.children.length) return;

  let payload;
  try {
    const response = await apiFetch('/api/specs');
    if (!response.ok) return;
    payload = await response.json();
  } catch {
    return;
  }

  const isAdmin = window.state?.session?.role === 'admin';
  list.replaceChildren();
  (payload.specs || []).forEach(spec => list.appendChild(_row(spec, isAdmin)));
  _updateCount();
}
```

- [ ] **Step 3: Wire the tab into `web/assets/app.js`**

Modify the import line near line 14, adding a `?v=` placeholder value for
now (any value works as a placeholder -- Step 3a below replaces it with
the real one):

```javascript
import {loadImages, _wireImagesLoadMore} from './images.js?v=8508216';
import {loadSpecs} from './specs.js?v=0';
```

Modify the tab map near line 442-446:

```javascript
  const map = {
    backends: 'panelBackends', usage: 'panelUsage', stats: 'panelStats',
    server: 'panelServer', skills: 'panelSkills', app: 'panelApp',
    images: 'panelImages', specs: 'panelSpecs',
  };
```

Modify the panel-hide list near line 448-449:

```javascript
  ['panelBackends', 'panelUsage', 'panelStats', 'panelServer', 'panelSkills',
   'panelApp', 'panelImages', 'panelSpecs'].forEach(id => {
```

Modify the tab-switch dispatch near line 478:

```javascript
  if (tab === 'images') loadImages(true);
  if (tab === 'specs') loadSpecs(true);
```

- [ ] **Step 3a: Regenerate real cache-bust versions with the project's own tool**

Do not hand-pick the `?v=` number -- this project has a dedicated script
for exactly this (used earlier the same day this plan was written) that
derives it from actual file content and rewrites every reference
consistently:

Run: `python3 bin/wc-asset-versions.py`

Then verify:

Run: `.venv/bin/python -m pytest tests/test_qa_backend_status_on_first_paint.py -k ModuleVersionsAgree -v`
Expected: PASS

- [ ] **Step 4: Run the frontend syntax test**

Run: `.venv/bin/python -m pytest tests/test_frontend_syntax.py -v`
Expected: PASS -- this test parses every `web/assets/*.js` file and would
catch a syntax error introduced by the edits above.

- [ ] **Step 5: Manual smoke check**

Restart `webconsole.service` (check for in-flight turns first, per
project convention), open Settings, click the Specs tab, confirm the list
renders, click a spec's title (opens a new tab with rendered HTML), and
-- as an admin session -- confirm the delete button prompts before
removing a row.

- [ ] **Step 6: Commit**

```bash
git add web/assets/specs.js web/index.html web/assets/app.js
git commit -m "feat: add Settings > Specs tab (web/assets/specs.js)"
```

---

## Self-Review Notes

- **Spec coverage:** §1 (no DB table) → Task 1's design. §2 (inclusion rule) →
  Task 1's `discover_specs`/`_MARKER_RE` tests. §3 (architecture, all three
  routes, id encoding, markdown promotion, confirm-before-delete) → Tasks
  1, 3, 4, 5. §3.1 (three enrichments) → Task 2. §3.2 (deferred items) →
  intentionally has no task; noted here as out of scope for this plan.
  §4 (data flow) → Task 4 ties discovery/enrichment/rendering together in
  the route handlers. §5 (error handling) → covered per-case in Tasks
  1 (containment), 2 (enrichment isolation), 3 (render fallback), 4
  (404/403/idempotent delete). §6 (testing plan) → every listed test
  case has a corresponding test in Tasks 1-4.
- **Placeholder scan:** no TBD/TODO; every step has runnable code.
- **Type consistency:** `decode_id(encoded: str, root: Path) -> Path | None`
  is defined in Task 1 and used with that exact signature in Tasks 2 and 4.
  `enrich(repo_root: Path, spec: dict) -> dict` from Task 2 is called with
  that exact signature in Task 4's `handle_specs_list`. `render_markdown`
  from Task 3 is called with that exact signature in Task 4's
  `handle_spec_content`.
