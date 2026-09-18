"""QA: each task type's model rows can be hidden and unhidden, and arrive hidden.

Requested by Pedro on 2026-09-18, alongside the same change to Settings > Specs.
The Delegation page renders one card per task type, and under each card a table
with one row per (model, task_type). At eleven task types that is a page you
scroll rather than read.

Two properties, and they fail independently:

* the table arrives collapsed, so the page opens as a list of task types;
* the control actually toggles, which is what a `<details>` gives for free but
  which nothing proves until something clicks it.

The second is the one that needs a browser. `test_qa_specs_groups_start_closed.py`
asserts its equivalent from source, which is right for that file because
specs.js has no JS runner covering it -- but the lesson from this same week is
that source assertions and route tests both pass while a control does nothing
(the ceiling knob shipped with a bare `fetch`, 403 on every click, and its route
tests were green throughout). So this file clicks.

What is deliberately NOT asserted: that the knob, ladder line and blockers stay
visible when the table is shut. They are rendered above it and are not inside
the <details>, so no collapse can hide them -- asserting it would test the DOM
order of a function this file does not call.
"""
from __future__ import annotations

import unittest

from tests.test_frontend_browser import DRIVER_OK, DRIVER_WHY, _BrowserFixture


@unittest.skipUnless(DRIVER_OK, DRIVER_WHY)
class DelegationModelTableToggleBrowserTests(_BrowserFixture):

    def _seed(self):
        """Two task types with two models each.

        The fixture server starts on an empty database, so without rows there
        are no cards and every assertion below fails on navigation rather than
        on the thing it is testing -- which is exactly how the first run of
        this file failed. Two types, because one of the tests is that the
        toggles are independent; two models each, so the summary's count has
        something to be right or wrong about.
        """
        import sqlite3
        from pathlib import Path

        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute("DELETE FROM delegation_operational")
        for task_type in ("widget", "gadget"):
            for model in ("claude-sonnet-5", "azure_ai/gpt-5.6-luna"):
                con.execute(
                    "INSERT OR REPLACE INTO delegation_capability "
                    "(model, task_type, accuracy, n, cost_per_1m_tokens, "
                    " median_latency_s, max_context, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (model, task_type, 1.0, 12, 0.0, 10.0, 1_000_000,
                     "2026-09-18T00:00:00Z"))
        con.commit()
        con.close()

    def _open_delegation(self):
        self._seed()
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="delegation"]')
        self.page.wait_for_selector("#panelDelegation:not([hidden])", timeout=10_000)
        self.page.wait_for_selector(".delegation-card", timeout=10_000)
        self.page.wait_for_timeout(300)

    def test_the_model_table_arrives_hidden(self):
        """The requested default. `open` absent means the browser has the table
        collapsed; the table element exists either way, so this asks the
        browser whether it is visible rather than whether it is in the DOM."""
        self._open_delegation()
        first = self.page.locator(".delegation-models").first
        self.assertFalse(
            first.evaluate("el => el.open"),
            "a task type's model rows must arrive collapsed",
        )
        self.assertFalse(
            first.locator(".delegation-model-table").is_visible(),
            "the table is in the DOM but must not be visible while collapsed",
        )

    def test_clicking_the_summary_unhides_and_hides_again(self):
        """The load-bearing one: the control works. Asserts visibility from the
        browser after each click, not the `open` attribute alone -- an attribute
        can be set while the content stays hidden by CSS."""
        self._open_delegation()
        first = self.page.locator(".delegation-models").first
        summary = first.locator(".delegation-models-summary")
        table = first.locator(".delegation-model-table")

        summary.click()
        self.page.wait_for_timeout(150)
        self.assertTrue(table.is_visible(), "clicking the summary must reveal the table")

        summary.click()
        self.page.wait_for_timeout(150)
        self.assertFalse(table.is_visible(), "clicking again must hide it")

    def test_each_task_type_toggles_independently(self):
        """One control per table, not one for the page. Opening the first must
        leave the second shut -- a shared handler or a single id would fail
        here and nowhere else."""
        self._open_delegation()
        groups = self.page.locator(".delegation-models")
        # assertEqual, not skipTest. The first draft skipped when fewer than
        # two groups were found, which meant that with the feature absent
        # entirely -- zero groups -- this test SKIPPED rather than failed, and
        # a mutation run against HEAD reported "3 failed, 1 skipped" instead of
        # 4 failed. `_seed` writes exactly two task types, so any other number
        # is a real failure and must be reported as one.
        self.assertEqual(
            groups.count(), 2,
            "_seed writes two task types, so two collapsible model tables must "
            "render; a different count means the feature or the seeding broke",
        )

        groups.nth(0).locator(".delegation-models-summary").click()
        self.page.wait_for_timeout(150)
        self.assertTrue(groups.nth(0).evaluate("el => el.open"))
        self.assertFalse(
            groups.nth(1).evaluate("el => el.open"),
            "opening one task type's rows must not open another's",
        )

    def test_the_summary_says_how_many_rows_it_hides(self):
        """What makes a collapsed table safe to arrive at. Settings > Specs
        shipped collapsible groups whose headers named only the group, and with
        everything in one group the panel read as empty. The count is the
        difference, so it is pinned rather than left to a comment."""
        self._open_delegation()
        first = self.page.locator(".delegation-models").first
        text = first.locator(".delegation-models-summary").inner_text()
        rows = first.locator(".delegation-model-table tbody tr").count()
        self.assertIn(
            str(rows), text,
            f"the summary ({text!r}) must state the {rows} rows it is hiding",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
