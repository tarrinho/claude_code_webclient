"""QA coverage for a finished turn saying so.

A completed turn used to announce itself by disappearing. `setStreamState`
set the composer status to `''` the moment the state returned to `ready`, so
the only evidence the work had ended was the absence of "Responding…" -- which
reads exactly the same as a turn that never started. On a phone, where the
reply itself may be scrolled off, there was nothing on screen that said the
action had finished.

The status line now says "Finished", with how long the turn ran when that is
known. The distinction that matters is the one this file spends most of its
assertions on: it must fire on the way DOWN from an active state and at no
other time, or simply opening a conversation would claim that something had
just completed.

Driven through a real browser because the behaviour is a DOM effect of a
module-private state machine; asserting on the source text would pass against
a version that never writes to the element.
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment without playwright
    sync_playwright = None


def _driver_ok() -> tuple[bool, str]:
    """Whether playwright's own node driver can start, and why not if it can't."""
    if sync_playwright is None:
        return False, "playwright not installed"
    try:
        from playwright._impl._driver import compute_driver_executable
    except Exception as exc:  # noqa: BLE001
        return False, f"playwright not importable: {exc.__class__.__name__}"
    try:
        # Private helper; it is the only way to learn whether the bundled node
        # is actually present without paying for a browser launch.
        path = compute_driver_executable()
        path = path[0] if isinstance(path, (tuple, list)) else path
    except Exception as exc:  # noqa: BLE001
        return False, f"driver path unavailable: {exc.__class__.__name__}"
    if not Path(path).exists():
        return False, f"driver missing: {path} (try: playwright install)"
    return True, ""


DRIVER_OK, DRIVER_WHY = _driver_ok()
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

# Builds a controller against stub elements and returns the composer status
# after each transition, so the assertions read as a sequence of states.
HARNESS = """
async (transitions) => {
  const mod = await import('/assets/conversation.js');
  const make = () => ({
    textContent: '', dataset: {}, style: {}, disabled: false,
    classList: {toggle(){}, add(){}, remove(){}},
    setAttribute(){}, addEventListener(){}, removeEventListener(){},
    hidden: false, scrollHeight: 20, value: '',
  });
  const elements = {
    messages: Object.assign(make(), {addEventListener(){}}),
    composerInput: make(), modelPicker: make(), sendButton: make(),
    retryButton: make(), jumpButton: make(), runState: make(),
    composerStatus: make(), queueBar: make(), queueList: make(),
    queueTag: make(), queueNote: make(),
  };
  const state = {};
  const controller = mod.createConversationController({
    state, elements,
    apiFetch: async () => ({ok: true, json: async () => ({})}),
    storageGet: () => null, storageSet(){}, storageRemove(){},
    showToast(){}, onChatLoaded(){}, refreshChats: async () => {},
  });
  const seen = [];
  for (const [next, waitMs] of transitions) {
    if (waitMs) await new Promise(r => setTimeout(r, waitMs));
    controller.setStreamState(next);
    seen.push({state: next, status: elements.composerStatus.textContent,
               runState: elements.runState.textContent});
  }
  return seen;
}
"""


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipUnless(CHROMIUM, "no chromium binary")
class TurnFinishedTests(unittest.TestCase):
    """The module is served over http:// so its imports resolve."""

    @classmethod
    def setUpClass(cls):
        import functools
        import http.server
        import socketserver
        import threading

        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "web")
        )
        socketserver.TCPServer.allow_reuse_address = True
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            executable_path=CHROMIUM, args=["--no-sandbox"]
        )
        self.page = self.browser.new_page()
        self.page.goto(f"http://127.0.0.1:{self.port}/index.html")

    def tearDown(self):
        self.browser.close()
        self._pw.stop()

    def run_transitions(self, transitions):
        return self.page.evaluate(HARNESS, json.dumps(transitions) and transitions)

    def test_a_completed_turn_says_it_finished(self):
        seen = self.run_transitions([["responding", 0], ["ready", 0]])
        self.assertTrue(
            seen[-1]["status"].startswith("Finished"),
            f"a finished turn must say so, got {seen[-1]['status']!r}",
        )

    def test_opening_a_conversation_claims_nothing_finished(self):
        """ready -> ready is what happens on load; it completed nothing."""
        seen = self.run_transitions([["ready", 0]])
        self.assertEqual(seen[-1]["status"], "")

    def test_a_turn_that_never_started_claims_nothing(self):
        seen = self.run_transitions([["ready", 0], ["ready", 0]])
        self.assertEqual(seen[-1]["status"], "")

    def test_the_duration_is_reported_once_a_second_has_passed(self):
        seen = self.run_transitions([["responding", 0], ["ready", 1100]])
        self.assertRegex(
            seen[-1]["status"], r"^Finished · \d+s$",
            "a turn that ran for over a second should say how long",
        )

    def test_a_sub_second_turn_reports_no_misleading_zero(self):
        seen = self.run_transitions([["responding", 0], ["ready", 0]])
        self.assertEqual(seen[-1]["status"], "Finished",
                         "'Finished · 0s' would be noise")

    def test_the_clock_starts_when_the_turn_did_not_at_its_last_step(self):
        """connecting -> thinking -> responding is one turn, not three."""
        seen = self.run_transitions(
            [["connecting", 0], ["thinking", 600], ["responding", 600], ["ready", 0]]
        )
        self.assertRegex(seen[-1]["status"], r"^Finished · 1s$")

    def test_a_stopped_turn_still_reports_that_it_ended(self):
        seen = self.run_transitions([["responding", 0], ["stopped", 0]])
        self.assertEqual(seen[-1]["status"], "Stopped")

    def test_a_failed_turn_still_reports_that_it_ended(self):
        seen = self.run_transitions([["responding", 0], ["failed", 0]])
        self.assertEqual(seen[-1]["status"], "Failed")

    def test_a_second_turn_is_timed_from_its_own_start(self):
        """The first turn's clock must not leak into the second."""
        seen = self.run_transitions(
            [["responding", 0], ["ready", 1100], ["responding", 0], ["ready", 0]]
        )
        self.assertRegex(seen[1]["status"], r"^Finished · 1s$")
        self.assertEqual(seen[-1]["status"], "Finished",
                         "the second turn was instant and must say so")

    def test_the_run_state_still_reads_ready(self):
        """The badge tracks the machine; only the status line narrates."""
        seen = self.run_transitions([["responding", 0], ["ready", 0]])
        self.assertEqual(seen[-1]["runState"], "Ready")


if __name__ == "__main__":
    unittest.main()
