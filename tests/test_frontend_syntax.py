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

import json
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
                except Exception as exc:
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


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class DelegationColumnsResolutionTests(unittest.TestCase):
    """F8: the settings page must derive its editable columns from
    `GET /api/delegation`'s `editable_columns` field (routes/delegation.py's
    `_EDITABLE`) instead of carrying a fourth hardcoded copy of the five
    measured column names -- `tiered_delegation._REQUIRED_COLUMNS`,
    `routes/db_delegation._COLUMNS` and `routes/delegation._EDITABLE` are
    the other three, and only the first three were ever pinned together by
    a test.

    This actually *executes* `delegation.js`'s `_resolveColumns`, extracted
    from the shipped source rather than retyped here, so a change to the
    real function is what this test exercises -- a text-content assertion
    on the source would keep passing if the function's logic changed while
    its name stayed put.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (ASSETS / "delegation.js").read_text()

    def _resolve(self, payload_json: str) -> list:
        """Run the shipped `_resolveColumns` (plus the `_DEFAULT_COLUMNS`
        constant it falls back to) against *payload_json*, taken verbatim
        from the source so this cannot drift from what ships."""
        default_cols = re.search(
            r"const _DEFAULT_COLUMNS = (\[[^\]]*\]);", self.source)
        self.assertIsNotNone(
            default_cols, "_DEFAULT_COLUMNS not found in delegation.js -- "
            "the fallback constant was renamed or removed")
        resolver = re.search(
            r"function _resolveColumns\(payload\) \{.*?\n\}",
            self.source, re.DOTALL)
        self.assertIsNotNone(
            resolver, "_resolveColumns not found in delegation.js -- F8's "
            "column-derivation function was renamed or removed")
        script = (
            f"const _DEFAULT_COLUMNS = {default_cols.group(1)};\n"
            f"{resolver.group(0)}\n"
            f"JSON.stringify(_resolveColumns({payload_json}));"
        )
        return json.loads(quickjs.Context().eval(script))

    def test_uses_editable_columns_from_the_response(self):
        """The normal case: the server's list wins, in the server's order,
        even when it differs from the five-column default -- proving this
        is read from the payload and not just falling through to the
        fallback by coincidence."""
        result = self._resolve(
            '{"editable_columns": ["accuracy", "cost_basis"]}')
        self.assertEqual(result, ["accuracy", "cost_basis"])

    def test_falls_back_when_editable_columns_is_missing(self):
        """An older cached `GET /api/delegation` response, from before this
        field existed, must still render the ordinary table -- not a broken
        or empty one."""
        result = self._resolve('{"rows": []}')
        self.assertEqual(
            result,
            ["accuracy", "n", "cost_per_1m_tokens", "median_latency_s",
             "max_context"])

    def test_falls_back_when_editable_columns_is_empty(self):
        result = self._resolve('{"editable_columns": []}')
        self.assertEqual(
            result,
            ["accuracy", "n", "cost_per_1m_tokens", "median_latency_s",
             "max_context"])

    def test_falls_back_when_editable_columns_is_not_an_array(self):
        """A malformed or truncated cached response naming the field as the
        wrong type must not raise -- it degrades the same as a missing
        field."""
        result = self._resolve('{"editable_columns": "accuracy"}')
        self.assertEqual(
            result,
            ["accuracy", "n", "cost_per_1m_tokens", "median_latency_s",
             "max_context"])


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class DelegationStatusAndBlockerTests(unittest.TestCase):
    """The Settings > Delegation redesign's status band and per-card blocker
    box are each driven by a pure function extracted from the shipped source
    and actually executed here, the same way
    `DelegationColumnsResolutionTests` above exercises `_resolveColumns` --
    a text-content assertion on the source would keep passing if the
    function's logic changed while its name stayed put.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (ASSETS / "delegation.js").read_text()

    def _extract(self, name: str) -> str:
        match = re.search(
            rf"function {name}\(.*?\n\}}", self.source, re.DOTALL)
        self.assertIsNotNone(
            match, f"{name} not found in delegation.js -- it was renamed "
            "or removed")
        return match.group(0)

    def _run(self, name: str, call: str):
        script = f"{self._extract(name)}\nJSON.stringify({call});"
        return json.loads(quickjs.Context().eval(script))

    # ── _statusHeadline ──────────────────────────────────────────────────

    def test_status_headline_nothing_operational(self):
        self.assertEqual(
            self._run("_statusHeadline", "_statusHeadline(0, 9)"),
            "Nothing is routing. 0 of 9 task types operational.")

    def test_status_headline_everything_operational(self):
        self.assertEqual(
            self._run("_statusHeadline", "_statusHeadline(9, 9)"),
            "Everything is routing. 9 of 9 task types operational.")

    def test_status_headline_partial(self):
        self.assertEqual(
            self._run("_statusHeadline", "_statusHeadline(3, 9)"),
            "3 of 9 task types operational.")

    def test_status_headline_singular_task_type(self):
        self.assertEqual(
            self._run("_statusHeadline", "_statusHeadline(0, 1)"),
            "Nothing is routing. 0 of 1 task type operational.")

    def test_status_headline_no_task_types(self):
        self.assertEqual(
            self._run("_statusHeadline", "_statusHeadline(0, 0)"),
            "No task types are in the table yet.")

    # ── _measuredCellsSummary ────────────────────────────────────────────

    def test_measured_cells_counts_non_null_values_only(self):
        rows = (
            '[{"accuracy": 0.9, "n": null}, '
            '{"accuracy": null, "n": 4}]'
        )
        result = self._run(
            "_measuredCellsSummary",
            f'_measuredCellsSummary({rows}, ["accuracy", "n"])')
        self.assertEqual(result, {"measured": 2, "total": 4})

    def test_measured_cells_empty_rows(self):
        result = self._run(
            "_measuredCellsSummary",
            '_measuredCellsSummary([], ["accuracy", "n"])')
        self.assertEqual(result, {"measured": 0, "total": 0})

    # ── _blockerLines ────────────────────────────────────────────────────

    def test_blocker_lines_combines_policy_then_data(self):
        entry = '{"policy": "held by spec 12", "data": ["a problem"]}'
        result = self._run("_blockerLines", f"_blockerLines({entry})")
        self.assertEqual(result, ["held by spec 12", "a problem"])

    def test_blocker_lines_empty_for_a_clean_type(self):
        entry = '{"policy": null, "data": []}'
        result = self._run("_blockerLines", f"_blockerLines({entry})")
        self.assertEqual(result, [])

    def test_blocker_lines_handles_a_missing_entry(self):
        result = self._run("_blockerLines", "_blockerLines(undefined)")
        self.assertEqual(result, [])

    def test_blocker_lines_data_only(self):
        entry = '{"policy": null, "data": ["problem one", "problem two"]}'
        result = self._run("_blockerLines", f"_blockerLines({entry})")
        self.assertEqual(result, ["problem one", "problem two"])


if __name__ == "__main__":
    unittest.main()


class DelegationErrorMessageTests(unittest.TestCase):
    """The settings page must show the server's reason for refusing a write.

    This app's error contract is `{"error": "..."}` -- `app.py`'s
    `handle_http_exception` serialises every `HTTPException` that way, so
    FastAPI's `detail` name never reaches the browser. `delegation.js` read
    `data.detail` in both of its write paths, so every refusal arrived as
    `undefined` and was replaced by a generic fallback: a knob that would not
    move said only "Could not change the operational flag", and a rejected
    cell edit said "Could not save" instead of naming the broken invariant
    and the column -- which spec 1.1 requires the refusal to name.

    Nothing caught it because the route tests assert the API's response,
    which was correct all along; the loss happened in the browser. So this
    executes the shipped `_errorMessage` rather than asserting on source
    text, the same way `DelegationColumnsResolutionTests` does.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (ASSETS / "delegation.js").read_text()

    def _message(self, data_json: str, fallback: str = "fallback") -> str:
        fn = re.search(
            r"function _errorMessage\(data, fallback\) \{.*?\n\}",
            self.source, re.DOTALL)
        self.assertIsNotNone(
            fn, "_errorMessage not found in delegation.js -- the refusal-"
            "reason resolver was renamed or removed")
        script = (
            f"{fn.group(0)}\n"
            f"JSON.stringify(_errorMessage({data_json}, {json.dumps(fallback)}));"
        )
        return json.loads(quickjs.Context().eval(script))

    def test_the_apps_error_key_is_read(self):
        """The shape `app.py`'s exception handler actually returns. This is
        the case that was broken: the reason was present and discarded."""
        self.assertEqual(
            self._message('{"error": "coding: spec 12 holds this flip"}'),
            "coding: spec 12 holds this flip")

    def test_detail_is_read_when_error_is_absent(self):
        """A plain FastAPI error path that never reached the custom handler
        carries `detail`. A real reason under either key beats a fallback."""
        self.assertEqual(
            self._message('{"detail": "median_latency_s must be a number"}'),
            "median_latency_s must be a number")

    def test_error_wins_over_detail_when_both_are_present(self):
        """`error` is this app's contract; `detail` is the compatibility
        second choice, so it must not shadow the real one."""
        self.assertEqual(
            self._message('{"error": "the real reason", "detail": "the other one"}'),
            "the real reason")

    def test_a_body_with_no_reason_falls_back(self):
        """`response.json()` failing yields `{}` at the call site, and a 500
        may carry no reason at all -- the caller still needs a message."""
        self.assertEqual(self._message("{}"), "fallback")

    def test_a_blank_reason_falls_back(self):
        """An empty or whitespace-only reason is not a reason. Without this
        the user gets a toast with no text, which reads as a silent failure
        -- the exact symptom this class exists to prevent."""
        self.assertEqual(self._message('{"error": "   "}'), "fallback")

    def test_a_non_object_body_falls_back(self):
        """`response.json()` can legitimately yield a string or null; reading
        a property off either must not throw inside the error handler."""
        self.assertEqual(self._message("null"), "fallback")
        self.assertEqual(self._message('"not an object"'), "fallback")
