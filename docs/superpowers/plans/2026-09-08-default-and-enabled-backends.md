# Default and Enabled Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate "which backend is used when nothing else says otherwise" (the default) from "which backends may be used at all" (enabled/disabled), and show the difference in Settings → Backends.

**Architecture:** One additive column, `ai_machines.enabled INTEGER NOT NULL DEFAULT 1`. The existing `active` column keeps meaning *the default* — deliberately, since seven resolvers already read it. Disabling is refused while the backend is the default or has conversations pinned to it, and the refusal names the conversations. Every picker that lists backends gains an `enabled = 1` filter.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, vanilla ES modules (no build step), unittest via pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-default-and-enabled-backends-design.md` — read it before starting; it argues the decisions this plan only executes.

## Global Constraints

- **Shared git working tree.** Several Claude sessions commit here concurrently. Use targeted pathspecs on every `git add`. **Never** `git add -A`, `git add .`, or `git commit -a`. Never `git stash` (it moves other sessions' work).
- **Read `CLAUDE.md` in the repo root before touching anything.** It governs the turn/backend-routing path. This change is additive and must not alter turn mechanics.
- **Tests:** `.venv/bin/python -m pytest` invoked **bare**. Not `pytest tests/` (misses files), not plain `python` (skips the browser layer).
- **Never run `db.init()` against `data/webconsole.db`.** It migrates. Copy via the SQLite backup API to a throwaway `WC_DB_PATH` (CLAUDE.md rule 9).
- **A full single-process `pytest` run gets OOM-killed on this box** (3.8 GB, live server resident). For a whole-suite reading use `bin/run-suite-chunked.sh`. Per-task runs are single files and are fine.
- The user-facing vocabulary is **Default** / **Active** / **Inactive**. The database column is `active` (meaning default) plus new `enabled` (meaning usable). Do not rename `active`.
- Refusals name the dependent conversations, never just a count. Cap the list at **8** titles with a `(N more)` tail.
- `enabled` is a manual flag. It says nothing about reachability — transport Check answers that, and the two stay separate.

---

### Task 1: The `enabled` column and reading it back

**Files:**
- Modify: `db.py:1121` region (inside `_ensure_chat_columns`, after the `transport_id` ALTER)
- Modify: `routes/db_machines.py:24` (`ai_machine_active`), `:34` (`ai_machines_list`), `:49` (`ai_machine_get`)
- Test: `tests/test_qa_backend_enabled_column.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `ai_machines.enabled` column; `enabled` key (int 0/1) on the dicts returned by `db.ai_machine_active`, `db.ai_machines_list`, `db.ai_machine_get`.

- [ ] **Step 1: Check the tree before touching it**

```bash
cd /home/kali/projects/claude-code-webconsole
git status --porcelain | grep -v '^??'
git log --oneline -3
```

Expected: other sessions' modified files may be listed. Note them — you must not `git add` any file that is not in your own task's list. If a file *you* need to modify is already modified by someone else, stop and report it rather than editing over them.

- [ ] **Step 2: Write the failing test**

Create `tests/test_qa_backend_enabled_column.py`:

```python
"""QA: the enabled column, and that an old database survives gaining it.

The migration is additive with DEFAULT 1 so an existing backend stays usable.
The column-less case is the one that decides whether a mid-work host survives
the deploy, which is why it is asserted rather than assumed.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wc-enabled-")) / "wc.db"
    os.environ["WC_DB_PATH"] = str(tmp)
    os.environ.setdefault("WC_PROXY_ENABLED", "0")
    import config

    importlib.reload(config)
    import db

    db.config = config
    return tmp


class EnabledColumnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = _fresh_db()
        import db

        self.db = db
        await db.init()

    async def asyncTearDown(self):
        await self.db.close()

    def _columns(self) -> set[str]:
        con = sqlite3.connect(str(self.path))
        try:
            return {r[1] for r in con.execute("PRAGMA table_info(ai_machines)")}
        finally:
            con.close()

    async def test_the_column_exists_after_init(self):
        self.assertIn("enabled", self._columns())

    async def test_init_is_idempotent(self):
        await self.db.close()
        await self.db.init()
        self.assertIn("enabled", self._columns())

    async def test_a_database_without_the_column_gains_it_enabled(self):
        """The deploy-safety case: an existing backend must stay usable."""
        await self.db.close()
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("ALTER TABLE ai_machines DROP COLUMN enabled")
            con.execute(
                "INSERT INTO ai_machines "
                "(id, name, provider, host, port, model, owner_id, active, "
                " created_at, updated_at) "
                "VALUES ('m-old','Old','claude_code','api.anthropic.com',443,"
                "'claude-opus-5','admin',1,'2026-01-01T00:00:00Z',"
                "'2026-01-01T00:00:00Z')"
            )
            con.commit()
        finally:
            con.close()
        await self.db.init()
        self.assertIn("enabled", self._columns())
        machine = await self.db.ai_machine_get("m-old", "admin")
        self.assertEqual(machine["enabled"], 1,
                         "an existing backend must not be shelved by the migration")

    async def test_the_readers_all_return_enabled(self):
        await self.db.ai_machine_create(
            "m-1", "One", "api.anthropic.com", 443, None, "claude-opus-5",
            "https://api.anthropic.com", None, "admin", provider="claude_code",
        )
        await self.db.ai_machine_activate("m-1", "admin")
        self.assertIn("enabled", await self.db.ai_machine_get("m-1", "admin"))
        self.assertIn("enabled", (await self.db.ai_machines_list("admin"))[0])
        self.assertIn("enabled", await self.db.ai_machine_active("admin"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to confirm it fails**

```bash
cd /home/kali/projects/claude-code-webconsole
.venv/bin/python -m pytest tests/test_qa_backend_enabled_column.py -v
```

Expected: FAIL — `AssertionError: 'enabled' not found in {...}` on `test_the_column_exists_after_init`.

- [ ] **Step 4: Add the migration**

In `db.py`, find the `transport_id` block near line 1121:

```python
    if ma_columns and "transport_id" not in ma_columns:
```

Immediately **after** that block's `await db_conn.execute(...)` call, add:

```python
    # Whether a backend may be used at all -- distinct from `active`, which on
    # this table means "is the default". DEFAULT 1 is what makes this additive
    # migration safe on a live host: every backend that already exists stays
    # usable, and a database predating the column behaves exactly as before.
    #
    # The two names disagree with the interface's vocabulary (Default /
    # Active / Inactive) on purpose -- renaming `active` would flip the
    # meaning of a column seven resolvers read, and any resolver missed would
    # see active=1 everywhere and silently pick an arbitrary backend. See the
    # spec's "Naming" section.
    if ma_columns and "enabled" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
```

Note the `ma_columns and` guard: it matches every sibling ALTER in this function and skips the branch when `PRAGMA table_info` returned nothing (a database with no such table yet — the CREATE TABLE already includes the column in that case).

- [ ] **Step 5: Add `enabled` to the CREATE TABLE**

In `db.py`, find `CREATE TABLE IF NOT EXISTS ai_machines` and add to the column list, after `active`:

```sql
            enabled      INTEGER NOT NULL DEFAULT 1,
```

- [ ] **Step 6: Add `enabled` to the three read paths**

In `routes/db_machines.py`, add `enabled` to each SELECT list:

`ai_machine_active` (line ~24):
```python
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, active, enabled "
```

`ai_machines_list` (line ~34):
```python
        "SELECT id, name, provider, host, port, model, active_models, base_url, description, "
        "CASE WHEN active = 1 THEN 1 ELSE 0 END AS active, "
        "CASE WHEN enabled = 1 THEN 1 ELSE 0 END AS enabled, "
        "created_at, updated_at, transport_id "
```

`ai_machine_get` (line ~49): add after the `active` CASE line:
```python
        "CASE WHEN enabled = 1 THEN 1 ELSE 0 END AS enabled, "
```

- [ ] **Step 7: Run the test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_qa_backend_enabled_column.py -v
```

Expected: PASS, 4 tests.

- [ ] **Step 8: Run the neighbouring suites for regressions**

```bash
.venv/bin/python -m pytest tests/test_machine_provider.py tests/test_qa_active_models.py tests/test_qa_machine_activate_backend.py -q
```

Expected: no *new* failures. `test_machine_provider.py` may already fail on unrelated pre-existing issues — compare against a run on `HEAD` before your changes if unsure, and report rather than "fixing" someone else's failure.

- [ ] **Step 9: Verify the migration against a copy of production**

```bash
.venv/bin/python - <<'PY'
import asyncio, os, sqlite3, tempfile, importlib
from pathlib import Path
src = "data/webconsole.db"
tmp = Path(tempfile.mkdtemp(prefix="wc-verify-")) / "wc.db"
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(str(tmp))
s.backup(d); s.close(); d.close()
os.environ["WC_DB_PATH"] = str(tmp); os.environ["WC_PROXY_ENABLED"] = "0"
import config; importlib.reload(config)
import db; db.config = config
async def main():
    await db.init()
    for m in await db.ai_machines_list("admin"):
        print(f"  {m['name']:<45} active={m['active']} enabled={m['enabled']}")
    await db.close()
asyncio.run(main())
PY
```

Expected: every existing backend prints `enabled=1`. The production database is opened **read-only** and copied; `db.init()` runs only against the copy.

- [ ] **Step 10: Commit**

```bash
git add db.py routes/db_machines.py tests/test_qa_backend_enabled_column.py
git commit -m "feat: add ai_machines.enabled, distinct from active-as-default"
```

---

### Task 2: Which conversations pin a backend

**Files:**
- Modify: `routes/db_chats.py` (append a new function)
- Modify: `db.py` — add `"chats_pinned_to_machine": "routes.db_chats"` to the `__getattr__` dispatch table (the `# chats` block, near line 67)
- Test: `tests/test_qa_chats_pinned_to_machine.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `db.chats_pinned_to_machine(machine_id: str, owner_id: str, limit: int = 8) -> dict` returning `{"total": int, "titles": list[str], "ids": list[str]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qa_chats_pinned_to_machine.py`:

```python
"""QA: naming the conversations that pin a backend.

A refusal that says "8 conversations are pinned" sends the reader hunting
through the sidebar. Naming them is the whole value, so the shape of this
helper is asserted rather than left to the caller.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path


class PinnedChatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-pins-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()
        await db.ai_machine_create(
            "m-1", "Gateway", "gw.example.com", 443, None, "vllm/x",
            "https://gw.example.com", None, "admin", provider="claude_code",
        )

    async def asyncTearDown(self):
        await self.db.close()

    async def _chat(self, chat_id: str, title: str, machine: str | None):
        # chat_set_machine's real signature is (chat_id, owner_id, machine_id)
        # -- owner second, machine third. Transposing them silently pins the
        # chat to a machine id of "admin", which resolves to nothing and makes
        # every assertion below pass for the wrong reason.
        await self.db.chat_create(chat_id, title, None, "/tmp", "admin")
        if machine:
            await self.db.chat_set_machine(chat_id, "admin", machine)

    async def test_no_pins_reports_zero(self):
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["titles"], [])

    async def test_pins_are_named(self):
        await self._chat("c1", "cweb2", "m-1")
        await self._chat("c2", "voice test", "m-1")
        await self._chat("c3", "unpinned", None)
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 2)
        self.assertEqual(set(result["titles"]), {"cweb2", "voice test"})
        self.assertEqual(set(result["ids"]), {"c1", "c2"})

    async def test_the_list_is_capped_but_the_total_is_not(self):
        """40 pins must not produce a 40-line error message."""
        for i in range(12):
            await self._chat(f"c{i}", f"chat {i}", "m-1")
        result = await self.db.chats_pinned_to_machine("m-1", "admin", limit=8)
        self.assertEqual(result["total"], 12)
        self.assertEqual(len(result["titles"]), 8)

    async def test_it_is_scoped_by_owner(self):
        await self._chat("c1", "mine", "m-1")
        result = await self.db.chats_pinned_to_machine("m-1", "someone-else")
        self.assertEqual(result["total"], 0)

    async def test_an_archived_chat_still_counts(self):
        """Archived is not deleted: it can be restored and would then be
        pinned to a backend that had been shelved underneath it."""
        await self._chat("c1", "archived one", "m-1")
        await self.db.chat_archive("c1", "admin", True)
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_qa_chats_pinned_to_machine.py -v
```

Expected: FAIL — `AttributeError: module 'db' has no attribute 'chats_pinned_to_machine'`.

- [ ] **Step 3: Add the helper**

Append to `routes/db_chats.py`:

```python
async def chats_pinned_to_machine(
    machine_id: str, owner_id: str, limit: int = 8
) -> dict[str, Any]:
    """Conversations pinned to *machine_id*, named rather than counted.

    `total` is the real count; `titles` is capped at *limit* so a refusal
    message stays readable when a popular backend has forty. A count alone
    sends the reader hunting through the sidebar, which is the hunt the
    refusal exists to save them.

    Archived conversations count. Archived is not deleted -- it can be
    restored, and it would then be pinned to a backend shelved underneath it.

    Deleted ones do not: `deleted_at IS NOT NULL` is a tombstone and those
    conversations are not coming back.
    """
    cur = await db.db_conn.execute(
        "SELECT COUNT(*) AS n FROM chats "
        "WHERE ai_machine_id = ? AND owner_id = ? AND deleted_at IS NULL",
        (machine_id, owner_id),
    )
    total = (await cur.fetchone())["n"]
    cur = await db.db_conn.execute(
        "SELECT id, title FROM chats "
        "WHERE ai_machine_id = ? AND owner_id = ? AND deleted_at IS NULL "
        "ORDER BY updated_at DESC LIMIT ?",
        (machine_id, owner_id, int(limit)),
    )
    rows = await cur.fetchall()
    return {
        "total": total,
        "titles": [r["title"] for r in rows],
        "ids": [r["id"] for r in rows],
    }
```

If `Any` is not already imported in that file, add `from typing import Any` to its imports.

- [ ] **Step 4: Register it in the dispatch table**

In `db.py`, in the `# chats` block of `__getattr__`'s `_SYMBOLS` dict (near line 67), add:

```python
        "chats_pinned_to_machine": "routes.db_chats",
```

This is required — `db` resolves its surface through that table, and a missing entry is an `AttributeError` at call time rather than import time. `tests/test_qa_db_dispatch_names.py` guards this.

- [ ] **Step 5: Run the test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_qa_chats_pinned_to_machine.py tests/test_qa_db_dispatch_names.py -v
```

Expected: PASS, 5 + 2 tests.

- [ ] **Step 6: Commit**

```bash
git add routes/db_chats.py db.py tests/test_qa_chats_pinned_to_machine.py
git commit -m "feat: name the conversations pinned to a backend"
```

---

### Task 3: Setting `enabled`, and refusing to smuggle a disabled backend in as default

**Files:**
- Modify: `routes/db_machines.py` (append `ai_machine_set_enabled`; guard `ai_machine_activate` at line ~163)
- Modify: `db.py` — dispatch entry `"ai_machine_set_enabled": "routes.db_machines"` in the `# machines` block (near line 104)
- Test: `tests/test_qa_backend_set_enabled.py` (create)

**Interfaces:**
- Consumes: `ai_machines.enabled` from Task 1.
- Produces: `db.ai_machine_set_enabled(machine_id: str, owner_id: str, enabled: bool) -> bool` (True when a row changed). `db.ai_machine_activate` now returns `False` for a disabled machine instead of activating it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qa_backend_set_enabled.py`:

```python
"""QA: enabling and disabling a backend at the database layer.

The activate guard is the load-bearing one: without it "make default" is a
way to smuggle a shelved backend back into service, and it would do so
silently -- the flag says disabled while every turn routes there.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path


class SetEnabledTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-setenabled-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()
        for mid, name in (("m-1", "One"), ("m-2", "Two")):
            await db.ai_machine_create(
                mid, name, "api.anthropic.com", 443, None, "claude-opus-5",
                "https://api.anthropic.com", None, "admin",
                provider="claude_code",
            )

    async def asyncTearDown(self):
        await self.db.close()

    async def test_disabling_sets_the_flag(self):
        self.assertTrue(await self.db.ai_machine_set_enabled("m-1", "admin", False))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["enabled"], 0)

    async def test_enabling_sets_it_back(self):
        await self.db.ai_machine_set_enabled("m-1", "admin", False)
        self.assertTrue(await self.db.ai_machine_set_enabled("m-1", "admin", True))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["enabled"], 1)

    async def test_it_is_scoped_by_owner(self):
        self.assertFalse(
            await self.db.ai_machine_set_enabled("m-1", "someone-else", False))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["enabled"], 1)

    async def test_activate_refuses_a_disabled_machine(self):
        """The guard: 'make default' must not re-enable by the back door."""
        await self.db.ai_machine_set_enabled("m-1", "admin", False)
        self.assertFalse(await self.db.ai_machine_activate("m-1", "admin"))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["active"], 0)

    async def test_a_refused_activate_leaves_the_previous_default_alone(self):
        """activate() deactivates everything before activating one. A refusal
        that ran the first UPDATE would leave the owner with no default at
        all -- worse than the state it refused to leave."""
        await self.db.ai_machine_activate("m-2", "admin")
        await self.db.ai_machine_set_enabled("m-1", "admin", False)
        self.assertFalse(await self.db.ai_machine_activate("m-1", "admin"))
        self.assertEqual((await self.db.ai_machine_get("m-2", "admin"))["active"], 1)

    async def test_activate_still_works_for_an_enabled_machine(self):
        self.assertTrue(await self.db.ai_machine_activate("m-1", "admin"))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["active"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_qa_backend_set_enabled.py -v
```

Expected: FAIL — `AttributeError: module 'db' has no attribute 'ai_machine_set_enabled'`.

- [ ] **Step 3: Add the setter**

Append to `routes/db_machines.py`:

```python
async def ai_machine_set_enabled(
    machine_id: str, owner_id: str, enabled: bool
) -> bool:
    """Shelve a backend, or bring it back. True when a row changed.

    Its own function rather than a field on ai_machine_update: that builder
    only writes a field when the value is not None, so a bare False would be
    written but the intent reads ambiguously beside eight text fields, and
    the callers that must refuse (see routes/machines.handle_machine_patch)
    need a single obvious entry point to guard.
    """
    cur = await db.db_conn.execute(
        "UPDATE ai_machines SET enabled = ?, updated_at = ? "
        "WHERE id = ? AND owner_id = ?",
        (1 if enabled else 0, db._now(), machine_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 4: Guard `ai_machine_activate`**

In `routes/db_machines.py`, at the top of `ai_machine_activate`'s `try:` block (line ~173), **before** the first UPDATE:

```python
    # Refuse before touching anything. A disabled backend must not become the
    # default: that would route every unpinned turn to a backend the operator
    # shelved, while the flag still said disabled.
    #
    # Checked before the deactivate-everything UPDATE below, not after: a
    # refusal that had already run that statement would leave the owner with
    # no default at all, which is worse than the state it declined to leave.
    cur = await db.db_conn.execute(
        "SELECT enabled FROM ai_machines WHERE id = ? AND owner_id = ?",
        (machine_id, owner_id),
    )
    row = await cur.fetchone()
    if not row or not row["enabled"]:
        return False
```

- [ ] **Step 5: Register the setter in the dispatch table**

In `db.py`, in the `# machines` block of `_SYMBOLS` (near line 104), add:

```python
        "ai_machine_set_enabled": "routes.db_machines",
```

- [ ] **Step 6: Run the tests to confirm they pass**

```bash
.venv/bin/python -m pytest tests/test_qa_backend_set_enabled.py tests/test_qa_db_dispatch_names.py -v
```

Expected: PASS, 6 + 2 tests.

- [ ] **Step 7: Check the activate guard broke nothing**

```bash
.venv/bin/python -m pytest tests/test_qa_machine_activate_backend.py tests/test_qa_model_backend.py -q
```

Expected: no new failures. Existing machines all have `enabled = 1`, so the guard is transparent to every current test.

- [ ] **Step 8: Commit**

```bash
git add routes/db_machines.py db.py tests/test_qa_backend_set_enabled.py
git commit -m "feat: ai_machine_set_enabled, and refuse to default a disabled backend"
```

---

### Task 4: The PATCH endpoint and its 409

**Files:**
- Modify: `routes/machines.py:46` (`_MACHINE_ALLOWED_FIELDS`), `:218` (`handle_machine_patch`)
- Test: `tests/test_qa_backend_disable_route.py` (create)

**Interfaces:**
- Consumes: `db.ai_machine_set_enabled` (Task 3), `db.chats_pinned_to_machine` (Task 2).
- Produces: `PATCH /api/machines/{id}` accepting `{"enabled": bool}`; 409 body `{"error": str, "is_default": bool, "pinned_chats": [{"id": str, "title": str}], "pinned_total": int}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qa_backend_disable_route.py`:

```python
"""QA: disabling a backend is refused while anything depends on it.

The refusal names the conversations. A count would send the reader hunting,
and the alternative design -- letting pinned chats fall back to the default --
was rejected in the spec because those chats serve disjoint model ids and
would start answering 429 "No deployments available for selected model",
which reads as capacity and is really routing (CLAUDE.md 0.1).
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import machines as mr


class _Request:
    def __init__(self, body: dict, user: str = "admin"):
        self._body = body
        self.state = type("S", (), {"session": {"user": user}})()

    async def json(self):
        return self._body


_MACHINE = {"id": "m-1", "name": "Gateway", "provider": "claude_code",
            "host": "gw.example.com", "port": 443, "active": 0, "enabled": 1}


def _body(response) -> dict:
    return json.loads(bytes(response.body))


def _patched(machine, pins, set_enabled=None):
    """Patch the three db calls the handler makes."""
    return (
        patch.object(mr.db, "ai_machine_get", AsyncMock(return_value=machine)),
        patch.object(mr.db, "chats_pinned_to_machine", AsyncMock(return_value=pins)),
        patch.object(mr.db, "ai_machine_set_enabled",
                     set_enabled or AsyncMock(return_value=True)),
    )


_NO_PINS = {"total": 0, "titles": [], "ids": []}


class DisableIsAllowedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_plain_backend_can_be_disabled(self):
        setter = AsyncMock(return_value=True)
        a, b, c = _patched(dict(_MACHINE), _NO_PINS, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        self.assertEqual(response.status_code, 200)
        setter.assert_awaited_once_with("m-1", "admin", False)

    async def test_enabling_is_never_refused(self):
        """A disabled, pinned, formerly-default backend can always come back."""
        setter = AsyncMock(return_value=True)
        machine = {**_MACHINE, "enabled": 0, "active": 1}
        pins = {"total": 8, "titles": ["a"] * 8, "ids": ["x"] * 8}
        a, b, c = _patched(machine, pins, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": True}), "m-1")
        self.assertEqual(response.status_code, 200)
        setter.assert_awaited_once_with("m-1", "admin", True)


class DisableIsRefusedTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_default_cannot_be_disabled(self):
        setter = AsyncMock()
        a, b, c = _patched({**_MACHINE, "active": 1}, _NO_PINS, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(_body(response)["is_default"])
        setter.assert_not_awaited()

    async def test_pinned_conversations_are_named_not_counted(self):
        pins = {"total": 3, "titles": ["cweb2", "voice test", "kali3 test"],
                "ids": ["c1", "c2", "c3"]}
        setter = AsyncMock()
        a, b, c = _patched(dict(_MACHINE), pins, setter)
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        self.assertEqual(response.status_code, 409)
        body = _body(response)
        self.assertEqual(body["pinned_total"], 3)
        self.assertEqual([c["title"] for c in body["pinned_chats"]],
                         ["cweb2", "voice test", "kali3 test"])
        setter.assert_not_awaited()

    async def test_the_message_reports_the_full_total_not_the_capped_list(self):
        pins = {"total": 40, "titles": [f"chat {i}" for i in range(8)],
                "ids": [f"c{i}" for i in range(8)]}
        a, b, c = _patched(dict(_MACHINE), pins, AsyncMock())
        with a, b, c:
            response = await mr.handle_machine_patch(
                _Request({"enabled": False}), "m-1")
        body = _body(response)
        self.assertEqual(body["pinned_total"], 40)
        self.assertEqual(len(body["pinned_chats"]), 8)
        self.assertIn("40", body["error"])

    async def test_an_unknown_machine_is_404_not_409(self):
        a, b, c = _patched(None, _NO_PINS, AsyncMock())
        with a, b, c:
            with self.assertRaises(HTTPException) as caught:
                await mr.handle_machine_patch(_Request({"enabled": False}), "m-1")
        self.assertEqual(caught.exception.status_code, 404)


class FieldValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_must_be_a_boolean(self):
        a, b, c = _patched(dict(_MACHINE), _NO_PINS, AsyncMock())
        with a, b, c:
            with self.assertRaises(HTTPException) as caught:
                await mr.handle_machine_patch(
                    _Request({"enabled": "yes"}), "m-1")
        self.assertEqual(caught.exception.status_code, 400)

    async def test_enabled_is_an_accepted_field(self):
        """It must be in _MACHINE_ALLOWED_FIELDS or the handler 400s before
        ever reaching the logic above."""
        self.assertIn("enabled", mr._MACHINE_ALLOWED_FIELDS)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_qa_backend_disable_route.py -v
```

Expected: FAIL — `test_enabled_is_an_accepted_field` fails on the missing field, and the others 400 with "No valid fields to update".

- [ ] **Step 3: Allow the field**

In `routes/machines.py`, add to `_MACHINE_ALLOWED_FIELDS` (line 46):

```python
    "enabled",
```

- [ ] **Step 4: Handle `enabled` in the PATCH handler**

In `handle_machine_patch`, immediately after the `_MACHINE_TEXT_FIELDS` type-check loop (before the port validation), insert:

```python
    # `enabled` is handled here and removed from `data`, because it has its own
    # setter and its own refusal rules -- it is not one of the plain columns
    # ai_machine_update writes.
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise HTTPException(
                status_code=400, detail="enabled must be true or false")
        wanted = data.pop("enabled")
        machine = await db.ai_machine_get(machine_id, session["user"])
        if not machine:
            raise HTTPException(status_code=404, detail="Machine not found")

        # Enabling is never refused: a backend coming back into service breaks
        # nothing, whatever depends on it.
        if not wanted:
            pins = await db.chats_pinned_to_machine(machine_id, session["user"])
            is_default = bool(machine.get("active"))
            if is_default or pins["total"]:
                # Named, not counted. The list is what makes this actionable;
                # "8 conversations are pinned" sends the reader hunting through
                # the sidebar for which eight.
                parts = []
                if is_default:
                    parts.append("it is the default backend")
                if pins["total"]:
                    shown = " · ".join(pins["titles"])
                    more = pins["total"] - len(pins["titles"])
                    if more > 0:
                        shown += f" · … ({more} more)"
                    parts.append(
                        f"{pins['total']} conversation(s) are pinned to it: {shown}")
                _log.info(
                    "ai_machine disable refused user=%s id=%s default=%s pinned=%d",
                    session["user"], machine_id, is_default, pins["total"],
                )
                return JSONResponse(
                    {
                        "error": f"Cannot disable {machine['name']} — "
                                 + "; ".join(parts)
                                 + ". Repoint them, or make another backend "
                                   "the default first.",
                        "is_default": is_default,
                        "pinned_total": pins["total"],
                        "pinned_chats": [
                            {"id": i, "title": t}
                            for i, t in zip(pins["ids"], pins["titles"])
                        ],
                    },
                    status_code=409,
                )

        await db.ai_machine_set_enabled(machine_id, session["user"], wanted)
        _log.info(
            "ai_machine %s user=%s id=%s",
            "enabled" if wanted else "disabled", session["user"], machine_id,
        )
        # A request that carried only `enabled` is complete; anything else in
        # `data` falls through to the normal update path below.
        if not data:
            return JSONResponse({"ok": True, "enabled": wanted})
```

- [ ] **Step 5: Run the test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_qa_backend_disable_route.py -v
```

Expected: PASS, 8 tests.

- [ ] **Step 6: Check the route surface is still single-registered**

```bash
.venv/bin/python -m pytest tests/test_qa_duplicate_routes.py tests/test_qa_coverage.py -q
```

Expected: no new failures. `test_qa_coverage.py` has pre-existing failures on this tree — compare, don't fix.

- [ ] **Step 7: Commit**

```bash
git add routes/machines.py tests/test_qa_backend_disable_route.py
git commit -m "feat: PATCH enabled, refusing while a backend is depended on"
```

---

### Task 5: Every picker filters disabled backends out

**Files:**
- Modify: `routes/misc.py` — `handle_settings_get`'s machine SELECT (the `voice_backend_options` query)
- Modify: `bin/wc-backend-env.py:67` (`machine_for`) — the active branch at line ~135 and the profile branch at ~112
- Test: `tests/test_qa_disabled_backend_not_offered.py` (create)

**Interfaces:**
- Consumes: `ai_machines.enabled` (Task 1).
- Produces: no new symbols. `machine_for()` returns `{}` rather than a disabled machine; `--profile <disabled>` exits 2.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qa_disabled_backend_not_offered.py`:

```python
"""QA: a shelved backend is not offered anywhere.

machine_for() is the load-bearing one. It is the terminal path: every
interactive `claude` on this host goes through bin/wc-claude.sh, which asks
bin/wc-backend-env.py which backend to use. If that returns a disabled
machine, the wrapper launches a session against a backend the operator
shelved -- silently, and against possibly the wrong endpoint. That is the
exact class of failure the wrapper exists to prevent (CLAUDE.md 0.1).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "wc-backend-env.py"


def _db_with(machines: list[tuple[str, str, int, int]]) -> Path:
    """machines: (id, name, active, enabled)."""
    path = Path(tempfile.mkdtemp(prefix="wc-offered-")) / "wc.db"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE ai_machines (id TEXT PRIMARY KEY, name TEXT, provider TEXT, "
        "host TEXT, port INTEGER, api_key TEXT, model TEXT, base_url TEXT, "
        "description TEXT, active INTEGER, enabled INTEGER NOT NULL DEFAULT 1, "
        "owner_id TEXT, created_at TEXT, updated_at TEXT, active_models TEXT)"
    )
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    for mid, name, active, enabled in machines:
        con.execute(
            "INSERT INTO ai_machines (id,name,provider,host,port,api_key,model,"
            "base_url,description,active,enabled,owner_id,created_at,updated_at,"
            "active_models) VALUES (?,?,'claude_code','api.anthropic.com',443,"
            "NULL,'claude-opus-5','https://api.anthropic.com',NULL,?,?,'admin',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','[]')",
            (mid, name, active, enabled),
        )
    con.commit()
    con.close()
    return path


def _run(db: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"WC_DB_PATH": str(db), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )


class MachineForTests(unittest.TestCase):
    def test_a_disabled_active_machine_is_not_returned(self):
        """The terminal path. A disabled backend that is still flagged active
        must not reach bin/wc-claude.sh."""
        sys.path.insert(0, str(ROOT))
        import importlib.util

        spec = importlib.util.spec_from_file_location("wcbe", HELPER)
        wcbe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wcbe)
        db = _db_with([("m-1", "Shelved", 1, 0)])
        self.assertEqual(wcbe.machine_for(db), {},
                         "a disabled backend must never be resolved as active")

    def test_an_enabled_active_machine_is_returned(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("wcbe2", HELPER)
        wcbe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wcbe)
        db = _db_with([("m-1", "Live", 1, 1)])
        self.assertEqual(wcbe.machine_for(db).get("name"), "Live")

    def test_naming_a_disabled_profile_is_refused(self):
        db = _db_with([("m-1", "Shelved", 0, 0), ("m-2", "Live", 1, 1)])
        result = _run(db, "--profile", "shelved", "--fields")
        self.assertNotEqual(result.returncode, 0,
                            "a disabled profile must not resolve")
        self.assertIn("live", (result.stderr + result.stdout).lower(),
                      "the refusal should list the profiles that do work")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_qa_disabled_backend_not_offered.py -v
```

Expected: FAIL — `machine_for` returns the shelved machine instead of `{}`.

- [ ] **Step 3: Filter in `machine_for`**

In `bin/wc-backend-env.py`, `machine_for()`, replace the owner-scoping block's trailing lines so disabled rows drop out before either branch. After the existing owner filter:

```python
    if owner:
        rows = [r for r in rows if str(r.get("owner_id") or "").strip() == owner]
```

add:

```python
    # A shelved backend is offered to nobody, including a terminal.
    #
    # This is the terminal path and the reason the filter lives here rather
    # than only in the console: every interactive `claude` on this host runs
    # through bin/wc-claude.sh, which asks this helper which backend to use.
    # Returning a disabled machine would launch a session against a backend
    # the operator shelved -- silently, and possibly against the wrong
    # endpoint, which is the failure CLAUDE.md 0.1 exists to prevent.
    #
    # `.get("enabled", 1)` defaults to enabled: this helper deliberately does
    # SELECT * so it keeps working against a database that predates a column
    # (the same reason active_models is read through .get()).
    rows = [r for r in rows if int(r.get("enabled", 1) or 0) == 1]
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_qa_disabled_backend_not_offered.py -v
```

Expected: PASS, 3 tests. The profile refusal passes for free — a disabled row is no longer in `rows`, so `profile_slug` never matches it and the existing "no backend named X. Known profiles: ..." path fires.

- [ ] **Step 5: Filter the voice backend picker**

In `routes/misc.py`, `handle_settings_get`, the machine query currently reads:

```python
        "SELECT id, name, provider, active_models FROM ai_machines "
        "WHERE owner_id = ? ORDER BY active DESC, name ASC",
```

Change the WHERE clause:

```python
        "SELECT id, name, provider, active_models FROM ai_machines "
        "WHERE owner_id = ? AND enabled = 1 ORDER BY active DESC, name ASC",
```

- [ ] **Step 6: Confirm the voice picker still works and excludes disabled**

```bash
.venv/bin/python -m pytest tests/test_qa_voice_model_per_backend.py tests/test_voice_turn.py -q
```

Expected: `test_qa_voice_model_per_backend.py` passes (its fixture stubs the query, so it is unaffected). `test_voice_turn.py` has three pre-existing failures on this tree — compare, don't fix.

- [ ] **Step 7: Verify the wrapper end to end against a copy**

```bash
.venv/bin/python - <<'PY'
import sqlite3, tempfile
from pathlib import Path
src = "data/webconsole.db"
tmp = Path(tempfile.mkdtemp(prefix="wc-wrap-")) / "wc.db"
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(str(tmp))
s.backup(d); s.close(); d.close()
con = sqlite3.connect(str(tmp))
con.execute("ALTER TABLE ai_machines ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
con.execute("UPDATE ai_machines SET enabled = 0 WHERE active = 1")
con.commit(); con.close()
print("throwaway db with the active backend shelved:", tmp)
PY
```

Then, with the path it printed:

```bash
WC_DB_PATH=<that path> .venv/bin/python bin/wc-backend-env.py --json
```

Expected: it reports no active machine rather than the shelved one. Then:

```bash
WC_DB_PATH=<that path> WC_CLAUDE_DRY_RUN=1 bash bin/wc-claude.sh
```

Expected: `wc-claude: no active machine in WebConsole — starting claude unchanged`. It must **not** print the shelved backend's name.

- [ ] **Step 8: Commit**

```bash
git add routes/misc.py bin/wc-backend-env.py tests/test_qa_disabled_backend_not_offered.py
git commit -m "feat: disabled backends are offered to no picker, including terminals"
```

---

### Task 6: Three card states in Settings → Backends

**Files:**
- Modify: `web/assets/machines.js:362-432` (`_buildMachineCard`), and its `./app.js?v=NN` import version
- Modify: `web/index.html` (styling for the inactive card and the badges)
- Test: `tests/test_frontend.py` (append assertions)

**Interfaces:**
- Consumes: `enabled` on the `/api/machines` payload (Task 1); `PATCH {"enabled": bool}` and its 409 (Task 4).
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_frontend.py` (match the file's existing class style; if it uses a source-reading fixture, reuse it rather than adding a second reader):

```python
class BackendEnabledCardTests(unittest.TestCase):
    """The three states must be distinguishable in the source.

    Asserted on the source rather than a rendered DOM because this panel has
    no browser test that opens it with a disabled backend, and the states are
    what the whole change is for.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.machines_js = (root / "web/assets/machines.js").read_text()
        cls.index_html = (root / "web/index.html").read_text()

    def test_the_default_card_says_default(self):
        self.assertIn("'Default'", self.machines_js)

    def test_a_disabled_card_is_marked_inactive(self):
        self.assertIn("machine-disabled", self.machines_js)
        self.assertIn("'Inactive'", self.machines_js)

    def test_disable_and_enable_are_both_offered(self):
        self.assertIn("'Disable'", self.machines_js)
        self.assertIn("'Enable'", self.machines_js)

    def test_make_default_replaces_activate(self):
        self.assertIn("'Make default'", self.machines_js)

    def test_the_disabled_state_is_styled(self):
        self.assertIn(".machine-disabled", self.index_html)

    def test_the_disable_button_carries_its_obstacle(self):
        """A button that will 409 should say so before it is pressed."""
        self.assertIn("_disableObstacle", self.machines_js)
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_frontend.py -k BackendEnabledCard -v
```

Expected: FAIL on every assertion.

- [ ] **Step 3: Rewrite the card's state label**

In `web/assets/machines.js`, `_buildMachineCard`, replace the `state` block (lines ~365-375):

```python
    if (m.active) card.classList.add('machine-active');
    if (!m.enabled) card.classList.add('machine-disabled');

    const top = document.createElement('div');
    top.className = 'machine-card-top';

    // Three states, said in words rather than implied by a border. `active`
    // in the payload means "is the default" -- the column keeps that meaning
    // deliberately (see the spec's Naming section); only the label changed.
    const state = document.createElement('span');
    if (!m.enabled) {
      state.className = 'machine-state machine-state-off';
      state.textContent = 'Inactive';
    } else if (m.active) {
      state.className = 'machine-state machine-state-live';
      state.textContent = 'Default';
    } else {
      state.className = 'machine-state';
      state.textContent = 'Active';
    }
    top.appendChild(state);
```

- [ ] **Step 4: Add the obstacle helper and rewrite the buttons**

Above `_buildMachineCard` in `machines.js`, add:

```javascript
// Why Disable would be refused, or '' when it would succeed.
//
// The server refuses with 409 either way; this only lets the button say so
// before it is pressed. The 409 path still exists for the race where a
// conversation is pinned between render and click.
function _disableObstacle(m) {
  if (m.active) return 'This is the default backend — make another the default first.';
  const pinned = (m.pinned_chats_count || 0);
  if (pinned) return `${pinned} conversation(s) are pinned to this backend.`;
  return '';
}
```

Then in `_buildMachineCard`, replace the `if (!m.active) { ... activateBtn ... }` block (lines ~412-420) with:

```javascript
    if (m.enabled && !m.active) {
      const defaultBtn = document.createElement('button');
      defaultBtn.type = 'button';
      defaultBtn.className = 'machine-action';
      defaultBtn.textContent = 'Make default';
      defaultBtn.addEventListener('click', () => _activateMachine(m.id));
      actions.appendChild(defaultBtn);
    }

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'machine-action';
    toggleBtn.textContent = m.enabled ? 'Disable' : 'Enable';
    if (m.enabled) {
      const obstacle = _disableObstacle(m);
      if (obstacle) {
        toggleBtn.disabled = true;
        toggleBtn.title = obstacle;
      }
    }
    toggleBtn.addEventListener('click', () => _setMachineEnabled(m, !m.enabled));
    actions.appendChild(toggleBtn);
```

- [ ] **Step 5: Add the toggle handler**

Append to `machines.js`, beside `_activateMachine`:

```javascript
/** Enable or disable a backend, surfacing the server's refusal in full.
 *
 * The 409 body carries the pinned conversations by name. Rendering them is
 * the point: "8 conversations are pinned" sends the reader hunting through
 * the sidebar for which eight, and the whole reason disabling refuses rather
 * than cascading is to make the dependency visible instead of surprising.
 */
export async function _setMachineEnabled(machine, enabled) {
  try {
    const resp = await apiFetch(`/api/machines/${encodeURIComponent(machine.id)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      if (resp.status === 409) {
        const names = (data.pinned_chats || []).map(c => c.title).join(' · ');
        const more = (data.pinned_total || 0) - (data.pinned_chats || []).length;
        notifyResult(
          data.error + (names ? `\n${names}${more > 0 ? ` · … (${more} more)` : ''}` : ''),
          'error');
        return;
      }
      throw new Error(data.error || data.detail || `Failed (${resp.status})`);
    }
    await loadMachines(true);
    _renderMachineList();
    notifyResult(enabled ? `${machine.name} enabled` : `${machine.name} disabled`);
  } catch (error) {
    notifyResult(error.message, 'error');
  }
}
```

- [ ] **Step 6: Style the inactive card**

In `web/index.html`, beside the existing `.machine-card` / `.machine-active` rules, add:

```css
        /* Dimmed, not hidden. A shelved backend is still configuration you
           own and will want to find again -- hiding it is how people end up
           recreating a backend that already exists (this deployment
           accumulated seven duplicate "Anthropic API" rows for a related
           reason). */
        .machine-card.machine-disabled { opacity: 0.55; border-style: dashed; }
        .machine-card.machine-disabled .machine-models { display: none; }
        .machine-state-off {
            background: var(--muted-bg, #e5e7eb);
            color: var(--muted-fg, #6b7280);
        }
```

- [ ] **Step 7: Run the test to confirm it passes**

```bash
.venv/bin/python -m pytest tests/test_frontend.py -k BackendEnabledCard -v
```

Expected: PASS, 6 tests.

- [ ] **Step 8: Validate the JavaScript**

```bash
.venv/bin/python -m pytest tests/test_frontend_syntax.py tests/test_qa_js_module_scope.py -q
```

Expected: `machines.js` parses and stays inside its module scope. Note `supervisor-map.js` has a **pre-existing** failure on this tree (it reads a `_tree` declared nowhere) — that is another session's file; report it, do not fix it.

- [ ] **Step 9: Bump the module version if you changed an import**

If `machines.js`'s `./app.js?v=NN` import was already at the version other files use, leave it. If you added an import of a symbol from another module, bump **every** URL for that module in lockstep — `app.js`, `index.html`, and any other importer. Differing query strings are separate cache keys and the module is evaluated twice as two instances. Check with:

```bash
grep -rn "machines.js?v=\|app.js?v=" web/assets/*.js web/*.html
```

Expected: one version per module across every reference.

- [ ] **Step 10: Commit**

```bash
git add web/assets/machines.js web/index.html tests/test_frontend.py
git commit -m "feat: Default / Active / Inactive states on the backend cards"
```

---

### Task 7: Whole-suite reading and a live check

**Files:** none modified.

- [ ] **Step 1: Run the suite the way this box can survive**

```bash
cd /home/kali/projects/claude-code-webconsole
WC_SUITE_OUT=/tmp/wc-suite-enabled bash bin/run-suite-chunked.sh 2>&1 | tail -30
```

A single-process `.venv/bin/python -m pytest` gets OOM-killed here (3.8 GB with the live server resident); this runner returns memory at every file boundary. Note its header's own caveat: a green chunked run is not the same claim as a green single-process run.

Expected: no failures in the files this plan created or modified. Pre-existing failures elsewhere on this tree (`test_voice_turn.py`, `test_qa_coverage.py`, `test_machine_provider.py`, `supervisor-map.js`) belong to other sessions — list them in your report, do not fix them.

- [ ] **Step 2: Confirm your own files are green**

```bash
.venv/bin/python -m pytest \
  tests/test_qa_backend_enabled_column.py \
  tests/test_qa_chats_pinned_to_machine.py \
  tests/test_qa_backend_set_enabled.py \
  tests/test_qa_backend_disable_route.py \
  tests/test_qa_disabled_backend_not_offered.py \
  tests/test_qa_db_dispatch_names.py \
  tests/test_qa_duplicate_routes.py -q
```

Expected: all pass.

- [ ] **Step 3: Verify against a copy of the production database**

```bash
.venv/bin/python - <<'PY'
import asyncio, os, sqlite3, tempfile, importlib
from pathlib import Path
src = "data/webconsole.db"
tmp = Path(tempfile.mkdtemp(prefix="wc-final-")) / "wc.db"
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(str(tmp))
s.backup(d); s.close(); d.close()
os.environ["WC_DB_PATH"] = str(tmp); os.environ["WC_PROXY_ENABLED"] = "0"
import config; importlib.reload(config)
import db; db.config = config
async def main():
    await db.init()
    machines = await db.ai_machines_list("admin")
    for m in machines:
        pins = await db.chats_pinned_to_machine(m["id"], "admin")
        flag = "DEFAULT" if m["active"] else ("active" if m["enabled"] else "INACTIVE")
        print(f"  {m['name']:<45} {flag:<8} enabled={m['enabled']} pinned={pins['total']}")
    await db.close()
asyncio.run(main())
PY
```

Expected: every backend `enabled=1`; exactly one `DEFAULT`; pin counts matching the four backends the spec recorded (CF AI Machine 8, CF AI Machine API 5, Anthropic Oauth via appsectools 1, CF AI Machine (via Pentester) 1) — allowing for drift, since other sessions are using this host.

- [ ] **Step 4: Report, do not deploy**

The console needs a restart to serve the new column and endpoint. **Do not restart it** — a restart cancels in-flight turns on a host shared with other sessions and the user. Report that a restart is pending and let them choose the moment.

- [ ] **Step 5: Final scope check on the commits**

```bash
git log --oneline -7
git show --stat HEAD~6..HEAD | grep -E '^\s+\S+\s+\|' | sort -u
```

Expected: only the files this plan names appear. If any commit picked up a file belonging to another session, say so plainly in your report — this is the shared-tree risk named in Global Constraints, and it has already cost this tree one lost set of edits today.

---

## Self-review

**Spec coverage.** Every spec section maps to a task: the `enabled` column and its `DEFAULT 1` safety → Task 1; naming the dependents → Task 2; the setter and the activate guard → Task 3; the refusal rules and the 409 payload → Task 4; the picker filters including `machine_for` → Task 5; the three visual states and the dim-not-hide decision → Task 6. The spec's "Known limits" need no task — they record accepted trades. The spec's claim that a chat pinned to a disabled backend keeps working needs no code: nothing in Tasks 1-6 filters by `enabled` on the turn path, which is what makes it true by construction.

**Two spec items deliberately not given their own task.** `GET /api/machines/{id}` including `enabled` is covered by Task 1's Step 6 (it edits `ai_machine_get`, which both routes return). The model picker's filter is Task 5's `handle_settings_get` change — the picker reads that payload rather than querying separately.

**Type consistency.** `chats_pinned_to_machine` returns `{"total", "titles", "ids"}` in Task 2 and is consumed with exactly those keys in Tasks 4 and 7. `ai_machine_set_enabled(machine_id, owner_id, enabled)` is defined in Task 3 and awaited with that argument order in Task 4. The 409 body keys `is_default` / `pinned_total` / `pinned_chats` are produced in Task 4 and read in Task 6's `_setMachineEnabled`.

**One known gap in the frontend test.** Task 6 asserts against the *source* of `machines.js`, not a rendered DOM, because no browser test opens the Backends panel with a disabled backend. A source assertion catches a deleted feature but not a broken render. Adding a browser test would mean seeding a disabled machine into the fixture database in `tests/test_frontend_browser.py` — worth doing, out of scope here, and named so it is a decision rather than an oversight.
