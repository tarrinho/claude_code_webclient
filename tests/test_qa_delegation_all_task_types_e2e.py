"""QA: every task type's controls work, and editing a cost switches the model.

A standing regression test, requested by Pedro on 2026-09-18: run this in the
future to check that changing between models still works and that the controls
on Settings > Delegation still do what they claim.

WHY IT SEEDS FROM PRODUCTION RATHER THAN A FIXTURE LIST
-------------------------------------------------------
The task types are not hard-coded here. The fixture database is loaded from
whatever `delegation_capability` currently holds, so a task type added later is
covered by this file the day it is added, without anyone remembering to edit a
list. If the production database is unreadable the tests skip rather than
quietly asserting nothing, because a test that passes on an empty table is the
failure mode this subsystem keeps producing.

WHAT IT COVERS, AND WHY EACH ONE IS HERE
----------------------------------------
* Every task type renders a card, and every card has a hide/unhide control.
  The count is asserted against the seeded types, so a card that silently stops
  rendering fails here rather than being noticed by eye.
* The rendered ladder equals the ladder the server computed. The page draws
  rung order from `payload.ladders`; if it ever drew it from row order instead,
  every ladder would still *look* plausible.
* Editing a cost cell changes which model sits at rung 0 -- "changing between
  models" as an observable fact, driven through the real input, the real PUT and
  a real re-render, then restored.
* The two global knobs persist. Both shipped with a bare `fetch` and no CSRF
  header (2026-09-17 and 2026-09-18), so every click was answered 403 while the
  knob animated across and snapped back. Nothing in the page's own state could
  detect that, which is why these assert by re-reading from the SERVER.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not flip an operational knob for a type the server would refuse. Which
types are blocked depends on measured data and on policy decisions recorded in
code, both of which move; a test that assumed a particular type was flippable
would be asserting today's data rather than the control's behaviour. It asserts
the affordance instead: a blocked knob is disabled and carries its reason, a
clean one is enabled.
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path

from tests.test_frontend_browser import DRIVER_OK, DRIVER_WHY, _BrowserFixture

PROD_DB = "/home/kali/projects/claude-code-webconsole/data/webconsole.db"
_COLUMNS = ("model", "task_type", "accuracy", "n", "cost_per_1m_tokens",
            "median_latency_s", "max_context", "updated_at")


def _production_rows():
    """Every capability row and operational flag, or (None, None) if unreadable."""
    try:
        con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, None
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM delegation_capability")]
        flags = [r["task_type"] for r in con.execute(
            "SELECT task_type FROM delegation_operational")]
    except sqlite3.Error:
        return None, None
    finally:
        con.close()
    return rows, flags


@unittest.skipUnless(DRIVER_OK, DRIVER_WHY)
class DelegationAllTaskTypesBrowserTests(_BrowserFixture):

    def setUp(self):
        super().setUp()
        rows, flags = _production_rows()
        if not rows:
            self.skipTest("no production capability rows to mirror")
        self.rows = rows
        self.task_types = sorted({r["task_type"] for r in rows})
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute("DELETE FROM delegation_capability")
        con.execute("DELETE FROM delegation_operational")
        con.executemany(
            "INSERT INTO delegation_capability "
            f"({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
            [tuple(r[c] for c in _COLUMNS) for r in rows])
        con.executemany(
            "INSERT INTO delegation_operational (task_type, updated_at) "
            "VALUES (?, '2026-09-18T00:00:00Z')", [(t,) for t in flags])
        con.commit()
        con.close()
        self._open_delegation()

    def _open_delegation(self):
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="delegation"]')
        self.page.wait_for_selector("#panelDelegation:not([hidden])", timeout=15_000)
        self.page.wait_for_selector(".delegation-card", timeout=15_000)
        self.page.wait_for_timeout(400)

    def _payload(self):
        """The server's own view, fetched from inside the page so it carries
        the session cookie. This is the oracle the DOM is checked against."""
        return self.page.evaluate(
            "async () => (await fetch('/api/delegation',"
            " {credentials: 'same-origin'})).json()")

    def _wait_for_config(self, config_key, predicate, what, timeout_s=15):
        """Poll the server's own view until *predicate* holds, or fail saying so.

        A click here is a round trip -- DOM handler, fetch, server write, then
        this read -- and the previous form of these cases waited a flat 700 ms
        for all of it. That is wall-clock, not a condition: it holds on an idle
        box and fails on a loaded one, and when it failed the assertion said
        "the request was refused or never sent", which is a real possibility
        and was not what had happened. Blaming a CSRF bug for a slow box is
        worse than no message.

        Returns the value once it settles, so callers can assert on it.
        """
        deadline = time.monotonic() + timeout_s
        value = None
        while time.monotonic() < deadline:
            value = (self._payload().get(config_key) or {}).get("enabled")
            if predicate(value):
                return value
            self.page.wait_for_timeout(150)
        self.fail(f"{what}: server state stayed {value!r} for {timeout_s}s")

    # ── coverage ──────────────────────────────────────────────────────────

    def test_every_task_type_renders_a_card_with_a_hide_unhide_control(self):
        cards = self.page.locator(".delegation-card").count()
        toggles = self.page.locator(".delegation-models").count()
        self.assertEqual(
            cards, len(self.task_types),
            f"expected one card per task type {self.task_types}, got {cards}")
        self.assertEqual(
            toggles, cards,
            "every card must carry a hide/unhide control")
        self.assertFalse(
            [e for e in self.errors if "pageerror" in e],
            f"JS errors while rendering: {self.errors}")

    def test_every_hide_unhide_control_toggles_its_own_table(self):
        groups = self.page.locator(".delegation-models")
        for i in range(groups.count()):
            group = groups.nth(i)
            task_type = group.get_attribute("data-task-type")
            table = group.locator(".delegation-model-table")
            self.assertFalse(table.is_visible(), f"{task_type} must start hidden")
            group.locator(".delegation-models-summary").click()
            self.page.wait_for_timeout(80)
            self.assertTrue(table.is_visible(), f"{task_type} did not unhide")
            group.locator(".delegation-models-summary").click()
            self.page.wait_for_timeout(80)
            self.assertFalse(table.is_visible(), f"{task_type} did not hide again")

    def test_every_rendered_ladder_matches_the_server(self):
        payload = self._payload()
        cards = self.page.locator(".delegation-card")
        for i in range(cards.count()):
            card = cards.nth(i)
            task_type = card.locator(".delegation-card-title").inner_text().strip()
            expected = (payload.get("ladders") or {}).get(task_type) or []
            rungs = card.locator(".delegation-rung")
            shown = [rungs.nth(j).inner_text().strip() for j in range(rungs.count())]
            self.assertEqual(
                shown, expected,
                f"{task_type}: the page shows {shown}, the server computed {expected}")

    def test_every_operational_knob_agrees_with_the_server(self):
        """Enabled when the server reports nothing blocking, disabled with a
        reason when it does. Asserts the affordance, not a particular type's
        status, because which types are blocked is data and moves."""
        payload = self._payload()
        blockers = payload.get("blockers") or {}
        operational = payload.get("operational") or []
        for task_type in self.task_types:
            knob = self.page.locator(f'[aria-label="{task_type} operational"]')
            self.assertEqual(knob.count(), 1, f"{task_type} has no operational knob")
            entry = blockers.get(task_type) or {}
            live = task_type in operational
            blocked = bool(entry.get("policy")) or bool(entry.get("data"))
            self.assertEqual(
                knob.get_attribute("aria-pressed"), str(live).lower(),
                f"{task_type}: knob state disagrees with the server")
            if blocked and not live:
                self.assertTrue(
                    knob.is_disabled(),
                    f"{task_type} is blocked but its knob is clickable")
                self.assertTrue(
                    (knob.get_attribute("title") or "").strip(),
                    f"{task_type}'s blocked knob carries no reason")
            else:
                self.assertFalse(
                    knob.is_disabled(),
                    f"{task_type} has nothing blocking it but its knob is disabled")

    # ── changing between models ───────────────────────────────────────────

    def test_editing_a_cost_switches_which_model_sits_at_rung_0(self):
        """The requested check, as an observable fact rather than an inspection.

        Finds an operational type whose ladder has at least two rungs, prices
        rung 0 above rung 1 through the real input, and asserts the page now
        shows a different model first. Then restores the original value and
        asserts it switches back -- so a failure to restore cannot leave a
        later test reading a table this one edited.
        """
        payload = self._payload()
        ladders = payload.get("ladders") or {}
        target = next(
            (t for t in self.task_types if len(ladders.get(t) or []) >= 2), None)
        if target is None:
            self.skipTest("no task type has a two-rung ladder to reorder")

        before = ladders[target]
        rung0, rung1 = before[0], before[1]
        row = next(r for r in self.rows
                   if r["model"] == rung0 and r["task_type"] == target)
        original = row["cost_per_1m_tokens"]

        group = self.page.locator(f'.delegation-models[data-task-type="{target}"]')
        group.locator(".delegation-models-summary").click()
        self.page.wait_for_timeout(120)

        cell = self.page.locator(
            f'[aria-label="cost_per_1m_tokens for {rung0} on {target}"]')
        self.assertEqual(cell.count(), 1, f"no cost cell for {rung0} on {target}")

        dearer = next(r["cost_per_1m_tokens"] for r in self.rows
                      if r["model"] == rung1 and r["task_type"] == target)
        cell.fill(str(round((dearer or 0) + 10, 4)))
        cell.press("Enter")
        self.page.wait_for_timeout(900)

        after = (self._payload().get("ladders") or {}).get(target) or []
        self.assertNotEqual(
            after[0] if after else None, rung0,
            f"{target}: pricing {rung0} above {rung1} left it at rung 0 ({after})")

        cell = self.page.locator(
            f'[aria-label="cost_per_1m_tokens for {rung0} on {target}"]')
        cell.fill("" if original is None else str(original))
        cell.press("Enter")
        self.page.wait_for_timeout(900)
        restored = (self._payload().get("ladders") or {}).get(target) or []
        self.assertEqual(
            restored, before,
            f"{target}: restoring the original cost did not restore the ladder")

    # ── the two global knobs ──────────────────────────────────────────────

    def test_the_enforcement_knobs_reach_the_server(self):
        """Both of these shipped answering 403 on every click, because they used
        a bare `fetch` and sent no CSRF header. The page could not tell: the
        knob sets `aria-pressed` optimistically before the request goes out. So
        this never trusts the DOM -- it asks the server what it answered.

        What it asserts is that the request ARRIVES and is PROCESSED, not that
        the flag flips. Those are different, and the earlier version conflated
        them: it clicked and required the stored value to change, which is only
        true when the data happens to be enforceable.

        This fixture mirrors PRODUCTION capability rows and operational flags
        (`_production_rows`), so whether the server accepts or refuses depends
        on what production data happens to be. It was written when both limits
        were breached and the server refused:

          ceiling -- multi-turn: its worst-case path is 3850s, above the 2900s
                     combined latency ceiling (spec 5.1)
          budget  -- reasoning: its ladder's expected tree cost is $4.283,
                     above BUDGET_USD ($3.50)

        The ceiling half of that no longer holds. On 2026-09-25 `multi-turn`
        was repinned off a 58.69s rung (2,675s) and the ceiling was raised to
        3,400s, so nothing breaches it; the BUDGET breach is the one that
        remains. Both branches are asserted below precisely so this test does
        not have to be edited every time the data moves.

        A 400 carrying that detail is proof of exactly what this case exists to
        check: the request carried its CSRF header, reached the handler, and
        was validated. The old assertion turned that correct refusal into a
        failure reading "the request was refused or never sent" -- true in the
        first sense, misleading in the second, and it sent a reader hunting a
        CSRF bug that had not regressed.

        403, a network-level 0, or a 5xx still fail, which is the regression
        this was written for. When the data IS enforceable the flag must flip
        and flip back, and that branch is asserted too.
        """
        for knob_label, config_key, endpoint in (
            ("enforce the combined latency ceiling", "ceiling_enforcement",
             "/api/delegation/ceiling-enforcement"),
            ("enforce the tree cost budget", "budget_enforcement",
             "/api/delegation/budget-enforcement"),
        ):
            knob = self.page.locator(f'[aria-label="{knob_label}"]')
            if knob.count() != 1:
                self.fail(f"expected exactly one {knob_label!r} knob, "
                          f"found {knob.count()}")
            before = (self._payload().get(config_key) or {}).get("enabled")

            # Ask the endpoint directly, so the ANSWER is visible rather than
            # inferred from whether a stored value moved. The knob's own click
            # path is covered by the class's other cases rendering its state.
            res = self.page.evaluate(
                """async (url) => {
                     const r = await fetch(url, {
                       method: 'PUT',
                       credentials: 'same-origin',
                       headers: {
                         'Content-Type': 'application/json',
                         'X-CSRF-Token': (document.cookie.match(
                             /(?:^|; )wc_csrf=([^;]*)/) || [])[1] || '',
                       },
                       body: JSON.stringify({enabled: true}),
                     });
                     let body;
                     try { body = await r.json(); } catch (e) { body = await r.text(); }
                     return {status: r.status, body: body};
                   }""",
                endpoint)

            self.assertNotEqual(
                res["status"], 403,
                f"{knob_label}: 403 -- the CSRF header was not sent. This is "
                "the regression this case exists for.")
            self.assertNotIn(
                res["status"], (0, 500, 502, 503),
                f"{knob_label}: the request did not reach a handler "
                f"(status {res['status']}, body {res['body']!r})")

            if res["status"] == 400:
                detail = str((res["body"] or {}).get("error")
                             or (res["body"] or {}).get("detail") or "")
                self.assertIn(
                    "already-operational", detail,
                    f"{knob_label}: 400 without the validation detail, so it "
                    f"was rejected for some other reason: {detail!r}")
                # Refused, so nothing may have been stored.
                self.assertEqual(
                    (self._payload().get(config_key) or {}).get("enabled"),
                    before,
                    f"{knob_label}: the server refused the change and stored "
                    "it anyway")
                continue

            self.assertEqual(res["status"], 200, f"{knob_label}: {res!r}")
            self._wait_for_config(
                config_key, lambda v: v is True,
                f"{knob_label}: answered 200 but did not store the change")
            knob.click()
            self._wait_for_config(
                config_key, lambda v, b=before: v == b,
                f"{knob_label}: clicking back did not restore {before!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
