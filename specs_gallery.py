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
