#!/usr/bin/env python3
"""Derive every asset cache-buster from the asset's own content.

Why this exists
---------------
`web/index.html` and the ES modules under `web/assets/` reference each other
with a `?v=N` querystring, and N was maintained by hand. Three separate times
on 2026-09-10 a module's content shipped while its N stood still, so browsers
holding the old copy kept running it:

* `app.js` changed at `?v=55` and `styles.css` at `?v=41`, found while fixing
  the Backends panel.
* `machines.js` gained a four-state transport status at `?v=10` and was never
  bumped, because its version had already moved earlier that day -- so the
  edit that bumped everything else at `?v=55` did not touch it, and
  `test_qa_asset_module_versions` stayed green because that test asserts the
  references *agree with each other*, not that a version moved when content
  did. A scan on 2026-09-10 found five modules in that state at once.

Nothing detects staleness by reading the files, because staleness is a fact
about history rather than about the current text. Deriving the number from the
content removes the class instead: the reference cannot disagree with the file,
because it is a function of the file.

The one design decision worth knowing
-------------------------------------
The hash is computed over each file **with its own `?v=` querystrings
stripped**, and that is what makes the scheme terminate.

Hashing the raw bytes does not. `machines.js` imports `app.js?v=N`; if
`app.js` changed, `machines.js` would have to be rewritten, which changes
`machines.js`'s bytes, which changes its hash, which forces `index.html` to be
rewritten, and so on. There is no fixed point to converge to. Stripping the
querystrings first means a module's version answers only for its own semantic
content and never for its dependencies' versions, so one pass is always
enough and an unchanged module's number never moves.

Why the value stays decimal
---------------------------
A hex digest would read more naturally, and `?v=` is matched as `(\\d+)` by
`test_qa_asset_module_versions.py`, `test_qa_backend_status_on_first_paint.py`
and `test_qa_backend_groups_collapse_on_open.py`. Keeping it decimal leaves
those tests -- two of which belong to other work -- untouched.

Usage
-----
    bin/wc-asset-versions.py            # rewrite references in place
    bin/wc-asset-versions.py --check    # exit 1 if anything is stale

`--check` is what `tests/test_qa_asset_versions_match_content.py` asserts, so
editing a module without running this fails the suite rather than reaching a
browser.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = WEB / "assets"

# Files that carry references. index.html and orchestrator.html load modules
# with <script src>, and the modules import each other.
def _reference_files() -> list[Path]:
    files = sorted(p for p in ASSETS.rglob("*.js"))
    files += sorted(p for p in WEB.glob("*.html"))
    return files


# `?v=` appears against .js and .css alike; styles.css is versioned too.
# Matches ANY cache-buster value, not just a decimal one, and that breadth is
# deliberate. These patterns used to require `\d+`, so a reference written by
# hand as `./delegation.js?v=delegation_pin_20260920` matched neither of them:
# the scan never compared it against its target, the rewriter never updated it,
# and `--check` reported every reference as matching while that one sat
# permanently unmanaged. A tag of exactly that shape survived several rounds of
# version syncing in web/assets/app.js on 2026-09-20 without one complaint.
#
# The written value is still always decimal (see version_for and the note above
# on why); what widened is only what these will RECOGNISE, so a non-decimal tag
# is now reported and replaced instead of ignored.
#
# The class stops at quotes, whitespace, `>` and `)` because that is what
# terminates a reference in the two places they appear -- `import '…?v=N'` in a
# module and `src="…?v=N">` in the HTML.
_BUSTER = r"[^\"'\s>)]+"
_REF_RE = re.compile(r"([\w./-]+\.(?:js|css))\?v=(" + _BUSTER + r")")
# Used to neutralise querystrings before hashing -- see the module docstring.
# This one must match the same breadth or the scheme stops terminating: a
# dependency's non-decimal tag would survive into the hashed text, so rewriting
# that tag would move the hash of every file importing it.
_STRIP_RE = re.compile(r"(\.(?:js|css))\?v=" + _BUSTER)


# When set, content is read from this git ref rather than the working tree.
# See --ref: it exists so a version can describe committed content while
# somebody else's uncommitted edits are sitting in the same file.
_REF: str | None = None


def _content(path: Path) -> str:
    """The file's text, from the working tree or from `--ref`."""
    if _REF is None:
        return path.read_text(encoding="utf-8")
    rel = path.resolve().relative_to(ROOT)
    out = subprocess.run(
        ["git", "show", f"{_REF}:{rel.as_posix()}"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if out.returncode != 0:
        # Untracked at that ref: fall back to the working tree rather than
        # inventing a hash for a file the ref does not have.
        return path.read_text(encoding="utf-8")
    return out.stdout


def _semantic_bytes(path: Path) -> bytes:
    """The file's content with every cache-buster removed.

    So a module's version tracks what the module *does*, not which versions of
    its dependencies it happened to be pointing at when it was hashed.
    """
    return _STRIP_RE.sub(r"\1", _content(path)).encode("utf-8")


def version_for(path: Path) -> int:
    """A stable decimal cache-buster for *path*.

    Six hex digits of SHA-256, as an integer: 16.7M values, which is ample for
    telling one revision of a file from the next, and short enough to read in a
    URL. Not a security boundary -- a collision means a browser keeps a stale
    copy, and that is what a hard reload is for.
    """
    digest = hashlib.sha256(_semantic_bytes(path)).hexdigest()
    return int(digest[:6], 16)


def _target(name: str, holder: Path) -> Path | None:
    """Resolve a reference as written, relative to the file that makes it.

    Resolved properly rather than by basename. The first version of this took
    `Path(name).name` and looked only in `web/assets/`, which silently missed
    `web/assets/orchestrator/main.js` -- referenced from `orchestrator.html`
    as `/assets/orchestrator/main.js` -- and reported it as "not a file". It
    also made `../format.js` from a subdirectory resolve by luck rather than
    by path. A resolver that flattens directories cannot tell two same-named
    modules apart, which is exactly the confusion cache-busters exist to
    prevent.
    """
    if name.startswith("/"):
        candidate = WEB / name.lstrip("/")
    else:
        candidate = holder.parent / name
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    # Never resolve outside web/, whatever a reference claims.
    if WEB.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def scan() -> tuple[list[str], int]:
    """Return (problems, reference_count) without writing anything."""
    problems: list[str] = []
    seen = 0
    for holder in _reference_files():
        # Through _content, so the reference and the hash it is compared
        # against come from the *same* snapshot. Reading the holder from the
        # working tree while hashing targets from --ref compares a reference
        # in one revision against content in another, and every uncommitted
        # asset edit then fails the check -- which is the "gate people switch
        # off" failure the --ref scoping exists to avoid.
        text = _content(holder)
        for match in _REF_RE.finditer(text):
            name, found = match.group(1), match.group(2)
            target = _target(name, holder)
            if target is None:
                problems.append(
                    f"{holder.relative_to(ROOT)}: references {name}, which does "
                    f"not resolve to a file under web/")
                continue
            seen += 1
            want = version_for(target)
            if not found.isdigit():
                # Reported separately from a stale number because the fix is
                # the same but the failure is worse: a hand-written tag is not
                # merely out of date, it was never being tracked at all.
                problems.append(
                    f"{holder.relative_to(ROOT)}: {name}?v={found} is not a "
                    f"content hash, so it is never checked or updated; "
                    f"should be {want}")
            elif int(found) != want:
                problems.append(
                    f"{holder.relative_to(ROOT)}: {name}?v={found} but its "
                    f"content hashes to {want}")
    return problems, seen


def rewrite() -> tuple[int, int]:
    """Rewrite every reference to match its target's content. Idempotent."""
    changed_files = 0
    changed_refs = 0
    for holder in _reference_files():
        text = holder.read_text(encoding="utf-8")

        def _sub(match: re.Match[str]) -> str:
            nonlocal changed_refs
            name, found = match.group(1), match.group(2)
            target = _target(name, holder)
            if target is None:
                return match.group(0)
            want = version_for(target)
            # A non-decimal tag always counts as changed: it cannot equal the
            # hash, and it is the case most worth reporting in the summary
            # line, since it means that reference had never been managed.
            if not found.isdigit() or int(found) != want:
                changed_refs += 1
            return f"{name}?v={want}"

        new = _REF_RE.sub(_sub, text)
        if new != text:
            holder.write_text(new, encoding="utf-8")
            changed_files += 1
    return changed_files, changed_refs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report staleness and exit 1; write nothing")
    parser.add_argument(
        "--ref", metavar="GITREF",
        help="hash content from this git ref instead of the working tree. "
             "Use when another session has uncommitted edits in an asset: the "
             "number then describes the committed content, so HEAD stays "
             "self-consistent and their dirty tree correctly reports as stale "
             "until they re-run this.")
    args = parser.parse_args()

    global _REF
    _REF = args.ref

    if args.check:
        problems, seen = scan()
        if problems:
            print(f"{len(problems)} stale or unresolvable reference(s) of {seen}:")
            for line in problems:
                print(f"  {line}")
            print("\nRun bin/wc-asset-versions.py to fix.")
            return 1
        print(f"all {seen} asset references match their content")
        return 0

    files, refs = rewrite()
    if refs:
        print(f"updated {refs} reference(s) across {files} file(s)")
    else:
        print("nothing to do; every reference already matches its content")
    # Rewriting a reference changes the holder's raw bytes but not its semantic
    # bytes, so a second pass must find nothing. Asserted rather than trusted:
    # if it ever does, the strip-before-hash rule has been broken and the
    # scheme no longer terminates.
    problems, _ = scan()
    if problems:
        print("\nERROR: still stale after a full pass -- the hash is not "
              "independent of the querystrings it rewrites:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
