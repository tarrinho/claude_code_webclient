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
    # DOTALL alongside MULTILINE: a long named-import list wraps onto several
    # lines (app.js's and machines.js's both do), and `.` matching only
    # within one line left the wrapped continuation -- starting with a bare
    # `{` or a name list -- in the QuickJS input, which choked on it as a
    # script-mode syntax error while the file itself was perfectly valid
    # module syntax. Non-greedy `.*?` still stops at this statement's own
    # `;` rather than swallowing everything up to the last import in the file.
    src = re.sub(
        r"^\s*import\s.*?;\s*$", "", source, flags=re.MULTILINE | re.DOTALL
    )
    src = re.sub(
        r"^\s*export\s+(?=(?:async\s+)?(?:function|class|const|let|var)\b)",
        "",
        src,
        flags=re.MULTILINE,
    )
    # Re-export lists are module syntax too (for example `export { notifyResult
    # };`) and must be removed before wrapping the source in a script function.
    src = re.sub(
        r"^\s*export\s*\{.*?\};\s*$",
        "",
        src,
        flags=re.MULTILINE | re.DOTALL,
    )
    # `export default <expr>;` is a third module-only form the two patterns
    # above do not touch -- neither is a declaration keyword (function/class/
    # const/...) nor a `{ name, ... }` re-export list, so a file whose only
    # remaining export is `export default { a, b };` reached QuickJS unstripped
    # and failed as "unsupported keyword: export". Bodies are typically object
    # literals or identifiers, both of which can themselves contain braces, so
    # this drops only the `export default ` prefix rather than trying to
    # balance braces -- QuickJS then parses whatever expression is left,
    # exactly as it does for every other bare statement in the file.
    src = re.sub(r"^\s*export\s+default\s+", "", src, flags=re.MULTILINE)
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

    def test_gate_strips_export_default(self):
        """supervisor-map.js's real shape: named exports plus one
        `export default { ... }` at the end. Neither of the two existing
        strip patterns touches a default export, so this file reached
        QuickJS unstripped and failed with "unsupported keyword: export"
        until the stripping gained a third pattern for it.
        """
        _parse("function a() {}\nfunction b() {}\nexport default { a, b };")
        _parse("const value = state?.chat?.model ?? 'none';")


if __name__ == "__main__":
    unittest.main()
