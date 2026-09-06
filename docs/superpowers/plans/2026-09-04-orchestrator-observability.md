# Orchestrator Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make silent write-failures in the orchestrator engine and the plain
usage-recording path visible as a `degraded` flag on `chats`/`supervisors`,
and give the orchestrator pane a dependency-ordered task rail plus a single,
always-visible marker for whatever is genuinely blocked on a human.

**Architecture:** Two additive, independent halves. Part A adds two schema
columns and four DB helper functions, wired into the nine existing
`except Exception: _log.exception(...)`-only sites already identified in the
spec, plus two small UI badges that read the new field. Part B adds a pure
dependency-layering function and two new small UI elements (a rail, a topbar
gate marker) that read state the pane already fetches — no new backend
endpoints, no new polling interval (one existing interval gains one more call).

**Tech Stack:** Python 3 / FastAPI / aiosqlite (backend), vanilla ES modules,
no build step (frontend), `unittest.IsolatedAsyncioTestCase` for async DB
tests, plain `unittest.TestCase` with source-string assertions for structural
frontend tests — all matching this repo's existing conventions exactly (see
`tests/test_db.py`, `tests/test_qa_usage_logging.py`,
`tests/test_qa_supervisor_usage.py`, `tests/test_frontend.py`).

**Spec:** `docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md`

## Global Constraints

- **Shared working tree.** Other Claude sessions commit to this checkout
  concurrently (`git log` shows releases landing from other sessions within
  the last hour, as of this plan). Every `git add` in every task below names
  its files explicitly — never `git add -A`, never `git add .`. Run
  `git status --porcelain` and `git diff --stat <files-this-task-touches>`
  immediately before staging anything, to confirm no other session has
  touched the same lines since you last read them.
- **Never run `db.init()` against the production database.** Every test in
  this plan uses `tempfile.TemporaryDirectory()` plus
  `patch.object(config, "DB_PATH", ...)`, exactly like `tests/test_db.py` and
  `tests/test_qa_usage_logging.py` already do. Do not add a test that opens
  the real `DB_PATH`.
- **Tests run as** `.venv/bin/python -m pytest` **(bare)**, not
  `pytest tests/` — the latter misses files, per `CLAUDE.md` §9. A trustworthy
  run reports exactly 6 skips.
- **Turn/backend mechanics are out of bounds.** Nothing in this plan touches
  `runner._build_cmd_direct`, `claude_proxy.py`'s spawn, `_build_env`, or the
  CLI argv/env assembly `CLAUDE.md` governs. Every change here is: a new
  column, a new small helper function, a call to that helper added next to an
  existing `except` block, or new frontend code reading fields that already
  ride along on existing API responses.
- **No new tables, no new endpoints, no per-task `degraded` column, no
  write-ahead log, no cross-owner admin view, no change to `classify_chat`.**
  Per the spec's explicit Non-goals section.
- **`degraded` is never user-settable.** It must never be added to
  `_ALLOWED_CHAT_FIELDS` in `routes/db_chats.py`, and it is set only through
  the new `chat_mark_degraded`/`supervisor_mark_degraded` helpers, never
  through `PATCH /api/chats/{id}` or `PATCH /api/supervisors/{id}`.
- **A degraded flag clears only when a write of the *same kind* later
  succeeds**, not on any unrelated successful write. This refines the spec's
  "last-write-wins" wording (which described the storage shape, not the clear
  condition) to match the spec's own per-row "clears when" column, which is
  already kind-scoped. `kind` is a short fixed literal (`"usage"`,
  `"progress"`, `"status"`, `"task_create"`, `"task_message"`,
  `"task_status_done"`, `"task_status_failed"`), never derived from anything
  a model or a user wrote.

---

### Task 1: Chats table — schema, helpers, and degraded-flag tests

**Files:**
- Modify: `db.py` (`_ensure_chat_columns()`, and the `__getattr__` `_SYMBOLS`
  dict's chats block)
- Modify: `routes/db_chats.py` (`_CHAT_COLUMNS`, new helpers)
- Test: `tests/test_qa_chat_degraded.py` (new)

**Interfaces:**
- Produces: `db.chat_mark_degraded(chat_id: str, kind: str, detail: str) -> None`,
  `db.chat_clear_degraded(chat_id: str, kind: str) -> None`. Both resolve via
  `db.py`'s `__getattr__` to `routes.db_chats`, exactly like every other
  `db.chat_*` function already does.
- Consumes: nothing new — `db.db_conn`, `db._now()` already exist.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_chat_degraded.py`:

```python
"""QA: a chat's degraded flag is set by the mark/clear helpers, kind-scoped.

`degraded` exists so a write that silently failed (usage recording is the
first caller) leaves a visible trace instead of only a log line -- see
docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db


class ChatDegradedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_schema_has_the_new_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"degraded", "degraded_reason", "degraded_at"}.issubset(columns))

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        names = [row["name"] for row in await cursor.fetchall()]
        self.assertEqual(names.count("degraded"), 1)

    async def test_a_new_chat_is_not_degraded(self):
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)
        self.assertIsNone(chat["degraded_reason"])

    async def test_marking_sets_the_flag_and_reason(self):
        await db.chat_mark_degraded("c1", "usage", "no frame reached the handler")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("usage:", chat["degraded_reason"])
        self.assertIn("no frame reached the handler", chat["degraded_reason"])
        self.assertIsNotNone(chat["degraded_at"])

    async def test_clearing_the_same_kind_resets_it(self):
        await db.chat_mark_degraded("c1", "usage", "boom")
        await db.chat_clear_degraded("c1", "usage")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)
        self.assertIsNone(chat["degraded_reason"])
        self.assertIsNone(chat["degraded_at"])

    async def test_clearing_a_different_kind_does_not_touch_it(self):
        """A success of kind B must not hide an unresolved kind-A failure."""
        await db.chat_mark_degraded("c1", "usage", "boom")
        await db.chat_clear_degraded("c1", "some_other_kind")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("usage:", chat["degraded_reason"])

    async def test_marking_never_raises_even_if_the_write_fails(self):
        with patch.object(db.db_conn, "execute", AsyncMock(side_effect=RuntimeError("disk"))):
            await db.chat_mark_degraded("c1", "usage", "boom")  # must not raise


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_degraded.py -v`
Expected: FAIL — `PRAGMA table_info` assertion fails (columns do not exist
yet), and `db.chat_mark_degraded` raises `AttributeError` (not registered).

- [ ] **Step 3: Add the migration**

In `db.py`, inside `_ensure_chat_columns()`, add three entries to the
`migrations` dict (the function already reads `PRAGMA table_info(chats)` into
`columns` and loops `for name, sql in migrations.items(): if name not in
columns: await db_conn.execute(sql)` — just extend the dict):

```python
        "degraded": "ALTER TABLE chats ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0",
        "degraded_reason": "ALTER TABLE chats ADD COLUMN degraded_reason TEXT",
        "degraded_at": "ALTER TABLE chats ADD COLUMN degraded_at TEXT",
```

- [ ] **Step 4: Add the columns to `_CHAT_COLUMNS` and the helpers**

In `routes/db_chats.py`, change `_CHAT_COLUMNS` (do **not** touch
`_ALLOWED_CHAT_FIELDS` — `degraded` must never be PATCH-settable):

```python
_CHAT_COLUMNS = (
    "id, title, description, session_id, work_dir, owner_id, created_at, "
    "updated_at, archived, pinned, pinned_at, position, deleted_at, model, ai_machine_id, "
    "transcript_offset, degraded, degraded_reason, degraded_at"
)
```

Add the two helpers at the end of `routes/db_chats.py`:

```python
async def chat_mark_degraded(chat_id: str, kind: str, detail: str) -> None:
    """Flag *chat_id* as carrying a known write failure of *kind*.

    Never raises: marking degradation must not become a second thing that
    fails a turn, the same rule db.usage_record already states for itself.
    """
    try:
        await db.db_conn.execute(
            "UPDATE chats SET degraded = 1, degraded_reason = ?, degraded_at = ? "
            "WHERE id = ?",
            (f"{kind}: {detail}", db._now(), chat_id),
        )
        await db.db_conn.commit()
    except Exception:  # noqa: BLE001 -- marking degraded must not itself fail a turn
        _log.exception("chat_mark_degraded failed chat_id=%s kind=%s", chat_id, kind)


async def chat_clear_degraded(chat_id: str, kind: str) -> None:
    """Clear *chat_id*'s degraded flag, but only if it names this same *kind*.

    A chat degraded for kind A must not be silently cleared by an unrelated
    successful write of kind B -- that would hide a problem that is still real.
    """
    try:
        await db.db_conn.execute(
            "UPDATE chats SET degraded = 0, degraded_reason = NULL, degraded_at = NULL "
            "WHERE id = ? AND degraded_reason LIKE ?",
            (chat_id, f"{kind}:%"),
        )
        await db.db_conn.commit()
    except Exception:  # noqa: BLE001
        _log.exception("chat_clear_degraded failed chat_id=%s kind=%s", chat_id, kind)
```

- [ ] **Step 5: Register the two new names in `db.py`'s `__getattr__`**

In `db.py`, inside the `# chats` block of `_SYMBOLS` (next to
`"chat_list": "routes.db_chats"` etc.), add:

```python
        "chat_mark_degraded": "routes.db_chats",
        "chat_clear_degraded": "routes.db_chats",
```

This step is easy to skip and the failure is silent until something calls
`db.chat_mark_degraded` at runtime and gets `AttributeError` — it is not
caught by importing `routes.db_chats` directly in a test, only by going
through `db.<name>` the way every real caller will. The test suite in Step 1
calls `db.chat_mark_degraded`, not `routes.db_chats.chat_mark_degraded`,
specifically so this omission cannot pass unnoticed.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_degraded.py -v`
Expected: PASS, all 6 tests.

- [ ] **Step 7: Run the full suite to confirm no regression**

Run: `.venv/bin/python -m pytest`
Expected: same pass count as before plus 6, same skip count (6).

- [ ] **Step 8: Commit**

```bash
git status --porcelain -- db.py routes/db_chats.py tests/test_qa_chat_degraded.py
git add db.py routes/db_chats.py tests/test_qa_chat_degraded.py
git commit -m "feat: add a kind-scoped degraded flag to chats"
```

---

### Task 2: Supervisors table — schema, helpers, and degraded-flag tests

**Files:**
- Modify: `db.py` (`_ensure_supervisor_columns()`, `__getattr__` `_SYMBOLS`
  dict's supervisors block)
- Modify: `routes/db_supervisors.py` (add `logging` import + `_log`, extend
  `supervisor_get`/`supervisor_list` SELECT column lists, new helpers)
- Test: `tests/test_qa_supervisor_degraded.py` (new)

**Interfaces:**
- Produces: `db.supervisor_mark_degraded(supervisor_id: str, kind: str, detail: str) -> None`,
  `db.supervisor_clear_degraded(supervisor_id: str, kind: str) -> None`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_supervisor_degraded.py`:

```python
"""QA: a orchestrator's degraded flag, same contract as chats' (see
tests/test_qa_chat_degraded.py) -- kind-scoped mark/clear, never raises.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db


class SupervisorDegradedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.supervisor_create("sup-1", "Sup", None, "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_schema_has_the_new_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(supervisors)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"degraded", "degraded_reason"}.issubset(columns))

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cursor = await db.db_conn.execute("PRAGMA table_info(supervisors)")
        names = [row["name"] for row in await cursor.fetchall()]
        self.assertEqual(names.count("degraded"), 1)

    async def test_get_and_list_both_carry_the_new_fields(self):
        sup = await db.supervisor_get("sup-1", "admin")
        self.assertIn("degraded", sup)
        self.assertIn("degraded_reason", sup)
        listed = await db.supervisor_list("admin")
        self.assertIn("degraded", listed[0])
        self.assertIn("degraded_reason", listed[0])

    async def test_marking_and_clearing_is_kind_scoped(self):
        await db.supervisor_mark_degraded("sup-1", "status", "write raised")
        sup = await db.supervisor_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 1)
        self.assertIn("status:", sup["degraded_reason"])

        await db.supervisor_clear_degraded("sup-1", "progress")  # different kind
        sup = await db.supervisor_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 1, "an unrelated success must not clear it")

        await db.supervisor_clear_degraded("sup-1", "status")  # same kind
        sup = await db.supervisor_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 0)
        self.assertIsNone(sup["degraded_reason"])

    async def test_marking_never_raises_even_if_the_write_fails(self):
        with patch.object(db.db_conn, "execute", AsyncMock(side_effect=RuntimeError("disk"))):
            await db.supervisor_mark_degraded("sup-1", "status", "boom")  # must not raise


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qa_supervisor_degraded.py -v`
Expected: FAIL — columns absent, `AttributeError` on the new functions.

- [ ] **Step 3: Add the migration**

In `db.py`, inside `_ensure_supervisor_columns()`, add two entries to the
`sup_migrations` dict (next to `"completed_at": ...`):

```python
        "degraded": "ALTER TABLE supervisors ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0",
        "degraded_reason": "ALTER TABLE supervisors ADD COLUMN degraded_reason TEXT",
```

- [ ] **Step 4: Add logging, extend the SELECT lists, add the helpers**

In `routes/db_supervisors.py`, the file currently has no logger. Change the
top of the file from:

```python
import json
from typing import Any

import db
```

to:

```python
import json
import logging
from typing import Any

import db

_log = logging.getLogger("wc.db.supervisors")
```

Change `supervisor_list`'s SELECT (currently
`"SELECT id, title, description, config, status, progress_pct, "
"created_at, updated_at, completed_at "`) to add the two new columns:

```python
async def supervisor_list(owner_id: str) -> list[dict[str, Any]]:
    """All supervisors for *owner_id*, newest first."""
    cur = await db.db_conn.execute(
        "SELECT id, title, description, config, status, progress_pct, "
        "created_at, updated_at, completed_at, degraded, degraded_reason "
        "FROM supervisors WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]
```

Change `supervisor_get`'s SELECT the same way:

```python
async def supervisor_get(supervisor_id: str, owner_id: str) -> dict[str, Any] | None:
    """Fetch one orchestrator, owner-scoped."""
    cur = await db.db_conn.execute(
        "SELECT id, title, description, config, status, plan, progress_pct, "
        "created_at, updated_at, completed_at, degraded, degraded_reason "
        "FROM supervisors WHERE id = ? AND owner_id = ?",
        (supervisor_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None
```

Add the two helpers at the end of `routes/db_supervisors.py`:

```python
async def supervisor_mark_degraded(supervisor_id: str, kind: str, detail: str) -> None:
    """Flag *supervisor_id* as carrying a known write failure of *kind*.

    Never raises -- see chat_mark_degraded in routes/db_chats.py for the same
    reasoning; the two exist in parallel rather than as one shared function
    because there is no third caller and the two tables differ (chats also
    stamps degraded_at).
    """
    try:
        await db.db_conn.execute(
            "UPDATE supervisors SET degraded = 1, degraded_reason = ? WHERE id = ?",
            (f"{kind}: {detail}", supervisor_id),
        )
        await db.db_conn.commit()
    except Exception:  # noqa: BLE001 -- marking degraded must not itself fail a run
        _log.exception(
            "supervisor_mark_degraded failed id=%s kind=%s", supervisor_id, kind,
        )


async def supervisor_clear_degraded(supervisor_id: str, kind: str) -> None:
    """Clear the flag, but only if it currently names this same *kind*."""
    try:
        await db.db_conn.execute(
            "UPDATE supervisors SET degraded = 0, degraded_reason = NULL "
            "WHERE id = ? AND degraded_reason LIKE ?",
            (supervisor_id, f"{kind}:%"),
        )
        await db.db_conn.commit()
    except Exception:  # noqa: BLE001
        _log.exception(
            "supervisor_clear_degraded failed id=%s kind=%s", supervisor_id, kind,
        )
```

- [ ] **Step 5: Register the two new names in `db.py`'s `__getattr__`**

In `db.py`, inside the `# supervisors` block of `_SYMBOLS`, add:

```python
        "supervisor_mark_degraded": "routes.db_supervisors",
        "supervisor_clear_degraded": "routes.db_supervisors",
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_supervisor_degraded.py -v`
Expected: PASS, all 5 tests.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: prior count + 5 more passing, still 6 skips. In particular re-run
`tests/test_qa_supervisor_route.py` and `tests/test_qa_supervisor_members_ui.py`
by name and confirm they still pass — both read `supervisor_get`/`supervisor_list`
output and a wider SELECT list must not have broken anything they assert on.

Run: `.venv/bin/python -m pytest tests/test_qa_supervisor_route.py tests/test_qa_supervisor_members_ui.py -v`

- [ ] **Step 8: Commit**

```bash
git status --porcelain -- db.py routes/db_supervisors.py tests/test_qa_supervisor_degraded.py
git add db.py routes/db_supervisors.py tests/test_qa_supervisor_degraded.py
git commit -m "feat: add a kind-scoped degraded flag to supervisors"
```

---

### Task 3: Wire `_record_turn_usage`'s three failure branches

**Files:**
- Modify: `routes/chats.py:518-590` (`_record_turn_usage`)
- Test: `tests/test_qa_usage_logging.py` (extend the existing
  `RecordTurnUsageDiagnosticsTests` class — this file already tests exactly
  this function's failure branches, per its own docstring)

**Interfaces:**
- Consumes: `db.chat_mark_degraded`, `db.chat_clear_degraded` (Task 1).
- Produces: nothing new — this task only calls existing/new functions from
  inside an existing function; no new exported name.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_qa_usage_logging.py`'s `RecordTurnUsageDiagnosticsTests`
class (it already imports `db` and `chat_routes`, and already has a `c1`
chat created in `asyncSetUp`):

```python
    async def test_absent_frame_marks_the_chat_degraded(self):
        await chat_routes._record_turn_usage("c1", "admin", {})
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("usage:", chat["degraded_reason"])

    async def test_frame_without_models_marks_the_chat_degraded(self):
        await chat_routes._record_turn_usage("c1", "admin", {"models": {}, "cost_usd": 1})
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)

    async def test_a_failed_db_write_marks_the_chat_degraded(self):
        frame = {"models": {"m": {"input_tokens": 1, "output_tokens": 2}}}
        with (
            patch.object(db, "ai_machine_active", AsyncMock(return_value=None)),
            patch.object(db, "usage_record", AsyncMock(return_value=None)),
        ):
            await chat_routes._record_turn_usage("c1", "admin", frame)
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("usage:", chat["degraded_reason"])

    async def test_a_successful_record_clears_a_prior_degraded_flag(self):
        await db.chat_mark_degraded("c1", "usage", "earlier failure")
        frame = {"models": {"m": {"input_tokens": 1, "output_tokens": 2}}}
        with (
            patch.object(db, "ai_machine_active", AsyncMock(return_value=None)),
            patch.object(db, "usage_record", AsyncMock(return_value=1)),
        ):
            await chat_routes._record_turn_usage("c1", "admin", frame)
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)
```

This class's `asyncSetUp` already runs `await db.init()` against a temp DB
(Task 1/2 already made `db.chat_mark_degraded`/`chat_get` return the new
columns), so no fixture changes are needed here.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qa_usage_logging.py -v`
Expected: the four new tests FAIL — `_record_turn_usage` never calls
`chat_mark_degraded`/`chat_clear_degraded` yet, so `chat["degraded"]` stays 0
in all four.

- [ ] **Step 3: Wire the three branches**

In `routes/chats.py`, `_record_turn_usage` currently reads (line numbers as
of this plan):

```python
518	async def _record_turn_usage(chat_id: str, owner: str, frame: dict) -> None:
...
540	    if not frame:
541	        _log.warning(
542	            "usage_missing: chat_id=%s (no usage frame reached the handler; "
543	            "the CLI result frame carried none, or claude_proxy is running "
544	            "code older than its source and never emitted one)",
545	            chat_id,
546	        )
547	        return
548	    models = frame.get("models") or {}
549	    if not models:
550	        _log.warning(
551	            "usage_frame_has_no_models: chat_id=%s frame_keys=%s "
552	            "(a usage frame arrived but named no model, so nothing can be "
553	            "attributed)",
554	            chat_id, sorted(frame),
555	        )
556	        return
557	    try:
558	        machine = await db.ai_machine_active(owner)
...
566	    cost = frame.get("cost_usd")
567	    for index, (model, stats) in enumerate(models.items()):
568	        if not isinstance(stats, dict):
569	            continue
570	        await db.usage_record(
571	            chat_id,
572	            owner,
573	            model or "unknown",
574	            provider,
575	            input_tokens=stats.get("input_tokens", 0),
...
582	            is_error=bool(frame.get("is_error")),
583	            origin="web",
584	        )
585	    _log.info(
586	        "usage_recorded chat_id=%s provider=%s models=%s",
587	        chat_id, provider, list(models),
588	    )
```

Change the two early `return`s and the recording loop as follows.

Replace:

```python
    if not frame:
        _log.warning(
            "usage_missing: chat_id=%s (no usage frame reached the handler; "
            "the CLI result frame carried none, or claude_proxy is running "
            "code older than its source and never emitted one)",
            chat_id,
        )
        return
    models = frame.get("models") or {}
    if not models:
        _log.warning(
            "usage_frame_has_no_models: chat_id=%s frame_keys=%s "
            "(a usage frame arrived but named no model, so nothing can be "
            "attributed)",
            chat_id, sorted(frame),
        )
        return
```

with:

```python
    if not frame:
        _log.warning(
            "usage_missing: chat_id=%s (no usage frame reached the handler; "
            "the CLI result frame carried none, or claude_proxy is running "
            "code older than its source and never emitted one)",
            chat_id,
        )
        await db.chat_mark_degraded(chat_id, "usage", "no frame reached the handler")
        return
    models = frame.get("models") or {}
    if not models:
        _log.warning(
            "usage_frame_has_no_models: chat_id=%s frame_keys=%s "
            "(a usage frame arrived but named no model, so nothing can be "
            "attributed)",
            chat_id, sorted(frame),
        )
        await db.chat_mark_degraded(chat_id, "usage", "frame carried no models")
        return
```

Replace the recording loop and the final log line:

```python
    cost = frame.get("cost_usd")
    for index, (model, stats) in enumerate(models.items()):
        if not isinstance(stats, dict):
            continue
        await db.usage_record(
            chat_id,
            owner,
            model or "unknown",
            provider,
            input_tokens=stats.get("input_tokens", 0),
            output_tokens=stats.get("output_tokens", 0),
            cache_read_tokens=stats.get("cache_read_tokens", 0),
            cache_creation_tokens=stats.get("cache_creation_tokens", 0),
            cost_usd=cost if index == 0 else None,
            cost_basis=stats.get("cost_basis"),
            duration_ms=frame.get("duration_ms"),
            is_error=bool(frame.get("is_error")),
            origin="web",
        )
    _log.info(
        "usage_recorded chat_id=%s provider=%s models=%s",
        chat_id, provider, list(models),
    )
```

with:

```python
    cost = frame.get("cost_usd")
    any_written = False
    any_failed = False
    for index, (model, stats) in enumerate(models.items()):
        if not isinstance(stats, dict):
            continue
        row_id = await db.usage_record(
            chat_id,
            owner,
            model or "unknown",
            provider,
            input_tokens=stats.get("input_tokens", 0),
            output_tokens=stats.get("output_tokens", 0),
            cache_read_tokens=stats.get("cache_read_tokens", 0),
            cache_creation_tokens=stats.get("cache_creation_tokens", 0),
            cost_usd=cost if index == 0 else None,
            cost_basis=stats.get("cost_basis"),
            duration_ms=frame.get("duration_ms"),
            is_error=bool(frame.get("is_error")),
            origin="web",
        )
        if row_id is None:
            any_failed = True
        else:
            any_written = True
    if any_failed:
        await db.chat_mark_degraded(chat_id, "usage", "usage_record returned no row id")
    elif any_written:
        await db.chat_clear_degraded(chat_id, "usage")
    _log.info(
        "usage_recorded chat_id=%s provider=%s models=%s",
        chat_id, provider, list(models),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_usage_logging.py -v`
Expected: PASS, all tests (the pre-existing ones and the four new ones).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: prior count + 4, 6 skips.

- [ ] **Step 6: Commit**

```bash
git status --porcelain -- routes/chats.py tests/test_qa_usage_logging.py
git add routes/chats.py tests/test_qa_usage_logging.py
git commit -m "feat: mark a chat degraded when its usage recording silently fails"
```

---

### Task 4: Wire the six `orchestrator.py` sites

**Files:**
- Modify: `orchestrator.py` (`_record_usage`, `_persist_progress`, `_set_status`,
  `_materialise_plan`, `_execute_task`)
- Test: `tests/test_qa_supervisor_usage.py` (extend), new
  `tests/test_qa_supervisor_degraded_wiring.py`

**Interfaces:**
- Consumes: `db.supervisor_mark_degraded`, `db.supervisor_clear_degraded`
  (Task 2).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_qa_supervisor_usage.py`'s `UsageRecordingTests` class
(it already has `self.engine`, `self._frame()`, `self._record()`):

```python
    async def test_a_write_failure_marks_the_supervisor_degraded(self):
        async def failing_usage_record(**kwargs):
            return None  # simulates db.usage_record's own swallowed failure

        marks = []
        with (
            patch.object(runner, "take_last_usage", return_value=self._frame()),
            patch("db.usage_record", failing_usage_record),
            patch("db.supervisor_mark_degraded", AsyncMock(side_effect=lambda *a: marks.append(a))),
        ):
            await self.engine._record_usage("subtask_t001", "claude-sonnet-5")
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0][0], "sup-1")
        self.assertEqual(marks[0][1], "usage")

    async def test_a_raised_write_also_marks_the_supervisor_degraded(self):
        async def exploding_record(**kwargs):
            raise RuntimeError("database is locked")

        marks = []
        with (
            patch.object(runner, "take_last_usage", return_value=self._frame()),
            patch("db.usage_record", exploding_record),
            patch("db.supervisor_mark_degraded", AsyncMock(side_effect=lambda *a: marks.append(a))),
        ):
            await self.engine._record_usage("subtask_t001", "claude-sonnet-5")
        self.assertEqual(len(marks), 1)

    async def test_an_empty_frame_neither_marks_nor_clears(self):
        """No usage reported is not a failure of the write -- see the
        pre-existing test_an_empty_frame_writes_nothing above."""
        calls = []
        with (
            patch.object(runner, "take_last_usage", return_value={}),
            patch("db.usage_record", AsyncMock()),
            patch("db.supervisor_mark_degraded", AsyncMock(side_effect=lambda *a: calls.append(("mark", a)))),
            patch("db.supervisor_clear_degraded", AsyncMock(side_effect=lambda *a: calls.append(("clear", a)))),
        ):
            await self.engine._record_usage("subtask_t001", "claude-sonnet-5")
        self.assertEqual(calls, [])

    async def test_a_successful_write_clears_a_prior_degraded_flag(self):
        calls = []
        with (
            patch.object(runner, "take_last_usage", return_value=self._frame()),
            patch("db.usage_record", AsyncMock(return_value=1)),
            patch("db.supervisor_clear_degraded", AsyncMock(side_effect=lambda *a: calls.append(a))),
        ):
            await self.engine._record_usage("subtask_t001", "claude-sonnet-5")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("sup-1", "usage"))
```

Note: `patch("db.usage_record", ...)` and `patch("db.supervisor_mark_degraded", ...)`
patch the *module-level* `db` object's attributes directly (as the existing
`test_a_write_failure_does_not_raise` already does with `patch("db.usage_record",
exploding_record)`), which works here because `orchestrator.py` does
`import db` inside the method body and then calls `db.usage_record(...)` /
`db.supervisor_mark_degraded(...)` as attribute lookups against that same
shared module object — patching `db.<name>` affects every such lookup
regardless of which function does the `import db`.

Create `tests/test_qa_supervisor_degraded_wiring.py` for the other five sites,
using the same `_body()` source-slicing helper `test_qa_supervisor_usage.py`
already defines in its `CallSiteTests` class (copied here since it is a small,
self-contained staticmethod-style helper, not worth extracting into a shared
module for two callers):

```python
"""QA: every DB write in orchestrator.py that can silently fail marks the run
degraded, and every one that can silently start working again clears it.

See docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md's
failure-policy table. _record_usage's own wiring is covered in
tests/test_qa_supervisor_usage.py; this file covers the other five sites.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import orchestrator


class CallSiteTests(unittest.TestCase):
    """Asserted on source, the same way test_qa_supervisor_usage.py's
    CallSiteTests class asserts _record_usage's call sites -- driving all of
    these live needs a real CLI turn, and a missed call site is silent: it
    looks exactly like a orchestrator that happened not to hit that failure."""

    @classmethod
    def setUpClass(cls):
        cls.source = Path(orchestrator.__file__).read_text(encoding="utf-8")

    def _body(self, name):
        start = self.source.index(f"    async def {name}(")
        rest = self.source[start + 10:]
        ends = [
            offset for offset in (
                rest.find("\n    async def "), rest.find("\n    def "),
                rest.find("\nclass "),
            ) if offset != -1
        ]
        return rest[:min(ends)] if ends else rest

    def test_persist_progress_marks_and_clears(self):
        body = self._body("_persist_progress")
        self.assertIn('supervisor_mark_degraded(self.supervisor_id, "progress"', body)
        self.assertIn('supervisor_clear_degraded(self.supervisor_id, "progress")', body)

    def test_set_status_marks_and_clears(self):
        body = self._body("_set_status")
        self.assertIn('supervisor_mark_degraded(self.supervisor_id, "status"', body)
        self.assertIn('supervisor_clear_degraded(self.supervisor_id, "status")', body)

    def test_materialise_plan_marks_per_task_and_clears_once_for_the_whole_plan(self):
        body = self._body("_materialise_plan")
        self.assertIn('supervisor_mark_degraded(self.supervisor_id, "task_create"', body)
        self.assertIn('supervisor_clear_degraded(self.supervisor_id, "task_create")', body)

    def test_execute_task_success_path_marks_and_clears_message_and_status(self):
        body = self._body("_execute_task")
        boundary = body.index("except Exception as exc:")
        before = body[:boundary]
        self.assertIn('supervisor_mark_degraded(self.supervisor_id, "task_message"', before)
        self.assertIn('supervisor_clear_degraded(self.supervisor_id, "task_message"', before)
        self.assertIn('supervisor_mark_degraded(self.supervisor_id, "task_status_done"', before)
        self.assertIn('supervisor_clear_degraded(self.supervisor_id, "task_status_done"', before)

    def test_execute_task_failure_path_marks_and_clears_status(self):
        body = self._body("_execute_task")
        boundary = body.index("except Exception as exc:")
        after = body[boundary:]
        self.assertIn('supervisor_mark_degraded(self.supervisor_id, "task_status_failed"', after)
        self.assertIn('supervisor_clear_degraded(self.supervisor_id, "task_status_failed"', after)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qa_supervisor_usage.py tests/test_qa_supervisor_degraded_wiring.py -v`
Expected: the new tests FAIL — none of the five sites call the helpers yet.

- [ ] **Step 3: Wire `_record_usage`**

Replace the whole body of `_record_usage` (the `try` block's inner logic —
the method signature, docstring, and outer `try`/`except` structure stay):

Original inner body (inside the existing `try:`, after the two `import`s):

```python
            frames = [runner.take_last_usage(chat_id), *runner.take_retried_usage(chat_id)]
            for frame in frames:
                if not frame:
                    continue
                models = frame.get("models") or {}
                # The CLI reports cost for the whole turn, not per model, so it
                # is attached to the first row only -- the same rule app.py
                # applies, or a two-model turn would be billed twice.
                cost = frame.get("cost_usd")
                for name, stats in models.items():
                    await db.usage_record(
                        chat_id=chat_id,
                        owner_id=self.owner_id,
                        model=name or (model or ""),
                        provider="proxy" if config.PROXY_ENABLED else "anthropic",
                        input_tokens=stats.get("input_tokens", 0),
                        output_tokens=stats.get("output_tokens", 0),
                        cache_read_tokens=stats.get("cache_read_tokens", 0),
                        cache_creation_tokens=stats.get("cache_creation_tokens", 0),
                        cost_usd=cost,
                        cost_basis=stats.get("cost_basis"),
                        duration_ms=frame.get("duration_ms"),
                        is_error=bool(frame.get("is_error")),
                        origin="orchestrator",
                    )
                    cost = None
        except Exception:  # noqa: BLE001 -- accounting must not fail a turn
            _log.exception(
                "supervisor_usage_not_recorded supervisor_id=%s chat_id=%s",
                self.supervisor_id, chat_id,
            )
```

New:

```python
            frames = [runner.take_last_usage(chat_id), *runner.take_retried_usage(chat_id)]
            any_written = False
            any_failed = False
            for frame in frames:
                if not frame:
                    continue
                models = frame.get("models") or {}
                if not models:
                    continue
                # The CLI reports cost for the whole turn, not per model, so it
                # is attached to the first row only -- the same rule app.py
                # applies, or a two-model turn would be billed twice.
                cost = frame.get("cost_usd")
                for name, stats in models.items():
                    row_id = await db.usage_record(
                        chat_id=chat_id,
                        owner_id=self.owner_id,
                        model=name or (model or ""),
                        provider="proxy" if config.PROXY_ENABLED else "anthropic",
                        input_tokens=stats.get("input_tokens", 0),
                        output_tokens=stats.get("output_tokens", 0),
                        cache_read_tokens=stats.get("cache_read_tokens", 0),
                        cache_creation_tokens=stats.get("cache_creation_tokens", 0),
                        cost_usd=cost,
                        cost_basis=stats.get("cost_basis"),
                        duration_ms=frame.get("duration_ms"),
                        is_error=bool(frame.get("is_error")),
                        origin="orchestrator",
                    )
                    cost = None
                    if row_id is None:
                        any_failed = True
                    else:
                        any_written = True
            if any_failed:
                await db.supervisor_mark_degraded(
                    self.supervisor_id, "usage",
                    f"usage_record returned no row id for chat_id={chat_id}",
                )
            elif any_written:
                await db.supervisor_clear_degraded(self.supervisor_id, "usage")
        except Exception:  # noqa: BLE001 -- accounting must not fail a turn
            _log.exception(
                "supervisor_usage_not_recorded supervisor_id=%s chat_id=%s",
                self.supervisor_id, chat_id,
            )
            try:
                await db.supervisor_mark_degraded(
                    self.supervisor_id, "usage", f"usage recording raised for chat_id={chat_id}",
                )
            except Exception:  # noqa: BLE001 -- must not compound the failure
                pass
```

(`db` is already imported inside the `try` block above this point, so the
`except` clause's inner `try: await db.supervisor_mark_degraded(...)` can
reference it — but only if the `NameError` risk described in `_execute_task`'s
own comments does not apply here. It does not: `import db` is the *first*
line inside the outer `try`, so it has already run by the time any later line
in that same `try` could raise and reach this `except`.)

- [ ] **Step 4: Wire `_persist_progress`**

Original:

```python
    async def _persist_progress(self) -> None:
        """Write the graph's overall progress onto the orchestrator row.
        ...
        """
        import db

        try:
            await db.supervisor_update(
                self.supervisor_id, self.owner_id,
                progress_pct=self.graph.overall_progress(),
            )
        except Exception:  # noqa: BLE001 -- reporting must not stop the run
            _log.exception("could not persist progress for %s", self.supervisor_id)
```

New — the caught exception is bound (`except Exception as exc:`) so its text
reaches `degraded_reason` rather than a placeholder string:

```python
    async def _persist_progress(self) -> None:
        """Write the graph's overall progress onto the orchestrator row.
        ...
        """
        import db

        try:
            await db.supervisor_update(
                self.supervisor_id, self.owner_id,
                progress_pct=self.graph.overall_progress(),
            )
            await db.supervisor_clear_degraded(self.supervisor_id, "progress")
        except Exception as exc:  # noqa: BLE001 -- reporting must not stop the run
            _log.exception("could not persist progress for %s", self.supervisor_id)
            try:
                await db.supervisor_mark_degraded(self.supervisor_id, "progress", str(exc))
            except Exception:  # noqa: BLE001
                pass
```

- [ ] **Step 5: Wire `_set_status`**

Original:

```python
    async def _set_status(self, status: str) -> None:
        """Record the run's overall status where the UI actually reads it.
        ...
        """
        self.graph.update_status("orchestrator", status)
        try:
            import db  # local import: db imports this module at load time
            await db.supervisor_update(self.supervisor_id, self.owner_id,
                                       status=status)
        except Exception:  # noqa: BLE001 -- a status write must not end the run
            _log.exception(
                "supervisor_status_not_persisted supervisor_id=%s status=%s",
                self.supervisor_id, status,
            )
```

New:

```python
    async def _set_status(self, status: str) -> None:
        """Record the run's overall status where the UI actually reads it.
        ...
        """
        self.graph.update_status("orchestrator", status)
        try:
            import db  # local import: db imports this module at load time
            await db.supervisor_update(self.supervisor_id, self.owner_id,
                                       status=status)
            await db.supervisor_clear_degraded(self.supervisor_id, "status")
        except Exception as exc:  # noqa: BLE001 -- a status write must not end the run
            _log.exception(
                "supervisor_status_not_persisted supervisor_id=%s status=%s",
                self.supervisor_id, status,
            )
            try:
                await db.supervisor_mark_degraded(self.supervisor_id, "status", str(exc))
            except Exception:  # noqa: BLE001
                pass
```

This is the highest-severity site in the spec's failure-policy table: it is
the exact bug this file's own comments already document (a status stuck on
`"planning"` forever because the write silently failed). `import db` runs
before `db.supervisor_update` in the same `try`, so it is bound by the time
the `except` clause's own `db.supervisor_mark_degraded` call runs.

- [ ] **Step 6: Wire `_materialise_plan`**

Original per-task loop (inside `_materialise_plan`, after `_row_id` is
defined):

```python
        for parsed_task in tasks:
            node = TaskNode(
                id=_row_id(parsed_task.id),
                title=parsed_task.title,
                description=parsed_task.description,
                model=parsed_task.model,
                parent_id=None,
                depends_on=[_row_id(d) for d in parsed_task.depends_on],
                created_at=db._now(),
                updated_at=db._now(),
            )
            self.graph.add_task(node)
            try:
                await db.supervisor_task_create(
                    supervisor_id=self.supervisor_id,
                    task_id=node.id,
                    title=parsed_task.title,
                    description=parsed_task.description,
                    model=parsed_task.model,
                    parent_task_id=None,
                    depends_on=node.depends_on,
                )
                self.tracker.record(ProgressEvent(
                    event_type="plan",
                    task_id=node.id,
                    data={
                        "created": True,
                        "title": parsed_task.title,
                        "model": parsed_task.model,
                    },
                ))
            except Exception:  # noqa: BLE001 -- one bad row, not the whole plan
                # `exception`, not `warning`: this was a bare warning with no
                # reason attached, which is why a task list that stayed empty
                # while the work ran took a live run to notice at all.
                _log.exception(
                    "supervisor_task_create failed for %s", parsed_task.id,
                )

        self.config["parsed_tasks"] = [
            {"id": t.id, "title": t.title, "status": t.status}
            for t in self.graph.tasks.values()
        ]
```

New:

```python
        any_task_create_failed = False
        for parsed_task in tasks:
            node = TaskNode(
                id=_row_id(parsed_task.id),
                title=parsed_task.title,
                description=parsed_task.description,
                model=parsed_task.model,
                parent_id=None,
                depends_on=[_row_id(d) for d in parsed_task.depends_on],
                created_at=db._now(),
                updated_at=db._now(),
            )
            self.graph.add_task(node)
            try:
                await db.supervisor_task_create(
                    supervisor_id=self.supervisor_id,
                    task_id=node.id,
                    title=parsed_task.title,
                    description=parsed_task.description,
                    model=parsed_task.model,
                    parent_task_id=None,
                    depends_on=node.depends_on,
                )
                self.tracker.record(ProgressEvent(
                    event_type="plan",
                    task_id=node.id,
                    data={
                        "created": True,
                        "title": parsed_task.title,
                        "model": parsed_task.model,
                    },
                ))
            except Exception as exc:  # noqa: BLE001 -- one bad row, not the whole plan
                # `exception`, not `warning`: this was a bare warning with no
                # reason attached, which is why a task list that stayed empty
                # while the work ran took a live run to notice at all.
                _log.exception(
                    "supervisor_task_create failed for %s", parsed_task.id,
                )
                any_task_create_failed = True
                try:
                    await db.supervisor_mark_degraded(
                        self.supervisor_id, "task_create",
                        f"{node.id}: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass

        # Cleared only once, for the whole plan -- an earlier plan's missing
        # row is not fixed by a later plan's success, so this is not per-task.
        if not any_task_create_failed:
            try:
                await db.supervisor_clear_degraded(self.supervisor_id, "task_create")
            except Exception:  # noqa: BLE001
                pass

        self.config["parsed_tasks"] = [
            {"id": t.id, "title": t.title, "status": t.status}
            for t in self.graph.tasks.values()
        ]
```

- [ ] **Step 7: Wire `_execute_task`'s success path (message append + status update)**

Original success-path block (inside the `try:` of `_execute_task`, after
`graph.update_status(task_id, "done")`):

```python
            # Write the task result to the messages table so the chat shows it.
            try:
                import db
                node_title = node.title or task_id
                clean = clean_result(result)
                # A task can finish having emitted nothing but tool calls, and
                # cleaning those away leaves an empty body. Saying so beats a
                # header over blank space -- and the char count has to describe
                # what is actually displayed, not the text that was filtered
                # out, or it reads as a message that failed to load.
                if clean:
                    body = f"Task '{node_title}' completed ({len(clean)} chars)\n\n{clean[:3000]}"
                else:
                    body = (
                        f"Task '{node_title}' completed with no text output "
                        f"-- it only made tool calls."
                    )
                await db.supervisor_messages_append(
                    self.supervisor_id, "orchestrator",
                    body,
                    {"kind": "task_result", "task_id": task_id},
                )
            except Exception:  # noqa: BLE001 -- task success must not fail silently
                _log.exception("could not record task result message for %s", task_id)

            # Also update DB task row
            try:
                import db
                await db.supervisor_task_update(
                    supervisor_id=self.supervisor_id,
                    task_id=task_id,
                    owner_id=self.owner_id,
                    status="done",
                    result=result,
                    progress_pct=100.0,
                )
            except Exception:  # noqa: BLE001
                _log.exception("could not record task %s as done", task_id)

            return result
```

New:

```python
            # Write the task result to the messages table so the chat shows it.
            try:
                import db
                node_title = node.title or task_id
                clean = clean_result(result)
                # A task can finish having emitted nothing but tool calls, and
                # cleaning those away leaves an empty body. Saying so beats a
                # header over blank space -- and the char count has to describe
                # what is actually displayed, not the text that was filtered
                # out, or it reads as a message that failed to load.
                if clean:
                    body = f"Task '{node_title}' completed ({len(clean)} chars)\n\n{clean[:3000]}"
                else:
                    body = (
                        f"Task '{node_title}' completed with no text output "
                        f"-- it only made tool calls."
                    )
                await db.supervisor_messages_append(
                    self.supervisor_id, "orchestrator",
                    body,
                    {"kind": "task_result", "task_id": task_id},
                )
                await db.supervisor_clear_degraded(self.supervisor_id, "task_message")
            except Exception as msg_exc:  # noqa: BLE001 -- task success must not fail silently
                _log.exception("could not record task result message for %s", task_id)
                try:
                    import db
                    await db.supervisor_mark_degraded(
                        self.supervisor_id, "task_message", f"{task_id}: {msg_exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass

            # Also update DB task row
            try:
                import db
                await db.supervisor_task_update(
                    supervisor_id=self.supervisor_id,
                    task_id=task_id,
                    owner_id=self.owner_id,
                    status="done",
                    result=result,
                    progress_pct=100.0,
                )
                await db.supervisor_clear_degraded(self.supervisor_id, "task_status_done")
            except Exception as status_exc:  # noqa: BLE001
                _log.exception("could not record task %s as done", task_id)
                try:
                    await db.supervisor_mark_degraded(
                        self.supervisor_id, "task_status_done", f"{task_id}: {status_exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass

            return result
```

Note the `kind` string used for `mark`/`clear` here is the bare literal
`"task_message"` / `"task_status_done"` (no `:{task_id}` suffix on the
*kind* itself — the task id is part of the `detail` argument, which becomes
the human-readable text after the `kind:` prefix inside `degraded_reason`,
e.g. `"task_message: t003: RuntimeError(...)"`). The structural test in
Step 1 above checks for the literal string
`'supervisor_mark_degraded(self.supervisor_id, "task_message"'` (kind as a
bare literal, not including the task id), matching this.

Also note the two inner exception bindings here are `msg_exc` and
`status_exc`, not `exc`. Both blocks sit inside `_execute_task`'s outer
`try:`, ahead of its real failure-path boundary
(`except Exception as exc:` in Step 8 below) — the Step 1 test's
`_body("_execute_task")` helper locates that boundary by the *first*
occurrence of the literal string `"except Exception as exc:"`, so naming
either of these two inner bindings `exc` would make the test mistake an
inner block's own except for the real outer boundary and silently truncate
its "before" slice.

- [ ] **Step 8: Wire `_execute_task`'s failure path**

Original failure-path block (the method's outer `except Exception as exc:`):

```python
        except Exception as exc:  # noqa: BLE001
            graph.update_status(task_id, "failed")
            # A failed turn still spent tokens, and often more than a successful
            # one: a task that ran for two minutes and then hit an error has been
            # paid for. Recording only on success would make the cheapest-looking
            # orchestrator the one that fails most.
            await self._record_usage(task_chat_id, model)
            self.tracker.record(ProgressEvent(
                event_type="task_error",
                task_id=task_id,
                data={"error": str(exc)},
            ))
            _log.error("Task %s failed: %s", task_id, exc)

            # Also update DB task row
            try:
                import db
                await db.supervisor_task_update(
                    supervisor_id=self.supervisor_id,
                    task_id=task_id,
                    owner_id=self.owner_id,
                    status="failed",
                    progress_pct=0.0,
                )
            except Exception:  # noqa: BLE001
                _log.exception("could not record task %s as failed", task_id)

            return ""
```

New:

```python
        except Exception as exc:  # noqa: BLE001
            graph.update_status(task_id, "failed")
            # A failed turn still spent tokens, and often more than a successful
            # one: a task that ran for two minutes and then hit an error has been
            # paid for. Recording only on success would make the cheapest-looking
            # orchestrator the one that fails most.
            await self._record_usage(task_chat_id, model)
            self.tracker.record(ProgressEvent(
                event_type="task_error",
                task_id=task_id,
                data={"error": str(exc)},
            ))
            _log.error("Task %s failed: %s", task_id, exc)

            # Also update DB task row
            try:
                import db
                await db.supervisor_task_update(
                    supervisor_id=self.supervisor_id,
                    task_id=task_id,
                    owner_id=self.owner_id,
                    status="failed",
                    progress_pct=0.0,
                )
                await db.supervisor_clear_degraded(self.supervisor_id, "task_status_failed")
            except Exception as write_exc:  # noqa: BLE001
                _log.exception("could not record task %s as failed", task_id)
                try:
                    await db.supervisor_mark_degraded(
                        self.supervisor_id, "task_status_failed", f"{task_id}: {write_exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass

            return ""
```

(Renamed the inner exception binding to `write_exc` — the outer `except
Exception as exc:` already bound `exc` to the *task's own* failure, and
reusing that name for the DB-write failure inside this nested `try` would
shadow it silently.)

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_supervisor_usage.py tests/test_qa_supervisor_degraded_wiring.py -v`
Expected: PASS, all tests.

- [ ] **Step 10: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: prior count + 9 (4 in `test_qa_supervisor_usage.py` +
5 in `test_qa_supervisor_degraded_wiring.py`), 6 skips. Also explicitly
re-run the orchestrator route/orchestration tests since this task edits
`orchestrator.py` broadly:

Run: `.venv/bin/python -m pytest tests/test_qa_supervisor_route.py tests/test_qa_supervisor_members_ui.py tests/test_qa_supervisor_usage.py -v`

- [ ] **Step 11: Commit**

```bash
git status --porcelain -- orchestrator.py tests/test_qa_supervisor_usage.py tests/test_qa_supervisor_degraded_wiring.py
git add orchestrator.py tests/test_qa_supervisor_usage.py tests/test_qa_supervisor_degraded_wiring.py
git commit -m "feat: mark a orchestrator degraded when its own DB writes silently fail"
```

---

### Task 5: Frontend — the sidebar's degraded badge

**Files:**
- Modify: `web/assets/chat-list.js:234-330` (`renderSection`)
- Modify: `web/assets/styles.css` (near the existing `.chat-queued` rule,
  line 186)
- Test: `tests/test_frontend.py` (extend)

**Interfaces:**
- Consumes: `chat.degraded` (0/1), `chat.degraded_reason` (string or null) —
  both already ride along on `GET /api/chats` once Task 1 lands, since
  `chat_list`/`chat_get` `SELECT` from `_CHAT_COLUMNS` and return
  `dict(row)` directly.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_frontend.py`'s `FrontendStructureTests` class:

```python
    def test_degraded_chats_show_a_badge(self):
        """chat.degraded rides along on GET /api/chats once Task 1 lands;
        the sidebar must read it rather than silently ignoring the field."""
        self.assertIn("chat.degraded", self.chat_list)
        self.assertIn("chat-degraded", self.chat_list)
        self.assertIn(".chat-degraded{", self.css)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -k test_degraded_chats_show_a_badge -v`
Expected: FAIL — none of the three strings exist yet.

- [ ] **Step 3: Add the badge**

In `web/assets/chat-list.js`, inside `renderSection`, immediately after the
existing:

```js
      if (chat.queued) {
        const queued = document.createElement('span');
        queued.className = 'chat-queued';
        queued.textContent = String(chat.queued);
        queued.setAttribute('aria-label', `${chat.queued} prompts queued`);
        queued.title = `${chat.queued} prompt${chat.queued > 1 ? 's' : ''} waiting to send`;
        title.append(queued);
      }
```

add:

```js
      // Independent of the leading-dot chain above (running/terminal-busy/
      // unread/ended/free): a chat can be actively running right now and
      // still carry a degraded flag from an earlier turn's silent write
      // failure -- the two facts do not exclude each other, so this is a
      // second, trailing marker rather than another tier in that chain.
      if (chat.degraded) {
        const warn = document.createElement('span');
        warn.className = 'chat-degraded';
        warn.textContent = '⚠';
        warn.setAttribute('aria-label', 'Some state for this conversation may be stale');
        warn.title = chat.degraded_reason
          || 'A background write failed for this conversation; check the server logs.';
        title.append(warn);
      }
```

- [ ] **Step 4: Add the CSS**

In `web/assets/styles.css`, immediately after the existing `.chat-queued`
rule (line 186):

```css
.chat-degraded{display:inline-block;margin-left:6px;color:var(--warn);font-size:11px;vertical-align:middle;cursor:help}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -v`
Expected: PASS, including the new test and every pre-existing one in that file.

- [ ] **Step 6: Commit**

```bash
git status --porcelain -- web/assets/chat-list.js web/assets/styles.css tests/test_frontend.py
git add web/assets/chat-list.js web/assets/styles.css tests/test_frontend.py
git commit -m "feat: show a degraded badge in the sidebar for chats with a known silent write failure"
```

---

### Task 6: Frontend — the orchestrator list's degraded title/glyph

**Files:**
- Modify: `web/assets/orchestrator/list.js:78-89` (`renderSupervisorList`)
- Test: `tests/test_frontend.py` (extend — this file already reads
  `orchestrator.js`; extend it to also read `orchestrator/list.js`)

**Interfaces:**
- Consumes: `s.degraded`, `s.degraded_reason` — both ride along on
  `GET /api/supervisors` once Task 2 lands.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_frontend.py`'s `setUpClass` (alongside the existing
`cls.orchestrator = (ASSETS / "orchestrator.js").read_text()`):

```python
        cls.supervisor_list_js = (ASSETS / "orchestrator" / "list.js").read_text()
```

Add a test:

```python
    def test_degraded_supervisors_are_marked_on_their_status_badge(self):
        self.assertIn("s.degraded", self.supervisor_list_js)
        self.assertIn("degraded_reason", self.supervisor_list_js)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -k test_degraded_supervisors_are_marked_on_their_status_badge -v`
Expected: FAIL.

- [ ] **Step 3: Add the marker**

In `web/assets/orchestrator/list.js`, `renderSupervisorList`'s template string
currently has:

```js
        return `<div class="orchestrator-list-item ${
          s.id === state.activeSupervisorId ? "active" : ""
        }" data-id="${esc(s.id)}">
          <div class="sl-title">${esc(s.title || "Untitled")}</div>
          <div class="sl-status">
            <span class="status-badge ${esc(statusClass)}">${esc(statusClass)}</span>
            ${s.progress_pct != null ? `<span>${Math.round(s.progress_pct)}%</span>` : ""}
            ${timeLabel ? `<span style="margin-left:4px">${timeLabel}</span>` : ""}
          </div>
        </div>`;
```

Replace the `status-badge` line with:

```js
        const degradedTitle = s.degraded
          ? ` title="${esc(s.degraded_reason || 'Some state may be stale')}"`
          : "";
        const degradedGlyph = s.degraded ? " ⚠" : "";
        return `<div class="orchestrator-list-item ${
          s.id === state.activeSupervisorId ? "active" : ""
        }" data-id="${esc(s.id)}">
          <div class="sl-title">${esc(s.title || "Untitled")}</div>
          <div class="sl-status">
            <span class="status-badge ${esc(statusClass)}"${degradedTitle}>${esc(statusClass)}${degradedGlyph}</span>
            ${s.progress_pct != null ? `<span>${Math.round(s.progress_pct)}%</span>` : ""}
            ${timeLabel ? `<span style="margin-left:4px">${timeLabel}</span>` : ""}
          </div>
        </div>`;
```

(`degradedTitle`/`degradedGlyph` must be declared before the `return`
statement inside the same `.map((s) => { ... })` callback, alongside the
existing `statusClass`/`timeLabel` locals.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git status --porcelain -- web/assets/orchestrator/list.js tests/test_frontend.py
git add web/assets/orchestrator/list.js tests/test_frontend.py
git commit -m "feat: mark a degraded orchestrator's status badge in the sidebar list"
```

---

### Task 7: `computeLayers` — pure dependency layering, unit-tested

**Files:**
- Create: `web/assets/orchestrator/rail.js` (only `computeLayers` in this task
  — `renderRail` is added in Task 8, since it needs the DOM ref this task
  does not touch)
- Test: `tests/test_qa_rail_layers.py` (new — runs the JS through Node if
  available, or asserts on source structure if not; see Step 1)

**Interfaces:**
- Produces: `export function computeLayers(tasks)` — pure, no DOM. Input:
  array of `{id, depends_on}`-shaped objects (a subset of the task shape
  `supervisor_tasks_get` already returns). Output: array of arrays of the
  same task objects, grouped into dependency layers.

This repo's frontend has no build step and no existing JS unit-test runner
(every existing frontend test in `tests/test_frontend.py` is a structural
source-string assertion, per `CLAUDE.md`'s silence on a JS test runner and
`tests/test_frontend.py`'s own docstring pattern). Rather than introduce a
new test runner for one pure function, this task tests `computeLayers`
two ways: a structural test matching this repo's existing convention, and a
small inline `node -e` smoke test run manually in Step 4 (not part of the
pytest suite, since there is no Node dependency declared anywhere in this
repo and adding one is out of scope) to verify the logic by hand before
committing.

- [ ] **Step 1: Write the failing structural test**

Create `tests/test_qa_rail_layers.py`:

```python
"""QA: computeLayers is a pure, DOM-free function -- structural checks only,
matching this repo's convention for frontend logic (see test_frontend.py).
The actual topological-sort behavior is verified by hand via `node -e` per
this task's plan step, since there is no JS test runner in this repo.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"


class RailLayersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ASSETS / "orchestrator" / "rail.js").read_text()

    def test_compute_layers_is_exported_and_pure(self):
        self.assertIn("export function computeLayers(tasks)", self.source)
        # No DOM access in this function specifically -- renderRail (added in
        # a later task) is the only place in this file allowed to touch it.
        start = self.source.index("export function computeLayers(")
        rest = self.source[start:]
        end = rest.find("\nexport function ", 1)
        body = rest[:end] if end != -1 else rest
        self.assertNotIn("document.", body)

    def test_cycle_safety_is_present(self):
        """A cycle should never reach here (PlanParser excludes
        self-references) but must not hang the UI if one somehow does."""
        self.assertIn("pass <= tasks.length", self.source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_rail_layers.py -v`
Expected: FAIL — `web/assets/orchestrator/rail.js` does not exist yet.

- [ ] **Step 3: Create `rail.js` with `computeLayers`**

Create `web/assets/orchestrator/rail.js`:

```js
// orchestrator/rail.js — dependency-ordered overview of the task DAG.
//
// Additive: the flat #task-tree list (tasks.js) stays exactly as it is for
// per-task detail and click-to-expand. This is an overview strip above it.

/** Topologically layer *tasks* by depends_on. Pure, DOM-free, unit-testable.
 *
 * A task lands in the earliest layer every one of its dependencies has
 * already cleared. A dependency cycle should never reach here -- PlanParser
 * (orchestrator.py) excludes self-references -- but must not hang the UI if
 * one somehow does: anything still unplaced after `tasks.length` passes is
 * dumped into one final "unresolved" layer rather than looping forever.
 */
export function computeLayers(tasks) {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const placed = new Map(); // id -> layer index
  const layers = [];
  let remaining = tasks.slice();
  let pass = 0;
  while (remaining.length && pass <= tasks.length) {
    const ready = remaining.filter((t) =>
      (t.depends_on || []).every((d) => !byId.has(d) || placed.has(d))
    );
    if (!ready.length) break; // cycle or missing dep -- fall through below
    const layerIndex = layers.length;
    layers.push(ready);
    ready.forEach((t) => placed.set(t.id, layerIndex));
    const readyIds = new Set(ready.map((t) => t.id));
    remaining = remaining.filter((t) => !readyIds.has(t.id));
    pass += 1;
  }
  if (remaining.length) layers.push(remaining); // unresolved: cycle safety
  return layers;
}
```

- [ ] **Step 4: Verify the logic by hand with Node**

This step is manual verification, not part of the automated suite (see this
task's preamble for why). Run:

```bash
node -e '
import("./web/assets/orchestrator/rail.js").then(({computeLayers}) => {
  const layers = computeLayers([
    {id: "a", depends_on: []},
    {id: "b", depends_on: ["a"]},
    {id: "c", depends_on: ["a"]},
    {id: "d", depends_on: ["b", "c"]},
  ]);
  console.log(JSON.stringify(layers.map(l => l.map(t => t.id))));
  // Expected: [["a"],["b","c"],["d"]]

  const cyclic = computeLayers([
    {id: "x", depends_on: ["y"]},
    {id: "y", depends_on: ["x"]},
  ]);
  console.log(JSON.stringify(cyclic.map(l => l.map(t => t.id))));
  // Expected: [["x","y"]] -- the unresolved fallback layer, not a hang
});
'
```

Confirm both printed lines match the expected comments before continuing. If
`node` is not available in this environment, skip this step and rely on the
structural test plus careful reading — do not skip writing `rail.js` itself.

- [ ] **Step 5: Run the structural test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_rail_layers.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git status --porcelain -- web/assets/orchestrator/rail.js tests/test_qa_rail_layers.py
git add web/assets/orchestrator/rail.js tests/test_qa_rail_layers.py
git commit -m "feat: add a pure dependency-layering function for the task rail"
```

---

### Task 8: `renderRail` — the visible rail, wired into the pane

**Files:**
- Modify: `web/assets/orchestrator/rail.js` (add `renderRail`)
- Modify: `web/assets/orchestrator/dom.js` (add `taskRail` ref)
- Modify: `web/orchestrator.html` (new `#task-rail` div, new CSS rules,
  `<script>` version bump)
- Modify: `web/assets/orchestrator/tasks.js` (call `renderRail` from
  `loadTasks` and `renderTaskTree`)
- Modify: `web/assets/orchestrator/stream.js` (call `renderRail` from
  `handleProgress`)
- Test: `tests/test_frontend.py` (extend)

**Interfaces:**
- Consumes: `computeLayers` (Task 7), `state.tasks` (already populated by
  `tasks.js`/`stream.js`).
- Produces: `export function renderRail(tasks)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_frontend.py`'s `setUpClass`:

```python
        cls.rail = (ASSETS / "orchestrator" / "rail.js").read_text()
        cls.tasks_js = (ASSETS / "orchestrator" / "tasks.js").read_text()
        cls.stream_js = (ASSETS / "orchestrator" / "stream.js").read_text()
        cls.supervisor_html = (WEB / "orchestrator.html").read_text()
```

Also add `cls.dom_js = (ASSETS / "orchestrator" / "dom.js").read_text()` to
`setUpClass`, then add:

```python
    def test_rail_is_rendered_and_wired_into_the_pane(self):
        self.assertIn("export function renderRail(tasks)", self.rail)
        self.assertIn('id="task-rail"', self.supervisor_html)
        self.assertIn("renderRail(state.tasks)", self.tasks_js)
        self.assertIn("taskRail", self.dom_js)
```

(No `stream.js` assertion: `stream.js:handleProgress` already calls
`renderTaskTree()`, and Step 7 below makes `renderTaskTree` itself call
`renderRail` — so every existing caller of `renderTaskTree`, in both
`tasks.js` and `stream.js`, gets the rail for free with no separate wiring in
`stream.js`. Do not add `cls.stream_js` for this test either.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -k test_rail_is_rendered_and_wired_into_the_pane -v`
Expected: FAIL.

- [ ] **Step 3: Add `#task-rail` to `orchestrator.html`**

In `web/orchestrator.html`, the left panel currently reads (around line
997-1010):

```html
        <div id="panel-left">
            <div class="panel-header">
                <button class="panel-btn" data-panel="left" title="Minimize">&#9654;</button>
                <h2>Task Tree</h2>
                <button class="panel-btn" id="sortToggleBtn" title="Sort by: newest" aria-label="Sort by: newest">&#8645;</button>
                <button class="panel-btn" data-max="left" title="Maximize">&#9633;</button>
            </div>
            <div id="orchestrator-list"></div>
            <!-- tabindex="-1" so the 1-4 shortcuts can focus these panels.
                 A div is not focusable without it, so .focus() was silently
                 doing nothing. -1 keeps them out of the tab order while still
                 letting arrow keys scroll them once focused. -->
            <div id="task-tree" tabindex="-1"></div>
```

Insert `#task-rail` between `#orchestrator-list` and `#task-tree`:

```html
        <div id="panel-left">
            <div class="panel-header">
                <button class="panel-btn" data-panel="left" title="Minimize">&#9654;</button>
                <h2>Task Tree</h2>
                <button class="panel-btn" id="sortToggleBtn" title="Sort by: newest" aria-label="Sort by: newest">&#8645;</button>
                <button class="panel-btn" data-max="left" title="Maximize">&#9633;</button>
            </div>
            <div id="orchestrator-list"></div>
            <!-- Dependency-ordered overview of the current plan's tasks.
                 Additive to #task-tree below, not a replacement: this shows
                 shape (what depends on what, and each one's status at a
                 glance); #task-tree keeps the per-task detail and
                 click-to-expand. Hidden when there are no tasks yet. -->
            <div id="task-rail" hidden></div>
            <!-- tabindex="-1" so the 1-4 shortcuts can focus these panels.
                 A div is not focusable without it, so .focus() was silently
                 doing nothing. -1 keeps them out of the tab order while still
                 letting arrow keys scroll them once focused. -->
            <div id="task-tree" tabindex="-1"></div>
```

- [ ] **Step 4: Add the CSS**

In `web/orchestrator.html`'s `<style>` block, add (near the existing
`.task-status-dot` rules, which `#task-rail`'s nodes reuse directly):

```css
        /* ── Task rail: dependency-ordered overview above #task-tree ──── */
        #task-rail {
            display: flex;
            align-items: center;
            gap: 4px;
            padding: 8px 12px;
            border-bottom: 1px solid #e2e8f0;
            overflow-x: auto;
            flex-shrink: 0;
        }
        #task-rail[hidden] { display: none; }
        .rail-layer {
            display: flex;
            flex-direction: column;
            gap: 4px;
            flex-shrink: 0;
        }
        .rail-connector {
            width: 16px;
            height: 2px;
            background: #e2e8f0;
            flex-shrink: 0;
        }
```

(`.rail-layer`'s children reuse `.task-status-dot` and its
`.pending`/`.ready`/`.running`/`.done`/`.failed`/`.blocked` modifiers exactly
as already defined — no new color rules needed.)

- [ ] **Step 5: Add `taskRail` to `dom.js`**

In `web/assets/orchestrator/dom.js`, add one line to the `el` object (next to
`taskTree: $("#task-tree"),`):

```js
    taskRail: $("#task-rail"),
```

- [ ] **Step 6: Add `renderRail` to `rail.js`**

Append to `web/assets/orchestrator/rail.js` (after `computeLayers`):

```js
import { el } from "./dom.js";

export function renderRail(tasks) {
  if (!el.taskRail) return;
  if (!tasks.length) {
    el.taskRail.replaceChildren();
    el.taskRail.hidden = true;
    return;
  }
  el.taskRail.hidden = false;
  const layers = computeLayers(tasks);
  el.taskRail.replaceChildren();
  layers.forEach((layer, index) => {
    const col = document.createElement("div");
    col.className = "rail-layer";
    layer.forEach((t) => {
      const node = document.createElement("span");
      node.className = `task-status-dot ${t.status || "pending"}`;
      node.title = `${t.title || t.id} — ${t.status || "pending"}`;
      col.appendChild(node);
    });
    el.taskRail.appendChild(col);
    if (index < layers.length - 1) {
      const connector = document.createElement("div");
      connector.className = "rail-connector";
      el.taskRail.appendChild(connector);
    }
  });
}
```

The `import` line must move to the top of the file (ES modules require
imports before other statements are conventionally placed, and this repo's
existing files all do so) — the final file should read:

```js
// orchestrator/rail.js — dependency-ordered overview of the task DAG.
//
// Additive: the flat #task-tree list (tasks.js) stays exactly as it is for
// per-task detail and click-to-expand. This is an overview strip above it.

import { el } from "./dom.js";

/** Topologically layer *tasks* by depends_on. Pure, DOM-free, unit-testable.
 * ...
 */
export function computeLayers(tasks) {
  ...
}

export function renderRail(tasks) {
  ...
}
```

- [ ] **Step 7: Wire `renderRail` into `tasks.js`**

In `web/assets/orchestrator/tasks.js`, add the import (next to the existing
imports at the top):

```js
import { renderRail } from "./rail.js";
```

In `renderTaskTree()`, add a call at the very start of the function (it
already has an early-return branch for the empty case — mirror that with the
rail):

```js
  export function renderTaskTree() {
    renderRail(state.tasks);
    if (!state.tasks.length) {
```

`renderTaskTree` does not currently import `state` — check the top of
`tasks.js`: it already does (`import { state } from "./state.js";` is the
first import line). No new import needed for `state` itself, only for
`renderRail`.

`stream.js` needs **no changes** for the rail: `handleProgress` already calls
`renderTaskTree()` unconditionally whenever `data.tasks` is present, and
`renderTaskTree` now always calls `renderRail(state.tasks)` first (this
step) — so both existing callers of `renderTaskTree` (`tasks.js:loadTasks`
and `stream.js:handleProgress`) get the rail for free, with the wiring
living in exactly one place.

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -v`
Expected: PASS, including the new test and every pre-existing one.

- [ ] **Step 9: Commit**

```bash
git status --porcelain -- web/assets/orchestrator/rail.js web/assets/orchestrator/dom.js web/orchestrator.html web/assets/orchestrator/tasks.js tests/test_frontend.py
git add web/assets/orchestrator/rail.js web/assets/orchestrator/dom.js web/orchestrator.html web/assets/orchestrator/tasks.js tests/test_frontend.py
git commit -m "feat: render a dependency-ordered task rail above the task tree"
```

---

### Task 9: The human-gate marker

**Files:**
- Modify: `web/assets/orchestrator/banners.js` (add `updateGateMarker`)
- Modify: `web/assets/orchestrator/dom.js` (add `topbarGate` ref)
- Modify: `web/assets/orchestrator/state.js` (add `members: []`)
- Modify: `web/assets/orchestrator/members.js` (`loadMembers` populates
  `state.members` and calls `updateGateMarker`)
- Modify: `web/assets/orchestrator/list.js` (`showActiveSupervisor` calls
  `loadMembers` — see rationale in Step 3; this closes a pre-existing gap
  where members were never loaded on orchestrator selection, only after using
  the add/remove picker)
- Modify: `web/assets/orchestrator/stream.js` (`handleStatusUpdate` and
  `handleProgress` call `updateGateMarker`)
- Modify: `web/assets/orchestrator/main.js` (extend the existing 30s poller —
  no new interval — and import `loadMembers`)
- Modify: `web/orchestrator.html` (new `#topbar-gate` button, new CSS)
- Test: `tests/test_frontend.py` (extend)

**Interfaces:**
- Consumes: `state.activeSupervisor.status`, `state.members` (new).
- Produces: `export function updateGateMarker(supervisorStatus, members)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_frontend.py`'s `setUpClass`:

```python
        cls.banners_js = (ASSETS / "orchestrator" / "banners.js").read_text()
        cls.members_js = (ASSETS / "orchestrator" / "members.js").read_text()
        cls.list_js = (ASSETS / "orchestrator" / "list.js").read_text()
        cls.main_js_supervisor = (ASSETS / "orchestrator" / "main.js").read_text()
        # Task 8 deliberately dropped cls.stream_js (it needed no stream.js
        # change), so it must be re-added here: this task's own test below
        # references self.stream_js.
        cls.stream_js = (ASSETS / "orchestrator" / "stream.js").read_text()
```

Add:

```python
    def test_human_gate_marker_exists_and_is_wired(self):
        self.assertIn("export function updateGateMarker(supervisorStatus, members)", self.banners_js)
        self.assertIn('id="topbar-gate"', self.supervisor_html)
        self.assertIn("updateGateMarker(", self.members_js)
        self.assertIn("updateGateMarker(", self.stream_js)
        self.assertIn("topbarGate", self.dom_js)
        self.assertIn("state.members", self.members_js)
        # Members must load when a orchestrator is opened, not only after using
        # the add/remove picker -- otherwise the gate marker is blind until
        # someone happens to touch that dialog.
        self.assertIn("loadMembers(", self.list_js)
        self.assertIn("loadMembers", self.main_js_supervisor)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -k test_human_gate_marker_exists_and_is_wired -v`
Expected: FAIL.

- [ ] **Step 3: Add `members` to `state.js`**

In `web/assets/orchestrator/state.js`, add one field to the exported `state`
object (next to `eventLog: [],`):

```js
  members: [],
```

- [ ] **Step 4: Add `#topbar-gate` and its CSS to `orchestrator.html`**

The topbar currently reads:

```html
    <div id="topbar">
        <a href="/" class="home-link">WebConsole</a>
        <span>&#8212;</span>
        <span>Orchestrator</span>
        <span id="topbar-badge" class="notif-badge" hidden>0</span>
        <button id="new-orchestrator-btn">+ New</button>
```

Change to:

```html
    <div id="topbar">
        <a href="/" class="home-link">WebConsole</a>
        <span>&#8212;</span>
        <span>Orchestrator</span>
        <span id="topbar-badge" class="notif-badge" hidden>0</span>
        <!-- A button, not a span like #topbar-badge: this one is clickable
             (it un-maximizes every panel and jumps to whatever is actually
             gating on a person), so it must be reachable by keyboard. -->
        <button type="button" id="topbar-gate" class="notif-badge gate-badge" hidden title=""></button>
        <button id="new-orchestrator-btn">+ New</button>
```

Add CSS near the existing `.notif-badge` rules:

```css
        /* ── Human-gate marker: the one thing actually blocked on a person ── */
        .gate-badge {
            cursor: pointer;
            border: none;
            background: #ef4444;
        }
        .gate-badge:hover { background: #dc2626; }
```

(`.notif-badge`'s existing rules already give it size, shape, and the
`.visible` opacity/transform transition — `.gate-badge` only overrides color
and adds pointer/border-reset since it is now a `<button>`, not a `<span>`.)

- [ ] **Step 5: Add `topbarGate` to `dom.js`**

In `web/assets/orchestrator/dom.js`, add (next to `topbarBadge: $("#topbar-badge"),`):

```js
    topbarGate: $("#topbar-gate"),
```

- [ ] **Step 6: Add `updateGateMarker` to `banners.js`**

Append to `web/assets/orchestrator/banners.js`:

```js
  // ── Human-gate marker ────────────────────────────────────────────────
  // The one topbar element every body.max-* state leaves visible, so this is
  // the only reliable place to say "something needs you" regardless of which
  // panel is currently maximized. Two things actually gate a orchestrator on a
  // person -- the engine paused, or a watched member whose own status is
  // "waiting" -- see docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md
  // for why the task DAG itself never does (subtasks run one-shot,
  // non-interactive turns and never wait on anyone).

  export function updateGateMarker(supervisorStatus, members) {
    const badge = el.topbarGate;
    if (!badge) return;
    const waitingMember = (members || []).find((m) => m.status === "waiting");
    const gate = supervisorStatus === "paused"
      ? { kind: "paused" }
      : waitingMember
        ? { kind: "member", member: waitingMember }
        : null;
    if (!gate) {
      badge.hidden = true;
      badge.onclick = null;
      return;
    }
    badge.hidden = false;
    badge.textContent = "!";
    badge.title = gate.kind === "paused"
      ? "Paused — resume when ready"
      : `Waiting on you: ${gate.member.title || gate.member.id}`;
    badge.onclick = () => {
      document.body.classList.remove("max-left", "max-right", "max-center", "max-bottom");
      if (gate.kind === "paused") {
        el.pauseResumeBtn?.focus();
      } else {
        const row = document.querySelector(
          `.member-row[data-chat-id="${CSS.escape(gate.member.id)}"]`
        ) || document.getElementById("membersPanel");
        row?.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    };
  }
```

`members.js`'s row-building code does not currently set `data-chat-id` on
each `.member-row` — add it in Step 7 below so the click handler above can
find the specific row rather than only scrolling to the panel as a whole.

- [ ] **Step 7: Wire `loadMembers` to populate `state.members` and call `updateGateMarker`**

In `web/assets/orchestrator/members.js`, add two imports at the top (next to
the existing `import { apiFetch, formatTime } from "./api.js";`):

```js
import { state } from "./state.js";
import { updateGateMarker } from "./banners.js";
```

In `loadMembers`, the function currently reads:

```js
  export async function loadMembers(supervisorId) {
    const panel = document.getElementById("membersPanel");
    if (!panel || !supervisorId) return;
    let members = [];
    try {
      members = (await apiFetch(
        `/api/supervisors/${encodeURIComponent(supervisorId)}/members`)).members || [];
    } catch (err) {
      if (!panel.children.length) panel.replaceChildren(membersEmpty(err.message));
      return;
    }
    panel.replaceChildren();
    if (!members.length) {
      panel.appendChild(membersEmpty("No members yet. Use + to add one."));
      return;
    }
    members.forEach((m) => {
      const row = document.createElement('div');
      row.className = 'member-row';
```

Change to:

```js
  export async function loadMembers(supervisorId) {
    const panel = document.getElementById("membersPanel");
    if (!panel || !supervisorId) return;
    let members = [];
    try {
      members = (await apiFetch(
        `/api/supervisors/${encodeURIComponent(supervisorId)}/members`)).members || [];
    } catch (err) {
      if (!panel.children.length) panel.replaceChildren(membersEmpty(err.message));
      return;
    }
    state.members = members;
    updateGateMarker(state.activeSupervisor?.status, state.members);
    panel.replaceChildren();
    if (!members.length) {
      panel.appendChild(membersEmpty("No members yet. Use + to add one."));
      return;
    }
    members.forEach((m) => {
      const row = document.createElement('div');
      row.className = 'member-row';
      row.dataset.chatId = m.id;
```

(The `row.dataset.chatId = m.id;` line is the one referenced by the click
handler in Step 6 — `m.id` is already the field this loop uses elsewhere in
the same function, e.g. `drop.addEventListener('click', () => removeMember(supervisorId, m.id));`.)

- [ ] **Step 8: Load members when a orchestrator is opened, not only after the picker**

In `web/assets/orchestrator/list.js`, add the import:

```js
import { loadMembers } from "./members.js";
```

In `showActiveSupervisor()`, the function currently ends with:

```js
    // Start SSE stream
    connectSSE();
  }
```

Change to:

```js
    // Start SSE stream
    connectSSE();

    // Members were previously only loaded as a side effect of the
    // add/remove picker (members.js's own two call sites) -- opening a
    // orchestrator never populated the panel, so the human-gate marker
    // (banners.js:updateGateMarker) had nothing to read until someone
    // happened to touch that dialog. This is the fix, not a new feature:
    // the panel already exists and is meant to reflect membership from the
    // moment a orchestrator is opened.
    loadMembers(state.activeSupervisorId);
  }
```

- [ ] **Step 9: Call `updateGateMarker` from `stream.js`**

`stream.js` already imports from `banners.js` (line 5:
`import { incrementBadge, showCompletionBanner, shrinkGoalBanner } from "./banners.js";`).
Add `updateGateMarker` to that *same* import line rather than adding a
second, redundant `import ... from "./banners.js"` statement:

```js
import { incrementBadge, showCompletionBanner, shrinkGoalBanner, updateGateMarker } from "./banners.js";
```

(This is not a new dependency direction — `banners.js`'s own imports are only
from `state.js`, `dom.js`, and `list.js`, so there is no cycle — it is one
more name on an edge that already exists.)

In `handleStatusUpdate`, which currently ends with:

```js
      renderSupervisorList();
      updatePauseResumeBtn(data.status);
      updateOverallProgress();
    }
  }
```

change to:

```js
      renderSupervisorList();
      updatePauseResumeBtn(data.status);
      updateOverallProgress();
      updateGateMarker(data.status, state.members);
    }
  }
```

In `handleProgress`, which currently ends with:

```js
      renderSupervisorList();
      updatePauseResumeBtn(data.status);
    }
  }
```

change to:

```js
      renderSupervisorList();
      updatePauseResumeBtn(data.status);
      updateGateMarker(data.status, state.members);
    }
  }
```

- [ ] **Step 10: Extend the existing 30s poller — no new interval**

In `web/assets/orchestrator/main.js`, add `loadMembers` to the existing import
from `./members.js`:

```js
import { loadMembers, membersNotice, openMembersPicker } from "./members.js";
```

The poller currently reads:

```js
    if (!state._refreshTimer) {
      state._refreshTimer = setInterval(() => {
        loadSupervisors();
        if (state.activeSupervisorId) {
          loadTasks();
        }
      }, 30000);
    }
```

Change to:

```js
    if (!state._refreshTimer) {
      state._refreshTimer = setInterval(() => {
        loadSupervisors();
        if (state.activeSupervisorId) {
          loadTasks();
          // Members have no SSE push of their own (unlike tasks, which
          // arrive over the stream) -- without this the gate marker's view
          // of who is "waiting" would only ever refresh when a orchestrator is
          // first opened or the add/remove picker is used.
          loadMembers(state.activeSupervisorId);
        }
      }, 30000);
    }
```

- [ ] **Step 11: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_frontend.py -v`
Expected: PASS, all tests including the new one.

- [ ] **Step 12: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: prior count + all new tests across every task in this plan, still
6 skips.

- [ ] **Step 13: Commit**

```bash
git status --porcelain -- web/assets/orchestrator/banners.js web/assets/orchestrator/dom.js web/assets/orchestrator/state.js web/assets/orchestrator/members.js web/assets/orchestrator/list.js web/assets/orchestrator/stream.js web/assets/orchestrator/main.js web/orchestrator.html tests/test_frontend.py
git add web/assets/orchestrator/banners.js web/assets/orchestrator/dom.js web/assets/orchestrator/state.js web/assets/orchestrator/members.js web/assets/orchestrator/list.js web/assets/orchestrator/stream.js web/assets/orchestrator/main.js web/orchestrator.html tests/test_frontend.py
git commit -m "feat: add a topbar marker for whatever a orchestrator is actually gated on"
```

---

## Final verification

- [ ] Run the full suite once more from a clean `git status` read:
  `.venv/bin/python -m pytest` — confirm the total pass count is the
  pre-plan count plus every test added across all nine tasks, and skips are
  still exactly 6.
- [ ] `git log --oneline -9` shows the nine commits from this plan, each
  scoped to the files its own task named — confirm none of them accidentally
  picked up a file from a concurrent session's unrelated edit (this is the
  shared-tree risk named in Global Constraints; re-check with
  `git show --stat <sha>` for any commit that looks larger than its task's
  file list).
- [ ] Manually open `/orchestrator.html` in a browser against a running
  instance, start a orchestrator with a multi-task plan that has real
  dependencies, and confirm: the rail appears above the task tree and its
  layers match the plan's dependency structure; pausing the orchestrator lights
  the topbar gate marker; clicking it un-maximizes the panels; adding a
  member whose linked session is mid-`AskUserQuestion` also lights the
  marker once `classify_chat` reports it as `"waiting"`.
