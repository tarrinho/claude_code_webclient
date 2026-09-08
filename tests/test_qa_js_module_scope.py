"""QA: no module reads a private name that lives in another module.

Pedro reported the Skills page in Settings failing with `_skillFilter is not
defined`. `app.js` declares `let _skillFilter` and `skills.js` reads it — and ES
modules do not share top-level scope, so the read was a ReferenceError. The
file's own header comment already knew this, for `byId`:

    does not share app.js's own `const byId` (ES modules do not share
    top-level scope across files), so it needs its own

The same reasoning was simply not carried to the other names when the app.js
split moved this code out. Git remembers the shape of that split: the commit
before this area settled is literally titled *"fix: restore what the app.js
module split silently deleted"*.

Why it was invisible rather than loud: `loadSkills` wraps the render in
`try/catch`, so the ReferenceError was swallowed, `_skillsData` was set back to
null, and the count line was blanked. Measured before and after with a stubbed
payload:

    before: groups=1 (an error notice), count=""
    after:  groups=1 (the real group),  count="1 skills"

No crash, no failed request, no console noise for anyone not looking — just a
panel that renders an error.

This check reads every module and reports `_`-prefixed identifiers that are used
but neither declared nor imported there. Written for one bug, it found four:

* `_skillFilter`           app.js -> skills.js   (the reported one)
* `_collapsedSkillGroups`  app.js -> skills.js   (same render path, one line later)
* `_drawMapWires`          machines.js -> app.js (Backends tab)
* `_setModelDefault`       app.js -> machines.js (model default checkbox)
* `_toggleModelOffered`    app.js -> machines.js (model offered checkbox)

The leading underscore is the codebase's own marker for module-private, which is
what makes the heuristic cheap and precise: a name spelled that way is not meant
to cross a file boundary, so finding one that does is the finding.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "web" / "assets"

# The lookbehind excludes member accesses. `d._children` is a property on an
# object -- here, d3's own collapse idiom on a hierarchy node -- and a property
# is never a module-scope identifier, so it can neither be a cross-module read
# nor be fixed by an import. Without this, supervisor-map.js reported
# `_children` on the strength of eleven uses, all eleven of them `._children`.
# The bug this file was written for (`_skillFilter`) is a bare identifier and is
# still caught.
PRIVATE = re.compile(r"(?<![.\w$])(_[A-Za-z][A-Za-z0-9_]*)\b")
DECLARED = "const|let|var|function|class"


def _modules() -> list[Path]:
    """Every module this check owns, which excludes vendored bundles.

    A minified third-party bundle is one long line with its declarations inside
    function scopes this file's line-oriented patterns cannot see, so every
    private name in it reads as undeclared. d3.min.js alone reported four
    (`_intern`, `_key`, `_n`, `_partials`) — all of them real declarations d3
    makes and uses correctly. Vendored code is also not code we can fix, so a
    finding against it is noise either way, and noise is what gets a check
    switched off.
    """
    return [p for p in sorted(ASSETS.glob("*.js")) if not p.name.endswith(".min.js")]


def _code_only(source: str) -> str:
    """*source* with comments and string literals blanked.

    Necessary in both directions: a name mentioned in a comment is not a use
    (this file's own explanatory comments would trip it), and a `_`-prefixed
    DOM id inside a string is not an identifier.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r"`(?:\\.|[^`\\])*`", " `` ", source)
    source = re.sub(r"'(?:\\.|[^'\\\n])*'", " '' ", source)
    source = re.sub(r'"(?:\\.|[^"\\\n])*"', ' "" ', source)
    return source


def _is_available(name: str, code: str) -> bool:
    """Whether *name* is declared, imported, or bound as a parameter in *code*.

    Deliberately generous. A false positive here would be a test that fails on
    correct code, which gets suppressed and then protects nothing; a false
    negative only costs the coverage this heuristic was never going to give.
    """
    escaped = re.escape(name)
    return bool(
        re.search(rf"\b(?:{DECLARED})\s+{escaped}\b", code)
        or re.search(rf"import\s*\{{[^}}]*\b{escaped}\b[^}}]*\}}", code)
        or re.search(rf"import\s+{escaped}\b", code)
        # Positional or defaulted parameter: (a, _b) / (_b = 1)
        or re.search(rf"[(,]\s*{escaped}\s*[,)=]", code)
        # Destructured binding: const {_a} = x  /  ({_a}) => …
        or re.search(rf"\{{[^}}]*\b{escaped}\b[^}}]*\}}\s*=", code)
        or re.search(rf"catch\s*\(\s*{escaped}\s*\)", code)
    )


def offenders() -> dict[str, list[str]]:
    """Module file name -> private names it uses but does not have."""
    found: dict[str, list[str]] = {}
    for path in _modules():
        code = _code_only(path.read_text(encoding="utf-8"))
        missing = [name for name in sorted(set(PRIVATE.findall(code)))
                   if not _is_available(name, code)]
        if missing:
            found[path.name] = missing
    return found


def _defined_in(name: str) -> list[str]:
    """Where *name* is actually declared, to name the fix in the failure."""
    homes = []
    for path in _modules():
        code = _code_only(path.read_text(encoding="utf-8"))
        if re.search(rf"\b(?:{DECLARED})\s+{re.escape(name)}\b", code):
            homes.append(path.name)
    return homes


class NoCrossModulePrivateReadsTests(unittest.TestCase):
    """A private name must be declared or imported where it is used."""

    def test_there_are_modules_and_private_names_to_check(self):
        """Guards the guard.

        If the assets move, or the comment/string stripping starts eating the
        code, every assertion below passes against nothing — which is the same
        silent-pass this file exists to prevent.
        """
        modules = _modules()
        self.assertGreaterEqual(len(modules), 5, "found almost no modules")
        seen = set()
        for path in modules:
            seen |= set(PRIVATE.findall(_code_only(path.read_text(encoding="utf-8"))))
        self.assertGreaterEqual(
            len(seen), 20,
            "found almost no _-prefixed identifiers, so the stripping or the "
            "pattern has gone stale and this suite asserts nothing",
        )

    def test_no_module_reads_another_modules_private_name(self):
        found = offenders()
        if not found:
            return
        report = []
        for module, names in sorted(found.items()):
            for name in names:
                homes = _defined_in(name) or ["nowhere"]
                report.append(
                    f"{module} uses {name}, declared in {', '.join(homes)}")
        self.fail(
            "these are ReferenceErrors at runtime -- ES modules do not share "
            "top-level scope, and the leading underscore says the name was "
            "never meant to cross a file. Export and import it, or move it to "
            "the module that uses it:\n  " + "\n  ".join(report)
        )


if __name__ == "__main__":
    unittest.main()
