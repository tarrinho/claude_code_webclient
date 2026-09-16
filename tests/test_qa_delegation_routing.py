"""QA: the routing seam, and the guarantee that it changes nothing yet.

Spec section 1: assign_model returns config.ANTHROPIC_MODEL on both branches,
so the computed complexity is discarded. This closes that seam -- and the
headline test is that with no task type operational, routing is byte-identical
to what it was, because that is this release's whole claim.

Constructor note: ModelRouter takes a dict (``{"rules": [...]}``), not a bare
list -- its body does ``rules.get("rules", []) if rules else []``, so a
non-empty list raises AttributeError. Every construction below passes a dict.
"""
from __future__ import annotations

import unittest

import config
from orchestrator import ModelRouter
from tiered_delegation import CapabilityRow, CapabilityTable


def _table(operational=()):
    rows = [
        CapabilityRow("free/model", "coding", 0.66, 44, 0.0, 26.8, 229376),
        CapabilityRow("paid/model", "coding", 1.0, 24, 1.57, 15.5, 1000000),
    ]
    return CapabilityTable(rows, operational=operational)


class RoutingSeamTests(unittest.TestCase):
    def setUp(self):
        self.router = ModelRouter({"rules": []})

    def test_with_nothing_operational_routing_is_unchanged(self):
        """The claim of release 0.19.0. If this fails, the release is not what
        it says it is."""
        chosen = self.router.assign_model(
            "refactor large module", "migrate database schema",
            complexity=4, table=_table())
        self.assertEqual(chosen, config.ANTHROPIC_MODEL)

    def test_no_table_at_all_is_also_unchanged(self):
        """A caller that has not been taught about the table must keep working
        -- every existing call site passes no table."""
        chosen = self.router.assign_model("anything", "at all", complexity=4)
        self.assertEqual(chosen, config.ANTHROPIC_MODEL)

    def test_an_operational_type_takes_the_ladder_s_first_rung(self):
        """The seam is real, not decorative: flip the flag and routing moves."""
        chosen = self.router.assign_model(
            "refactor large module", "migrate database schema",
            complexity=4, table=_table(operational=("coding",)))
        self.assertEqual(chosen, "free/model")

    def test_an_explicit_rule_still_wins_over_the_ladder(self):
        """Rules are an operator's deliberate override and must outrank a
        measured ladder."""
        router = ModelRouter(
            {"rules": [{"pattern": "refactor", "model": "ruled/model"}]})
        chosen = router.assign_model(
            "refactor large module", "migrate database schema",
            complexity=4, table=_table(operational=("coding",)))
        self.assertEqual(chosen, "ruled/model")

    def test_an_operational_type_with_an_empty_ladder_falls_back(self):
        """Belt and braces: 1.1 refuses this configuration at startup, but a
        caller holding a table built another way must not crash."""
        empty = CapabilityTable([], operational=("coding",))
        chosen = self.router.assign_model(
            "refactor large", "migrate database", complexity=4, table=empty)
        self.assertEqual(chosen, config.ANTHROPIC_MODEL)


class DuplicateBranchTests(unittest.TestCase):
    def test_the_complexity_branch_is_not_two_identical_returns(self):
        """Spec section 1's opening defect, pinned so it cannot come back:
        `if complexity >= 4: return X / return X` computes a value and throws
        it away, which reads as routing while doing none."""
        import inspect
        source = inspect.getsource(ModelRouter.assign_model)
        self.assertNotIn(
            "if complexity >= 4:\n            return config.ANTHROPIC_MODEL\n"
            "        return config.ANTHROPIC_MODEL", source)


if __name__ == "__main__":
    unittest.main()
