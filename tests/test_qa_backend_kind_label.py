"""QA: the backend_kind -> display label mapping exists in exactly one place.

Pedro noticed the Settings machine list and the per-chat Backend picker
disagreeing about what to call an ssh_proxy machine, and asked why. Root
cause: the kind->label table was maintained twice -- once in app.js
(`_machineLabel`'s inline object) and once in machines.js
(`_BACKEND_KIND_LABELS`) -- and neither copy got an `ssh-proxy` entry added
when that provider shipped. Each one failed differently:

  * machines.js's `_providerLabel` fell through to its anthropic/else guess
    and showed the flatly wrong "Claude Code proxy" for an ssh_proxy machine
    in Settings.
  * app.js's `_machineLabel` fell through to `|| machine.backend_kind`, the
    raw internal string, and showed "ssh-proxy" (not a real label) in the
    per-chat Backend picker.

Fixed by exporting one function, `backendKindLabel(kind)`, from app.js --
the file both already share machine data (`_machines`) through -- and having
machines.js import and use it instead of keeping a second table. This test
checks both halves: the function itself covers every kind the server can
send (`shared.backend_kind`), and machines.js no longer carries its own copy
that could quietly drift out of sync with it again.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "web" / "assets" / "app.js"
MACHINES_JS = REPO / "web" / "assets" / "machines.js"
SHARED_PY = REPO / "shared.py"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def lift(path: Path, name: str) -> str:
    """The full text of `[export ]function <name>(...)`, braces matched."""
    source = path.read_text(encoding="utf-8")
    match = re.search(
        rf"(?:export\s+)?function\s+{re.escape(name)}\s*\(", source
    )
    if match is None:
        raise AssertionError(
            f"{name}() not found in {path.name}. If it was renamed or "
            "inlined, update this harness rather than letting it skip"
        )
    start = match.start()
    open_brace = source.index("{", match.end() - 1)
    depth, pos = 1, open_brace + 1
    while depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos].replace("export function", "function", 1)


# Every kind shared.py's backend_kind() can actually return.
KINDS = ["anthropic", "anthropic-compatible", "proxy", "ssh-proxy"]

PAGE = """<!doctype html><meta charset="utf-8"><title>pending</title>
<script>
%(function)s
const kinds = %(kinds)s;
const labels = kinds.map(backendKindLabel);
const failed = [];
if (new Set(labels).size !== labels.length) {
  failed.push('two different kinds produced the same label: ' + JSON.stringify(labels));
}
labels.forEach((label, i) => {
  if (!label || label === kinds[i]) {
    failed.push(kinds[i] + ' did not get a real label, got ' + JSON.stringify(label));
  }
});
document.title = failed.length ? 'FAIL: ' + failed.join(' | ') : 'OK';
</script>
"""


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class BackendKindLabelTests(unittest.TestCase):
    def test_every_kind_the_server_can_send_gets_a_real_label(self):
        function_src = lift(APP_JS, "backendKindLabel")
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "probe.html"
            page.write_text(PAGE % {
                "function": function_src,
                "kinds": json.dumps(KINDS),
            })
            result = subprocess.run(
                [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
                 f"--user-data-dir={page.parent}/chrome-profile",
                 "--virtual-time-budget=5000", "--dump-dom", f"file://{page}"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        match = re.search(r"<title>([^<]*)</title>", result.stdout)
        title = match.group(1) if match else ""
        if title == "pending":
            raise AssertionError(
                f"the page never ran -- chromium said: {result.stderr[-300:]}"
            )
        self.assertEqual(title, "OK", title)


class NoDuplicateLabelTableTests(unittest.TestCase):
    """The regression itself was two tables, not a wrong one -- this guards
    against a *third* copy being added the next time someone needs a label
    and reaches for a new object instead of the shared function."""

    def test_machines_js_has_no_kind_label_table_of_its_own(self):
        source = MACHINES_JS.read_text(encoding="utf-8")
        self.assertNotIn(
            "_BACKEND_KIND_LABELS", source,
            "machines.js has its own kind->label table again -- import "
            "backendKindLabel from app.js instead of adding a second one",
        )
        self.assertIn(
            "backendKindLabel(machine.backend_kind)", source,
            "_providerLabel no longer calls the shared backendKindLabel -- "
            "check it wasn't reverted to a local guess",
        )

    def test_machines_js_imports_it_from_app_js(self):
        source = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(r"from ['\"]\./app\.js(\?v=\d+)?['\"]", source)
        self.assertIsNotNone(match, "machines.js no longer imports from app.js at all")
        # The import statement itself is one `import {...} from './app.js?vN'`
        # block above the match; just confirm the name appears somewhere in
        # that block rather than parsing full import syntax.
        block_start = source.rfind("import {", 0, match.start())
        self.assertIn("backendKindLabel", source[block_start:match.end()])

    def test_backend_kind_label_is_exported(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertRegex(
            source, r"export function backendKindLabel\(",
            "backendKindLabel is not exported from app.js -- machines.js's "
            "import of it would 404 at parse time",
        )


class ShapeAgreesWithTheServerTests(unittest.TestCase):
    """The client-side map is only correct if it actually names every value
    shared.backend_kind() can return. A kind added there without a matching
    entry here reproduces this exact bug for the next provider type."""

    def test_every_server_side_kind_has_a_client_side_case(self):
        shared_source = SHARED_PY.read_text(encoding="utf-8")
        app_source = APP_JS.read_text(encoding="utf-8")
        # backend_kind() returns these four string literals; pull them out of
        # the return/comparison statements rather than hardcoding the list
        # twice, so a fifth kind added there fails this test until it is
        # added here too.
        server_kinds = set(re.findall(r'return "([a-z-]+)"', shared_source))
        server_kinds |= set(re.findall(r'== "([a-z-]+)"', shared_source))
        server_kinds &= set(KINDS) | {"anthropic-compatible"}  # drop unrelated matches
        missing = [k for k in server_kinds if f"'{k}'" not in app_source]
        self.assertEqual(
            missing, [],
            f"shared.py's backend_kind() can return {missing}, which "
            "app.js's backendKindLabel table has no entry for",
        )


if __name__ == "__main__":
    unittest.main()
