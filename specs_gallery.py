"""specs_gallery.py -- discovers, enriches, and serves this repo's design
specs for the Settings > Specs gallery.

No database table: specs are shared, git-tracked markdown files with no
per-owner concept, unlike generated_images (routes/db_images.py). See
docs/superpowers/specs/2026-09-12-design-specs-gallery-design.md.
"""
from __future__ import annotations

import base64
import html as _html
import re
import subprocess
from pathlib import Path
from typing import Any, Final

import markdown as _markdown
import nh3

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

# Shared by discover_specs()'s tree walk and find_references()'s grep: both
# would otherwise wander into .git/.venv/__pycache__/node_modules, and into
# .claude, which on this checkout holds *other sessions'* worktrees. Walking
# those made discover_specs slow and made find_references's grep take over
# 10s and hit its own timeout (returning [] -- indistinguishable from a
# genuine "no references"). One list, defined once, used by both.
_EXCLUDED_DIRS: Final[frozenset[str]] = frozenset({
    ".git", ".venv", "__pycache__", "node_modules", ".claude",
})

# What a design spec actually needs to render: headings, paragraphs, lists,
# code/pre, tables, emphasis, links, blockquotes. Deliberately narrower than
# nh3's own defaults (which include img/div/span/nav/... none of which a spec
# document needs) -- no script/style/iframe/form/object/embed, and nothing
# else either.
_ALLOWED_TAGS: Final[frozenset[str]] = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "ul", "ol", "li",
    "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td",
    "b", "i", "strong", "em",
    "a", "blockquote",
})
# nh3 strips event-handler attributes (onerror, onclick, ...) and non-listed
# schemes (javascript:) by default -- only href needs to be named explicitly
# here to survive at all.
_ALLOWED_ATTRIBUTES: Final[dict[str, set[str]]] = {"a": {"href"}}


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
        rel_parts = path.relative_to(root).parts
        if any(part in _EXCLUDED_DIRS for part in rel_parts):
            continue
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


def find_references(repo_root: Path, filename: str) -> list[str]:
    """Every file (relative path) that mentions *filename* -- a spec
    already gets referenced back from code today (routes/transports.py's
    own docstring names its design doc). Read-only, never raises: grep
    exiting non-zero (no matches) is a normal, empty result, not a failure.
    """
    cmd = ["grep", "-rl"]
    for excluded in _EXCLUDED_DIRS:
        cmd.append(f"--exclude-dir={excluded}")
    cmd += ["--", filename, str(repo_root)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode not in (0, 1):  # 1 == no matches, still fine
        return []
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    return [str(Path(h).relative_to(repo_root)) for h in hits
            if Path(h).name != filename]


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


def enrich(repo_root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Adds referenced_by/status/author/date to one discover_specs() entry.
    Each enrichment is isolated: one failing must never affect the others
    or fail the whole entry (spec section 5)."""
    out = dict(spec)

    # The id a client needs to view/delete this spec through the API --
    # encode_id() existed from Task 1 but nothing on the list path ever
    # called it, so every entry served to the gallery carried no id at all
    # and view/delete were both silently non-functional.
    out["id"] = encode_id(spec["path"])

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


def render_markdown(text: str) -> str:
    """Server-side render via the Markdown package. Falls back to escaped
    plain text on any rendering failure -- malformed input must never
    surface as a 500 (spec section 5). Specs are written by agents/humans
    working this repo, not untrusted external input, but escaping the
    fallback path costs nothing and closes the obvious XSS case regardless.

    The success path is sanitized through nh3 too, not just the fallback --
    the client-side DOMPurify pass in specs.js is defense-in-depth, not the
    only boundary. This app's CSP (script-src 'self', no unsafe-inline)
    happens to also block an injected <script> today, but that is incidental
    protection for anyone hitting /api/specs/{id}/content directly rather
    than through the gallery UI, not the actual fix.
    """
    try:
        rendered = _markdown.markdown(text, extensions=["fenced_code", "tables"])
        return nh3.clean(rendered, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES)
    except Exception:
        return f"<pre>{_html.escape(text)}</pre>"
