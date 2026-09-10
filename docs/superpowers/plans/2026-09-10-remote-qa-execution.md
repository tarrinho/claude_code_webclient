# Remote QA Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the QA suite run on a remote SSH transport instead of this
memory-constrained host, reusing the project's existing tunnel/sync machinery
rather than building a second one.

**Architecture:** A new orchestrator module (`qa_remote.py`) runs *inside* the
`webconsole.service` process — the only place `tunnel_manager`'s live SSH
connections exist — and is driven by a new streaming route
(`POST /api/qa/run`). A thin CLI wrapper (`bin/wc-run-suite-remote.sh`) mints
an API token and calls that route, printing events as they stream back. A new
one-time provisioning script (`bin/wc-provision-qa.sh`) sets up a transport's
remote Python venv + Chromium before it can take a run.

**Tech Stack:** Python 3 (FastAPI, `aiosqlite` via `db.py`, `paramiko` via
`tunnel_manager_ssh.py`), bash, pytest/`unittest.IsolatedAsyncioTestCase`.

**Spec:** `docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md`

## Global Constraints

- **Shared git working tree.** Several Claude sessions (`cweb1`..`cweb6`+)
  commit to this tree concurrently. Every `git add` uses explicit pathspecs —
  never a whole-tree add. Before every commit, run
  `git status --porcelain <the files this task touched>`; if a file shows a
  peer's changes mixed with yours, stage a computed blob instead of the
  working-tree file: `git show HEAD:<path>` → apply only your intended edit to
  that text in a scratch file → `git hash-object -w --path <path> <scratch>` →
  `git update-index --cacheinfo 100644,<sha>,<path>` → confirm with
  `git diff --cached -- <path>` that only your change is staged before
  committing.
- **Tests run bare.** `.venv/bin/python -m pytest <files> -q` — never
  `pytest tests/`, which misses the one file outside `tests/`
  (`test_functional.py`, not touched by this plan). Never run `db.init()`
  against the production database (`data/webconsole.db`); every test in this
  plan uses `config.DB_PATH` patched to a tempdir, matching every existing
  test in `tests/test_db_transports.py` / `tests/test_transport_sync_api.py`.
- **Resource guard.** A full single-process pytest run OOM-kills this box, and
  `bash bin/run-suite-chunked.sh` (note: `bash`, not direct exec — the file is
  not marked executable in this checkout) itself refuses to start under
  memory pressure. This plan's own purpose is relieving that pressure, so
  verification steps run **targeted test files**, never the full suite, during
  implementation.
- **No changes to `transport_sync.py`, `tunnel_manager_ssh.py`, or
  `run-suite-chunked.sh` itself.** All three are consumed as they already are
  (spec, "Files touched").
- **`~/wc-qa-checkout` is never `~/wc-proxy`.** The QA checkout never runs as
  the application, has its own sync pointer (`last_qa_synced_sha`), and is
  synced unattended (no approval-queue row) — the production sync's approval
  gate is a different mechanism entirely and this plan never calls it.
- **Token expiry is pinned to 1 day** (`bin/wc-token.py create --days 1`), and
  the scoping gap (a token carries its full owner's role, not scoped to one
  route) is stated as an accepted, inherited limitation — not something this
  plan adds new code to fix (spec §7).
- **Chunk result status is one of exactly four values**: `passed`,
  `test_failure`, `transport_error`, `capacity_refused` (spec §6). Every task
  that produces a chunk result uses these four literal strings, verbatim,
  nowhere else.
- **Capacity floor defaults to 700 MB**, matching `WC_SUITE_COST_MB`'s
  existing default for a chunk's declared local cost (spec §3).
- **Chunk wall-clock cap defaults to 600s**, matching
  `run-suite-chunked.sh`'s own `WC_CHUNK_TIMEOUT` default (spec §6).

---

## File Structure

| File | Responsibility |
|---|---|
| `config.py` | Two new constants: `QA_CAPACITY_FLOOR_MB`, `QA_CHUNK_TIMEOUT`. |
| `db.py` | `ssh_transports.last_qa_synced_sha` column + migration (mirrors `last_synced_sha` exactly). Dispatch-table entry for the new setter. |
| `routes/db_transports.py` | `_TRANSPORT_COLUMNS` gains the new column; `ssh_transport_set_last_qa_synced_sha` setter (mirrors `ssh_transport_set_last_synced_sha`). |
| `qa_remote.py` (new) | The orchestrator: capacity check, provisioning check, per-transport lock, node selection, sync, chunked remote execution, streamed events. Everything that needs the live app process's `tunnel_manager._STATE`. |
| `routes/qa.py` (new) | `POST /api/qa/run` — thin HTTP wrapper around `qa_remote.py`. Owner-scoped, `StreamingResponse`. |
| `app.py` | Register `routes.qa.router`. |
| `bin/wc-provision-qa.sh` (new) | One-time per-transport venv + Chromium setup. Standalone SSH, no live tunnel required (mirrors `bin/wc-deploy-proxy.sh`'s shape). |
| `bin/wc-run-suite-remote.sh` (new) | CLI wrapper: mint a 1-day token, `POST /api/qa/run`, stream the response to the terminal. |
| Tests | `tests/test_db_transports.py` (setter), `tests/test_qa_transport_qa_column.py` (new, migration), `tests/test_qa_remote_capacity.py` (new), `tests/test_qa_remote_provisioning.py` (new), `tests/test_qa_remote_lock_and_selection.py` (new), `tests/test_qa_remote_sync.py` (new), `tests/test_qa_remote_chunks.py` (new), `tests/test_qa_run_api.py` (new), `tests/test_qa_provision_script.py` (new), `tests/test_qa_run_remote_script.py` (new). |

---

### Task 1: `last_qa_synced_sha` column, setter, migration

**Files:**
- Modify: `db.py` — schema block around `db.py:330` (add column to `CREATE TABLE ssh_transports`), dispatch table around `db.py:137` (add new entry), migration function `db.py:1306-1334` (`_ensure_transport_columns`).
- Modify: `routes/db_transports.py` — `_TRANSPORT_COLUMNS` constant (`routes/db_transports.py:11-14`), new setter appended after `ssh_transport_set_last_synced_sha` (`routes/db_transports.py:116-125`).
- Test: `tests/test_qa_transport_qa_column.py` (new)
- Test: `tests/test_db_transports.py` (extend)

**Interfaces:**
- Produces: `db.ssh_transport_set_last_qa_synced_sha(transport_id: str, sha: str) -> None` — every later task that advances the QA sync pointer calls this exact name.
- Produces: `ssh_transports.last_qa_synced_sha` column, `TEXT NOT NULL DEFAULT ''`, readable via `db.ssh_transport_get(transport_id, owner_id)` and `db.ssh_transports_list(owner_id)` (both already return whatever `_TRANSPORT_COLUMNS` lists).

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_qa_transport_qa_column.py`:

```python
"""QA: last_qa_synced_sha exists on ssh_transports, and an old database
gains it without losing last_synced_sha.

Two independent sync pointers on the same table -- production
(last_synced_sha) and QA (last_qa_synced_sha) -- must never collide or
overwrite each other's column during migration. See
docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §2.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wc-qa-column-")) / "wc.db"
    os.environ["WC_DB_PATH"] = str(tmp)
    os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
    os.environ.setdefault("WC_PROXY_ENABLED", "0")
    import config

    importlib.reload(config)
    import db

    db.config = config
    return tmp


class QaSyncColumnTests(unittest.IsolatedAsyncioTestCase):
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
            return {r[1] for r in con.execute("PRAGMA table_info(ssh_transports)")}
        finally:
            con.close()

    async def test_the_column_exists_after_init(self):
        self.assertIn("last_qa_synced_sha", self._columns())

    async def test_init_is_idempotent(self):
        await self.db.close()
        await self.db.init()
        self.assertIn("last_qa_synced_sha", self._columns())

    async def test_a_database_without_the_column_gains_it_empty(self):
        """The deploy-safety case: an existing transport must stay usable,
        and its production sync pointer must survive the migration untouched."""
        await self.db.ssh_transport_create(
            "t-old", "Old", "admin", "old.example.net", "kali", "~/.ssh/id_ed25519")
        await self.db.ssh_transport_set_last_synced_sha("t-old", "prodsha123")
        await self.db.close()

        con = sqlite3.connect(str(self.path))
        try:
            con.execute("ALTER TABLE ssh_transports DROP COLUMN last_qa_synced_sha")
            con.commit()
        finally:
            con.close()

        await self.db.init()
        self.assertIn("last_qa_synced_sha", self._columns())
        row = await self.db.ssh_transport_get("t-old", "admin")
        self.assertEqual(row["last_qa_synced_sha"], "")
        self.assertEqual(
            row["last_synced_sha"], "prodsha123",
            "the migration must not disturb the unrelated production pointer")

    async def test_setter_advances_only_the_qa_pointer(self):
        await self.db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        await self.db.ssh_transport_set_last_synced_sha("t1", "prodsha")
        await self.db.ssh_transport_set_last_qa_synced_sha("t1", "qasha")

        row = await self.db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["last_synced_sha"], "prodsha")
        self.assertEqual(row["last_qa_synced_sha"], "qasha")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_transport_qa_column.py -q`
Expected: FAIL — `last_qa_synced_sha` not a column / `ssh_transport_set_last_qa_synced_sha` not defined.

- [ ] **Step 3: Add the column to the CREATE TABLE and the migration**

In `db.py`, the `ssh_transports` table definition (around line 330):

```python
        CREATE TABLE IF NOT EXISTS ssh_transports (
            id                        TEXT PRIMARY KEY,
            name                      TEXT NOT NULL,
            owner_id                  TEXT NOT NULL,
            ssh_host                  TEXT NOT NULL,
            ssh_user                  TEXT NOT NULL DEFAULT 'kali',
            ssh_key_path              TEXT NOT NULL DEFAULT '',
            ssh_host_key_fingerprint  TEXT NOT NULL DEFAULT '',
            remote_path               TEXT NOT NULL DEFAULT '~/wc-proxy',
            last_synced_sha           TEXT NOT NULL DEFAULT '',
            last_qa_synced_sha        TEXT NOT NULL DEFAULT '',
            created_at                TEXT NOT NULL,
            updated_at                TEXT NOT NULL
        );
```

In `_ensure_transport_columns()` (around line 1329), add a third block after
the existing `last_synced_sha` one:

```python
    if columns and "last_qa_synced_sha" not in columns:
        await db_conn.execute(
            "ALTER TABLE ssh_transports ADD COLUMN last_qa_synced_sha TEXT NOT NULL "
            "DEFAULT ''"
        )
        await db_conn.commit()
```

In the dispatch table (around line 137), add one line right after
`"ssh_transport_set_last_synced_sha": "routes.db_transports",`:

```python
        "ssh_transport_set_last_qa_synced_sha": "routes.db_transports",
```

- [ ] **Step 4: Add the setter and extend `_TRANSPORT_COLUMNS`**

In `routes/db_transports.py`, change `_TRANSPORT_COLUMNS`:

```python
_TRANSPORT_COLUMNS = (
    "id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
    "ssh_host_key_fingerprint, remote_path, last_synced_sha, "
    "last_qa_synced_sha, created_at, updated_at"
)
```

Append after `ssh_transport_set_last_synced_sha` (end of file):

```python


async def ssh_transport_set_last_qa_synced_sha(transport_id: str, sha: str) -> None:
    """Advance the QA sync pointer. Independent of last_synced_sha -- see
    ssh_transport_set_last_synced_sha's own docstring for the "no silent
    pointer advancement" rule, which applies here identically. No owner_id
    scoping, matching that setter: the caller (qa_remote.py) runs from a
    request handler that has already checked ownership.
    """
    await db.db_conn.execute(
        "UPDATE ssh_transports SET last_qa_synced_sha = ? WHERE id = ?",
        (sha, transport_id),
    )
    await db.db_conn.commit()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_transport_qa_column.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Extend the existing transport CRUD test for the new column**

In `tests/test_db_transports.py`, find the assertion around line 32 that lists
expected columns (`"last_synced_sha", "created_at", "updated_at"`) and add
`"last_qa_synced_sha"` to that list, in the same position as the schema
(right after `last_synced_sha`, before `created_at`).

- [ ] **Step 7: Run the full transports test file**

Run: `.venv/bin/python -m pytest tests/test_db_transports.py tests/test_qa_transport_qa_column.py -q`
Expected: PASS, all tests.

- [ ] **Step 8: Commit**

```bash
git status --porcelain db.py routes/db_transports.py tests/test_qa_transport_qa_column.py tests/test_db_transports.py
git add db.py routes/db_transports.py tests/test_qa_transport_qa_column.py tests/test_db_transports.py
git commit -m "feat: add ssh_transports.last_qa_synced_sha, independent of production sync pointer"
```

---

### Task 2: Capacity check and provisioning check

**Files:**
- Create: `qa_remote.py`
- Test: `tests/test_qa_remote_capacity.py` (new)

**Interfaces:**
- Consumes: `tunnel_manager_ssh.exec_command(machine_id: str, cmd: str, timeout: int) -> (stdin, stdout, stderr)` (existing).
- Produces: `qa_remote.QA_REMOTE_PATH: str = "~/wc-qa-checkout"` — every later task's remote paths use this constant, never a literal.
- Produces: `qa_remote._available_mb(machine_id: str) -> int | None` — `None` means the read itself failed (SSH/exec error), not "0 MB available"; later tasks (Task 4, Task 6) must not conflate the two.
- Produces: `qa_remote._check_capacity(machine_id: str, floor_mb: int) -> tuple[bool, int | None]` — `(ok, available_mb)`.
- Produces: `qa_remote._is_provisioned(machine_id: str) -> bool`.
- Produces: `qa_remote.QaRefusal(Exception)` with `.status_code: int` and `.reason: str` attributes — every precondition failure in later tasks raises this, never a bare `Exception` or `HTTPException` (the route in Task 7 is the only place that translates it to HTTP).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_remote_capacity.py`:

```python
"""QA: qa_remote's live capacity and provisioning checks.

Both read the transport directly over exec_command, never from stored
stats -- tunnel_manager_health's store_fn is separately broken (see
docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §3), so
nothing about remote memory can be trusted from the database.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import qa_remote


def _exec_returning(text: str):
    stdout = MagicMock()
    stdout.read.return_value = text.encode("utf-8")
    return AsyncMock(return_value=(MagicMock(), stdout, MagicMock()))


class AvailableMbTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_the_free_dash_m_output(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("2048\n")):
            self.assertEqual(await qa_remote._available_mb("m1"), 2048)

    async def test_a_raising_exec_command_is_none_not_zero(self):
        """None (could not read) and 0 (read, and it said zero) are different
        facts -- the capacity check below must not treat a broken SSH
        session as an empty host."""
        with patch("tunnel_manager_ssh.exec_command",
                    AsyncMock(side_effect=RuntimeError("tunnel not connected"))):
            self.assertIsNone(await qa_remote._available_mb("m1"))

    async def test_unparseable_output_is_none(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("garbage\n")):
            self.assertIsNone(await qa_remote._available_mb("m1"))


class CheckCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_above_floor_is_ok(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("900\n")):
            ok, available = await qa_remote._check_capacity("m1", floor_mb=700)
        self.assertTrue(ok)
        self.assertEqual(available, 900)

    async def test_below_floor_refuses(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("400\n")):
            ok, available = await qa_remote._check_capacity("m1", floor_mb=700)
        self.assertFalse(ok)
        self.assertEqual(available, 400)

    async def test_unreadable_refuses(self):
        with patch("tunnel_manager_ssh.exec_command",
                    AsyncMock(side_effect=RuntimeError("gone"))):
            ok, available = await qa_remote._check_capacity("m1", floor_mb=700)
        self.assertFalse(ok)
        self.assertIsNone(available)


class IsProvisionedTests(unittest.IsolatedAsyncioTestCase):
    async def test_provisioned_when_the_venv_python_is_executable(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("yes\n")):
            self.assertTrue(await qa_remote._is_provisioned("m1"))

    async def test_not_provisioned_when_the_check_reports_nothing(self):
        with patch("tunnel_manager_ssh.exec_command", _exec_returning("\n")):
            self.assertFalse(await qa_remote._is_provisioned("m1"))

    async def test_not_provisioned_when_exec_command_raises(self):
        with patch("tunnel_manager_ssh.exec_command",
                    AsyncMock(side_effect=RuntimeError("tunnel not connected"))):
            self.assertFalse(await qa_remote._is_provisioned("m1"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_capacity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'qa_remote'`.

- [ ] **Step 3: Write `qa_remote.py` (capacity + provisioning + refusal type)**

Create `qa_remote.py`:

```python
"""Orchestrate a QA suite run on a remote transport.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.

Runs *inside* the app process, never as a standalone script: exec_command,
open_sftp and therefore sync_transport are all bound to
tunnel_manager._STATE, the running webconsole.service process's own
in-memory live SSH connections (spec §4). routes/qa.py is the only caller
that should ever import this module from outside a request handler.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("wc.qa_remote")

# Deliberately not ~/wc-proxy (the transport's own remote_path, where
# claude_proxy.py actually runs from) -- see spec §2 for the blast-radius
# and cadence reasons this checkout is kept separate.
QA_REMOTE_PATH = "~/wc-qa-checkout"


class QaRefusal(Exception):
    """A QA run cannot proceed. .status_code is the HTTP status routes/qa.py
    should answer with; .reason is the user-facing message."""

    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


async def _available_mb(machine_id: str) -> int | None:
    """Live remote available memory in MB, read fresh every call -- never
    trusted from storage (spec §3: tunnel_manager_health's store_fn is
    separately broken, so nothing here can rely on persisted stats).
    Returns None on any failure to read or parse, distinct from a
    successfully-read 0 -- callers must not conflate "could not check" with
    "checked, and it's empty"."""
    import tunnel_manager_ssh

    try:
        _, stdout, _ = await tunnel_manager_ssh.exec_command(
            machine_id, "free -m | awk '/Mem:/{print $7}'", timeout=5)
        return int(stdout.read().decode("utf-8", "replace").strip())
    except Exception:
        return None


async def _check_capacity(machine_id: str, floor_mb: int) -> tuple[bool, int | None]:
    """(ok, available_mb). ok is False both when available is below floor_mb
    and when available could not be read at all."""
    available = await _available_mb(machine_id)
    if available is None:
        return False, None
    return available >= floor_mb, available


async def _is_provisioned(machine_id: str) -> bool:
    """Has bin/wc-provision-qa.sh ever run here? Checked live, the same way
    as capacity -- a stale "provisioned" flag would be worse than no flag at
    all, since the checkout could have been wiped since."""
    import tunnel_manager_ssh

    cmd = f"test -x {QA_REMOTE_PATH}/.venv/bin/python && echo yes"
    try:
        _, stdout, _ = await tunnel_manager_ssh.exec_command(machine_id, cmd, timeout=5)
        return stdout.read().decode("utf-8", "replace").strip() == "yes"
    except Exception:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_capacity.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git status --porcelain qa_remote.py tests/test_qa_remote_capacity.py
git add qa_remote.py tests/test_qa_remote_capacity.py
git commit -m "feat: qa_remote capacity and provisioning checks, read live over exec_command"
```

---

### Task 3: Per-transport lock and node selection

**Files:**
- Modify: `qa_remote.py`
- Test: `tests/test_qa_remote_lock_and_selection.py` (new)

**Interfaces:**
- Consumes: `qa_remote._check_capacity`, `qa_remote._is_provisioned`, `qa_remote.QaRefusal`, `qa_remote.QA_REMOTE_PATH` (Task 2).
- Consumes: `db.ssh_transports_list(owner_id)`, `db.ssh_transport_get(transport_id, owner_id)`, `db.ai_machines_list(owner_id)` (existing), `tunnel_manager.tunnel_status(machine_id) -> dict | None` (existing, key `"tunnel_up"` truthy means Active — same convention `routes/transports.py:424` already uses).
- Produces: `qa_remote._run_lock(machine_id: str) -> asyncio.Lock` — module-level dict of locks, one per `machine_id`, never cleaned up (mirrors `tunnel_manager._transport_lock`'s own justification: the number of machines is small).
- Produces: `qa_remote._machine_for_transport(transport_id: str, owner: str) -> str | None`.
- Produces: `qa_remote.Prepared` dataclass: `transport: dict`, `machine_id: str`, `floor_mb: int`. Task 4 (sync) and Task 6 (execution) both consume this exact shape from Task 7's route.
- Produces: `qa_remote.resolve_transport(owner: str, name: str | None, floor_mb: int = 700) -> Prepared` — raises `QaRefusal` for every refusal case; never returns partial success.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_remote_lock_and_selection.py`:

```python
"""QA: qa_remote's per-transport lock and node selection.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §5.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
import qa_remote


class _WithTransportsDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        qa_remote._RUN_LOCKS.clear()
        self.addCleanup(qa_remote._RUN_LOCKS.clear)

    async def _make_transport(self, tid, name, machine_id):
        await db.ssh_transport_create(
            tid, name, "admin", f"{name}.example.net", "kali", "~/.ssh/id_ed25519")
        await db.ai_machine_create(
            machine_id, f"{name} backend", "", 0, None, "", None, None, "admin",
            transport_id=tid)

    def _tunnel_up(self, **extra):
        return patch("tunnel_manager.tunnel_status",
                     AsyncMock(return_value={"tunnel_up": True, **extra}))

    def _tunnel_down(self):
        return patch("tunnel_manager.tunnel_status", AsyncMock(return_value=None))


class RunLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        qa_remote._RUN_LOCKS.clear()
        self.addCleanup(qa_remote._RUN_LOCKS.clear)

    async def test_same_machine_id_returns_the_same_lock(self):
        self.assertIs(qa_remote._run_lock("m1"), qa_remote._run_lock("m1"))

    async def test_different_machine_ids_get_different_locks(self):
        self.assertIsNot(qa_remote._run_lock("m1"), qa_remote._run_lock("m2"))


class MachineForTransportTests(_WithTransportsDb):
    async def test_finds_the_assigned_machine(self):
        await self._make_transport("t1", "One", "m1")
        self.assertEqual(await qa_remote._machine_for_transport("t1", "admin"), "m1")

    async def test_none_when_no_machine_assigned(self):
        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        self.assertIsNone(await qa_remote._machine_for_transport("t1", "admin"))


class ResolveNamedTransportTests(_WithTransportsDb):
    async def test_refuses_404_when_transport_does_not_exist(self):
        with self.assertRaises(qa_remote.QaRefusal) as ctx:
            await qa_remote.resolve_transport("admin", "nope")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_refuses_409_when_not_active(self):
        await self._make_transport("t1", "One", "m1")
        with self._tunnel_down():
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", "One")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Check or Init it first", ctx.exception.reason)

    async def test_refuses_409_when_not_provisioned(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=False)),
        ):
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", "One")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("wc-provision-qa.sh", ctx.exception.reason)

    async def test_refuses_409_when_already_locked(self):
        await self._make_transport("t1", "One", "m1")
        await qa_remote._run_lock("m1").acquire()
        try:
            with (
                self._tunnel_up(),
                patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            ):
                with self.assertRaises(qa_remote.QaRefusal) as ctx:
                    await qa_remote.resolve_transport("admin", "One")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("already has a QA run in progress", ctx.exception.reason)
        finally:
            qa_remote._run_lock("m1").release()

    async def test_refuses_503_below_capacity_floor(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", AsyncMock(return_value=(False, 400))),
        ):
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", "One", floor_mb=700)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_succeeds_when_every_precondition_passes(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
        ):
            prepared = await qa_remote.resolve_transport("admin", "One", floor_mb=700)
        self.assertEqual(prepared.transport["id"], "t1")
        self.assertEqual(prepared.machine_id, "m1")
        self.assertEqual(prepared.floor_mb, 700)


class ResolveUnnamedTransportTests(_WithTransportsDb):
    async def test_picks_the_roomiest_active_candidate(self):
        await self._make_transport("t1", "Small", "m1")
        await self._make_transport("t2", "Big", "m2")

        async def fake_capacity(machine_id, floor_mb):
            return (True, 900) if machine_id == "m2" else (True, 750)

        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", side_effect=fake_capacity),
        ):
            prepared = await qa_remote.resolve_transport("admin", None, floor_mb=700)
        self.assertEqual(prepared.transport["id"], "t2")

    async def test_excludes_locked_transports_from_selection(self):
        await self._make_transport("t1", "Locked", "m1")
        await self._make_transport("t2", "Free", "m2")
        await qa_remote._run_lock("m1").acquire()
        try:
            with (
                self._tunnel_up(),
                patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
                patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            ):
                prepared = await qa_remote.resolve_transport("admin", None, floor_mb=700)
            self.assertEqual(prepared.transport["id"], "t2")
        finally:
            qa_remote._run_lock("m1").release()

    async def test_refuses_503_when_none_qualify(self):
        await self._make_transport("t1", "Dead", "m1")
        with self._tunnel_down():
            with self.assertRaises(qa_remote.QaRefusal) as ctx:
                await qa_remote.resolve_transport("admin", None, floor_mb=700)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_never_falls_back_to_running_locally(self):
        """No transport qualifies -- refuse, and refuse only. There is no
        code path in resolve_transport that returns a local execution
        target; this test pins that absence."""
        with self.assertRaises(qa_remote.QaRefusal):
            await qa_remote.resolve_transport("admin", None, floor_mb=700)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_lock_and_selection.py -q`
Expected: FAIL — `AttributeError: module 'qa_remote' has no attribute '_RUN_LOCKS'` (and others).

- [ ] **Step 3: Add lock, node selection and `resolve_transport` to `qa_remote.py`**

Append to `qa_remote.py`:

```python
import asyncio
from dataclasses import dataclass

_RUN_LOCKS: dict[str, asyncio.Lock] = {}


def _run_lock(machine_id: str) -> asyncio.Lock:
    """One lock per machine_id, held for a QA run's whole lifetime (spec
    §5) -- sync through the last chunk. Never cleaned up, matching
    tunnel_manager._transport_lock's own reasoning: the number of machines
    a user configures is small and this is a cheap resource."""
    lock = _RUN_LOCKS.get(machine_id)
    if lock is None:
        lock = asyncio.Lock()
        _RUN_LOCKS[machine_id] = lock
    return lock


async def _machine_for_transport(transport_id: str, owner: str) -> str | None:
    """The machine tunnel_status/exec_command/open_sftp key on for this
    transport. A deliberate copy of routes.transports._machine_for_transport
    (module-private there, so not imported) -- one shared tunnel per
    transport, so any machine on it addresses the same connection."""
    import db

    machines = [
        m for m in await db.ai_machines_list(owner)
        if m.get("transport_id") == transport_id
    ]
    return machines[0]["id"] if machines else None


@dataclass
class Prepared:
    transport: dict
    machine_id: str
    floor_mb: int


async def _check_named(transport: dict, machine_id: str, floor_mb: int) -> None:
    """Raises QaRefusal on the first precondition that fails, in the order
    spec §4/§5 states them: liveness, provisioning, lock, capacity."""
    import tunnel_manager

    name = transport["name"]
    status = await tunnel_manager.tunnel_status(machine_id)
    if not status or not status.get("tunnel_up"):
        raise QaRefusal(409, f"{name} has no live tunnel — Check or Init it first")
    if not await _is_provisioned(machine_id):
        raise QaRefusal(
            409, f"{name} has not been provisioned for QA — "
                 f"run bin/wc-provision-qa.sh {name} first")
    if _run_lock(machine_id).locked():
        raise QaRefusal(
            409, f"{name} already has a QA run in progress — "
                 f"wait for it or pick another transport")
    ok, available = await _check_capacity(machine_id, floor_mb)
    if not ok:
        seen = f"{available} MB" if available is not None else "unknown (read failed)"
        raise QaRefusal(
            503, f"{name} does not have enough free memory for a QA run "
                 f"(available: {seen}, floor: {floor_mb} MB)")


async def resolve_transport(
    owner: str, name: str | None, floor_mb: int = 700,
) -> Prepared:
    """Named: use that transport or raise QaRefusal with the specific
    reason. Unnamed: consider every owner's Active, unlocked, provisioned
    transport, measure each one's live memory, and pick the roomiest --
    never fall back to running locally (spec §5)."""
    import db
    import tunnel_manager

    if name:
        transport = next(
            (t for t in await db.ssh_transports_list(owner) if t["name"] == name),
            None,
        )
        if not transport:
            raise QaRefusal(404, f"no transport named {name!r}")
        machine_id = await _machine_for_transport(transport["id"], owner)
        if not machine_id:
            raise QaRefusal(400, f"{name} has no backend assigned")
        await _check_named(transport, machine_id, floor_mb)
        return Prepared(transport=transport, machine_id=machine_id, floor_mb=floor_mb)

    best: Prepared | None = None
    best_available = -1
    for transport in await db.ssh_transports_list(owner):
        machine_id = await _machine_for_transport(transport["id"], owner)
        if not machine_id or _run_lock(machine_id).locked():
            continue
        status = await tunnel_manager.tunnel_status(machine_id)
        if not status or not status.get("tunnel_up"):
            continue
        if not await _is_provisioned(machine_id):
            continue
        ok, available = await _check_capacity(machine_id, floor_mb)
        if not ok or available is None:
            continue
        if available > best_available:
            best = Prepared(transport=transport, machine_id=machine_id, floor_mb=floor_mb)
            best_available = available
    if best is None:
        raise QaRefusal(503, "no transport is Active, provisioned and has enough "
                              "free memory for a QA run right now")
    return best
```

The lookup above is by the transport's `name` column, not its `id` —
`db.ssh_transport_get` takes an id and would be the wrong call here: the
route in Task 7 accepts a human-readable transport *name* in the request
body (`{"transport": "<name>"}`, spec §5), matching how
`bin/wc-run-suite-remote.sh [transport-name]` is invoked, and matching the
precedent `bin/wc-deploy-proxy.sh` already set — it resolves
`WHERE name = ?` directly, never by id, for the same reason: a human names a
transport by what they called it in the UI, not by its internal id.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_lock_and_selection.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Run Task 2's tests again to confirm no regression**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_capacity.py tests/test_qa_remote_lock_and_selection.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git status --porcelain qa_remote.py tests/test_qa_remote_lock_and_selection.py
git add qa_remote.py tests/test_qa_remote_lock_and_selection.py
git commit -m "feat: qa_remote per-transport lock and node selection"
```

---

### Task 4: Sync step, isolated from the production sync pointer

**Files:**
- Modify: `qa_remote.py`
- Test: `tests/test_qa_remote_sync.py` (new)

**Interfaces:**
- Consumes: `transport_sync.sync_transport(machine_id, remote_path, last_synced_sha) -> dict` (existing, unmodified), `db.ssh_transport_set_last_qa_synced_sha` (Task 1), `qa_remote.QA_REMOTE_PATH` (Task 2), `qa_remote.Prepared` (Task 3).
- Produces: `qa_remote._sync(prepared: Prepared) -> dict` — same return shape as `transport_sync.sync_transport` (`{"ok", "files_changed", "reason", "head_sha"}`); advances `last_qa_synced_sha` only when `ok` is True and `head_sha` is truthy, mirroring `routes/transports.py`'s `_run_sync`'s own rule exactly.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_remote_sync.py`:

```python
"""QA: qa_remote's sync step calls transport_sync unmodified, against the
QA path and the QA pointer -- never the production ones.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §2.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
import qa_remote


class SyncIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        await db.ssh_transport_set_last_synced_sha("t1", "prodsha-untouched")

    async def _prepared(self):
        transport = await db.ssh_transport_get("t1", "admin")
        return qa_remote.Prepared(transport=transport, machine_id="m1", floor_mb=700)

    async def test_sync_calls_the_qa_path_and_qa_pointer(self):
        fake = {"ok": True, "files_changed": 2, "reason": "", "head_sha": "qasha1"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=fake)) as mocked:
            await qa_remote._sync(await self._prepared())
        mocked.assert_awaited_once_with("m1", qa_remote.QA_REMOTE_PATH, "")

    async def test_success_advances_only_the_qa_pointer(self):
        fake = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "qasha2"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=fake)):
            await qa_remote._sync(await self._prepared())

        row = await db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["last_qa_synced_sha"], "qasha2")
        self.assertEqual(
            row["last_synced_sha"], "prodsha-untouched",
            "a QA sync must never read or write the production sync pointer")

    async def test_failure_does_not_advance_the_qa_pointer(self):
        fake = {"ok": False, "files_changed": 0, "reason": "boom", "head_sha": ""}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=fake)):
            await qa_remote._sync(await self._prepared())

        row = await db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["last_qa_synced_sha"], "")

    async def test_second_sync_passes_the_previously_advanced_qa_sha(self):
        first = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "qasha-a"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=first)):
            await qa_remote._sync(await self._prepared())

        second = {"ok": True, "files_changed": 0, "reason": "already up to date",
                  "head_sha": "qasha-a"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=second)) as mocked:
            await qa_remote._sync(await self._prepared())
        mocked.assert_awaited_once_with("m1", qa_remote.QA_REMOTE_PATH, "qasha-a")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_sync.py -q`
Expected: FAIL — `AttributeError: module 'qa_remote' has no attribute '_sync'`.

- [ ] **Step 3: Add `_sync` to `qa_remote.py`**

Append to `qa_remote.py`:

```python
async def _sync(prepared: Prepared) -> dict:
    """One call, no new sync engine code -- transport_sync.sync_transport is
    reused unmodified, pointed at the QA checkout and the QA pointer, never
    the production ones (spec §2)."""
    import db
    import transport_sync

    result = await transport_sync.sync_transport(
        prepared.machine_id, QA_REMOTE_PATH,
        prepared.transport.get("last_qa_synced_sha") or "")
    if result["ok"] and result["head_sha"]:
        await db.ssh_transport_set_last_qa_synced_sha(
            prepared.transport["id"], result["head_sha"])
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_sync.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git status --porcelain qa_remote.py tests/test_qa_remote_sync.py
git add qa_remote.py tests/test_qa_remote_sync.py
git commit -m "feat: qa_remote sync step, isolated from the production sync pointer"
```

---

### Task 5: Config constants for capacity floor and chunk timeout

**Files:**
- Modify: `config.py` — after the `TESTING_MODEL_ENFORCE_DEFAULT` line (`config.py:198`).
- Modify: `qa_remote.py` — replace the hardcoded `floor_mb: int = 700` default with `config.QA_CAPACITY_FLOOR_MB`.
- Test: extend `tests/test_qa_remote_lock_and_selection.py`.

**Interfaces:**
- Produces: `config.QA_CAPACITY_FLOOR_MB: int` (default 700, env `WC_SUITE_COST_MB` — deliberately the *same* env var `run-suite-chunked.sh` already reads, per spec §3's "matching WC_SUITE_COST_MB's existing default").
- Produces: `config.QA_CHUNK_TIMEOUT: int` (default 600, env `WC_CHUNK_TIMEOUT` — same var `run-suite-chunked.sh` reads).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_qa_remote_lock_and_selection.py`, a new test class at the end
(before `if __name__ == "__main__":`):

```python
class ConfigDefaultsTests(unittest.TestCase):
    def test_floor_defaults_to_700(self):
        import config

        self.assertEqual(config.QA_CAPACITY_FLOOR_MB, 700)
```

Add a second test as a new method on the existing `ResolveNamedTransportTests`
class (it needs that class's DB fixtures and `self._tunnel_up()` helper, so it
cannot live in the plain `ConfigDefaultsTests` above):

```python
    async def test_resolve_transport_uses_the_config_default_when_unspecified(self):
        await self._make_transport("t1", "One", "m1")
        with (
            self._tunnel_up(),
            patch("qa_remote._is_provisioned", AsyncMock(return_value=True)),
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))) as mocked,
        ):
            await qa_remote.resolve_transport("admin", "One")
        mocked.assert_awaited_once_with("m1", 700)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_lock_and_selection.py -q -k "ConfigDefaults or uses_the_config_default"`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'QA_CAPACITY_FLOOR_MB'`.

- [ ] **Step 3: Add the constants**

In `config.py`, after line 198 (`TESTING_MODEL_ENFORCE_DEFAULT = ...`):

```python

# --- remote QA execution -----------------------------------------------------
# See docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.
# Same env vars bin/run-suite-chunked.sh already reads for the equivalent
# local-run settings, deliberately -- a remote run and a local chunked run
# should agree on what "enough room" and "too long" mean unless told
# otherwise.
QA_CAPACITY_FLOOR_MB = _int("WC_SUITE_COST_MB", 700)
QA_CHUNK_TIMEOUT = _int("WC_CHUNK_TIMEOUT", 600)
```

In `qa_remote.py`, change the start of `resolve_transport` from:

```python
async def resolve_transport(
    owner: str, name: str | None, floor_mb: int = 700,
) -> Prepared:
    """Named: use that transport or raise QaRefusal with the specific
    reason. Unnamed: consider every owner's Active, unlocked, provisioned
    transport, measure each one's live memory, and pick the roomiest --
    never fall back to running locally (spec §5)."""
    import db
    import tunnel_manager

    if name:
```

to:

```python
async def resolve_transport(
    owner: str, name: str | None, floor_mb: int | None = None,
) -> Prepared:
    """Named: use that transport or raise QaRefusal with the specific
    reason. Unnamed: consider every owner's Active, unlocked, provisioned
    transport, measure each one's live memory, and pick the roomiest --
    never fall back to running locally (spec §5)."""
    import db
    import tunnel_manager

    if floor_mb is None:
        import config
        floor_mb = config.QA_CAPACITY_FLOOR_MB

    if name:
```

Everything after `if name:` (both the named and unnamed branches, already
written in Task 3) is unchanged — only the signature and the new
floor-resolution block above `if name:` are new.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_lock_and_selection.py -q`
Expected: PASS, all tests, including the two added in Step 1.

- [ ] **Step 5: Commit**

```bash
git status --porcelain config.py qa_remote.py tests/test_qa_remote_lock_and_selection.py
git add config.py qa_remote.py tests/test_qa_remote_lock_and_selection.py
git commit -m "feat: config defaults for QA capacity floor and chunk timeout"
```

---

### Task 6: Chunked remote execution, status tagging, streamed events

**Files:**
- Modify: `qa_remote.py`
- Test: `tests/test_qa_remote_chunks.py` (new)

**Interfaces:**
- Consumes: `qa_remote.Prepared`, `qa_remote._sync`, `qa_remote._check_capacity`, `qa_remote._run_lock`, `qa_remote.QA_REMOTE_PATH`, `config.QA_CHUNK_TIMEOUT` (earlier tasks).
- Consumes: `tunnel_manager_ssh.exec_command(machine_id, cmd, timeout) -> (stdin, stdout, stderr)`, where `stdout` is a paramiko `ChannelFile` exposing `.channel.recv_exit_status()`.
- Produces: `qa_remote._COLLECT_CMD_FRAGMENT`, `qa_remote._BROWSER_GREP_FRAGMENT`, `qa_remote._CHUNK_GROUP_SIZE = 6` — string/int constants a parity test checks against `bin/run-suite-chunked.sh`'s own source.
- Produces: `qa_remote._collect_chunks(machine_id: str) -> tuple[list[list[str]], list[str]]` — `(plain_file_groups, browser_files)`.
- Produces: `qa_remote.ChunkResult` dataclass: `name: str`, `status: str` (one of `"passed"`, `"test_failure"`, `"transport_error"`, `"capacity_refused"`), `output: str`, `returncode: int | None`.
- Produces: `qa_remote._run_chunk(machine_id, name, files, timeout, floor_mb) -> ChunkResult`.
- Produces: `qa_remote.execute(prepared: Prepared) -> AsyncIterator[dict]` — an async generator yielding event dicts (`{"type": "sync-start"}`, `{"type": "sync-done", ...}`, `{"type": "chunk-start", ...}`, `{"type": "chunk-result", ...}`, `{"type": "run-done", ...}`), holding `_run_lock(prepared.machine_id)` for its whole duration and releasing it in a `finally`. Task 7's route consumes this exact generator.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_remote_chunks.py`:

```python
"""QA: qa_remote's chunked remote execution, status tagging, and the
streamed event sequence.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §6.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import config
import db
import qa_remote

ROOT = Path(__file__).resolve().parents[1]


def _exec_result(text: str, rc: int = 0):
    stdout = MagicMock()
    stdout.read.return_value = text.encode("utf-8")
    stdout.channel.recv_exit_status.return_value = rc
    stderr = MagicMock()
    stderr.read.return_value = b""
    return (MagicMock(), stdout, stderr)


class ChunkFileListParityTests(unittest.TestCase):
    """run-suite-chunked.sh and qa_remote.py must never define "a chunk"
    two different ways -- pinned by checking both sides use the same
    literal fragments, not by re-deriving one from the other."""

    def setUp(self):
        self.source = (ROOT / "bin" / "run-suite-chunked.sh").read_text()

    def test_collect_fragment_matches_the_local_script(self):
        self.assertIn(qa_remote._COLLECT_CMD_FRAGMENT, self.source)

    def test_browser_grep_fragment_matches_the_local_script(self):
        self.assertIn(qa_remote._BROWSER_GREP_FRAGMENT, self.source)

    def test_group_size_matches_the_local_script(self):
        self.assertIn(f'-eq {qa_remote._CHUNK_GROUP_SIZE}', self.source)


class CollectChunksTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_plain_files_and_separates_browser_files(self):
        files = "\n".join(f"tests/test_{i}.py" for i in range(1, 8))  # 7 plain

        async def fake_exec(machine_id, cmd, timeout):
            if "playwright" in cmd:
                return _exec_result("tests/test_browser_x.py\n")
            return _exec_result(files + "\ntests/test_browser_x.py\n")

        with patch("tunnel_manager_ssh.exec_command", fake_exec):
            plain_chunks, browser_files = await qa_remote._collect_chunks("m1")

        self.assertEqual(browser_files, ["tests/test_browser_x.py"])
        self.assertEqual(len(plain_chunks), 2)  # 6 + 1, group size 6
        self.assertEqual(len(plain_chunks[0]), 6)
        self.assertEqual(len(plain_chunks[1]), 1)


class RunChunkTests(unittest.IsolatedAsyncioTestCase):
    async def test_passed_when_pytest_exits_zero(self):
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(return_value=_exec_result("2 passed\n", rc=0))),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.returncode, 0)

    async def test_test_failure_when_pytest_exits_nonzero(self):
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(return_value=_exec_result("1 failed\n", rc=1))),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "test_failure")
        self.assertEqual(result.returncode, 1)

    async def test_transport_error_when_exec_command_raises(self):
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(side_effect=RuntimeError("ssh dropped"))),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "transport_error")
        self.assertIsNone(result.returncode)

    async def test_capacity_refused_before_running_pytest_at_all(self):
        exec_mock = AsyncMock()
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(False, 300))),
            patch("tunnel_manager_ssh.exec_command", exec_mock),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "capacity_refused")
        exec_mock.assert_not_awaited()


class ExecuteEventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        qa_remote._RUN_LOCKS.clear()
        self.addCleanup(qa_remote._RUN_LOCKS.clear)

    async def _prepared(self):
        transport = await db.ssh_transport_get("t1", "admin")
        return qa_remote.Prepared(transport=transport, machine_id="m1", floor_mb=700)

    async def test_events_arrive_incrementally_not_only_at_the_end(self):
        """The whole reason execute() is a generator and not a coroutine
        returning one blocking result: a caller can observe sync-done before
        the first chunk has even started, not only after the entire run."""
        sync_result = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "sha1"}
        with (
            patch("qa_remote._sync", AsyncMock(return_value=sync_result)),
            patch("qa_remote._collect_chunks",
                  AsyncMock(return_value=([["tests/test_a.py"]], []))),
            patch("qa_remote._run_chunk",
                  AsyncMock(return_value=qa_remote.ChunkResult(
                      "plain-01", "passed", "1 passed", 0))),
        ):
            events = []
            async for event in qa_remote.execute(await self._prepared()):
                events.append(event["type"])
                if event["type"] == "sync-done":
                    # Proven mid-stream: the lock is already held here, well
                    # before run-done, which is what "for the run's whole
                    # duration" (spec §5) means operationally.
                    self.assertTrue(qa_remote._run_lock("m1").locked())

        self.assertEqual(
            events, ["sync-start", "sync-done", "chunk-start", "chunk-result", "run-done"])

    async def test_lock_is_released_after_the_run_even_on_failure(self):
        with patch("qa_remote._sync", AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                async for _ in qa_remote.execute(await self._prepared()):
                    pass
        self.assertFalse(qa_remote._run_lock("m1").locked())

    async def test_sync_failure_stops_before_any_chunk_runs(self):
        sync_result = {"ok": False, "files_changed": 0, "reason": "boom", "head_sha": ""}
        collect_mock = AsyncMock()
        with (
            patch("qa_remote._sync", AsyncMock(return_value=sync_result)),
            patch("qa_remote._collect_chunks", collect_mock),
        ):
            events = [e async for e in qa_remote.execute(await self._prepared())]
        collect_mock.assert_not_awaited()
        self.assertEqual(events[-1], {"type": "run-done", "ok": False, "reason": "boom"})

    async def test_run_done_totals_count_each_status(self):
        sync_result = {"ok": True, "files_changed": 0, "reason": "", "head_sha": "sha1"}
        results = [
            qa_remote.ChunkResult("plain-01", "passed", "", 0),
            qa_remote.ChunkResult("plain-02", "test_failure", "", 1),
        ]
        with (
            patch("qa_remote._sync", AsyncMock(return_value=sync_result)),
            patch("qa_remote._collect_chunks", AsyncMock(
                return_value=([["a.py"], ["b.py"]], []))),
            patch("qa_remote._run_chunk", AsyncMock(side_effect=results)),
        ):
            events = [e async for e in qa_remote.execute(await self._prepared())]
        done = events[-1]
        self.assertEqual(done["type"], "run-done")
        self.assertFalse(done["ok"])
        self.assertEqual(done["totals"]["passed"], 1)
        self.assertEqual(done["totals"]["test_failure"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_chunks.py -q`
Expected: FAIL — `AttributeError: module 'qa_remote' has no attribute '_collect_chunks'` (and others).

- [ ] **Step 3: Add chunk discovery, execution and `execute()` to `qa_remote.py`**

Append to `qa_remote.py` (`dataclass` is already imported at module level from
Task 3 — nothing new to import here):

```python
# Kept as literal fragments, not re-derived from bin/run-suite-chunked.sh --
# the two files cannot share code across bash/Python, so parity is pinned
# by a test asserting both contain these same substrings (spec §6: "the
# two runners can never define 'a chunk' two different ways").
_COLLECT_CMD_FRAGMENT = "pytest --collect-only -q"
_BROWSER_GREP_FRAGMENT = "grep -ln 'playwright\\|sync_playwright'"
_CHUNK_GROUP_SIZE = 6


async def _collect_chunks(machine_id: str) -> tuple[list[list[str]], list[str]]:
    """(plain_file_groups, browser_files), discovered remotely so the list
    reflects what is actually on the synced checkout. Mirrors
    run-suite-chunked.sh's own rule exactly: the file list comes from
    pytest's own collection, never a glob; browser files run one at a time;
    everything else in groups of _CHUNK_GROUP_SIZE."""
    import tunnel_manager_ssh

    collect_cmd = (
        f"cd {QA_REMOTE_PATH} && .venv/bin/python -m {_COLLECT_CMD_FRAGMENT} "
        "2>/dev/null | grep -oE '^[^:]+\\.py' | sort -u"
    )
    _, stdout, _ = await tunnel_manager_ssh.exec_command(machine_id, collect_cmd, timeout=60)
    all_files = [
        line.strip() for line in stdout.read().decode("utf-8", "replace").splitlines()
        if line.strip()
    ]

    browser_cmd = (
        f"cd {QA_REMOTE_PATH} && {_BROWSER_GREP_FRAGMENT} "
        f"{' '.join(all_files)} 2>/dev/null | sort"
    )
    _, stdout, _ = await tunnel_manager_ssh.exec_command(machine_id, browser_cmd, timeout=30)
    browser_files = [
        line.strip() for line in stdout.read().decode("utf-8", "replace").splitlines()
        if line.strip()
    ]

    browser_set = set(browser_files)
    plain_files = [f for f in all_files if f not in browser_set]
    plain_chunks = [
        plain_files[i:i + _CHUNK_GROUP_SIZE]
        for i in range(0, len(plain_files), _CHUNK_GROUP_SIZE)
    ]
    return plain_chunks, browser_files


@dataclass
class ChunkResult:
    name: str
    status: str  # "passed" | "test_failure" | "transport_error" | "capacity_refused"
    output: str
    returncode: int | None


async def _run_chunk(
    machine_id: str, name: str, files: list[str], timeout: int, floor_mb: int,
) -> ChunkResult:
    """Checked again immediately before running -- load can shift mid-run on
    a shared transport (spec §3). A capacity_refused chunk never reaches
    exec_command at all, so it can never be confused with a transport_error
    (SSH actually failing) or a test_failure (pytest actually ran)."""
    import tunnel_manager_ssh

    ok, available = await _check_capacity(machine_id, floor_mb)
    if not ok:
        seen = f"{available} MB" if available is not None else "unknown"
        return ChunkResult(
            name, "capacity_refused",
            f"only {seen} available, floor is {floor_mb} MB", None)

    file_args = " ".join(files)
    cmd = f"cd {QA_REMOTE_PATH} && .venv/bin/python -m pytest {file_args} -q --tb=short"
    try:
        _, stdout, stderr = await tunnel_manager_ssh.exec_command(
            machine_id, cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
    except Exception as exc:
        return ChunkResult(name, "transport_error", str(exc), None)
    status = "passed" if rc == 0 else "test_failure"
    return ChunkResult(name, status, out + err, rc)


async def execute(prepared: Prepared):
    """Sync, then every chunk, yielding one event per step. Holds
    _run_lock(prepared.machine_id) for the whole call -- released in
    `finally` whether the run finishes, fails, or the caller stops
    consuming early (an aborted HTTP connection cancels this generator,
    which still runs `finally`)."""
    import config

    lock = _run_lock(prepared.machine_id)
    await lock.acquire()
    try:
        yield {"type": "sync-start", "transport": prepared.transport["name"]}
        sync_result = await _sync(prepared)
        if not sync_result["ok"]:
            yield {"type": "run-done", "ok": False, "reason": sync_result["reason"]}
            return
        yield {"type": "sync-done", "files_changed": sync_result["files_changed"]}

        plain_chunks, browser_files = await _collect_chunks(prepared.machine_id)
        totals = {"passed": 0, "test_failure": 0, "transport_error": 0,
                  "capacity_refused": 0}

        async def _run_and_report(name: str, files: list[str]):
            yield {"type": "chunk-start", "name": name, "files": files}
            result = await _run_chunk(
                prepared.machine_id, name, files, config.QA_CHUNK_TIMEOUT,
                prepared.floor_mb)
            totals[result.status] += 1
            yield {
                "type": "chunk-result", "name": result.name,
                "status": result.status, "output": result.output,
            }

        for i, files in enumerate(plain_chunks, start=1):
            async for event in _run_and_report(f"plain-{i:02d}", files):
                yield event
        for f in browser_files:
            name = f"browser-{f.rsplit('/', 1)[-1].removesuffix('.py')}"
            async for event in _run_and_report(name, [f]):
                yield event

        ok = totals["test_failure"] == 0 and totals["transport_error"] == 0
        yield {"type": "run-done", "ok": ok, "totals": totals}
    finally:
        lock.release()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_chunks.py -q`
Expected: PASS, all tests.

- [ ] **Step 5: Run every qa_remote test file together**

Run: `.venv/bin/python -m pytest tests/test_qa_remote_capacity.py tests/test_qa_remote_lock_and_selection.py tests/test_qa_remote_sync.py tests/test_qa_remote_chunks.py -q`
Expected: PASS, all tests, no interaction failures between the files.

- [ ] **Step 6: Commit**

```bash
git status --porcelain qa_remote.py tests/test_qa_remote_chunks.py
git add qa_remote.py tests/test_qa_remote_chunks.py
git commit -m "feat: qa_remote chunked remote execution with 4-way status tagging"
```

---

### Task 7: `POST /api/qa/run` route

**Files:**
- Create: `routes/qa.py`
- Modify: `app.py` — import and `include_router` (mirror lines `app.py:60` and `app.py:487`).
- Test: `tests/test_qa_run_api.py` (new)

**Interfaces:**
- Consumes: `qa_remote.resolve_transport(owner, name) -> Prepared`, raising `qa_remote.QaRefusal` (Task 3); `qa_remote.execute(prepared) -> AsyncIterator[dict]` (Task 6).
- Produces: `POST /api/qa/run` — body `{"transport": "<name>"}` (optional key), owner from `request.state.session["user"]` (same pattern as every other route in `routes/transports.py`), `StreamingResponse` with `media_type="text/event-stream"`, one `data: <json>\n\n` line per `qa_remote.execute` event.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_qa_run_api.py`:

```python
"""QA: POST /api/qa/run -- owner scoping, refusal-to-status-code mapping,
and that the response streams incrementally rather than blocking until the
whole run finishes.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §4-§6.
"""
from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import auth
import config
import db
import qa_remote


def _client():
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url="https://testserver")


class QaRunApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(self.password))
        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")

    def _login(self):
        client = _client()
        resp = client.post("/login", json={"username": "admin", "password": self.password})
        self.assertEqual(resp.status_code, 200)
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def _parse_events(self, text: str) -> list[dict]:
        return [
            json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")
        ]

    async def test_refusal_from_resolve_transport_becomes_that_status_code(self):
        client, headers = self._login()
        with patch(
            "qa_remote.resolve_transport",
            AsyncMock(side_effect=qa_remote.QaRefusal(409, "One has no live tunnel")),
        ):
            resp = client.post("/api/qa/run", json={"transport": "t1"}, headers=headers)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("no live tunnel", resp.text)

    async def test_a_transport_owned_by_someone_else_is_not_found(self):
        """resolve_transport itself is owner-scoped (it reads via
        db.ssh_transports_list(owner)) -- this pins that a second user's
        request against the first user's transport name gets treated the
        same as a nonexistent one, never leaks whether the name exists."""
        other_password = secrets.token_urlsafe(16)
        await db.user_create("other", None, auth.hash_password(other_password))
        client = _client()
        resp = client.post("/login", json={"username": "other", "password": other_password})
        headers = {"X-CSRF-Token": client.cookies.get("wc_csrf")}

        resp = client.post("/api/qa/run", json={"transport": "One"}, headers=headers)
        self.assertEqual(resp.status_code, 404)

    async def test_successful_run_streams_events_in_order(self):
        prepared = qa_remote.Prepared(
            transport={"id": "t1", "name": "One"}, machine_id="m1", floor_mb=700)

        async def fake_events(_prepared):
            yield {"type": "sync-start", "transport": "One"}
            yield {"type": "sync-done", "files_changed": 0}
            yield {"type": "run-done", "ok": True, "totals": {}}

        client, headers = self._login()
        with (
            patch("qa_remote.resolve_transport", AsyncMock(return_value=prepared)),
            patch("qa_remote.execute", fake_events),
        ):
            resp = client.post("/api/qa/run", json={"transport": "t1"}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        events = self._parse_events(resp.text)
        self.assertEqual([e["type"] for e in events],
                         ["sync-start", "sync-done", "run-done"])

    async def test_no_transport_in_body_calls_resolve_transport_with_none(self):
        client, headers = self._login()
        with patch(
            "qa_remote.resolve_transport",
            AsyncMock(side_effect=qa_remote.QaRefusal(503, "none qualify")),
        ) as mocked:
            client.post("/api/qa/run", json={}, headers=headers)
        mocked.assert_awaited_once_with("admin", None)

    async def test_an_exception_mid_run_reaches_the_client_as_run_done(self):
        prepared = qa_remote.Prepared(
            transport={"id": "t1", "name": "One"}, machine_id="m1", floor_mb=700)

        async def failing_events(_prepared):
            yield {"type": "sync-start", "transport": "One"}
            raise RuntimeError("ssh connection reset")

        client, headers = self._login()
        with (
            patch("qa_remote.resolve_transport", AsyncMock(return_value=prepared)),
            patch("qa_remote.execute", failing_events),
        ):
            resp = client.post("/api/qa/run", json={"transport": "t1"}, headers=headers)
        events = self._parse_events(resp.text)
        self.assertEqual(events[-1]["type"], "run-done")
        self.assertFalse(events[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_run_api.py -q`
Expected: FAIL — `404 Not Found` for every request (route does not exist yet).

- [ ] **Step 3: Write `routes/qa.py`**

```python
"""POST /api/qa/run -- run the test suite on a remote transport instead of
this host. A thin HTTP wrapper around qa_remote.py, which is where the
actual orchestration lives (see its module docstring for why: it needs the
live app process's tunnel_manager._STATE).

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import qa_remote

_log = logging.getLogger("wc.qa")
router = APIRouter()


@router.post("/api/qa/run")
async def handle_qa_run(request: Request):
    session = request.state.session
    owner = session["user"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("transport") or None

    try:
        prepared = await qa_remote.resolve_transport(owner, name)
    except qa_remote.QaRefusal as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc

    async def event_stream():
        try:
            async for event in qa_remote.execute(prepared):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 -- a run dying must reach the
            # client as an event, not vanish into a 200 response with no body
            _log.exception("qa_run_failed transport=%s", prepared.transport.get("id"))
            yield f"data: {json.dumps({'type': 'run-done', 'ok': False, 'reason': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 4: Register the router in `app.py`**

Add the import next to the other `routes.*` imports (near `app.py:60`):

```python
from routes.qa import router as qa_router
```

Add the `include_router` call next to the others (near `app.py:487`):

```python
app.include_router(qa_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_run_api.py -q`
Expected: PASS, all tests.

- [ ] **Step 6: Run the full qa_remote + qa route test set together**

Run: `.venv/bin/python -m pytest tests/test_qa_transport_qa_column.py tests/test_qa_remote_capacity.py tests/test_qa_remote_lock_and_selection.py tests/test_qa_remote_sync.py tests/test_qa_remote_chunks.py tests/test_qa_run_api.py -q`
Expected: PASS, all tests.

- [ ] **Step 7: Commit**

```bash
git status --porcelain routes/qa.py app.py tests/test_qa_run_api.py
git add routes/qa.py app.py tests/test_qa_run_api.py
git commit -m "feat: POST /api/qa/run, streamed and owner-scoped"
```

---

### Task 8: `bin/wc-provision-qa.sh`

**Files:**
- Create: `bin/wc-provision-qa.sh`
- Test: `tests/test_qa_provision_script.py` (new)

**Interfaces:**
- Consumes: nothing from earlier tasks (standalone, its own one-shot SSH — spec §4 requires this, since provisioning must work on a transport that has never connected, unlike everything else in this plan).
- Produces: a remote `~/wc-qa-checkout/.venv` + Chromium, which `qa_remote._is_provisioned` (Task 2) checks for.

- [ ] **Step 1: Write the failing shape tests**

Create `tests/test_qa_provision_script.py`, mirroring
`tests/test_qa_deploy_entrypoint.py`'s source-scanning style (a real
provisioning run needs a live SSH host, so this pins the script's shape and
safety properties instead — the same choice that file already made for
`wc-deploy.sh`):

```python
"""QA: bin/wc-provision-qa.sh exists, is executable, is idempotent, and
resolves connection details the one way this project allows -- from
ssh_transports, never re-derived.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §1.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-provision-qa.sh"


def _tracked_mode(rel_path: str) -> str:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.split()[0]


class ProvisionScriptShapeTests(unittest.TestCase):
    def test_the_script_is_present(self):
        self.assertTrue(SCRIPT.is_file())

    def test_it_is_executable_where_it_counts(self):
        mode = _tracked_mode("bin/wc-provision-qa.sh")
        if not mode:
            self.skipTest("not a git checkout")
        self.assertEqual(mode, "100755")

    def test_it_resolves_connection_details_from_ssh_transports_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ssh_transports", source)

    def test_it_creates_a_venv_under_the_qa_checkout_path(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("wc-qa-checkout", source)
        self.assertIn("venv", source)

    def test_it_installs_both_requirement_files(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("requirements.txt", source)
        self.assertIn("requirements-dev.txt", source)

    def test_it_installs_chromium(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("playwright install chromium", source)

    def test_it_checks_before_redoing_the_expensive_parts(self):
        """Idempotent, mirroring wc-deploy-proxy.sh's own 'safe to press
        again' property -- a pip install plus a Chromium download is real
        time and bandwidth, not something to repeat unconditionally."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(".venv/bin/python", source)
        self.assertTrue(
            "if [ -x" in source or "if [ ! -x" in source or "command -v" in source,
            "no existence check found before the expensive setup steps")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_provision_script.py -q`
Expected: FAIL — script does not exist.

- [ ] **Step 3: Write `bin/wc-provision-qa.sh`**

```bash
#!/usr/bin/env bash
# One-time per-transport QA environment setup: a Python venv and Chromium
# under ~/wc-qa-checkout on the transport, so a later QA run only has to
# sync files and run pytest, never install anything.
#
# Usage: bin/wc-provision-qa.sh <transport-name>
#        bin/wc-provision-qa.sh --list
#
# Deliberately separate from every QA run (bin/wc-run-suite-remote.sh):
# a pip install plus a Chromium download is real time and bandwidth, worth
# paying once, not on every invocation. Run by hand, before a transport is
# usable for QA -- not triggered automatically by a sync or a run, so a
# provisioning failure (disk full, network flaky) is diagnosable on its own.
#
# Same shape as wc-deploy-proxy.sh, for the same reason: ssh_host/ssh_user/
# ssh_key_path always come from ssh_transports, never re-derived a second
# way, and this has to work on a transport nothing has connected to yet --
# it opens its own one-shot SSH connection rather than depending on
# tunnel_manager's live one.
#
# Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §1.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${WC_DB_PATH:-$PWD/data/webconsole.db}"
PY="${WC_PYTHON:-.venv/bin/python}"
REMOTE_DIR="wc-qa-checkout"

_die() { echo "wc-provision-qa: $*" >&2; exit 1; }

_query() {
    "$PY" - "$@" <<'PY'
import sqlite3, sys
db, which, name = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
if which == "list":
    for r in con.execute("SELECT name, ssh_user, ssh_host FROM ssh_transports ORDER BY name"):
        print(f"{r['name']}\t{r['ssh_user']}@{r['ssh_host']}")
elif which == "transport":
    r = con.execute(
        "SELECT ssh_host, ssh_user, ssh_key_path FROM ssh_transports WHERE name = ?",
        (name,),
    ).fetchone()
    if not r:
        sys.exit(3)
    print("\t".join([r["ssh_host"] or "", r["ssh_user"] or "", r["ssh_key_path"] or ""]))
con.close()
PY
}

[ -f "$DB" ] || _die "no database at $DB"

if [ "${1:-}" = "--list" ] || [ $# -eq 0 ]; then
    echo "transports in $DB:"
    _query "$DB" list | sed 's/^/  /'
    [ $# -eq 0 ] && _die "name a transport (or --list)"
    exit 0
fi

NAME="$1"
if ! row="$(_query "$DB" transport "$NAME")"; then
    _die "no transport named '$NAME' (try --list)"
fi
IFS=$'\t' read -r SSH_HOST SSH_USER SSH_KEY <<<"$row"
[ -n "$SSH_HOST" ] || _die "transport '$NAME' has no ssh_host"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
[ -r "$SSH_KEY" ] || _die "ssh key not readable: $SSH_KEY"

TARGET="$SSH_USER@$SSH_HOST"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -i "$SSH_KEY")

echo "wc-provision-qa: $NAME -> $TARGET"

ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<REMOTE
set -euo pipefail
mkdir -p ~/$REMOTE_DIR

if [ -x ~/$REMOTE_DIR/.venv/bin/python ]; then
  echo "  venv already present, skipping creation"
else
  echo "  creating venv"
  python3 -m venv ~/$REMOTE_DIR/.venv
fi
REMOTE

echo "  syncing requirements files"
scp "${SSH_OPTS[@]}" requirements.txt requirements-dev.txt "$TARGET:$REMOTE_DIR/"

ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<REMOTE
set -euo pipefail
cd ~/$REMOTE_DIR
echo "  installing requirements (this can take a while)"
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt

if .venv/bin/python -c 'from playwright.sync_api import sync_playwright; sync_playwright().start().chromium.executable_path' >/dev/null 2>&1; then
  echo "  chromium already present, skipping download"
else
  echo "  installing chromium (this can take a while)"
  .venv/bin/playwright install chromium
fi
REMOTE

echo "wc-provision-qa: $NAME provisioned"
```

- [ ] **Step 4: Make the script executable and stage the mode**

```bash
chmod +x bin/wc-provision-qa.sh
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_provision_script.py -q`
Expected: PASS, all tests (the executable-mode test only passes once the
file is both `chmod +x`'d *and* staged with `git add`, since it reads the
tracked mode via `git ls-files -s` — stage before this step if the test
still fails on mode).

- [ ] **Step 6: Commit**

```bash
git status --porcelain bin/wc-provision-qa.sh tests/test_qa_provision_script.py
git add bin/wc-provision-qa.sh tests/test_qa_provision_script.py
git commit -m "feat: bin/wc-provision-qa.sh, one-time per-transport QA environment setup"
```

---

### Task 9: `bin/wc-run-suite-remote.sh`

**Files:**
- Create: `bin/wc-run-suite-remote.sh`
- Test: `tests/test_qa_run_remote_script.py` (new)

**Interfaces:**
- Consumes: `bin/wc-token.py create --user <user> --name <name> --days 1 --out <file>` (existing CLI), `POST /api/qa/run` (Task 7).
- Produces: a CLI entry point Pedro (or a `cweb*` session) runs by hand:
  `bin/wc-run-suite-remote.sh [transport-name]`.

- [ ] **Step 1: Write the failing shape tests**

Create `tests/test_qa_run_remote_script.py`:

```python
"""QA: bin/wc-run-suite-remote.sh -- shape and safety properties. A live run
needs a running server and a real transport, so (mirroring
tests/test_qa_deploy_entrypoint.py and tests/test_qa_provision_script.py)
this pins what can be checked from the file itself: it mints a short-lived
token rather than the no-expiry default, and it is an HTTP client, not its
own SSH client.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §4, §7.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-run-suite-remote.sh"


def _tracked_mode(rel_path: str) -> str:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.split()[0]


class RunRemoteScriptShapeTests(unittest.TestCase):
    def test_the_script_is_present(self):
        self.assertTrue(SCRIPT.is_file())

    def test_it_is_executable_where_it_counts(self):
        mode = _tracked_mode("bin/wc-run-suite-remote.sh")
        if not mode:
            self.skipTest("not a git checkout")
        self.assertEqual(mode, "100755")

    def test_it_mints_a_token_with_a_pinned_short_expiry(self):
        """"short-lived" without a number was the gap the spec review
        caught (§7) -- this pins the actual number rather than trusting the
        adjective."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("wc-token.py", source)
        self.assertIn("--days 1", source)

    def test_it_never_calls_ssh_directly(self):
        """Cannot open its own SSH connection to the transport -- exec_command/
        sync_transport are bound to the running app process's own in-memory
        tunnel state (spec §4). This is an HTTP client only."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?<!#\s)\bssh\s")

    def test_it_posts_to_the_qa_run_route(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/api/qa/run", source)

    def test_it_sends_the_token_as_a_bearer_header(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Authorization: Bearer", source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_run_remote_script.py -q`
Expected: FAIL — script does not exist.

- [ ] **Step 3: Write `bin/wc-run-suite-remote.sh`**

```bash
#!/usr/bin/env bash
# Run the test suite on a remote transport instead of this host.
#
# Usage: bin/wc-run-suite-remote.sh [transport-name]
#
# Cannot open its own SSH connection to the transport: exec_command,
# open_sftp and sync_transport are all bound to the running
# webconsole.service process's own in-memory tunnel_manager._STATE, which
# a standalone script has no access to (spec §4). So this is an HTTP
# client only -- it mints a short-lived API token and POSTs to the running
# server's own /api/qa/run, printing the streamed response as it arrives.
#
# Token expiry is pinned to 1 day rather than left at wc-token.py's
# no-expiry default: a token left in a script's environment or a stray log
# line should be a bounded exposure, not a standing one (spec §7). Note
# what this does NOT do: scope the token to only this route -- no
# per-route scoping mechanism exists in this codebase today, so the token
# carries whatever role WC_QA_USER has, same as every other wc-token.py
# consumer.
#
# Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${WC_BASE_URL:-http://127.0.0.1:8080}"
QA_USER="${WC_QA_USER:-admin}"

TOKEN_FILE="$(mktemp)"
trap 'rm -f "$TOKEN_FILE"' EXIT

bin/wc-token.py create --user "$QA_USER" --name "qa-run-$(date +%s)" \
  --days 1 --out "$TOKEN_FILE" >&2
TOKEN="$(cat "$TOKEN_FILE")"

BODY='{}'
if [ $# -ge 1 ]; then
  BODY=$(.venv/bin/python -c 'import json,sys; print(json.dumps({"transport": sys.argv[1]}))' "$1")
fi

echo "wc-run-suite-remote: POSTing to $BASE_URL/api/qa/run" >&2

curl -N -sS -X POST "$BASE_URL/api/qa/run" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$BODY" | while IFS= read -r line; do
    case "$line" in
      data:*) echo "${line#data: }" ;;
    esac
  done

echo "wc-run-suite-remote: connection closed" >&2
echo "wc-run-suite-remote: if the run did not report run-done above, it was" >&2
echo "  interrupted (e.g. a webconsole.service restart) -- re-run to retry;" >&2
echo "  there is no resume-in-place (spec §6)." >&2
```

- [ ] **Step 4: Make the script executable**

```bash
chmod +x bin/wc-run-suite-remote.sh
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_run_remote_script.py -q`
Expected: PASS, all tests.

- [ ] **Step 6: Commit**

```bash
git status --porcelain bin/wc-run-suite-remote.sh tests/test_qa_run_remote_script.py
git add bin/wc-run-suite-remote.sh tests/test_qa_run_remote_script.py
git commit -m "feat: bin/wc-run-suite-remote.sh, thin CLI wrapper around POST /api/qa/run"
```

---

### Task 10: Full targeted regression pass

**Files:** none (verification only).

- [ ] **Step 1: Run every test file this plan touched or created, together**

```bash
.venv/bin/python -m pytest \
  tests/test_db_transports.py \
  tests/test_qa_transport_qa_column.py \
  tests/test_qa_remote_capacity.py \
  tests/test_qa_remote_lock_and_selection.py \
  tests/test_qa_remote_sync.py \
  tests/test_qa_remote_chunks.py \
  tests/test_qa_run_api.py \
  tests/test_qa_provision_script.py \
  tests/test_qa_run_remote_script.py \
  tests/test_transport_sync_api.py \
  tests/test_db_transport_sync.py \
  -q --tb=short
```

Expected: PASS, all tests, no regressions in the pre-existing transport-sync
tests this plan's new column/setter sit beside.

- [ ] **Step 2: Confirm nothing else in the repo references the old-shape
  assumptions this plan changed**

```bash
grep -rn "ssh_transports" routes/*.py db.py | grep -v "_qa_" | grep -iv "test"
```

Read through the output — confirm every non-test reference to
`ssh_transports` still matches `_TRANSPORT_COLUMNS`'s new shape (the column
list grew by one; nothing should be indexing columns positionally instead of
by name, since every query in this codebase already selects by name).

- [ ] **Step 3: Check for peer edits on every file this plan touched, before
  any further work**

```bash
git status --porcelain \
  config.py db.py routes/db_transports.py routes/qa.py app.py \
  qa_remote.py bin/wc-provision-qa.sh bin/wc-run-suite-remote.sh \
  tests/test_db_transports.py tests/test_qa_transport_qa_column.py \
  tests/test_qa_remote_capacity.py tests/test_qa_remote_lock_and_selection.py \
  tests/test_qa_remote_sync.py tests/test_qa_remote_chunks.py \
  tests/test_qa_run_api.py tests/test_qa_provision_script.py \
  tests/test_qa_run_remote_script.py
```

Expected: empty (everything already committed task-by-task) — this step is a
final confirmation, not a new commit.

- [ ] **Step 4: Report to the user**

State plainly: which of the 10 tasks are done, the full test count from Step
1, and the two things this plan explicitly does not attempt — full-suite
equivalence with a single local run (spec's "Explicit limitation"), and any
form of run resumability across a mid-run `webconsole.service` restart (spec
§6). Both are accepted, stated limitations, not gaps to silently work around.

---

## Self-Review

**Spec coverage:**
- §1 Provisioning → Task 8.
- §2 Sync → Task 1 (column/setter), Task 4 (`_sync`).
- §3 Capacity check → Task 2 (`_available_mb`/`_check_capacity`), Task 5
  (config default), Task 6 (per-chunk re-check).
- §4 Where this runs / preconditions → Task 2 (`_is_provisioned`), Task 3
  (liveness + provisioning + lock ordering in `_check_named`), Task 7 (route
  runs inside the app process).
- §5 Node selection + lock → Task 3.
- §6 Execution, streaming, restart behavior, status tagging → Task 6, Task 7.
- §7 Security / token expiry → Task 9.
- All five gap-fix behaviors named in the brainstorming session (streamed
  response + restart behavior, provisioning precheck, per-transport lock,
  pinned 1-day token expiry, four-way chunk status tagging) each have a task
  and explicit tests, not folded silently into another task.

**Placeholder scan:** no TBD/TODO; every step has literal code, not a
description of code.

**Type consistency:** `Prepared(transport, machine_id, floor_mb)` is defined
once (Task 3) and consumed with that exact shape in Task 4, Task 6, and
Task 7's tests. `ChunkResult(name, status, output, returncode)` is defined
once (Task 6) and its `status` literal set (`passed`/`test_failure`/
`transport_error`/`capacity_refused`) is used identically in Task 6's events
and nowhere redefined. `QaRefusal(status_code, reason)` is defined once
(Task 2) and every refusal in Task 3 raises it with the same two positional
args; Task 7 is the only place it is caught and translated to `HTTPException`.

**Scope check:** ten tasks, each independently testable, none touching
`transport_sync.py`/`tunnel_manager_ssh.py`/`run-suite-chunked.sh` — matches
the spec's own "Files touched" list exactly, plus the `config.py` constants
task the spec's Task list implied but didn't call out as its own line item
(added here as Task 5 rather than folded into Task 2, since it is a genuinely
separate, independently reviewable change).
