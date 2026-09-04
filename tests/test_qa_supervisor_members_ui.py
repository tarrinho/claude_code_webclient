"""QA coverage for the two ways a conversation joins a supervisor.

The API can be right while the UI never reaches it. That is not hypothetical
here: the supervisor page shipped with a getCsrf/apiFetch mutual recursion that
meant no request ever left the browser, and it looked fine in source.

So these drive the real files in a headless browser with fetch stubbed, and
assert what was actually requested.

Two of them exist because of specific bugs this project has already had:

* ``apiFetch`` resolves for 4xx as well as 2xx, so a handler that does not check
  ``response.ok`` reports success on a rejection. That is exactly how
  conversation reordering appeared to save and did not.
* An empty supervisor list must say so. Rendering an empty box reads as a
  failure when it is simply the first run.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "web" / "assets" / "app.js"
CHAT_LIST_JS = REPO / "web" / "assets" / "chat-list.js"
SUPERVISOR_JS = REPO / "web" / "assets" / "supervisor.js"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def run_page(html: str, budget_ms: int = 8000) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             # Chromium writes a ~126 MB profile per launch. Without this it
             # picks its own /tmp/org.chromium.Chromium.scoped_dir.* and
             # leaves it behind, so a single run of this file leaked 11 of
             # them and filled a 1.9 GB tmpfs -- after which every browser
             # test in the suite fails on a timeout and leaks another.
             f"--user-data-dir={page.parent}/chrome-profile",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>([^<]*)</title>", result.stdout)
    return match.group(1) if match else ""


class MenuEntrySourceTests(unittest.TestCase):
    """The row menu is the one-off entry point; it must reach the handler."""

    def setUp(self):
        self.source = CHAT_LIST_JS.read_text(encoding="utf-8")

    def test_the_menu_offers_the_action(self):
        self.assertIn("'Add to supervisor', 'add-to-supervisor'", self.source)

    def test_the_action_is_dispatched(self):
        """A button with no dispatch case is a menu entry that does nothing."""
        self.assertIn("action === 'add-to-supervisor'", self.source)
        self.assertIn("onAddToSupervisor", self.source)

    def test_the_callback_is_accepted_as_a_dependency(self):
        """Destructured from dependencies, or the dispatch calls undefined."""
        block = self.source.split("} = dependencies;", 1)[0]
        self.assertIn("onAddToSupervisor", block)

    def test_the_label_is_a_bare_verb_like_its_neighbours(self):
        """The menu deliberately avoids restating the title in every line."""
        self.assertNotIn("'Add to supervisor ' +", self.source)
        self.assertIn("makeButton('Add to supervisor'", self.source)


class PickerSourceTests(unittest.TestCase):

    def setUp(self):
        # _addChatToSupervisor moved to supervisor.js in a later module split;
        # appended rather than replacing APP_JS, since the other two tests in
        # this class still find their targets (the app.js-side wiring line,
        # the Escape handler chain) in app.js itself.
        self.source = (
            APP_JS.read_text(encoding="utf-8")
            + SUPERVISOR_JS.read_text(encoding="utf-8")
        )

    def test_the_menu_callback_is_wired_to_the_picker(self):
        self.assertIn("onAddToSupervisor: openSupervisorPicker", self.source)

    def test_the_add_checks_response_ok(self):
        """apiFetch resolves for 4xx, so without this a rejection reads as success."""
        body = self.source.split("async function _addChatToSupervisor", 1)[1]
        body = body.split("async function openSupervisorPicker", 1)[0]
        self.assertIn("response.ok", body)

    def test_escape_closes_it_before_the_other_dialogs(self):
        """It is the only dialog that can sit over another one."""
        block = self.source.split("if (event.key === 'Escape') {", 1)[1][:500]
        first = block.index("supervisorPickDialog")
        second = block.index("settingsDialog")
        self.assertLess(first, second,
                        "the topmost dialog must be the one Escape closes")


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class PickerBehaviourTests(unittest.TestCase):
    """Drive the real functions; assert what was actually requested."""

    def _harness(self, supervisors, add_status=200, add_body=None):
        """Lift the picker out of supervisor.js and run it against a stubbed
        fetch.

        Moved here in a later module split (was app.js). Two adjustments the
        move requires, beyond just reading a different file:

        * `export ` prefixes: legal on a real <script type="module">, a
          SyntaxError on the plain classic <script> this harness injects the
          slice into.
        * `state.previousFocus`, not a bare `previousFocus`: supervisor.js
          reads and writes it through the `state` object app.js exports (the
          same object machines.js and device-alerts.js share it through),
          since a bare imported `let` cannot be reassigned by the importer --
          only a shared object's own property can be.
        """
        source = SUPERVISOR_JS.read_text(encoding="utf-8")
        start = source.index("function _closeSupervisorPicker()")
        end = source.index("function openSupervisorPane()")
        picker = re.sub(r"^export ", "", source[start:end], flags=re.MULTILINE)
        import json as _json
        return f"""
        <body><script>
        let state = {{previousFocus: null}}, lastToast = null, addRequest = null;
        function showToast(msg, type) {{ lastToast = (type || 'ok') + ':' + msg; }}
        async function apiFetch(url, opts) {{
          if (url === '/api/supervisors') {{
            return {{ok: true, json: async () => ({{supervisors: {_json.dumps(supervisors)}}})}};
          }}
          addRequest = {{url, body: JSON.parse(opts.body)}};
          return {{ok: {str(add_status == 200).lower()}, status: {add_status},
                   json: async () => ({_json.dumps(add_body or {"added": ["c1"]})})}};
        }}
        {picker}
        (async () => {{
          await openSupervisorPicker("c1");
          const dialog = document.getElementById("supervisorPickDialog");
          const buttons = dialog ? dialog.querySelectorAll(".chat-menu button") : [];
          if (buttons.length) buttons[0].click();
          await new Promise(r => setTimeout(r, 50));
          const help = dialog && dialog.querySelector("p");
          document.title = JSON.stringify({{
            options: buttons.length,
            help: help ? help.textContent : "",
            sent: addRequest,
            toast: lastToast,
            stillOpen: !!document.getElementById("supervisorPickDialog"),
          }});
        }})();
        </script></body>
        """

    def test_it_offers_the_supervisors_and_posts_the_chosen_one(self):
        title = run_page(self._harness(
            [{"id": "s1", "title": "Release 0.9"}, {"id": "s2", "title": "Bug sweep"}]))
        self.assertIn('"options":2', title.replace(" ", ""))
        self.assertIn("/api/supervisors/s1/members", title)
        self.assertIn('"kind":"chat"', title.replace(" ", ""))
        self.assertIn('"ref_id":"c1"', title.replace(" ", ""))

    def test_an_empty_list_explains_itself(self):
        """An empty box reads as a failure; the first run is not one."""
        title = run_page(self._harness([]))
        self.assertIn("No supervisors yet", title)
        self.assertIn('"options":0', title.replace(" ", ""))

    def test_a_rejected_add_is_reported_as_an_error(self):
        title = run_page(self._harness(
            [{"id": "s1", "title": "Release 0.9"}],
            add_status=404, add_body={"error": "Supervisor not found"}))
        self.assertIn("error:", title)
        self.assertIn("Supervisor not found", title)

    def test_an_add_that_changed_nothing_says_so(self):
        """Reporting "Added" for a repeat would be a quiet lie."""
        title = run_page(self._harness(
            [{"id": "s1", "title": "Release 0.9"}],
            add_body={"added": [], "already_members": ["c1"]}))
        self.assertIn("Already in Release 0.9", title)

    def test_the_dialog_closes_after_a_successful_add(self):
        title = run_page(self._harness([{"id": "s1", "title": "Release 0.9"}]))
        self.assertIn('"stillOpen":false', title.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
