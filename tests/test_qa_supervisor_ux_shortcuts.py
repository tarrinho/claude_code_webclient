"""QA: the five supervisor UX affordances, driven in a real browser.

Keyboard shortcuts, expandable task rows, the unread badge, the status pulse
and the goal banner's slim strip were added together, and the first cut of each
was wired up in a way that read correctly and did not work:

* the bare ``1``-``4`` panel keys had no typing guard, so every digit typed
  into the composer threw focus at a panel -- the composer could not be used
  for any prompt containing a number;
* those keys called ``.focus()`` on plain ``<div>``s, a silent no-op without
  ``tabindex``, so three of the four did nothing even outside the composer;
* the restore arrow on the slim goal banner cleared the shrink flag but not the
  *reason* for it, so the next streamed message immediately re-shrank the
  banner the user had just expanded by hand;
* the unread badge counted log events only, missing chat messages -- the one
  thing you actually miss while scrolled up;
* the expand toggle's CSS was scoped under ``.task-detail-row``, but the button
  lives in ``.task-item``, a *sibling* of that row, so the selector matched
  nothing and the control rendered as bare default chrome.

Every one of those passes a source-substring test. The call is present, the
listener is attached, the class is in the stylesheet. So these tests drive the
real page in a real browser and assert on what the DOM ends up looking like.
``supervisor.js`` is an IIFE, so nothing inside is reachable by name -- which is
the point. The only handles used are the ones a user has: clicks, keystrokes and
scrolls, plus the stream, reached through the ``window._supervisorSSE`` the page
already exports.

Rewritten onto playwright. The first version drove ``chromium --headless
--dump-dom`` directly and shared one browser launch across all 28 tests, which
was wrong twice over. It depended on Chromium choosing to exit -- and once it
stopped doing so on this page (verified as a harness fault, not a page one: the
HTML alone exits in 1s, the HTML plus ``supervisor.js`` hangs, and the two files
were byte-identical to when the suite passed) every test failed on a 180s
timeout. And because one probe fed all of them, a single hang reported 27
failures with one cause, which is the opposite of what a test suite is for. A
page per test costs a second and localises the blame.

No server: ``page.route`` fulfils the document, the script and the API from
fixtures here, so the suite is hermetic and the task list is whatever a test
needs rather than whatever a database happens to hold.
"""
from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised only without the dev deps
    sync_playwright = None


def _driver_status() -> tuple[bool, str]:
    """Whether playwright's own node driver can start, and why not if it can't.

    Same check as tests/test_frontend_browser.py, and for the same reason:
    importing playwright proves nothing, because it shells out to a node binary
    it ships itself. Guarding on the import alone makes every test here raise
    FileNotFoundError on a machine without node instead of skipping.
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

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_HTML = ROOT / "web" / "supervisor.html"
SUPERVISOR_JS = ROOT / "web" / "supervisor.js"
CHROMIUM = (shutil.which("chromium") or shutil.which("chromium-browser")
            or shutil.which("google-chrome"))

# An origin that does not exist. Every request is intercepted, so nothing is
# ever sent; a real host here would mean a test could quietly depend on it.
ORIGIN = "http://supervisor.test"

SUPERVISOR_ID = "sup-1111"
SUPS = [{"id": SUPERVISOR_ID, "title": "Ship the thing", "status": "running",
         "progress_pct": 40, "created_at": "2026-09-01T09:00:00Z",
         "updated_at": "2026-09-01T09:30:00Z"}]
TASKS = [
    {"id": "t1", "title": "First task", "status": "done", "progress_pct": 100,
     "result": "found 42 files", "model": "sonnet", "depends_on": []},
    {"id": "t2", "title": "Second task", "status": "running",
     "progress_pct": 50, "result": None, "model": "opus", "depends_on": ["t1"]},
]

# Replaces EventSource before any page script runs. supervisor.js publishes the
# stream as window._supervisorSSE, which is how a test pushes a frame.
STUB_SSE = """
window.EventSource = function (url) {
  this.url = url;
  this.readyState = 1;
  this.close = function () { this.readyState = 2; };
  this.addEventListener = function () {};
};
"""


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "chromium not installed")
class _SupervisorPage(unittest.TestCase):
    """Browser lifecycle and page fixtures. No tests of its own.

    Kept separate so each feature area can subclass it: subclassing a class
    that has test methods re-runs every one of them under the new name.
    """

    def setUp(self):
        # addCleanup, not tearDown: unittest skips tearDown when setUp raises,
        # and a leaked playwright is not merely untidy -- its greenlet loop
        # stays flagged as the running asyncio loop for this thread, so every
        # IsolatedAsyncioTestCase afterwards dies on "Runner.run() cannot be
        # called from a running event loop". Cleanups run last-registered-first,
        # so close() lands before stop().
        self.errors: list[str] = []
        self._pw = sync_playwright().start()
        self.addCleanup(self._pw.stop)
        self.browser = self._pw.chromium.launch(
            executable_path=CHROMIUM, args=["--no-sandbox"]
        )
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        self._install_routes()

    def _install_routes(self):
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        js = SUPERVISOR_JS.read_text(encoding="utf-8")

        def handler(route):
            url = route.request.url
            if "supervisor.js" in url:
                route.fulfill(status=200, body=js,
                              content_type="application/javascript")
            elif "/api/" in url:
                route.fulfill(status=200, body=json.dumps(self._api_body(url)),
                              content_type="application/json")
            elif url.rstrip("/").endswith("supervisor.html") or url == ORIGIN + "/":
                route.fulfill(status=200, body=html, content_type="text/html")
            else:
                route.fulfill(status=404, body="")

        self.page.route("**/*", handler)
        self.page.add_init_script(STUB_SSE)

    @staticmethod
    def _api_body(url: str) -> dict:
        if "/tasks" in url:
            return {"tasks": TASKS}
        if "/messages" in url:
            return {"messages": []}
        if "/api/supervisors/" in url:
            return {"supervisor": SUPS[0]}
        if "/api/supervisors" in url:
            return {"supervisors": SUPS}
        return {}

    def open_page(self, *, select=True):
        """Load the supervisor page, optionally selecting the one supervisor.

        Selecting is what starts the stream and renders the task tree, so most
        areas need it; the load assertions want the state before it.
        """
        self.page.goto(f"{ORIGIN}/supervisor.html", wait_until="domcontentloaded")
        self.page.wait_for_selector(".supervisor-list-item", timeout=10_000)
        if select:
            self.page.click(".supervisor-list-item .sl-title")
            # The task tree arriving is the readiness signal: it means
            # showActiveSupervisor() has resolved and connectSSE() has run.
            self.page.wait_for_selector(".task-item", timeout=10_000)

    # ── Driving ─────────────────────────────────────────────────────────

    def push(self, frame: dict):
        """Deliver one SSE frame, the way the live stream would."""
        self.page.evaluate(
            """(frame) => {
                const es = window._supervisorSSE;
                if (!es) throw new Error('no SSE stream: supervisor not selected');
                if (!es.onmessage) throw new Error('stream has no onmessage');
                es.onmessage({ data: JSON.stringify(frame) });
            }""", frame)

    def push_message(self, text: str):
        self.push({"type": "messages", "messages": [
            {"role": "assistant", "content": text,
             "created_at": "2026-09-01T10:00:00Z"}]})

    def send_prompt(self, text: str):
        self.page.fill("#prompt-input", text)
        self.page.click("#send-btn")
        self.page.wait_for_selector("#goal-banner:not([hidden])", timeout=5_000)

    def scroll_up(self):
        """Park the chat away from the bottom so isNearBottom() reports false."""
        self.page.evaluate(
            """() => {
                const c = document.querySelector('#chat-messages');
                c.style.height = '40px';
                c.style.overflowY = 'scroll';
                c.scrollTop = 0;
                c.dispatchEvent(new Event('scroll'));
            }""")

    def scroll_bottom(self):
        self.page.evaluate(
            """() => {
                const c = document.querySelector('#chat-messages');
                c.scrollTop = c.scrollHeight;
                c.dispatchEvent(new Event('scroll'));
            }""")

    # ── Reading ─────────────────────────────────────────────────────────

    def active_id(self) -> str | None:
        return self.page.evaluate(
            "() => { const a = document.activeElement;"
            " return a ? (a.id || a.tagName.toLowerCase()) : null; }")

    def badge(self) -> dict:
        return self.page.evaluate(
            """() => { const b = document.querySelector('#topbar-badge');
                 return { hidden: !!b.hidden,
                          visible: b.classList.contains('visible'),
                          text: b.textContent }; }""")

    def has_class(self, selector: str, name: str) -> bool:
        return self.page.evaluate(
            "([s, n]) => { const e = document.querySelector(s);"
            " if (!e) throw new Error('no element: ' + s);"
            " return e.classList.contains(n); }", [selector, name])

    def is_hidden(self, selector: str) -> bool:
        return self.page.evaluate(
            "(s) => !!document.querySelector(s).hidden", selector)

    def press_bare(self, key: str) -> bool:
        """Dispatch a bare key at the document; True if the page claimed it.

        Synthetic rather than page.keyboard.press because the assertion is about
        defaultPrevented, which a real keypress does not report back.
        """
        return self.page.evaluate(
            """(key) => {
                const event = new KeyboardEvent('keydown',
                    { key, bubbles: true, cancelable: true });
                return !document.dispatchEvent(event);
            }""", key)


class PageLoadTests(_SupervisorPage):
    """Nothing below means anything if the page throws on the way up."""

    def test_the_page_loads_without_script_errors(self):
        self.open_page()
        self.assertEqual(self.errors, [])

    def test_the_new_elements_are_in_the_dom(self):
        self.open_page(select=False)
        self.assertIsNotNone(self.page.query_selector("#topbar-badge"))
        self.assertIsNotNone(self.page.query_selector(".goal-banner-restore"))

    def test_selecting_a_supervisor_starts_the_stream(self):
        """Guards every area below: without a stream there is nothing to push,
        and the badge and banner assertions would be vacuous."""
        self.open_page()
        self.assertTrue(self.page.evaluate("() => !!window._supervisorSSE"))


class KeyboardShortcutTests(_SupervisorPage):
    """The panel keys, and the composer they must not break."""

    def test_a_digit_typed_into_the_composer_does_not_move_focus(self):
        """The regression proper.

        Without a typing guard the shortcut fires on every keystroke that
        reaches document, so typing "step 1 of 4" threw focus at the task tree
        mid-word and left the composer unusable for any prompt with a number in
        it -- a worse bug than the missing shortcut. Typed with real keystrokes,
        because that is the thing that was broken.
        """
        self.open_page()
        self.page.click("#prompt-input")
        self.page.keyboard.type("step 1 of 4")
        self.assertEqual(self.active_id(), "prompt-input",
                         "a digit typed into the composer stole focus from it")
        self.assertEqual(
            self.page.input_value("#prompt-input"), "step 1 of 4",
            "the composer did not receive the digits it was typed")

    def test_a_digit_outside_a_text_field_focuses_the_panel(self):
        """Guards the test above: a guard that let nothing through at all would
        satisfy it trivially."""
        self.open_page()
        self.page.evaluate("() => document.body.focus()")
        self.page.keyboard.press("1")
        self.assertEqual(self.active_id(), "task-tree")

    def test_each_panel_key_focuses_its_own_panel(self):
        """These are plain divs, and .focus() on a div without tabindex is a
        silent no-op, so three of the four shortcuts did nothing at all."""
        self.open_page()
        for key, expected in (("2", "chat-messages"),
                              ("3", "event-log"),
                              ("4", "prompt-input")):
            with self.subTest(key=key):
                self.page.evaluate("() => document.body.focus()")
                self.page.keyboard.press(key)
                self.assertEqual(self.active_id(), expected)

    def test_a_modified_digit_is_not_a_panel_shortcut(self):
        """Ctrl+1 is the browser's own tab switch, not ours to take."""
        self.open_page()
        self.page.evaluate("() => document.body.focus()")
        self.page.keyboard.press("Control+1")
        self.assertNotEqual(self.active_id(), "task-tree")

    def test_the_page_claims_only_the_digits_it_handles(self):
        self.open_page()
        self.assertTrue(self.press_bare("1"),
                        "a handled shortcut must preventDefault")
        self.assertFalse(self.press_bare("9"),
                         "an unmapped key must be left to the browser")


class GoalBannerSlimStripTests(_SupervisorPage):
    """Shrink to a slim strip, and the restore arrow that undoes it."""

    def test_sending_a_prompt_shows_the_goal_expanded(self):
        self.open_page()
        self.send_prompt("Refactor the parser")
        self.assertFalse(self.has_class("#goal-banner", "slim"),
                         "goal banner started slimmed")
        self.assertTrue(self.is_hidden(".goal-banner-restore"),
                        "restore arrow shows while the banner is expanded")

    def test_a_streamed_message_shrinks_it(self):
        self.open_page()
        self.send_prompt("Refactor the parser")
        self.push_message("working on it")
        self.assertTrue(self.has_class("#goal-banner", "slim"))
        self.assertFalse(self.is_hidden(".goal-banner-restore"),
                         "slimmed with no visible way to expand it again")

    def test_the_restore_arrow_expands_it(self):
        self.open_page()
        self.send_prompt("Refactor the parser")
        self.push_message("working on it")
        self.page.click(".goal-banner-restore")
        self.assertFalse(self.has_class("#goal-banner", "slim"))

    def test_the_restore_survives_the_next_message(self):
        """The regression proper.

        restoreGoalBanner cleared the shrink flag but not the reason for it, so
        the very next streamed message re-shrank the banner the user had just
        expanded. Every individual step passed -- the click fired, the class
        came off, the listener was wired -- and the control still appeared to do
        nothing, because on a live supervisor the next message is a second away.
        """
        self.open_page()
        self.send_prompt("Refactor the parser")
        self.push_message("working on it")
        self.page.click(".goal-banner-restore")
        self.push_message("still working")
        self.assertFalse(
            self.has_class("#goal-banner", "slim"),
            "the message after a manual restore re-shrank the banner")

    def test_a_new_goal_starts_expanded(self):
        """Otherwise the sticky restore leaks into the next goal, which the
        user has not seen yet and has expressed no opinion about."""
        self.open_page()
        self.send_prompt("Refactor the parser")
        self.push_message("working on it")
        self.page.click(".goal-banner-restore")
        self.send_prompt("Now ship it")
        self.assertFalse(self.has_class("#goal-banner", "slim"))
        self.assertEqual(self.page.inner_text("#goal-text"), "Now ship it")

    def test_escape_dismisses_the_banner(self):
        self.open_page()
        self.send_prompt("Refactor the parser")
        self.page.keyboard.press("Escape")
        self.assertTrue(self.is_hidden("#goal-banner"))


class UnreadBadgeTests(_SupervisorPage):
    """The badge counts what is off screen, and nothing else."""

    def test_it_starts_empty(self):
        self.open_page()
        self.assertTrue(self.badge()["hidden"])

    def test_nothing_is_counted_while_parked_at_the_bottom(self):
        """A badge for content already in front of the user is just a chore."""
        self.open_page()
        self.push_message("one")
        self.assertTrue(self.badge()["hidden"],
                        "counted a message the user was looking at")

    def test_a_message_missed_while_scrolled_up_is_counted(self):
        """The regression proper: the badge counted log events only.

        A chat message arriving while you are scrolled up is the single thing
        the badge exists for, and it was the one thing that did not increment
        it.
        """
        self.open_page()
        self.scroll_up()
        self.push_message("two")
        state = self.badge()
        self.assertFalse(state["hidden"], "a missed message was not counted")
        self.assertTrue(state["visible"], "badge counted but stayed invisible")
        self.assertEqual(state["text"], "1")

    def test_further_traffic_accumulates(self):
        self.open_page()
        self.scroll_up()
        self.push_message("two")
        self.push_message("three")
        self.push({"type": "events", "events": [
            {"type": "task_start", "task_id": "t2",
             "data": {"title": "Second task"}}]})
        self.assertEqual(self.badge()["text"], "3")

    def test_returning_to_the_bottom_clears_it(self):
        self.open_page()
        self.scroll_up()
        self.push_message("two")
        self.assertEqual(self.badge()["text"], "1", "fixture must count first")
        self.scroll_bottom()
        state = self.badge()
        self.assertTrue(state["hidden"], "badge survived catching up")
        self.assertFalse(state["visible"])


class ExpandableTaskRowTests(_SupervisorPage):
    """The per-task expand toggle."""

    def test_every_task_gets_a_toggle_and_a_detail_row(self):
        self.open_page()
        self.assertIsNotNone(
            self.page.query_selector('.expand-toggle[data-expand="t1"]'))
        self.assertEqual(
            len(self.page.query_selector_all(".task-detail-row")), len(TASKS))

    def test_rows_start_collapsed(self):
        self.open_page()
        self.assertEqual(
            len(self.page.query_selector_all(".task-detail-row.expanded")), 0)

    def test_the_toggle_is_styled(self):
        """The rule was scoped under .task-detail-row, but the button sits in
        .task-item -- a *sibling* of that row, not an ancestor -- so it matched
        nothing and the control rendered as bare default chrome. Reading the
        stylesheet cannot catch that; asking the browser can."""
        self.open_page()
        cursor = self.page.eval_on_selector(
            '.expand-toggle[data-expand="t1"]',
            "el => getComputedStyle(el).cursor")
        self.assertEqual(cursor, "pointer",
                         "the .expand-toggle rule is not matching the button")

    def test_clicking_the_toggle_opens_that_row(self):
        self.open_page()
        self.page.click('.expand-toggle[data-expand="t1"]')
        self.assertTrue(self.has_class('.task-detail-row[data-detail="t1"]',
                                       "expanded"))
        self.assertIn("found 42 files",
                      self.page.inner_text('.task-detail-row[data-detail="t1"]'))

    def test_opening_a_row_does_not_select_the_task(self):
        """Expanding is not navigating: the detail panel must stay untouched.

        A behaviour lock, and deliberately labelled as one. The toggle sits
        inside the row's own click target, so it carries `stopPropagation()` and
        the row carries an `e.target.closest('.expand-toggle')` guard -- but
        mutation testing showed **neither is load-bearing**, and nor are both
        together: removing them does not fail this test. The reason is a third,
        accidental defence. The toggle handler calls `renderTaskTree()`, which
        replaces `#task-tree`'s innerHTML and destroys the row's listener before
        the click can bubble to it, so `selectTask` is unreachable on this path
        whatever the guards say.

        So this asserts the user-visible property rather than any one mechanism,
        which is the honest scope: if a future refactor stops re-rendering
        synchronously, the guards become load-bearing and this is what notices.
        Both observables are checked, because `.task-item.active` alone is set
        only by a re-render and would pass vacuously if selection ever became
        detail-only.
        """
        self.open_page()
        detail_before = self.page.inner_text("#detail-content")
        self.page.click('.expand-toggle[data-expand="t1"]')
        self.assertIsNone(self.page.query_selector(".task-item.active"),
                          "expanding a row also selected it")
        self.assertEqual(
            self.page.inner_text("#detail-content"), detail_before,
            "expanding a row populated the task detail panel")

    def test_only_one_row_is_open_at_a_time(self):
        self.open_page()
        self.page.click('.expand-toggle[data-expand="t1"]')
        self.page.click('.expand-toggle[data-expand="t2"]')
        self.assertTrue(self.has_class('.task-detail-row[data-detail="t2"]',
                                       "expanded"))
        self.assertFalse(self.has_class('.task-detail-row[data-detail="t1"]',
                                        "expanded"))

    def test_clicking_the_toggle_again_closes_the_row(self):
        self.open_page()
        self.page.click('.expand-toggle[data-expand="t2"]')
        self.assertTrue(self.has_class('.task-detail-row[data-detail="t2"]',
                                       "expanded"), "fixture must open it")
        self.page.click('.expand-toggle[data-expand="t2"]')
        self.assertFalse(self.has_class('.task-detail-row[data-detail="t2"]',
                                        "expanded"))

    def test_the_row_body_still_selects_the_task(self):
        """The guard against over-correcting: suppressing the row click
        entirely would pass every test above."""
        self.open_page()
        self.page.click('.task-item[data-task-id="t2"] .task-title')
        self.assertIsNotNone(self.page.query_selector(".task-item.active"))


class PanelFocusabilityTests(unittest.TestCase):
    """The panels the shortcuts target must be focusable at all.

    Structural, and deliberately not browser-gated: this is the property whose
    absence turns three shortcuts into silent no-ops, and it should fail on a
    machine with no browser too.
    """

    def setUp(self):
        self.html = SUPERVISOR_HTML.read_text(encoding="utf-8")

    def test_each_shortcut_target_carries_a_tabindex(self):
        for element_id in ("task-tree", "chat-messages", "event-log"):
            with self.subTest(element=element_id):
                tag = self.html.split(f'id="{element_id}"')[1].split(">")[0]
                self.assertIn(
                    "tabindex", tag,
                    f"#{element_id} is a div without tabindex, so .focus() on "
                    "it does nothing")


if __name__ == "__main__":
    unittest.main()
