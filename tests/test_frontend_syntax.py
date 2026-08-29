"""Parse every shipped JavaScript module.

The rest of the frontend suite asserts on substrings, which cannot tell a
working file from a syntactically broken one: a stray brace in app.js leaves
every one of those assertions passing while the page fails to load. There is no
node on this box, so this parses with QuickJS instead.

Why QuickJS and not esprima: esprima's grammar predates optional catch binding
(`catch { }`, ES2019), which appears on line 12 of app.js. It rejects all six
modules as syntax errors, so a gate built on it would fail on correct code --
the opposite failure, and a worse one, since it trains you to ignore the gate.

This catches syntax only. Nothing is executed -- the source is wrapped in a
function expression that is never called -- so a runtime error inside a handler
still gets through. See tests/test_frontend_browser.py for that.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - exercised only without the dev deps
    quickjs = None

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"


def _parse(source: str) -> None:
    """Parse *source* without running it.

    Module syntax is stripped because `import`/`export` are only legal at the
    top level of a module, and the wrapper is a function expression: QuickJS
    parses the whole body, and nothing calls it.
    """
    src = re.sub(r"^\s*import\s.*?;\s*$", "", source, flags=re.MULTILINE)
    src = re.sub(
        r"^\s*export\s+(?=(?:async\s+)?(?:function|class|const|let|var)\b)",
        "",
        src,
        flags=re.MULTILINE,
    )
    quickjs.Context().eval("(function(){\n" + src + "\n})")


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class JavaScriptParsesTests(unittest.TestCase):

    def test_every_module_parses(self):
        modules = sorted(ASSETS.glob("*.js"))
        # Guard the guard: an empty glob would make this pass vacuously, which
        # is the exact failure mode the file exists to prevent.
        self.assertTrue(modules, f"no JavaScript modules found under {ASSETS}")
        for path in modules:
            with self.subTest(module=path.name):
                try:
                    _parse(path.read_text())
                except Exception as exc:  # noqa: BLE001 -- report any parse failure
                    self.fail(f"{path.name} is not valid JavaScript: {exc}")

    def test_gate_rejects_broken_source(self):
        """Prove the parser fails on bad input rather than accepting anything."""
        for broken in ("function broken( { return 1;", "const x = {;", "if (true) { "):
            # quickjs raises its own JSException for a syntax error; naming it
            # keeps this from passing on some unrelated failure.
            with self.subTest(source=broken), self.assertRaises(quickjs.JSException):
                _parse(broken)

    def test_gate_accepts_the_modern_syntax_this_codebase_uses(self):
        """Optional catch binding and optional chaining must both parse.

        esprima rejects the first and a gate that cannot read the code it
        guards is worse than no gate.
        """
        _parse("try { risky(); } catch { fallback(); }")
        _parse("const value = state?.chat?.model ?? 'none';")


if __name__ == "__main__":
    unittest.main()
