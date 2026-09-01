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
listener is attached, the class is in the stylesheet. So these tests do not
grep the source: they load the real page in headless Chromium, dispatch real
events, and assert on what the DOM ends up looking like. ``supervisor.js`` is
an IIFE, so nothing inside is reachable by name -- which is the point. The only
handles used are the ones a user has: clicks, keystrokes and scrolls, plus the
stream, reached through the ``window._supervisorSSE`` the page already exports.

Chromium costs ~25s to start here and that is fixed overhead, not something the
virtual-time budget affects, so the whole file shares a single launch. Each
feature area runs inside its own try/catch and reports its own failure, so one
broken area does not erase the evidence from the others.
"""
from __future__ import annotations

import base64
import functools
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
SUPERVISOR_HTML = WEB / "supervisor.html"
SUPERVISOR_JS = WEB / "supervisor.js"

CHROMIUM = (shutil.which("chromium") or shutil.which("chromium-browser")
            or shutil.which("google-chrome"))

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

# Injected ahead of supervisor.js: the page must not reach the network from
# file://, and the fake EventSource is what lets a test push a stream frame.
STUBS = """
<script>
(function () {
  var SUPS = __SUPS__, TASKS = __TASKS__;
  window.fetch = function (url) {
    var u = String(url), body = {};
    if (u.indexOf('/tasks') !== -1) body = { tasks: TASKS };
    else if (u.indexOf('/messages') !== -1) body = { messages: [] };
    else if (/\\/api\\/supervisors\\/[^/?]+$/.test(u)) body = { supervisor: SUPS[0] };
    else if (u.indexOf('/api/supervisors') !== -1) body = { supervisors: SUPS };
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve(body); },
      text: function () { return Promise.resolve(JSON.stringify(body)); }
    });
  };
  function FakeES(url) {
    this.url = url; this.readyState = 1;
    this.close = function () { this.readyState = 2; };
    this.addEventListener = function () {};
  }
  window.EventSource = FakeES;
  window.__errors = [];
  window.addEventListener('error', function (e) {
    window.__errors.push(String(e.message));
  });
})();
</script>
"""

# The driver: helpers, the five feature areas, and the reporting channel.
#
# Areas run in sequence in one page. Order is deliberate -- the task and
# keyboard areas are independent, the goal area finishes by dismissing its
# banner, and the badge area needs a chat box it can scroll, so it goes last.
DRIVER = r"""
<script>
function $one(sel) { return document.querySelector(sel); }

function push(frame) {
  var es = window._supervisorSSE;
  if (!es) throw new Error('no SSE stream: supervisor was never selected');
  if (!es.onmessage) throw new Error('SSE stream has no onmessage handler');
  es.onmessage({ data: JSON.stringify(frame) });
}

function msgFrame(text) {
  return { type: 'messages', messages: [
    { role: 'assistant', content: text, created_at: '2026-09-01T10:00:00Z' }] };
}

function key(k, opts, target) {
  var init = { key: k, bubbles: true, cancelable: true };
  for (var p in (opts || {})) init[p] = opts[p];
  return (target || document).dispatchEvent(new KeyboardEvent('keydown', init));
}

function activeId() {
  var a = document.activeElement;
  return a ? (a.id || a.tagName.toLowerCase()) : null;
}

function badgeState() {
  var b = $one('#topbar-badge');
  return { hidden: !!b.hidden, visible: b.classList.contains('visible'),
           text: b.textContent };
}

function scrollUp() {
  var c = $one('#chat-messages');
  // Force a scrollable box, then park away from the bottom so the page's own
  // isNearBottom() reports false.
  c.style.height = '40px';
  c.style.overflowY = 'scroll';
  c.scrollTop = 0;
  c.dispatchEvent(new Event('scroll'));
}

function scrollBottom() {
  var c = $one('#chat-messages');
  c.scrollTop = c.scrollHeight;
  c.dispatchEvent(new Event('scroll'));
}

function settle() { return new Promise(function (r) { setTimeout(r, 60); }); }

// ── Areas ───────────────────────────────────────────────────────────────

function areaLoad() {
  return { errors: window.__errors.slice(),
           badgeInDom: !!$one('#topbar-badge'),
           restoreInDom: !!$one('.goal-banner-restore'),
           supervisorSelected: !!$one('.supervisor-list-item.active') };
}

function areaTasks() {
  var out = {};
  var toggle = $one('.expand-toggle[data-expand="t1"]');
  out.toggleExists = !!toggle;
  out.detailRows = document.querySelectorAll('.task-detail-row').length;
  out.expandedBefore = document.querySelectorAll('.task-detail-row.expanded').length;
  // If the CSS is scoped to the wrong ancestor the rule matches nothing and
  // this falls back to the button default rather than our pointer.
  out.toggleCursor = getComputedStyle(toggle).cursor;

  toggle.click();
  var opened = $one('.task-detail-row[data-detail="t1"]');
  out.expandedAfterClick = opened.classList.contains('expanded');
  out.resultShown = opened.textContent.indexOf('found 42 files') !== -1;
  out.activeAfterToggle = !!$one('.task-item.active');

  $one('.expand-toggle[data-expand="t2"]').click();
  out.t1StillOpen = $one('.task-detail-row[data-detail="t1"]')
    .classList.contains('expanded');
  out.t2Open = $one('.task-detail-row[data-detail="t2"]')
    .classList.contains('expanded');

  $one('.expand-toggle[data-expand="t2"]').click();
  out.t2AfterSecondClick = $one('.task-detail-row[data-detail="t2"]')
    .classList.contains('expanded');

  $one('.task-item[data-task-id="t2"] .task-title').click();
  out.activeAfterRowClick = !!$one('.task-item.active');
  return out;
}

function areaKeys() {
  var out = {};
  var composer = $one('#prompt-input');

  composer.focus();
  composer.value = 'step 1 of 4';
  key('1', {}, composer);
  out.afterDigitInComposer = activeId();

  document.body.focus();
  key('1');
  out.afterDigitOnBody = activeId();
  key('2');
  out.afterTwo = activeId();
  key('3');
  out.afterThree = activeId();
  key('4');
  out.afterFour = activeId();

  document.body.focus();
  key('1', { ctrlKey: true });
  out.afterCtrlDigit = activeId();

  document.body.focus();
  out.digitDefaultPrevented = !key('1');
  out.unmappedDigitPrevented = !key('9');

  // Clear the composer so the goal area starts from a known state.
  composer.value = '';
  return out;
}

function areaGoal() {
  var out = {};
  var banner = $one('#goal-banner');
  var composer = $one('#prompt-input');
  var restore = $one('.goal-banner-restore');

  composer.value = 'Refactor the parser';
  $one('#send-btn').click();
  return settle().then(function () {
    out.shownOnSend = !banner.hidden;
    out.slimOnSend = banner.classList.contains('slim');
    out.restoreHiddenOnSend = !!restore.hidden;

    push(msgFrame('working on it'));
    out.slimAfterMessage = banner.classList.contains('slim');
    out.restoreShownAfterMessage = !restore.hidden;

    restore.click();
    out.slimAfterRestore = banner.classList.contains('slim');

    // The point of the sticky flag: this must not undo the click above.
    push(msgFrame('still working'));
    out.slimAfterRestoreThenMessage = banner.classList.contains('slim');

    composer.value = 'Now ship it';
    $one('#send-btn').click();
    return settle().then(function () {
      out.slimOnNewGoal = banner.classList.contains('slim');
      out.textOnNewGoal = $one('#goal-text').textContent;

      key('Escape');
      out.hiddenAfterEscape = !!banner.hidden;
      return out;
    });
  });
}

function areaBadge() {
  var out = {};
  out.initial = badgeState();

  // Parked at the bottom: the content is already in front of the user.
  push(msgFrame('one'));
  out.atBottom = badgeState();

  scrollUp();
  push(msgFrame('two'));
  out.afterOneAway = badgeState();

  push(msgFrame('three'));
  push({ type: 'events', events: [
    { type: 'task_start', task_id: 't2', data: { title: 'Second task' } }] });
  out.afterMore = badgeState();

  scrollBottom();
  out.afterReturn = badgeState();
  return out;
}

// ── Runner ──────────────────────────────────────────────────────────────

function b64(s) {
  var bytes = new TextEncoder().encode(s), bin = '';
  bytes.forEach(function (x) { bin += String.fromCharCode(x); });
  return btoa(bin);
}

function report(v) {
  var d = document.createElement('div');
  d.id = '__result';
  // Pure base64, nothing else: --dump-dom escapes textContent, so any
  // delimiter with angle brackets comes back mangled and also collides with
  // this script's own source in the dumped document.
  d.textContent = b64(JSON.stringify(v));
  document.body.appendChild(d);
}

// Run areas in order, each isolated: an area that throws records its own
// error instead of taking the rest of the report down with it.
function runAreas(names, results) {
  if (!names.length) return Promise.resolve(results);
  var name = names[0];
  var rest = names.slice(1);
  var step;
  try {
    step = Promise.resolve(window['area' + name]());
  } catch (err) {
    step = Promise.reject(err);
  }
  return step.then(function (value) {
    results[name] = value;
  }, function (err) {
    results[name] = { __error: String((err && err.message) || err) };
  }).then(function () {
    return runAreas(rest, results);
  });
}

window.addEventListener('load', function () {
  setTimeout(function () {
    var results = {};
    try {
      var row = $one('.supervisor-list-item');
      if (!row) throw new Error('no supervisor row rendered');
      row.click();
    } catch (err) {
      report({ __fatal: String((err && err.message) || err) });
      return;
    }
    settle().then(function () {
      return runAreas(['Load', 'Tasks', 'Keys', 'Goal', 'Badge'], results);
    }).then(report, function (err) {
      report({ __fatal: String((err && err.message) || err) });
    });
  }, 250);
});
</script>
"""

CHROME_FLAGS = [
    "--headless", "--disable-gpu", "--no-sandbox", "--no-first-run",
    "--no-default-browser-check", "--disable-extensions", "--disable-sync",
    "--disable-background-networking", "--disable-component-update",
    "--disable-default-apps", "--disable-dev-shm-usage", "--mute-audio",
    "--virtual-time-budget=4000", "--dump-dom",
]


@functools.lru_cache(maxsize=1)
def _probe() -> dict:
    """Load the real supervisor page headless once and return every area.

    Returns a dict rather than raising, including on failure: lru_cache does
    not memoise exceptions, so a raising probe would relaunch Chromium for
    every test method in the file and turn a broken page into a ten-minute
    hang instead of a fast failure.
    """
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    stubs = (STUBS.replace("__SUPS__", json.dumps(SUPS))
                  .replace("__TASKS__", json.dumps(TASKS)))
    # The page loads supervisor.js with a cache-busting query that file://
    # cannot resolve; point at the copy beside the probe instead.
    marker = '<script src="supervisor.js?v=4"></script>'
    if marker not in html:
        return {"__fatal": f"script tag {marker!r} not found in supervisor.html"}
    html = html.replace(marker, stubs + '<script src="supervisor.js"></script>')
    html += DRIVER

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        shutil.copy(SUPERVISOR_JS, d / "supervisor.js")
        (d / "probe.html").write_text(html, encoding="utf-8")
        try:
            proc = subprocess.run(
                [CHROMIUM, *CHROME_FLAGS, f"file://{d / 'probe.html'}"],
                capture_output=True, text=True, timeout=180, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"__fatal": "chromium did not exit within 180s"}

    dom = proc.stdout or ""
    # Match the element, not a delimiter: the driver's own source appears in
    # the dumped document too, so a textual marker matches there first.
    found = re.search(r'id="__result"[^>]*>([A-Za-z0-9+/=]*)<', dom)
    if not found:
        return {"__fatal": "probe never reported; the page likely threw on "
                           f"load. stderr tail: {proc.stderr[-1500:]}"}
    try:
        return json.loads(base64.b64decode(found.group(1)).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"__fatal": f"unreadable probe payload: {exc}"}


def _area(name: str) -> dict:
    """One feature area's results, or a failure naming that area."""
    probe = _probe()
    if "__fatal" in probe:
        raise AssertionError(f"probe did not run: {probe['__fatal']}")
    if name not in probe:
        raise AssertionError(f"area {name!r} missing from probe: "
                             f"{sorted(probe)}")
    area = probe[name]
    if isinstance(area, dict) and "__error" in area:
        raise AssertionError(f"area {name!r} failed in-page: {area['__error']}")
    return area


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class PageLoadTests(unittest.TestCase):
    """Nothing below means anything if the page throws on the way up."""

    def test_the_page_loads_without_script_errors(self):
        self.assertEqual(_area("Load")["errors"], [])

    def test_the_new_elements_are_in_the_dom(self):
        load = _area("Load")
        self.assertTrue(load["badgeInDom"], "#topbar-badge missing")
        self.assertTrue(load["restoreInDom"], ".goal-banner-restore missing")

    def test_a_supervisor_is_selected(self):
        """Guards every area below: without a selection there is no stream and
        no task tree, and the feature assertions would be vacuous."""
        self.assertTrue(_area("Load")["supervisorSelected"])


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class KeyboardShortcutTests(unittest.TestCase):
    """The panel keys, and the composer they must not break."""

    def test_a_digit_typed_into_the_composer_does_not_move_focus(self):
        """The regression proper.

        Without a typing guard the shortcut fires on every keystroke that
        reaches document, so typing "step 1 of 4" threw focus at the task tree
        mid-word and left the composer unusable for any prompt with a number in
        it -- a worse bug than the missing shortcut.
        """
        self.assertEqual(
            _area("Keys")["afterDigitInComposer"], "prompt-input",
            "a bare digit typed into the composer stole focus from it")

    def test_a_digit_outside_a_text_field_focuses_the_panel(self):
        """Guards the test above: a guard that let nothing through at all
        would satisfy it trivially."""
        self.assertEqual(_area("Keys")["afterDigitOnBody"], "task-tree")

    def test_each_panel_key_focuses_its_own_panel(self):
        """These are plain divs, and .focus() on a div without tabindex is a
        silent no-op, so three of the four shortcuts did nothing at all."""
        keys = _area("Keys")
        for probe_key, expected in (("afterTwo", "chat-messages"),
                                    ("afterThree", "event-log"),
                                    ("afterFour", "prompt-input")):
            with self.subTest(target=expected):
                self.assertEqual(keys[probe_key], expected)

    def test_a_modified_digit_is_not_a_panel_shortcut(self):
        """Ctrl+1 is the browser's own tab switch, not ours to take."""
        self.assertNotEqual(_area("Keys")["afterCtrlDigit"], "task-tree")

    def test_the_page_claims_only_the_digits_it_handles(self):
        keys = _area("Keys")
        self.assertTrue(keys["digitDefaultPrevented"],
                        "a handled shortcut must preventDefault")
        self.assertFalse(keys["unmappedDigitPrevented"],
                         "an unmapped key must be left to the browser")


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class GoalBannerSlimStripTests(unittest.TestCase):
    """Shrink to a slim strip, and the restore arrow that undoes it."""

    def test_sending_a_prompt_shows_the_goal_expanded(self):
        goal = _area("Goal")
        self.assertTrue(goal["shownOnSend"], "goal banner did not appear")
        self.assertFalse(goal["slimOnSend"], "goal banner started slimmed")
        self.assertTrue(goal["restoreHiddenOnSend"],
                        "restore arrow shows while the banner is expanded")

    def test_a_streamed_message_shrinks_it(self):
        goal = _area("Goal")
        self.assertTrue(goal["slimAfterMessage"])
        self.assertTrue(goal["restoreShownAfterMessage"],
                        "slimmed with no visible way to expand it again")

    def test_the_restore_arrow_expands_it(self):
        self.assertFalse(_area("Goal")["slimAfterRestore"])

    def test_the_restore_survives_the_next_message(self):
        """The regression proper.

        restoreGoalBanner cleared the shrink flag but not the reason for it, so
        the very next streamed message re-shrank the banner the user had just
        expanded. Every individual step passed -- the click fired, the class
        came off, the listener was wired -- and the control still appeared to do
        nothing, because on a live supervisor the next message is a second away.
        """
        self.assertFalse(
            _area("Goal")["slimAfterRestoreThenMessage"],
            "the message after a manual restore re-shrank the banner")

    def test_a_new_goal_starts_expanded(self):
        """Otherwise the sticky restore leaks into the next goal, which the
        user has not seen yet and has expressed no opinion about."""
        goal = _area("Goal")
        self.assertFalse(goal["slimOnNewGoal"])
        self.assertEqual(goal["textOnNewGoal"], "Now ship it")

    def test_escape_dismisses_the_banner(self):
        self.assertTrue(_area("Goal")["hiddenAfterEscape"])


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class UnreadBadgeTests(unittest.TestCase):
    """The badge counts what is off screen, and nothing else."""

    def test_it_starts_empty(self):
        self.assertTrue(_area("Badge")["initial"]["hidden"])

    def test_nothing_is_counted_while_parked_at_the_bottom(self):
        """A badge for content already in front of the user is just a chore."""
        self.assertTrue(_area("Badge")["atBottom"]["hidden"],
                        "counted a message the user was looking at")

    def test_a_message_missed_while_scrolled_up_is_counted(self):
        """The regression proper: the badge counted log events only.

        A chat message arriving while you are scrolled up is the single thing
        the badge exists for, and it was the one thing that did not increment
        it.
        """
        state = _area("Badge")["afterOneAway"]
        self.assertFalse(state["hidden"], "a missed message was not counted")
        self.assertTrue(state["visible"], "badge counted but stayed invisible")
        self.assertEqual(state["text"], "1")

    def test_further_traffic_accumulates(self):
        self.assertEqual(_area("Badge")["afterMore"]["text"], "3")

    def test_returning_to_the_bottom_clears_it(self):
        state = _area("Badge")["afterReturn"]
        self.assertTrue(state["hidden"], "badge survived catching up")
        self.assertFalse(state["visible"])


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class ExpandableTaskRowTests(unittest.TestCase):
    """The per-task expand toggle."""

    def test_every_task_gets_a_toggle_and_a_detail_row(self):
        tasks = _area("Tasks")
        self.assertTrue(tasks["toggleExists"])
        self.assertEqual(tasks["detailRows"], len(TASKS))

    def test_rows_start_collapsed(self):
        self.assertEqual(_area("Tasks")["expandedBefore"], 0)

    def test_the_toggle_is_styled(self):
        """The rule was scoped under .task-detail-row, but the button sits in
        .task-item -- a *sibling* of that row, not an ancestor -- so it matched
        nothing and the control rendered as bare default chrome. Reading the
        stylesheet cannot catch that; asking the browser can."""
        self.assertEqual(_area("Tasks")["toggleCursor"], "pointer",
                         "the .expand-toggle rule is not matching the button")

    def test_clicking_the_toggle_opens_that_row(self):
        tasks = _area("Tasks")
        self.assertTrue(tasks["expandedAfterClick"])
        self.assertTrue(tasks["resultShown"], "detail row has no task result")

    def test_opening_a_row_does_not_select_the_task(self):
        """The toggle sits inside the row's own click target, so without
        stopPropagation expanding a task also navigated to it."""
        self.assertFalse(_area("Tasks")["activeAfterToggle"],
                         "expanding a row also selected it")

    def test_only_one_row_is_open_at_a_time(self):
        tasks = _area("Tasks")
        self.assertTrue(tasks["t2Open"])
        self.assertFalse(tasks["t1StillOpen"])

    def test_clicking_the_toggle_again_closes_the_row(self):
        self.assertFalse(_area("Tasks")["t2AfterSecondClick"])

    def test_the_row_body_still_selects_the_task(self):
        """The guard against over-correcting: suppressing the row click
        entirely would pass every test above."""
        self.assertTrue(_area("Tasks")["activeAfterRowClick"])


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
