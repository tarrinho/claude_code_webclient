# Tiered Agent Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build every mechanism the tiered-agent-delegation spec describes — classifier, oracle, three review gates, blast-radius check, derived deadlines, benchmark table, startup validation and settings page — while **nothing routes**: every task type ships non-operational and model selection behaves exactly as it does today.

**Architecture:** A pure classifier and a pure router sit behind the existing `ModelRouter.assign_model`. The benchmark table moves from a markdown snapshot into a real database table, read at startup into the `CapabilityTable` that `tiered_delegation.py` already implements. A per-task-type `operational` flag, default false, is the only thing that makes a type routable; with none set, `assign_model` falls through to today's behaviour and the pipeline never runs. The five-stage pipeline is built as separate, independently testable gates rather than one function.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, pytest (stdlib `unittest` style, as the repo uses), vanilla JS for the settings page.

**Spec:** `docs/superpowers/specs/2026-09-14-tiered-agent-delegation-spec-v3.md`

## Global Constraints

- **Nothing routes.** Every task type is non-operational (`operational` defaults false, spec §1.1). A non-operational type falls back to today's routing and each fallback is logged.
- **`coding` must NOT be flipped operational.** Spec §12 forbids it until the gate-type validation question is decided. No task in this plan sets it.
- **§12's three open items stay open.** Do not resolve the ≥70% free-rung target, the gate-type dependency, or the `claude_proxy.py` drift.
- **A model is never a bare string.** Every routing decision returns `(model, machine)` together (spec §9.3, CLAUDE.md §0.1). A model id alone reaches a gateway that does not serve it and returns `429 "No deployments available"`.
- **Run tests with `.venv/bin/python -m pytest`, invoked bare**, never `pytest tests/`. Any other interpreter skips the browser layer.
- **Never run `db.init()` against the production database.** It migrates. Copy to a throwaway `WC_DB_PATH` first.
- **Test files are `tests/test_qa_<topic>.py`**, `unittest` classes, with a module docstring saying what defect the file exists to catch.
- **Every new test is mutation-checked**: break the thing, confirm a *specific* test fails, restore. A test with no failing case is not a test.
- **Version is 0.19.0** — bumped in Task 10, not before.

---

### Task 1: Classifier — `(task_type, score, mutates)`

**Files:**
- Create: `delegation_classifier.py`
- Test: `tests/test_qa_delegation_classifier.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Classification` — frozen dataclass with `task_type: str`, `score: int`, `mutates: str`.
  - `classify(text: str) -> Classification`
  - `MUTATES_FALSE = "false"`, `MUTATES_SIDE_EFFECTING_READ = "side_effecting_read"`, `MUTATES_TRUE = "true"` — the three values of spec §2.3.
  - `PATTERNS: tuple[tuple[str, int, str, str], ...]` — `(regex, score, task_type, mutates)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_qa_delegation_classifier.py
"""QA: the classifier of spec section 2.

Three properties that are easy to get wrong and invisible when wrong:
specificity beats list order, a score is the MAXIMUM of matched patterns
rather than the winning pattern's own, and both defaults fail toward the
expensive branch.
"""
from __future__ import annotations

import unittest

import delegation_classifier as dc


class ClassifyTests(unittest.TestCase):
    def test_a_plain_read_is_long_context_and_does_not_mutate(self):
        result = dc.classify("read file config.py and summarize log output")
        self.assertEqual(result.task_type, "long-context")
        self.assertEqual(result.score, 1)
        self.assertEqual(result.mutates, dc.MUTATES_FALSE)

    def test_unmatched_text_defaults_to_comprehension_and_mutates(self):
        """Section 2.2: both defaults fail toward the expensive/safe branch."""
        result = dc.classify("zzzz nothing matches this at all")
        self.assertEqual(result.task_type, "comprehension")
        self.assertEqual(result.mutates, dc.MUTATES_TRUE)

    def test_score_is_the_maximum_of_every_match_not_the_winner_s(self):
        """Section 2.1. Score feeds the size factor and therefore the deadline;
        under-budgeting manufactures a false timeout, which costs a rung."""
        # "simple" scores 1; "orchestrate" scores 5. Both match.
        result = dc.classify("a simple change to orchestrate the agents")
        self.assertEqual(result.score, 5)

    def test_a_mutates_conflict_resolves_to_true(self):
        """Section 2.1: if any two matched patterns disagree, the answer is True."""
        # "read file" is False; "fix typo" is True.
        result = dc.classify("read file and fix typo in it")
        self.assertEqual(result.mutates, dc.MUTATES_TRUE)

    def test_task_type_is_decided_by_specificity_not_list_order(self):
        """The most specific matching pattern wins. 'migrate database' is a
        longer, more specific pattern than 'simple'."""
        result = dc.classify("simple migrate database task")
        self.assertEqual(result.task_type, "coding")


class MutatesVocabularyTests(unittest.TestCase):
    def test_the_three_values_are_distinct(self):
        """Section 2.3 is three-valued. Folding side_effecting_read into
        either neighbour is the mistake this guards."""
        values = {dc.MUTATES_FALSE, dc.MUTATES_SIDE_EFFECTING_READ, dc.MUTATES_TRUE}
        self.assertEqual(len(values), 3)

    def test_every_pattern_declares_a_known_mutates_value(self):
        known = {dc.MUTATES_FALSE, dc.MUTATES_SIDE_EFFECTING_READ, dc.MUTATES_TRUE}
        for pattern, _score, _task_type, mutates in dc.PATTERNS:
            self.assertIn(mutates, known, pattern)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'delegation_classifier'`

- [ ] **Step 3: Write the implementation**

```python
# delegation_classifier.py
"""Classify a task into (task_type, score, mutates) -- spec section 2.

Split out of orchestrator.COMPLEXITY_PATTERNS, which returned a score alone.
The two extra fields are what the rest of the design needs: task_type selects
the ladder, and mutates decides placement (section 8) and which pipeline
stages run (section 4.7).

Pure: no database, no config read, no import from app. The router is testable
by table without spawning anything (section 11).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

MUTATES_FALSE: Final[str] = "false"
MUTATES_SIDE_EFFECTING_READ: Final[str] = "side_effecting_read"
MUTATES_TRUE: Final[str] = "true"

#: (regex, score, task_type, mutates) -- spec section 2's table, verbatim.
PATTERNS: Final[tuple[tuple[str, int, str, str], ...]] = (
    (r"architect|design.*system|create.*framework", 4, "planning", MUTATES_FALSE),
    (r"implement.*multiple|coordinate.*agent|orchestrate", 5, "planning", MUTATES_TRUE),
    (r"debug.*complex|trace.*error.*chain|performance.*bottleneck", 4, "reasoning", MUTATES_FALSE),
    (r"write.*test.*suite|integration.*test|e2e.*test", 3, "coding", MUTATES_TRUE),
    (r"analyze.*code.*review|refactor.*large|migrate.*database", 4, "coding", MUTATES_TRUE),
    (r"write.*doc.*umentation|create.*tutorial|explain.*concept", 2, "comprehension", MUTATES_TRUE),
    (r"research.*api.*document|find.*replacement|evaluate.*option", 3, "comprehension", MUTATES_FALSE),
    (r"read.*file|list.*directory|grep.*pattern|summarize.*log", 1, "long-context", MUTATES_FALSE),
    (r"simple|small|quick|minor|fix.*typo", 1, "coding", MUTATES_TRUE),
)

DEFAULT_TASK_TYPE: Final[str] = "comprehension"
DEFAULT_MUTATES: Final[str] = MUTATES_TRUE


@dataclass(frozen=True)
class Classification:
    task_type: str
    score: int
    mutates: str


def classify(text: str) -> Classification:
    """Spec section 2, including 2.1's three resolution rules.

    Every multi-match is worth logging in production so pattern refinement is
    driven by data rather than guesswork (2.1); the caller does that, because
    this module stays free of a logger for the same reason it stays free of a
    database -- it is a pure function and its tests say so.
    """
    lowered = (text or "").lower()
    matched = [p for p in PATTERNS if re.search(p[0], lowered)]
    if not matched:
        return Classification(DEFAULT_TASK_TYPE, 1, DEFAULT_MUTATES)

    # task_type: the most specific pattern wins, and specificity is the
    # pattern's own length. Not list order -- ordering a dict was how this
    # silently depended on insertion sequence.
    most_specific = max(matched, key=lambda p: len(p[0]))

    # score: the HIGHEST among all matches, not the winning pattern's own.
    # Score feeds the size factor and therefore the deadline (5.1), and
    # under-budgeting a deadline manufactures a false timeout escalation.
    score = max(p[1] for p in matched)

    # mutates: any disagreement resolves to True (2.1), and side_effecting_read
    # outranks False for the same reason -- both are refused a transport.
    values = {p[3] for p in matched}
    if MUTATES_TRUE in values:
        mutates = MUTATES_TRUE
    elif MUTATES_SIDE_EFFECTING_READ in values:
        mutates = MUTATES_SIDE_EFFECTING_READ
    else:
        mutates = MUTATES_FALSE

    return Classification(most_specific[2], score, mutates)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_classifier.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Mutation-check the score rule**

Change `score = max(p[1] for p in matched)` to `score = most_specific[1]`, run the file, and confirm **only** `test_score_is_the_maximum_of_every_match_not_the_winner_s` fails. Restore.

- [ ] **Step 6: Commit**

```bash
git add delegation_classifier.py tests/test_qa_delegation_classifier.py
git commit -m "feat: classifier emits (task_type, score, mutates) per spec section 2"
```

---

### Task 2: The benchmark table becomes a database table

**Files:**
- Create: `routes/db_delegation.py`
- Modify: `db.py` (add `CREATE TABLE` beside `spec_status` near line 487; add three entries to the `__getattr__` symbol map near line 165)
- Test: `tests/test_qa_delegation_table.py`

**Interfaces:**
- Consumes: `tiered_delegation.CapabilityRow` (fields: `model`, `task_type`, `accuracy`, `n`, `cost_per_1m_tokens`, `median_latency_s`, `max_context`).
- Produces:
  - `async delegation_rows_all() -> list[dict]`
  - `async delegation_row_set(model: str, task_type: str, **columns) -> bool`
  - `async delegation_operational_all() -> set[str]`
  - `async delegation_operational_set(task_type: str, operational: bool) -> bool`
  - `rows_to_capability(rows: list[dict]) -> list[CapabilityRow]`

- [ ] **Step 1: Add the schema to `db.py`**

Beside the `spec_status` table (db.py around line 487), inside the same `executescript`:

```sql
        -- The spec 2.6 benchmark table. A real table, not a constant in
        -- source: section 2.6 says so outright, because the settings page
        -- (9.2) writes it, the re-benchmark job (10.2) writes it, and the
        -- ladder generator (3) reads it. Two copies would drift, and the
        -- drift would show up as a ladder that disagrees with the page that
        -- claims to configure it.
        CREATE TABLE IF NOT EXISTS delegation_capability (
            model             TEXT NOT NULL,
            task_type         TEXT NOT NULL,
            accuracy          REAL,
            n                 INTEGER,
            cost_per_1m_tokens REAL,
            median_latency_s  REAL,
            max_context       INTEGER,
            updated_at        TEXT NOT NULL,
            PRIMARY KEY (model, task_type)
        );

        -- Which task types are routable. Absent means non-operational, which
        -- is 1.1's bootstrap default: a type is submitted to validation by
        -- being flipped here, and that is the only way it becomes routable.
        CREATE TABLE IF NOT EXISTS delegation_operational (
            task_type  TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL
        );
```

- [ ] **Step 2: Register the symbols in `db.py`'s `__getattr__` map**

Beside the `spec_status` entries (db.py around line 165):

```python
        # tiered delegation
        "delegation_rows_all": "routes.db_delegation",
        "delegation_row_set": "routes.db_delegation",
        "delegation_operational_all": "routes.db_delegation",
        "delegation_operational_set": "routes.db_delegation",
```

- [ ] **Step 3: Write the failing test**

```python
# tests/test_qa_delegation_table.py
"""QA: the spec 2.6 benchmark table as a real database table.

Section 2.6 is explicit that this is a table rather than a constant, because
three separate things write and read it. The properties pinned here are the
ones that make it usable as a source of truth: a row round-trips with its
NULLs intact, and NULL is not confused with zero.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
from routes.db_delegation import rows_to_capability


class DelegationTableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start(); self.root_patch.start()
        self.addCleanup(self.db_patch.stop); self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_a_row_round_trips(self):
        await db.delegation_row_set(
            "vllm/Qwen3.6-35B-A3B-NVFP4", "coding",
            accuracy=0.66, n=44, cost_per_1m_tokens=0.0,
            median_latency_s=26.8, max_context=229376)
        rows = await db.delegation_rows_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(rows[0]["n"], 44)

    async def test_null_accuracy_is_preserved_and_is_not_zero(self):
        """A TBD accuracy makes a row ineligible (2.6). Reading it back as 0.0
        would make it eligible and measured-worst, which is the opposite."""
        await db.delegation_row_set("azure_ai/gpt-5.4-mini", "coding",
                                    accuracy=None, n=None,
                                    cost_per_1m_tokens=0.5261,
                                    median_latency_s=None, max_context=1050000)
        rows = await db.delegation_rows_all()
        self.assertIsNone(rows[0]["accuracy"])

    async def test_zero_cost_is_preserved_and_is_not_null(self):
        """Zero is a real rate -- the self-hosted model is free. None means
        unpriced, which 2.7 makes ineligible rather than free."""
        await db.delegation_row_set("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        rows = await db.delegation_rows_all()
        self.assertEqual(rows[0]["cost_per_1m_tokens"], 0.0)
        self.assertIsNotNone(rows[0]["cost_per_1m_tokens"])

    async def test_setting_the_same_pair_twice_updates_rather_than_duplicates(self):
        await db.delegation_row_set("m", "coding", accuracy=0.5, n=2,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=1.0, max_context=1000)
        await db.delegation_row_set("m", "coding", accuracy=0.9, n=20,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=1.0, max_context=1000)
        rows = await db.delegation_rows_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["accuracy"], 0.9)

    async def test_no_task_type_is_operational_by_default(self):
        """1.1's bootstrap exemption, and this plan's whole scope: nothing
        routes until something is flipped, and nothing flips it here."""
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_operational_can_be_set_and_cleared(self):
        await db.delegation_operational_set("long-context", True)
        self.assertEqual(await db.delegation_operational_all(), {"long-context"})
        await db.delegation_operational_set("long-context", False)
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_rows_convert_to_capability_rows(self):
        await db.delegation_row_set("m", "coding", accuracy=0.66, n=44,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=26.8, max_context=229376)
        rows = rows_to_capability(await db.delegation_rows_all())
        self.assertEqual(rows[0].model, "m")
        self.assertEqual(rows[0].accuracy, 0.66)
```

- [ ] **Step 4: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_table.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routes.db_delegation'`

- [ ] **Step 5: Write `routes/db_delegation.py`**

```python
# db_delegation.py -- the spec 2.6 benchmark table and the operational flags.
#
# Section 2.6 says this is a database table rather than a constant in source,
# and names the three things that touch it: the settings page (9.2) writes it,
# the re-benchmark job (10.2) writes it, and the ladder generator (3) reads it.
# The markdown table in the spec is a snapshot of this table's contents, not a
# second source of truth.
from __future__ import annotations

from typing import Any

import db
from tiered_delegation import CapabilityRow

_COLUMNS = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context")


async def delegation_rows_all() -> list[dict[str, Any]]:
    """Every (model, task_type) row. NULL stays None."""
    cur = await db.db_conn.execute(
        "SELECT model, task_type, accuracy, n, cost_per_1m_tokens, "
        "median_latency_s, max_context FROM delegation_capability "
        "ORDER BY task_type, model"
    )
    return [dict(row) for row in await cur.fetchall()]


async def delegation_row_set(model: str, task_type: str, **columns: Any) -> bool:
    """Insert or update one row. Unknown column names are refused rather than
    silently dropped -- a typo in a column name would otherwise read as a
    successful write of nothing."""
    unknown = set(columns) - set(_COLUMNS)
    if unknown:
        raise ValueError(f"unknown capability columns: {sorted(unknown)}")
    values = {c: columns.get(c) for c in _COLUMNS}
    await db.db_conn.execute(
        "INSERT INTO delegation_capability "
        "(model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, "
        " max_context, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(model, task_type) DO UPDATE SET "
        "  accuracy = excluded.accuracy, n = excluded.n, "
        "  cost_per_1m_tokens = excluded.cost_per_1m_tokens, "
        "  median_latency_s = excluded.median_latency_s, "
        "  max_context = excluded.max_context, updated_at = excluded.updated_at",
        (model, task_type, values["accuracy"], values["n"],
         values["cost_per_1m_tokens"], values["median_latency_s"],
         values["max_context"], db._now()),
    )
    await db.db_conn.commit()
    return True


async def delegation_operational_all() -> set[str]:
    """Task types flagged routable. Absent means non-operational (1.1)."""
    cur = await db.db_conn.execute("SELECT task_type FROM delegation_operational")
    return {row["task_type"] for row in await cur.fetchall()}


async def delegation_operational_set(task_type: str, operational: bool) -> bool:
    if operational:
        await db.db_conn.execute(
            "INSERT INTO delegation_operational (task_type, updated_at) "
            "VALUES (?, ?) ON CONFLICT(task_type) DO UPDATE SET "
            "updated_at = excluded.updated_at",
            (task_type, db._now()),
        )
    else:
        await db.db_conn.execute(
            "DELETE FROM delegation_operational WHERE task_type = ?", (task_type,))
    await db.db_conn.commit()
    return True


def rows_to_capability(rows: list[dict[str, Any]]) -> list[CapabilityRow]:
    """Database rows -> the dataclass the ladder generator already takes."""
    return [
        CapabilityRow(
            model=row["model"], task_type=row["task_type"],
            accuracy=row["accuracy"], n=row["n"],
            cost_per_1m_tokens=row["cost_per_1m_tokens"],
            median_latency_s=row["median_latency_s"],
            max_context=row["max_context"],
        )
        for row in rows
    ]
```

- [ ] **Step 6: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_table.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Commit**

```bash
git add routes/db_delegation.py db.py tests/test_qa_delegation_table.py
git commit -m "feat: the delegation benchmark table becomes a real database table"
```

---

### Task 3: Startup validation and the operational flag

**Files:**
- Create: `delegation_startup.py`
- Modify: `app.py` (call it from the lifespan, beside the other startup steps near line 409)
- Test: `tests/test_qa_delegation_startup.py`

**Interfaces:**
- Consumes: `db.delegation_rows_all`, `db.delegation_operational_all`, `routes.db_delegation.rows_to_capability`, `tiered_delegation.CapabilityTable.validate`.
- Produces:
  - `async load_capability_table() -> CapabilityTable`
  - `async validate_or_die() -> CapabilityTable` — raises `DelegationConfigError` listing every broken invariant.
  - `class DelegationConfigError(RuntimeError)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_startup.py
"""QA: spec 1.1's startup validation, and its bootstrap exemption.

The exemption is the part worth testing hardest. Section 2.6 ships mostly
unmeasured, so a check that refused every TBD would mean the system could
never start a first time -- and a validation gate that cannot be satisfied
gets satisfied with junk values instead.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
import delegation_startup as ds


class StartupValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _row(self, model, task_type, **kw):
        defaults = dict(accuracy=None, n=None, cost_per_1m_tokens=None,
                        median_latency_s=None, max_context=None)
        defaults.update(kw)
        await db.delegation_row_set(model, task_type, **defaults)

    async def test_an_empty_table_starts_fine(self):
        """The bootstrap case: nothing measured, nothing operational."""
        table = await ds.validate_or_die()
        self.assertEqual(table.ladder("coding"), [])

    async def test_a_non_operational_type_may_hold_tbd(self):
        await self._row("m", "coding", cost_per_1m_tokens=1.0)
        await ds.validate_or_die()  # must not raise

    async def test_an_operational_type_with_a_tbd_ladder_row_refuses_to_start(self):
        await self._row("m", "coding", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await db.delegation_operational_set("coding", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        self.assertIn("coding", str(ctx.exception))

    async def test_an_operational_type_with_no_eligible_row_refuses_to_start(self):
        """'At least one ladder-eligible row must exist' -- a type whose rows
        are all TBD has an empty ladder, and an empty ladder cannot route."""
        await self._row("m", "coding", cost_per_1m_tokens=1.0)
        await db.delegation_operational_set("coding", True)
        with self.assertRaises(ds.DelegationConfigError):
            await ds.validate_or_die()

    async def test_a_complete_operational_type_starts(self):
        await self._row("m", "long-context", accuracy=1.0, n=10,
                        cost_per_1m_tokens=0.0, median_latency_s=12.0,
                        max_context=229376)
        await db.delegation_operational_set("long-context", True)
        table = await ds.validate_or_die()
        self.assertEqual(table.ladder("long-context"), ["m"])

    async def test_the_error_names_every_broken_invariant_not_just_the_first(self):
        """'The error lists every broken invariant so the operator can fix the
        data before deployment' -- one at a time means one restart each."""
        await self._row("a", "coding", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await self._row("b", "reasoning", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await db.delegation_operational_set("coding", True)
        await db.delegation_operational_set("reasoning", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        message = str(ctx.exception)
        self.assertIn("coding", message)
        self.assertIn("reasoning", message)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_startup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'delegation_startup'`

- [ ] **Step 3: Write `delegation_startup.py`**

```python
# delegation_startup.py -- load the capability table and enforce spec 1.1.
#
# Kept apart from tiered_delegation.py because that module is deliberately
# pure: no database, no config read, no import from app. This is the half
# that touches the database, so the pure half stays testable against a
# constructed table rather than against production data.
from __future__ import annotations

import logging

import db
from routes.db_delegation import rows_to_capability
from tiered_delegation import CapabilityTable

_log = logging.getLogger("wc.app")


class DelegationConfigError(RuntimeError):
    """Spec 1.1: the system refuses to start rather than route on bad data."""


async def load_capability_table() -> CapabilityTable:
    rows = rows_to_capability(await db.delegation_rows_all())
    operational = await db.delegation_operational_all()
    return CapabilityTable(rows, operational=operational)


async def validate_or_die() -> CapabilityTable:
    """Build the table and refuse to start if any invariant is broken.

    Every problem is reported at once. Reporting the first only means one
    restart per problem, and the operator is fixing data in a settings page
    rather than reading a stack trace.
    """
    table = await load_capability_table()
    problems = table.validate()
    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise DelegationConfigError(
            "tiered delegation configuration is invalid; refusing to start:\n"
            + detail
        )
    operational = sorted(table._operational)
    if operational:
        _log.info("delegation: operational task types: %s", ", ".join(operational))
    else:
        # The shipped state for 0.19.0. Said out loud so an operator wondering
        # why nothing routes finds the answer in the log rather than in a spec.
        _log.info(
            "delegation: no task type is operational; routing falls back to "
            "the existing behaviour for every task"
        )
    return table
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_startup.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Wire it into `app.py`'s lifespan**

In `app.py`, beside the other startup steps (near line 409, before the `yield`):

```python
    # Spec 1.1. Fails loudly rather than routing on bad data -- and on this
    # release no task type is operational, so this validates an empty set and
    # logs that nothing routes.
    from delegation_startup import validate_or_die
    app.state.capability_table = await validate_or_die()
```

- [ ] **Step 6: Verify the app still boots**

Run:
```bash
WC_DB_PATH=/tmp/x/db WC_PROJECTS_ROOT=/tmp/x/p WC_SESSION_SECRET=verify-only \
WC_PROXY_TOKEN=verify-only-not-a-real-token-32chars \
  .venv/bin/python -c "import app; print('imports OK')"
```
Expected: `imports OK`

- [ ] **Step 7: Commit**

```bash
git add delegation_startup.py app.py tests/test_qa_delegation_startup.py
git commit -m "feat: spec 1.1 startup validation, with nothing operational"
```

---

### Task 4: Fix `assign_model`'s duplicate branch and add the routing seam

**Files:**
- Modify: `orchestrator.py:277-299` (`ModelRouter.assign_model`)
- Test: `tests/test_qa_delegation_routing.py`

**Interfaces:**
- Consumes: `delegation_classifier.classify`, `tiered_delegation.CapabilityTable.ladder`.
- Produces: `ModelRouter.assign_model(task_title, task_desc, complexity=1, table=None) -> str` — unchanged signature plus an optional `table`.

**Why this task matters most:** spec §1 opens by naming this defect. Both branches of the complexity check return `config.ANTHROPIC_MODEL`, so complexity is computed and discarded. The seam must be added **without changing behaviour**, because nothing is operational.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_routing.py
"""QA: the routing seam, and the guarantee that it changes nothing yet.

Spec section 1: assign_model returns config.ANTHROPIC_MODEL on both branches,
so the computed complexity is discarded. This closes that seam -- and the
headline test is that with no task type operational, routing is byte-identical
to what it was, because that is this release's whole claim.
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
        self.router = ModelRouter(rules=[])

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
        router = ModelRouter(rules=[{"pattern": "refactor", "model": "ruled/model"}])
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_routing.py -v`
Expected: FAIL — `assign_model() got an unexpected keyword argument 'table'`

- [ ] **Step 3: Replace `assign_model` in `orchestrator.py`**

```python
    def assign_model(
        self, task_title: str, task_desc: str, complexity: int = 1,
        table: Any | None = None,
    ) -> str:
        """Pick the model for a task.

        Order: an operator's explicit rule, then the measured ladder for an
        *operational* task type, then today's fallback.

        `table` is optional and defaults to None, which is what every existing
        call site passes -- with no table, or with one whose task types are
        all non-operational, this returns exactly what it returned before.
        That is release 0.19.0's claim and tests/test_qa_delegation_routing.py
        asserts it.

        The previous fallback was `if complexity >= 4: return X` followed by
        `return X` -- the same value on both branches, so complexity was
        computed and discarded (spec section 1). The parameter is now used, or
        it is honestly unused; it is no longer pretend.
        """
        combined = (task_title + " " + task_desc).lower()

        for rule in self.rules:
            pattern = rule.get("pattern", "")
            model = rule.get("model", "")
            if pattern and model:
                try:
                    if re.search(pattern, combined):
                        return model
                except re.error:
                    _log.warning(
                        "Invalid regex in model routing rule: %s", pattern
                    )

        if table is not None:
            from delegation_classifier import classify
            decision = classify(combined)
            if table.is_operational(decision.task_type):
                rungs = table.ladder(decision.task_type)
                if rungs:
                    return rungs[0]
                # An operational type with an empty ladder is refused at
                # startup (1.1). Reaching here means a table built some other
                # way, and falling back beats raising inside a router.
                _log.warning(
                    "delegation: %s is operational but its ladder is empty; "
                    "falling back", decision.task_type,
                )

        return config.ANTHROPIC_MODEL
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_routing.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the orchestrator's existing tests — nothing may change**

Run: `.venv/bin/python -m pytest tests/ -q -k "orchestrator or supervisor"`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add orchestrator.py tests/test_qa_delegation_routing.py
git commit -m "fix: assign_model stops discarding complexity, and gains the ladder seam"
```

---

### Task 5: The coding oracle (spec §4.2)

**Files:**
- Create: `delegation_oracle.py`
- Test: `tests/test_qa_delegation_oracle.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class OracleVerdict` — frozen dataclass: `passed: bool`, `reason: str`, `infrastructure_failure: bool`.
  - `check_python(code: str, timeout_s: float = 10.0) -> OracleVerdict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_oracle.py
"""QA: spec 4.2, stage 2 -- execution verification only.

The distinction this file exists to hold: "the code failed verification" and
"the harness broke" are different signals. Section 4.2 says the second is
tagged separately so production accuracy metrics are not polluted by tooling
failures, and a test that only checks `passed` cannot tell them apart.
"""
from __future__ import annotations

import unittest

import delegation_oracle as oracle


class OracleTests(unittest.TestCase):
    def test_code_that_parses_and_runs_passes(self):
        verdict = oracle.check_python("def f():\n    return 1\n")
        self.assertTrue(verdict.passed)
        self.assertFalse(verdict.infrastructure_failure)

    def test_code_that_does_not_parse_fails(self):
        verdict = oracle.check_python("def f(:\n")
        self.assertFalse(verdict.passed)
        self.assertIn("Syntax", verdict.reason)

    def test_a_syntax_failure_is_not_an_infrastructure_failure(self):
        """The whole point of the second field. Bad code is the model's
        problem; a broken sandbox is ours, and averaging them together hides
        both."""
        verdict = oracle.check_python("def f(:\n")
        self.assertFalse(verdict.infrastructure_failure)

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        """'No extractable code' was a real benchmark outcome. Passing it
        would score a non-answer as correct."""
        verdict = oracle.check_python("")
        self.assertFalse(verdict.passed)

    def test_a_timeout_is_an_infrastructure_failure(self):
        verdict = oracle.check_python("while True:\n    pass\n", timeout_s=0.5)
        self.assertFalse(verdict.passed)
        self.assertTrue(verdict.infrastructure_failure)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_oracle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'delegation_oracle'`

- [ ] **Step 3: Write `delegation_oracle.py`**

```python
# delegation_oracle.py -- spec 4.2, stage 2 of the coding pipeline.
#
# Execution verification only: does the produced code parse, import, compile?
# QA and regression testing are stage 3 and deliberately not here -- section
# 4.2 separates them so a compile failure and a regression failure are
# distinguishable signals rather than one undifferentiated "it failed".
from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OracleVerdict:
    passed: bool
    reason: str
    #: True when the harness failed rather than the code. Section 4.2 keeps
    #: this separate so tooling failures do not pollute accuracy metrics --
    #: a sandbox crash counted as a wrong answer makes a model look worse
    #: every time the box is busy.
    infrastructure_failure: bool = False


def check_python(code: str, timeout_s: float = 10.0) -> OracleVerdict:
    """Compile *code* in a subprocess and report what happened.

    A subprocess rather than `compile()` in-process: the point is to find out
    whether the produced code runs, and importing it here would run it in the
    server's own interpreter.
    """
    if not code.strip():
        # A real benchmark outcome ("no extractable code in the response").
        # Passing it would score a non-answer as correct.
        return OracleVerdict(False, "no code produced")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-c", f"import py_compile,sys;"
                 f"py_compile.compile({str(path)!r}, doraise=True)"],
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return OracleVerdict(False, f"timed out after {timeout_s}s",
                                 infrastructure_failure=True)
        except OSError as exc:
            return OracleVerdict(False, f"could not run the checker: {exc}",
                                 infrastructure_failure=True)

    if result.returncode == 0:
        return OracleVerdict(True, "compiles")
    detail = (result.stderr or result.stdout or "").strip()
    return OracleVerdict(False, detail.splitlines()[-1] if detail else "did not compile")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_oracle.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add delegation_oracle.py tests/test_qa_delegation_oracle.py
git commit -m "feat: the coding oracle, with infrastructure failure tagged apart"
```

---

### Task 6: Blast radius, the trivial bypass, and stage precedence

**Files:**
- Create: `delegation_pipeline.py`
- Test: `tests/test_qa_delegation_stages.py`

**Interfaces:**
- Consumes: `delegation_classifier.Classification`, `MUTATES_*`.
- Produces:
  - `MAX_FILES_TRIVIAL: int = 3`
  - `stages_for(decision: Classification, files_changed: int) -> list[int]` — which of stages 1–5 run.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_stages.py
"""QA: which pipeline stages run -- spec 4.6, 4.7 and 4.8, and their order.

Three rules each subtract stages, so the order they resolve in is the whole
behaviour. The table in 4.8 is the specification and this file is that table.
"""
from __future__ import annotations

import unittest

import delegation_classifier as dc
import delegation_pipeline as pipeline


def _d(task_type="coding", score=3, mutates=dc.MUTATES_TRUE):
    return dc.Classification(task_type, score, mutates)


class StageSelectionTests(unittest.TestCase):
    def test_a_non_trivial_write_runs_all_five(self):
        self.assertEqual(pipeline.stages_for(_d(score=3), files_changed=1),
                         [1, 2, 3, 4, 5])

    def test_a_trivial_write_runs_one_and_two(self):
        self.assertEqual(pipeline.stages_for(_d(score=1), files_changed=1),
                         [1, 2])

    def test_blast_radius_overrides_the_trivial_bypass(self):
        """4.6: measured blast radius beats the classifier's guess from prompt
        text. A score-1 task touching more than MAX_FILES_TRIVIAL files gets
        the full pipeline."""
        self.assertEqual(
            pipeline.stages_for(_d(score=1),
                                files_changed=pipeline.MAX_FILES_TRIVIAL + 1),
            [1, 2, 3, 4, 5])

    def test_a_read_only_task_runs_one_to_three(self):
        """4.7: stages 4 and 5 check consequences of changing something, and
        a task that changes nothing cannot produce them."""
        self.assertEqual(
            pipeline.stages_for(_d(score=3, mutates=dc.MUTATES_FALSE),
                                files_changed=0),
            [1, 2, 3])

    def test_a_trivial_read_runs_one_and_two_not_one_to_three(self):
        """The precedence case. The bypass removes stage 3 before 4.7 is
        reached, so read-only does not put it back -- no rule ever ADDS a
        stage an earlier rule removed."""
        self.assertEqual(
            pipeline.stages_for(_d(score=1, mutates=dc.MUTATES_FALSE),
                                files_changed=0),
            [1, 2])

    def test_side_effecting_read_takes_all_five(self):
        """4.7 covers mutates=False only. side_effecting_read spends money or
        consumes a rate limit, so it has real consequences to review."""
        self.assertEqual(
            pipeline.stages_for(_d(score=3, mutates=dc.MUTATES_SIDE_EFFECTING_READ),
                                files_changed=0),
            [1, 2, 3, 4, 5])

    def test_blast_radius_never_fires_on_a_read(self):
        """Its blast radius is zero by definition, so the override exists only
        for writes misclassified as trivial."""
        self.assertEqual(
            pipeline.stages_for(_d(score=1, mutates=dc.MUTATES_FALSE),
                                files_changed=99),
            [1, 2])
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_stages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'delegation_pipeline'`

- [ ] **Step 3: Write `delegation_pipeline.py`**

```python
# delegation_pipeline.py -- which of the five stages run, and in what order.
#
# Spec 4.8's precedence table, as code. Three rules each subtract stages, so
# the order they resolve in is the whole behaviour:
#
#   1. blast radius (4.6) -- the only rule based on what the task DID rather
#      than what its text predicted, so it overrides the trivial bypass
#   2. the trivial bypass (4.8), if blast radius did not override it
#   3. read-only (4.7), applied to whatever survives
#
# No rule ever ADDS a stage an earlier rule removed.
from __future__ import annotations

from typing import Final

import delegation_classifier as dc

#: Above this, a task gets the full pipeline whatever the classifier said.
MAX_FILES_TRIVIAL: Final[int] = 3

ALL_STAGES: Final[list[int]] = [1, 2, 3, 4, 5]
TRIVIAL_SCORE: Final[int] = 1


def stages_for(decision: dc.Classification, files_changed: int) -> list[int]:
    """The stages to run for one leaf."""
    stages = list(ALL_STAGES)

    # 1. Blast radius first. A read-only task has a blast radius of zero by
    #    definition, so this never fires on one -- the override exists for
    #    writes that were misclassified as trivial.
    over_blast_radius = files_changed > MAX_FILES_TRIVIAL

    # 2. The trivial bypass, unless blast radius overrode it.
    if decision.score <= TRIVIAL_SCORE and not over_blast_radius:
        stages = [s for s in stages if s in (1, 2)]

    # 3. Read-only removes 4 and 5 from whatever survives. mutates=False only:
    #    side_effecting_read spends money or consumes an external rate limit,
    #    so it keeps all five.
    if decision.mutates == dc.MUTATES_FALSE:
        stages = [s for s in stages if s not in (4, 5)]

    return stages
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_stages.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Mutation-check the precedence order**

Move the read-only block **above** the trivial-bypass block, run the file, and confirm `test_a_trivial_read_runs_one_and_two_not_one_to_three` still passes (it should — the result is the same either way for that case) **and** that no test fails. If none fails, the order is under-tested: add a case that distinguishes them before restoring. This is the check §16a of rules.md calls for.

- [ ] **Step 6: Commit**

```bash
git add delegation_pipeline.py tests/test_qa_delegation_stages.py
git commit -m "feat: stage selection -- blast radius, trivial bypass, read-only, in order"
```

---

### Task 7: Derived deadlines and the latency ceiling

**Files:**
- Modify: `delegation_pipeline.py` (add the deadline functions)
- Test: `tests/test_qa_delegation_deadlines.py`

**Interfaces:**
- Consumes: `tiered_delegation.CapabilityTable`.
- Produces:
  - `SIZE_FACTORS: dict[int, float]` — `{1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5, 5: 2.0}`
  - `TIER0_DEADLINE: dict[str, int]` — `{"long-context": 45, "coding": 90}`
  - `LATENCY_CEILING_S: int = 1500`
  - `speed_multiplier(table, model, task_type) -> float`
  - `effective_deadline(table, model, task_type, score) -> float`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_deadlines.py
"""QA: spec 5.1 -- effective_deadline = baseline x size factor x multiplier.

The multiplier is DERIVED from the benchmark table, never stored. Three
successive revisions of this arithmetic were wrong (7.40, 7.77, 7.39) while
the conclusion survived each time, which is why these tests reproduce the
number from the table rather than asserting a constant.
"""
from __future__ import annotations

import unittest

import delegation_pipeline as pipeline
from tiered_delegation import CapabilityRow, CapabilityTable


def _table():
    return CapabilityTable([
        CapabilityRow("fast", "coding", 1.0, 24, 1.57, 10.0, 1000000),
        CapabilityRow("slow", "coding", 0.66, 44, 0.0, 30.0, 229376),
    ])


class MultiplierTests(unittest.TestCase):
    def test_the_fastest_eligible_model_is_exactly_one(self):
        self.assertEqual(pipeline.speed_multiplier(_table(), "fast", "coding"), 1.0)

    def test_a_model_three_times_slower_is_three(self):
        self.assertEqual(pipeline.speed_multiplier(_table(), "slow", "coding"), 3.0)

    def test_changing_a_latency_changes_the_multiplier(self):
        """The derivation is the durable part. A multiplier that survives a
        latency change unchanged is a constant wearing a derivation's clothes."""
        table = CapabilityTable([
            CapabilityRow("fast", "coding", 1.0, 24, 1.57, 10.0, 1000000),
            CapabilityRow("slow", "coding", 0.66, 44, 0.0, 50.0, 229376),
        ])
        self.assertEqual(pipeline.speed_multiplier(table, "slow", "coding"), 5.0)


class DeadlineTests(unittest.TestCase):
    def test_a_mid_sized_task_on_the_fastest_model_is_the_baseline(self):
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "coding", score=3), 90.0)

    def test_the_size_factor_scales_it(self):
        """Score 3 is the reference point; 1 halves and 5 doubles."""
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "coding", score=1), 45.0)
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "coding", score=5), 180.0)

    def test_all_three_factors_compose(self):
        """baseline 90 x size 2.0 x multiplier 3.0."""
        self.assertEqual(
            pipeline.effective_deadline(_table(), "slow", "coding", score=5), 540.0)

    def test_a_task_type_with_no_baseline_gets_the_longest_not_the_shortest(self):
        """5.1: an unknown type must receive the LONGEST deadline. Defaulting
        to the shortest turns 'we have not measured this' into a timeout."""
        longest = max(pipeline.TIER0_DEADLINE.values())
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "reasoning", score=3),
            float(longest))
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_deadlines.py -v`
Expected: FAIL — `module 'delegation_pipeline' has no attribute 'speed_multiplier'`

- [ ] **Step 3: Append to `delegation_pipeline.py`**

```python
# --- deadlines (spec 5.1) ----------------------------------------------------

#: Per-type baselines. A type absent here never uses the free rung, and an
#: unknown type takes the LONGEST of these -- never the shortest, because
#: "we have not measured this" must not become a timeout.
TIER0_DEADLINE: Final[dict[str, int]] = {
    "long-context": 45,
    "coding": 90,
}

#: From the classifier's complexity score. Score 3 is the reference point, so
#: a mid-sized task gets exactly the baseline.
SIZE_FACTORS: Final[dict[int, float]] = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5, 5: 2.0}

#: Governs all five stages of one leaf together, where effective_deadline
#: governs a single attempt. Derived, not chosen -- see spec 5.1.
LATENCY_CEILING_S: Final[int] = 1500


def speed_multiplier(table, model: str, task_type: str) -> float:
    """Derived from measured latency, never stored.

    The fastest ladder-eligible model on a task type is the reference at 1.0;
    every other model is its own median divided by that. Because the input is
    the same table the ladders come from, a re-benchmark updates the deadlines
    in the same act that updates the rung order, and the two cannot drift.
    """
    eligible = [r for r in table.ladder_eligible(task_type)
                if r.median_latency_s is not None]
    if not eligible:
        return 1.0
    reference = min(r.median_latency_s for r in eligible)
    if reference <= 0:
        return 1.0
    mine = next((r.median_latency_s for r in eligible if r.model == model), None)
    if mine is None:
        return 1.0
    return mine / reference


def effective_deadline(table, model: str, task_type: str, score: int) -> float:
    """baseline x size factor x model-speed multiplier (spec 5.1)."""
    baseline = TIER0_DEADLINE.get(task_type)
    if baseline is None:
        baseline = max(TIER0_DEADLINE.values())
    size = SIZE_FACTORS.get(score, 1.0)
    return float(baseline) * size * speed_multiplier(table, model, task_type)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_deadlines.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add delegation_pipeline.py tests/test_qa_delegation_deadlines.py
git commit -m "feat: deadlines derived from the benchmark table, not configured"
```

---

### Task 8: The three review gates

**Files:**
- Modify: `delegation_pipeline.py` (add the gate result type and escalation rules)
- Test: `tests/test_qa_delegation_gates.py`

**Interfaces:**
- Produces:
  - `class GateResult` — frozen dataclass: `gate: str`, `passed: bool`, `reason: str`.
  - `SECURITY_RERUN_CAP: int = 2`
  - `next_generator_rung(current: int, gate: GateResult, max_attempts: int = 3) -> int`
  - `security_exhausted(cycles: int) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_gates.py
"""QA: spec 4.3-4.5 -- the three review gates and what a rejection moves.

The property that is easy to get backwards: a rejection escalates the
GENERATOR, not the reviewer. The reviewer stays put, because a reviewer that
climbs on every rejection is answering bad code by getting more expensive.
"""
from __future__ import annotations

import unittest

import delegation_pipeline as pipeline


def _reject(gate):
    return pipeline.GateResult(gate=gate, passed=False, reason="nope")


class GateEscalationTests(unittest.TestCase):
    def test_a_reviewer_rejection_moves_the_generator(self):
        self.assertEqual(pipeline.next_generator_rung(0, _reject("reviewer")), 1)

    def test_a_qa_rejection_escalates_the_same_way(self):
        self.assertEqual(pipeline.next_generator_rung(0, _reject("qa")), 1)

    def test_a_security_rejection_escalates_the_same_way(self):
        self.assertEqual(pipeline.next_generator_rung(0, _reject("security")), 1)

    def test_a_pass_does_not_escalate(self):
        ok = pipeline.GateResult(gate="reviewer", passed=True, reason="")
        self.assertEqual(pipeline.next_generator_rung(1, ok), 1)

    def test_escalation_stops_at_the_attempt_cap(self):
        """MAX_ATTEMPTS is 3, so the top rung index is 2 and a rejection there
        does not invent a fourth."""
        self.assertEqual(pipeline.next_generator_rung(2, _reject("reviewer")), 2)


class SecurityCapTests(unittest.TestCase):
    def test_two_cycles_are_allowed(self):
        self.assertFalse(pipeline.security_exhausted(1))
        self.assertFalse(pipeline.security_exhausted(2))

    def test_the_third_is_a_human_failure(self):
        """4.5: a security fix can introduce a new vulnerability, so the cap
        is what prevents infinite recursion -- not the depth or node limits,
        which do not track cycles."""
        self.assertTrue(pipeline.security_exhausted(3))
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_gates.py -v`
Expected: FAIL — `module 'delegation_pipeline' has no attribute 'GateResult'`

- [ ] **Step 3: Append to `delegation_pipeline.py`**

```python
# --- review gates (spec 4.3-4.5) ---------------------------------------------

from dataclasses import dataclass  # noqa: E402 -- grouped with its users


@dataclass(frozen=True)
class GateResult:
    #: "reviewer", "qa" or "security" -- each tagged with its own gate so an
    #: escalation is traceable to which one rejected it (spec 10).
    gate: str
    passed: bool
    reason: str


#: Generation attempts per leaf, so the top rung index is MAX_ATTEMPTS - 1.
MAX_ATTEMPTS: Final[int] = 3

#: generation -> security review -> fix -> security review. After that the leaf
#: fails with a human flag: a security fix can introduce a new vulnerability
#: (an unsanitized os.system replaced by an unsanitized subprocess.run), and
#: the depth and node limits do not track cycles.
SECURITY_RERUN_CAP: Final[int] = 2


def next_generator_rung(current: int, gate: GateResult,
                        max_attempts: int = MAX_ATTEMPTS) -> int:
    """A rejection escalates the GENERATOR, never the reviewer.

    The reviewer stays at its entry rung and re-reviews the new output. It
    climbs only if it keeps rejecting the generator's *top* rung, which
    indicates reviewer miscalibration rather than bad code -- and that is a
    separate decision, not this function's.
    """
    if gate.passed:
        return current
    return min(current + 1, max_attempts - 1)


def security_exhausted(cycles: int) -> bool:
    """True once the security gate has been round-tripped past its cap."""
    return cycles > SECURITY_RERUN_CAP
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_gates.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add delegation_pipeline.py tests/test_qa_delegation_gates.py
git commit -m "feat: review gates escalate the generator, with a security re-run cap"
```

---

### Task 9: Settings page — the editable matrix (spec §9.2)

**Files:**
- Create: `routes/delegation.py`
- Create: `web/assets/delegation.js`
- Modify: `web/index.html` (tab button beside `tabSpecs` near line 265; panel beside `panelSpecs`)
- Modify: `web/assets/app.js` (import and tab dispatch, as `specs.js` is wired)
- Modify: `app.py` (`include_router`)
- Test: `tests/test_qa_delegation_routes.py`

**Interfaces:**
- Consumes: `db.delegation_rows_all`, `db.delegation_row_set`, `db.delegation_operational_all`, `db.delegation_operational_set`, `delegation_startup.load_capability_table`.
- Produces: `GET /api/delegation`, `PUT /api/delegation/row`, `PUT /api/delegation/operational`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_routes.py
"""QA: the settings page's endpoints -- spec 9.2.

The load-bearing property is that a write which would break an invariant of an
operational task type is REFUSED, not stored. The table is live-editable and
the ladders regenerate from it, so a check that ran only at startup would let
an operator break routing at 15:00 and discover it at the next restart.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import routes.delegation as delegation_routes


def _request(role="admin", body=None, query=None):
    request = SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": role}),
        query_params=query or {},
    )
    if body is not None:
        request.json = AsyncMock(return_value=body)
    return request


class DelegationRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_the_list_reports_nothing_operational(self):
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        self.assertEqual(body["operational"], [])

    async def test_a_row_can_be_written_and_read_back(self):
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.9, "n": 4,
            "cost_per_1m_tokens": 1.0, "median_latency_s": 2.0,
            "max_context": 1000}))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(len(body["rows"]), 1)

    async def test_a_non_admin_cannot_write(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(role="user", body={
                "model": "m", "task_type": "coding"}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_flipping_a_type_operational_on_incomplete_data_is_refused(self):
        """9.2: the flip re-runs 1.1's validation immediately and refuses with
        the offending column named, rather than accepting it and failing at the
        next restart."""
        from fastapi import HTTPException
        await db.delegation_row_set("m", "coding", accuracy=0.9, n=4,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "coding", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("coding", str(ctx.exception.detail))

    async def test_flipping_a_complete_type_operational_is_allowed(self):
        await db.delegation_row_set("m", "long-context", accuracy=1.0, n=10,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))
        self.assertEqual(await db.delegation_operational_all(), {"long-context"})

    async def test_a_refused_flip_leaves_the_stored_state_alone(self):
        """'the stored value is left as it was' -- a rejected write that half
        applied would be worse than one that failed outright."""
        from fastapi import HTTPException
        await db.delegation_row_set("m", "coding", accuracy=0.9, n=4,
                                    cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        with self.assertRaises(HTTPException):
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "coding", "operational": True}))
        self.assertEqual(await db.delegation_operational_all(), set())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routes.delegation'`

- [ ] **Step 3: Write `routes/delegation.py`**

```python
"""Routes for /api/delegation -- the tiered-delegation settings matrix.

Spec 9.2. The benchmark table is live-editable and the ladders regenerate from
it at runtime, so every write is validated before it is stored, not only the
operational flip: a table checked once at startup can be broken at any time
and will not say so until the next restart.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from delegation_startup import load_capability_table
from routes.db_delegation import rows_to_capability
from tiered_delegation import CapabilityTable

_log = logging.getLogger("wc.app")

router = APIRouter()

_EDITABLE = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s", "max_context")


def _require_admin(request: Request) -> dict:
    session = request.state.session
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return session


async def handle_delegation_get(request: Request):
    """GET /api/delegation -- the matrix, the flags, and the derived ladders."""
    rows = await db.delegation_rows_all()
    operational = sorted(await db.delegation_operational_all())
    table = await load_capability_table()
    task_types = sorted({row["task_type"] for row in rows})
    return JSONResponse({
        "rows": rows,
        "operational": operational,
        # Derived, never stored: 3 is the output of walking 2.6, so sending a
        # stored copy would let the page show a ladder the generator does not
        # produce.
        "ladders": {t: table.ladder(t) for t in task_types},
        "editable_columns": list(_EDITABLE),
    })


async def handle_row_put(request: Request):
    """PUT /api/delegation/row -- write one cell set for one (model, task)."""
    _require_admin(request)
    data = await request.json()
    model = (data.get("model") or "").strip()
    task_type = (data.get("task_type") or "").strip()
    if not model or not task_type:
        raise HTTPException(status_code=400, detail="model and task_type are required")
    columns = {c: data.get(c) for c in _EDITABLE if c in data}

    # Validate the resulting table before storing it, for operational types
    # only -- editing a non-operational type's rows stays free, because that
    # is the bootstrap path and gating it would make the table impossible to
    # fill in.
    rows = await db.delegation_rows_all()
    merged = [r for r in rows if not (r["model"] == model and r["task_type"] == task_type)]
    merged.append({"model": model, "task_type": task_type,
                   **{c: columns.get(c) for c in _EDITABLE}})
    operational = await db.delegation_operational_all()
    problems = CapabilityTable(rows_to_capability(merged), operational=operational).validate()
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))

    await db.delegation_row_set(model, task_type, **columns)
    _log.info("delegation_row_set model=%s task_type=%s", model, task_type)
    return JSONResponse({"ok": True})


async def handle_operational_put(request: Request):
    """PUT /api/delegation/operational -- flip a task type routable.

    This is the act that submits a task type to 1.1's validation. A refused
    flip leaves the stored state exactly as it was.
    """
    _require_admin(request)
    data = await request.json()
    task_type = (data.get("task_type") or "").strip()
    operational = bool(data.get("operational"))
    if not task_type:
        raise HTTPException(status_code=400, detail="task_type is required")

    if operational:
        rows = rows_to_capability(await db.delegation_rows_all())
        current = await db.delegation_operational_all()
        problems = CapabilityTable(rows, operational=current | {task_type}).validate()
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))

    await db.delegation_operational_set(task_type, operational)
    _log.info("delegation_operational_set task_type=%s operational=%s",
              task_type, operational)
    return JSONResponse({"ok": True, "operational": operational})


@router.get("/api/delegation")
async def _api_delegation_get(request: Request):
    return await handle_delegation_get(request)


@router.put("/api/delegation/row")
async def _api_delegation_row_put(request: Request):
    return await handle_row_put(request)


@router.put("/api/delegation/operational")
async def _api_delegation_operational_put(request: Request):
    return await handle_operational_put(request)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_routes.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Wire the router into `app.py`**

Beside the other route imports (near line 61):

```python
from routes.delegation import router as delegation_router
```

Beside the other `include_router` calls (near line 603):

```python
app.include_router(delegation_router)
```

- [ ] **Step 6: Add the tab and panel to `web/index.html`**

Beside `tabSpecs` (near line 265):

```html
      <button class="settings-tab" id="tabDelegation" role="tab" tabindex="-1" aria-selected="false" data-tab="delegation" aria-controls="panelDelegation">Delegation</button>
```

Beside `panelSpecs`:

```html
      <!-- The spec 2.6 benchmark matrix, editable. Each cell is one measured
           column; flipping a task type operational is what submits it to the
           startup validation, and this release ships with none flipped. -->
      <div class="settings-panel" id="panelDelegation" role="tabpanel" aria-labelledby="tabDelegation" tabindex="0" hidden>
        <div class="skills-header">
          <span class="skills-count" id="delegationCount" role="status" aria-live="polite"></span>
        </div>
        <div class="delegation-matrix" id="delegationMatrix"></div>
      </div>
```

- [ ] **Step 7: Write `web/assets/delegation.js`**

```javascript
// delegation.js — Settings > Delegation: the spec 2.6 benchmark matrix.
//
// Each row is one (model, task_type) pair with its five measured columns.
// The ladder shown per task type is DERIVED server-side by walking the table;
// it is never stored, so the page cannot show a ladder the generator would
// not produce.
import {apiFetch} from './api.js?v=1';
import {notifyResult} from './server-stats.js?v=1';

const byId = id => document.getElementById(id);

const COLUMNS = ['accuracy', 'n', 'cost_per_1m_tokens', 'median_latency_s', 'max_context'];

function _cell(row, column) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'delegation-cell';
  input.value = row[column] === null || row[column] === undefined ? '' : row[column];
  input.setAttribute('aria-label', `${column} for ${row.model} on ${row.task_type}`);
  input.addEventListener('change', () => _saveRow(row, column, input));
  return input;
}

async function _saveRow(row, column, input) {
  const raw = input.value.trim();
  // An empty cell is TBD, which is NOT zero: TBD makes the row ineligible,
  // zero is a real measurement and for cost means free.
  const value = raw === '' ? null : Number(raw);
  if (raw !== '' && Number.isNaN(value)) {
    notifyResult(`${column} must be a number or empty`, 'error');
    return;
  }
  try {
    const response = await apiFetch('/api/delegation/row', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: row.model, task_type: row.task_type, [column]: value}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Could not save');
    }
    row[column] = value;
    loadDelegation(true);
  } catch (error) {
    notifyResult(error.message, 'error');
    input.value = row[column] === null ? '' : row[column];
  }
}

export async function loadDelegation(force = false) {
  const host = byId('delegationMatrix');
  if (!host) return null;
  if (!force && host.children.length) return null;

  let payload;
  try {
    const response = await apiFetch('/api/delegation');
    if (!response.ok) throw new Error('Could not load the delegation table');
    payload = await response.json();
  } catch (error) {
    notifyResult(error.message, 'error');
    return null;
  }

  host.replaceChildren();
  const byType = {};
  (payload.rows || []).forEach(row => {
    (byType[row.task_type] = byType[row.task_type] || []).push(row);
  });

  Object.keys(byType).sort().forEach(taskType => {
    const group = document.createElement('details');
    group.className = 'delegation-group';
    const summary = document.createElement('summary');
    const ladder = (payload.ladders || {})[taskType] || [];
    const live = (payload.operational || []).includes(taskType);
    summary.textContent = `${taskType} · ${live ? 'operational' : 'not operational'}`
      + (ladder.length ? ` · ladder: ${ladder.join(' → ')}` : ' · no ladder');
    group.appendChild(summary);

    byType[taskType].forEach(row => {
      const line = document.createElement('div');
      line.className = 'delegation-row';
      const name = document.createElement('span');
      name.className = 'delegation-model';
      name.textContent = row.model;
      line.appendChild(name);
      COLUMNS.forEach(column => line.appendChild(_cell(row, column)));
      group.appendChild(line);
    });
    host.appendChild(group);
  });

  const count = byId('delegationCount');
  if (count) {
    const total = (payload.rows || []).length;
    count.textContent = `${total} row${total === 1 ? '' : 's'} · `
      + `${(payload.operational || []).length} operational`;
  }
  return (payload.rows || []).length;
}
```

- [ ] **Step 8: Wire the tab into `web/assets/app.js`**

Add the import beside the `specs.js` import:

```javascript
import {loadDelegation} from './delegation.js?v=1';
```

Add `delegation: 'panelDelegation'` to the tab map, add `'panelDelegation'` to the panel-hide list, and add to the tab-switch dispatch:

```javascript
  if (tab === 'delegation') loadDelegation(true);
```

- [ ] **Step 9: Regenerate asset versions and verify**

Run:
```bash
python3 bin/wc-asset-versions.py
.venv/bin/python -m pytest -q tests/test_frontend_syntax.py \
  tests/test_qa_asset_module_versions.py tests/test_qa_asset_versions_match_content.py
```
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add routes/delegation.py web/assets/delegation.js web/index.html \
        web/assets/app.js app.py tests/test_qa_delegation_routes.py
git commit -m "feat: Settings > Delegation, the editable benchmark matrix"
```

---

### Task 10: Seed the table, bump to 0.19.0, and close the release

**Files:**
- Create: `bin/wc-seed-delegation.py`
- Modify: `config.py` (`VERSION`), `ARCHITECTURE.md`, `web/index.html`, `web/orchestrator.html`, `web/assets/orchestrator/main.js`, `CHANGELOG.md`
- Test: `tests/test_qa_delegation_shipped_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qa_delegation_shipped_state.py
"""QA: release 0.19.0 ships the machinery and routes nothing.

This is the release's entire claim, so it gets a test rather than a sentence
in a changelog. Spec section 12 forbids flipping `coding` operational until the
gate-type question is decided, and nothing in this release decides it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

ROOT = Path(__file__).resolve().parents[1]


class ShippedStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_a_fresh_database_has_no_operational_task_type(self):
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_the_seed_script_flips_nothing_operational(self):
        """It fills measurements. Flipping a type routable is a decision, and
        section 12 has not made it."""
        source = (ROOT / "bin" / "wc-seed-delegation.py").read_text()
        self.assertNotIn("delegation_operational_set", source)

    def test_no_source_file_flips_coding_operational(self):
        """Section 12: 'Until it is decided, do not flip coding to operational.'"""
        for path in list(ROOT.glob("*.py")) + list((ROOT / "routes").glob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn('delegation_operational_set("coding", True)', source)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_shipped_state.py -v`
Expected: FAIL — `bin/wc-seed-delegation.py` does not exist.

- [ ] **Step 3: Write `bin/wc-seed-delegation.py`**

```python
#!/usr/bin/env python3
"""Seed the delegation capability table from the spec's 2.6 snapshot.

The markdown table in the spec is a snapshot of this database table, not a
second source of truth (section 2.6). This puts the measured rows in so the
settings page has something to show; it flips NOTHING operational, because
that is a decision and section 12 has not made it.

    .venv/bin/python bin/wc-seed-delegation.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

#: (model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, max_context)
#: None means TBD, which is not zero -- see CapabilityRow's docstring.
ROWS = [
    ("vllm/Qwen3.6-35B-A3B-NVFP4", "coding", 0.66, 44, 0.0, 26.8, 229376),
    ("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context", 1.0, 10, 0.0, None, 229376),
    ("azure_ai/gpt-5.6-luna", "coding", 1.0, 24, 0.0285, 12.8, 922000),
    ("azure_ai/gpt-5.6-luna", "long-context", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "comprehension", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "reasoning", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "voice", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "reviewer-gate", None, 9, 0.0285, 11.1, 922000),
    ("azure_ai/gpt-5.4-mini", "coding", None, None, 0.5261, None, 1050000),
    ("azure_ai/gpt-5.4-mini", "reasoning", 0.86, None, 0.5261, None, 1050000),
    ("claude-sonnet-5", "coding", 1.0, 24, 1.5709, 15.5, 1000000),
    ("claude-sonnet-5", "comprehension", 1.0, 2, 1.5709, None, 1000000),
    ("claude-sonnet-5", "reasoning", 0.75, 2, 1.5709, None, 1000000),
    ("claude-sonnet-5", "voice", None, None, 1.5709, None, 1000000),
    ("claude-sonnet-5", "reviewer-gate", None, None, 1.5709, None, 1000000),
    ("claude-opus-5", "comprehension", 0.5, 2, 3.6082, None, 1000000),
    ("claude-opus-5", "reasoning", None, None, 3.6082, None, 1000000),
]


async def main() -> int:
    await db.init()
    try:
        for model, task_type, accuracy, n, cost, latency, context in ROWS:
            await db.delegation_row_set(
                model, task_type, accuracy=accuracy, n=n,
                cost_per_1m_tokens=cost, median_latency_s=latency,
                max_context=context)
        print(f"seeded {len(ROWS)} capability rows; nothing is operational")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_delegation_shipped_state.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Bump the version to 0.19.0 in all six places**

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
for path, old, new in [
    ("config.py", 'VERSION = "WebConsole_0.18.1"', 'VERSION = "WebConsole_0.19.0"'),
    ("ARCHITECTURE.md", "**Version:** 0.18.1", "**Version:** 0.19.0"),
    ("web/index.html", 'id="ver">0.18.1<', 'id="ver">0.19.0<'),
    ("web/orchestrator.html", "0.18.1", "0.19.0"),
    ("web/assets/orchestrator/main.js", 'textContent = "0.18.1"', 'textContent = "0.19.0"'),
]:
    p = Path(path); s = p.read_text()
    assert old in s, f"{path}: {old!r} not found"
    p.write_text(s.replace(old, new)); print("ok", path)
PY
```

- [ ] **Step 6: Add the CHANGELOG entry**

Insert above the `## [0.18.1]` heading in `CHANGELOG.md`:

```markdown
## [0.19.0] — 2026-09-16

### Added

- **Tiered agent delegation — the machinery, routing nothing.** The classifier,
  the coding oracle, the three review gates, the blast-radius check, derived
  per-attempt deadlines, the benchmark table as a real database table, startup
  validation, and a Settings > Delegation page for editing the measurements.

  **Nothing routes.** Every task type ships non-operational, which is the
  spec's own bootstrap default: a type becomes routable only by being flipped,
  and flipping it is what submits it to validation. With none flipped, model
  selection behaves exactly as it did in 0.18.1, and a test asserts that rather
  than a sentence claiming it.

  The spec's three open items stay open. In particular `coding` is not
  operational: it clears all six startup invariants, but the gates its later
  stages run on do not, and nothing in the design refuses that combination yet.

### Fixed

- **`assign_model` stopped discarding the complexity it computes.** Both
  branches of its fallback returned the same value, so the score was calculated
  and thrown away — routing that read as deliberate while doing none.
```

- [ ] **Step 7: Regenerate asset versions and run the version checks**

Run:
```bash
python3 bin/wc-asset-versions.py
.venv/bin/python -m pytest -q tests/test_qa_version_consistency.py \
  tests/test_qa_asset_versions_match_content.py tests/test_qa_asset_module_versions.py
```
Expected: PASS

- [ ] **Step 8: Run every delegation test together**

Run:
```bash
.venv/bin/python -m pytest -q tests/test_qa_delegation_*.py tests/test_qa_tiered_delegation.py
```
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add bin/wc-seed-delegation.py config.py ARCHITECTURE.md CHANGELOG.md \
        web/index.html web/orchestrator.html web/assets/orchestrator/main.js \
        web/assets/app.js tests/test_qa_delegation_shipped_state.py
git commit -m "release: 0.19.0 — tiered delegation machinery, routing nothing"
```

---

## Self-Review Notes

**Spec coverage.** §1 (assign_model fix) → Task 4. §1.1 (startup validation, operational flag) → Tasks 2, 3. §2, §2.1–2.4 (classifier) → Task 1. §2.6 (benchmark table as a database table) → Task 2. §2.7 + §3 (cost ceiling, ladder generation) → already in `tiered_delegation.py`, consumed by Tasks 3, 4, 7. §4.2 (oracle) → Task 5. §4.3–4.5 (gates, security cap) → Task 8. §4.6–4.8 (blast radius, read-only, precedence) → Task 6. §5.1 (deadlines, multiplier, ceiling) → Task 7. §9.2 (settings page) → Task 9. §9.3 (model travels with machine) → Global Constraints; the ladder returns model ids and the machine pairing is enforced at the existing `get_backend` seam, unchanged by this release.

**Deliberately out of scope**, because nothing routes: §4.9 (context bundle to gates), §5.2 (termination guard), §6 (failure signals), §7 (sub-agent lifecycle), §8 (placement), §10 (observability), §10.2 (scheduled re-benchmark). Each needs a running pipeline to be meaningful, and building them against a pipeline that never executes would produce untested code wearing tests. They belong to the release that makes a task type operational.

**Placeholder scan.** No TBD, no "handle errors appropriately", no "similar to Task N" — every code step carries its actual content.

**Type consistency.** `CapabilityRow`'s seven fields are used with those exact names in Tasks 2, 3, 7 and 9. `Classification(task_type, score, mutates)` from Task 1 is consumed with those names in Tasks 4 and 6. `GateResult(gate, passed, reason)` from Task 8 is used only within Task 8. `stages_for(decision, files_changed)` and `effective_deadline(table, model, task_type, score)` keep their signatures across Tasks 6 and 7.

**One known gap, stated rather than hidden.** Task 9's `_saveRow` sends a single column per request, so the server-side validation in `handle_row_put` sees the merged row rather than the whole matrix. That is correct for the invariants it checks (they are per task type), but it means an operator cannot make two edits that are only valid together. No task type is operational in this release, so the validation never fires in practice — worth revisiting when one is.
