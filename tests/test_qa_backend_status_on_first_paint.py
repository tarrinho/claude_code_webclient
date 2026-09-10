"""QA: Settings + Backends shows the real transport status on first paint.

The complaint, made six times across 2026-09-08, -09 and -10 and never
closed out: the panel "takes too long and it keeps checking and showing
wrong information", every transport reads Uninitialized, and pressing Check
appears to do nothing even when Check itself reports success.

One cause, two halves, both in the open path rather than anywhere near the
tunnel manager (which is healthy -- it ticks every 30s and persists to
ssh_tunnels, verified live by watching last_check advance):

1. `_pollTunnelStatus` only armed a 5s setInterval. It performed no fetch of
   its own, so the status cache stayed empty for the first five seconds after
   the panel opened.
2. `loadBackends` called `_renderMachineList()` *before* any status fetch,
   and awaited `loadTurnCounts()` -- a 30-day usage aggregate -- before that
   render.

`_transportStatus` reads a missing cache entry as "uninitialized", because
its three states give it no way to say "not known yet". So the first paint
asserted Uninitialized for every transport-backed group as a fact, then
corrected itself up to five seconds later. Check's result was never being
ignored; the badge was waiting on a poll that had not happened.

Asserted from source, which is this repo's convention for app.js and
machines.js -- there is no JS runner for them (the quickjs harness in
tests/js/ serves supervisor-map.js only). See
test_qa_backend_groups_collapse_on_open.py, whose ordering tests these
mirror, including its comment-stripping: body.index() matches a mention as
readily as a call, and the comments here name the calls being ordered.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "assets" / "app.js"
MACHINES_JS = ROOT / "web" / "assets" / "machines.js"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


class PollTunnelStatusFetchesImmediatelyTests(unittest.TestCase):
    """Half one: arming an interval is not fetching."""

    def setUp(self):
        js = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(
            r"export function _pollTunnelStatus\(active\)\s*\{(.*?)\n\}",
            js, re.DOTALL)
        self.assertIsNotNone(match, "_pollTunnelStatus missing from machines.js")
        self.body = _strip_comments(match.group(1))

    def test_it_refreshes_once_when_activated(self):
        """The defect: only setInterval, so nothing was known for 5s."""
        self.assertIn(
            "_refreshTunnelStatus()", self.body,
            "activating the poller must fetch once immediately, not wait a "
            "full interval for the first result",
        )

    def test_the_immediate_refresh_is_returned_so_callers_can_await_it(self):
        """loadBackends has to be able to wait for it before rendering; a
        fire-and-forget call would leave the same empty-cache first paint."""
        self.assertRegex(
            self.body, r"return\s+_refreshTunnelStatus\(\)",
            "the immediate refresh must be returned, or awaiting it does "
            "nothing and the first paint races the fetch again",
        )

    def test_it_still_arms_the_interval(self):
        """The immediate fetch replaces the initial delay, not the polling."""
        self.assertIn("setInterval(_refreshTunnelStatus, 5000)", self.body)

    def test_it_always_returns_something_awaitable(self):
        """Called with active=false (no SSH backends) it must still be safe to
        await, or `await _pollTunnelStatus(false)` throws on undefined."""
        self.assertIn("return Promise.resolve()", self.body)


class LoadBackendsOrderingTests(unittest.TestCase):
    """Half two: what is fetched before the badges are painted."""

    def setUp(self):
        js = APP_JS.read_text(encoding="utf-8")
        match = re.search(
            r"async function loadBackends\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        self.assertIsNotNone(match, "loadBackends not found in app.js")
        self.body = _strip_comments(match.group(1))

    def test_status_is_awaited_before_the_first_render(self):
        """The regression that produced six reports. Rendering first means
        painting Uninitialized for everything and correcting it later."""
        self.assertRegex(self.body, r"await\s+_pollTunnelStatus\(")
        self.assertLess(
            self.body.index("_pollTunnelStatus("),
            self.body.index("_renderMachineList()"),
            "tunnel status must be fetched before the badges are painted, or "
            "the first paint is a guess",
        )

    def test_turn_counts_do_not_block_the_render(self):
        """A 30-day usage aggregate must not gate the panel appearing. It
        feeds one column, and that column can fill in late."""
        self.assertNotIn(
            "await loadTurnCounts()", self.body,
            "the panel waits on the slowest call it makes to paint anything",
        )
        self.assertIn("loadTurnCounts()", self.body,
                      "the counts should still be fetched, just not awaited "
                      "ahead of the render")

    def test_turn_counts_still_trigger_a_re_render_when_they_arrive(self):
        """Not awaiting them is only correct if something repaints after --
        otherwise the column stays permanently empty on first open."""
        self.assertRegex(
            self.body, r"loadTurnCounts\(\)\s*\.then\(\s*_renderMachineList\s*\)",
            "counts fetched but never rendered is a silent regression: the "
            "column would simply stay blank",
        )

    def test_machines_and_transports_are_still_loaded_first(self):
        """Status is keyed by machine id, so the machines have to exist
        before the status fetch is worth anything."""
        self.assertLess(
            self.body.index("await loadMachines()"),
            self.body.index("_pollTunnelStatus("),
        )
        self.assertLess(
            self.body.index("await loadTransports()"),
            self.body.index("_pollTunnelStatus("),
        )


class ModuleVersionsAgreeTests(unittest.TestCase):
    """Both files changed, so both cache-busters had to move together. This
    repo has shipped the ES-module identity split more than once -- two
    querystrings for one module means two module instances, doubled handlers
    and doubled fetches."""

    def test_every_reference_to_app_js_uses_one_version(self):
        versions = set()
        for path in [ROOT / "web" / "index.html", *(ROOT / "web" / "assets").glob("*.js")]:
            versions |= set(re.findall(r"app\.js\?v=(\d+)", path.read_text(encoding="utf-8")))
        self.assertEqual(len(versions), 1, f"app.js referenced at {versions}")

    def test_every_reference_to_machines_js_uses_one_version(self):
        versions = set()
        for path in [ROOT / "web" / "index.html", *(ROOT / "web" / "assets").glob("*.js")]:
            versions |= set(re.findall(r"machines\.js\?v=(\d+)", path.read_text(encoding="utf-8")))
        self.assertEqual(len(versions), 1, f"machines.js referenced at {versions}")


if __name__ == "__main__":
    unittest.main()
