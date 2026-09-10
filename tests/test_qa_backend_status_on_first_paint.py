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


class PollPredicateTests(unittest.TestCase):
    """The root cause, and the reason five earlier attempts at this panel
    found nothing: the client never asked for tunnel status at all.

    `_pollTunnelStatus` was armed on `backend_kind === 'ssh_proxy'`, while
    shared.py:backend_kind emits "ssh-proxy" with a hyphen -- the underscore
    form survives only in app.js's label table as a legacy *provider* value.
    The predicate could therefore never be true, the poller was never started,
    and badges only ever updated via the wc:tunnel-start-queued event, i.e.
    after pressing Init or Check. Every fix aimed at the status pipeline was
    aimed at the wrong half.

    Both call sites now derive the condition from `transport_id` -- the same
    field _transportStatus reads -- so the trigger and the thing it feeds
    cannot drift apart into two spellings again."""

    def setUp(self):
        self.js = APP_JS.read_text(encoding="utf-8")
        self.code = _strip_comments(self.js)

    def test_the_unmatchable_underscore_comparison_is_gone(self):
        self.assertNotIn(
            "backend_kind === 'ssh_proxy'", self.code,
            "this compares against a string shared.py never emits, so the "
            "poller is never armed",
        )

    def test_every_poll_call_is_keyed_on_transport_id(self):
        calls = re.findall(r"_pollTunnelStatus\(([^)]*)\)", self.code)
        self.assertTrue(calls, "no _pollTunnelStatus call sites found")
        for arg in calls:
            self.assertNotIn(
                "backend_kind", arg,
                f"_pollTunnelStatus({arg}) keys on a display string; the "
                f"badge reads transport_id",
            )

    def test_both_call_sites_are_still_present(self):
        """One arms it for the Backends panel, one at boot. Losing the boot
        call would leave badges stale until Settings was opened."""
        self.assertEqual(
            len(re.findall(r"_pollTunnelStatus\(", self.code)), 2,
            "expected exactly the panel-open and boot call sites",
        )


class StatusTableTests(unittest.TestCase):
    """`unknown` has to exist in both lookup tables or it renders as
    `undefined` in the badge and `NaN` in the sort comparator."""

    def setUp(self):
        self.js = MACHINES_JS.read_text(encoding="utf-8")

    def test_unknown_has_a_label(self):
        table = re.search(r"TRANSPORT_STATUS_LABEL = \{(.*?)\}", self.js, re.DOTALL)
        self.assertIsNotNone(table)
        self.assertIn("unknown:", table.group(1),
                      "a status with no label renders as undefined")

    def test_unknown_has_a_sort_position(self):
        table = re.search(r"TRANSPORT_STATUS_ORDER = \{(.*?)\}", self.js, re.DOTALL)
        self.assertIsNotNone(table)
        self.assertIn("unknown:", table.group(1),
                      "a status missing from the order table sorts as NaN")

    def test_unknown_sorts_next_to_active_not_beside_uninitialized(self):
        """Almost every unknown group resolves to active a moment later.
        Sorting it beside uninitialized would make the list reshuffle
        visibly as the first status lands."""
        table = re.search(r"TRANSPORT_STATUS_ORDER = \{(.*?)\}", self.js, re.DOTALL).group(1)
        order = dict(re.findall(r"(\w+):\s*(\d+)", table))
        self.assertEqual(int(order["unknown"]) - int(order["active"]), 1,
                         f"unknown should sort immediately after active: {order}")

    def test_the_cache_starts_null_so_unknown_is_reachable(self):
        """Without this the unknown branch is dead code in the real app.

        _transportStatus distinguishes null ("not fetched") from {} ("fetched,
        nothing to report"), so the module's initial value decides whether the
        distinction exists at all. Initialising it to {} passes every
        behavioural test -- those call the function directly with null -- while
        the running page can never reach the unknown state and goes straight
        back to reporting Uninitialized before the first fetch. Found by
        mutation: flipping null to {} broke nothing until this existed."""
        self.assertRegex(
            self.js, r"let _tunnelStatusCache = null;",
            "the cache must start null, or 'unknown' is unreachable and the "
            "first paint lies again",
        )

    def test_the_badge_has_a_style_for_unknown(self):
        """An unstyled badge inherits nothing and reads as unstyled text."""
        css = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".transport-status-badge-unknown", css)


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
