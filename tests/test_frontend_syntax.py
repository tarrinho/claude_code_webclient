"""Parse every shipped JavaScript module.

The rest of the frontend suite asserts on substrings, which cannot tell a
working file from a syntactically broken one: a stray brace in app.js leaves
every one of those assertions passing while the page fails to load. There is no
node on the dev box, so this uses esprima, a pure-Python ES parser, to actually
parse each module.

This catches syntax only. It does not run the code, so a runtime error inside a
handler still gets through -- see tests/test_frontend_browser.py for the parts
that need a real DOM.
"""
from __future__ import annotations

import unittest
from pathlib import Path

try:
    import esprima
except ImportError:  # pragma: no cover - exercised only without the dev deps
    esprima = None

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"


@unittest.skipIf(esprima is None, "esprima not installed (pip install -r requirements-dev.txt)")
class JavaScriptParsesTests(unittest.TestCase):

    def _modules(self):
        modules = sorted(ASSETS.glob("*.js"))
        # Guard the guard: an empty glob would make every assertion below
        # vacuous, which is the failure mode this whole file exists to prevent.
        self.assertTrue(modules, f"no JavaScript modules found under {ASSETS}")
        return modules

    def test_every_module_parses(self):
        for path in self._modules():
            with self.subTest(module=path.name):
                try:
                    esprima.parseModule(path.read_text(), {"jsx": False})
                except esprima.Error as exc:  # pragma: no cover - failure path
                    self.fail(f"{path.name} is not valid JavaScript: {exc}")

    def test_parser_rejects_broken_source(self):
        """Prove the parser would actually fail, rather than accepting anything."""
        with self.assertRaises(esprima.Error):
            esprima.parseModule("function broken( { return 1;")

    def test_modules_declare_their_imports(self):
        """A module using an identifier it never imports fails at load time.

        app.js reaches into chat-list.js and conversation.js; if a refactor
        drops an import the page dies on the first call, which no substring
        assertion would notice.
        """
        app = (ASSETS / "app.js").read_text()
        tree = esprima.parseModule(app)
        imported: set[str] = set()
        for node in tree.body:
            if node.type != "ImportDeclaration":
                continue
            for spec in node.specifiers or []:
                if getattr(spec, "local", None) is not None:
                    imported.add(spec.local.name)
        for expected in ("apiFetch", "createChatListController", "createConversationController"):
            self.assertIn(expected, imported, f"app.js no longer imports {expected}")


if __name__ == "__main__":
    unittest.main()
