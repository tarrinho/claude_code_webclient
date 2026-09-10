"""QA: the transport-group status shown in Settings -> Backends.

`_transportStatus` in web/assets/machines.js decides Active / Uninitialized /
Disabled for a group of machines, purely from data already fetched -- no
schema change, no new endpoint. These tests execute the real function via
QuickJS rather than re-implementing its truth table in Python: a Python copy
of the logic would drift from the JS the browser actually runs and could
pass forever after an edit broke the real thing (the same reasoning
tests/test_frontend_syntax.py and test_qa_launch_reclaim.py already state for
their own "run it, don't read it" tests).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - exercised only without the dev deps
    quickjs = None

MACHINES_JS = (
    Path(__file__).resolve().parents[1] / "web" / "assets" / "machines.js"
)


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level function's exact source out of a module by name.

    Brace-balanced rather than a regex with a fixed end pattern, so a nested
    `{}` inside the function body (an object literal, an if-block) cannot
    truncate the extraction early. The parameter list is balanced first and
    separately: `_transportStatus`'s own default argument, `= {}`, contains a
    brace pair that would otherwise be mistaken for the body's opening brace,
    extracting a signature with no body at all.
    """
    marker = f"function {name}("
    start = source.index(marker)
    paren_start = source.index("(", start)
    depth = 0
    j = paren_start
    for j in range(paren_start, len(source)):
        if source[j] == "(":
            depth += 1
        elif source[j] == ")":
            depth -= 1
            if depth == 0:
                break
    brace_start = source.index("{", j)
    depth = 0
    i = brace_start
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                break
    return source[start:i + 1]


def _call_transport_status(machines: list[dict], tunnel_status: dict) -> str:
    source = MACHINES_JS.read_text(encoding="utf-8")
    fn = _extract_function(source, "_transportStatus")
    result = quickjs.Context().eval(
        f"{fn}\n_transportStatus({json.dumps(machines)}, {json.dumps(tunnel_status)})"
    )
    return result


@unittest.skipIf(quickjs is None, "quickjs not installed (pip install -r requirements-dev.txt)")
class TransportStatusTests(unittest.TestCase):

    # ── "not known yet" is not a verdict ──────────────────────────────────
    #
    # Added 2026-09-10. Six reports across three days said this panel showed
    # Uninitialized and "wrong information". Two causes, both here: the client
    # never fetched status at all (its predicate compared backend_kind against
    # 'ssh_proxy' while shared.py emits "ssh-proxy"), and when it had no data
    # this function reported the absence as a fact. The first is fixed in
    # app.js; these pin the second.

    def test_a_never_fetched_cache_is_unknown_not_uninitialized(self):
        """The defect. A null cache means no fetch has happened, which is not
        the same as knowing the tunnel is down -- and saying "Uninitialized"
        for it is a false statement, not a placeholder."""
        machines = [{"id": "m1", "transport_id": "t1", "enabled": True}]
        self.assertEqual(_call_transport_status(machines, None), "unknown")

    def test_a_fetched_but_empty_cache_is_still_uninitialized(self):
        """The other side of the distinction, and the reason null rather than
        a flag: an empty object is a report ("nothing to tell you about these
        machines"), so the Uninitialized verdict is earned here."""
        machines = [{"id": "m1", "transport_id": "t1", "enabled": True}]
        self.assertEqual(_call_transport_status(machines, {}), "uninitialized")

    def test_a_local_group_is_active_even_with_no_status_fetched(self):
        """Unknown must not leak onto groups that have no tunnel to know
        about -- a direct backend's readiness does not depend on the fetch."""
        machines = [{"id": "m1", "enabled": True}]
        self.assertEqual(_call_transport_status(machines, None), "active")

    def test_disabled_still_wins_over_unknown(self):
        """A deliberately-disabled group is known to be off regardless of
        whether any status has been fetched; unknown must not mask it."""
        machines = [{"id": "m1", "transport_id": "t1", "enabled": False}]
        self.assertEqual(_call_transport_status(machines, None), "disabled")

    def test_a_group_with_no_machines_is_uninitialized_even_unfetched(self):
        """Nothing to be unsure about: an empty group is known to have no
        backend assigned, which is what Uninitialized means."""
        self.assertEqual(_call_transport_status([], None), "uninitialized")

    def test_a_transport_with_no_machines_is_uninitialized(self):
        """A transport just created, nothing assigned to it yet."""
        self.assertEqual(_call_transport_status([], {}), "uninitialized")

    def test_every_machine_disabled_wins_over_a_working_tunnel(self):
        """Deliberately off must not read as Active because a stale tunnel
        from before it was disabled happens to still show proxy_ok."""
        machines = [{"id": "m1", "transport_id": "t1", "enabled": False}]
        tunnel_status = {"m1": {"proxy_ok": True}}
        self.assertEqual(_call_transport_status(machines, tunnel_status), "disabled")

    def test_one_enabled_machine_among_disabled_ones_is_not_disabled(self):
        machines = [
            {"id": "m1", "transport_id": "t1", "enabled": False},
            {"id": "m2", "transport_id": "t1", "enabled": True},
        ]
        tunnel_status = {"m1": {"proxy_ok": True}}
        self.assertEqual(_call_transport_status(machines, tunnel_status), "active")

    def test_enabled_with_a_confirmed_tunnel_is_active(self):
        machines = [{"id": "m1", "transport_id": "t1", "enabled": True}]
        tunnel_status = {"m1": {"proxy_ok": True}}
        self.assertEqual(_call_transport_status(machines, tunnel_status), "active")

    def test_enabled_with_no_tunnel_entry_is_uninitialized(self):
        """Check/Init never run: the status cache has nothing for this
        machine at all, not a False -- both must read the same way."""
        machines = [{"id": "m1", "transport_id": "t1", "enabled": True}]
        self.assertEqual(_call_transport_status(machines, {}), "uninitialized")

    def test_enabled_with_proxy_not_ok_is_uninitialized(self):
        machines = [{"id": "m1", "transport_id": "t1", "enabled": True}]
        tunnel_status = {"m1": {"proxy_ok": False}}
        self.assertEqual(_call_transport_status(machines, tunnel_status), "uninitialized")

    def test_a_local_direct_machine_needs_no_tunnel_to_be_active(self):
        """No transport_id at all -- the Direct group's whole case."""
        machines = [{"id": "m1", "enabled": True}]
        self.assertEqual(_call_transport_status(machines, {}), "active")

    def test_a_disabled_local_machine_is_disabled(self):
        machines = [{"id": "m1", "enabled": False}]
        self.assertEqual(_call_transport_status(machines, {}), "disabled")

    def test_enabled_defaults_true_when_the_field_is_absent(self):
        """The server's own default (ALTER TABLE ... DEFAULT 1): a machine
        that predates the enabled column must not read as disabled."""
        machines = [{"id": "m1", "transport_id": "t1"}]
        tunnel_status = {"m1": {"proxy_ok": True}}
        self.assertEqual(_call_transport_status(machines, tunnel_status), "active")

    def test_a_shared_tunnels_status_speaks_for_the_whole_group(self):
        """One tunnel per transport (Task 5): the group's status is read off
        the first machine, not recomputed per machine."""
        machines = [
            {"id": "m1", "transport_id": "t1", "enabled": True},
            {"id": "m2", "transport_id": "t1", "enabled": True},
        ]
        tunnel_status = {"m1": {"proxy_ok": True}}  # m2 has no entry at all
        self.assertEqual(_call_transport_status(machines, tunnel_status), "active")


if __name__ == "__main__":
    unittest.main()
