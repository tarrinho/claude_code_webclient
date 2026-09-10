"""QA: an older, slower GET /api/chats response must never overwrite a newer,
faster one.

The other half of a sidebar-flicker report investigated live: even after
fixing the single-response race in `handle_chats_list` (see
tests/test_qa_chats_list_running_race.py), a real browser against the real
deployment still occasionally showed a chat's marker revert from
`.chat-ended` back to `.chat-running` a few seconds after correctly settling
-- with the server logs showing only the one turn the whole time. Root
cause: `refreshChats()` (app.js) is called on a plain 6s `setInterval`
(`CHAT_POLL_MS`) with no guard against two calls being in flight at once, and
this host runs under enough real concurrent load (several agent sessions,
background turns, sysstats, `git`/DB activity from other tooling) that
request latency genuinely varies from poll to poll. Two overlapping calls
resolving out of order let the *older* (slower) response apply itself last,
silently overwriting the *newer* (faster) one that had already rendered
correctly -- reading as a highlight appearing and then disappearing, even
though the data was never actually wrong at any single point in time, only
applied in the wrong order.

Fixed with a monotonic sequence number: each call captures its own value
before awaiting the fetch, and discards its own result if a later call has
since started. Tested here by lifting the real function out of app.js
(`node` is not installed on this host; this is the project's usual pattern,
see test_qa_series_continuity_js.py) and driving two overlapping calls with
manually controlled resolve order -- the faster, newer one resolves first,
then the slower, older one resolves second, and only the newer one's data
must survive.
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
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def lift(path: Path, name: str) -> str:
    """The full text of `[async ][export ]function <name>(...)`, braces matched.

    Broader than the `export function`-only lifter test_qa_series_continuity_js.py
    uses: refreshChats is a plain top-level `async function`, not exported.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(
        rf"(?:export\s+)?(?:async\s+)?function\s+{re.escape(name)}\s*\(",
        source,
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
    return source[start:pos]


PAGE = """<!doctype html><meta charset="utf-8"><title>pending</title>
<script>
const calls = [];
const state = {chats: []};
const listController = {
  setActiveTurns: (ids) => calls.push(['setActiveTurns', ids]),
  setUnread: (ids) => calls.push(['setUnread', ids]),
  setEnded: (ids) => calls.push(['setEnded', ids]),
  render: (chats) => calls.push(['render', chats.map(c => c.id)]),
};
function unreadChatIds() { return []; }
function updateEndedTracking() { return []; }

const resolvers = [];
async function apiFetch() {
  return new Promise((resolve) => resolvers.push(resolve));
}

%(function)s

async function run() {
  // Call #1 starts first (the older poll) and will resolve *last*.
  const p1 = refreshChats();
  await Promise.resolve();
  await Promise.resolve();
  // Call #2 starts second (a newer poll) and will resolve *first* --
  // exactly the out-of-order interleaving a slower earlier request causes
  // under real load.
  const p2 = refreshChats();
  await Promise.resolve();
  await Promise.resolve();

  if (resolvers.length !== 2) {
    document.title = 'FAIL: expected 2 in-flight fetches, saw ' + resolvers.length;
    return;
  }

  const fresh = {ok: true, json: async () => ({chats: [{id: 'x', running: false}]})};
  const stale = {ok: true, json: async () => ({chats: [{id: 'x', running: true}]})};

  resolvers[1](fresh);   // the newer call's response lands first
  await p2;
  resolvers[0](stale);   // the older call's stale response lands after
  await p1;

  const finalRunning = state.chats[0] && state.chats[0].running;
  const renders = calls.filter(c => c[0] === 'render').length;
  if (finalRunning !== false) {
    document.title = 'FAIL: final state.chats[0].running was ' + finalRunning +
      ' -- the stale (older) response overwrote the fresh (newer) one';
  } else if (renders !== 1) {
    document.title = 'FAIL: expected exactly 1 render (the stale response ' +
      'must be dropped before calling render), saw ' + renders;
  } else {
    document.title = 'OK';
  }
}
run();
</script>
"""


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class RefreshChatsOutOfOrderTests(unittest.TestCase):
    def test_a_stale_response_never_overwrites_a_fresher_one(self):
        function_src = lift(APP_JS, "refreshChats")
        # The sequence-number declaration sits immediately above the
        # function; lift it as a fixed block rather than by name so a
        # rename does not silently drop the guard from this test's page
        # while leaving the real one running.
        seq_decl = "let _refreshSeq = 0;"
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn(
            seq_decl, source,
            "the sequence-number guard's declaration is missing from app.js -- "
            "either it was removed (regression) or renamed (update this test)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "probe.html"
            page.write_text(PAGE % {"function": seq_decl + "\n" + function_src})
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
                f"the page never finished -- chromium said: {result.stderr[-300:]}"
            )
        self.assertEqual(title, "OK", title)


if __name__ == "__main__":
    unittest.main()
