"""QA: every JS module resolves to exactly one URL.

Pedro reported the Skills page in Settings not working. It was not the skills
code and not the endpoint -- there were no `/api/skills` requests in the log at
all. `index.html` loaded `app.js?v=32` while six panel modules still imported
`./app.js?v=31`.

The browser keys a module by its **resolved URL, query string included**, so
those are two different modules. Measured in Chromium rather than assumed:

    v32 vs v31 same module: false
    v32 vs v32 same module: true
    top-level evaluations:  2

So `app.js` was evaluated twice, giving two separate `state` objects. `skills.js`
imports `state` from `app.js?v=31` and reads `state.currentChat?.id` to scope its
request, while `app.js?v=32` is the instance the real page drives -- so the
skills request went out unscoped and the session line rendered blank. Every
other panel module had the same defect; skills is simply where it was noticed.

The cause was a half-finished cache-bust: whoever bumped `app.js` in the HTML
did not bump the six imports. A cache-buster is only a cache-buster if every
reference to the file agrees; where they disagree it silently becomes a module
duplicator, and nothing fails loudly.

This test is the guard. It fails while a bump is half-applied -- which is
exactly the state that shipped -- so the next person to bump a version finds out
before a user does.
"""
from __future__ import annotations

import collections
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
ASSETS = WEB / "assets"

# <script type="module" src="/assets/app.js?v=32">
SCRIPT_SRC = re.compile(r'src="/assets/([A-Za-z0-9_-]+\.js)(\?v=\d+)?"')
# import ... from './app.js?v=32'
MODULE_IMPORT = re.compile(r"""from ['"]\./([A-Za-z0-9_-]+\.js)(\?v=\d+)?['"]""")

NO_QUERY = "(no ?v=)"


def references() -> dict[str, set[str]]:
    """Every reference to a module file, mapped to the query strings used.

    Both directions matter and both were wrong here: the HTML entry points name
    the file, and the modules name each other. A file referenced with two
    different queries is loaded twice, whichever side the disagreement is on.
    """
    found: dict[str, set[str]] = collections.defaultdict(set)
    for html in sorted(WEB.glob("*.html")):
        for match in SCRIPT_SRC.finditer(html.read_text(encoding="utf-8")):
            found[match.group(1)].add(match.group(2) or NO_QUERY)
    for module in sorted(ASSETS.glob("*.js")):
        for match in MODULE_IMPORT.finditer(module.read_text(encoding="utf-8")):
            found[match.group(1)].add(match.group(2) or NO_QUERY)
    return found


def _where(name: str) -> list[str]:
    """Every file and line that references *name*, for the failure message.

    Without this the failure says a version is inconsistent and leaves the
    reader to grep for the other half, which is the tedious part.
    """
    hits: list[str] = []
    for path in [*sorted(WEB.glob("*.html")), *sorted(ASSETS.glob("*.js"))]:
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in (SCRIPT_SRC, MODULE_IMPORT):
                match = pattern.search(line)
                if match and match.group(1) == name:
                    query = match.group(2) or NO_QUERY
                    hits.append(f"{path.relative_to(REPO)}:{number}  {query}")
    return hits


class ModuleUrlsAreUnambiguousTests(unittest.TestCase):
    """One file, one URL. Anything else duplicates the module."""

    def test_there_are_references_to_check(self):
        """Guards the guard. If the regexes stop matching -- a bundler, a
        different quoting style, assets moved -- every assertion below passes
        against nothing, which is the failure mode this file exists to prevent
        in the first place."""
        found = references()
        self.assertGreaterEqual(
            len(found), 8,
            "found almost no module references, so the patterns have gone stale "
            "and this suite is asserting nothing",
        )
        self.assertIn("app.js", found, "app.js is not being found at all")

    def test_every_module_resolves_to_one_url(self):
        offenders = {n: v for n, v in references().items() if len(v) > 1}
        if not offenders:
            return
        report = []
        for name, queries in sorted(offenders.items()):
            report.append(f"{name} is referenced as {sorted(queries)}:")
            report.extend(f"    {hit}" for hit in _where(name))
        self.fail(
            "these modules resolve to more than one URL, so the browser loads "
            "each of them twice -- separate module instances, separate "
            "top-level state, and no error anywhere:\n  " + "\n  ".join(report)
        )

    def test_app_js_specifically(self):
        """Called out on its own because it is the one that carries `state`.

        Duplicating a leaf module wastes a fetch. Duplicating app.js gives the
        panels a different `state` object from the one the page drives, which is
        how the Skills tab came to request unscoped data and render blank.
        """
        queries = references().get("app.js", set())
        self.assertEqual(
            len(queries), 1,
            f"app.js is referenced as {sorted(queries)}. Every module that "
            "imports it must use the same query as the <script> tag, or there "
            "are two `state` objects and the panels read the wrong one.",
        )


class ImportedFilesExistTests(unittest.TestCase):
    """A version bump must not outlive the file it names."""

    def test_every_referenced_module_is_on_disk(self):
        missing = [name for name in references()
                   if not (ASSETS / name).is_file()]
        self.assertEqual(
            missing, [],
            f"referenced but absent from web/assets: {missing}. A module import "
            "that 404s takes down the whole importing graph, so one renamed "
            "file silently kills every panel.",
        )


if __name__ == "__main__":
    unittest.main()
