"""QA coverage for the bulk "+ Add members" picker on the supervisor page.

The row menu adds one conversation as you come across it. This is the other
entry point: setting a supervisor up in one pass, choosing several agents and
conversations at once.

Written before the implementation. Each test names a behaviour the picker must
have rather than describing one it happens to have, which is the only way the
test can disagree with the code later.

Four of these exist because of specific mistakes already made in this project:

* ``apiFetch`` resolves for 4xx as well as 2xx, so a handler that does not check
  ``response.ok`` reports success on a rejection. That is how conversation
  reordering appeared to save and did not.
* An empty list must say why it is empty. A blank box reads as a failure when
  it is simply the first run.
* A repeat add must not claim it added something. The server distinguishes
  ``added`` from ``already_members`` precisely so the UI can be honest.
* Existing members must be shown as members. Offering them again invites a
  click that does nothing and looks broken.

The picker is driven in a headless browser with fetch stubbed, and the tests
assert what was actually requested. Source inspection cannot answer "did this
send the right thing", which is the question that matters here.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR_JS = REPO / "web" / "assets" / "supervisor" / "main.js"
SUPERVISOR_HTML = REPO / "web" / "supervisor.html"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def _driver_status() -> tuple[bool, str]:
    """Whether playwright's own node driver can start, and why not if it can't.

    Same check as tests/test_frontend_browser.py and the UX-shortcuts suite,
    and for the same reason: importing playwright proves nothing, because it
    shells out to a node binary it ships itself. Guarding on the import alone
    makes every test here raise FileNotFoundError on a machine without node
    instead of skipping.
    """
    try:
        from playwright._impl._driver import compute_driver_executable
    except Exception as exc:  # noqa: BLE001
        return False, f"playwright not importable: {exc.__class__.__name__}"
    try:
        parts = compute_driver_executable()
    except Exception as exc:  # noqa: BLE001
        return False, f"driver path unresolvable: {exc.__class__.__name__}"
    for path in (parts if isinstance(parts, (list, tuple)) else [parts]):
        if not os.path.exists(path):
            return False, f"driver missing: {path} (try: playwright install)"
    return True, "ok"


DRIVER_OK, DRIVER_WHY = _driver_status()

# Marks the section this feature owns, so it can be lifted out of the file
# without dragging in the goal-banner and scheduler code around it.
SECTION_START = "// ── Members ─"
SECTION_END = "// ── Members end ─"


class ProbeFailed(AssertionError):
    """The browser never produced a title. Raised rather than returned.

    The old helper returned ``""`` on failure and the caller wrote
    ``json.loads(run_page(...) or "{}")``, so a browser that answered nothing
    became a successful parse of an empty dict. Every assertion downstream then
    failed on missing keys -- "an empty picker must explain itself, got ''" --
    which describes the picker and not the browser, and sent three sessions
    looking at the markup. An instrument that cannot answer must say so.
    """


def run_page(html: str, budget_ms: int = 20000) -> str:
    """Render *html* and return its ``document.title``.

    Driven through playwright rather than ``chromium --dump-dom``, which does
    not work on this page. Measured on chromium 148.0.7778.178: a trivial page
    dumps in 0.6s, `web/supervisor.html` alone dumps in 0.6s, and the same
    markup plus `web/supervisor.js` never exits at all -- rc=124 under
    `--headless`, `--headless=old` and `--headless=new` alike, with no output
    and no stderr. Bisected to the 30-second `setInterval` that `init()`
    installs: neutralise that one call and the identical page dumps in 1.0s.
    So `--virtual-time-budget` never retires while that timer is outstanding,
    and the budget is what `--dump-dom` waits on.

    Playwright drives the same chromium binary and renders the same page in
    under four seconds, because it asks the DevTools protocol for the DOM
    instead of depending on virtual time to expire. It also owns its own
    profile directory, which retires the 126 MB-per-launch leak this file used
    to cause -- a single run left 11 of them and filled a 1.9 GB tmpfs, after
    which every browser test in the suite failed on a timeout and leaked
    another.
    """
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "probe.html"
        page_file.write_text(html, encoding="utf-8")
        with sync_playwright() as pw:
            # The system chromium, as the other playwright suites do. Without
            # executable_path playwright looks for a browser it downloads
            # itself, which is not installed here.
            browser = pw.chromium.launch(
                executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"file://{page_file}")
                try:
                    # The harness signals completion by setting document.title
                    # as its last act, so that is the wait -- not a fixed sleep,
                    # which is what made the old suite flaky under load.
                    page.wait_for_function(
                        "document.title.length > 0", timeout=budget_ms)
                except PWTimeout:
                    raise ProbeFailed(
                        "page never set document.title within "
                        f"{budget_ms}ms; page errors: {errors or 'none'}"
                    ) from None
                return page.title()
            finally:
                browser.close()


def _lift(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    # De-indent from inside the file's IIFE so it runs at top level.
    return source[start:end].replace("\n  ", "\n")


def members_section() -> str:
    """The picker, the panel, AND the page's own getCsrf/apiFetch.

    Lifting the real apiFetch is the whole point. The first version of this
    harness supplied its own, modelled on app.js's -- which returns a Response
    and resolves for 4xx. supervisor.js's returns the PARSED BODY and throws on
    a non-2xx. Every `if (!r.ok)` in the picker was therefore checking a
    property that does not exist, and fired on success.

    The harness also defined showToast(), which this page does not have at all,
    so every call threw ReferenceError -- including the one in the catch.

    Both bugs shipped green: thirteen passing tests against a contract the file
    never had. A harness may stub the boundary (fetch), never the code under
    test's own collaborators.
    """
    source = SUPERVISOR_JS.read_text(encoding="utf-8")
    if SECTION_START not in source:
        raise unittest.SkipTest("members section not implemented yet")
    return (
        "let csrfToken = '';\n"
        + _lift(source, "function getCsrf()", "// ── API helpers")
        + _lift(source, "async function apiFetch(", "// Every timestamp on this page")
        + _lift(source, SECTION_START, SECTION_END)
    )


AGENTS = [
    {"sessionId": "sess-cweb1", "name": "cweb1", "status": "busy"},
    {"sessionId": "sess-cweb5", "name": "cweb5", "status": "idle"},
]
CHATS = [
    {"id": "chat-a", "title": "conversation1"},
    {"id": "chat-b", "title": "qa work"},
]


class MarkupTests(unittest.TestCase):
    """The page must offer the control and somewhere to render into."""

    def setUp(self):
        if not SUPERVISOR_HTML.exists():
            self.skipTest("supervisor.html not present")
        self.html = SUPERVISOR_HTML.read_text(encoding="utf-8")

    def test_the_page_offers_an_add_members_control(self):
        self.assertIn("addMembersBtn", self.html)

    def test_the_page_has_a_container_for_the_members_panel(self):
        self.assertIn("membersPanel", self.html)


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipUnless(CHROMIUM, "chromium not installed")
class PickerTests(unittest.TestCase):
    """Drive the real picker; assert the request it produces."""

    def _harness(self, *, members=(), add_response=None, add_ok=True,
                 clicks="all", filter_text=None):
        """Open the picker, tick some boxes, submit, and report what happened."""
        section = members_section()
        return f"""
        <body>
          <button id="addMembersBtn">Add members</button>
          <div id="membersPanel"></div>
        <script>
        let lastToast = null, posted = null, refreshed = 0;
        // alert() is what this page uses to report a failure, so that is what
        // the test captures. Nothing else is stubbed above the network.
        window.alert = (msg) => {{ lastToast = 'error:' + msg; }};
        const SUPERVISOR_ID = "sup1";
        // Only the boundary is faked. getCsrf and apiFetch are the page's own,
        // lifted verbatim, so the test exercises the real contract.
        document.cookie = "wc_csrf=t";
        const reply = (body, ok) => Promise.resolve({{
          ok: ok, status: ok ? 200 : 400, json: async () => body,
        }});
        window.fetch = async (url, opts) => {{
          if (url === '/api/sessions') return reply({{sessions: {json.dumps(AGENTS)}}}, true);
          if (url === '/api/chats') return reply({{chats: {json.dumps(CHATS)}}}, true);
          if (url.endsWith('/members') && (!opts || !opts.method || opts.method === 'GET')) {{
            refreshed += 1;
            return reply({{members: {json.dumps(list(members))}, count: {len(members)}}}, true);
          }}
          if (opts && opts.method === 'POST') {{
            posted = {{url, body: JSON.parse(opts.body)}};
            return reply({json.dumps(add_response or {"added": ["chat-a"], "already_members": [], "failed": []})}, {str(add_ok).lower()});
          }}
          return reply({{}}, true);
        }};
        {section}
        (async () => {{
          await openMembersPicker(SUPERVISOR_ID);
          const dialog = document.getElementById('membersPickerDialog');
          const boxes = dialog ? [...dialog.querySelectorAll('input[type=checkbox]')] : [];
          const filter = dialog ? dialog.querySelector('input[type=search]') : null;
          {"if (filter) { filter.value = " + json.dumps(filter_text) + "; filter.dispatchEvent(new Event('input')); }" if filter_text else ""}
          const visible = boxes.filter(b => b.closest('label').offsetParent !== null
                                            || !b.closest('label').hidden);
          const enabled = boxes.filter(b => !b.disabled);
          {"enabled.forEach(b => { b.checked = true; });" if clicks == "all" else ""}
          // Captured here, not at the start: openMembersPicker itself reads
          // the members endpoint to learn existing membership, so an absolute
          // count is already >= 1 before anything is added. Measuring the
          // increase is what actually tests the refresh -- the first version
          // asserted refreshed >= 1 and passed with the refresh deleted.
          const refreshedBefore = refreshed;
          const submit = dialog ? dialog.querySelector('[data-action=confirm-members]') : null;
          if (submit) submit.click();
          // Wait for the click's promise chain to settle rather than sleeping a
          // fixed 60ms. Under a full-suite run several browser tests compete for
          // CPU, and the fixed wait expired before the POST resolved -- the test
          // passed alone and failed in the suite, which reads as tree churn and
          // is not. Deadline generous, exit as soon as it has landed.
          // Poll for completion rather than sleeping a fixed 60ms. Under a
          // full-suite run several browser tests compete for CPU and the fixed
          // wait expired before the promise chain finished -- the test passed
          // alone and failed in the suite, which reads as tree churn and is not.
          //
          // The signal is the dialog closing, which submitMembers does last.
          // `posted` is the wrong signal: the stub sets it synchronously, so
          // polling on it exits before the response is even read.
          const sleep = (ms) => new Promise(r => setTimeout(r, ms));
          const gone = () => !document.getElementById('membersPickerDialog');
          for (let i = 0; i < 200 && !gone(); i++) await sleep(25);
          // Then let the panel refresh that follows the close actually land.
          for (let i = 0; i < 80 && posted !== null && refreshed === refreshedBefore; i++) {{
            await sleep(25);
          }}
          const help = dialog ? dialog.querySelector('p') : null;
          document.title = JSON.stringify({{
            offered: boxes.length,
            disabled: boxes.length - enabled.length,
            shown: visible.length,
            checkedByDefault: boxes.filter(b => b.checked).length,
            posted: posted,
            toast: lastToast || (document.querySelector('.members-note')
                                 ? 'ok:' + document.querySelector('.members-note').textContent
                                 : null),
            refreshed: refreshed,
            refreshedAfterAdd: refreshed - refreshedBefore,
            stillOpen: !!document.getElementById('membersPickerDialog'),
            help: help ? help.textContent : '',
          }});
        }})();
        </script></body>
        """

    def _run(self, **kwargs):
        # No `or "{}"` fallback: run_page raises when the browser gives it
        # nothing, and swallowing that turned a dead browser into a picker
        # that "rendered no text". Let the real failure surface.
        return json.loads(run_page(self._harness(**kwargs)))

    def test_it_offers_both_agents_and_conversations(self):
        result = self._run(clicks="none")
        self.assertEqual(result.get("offered"), 4,
                         "two live agents and two conversations must both be listed")

    def test_a_session_is_sent_as_a_session_and_a_chat_as_a_chat(self):
        """The server adopts a session into a chat; it must know which is which."""
        result = self._run()
        sent = result["posted"]["body"]["members"]
        kinds = {m["ref_id"]: m["kind"] for m in sent}
        self.assertEqual(kinds["sess-cweb1"], "session")
        self.assertEqual(kinds["sess-cweb5"], "session")
        self.assertEqual(kinds["chat-a"], "chat")
        self.assertEqual(kinds["chat-b"], "chat")

    def test_everything_selected_goes_in_one_request(self):
        """A request per item could half-apply and leave an unknown membership."""
        result = self._run()
        self.assertEqual(result["posted"]["url"], "/api/supervisors/sup1/members")
        self.assertEqual(len(result["posted"]["body"]["members"]), 4)

    def test_existing_members_are_shown_as_members_and_cannot_be_re_added(self):
        """Offering a member again invites a click that does nothing."""
        result = self._run(
            members=[{"kind": "chat", "id": "chat-a", "title": "conversation1",
                      "status": "idle"}],
            clicks="none")
        self.assertGreaterEqual(result.get("disabled", 0), 1,
                                "an existing member must not be selectable again")
        self.assertGreaterEqual(result.get("checkedByDefault", 0), 1,
                                "an existing member must be shown ticked")

    def test_the_filter_narrows_the_list(self):
        result = self._run(filter_text="cweb5", clicks="none")
        self.assertLess(result.get("shown", 99), 4,
                        "typing a filter must hide the rows that do not match")

    def test_a_rejected_add_is_reported_as_an_error(self):
        result = self._run(add_ok=False, add_response={"error": "Supervisor not found"})
        self.assertTrue(str(result.get("toast", "")).startswith("error:"),
                        f"a 400 must surface as an error, got {result.get('toast')!r}")
        self.assertIn("Supervisor not found", str(result.get("toast")))

    def test_an_add_that_changed_nothing_says_so(self):
        result = self._run(add_response={"added": [], "already_members": ["chat-a"],
                                         "failed": []})
        self.assertNotIn("error:", str(result.get("toast", "")))
        self.assertIn("lready", str(result.get("toast", "")),
                      "a repeat add must not claim it added something")

    def test_a_partial_failure_reports_both_halves(self):
        """Nine added and one refused must say so, not silently drop the one."""
        result = self._run(add_response={
            "added": ["chat-a", "chat-b"], "already_members": [],
            "failed": [{"ref_id": "sess-cweb5", "error": "Agent could not be adopted"}]})
        toast = str(result.get("toast", ""))
        self.assertIn("2", toast, "the successful count must be reported")
        self.assertIn("1", toast, "the failure must be reported too")

    def test_a_successful_add_closes_and_refreshes_the_panel(self):
        result = self._run()
        self.assertFalse(result.get("stillOpen"), "the dialog must close on success")
        self.assertGreaterEqual(
            result.get("refreshedAfterAdd", 0), 1,
            "the members panel must reload AFTER adding, not merely have been "
            "read when the picker opened")

    def test_nothing_selected_sends_no_request(self):
        """An empty POST would be a 400 the user did nothing to deserve."""
        result = self._run(clicks="none")
        self.assertIsNone(result.get("posted"),
                          "confirming with nothing ticked must not call the API")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipUnless(CHROMIUM, "chromium not installed")
class EmptyStateTests(unittest.TestCase):
    """An empty box reads as a failure; the first run is not one."""

    def test_it_explains_an_empty_list(self):
        section = members_section()
        html = f"""
        <body><button id="addMembersBtn"></button><div id="membersPanel"></div>
        <script>
        // Same rule as the other harness: stub the network, never the page's
        // own apiFetch. Faking it is what let two contract bugs ship green.
        window.alert = () => {{}};
        document.cookie = "wc_csrf=t";
        window.fetch = async (url) => ({{
          ok: true, status: 200,
          json: async () =>
            url === '/api/sessions' ? {{sessions: []}}
            : url === '/api/chats' ? {{chats: []}}
            : {{members: [], count: 0}},
        }});
        {section}
        (async () => {{
          await openMembersPicker('sup1');
          const dialog = document.getElementById('membersPickerDialog');
          document.title = dialog ? dialog.textContent : 'no dialog';
        }})();
        </script></body>
        """
        title = run_page(html)
        self.assertRegex(title, r"[Nn]othing|[Nn]o conversations|[Nn]o agents",
                         f"an empty picker must explain itself, got {title!r}")


if __name__ == "__main__":
    unittest.main()
