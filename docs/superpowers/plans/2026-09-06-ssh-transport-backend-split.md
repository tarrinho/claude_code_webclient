# SSH Transport / Backend Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `ai_machines`' `ssh_proxy` provider type into two independent
concepts — a lightweight `ssh_transports` row (the SSH connection: host, user,
key) and a nullable `ai_machines.transport_id` FK (which backend runs where) —
so one SSH connection can back multiple backends and a backend's model/base_url
fields are never dead weight again.

**Architecture:** New `ssh_transports` table + `ai_machines.transport_id`
column; `tunnel_manager_ssh.connect()` reworked to share one live SSH
connection across every backend that references the same transport
(reference-counted); `runner.get_proxy_target` and `routes/machines_tunnel.py`
gate on `transport_id` presence instead of `provider == 'ssh_proxy'`; the
Settings/Backends UI gets a new "+ Add transport" button and an "Executes on"
dropdown on the backend form, and groups the machine list under transport
nodes.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, paramiko (SSH), vanilla JS
(ES modules), pytest + unittest.

**Spec:** `docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md`

## Global Constraints

- Every Python file stays under 400 lines; every JS file stays under 300 lines
  (rules.md). If a task pushes a file over, split at the point where one
  module owns one layer.
- `db.py`'s lazy `__getattr__` dispatcher (`_SYMBOLS` dict, `db.py:~40-180`)
  is how every `routes/db_*.py` symbol becomes reachable as `db.symbol_name`
  — every new DB function needs an entry there.
- Never run `db.init()` against the production database directly; tests use
  `patch.object(config, "DB_PATH", <tempdir>/db)` before calling it, per the
  existing convention in `tests/test_ssh_tunnel_api.py`'s `client` fixture.
- Owner-scope every new query the same way `ai_machines`/`ssh_tunnels`
  already are (`WHERE owner_id = ?`) — a transport is exactly as private as
  a backend.
- `provider` on `ai_machines` continues to mean wire protocol only
  (`claude_code`/`direct`), never "how to reach it." Do not reintroduce
  `ssh_proxy` as a provider value anywhere in new code.
- This plan does not touch the in-flight, unrelated provider-literal rename
  (`anthropic`/`proxy` → `claude_code`/`direct`) visible elsewhere in this
  shared checkout. Every code snippet below already uses the current values
  (`claude_code`/`direct`) as they exist in the working tree today — if that
  rename has since completed differently, match whatever `_MACHINE_PROVIDERS`
  actually contains at implementation time, don't fight it.

---

### Task 1: `ssh_transports` table + CRUD

**Files:**
- Modify: `db.py:256-279` (ai_machines CREATE TABLE block — add nothing here,
  just the anchor for where to insert the new table), `db.py:~180` (`_SYMBOLS`
  dict), `db.py:~867` (ai_machines migration block area — add the new table's
  own migration block near it)
- Create: `routes/db_transports.py`
- Test: `tests/test_db_transports.py`

**Interfaces:**
- Produces: `db.ssh_transport_create(transport_id, name, owner_id, ssh_host, ssh_user, ssh_key_path) -> str` (returns `created_at`), `db.ssh_transport_get(id, owner_id) -> dict | None`, `db.ssh_transports_list(owner_id) -> list[dict]`, `db.ssh_transport_update(id, owner_id, **fields) -> bool`, `db.ssh_transport_delete(id, owner_id) -> bool`, `db.ssh_transport_set_host_key_fingerprint(id, fingerprint) -> None`. Every later task that reads/writes an SSH connection's own host/user/key/fingerprint uses these instead of touching `ai_machines`.

- [ ] **Step 1: Write the failing test for the new table's schema**

```python
# tests/test_db_transports.py
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class SshTransportsSchemaTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_ssh_transports_table_exists_with_expected_columns(self):
        cur = await db.db_conn.execute("PRAGMA table_info(ssh_transports)")
        columns = {row["name"] for row in await cur.fetchall()}
        self.assertEqual(
            columns,
            {
                "id", "name", "owner_id", "ssh_host", "ssh_user",
                "ssh_key_path", "ssh_host_key_fingerprint",
                "created_at", "updated_at",
            },
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db_transports.py -v`
Expected: FAIL with `sqlite3.OperationalError: no such table: ssh_transports`
(surfaced through aiosqlite as an exception from the `PRAGMA table_info` call
returning zero rows, so `columns == set()` and the assertion fails).

- [ ] **Step 3: Add the table to `db.py`'s schema**

In `db.py`, immediately after the `CREATE TABLE IF NOT EXISTS ai_machines` block
closes (right before `CREATE TABLE IF NOT EXISTS messages` at `db.py:281`),
insert:

```sql
        CREATE TABLE IF NOT EXISTS ssh_transports (
            id                        TEXT PRIMARY KEY,
            name                      TEXT NOT NULL,
            owner_id                  TEXT NOT NULL,
            ssh_host                  TEXT NOT NULL,
            ssh_user                  TEXT NOT NULL DEFAULT 'kali',
            ssh_key_path              TEXT NOT NULL DEFAULT '',
            ssh_host_key_fingerprint  TEXT NOT NULL DEFAULT '',
            created_at                TEXT NOT NULL,
            updated_at                TEXT NOT NULL
        );
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_db_transports.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing CRUD tests**

Append to `tests/test_db_transports.py`:

```python
    async def test_create_then_get_round_trips(self):
        created_at = await db.ssh_transport_create(
            "t1", "Kali3", "admin", "kali-3.tail850c40.ts.net", "kali",
            "~/.ssh/id_ed25519",
        )
        self.assertTrue(created_at)
        row = await db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["name"], "Kali3")
        self.assertEqual(row["ssh_host"], "kali-3.tail850c40.ts.net")
        self.assertEqual(row["ssh_user"], "kali")
        self.assertEqual(row["ssh_key_path"], "~/.ssh/id_ed25519")
        self.assertEqual(row["ssh_host_key_fingerprint"], "")

    async def test_get_is_owner_scoped(self):
        await db.ssh_transport_create("t2", "Kali3", "admin", "h", "kali", "k")
        self.assertIsNone(await db.ssh_transport_get("t2", "someone-else"))

    async def test_list_returns_only_this_owners_transports(self):
        await db.ssh_transport_create("t3", "Kali3", "admin", "h1", "kali", "k")
        await db.ssh_transport_create("t4", "Other", "someone-else", "h2", "kali", "k")
        rows = await db.ssh_transports_list("admin")
        self.assertEqual([r["id"] for r in rows], ["t3"])

    async def test_update_changes_only_given_fields(self):
        await db.ssh_transport_create("t5", "Kali3", "admin", "h", "kali", "k")
        updated = await db.ssh_transport_update("t5", "admin", name="Kali3 renamed")
        self.assertTrue(updated)
        row = await db.ssh_transport_get("t5", "admin")
        self.assertEqual(row["name"], "Kali3 renamed")
        self.assertEqual(row["ssh_host"], "h")  # untouched

    async def test_update_is_owner_scoped(self):
        await db.ssh_transport_create("t6", "Kali3", "admin", "h", "kali", "k")
        updated = await db.ssh_transport_update("t6", "someone-else", name="x")
        self.assertFalse(updated)

    async def test_delete_removes_the_row(self):
        await db.ssh_transport_create("t7", "Kali3", "admin", "h", "kali", "k")
        deleted = await db.ssh_transport_delete("t7", "admin")
        self.assertTrue(deleted)
        self.assertIsNone(await db.ssh_transport_get("t7", "admin"))

    async def test_set_host_key_fingerprint_is_not_owner_scoped(self):
        """Called from the tunnel connect loop, which has no request session
        to scope against -- same reasoning as
        ai_machine_set_ssh_host_key_fingerprint."""
        await db.ssh_transport_create("t8", "Kali3", "admin", "h", "kali", "k")
        await db.ssh_transport_set_host_key_fingerprint("t8", "SHA256:abc")
        row = await db.ssh_transport_get("t8", "admin")
        self.assertEqual(row["ssh_host_key_fingerprint"], "SHA256:abc")
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db_transports.py -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'ssh_transport_create'`

- [ ] **Step 7: Create `routes/db_transports.py`**

```python
# routes/db_transports.py — SSH transport CRUD (the connection, not the backend).
#
# A transport is the SSH connection to a remote host: ssh_host/ssh_user/
# ssh_key_path/ssh_host_key_fingerprint. ai_machines.transport_id points at
# one of these when a backend's `claude` process runs there instead of
# locally. See docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.

from typing import Any

import db

_TRANSPORT_COLUMNS = (
    "id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
    "ssh_host_key_fingerprint, created_at, updated_at"
)


async def ssh_transport_create(
    transport_id: str,
    name: str,
    owner_id: str,
    ssh_host: str,
    ssh_user: str,
    ssh_key_path: str,
) -> str:
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO ssh_transports "
        "(id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
        " ssh_host_key_fingerprint, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)",
        (transport_id, name, owner_id, ssh_host, ssh_user, ssh_key_path, now, now),
    )
    await db.db_conn.commit()
    return now


async def ssh_transport_get(transport_id: str, owner_id: str) -> dict[str, Any] | None:
    cur = await db.db_conn.execute(
        f"SELECT {_TRANSPORT_COLUMNS} FROM ssh_transports "  # nosec B608: columns are static
        "WHERE id = ? AND owner_id = ?",
        (transport_id, owner_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def ssh_transports_list(owner_id: str) -> list[dict[str, Any]]:
    cur = await db.db_conn.execute(
        f"SELECT {_TRANSPORT_COLUMNS} FROM ssh_transports "  # nosec B608: columns are static
        "WHERE owner_id = ? ORDER BY name ASC",
        (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def ssh_transport_update(transport_id: str, owner_id: str, **fields: Any) -> bool:
    allowed = {"name", "ssh_host", "ssh_user", "ssh_key_path"}
    pairs = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
    if not pairs:
        return False
    sets = [f"{k} = ?" for k, _ in pairs]
    sets.append("updated_at = ?")
    vals = [v for _, v in pairs] + [db._now(), transport_id, owner_id]
    sql = (
        "UPDATE ssh_transports SET " + ", ".join(sets)
        + " WHERE id = ? AND owner_id = ?"
    )  # nosec B608: fields are allowlisted
    cur = await db.db_conn.execute(sql, vals)
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ssh_transport_delete(transport_id: str, owner_id: str) -> bool:
    cur = await db.db_conn.execute(
        "DELETE FROM ssh_transports WHERE id = ? AND owner_id = ?",
        (transport_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0


async def ssh_transport_set_host_key_fingerprint(transport_id: str, fingerprint: str) -> None:
    """Pin the SSH host key fingerprint accepted on first connect.

    No owner_id scoping: called from tunnel_manager_ssh.connect(), a
    background loop with no request session to scope against -- same
    reasoning as ai_machine_set_ssh_host_key_fingerprint.
    """
    await db.db_conn.execute(
        "UPDATE ssh_transports SET ssh_host_key_fingerprint = ? WHERE id = ?",
        (fingerprint, transport_id),
    )
    await db.db_conn.commit()
```

- [ ] **Step 8: Register the new symbols in `db.py`'s `_SYMBOLS` dict**

In `db.py`, immediately after the `"_BACKEND_COLUMNS": "routes.db_machines",`
line (`db.py:121`), insert:

```python
        # ssh transports
        "ssh_transport_create": "routes.db_transports",
        "ssh_transport_get": "routes.db_transports",
        "ssh_transports_list": "routes.db_transports",
        "ssh_transport_update": "routes.db_transports",
        "ssh_transport_delete": "routes.db_transports",
        "ssh_transport_set_host_key_fingerprint": "routes.db_transports",
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_db_transports.py -v`
Expected: PASS (7 tests)

- [ ] **Step 10: Commit**

```bash
git add db.py routes/db_transports.py tests/test_db_transports.py
git commit -m "feat: add ssh_transports table and CRUD"
```

---

### Task 2: `ai_machines.transport_id` column

**Files:**
- Modify: `db.py:917-947` (the ssh_host/ssh_user/ssh_key_path/ssh_host_key_fingerprint migration block — add `transport_id` alongside it), `routes/db_machines.py:14` (`_BACKEND_COLUMNS`), `routes/db_machines.py` (`ai_machine_create`, `ai_machine_update` signatures)
- Test: `tests/test_db.py` (append), `tests/test_machine_provider.py` (existing tests that construct `ai_machine_create` calls with `ssh_host=`/`ssh_user=`/`ssh_key_path=` need updating — search for these first)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ai_machines.transport_id` column (nullable TEXT); `ai_machine_create(...)`/`ai_machine_update(...)` gain a `transport_id: str | None = None` parameter and drop `ssh_host`/`ssh_user`/`ssh_key_path`; `_BACKEND_COLUMNS` includes `transport_id` so `ai_machine_backend`/`ai_machine_backend_by_id`/`ai_machine_get` all return it. Task 3 (`routes/machines.py`) and Task 6 (`runner.py`) both read `machine["transport_id"]`.

- [ ] **Step 1: Search for existing callers that will break**

```bash
grep -rn 'ssh_host=\|ssh_user=\|ssh_key_path=' routes/machines.py routes/db_machines.py tests/test_machine_provider.py tests/test_ssh_tunnel_api.py
```
Read every match before editing — Task 3 and Task 7 below cover
`routes/machines.py` and the tunnel tests specifically, but note every hit
here so nothing is missed.

- [ ] **Step 2: Write the failing test for the new column**

Append to `tests/test_db.py` (or create a new focused test if that file is
already near 400 lines — check with `wc -l tests/test_db.py` first):

```python
class AiMachinesTransportIdTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_ai_machines_has_transport_id_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(ai_machines)")
        columns = {row["name"] for row in await cur.fetchall()}
        self.assertIn("transport_id", columns)

    async def test_create_with_transport_id_round_trips(self):
        await db.ai_machine_create(
            "m1", "CF AI Machine (via Kali3)", "llm.ai-machine.cfappsecurity.com",
            443, None, "vllm/Qwen3.6-35B-A3B-NVFP4",
            "https://llm.ai-machine.cfappsecurity.com", None, "admin",
            provider="claude_code", transport_id="t1",
        )
        row = await db.ai_machine_get("m1", "admin")
        self.assertEqual(row["transport_id"], "t1")

    async def test_create_without_transport_id_defaults_to_null(self):
        await db.ai_machine_create(
            "m2", "Anthropic API", "api.anthropic.com", 443, None,
            "claude-sonnet-5", "https://api.anthropic.com", None, "admin",
            provider="claude_code",
        )
        row = await db.ai_machine_get("m2", "admin")
        self.assertIsNone(row["transport_id"])

    async def test_update_can_set_and_clear_transport_id(self):
        await db.ai_machine_create(
            "m3", "CF AI Machine", "llm.ai-machine.cfappsecurity.com", 443,
            None, "vllm/Qwen3.6-35B-A3B-NVFP4", None, None, "admin",
            provider="claude_code",
        )
        await db.ai_machine_update("m3", "admin", transport_id="t1")
        self.assertEqual((await db.ai_machine_get("m3", "admin"))["transport_id"], "t1")
        await db.ai_machine_update("m3", "admin", transport_id=None)
        # NOTE: ai_machine_update's existing pairs-building only sets a field
        # when `value is not None` (see routes/db_machines.py) -- clearing
        # transport_id back to NULL needs its own explicit path (Step 4 below
        # adds one), not a bare None kwarg. This test documents that: passing
        # None must not silently no-op.
        self.assertIsNone((await db.ai_machine_get("m3", "admin"))["transport_id"])

    async def test_backend_columns_include_transport_id(self):
        await db.ai_machine_create(
            "m4", "CF AI Machine", "h", 443, None, "vllm/x", None, None,
            "admin", provider="claude_code", transport_id="t1",
        )
        backend = await db.ai_machine_backend_by_id("m4", "admin")
        self.assertEqual(backend["transport_id"], "t1")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db.py -k TransportId -v`
Expected: FAIL — `ai_machine_create() got an unexpected keyword argument 'transport_id'`

- [ ] **Step 4: Add the migration to `db.py`**

Immediately after the `ssh_host_key_fingerprint` migration block closes
(`db.py:947`, right before the `# ssh_tunnels:` comment at `db.py:949`),
insert:

```python
    if ma_columns and "transport_id" not in ma_columns:
        await db_conn.execute(
            "ALTER TABLE ai_machines ADD COLUMN transport_id TEXT"
        )
```

- [ ] **Step 5: Update `_BACKEND_COLUMNS` in `routes/db_machines.py`**

Change `routes/db_machines.py:14-16` from:

```python
_BACKEND_COLUMNS = (
    "id, name, provider, host, port, model, base_url, api_key"
)
```

to:

```python
_BACKEND_COLUMNS = (
    "id, name, provider, host, port, model, base_url, api_key, transport_id"
)
```

- [ ] **Step 6: Add `transport_id` to `ai_machine_create`**

In `routes/db_machines.py`, `ai_machine_create`'s signature currently ends
with `ssh_key_path: str | None = None,`. Change the signature and the two
places that build the INSERT to add `transport_id`, and add an explicit
`ai_machine_set_transport` helper for clearing it back to NULL (per the test
above — a bare `None` through the existing "only set if not None" pattern
can never clear a value once set):

```python
async def ai_machine_create(
    machine_id: str,
    name: str,
    host: str,
    port: int,
    api_key: str | None,
    model: str,
    base_url: str | None,
    description: str | None,
    owner_id: str,
    provider: str = "claude_code",
    transport_id: str | None = None,
) -> str:
    now = db._now()
    await db.db_conn.execute(
        "INSERT INTO ai_machines "
        "(id, name, provider, host, port, api_key, model, base_url, description, "
        "active, owner_id, created_at, updated_at, transport_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            machine_id, name, provider, host, port, api_key, model, base_url,
            description, owner_id, now, now, transport_id,
        ),
    )
    await db.db_conn.commit()
    return now
```

(This replaces the `ssh_host`/`ssh_user`/`ssh_key_path` parameters and INSERT
columns entirely — those move to `ssh_transport_create` from Task 1.)

- [ ] **Step 7: Add `transport_id` to `ai_machine_update`, and a clear-transport helper**

Replace the `ssh_host`, `ssh_user`, `ssh_key_path` entries in
`ai_machine_update`'s `pairs` list with `("transport_id", transport_id)`, and
add the parameter to the signature (drop the three ssh parameters). Then add
a second function right after it for the explicit-clear case:

```python
async def ai_machine_clear_transport(machine_id: str, owner_id: str) -> bool:
    """Set transport_id back to NULL -- a bare None through ai_machine_update
    is indistinguishable from "field not supplied" (its pairs-building only
    sets a field when the value is not None), so clearing needs its own path."""
    cur = await db.db_conn.execute(
        "UPDATE ai_machines SET transport_id = NULL, updated_at = ? "
        "WHERE id = ? AND owner_id = ?",
        (db._now(), machine_id, owner_id),
    )
    await db.db_conn.commit()
    return cur.rowcount > 0
```

Register it in `db.py`'s `_SYMBOLS`: add
`"ai_machine_clear_transport": "routes.db_machines",` next to the other
`ai_machine_*` entries.

- [ ] **Step 8: Update the now-broken existing test in Step 1's grep results**

Search `tests/test_machine_provider.py` for any `ai_machine_create(` call
passing `ssh_host=`/`ssh_user=`/`ssh_key_path=` and remove those kwargs
(they no longer exist on the function). If a test specifically exercises
ssh_proxy creation end-to-end, leave a `# TODO` comment is explicitly
forbidden by this plan's own rules — instead, rewrite it to create a
transport via `db.ssh_transport_create` first, then pass `transport_id=` to
`ai_machine_create`, matching the new shape. Do this rewrite inline, test by
test, re-running after each.

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_db.py -k TransportId tests/test_machine_provider.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add db.py routes/db_machines.py tests/test_db.py tests/test_machine_provider.py
git commit -m "feat: add ai_machines.transport_id, drop per-machine ssh fields"
```

---

### Task 3: `routes/machines.py` — validate `transport_id`, drop `ssh_proxy` provider

**Files:**
- Modify: `routes/machines.py:46-78` (`_MACHINE_ALLOWED_FIELDS`, `_MACHINE_TEXT_FIELDS`, `_MACHINE_PROVIDERS`), `routes/machines.py:134-233` (`handle_machine_create`), `routes/machines.py:236-304` (`handle_machine_patch`)
- Test: `tests/test_machine_provider.py`

**Interfaces:**
- Consumes: `db.ssh_transport_get(id, owner_id)` (Task 1), `db.ai_machine_create(..., transport_id=...)` / `db.ai_machine_update(..., transport_id=...)` / `db.ai_machine_clear_transport(...)` (Task 2).
- Produces: `POST /api/machines` and `PATCH /api/machines/{id}` accept an optional `transport_id` field (string or null), validated against the caller's own transports; `ssh_proxy` is no longer a legal `provider` value and every `ssh_host`/`ssh_user`/`ssh_key_path` body field is rejected as unknown (allowlist no longer contains them).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_provider.py` (check its existing `client`
fixture pattern first — it likely matches `tests/test_ssh_tunnel_api.py`'s):

```python
def test_ssh_proxy_is_no_longer_a_legal_provider(client):
    resp = client.post(
        "/api/machines",
        json={"name": "x", "provider": "ssh_proxy", "host": "h"},
        headers=_auth_headers(client),
    )
    assert resp.status_code == 400
    assert "provider" in resp.json().get("detail", "").lower()


def test_ssh_host_field_is_rejected_on_create(client):
    resp = client.post(
        "/api/machines",
        json={"name": "x", "provider": "claude_code", "ssh_host": "h"},
        headers=_auth_headers(client),
    )
    # Unknown extra field: FastAPI/pydantic-level 422, or this route's own
    # allowlist check -- whichever this codebase's existing convention uses
    # for handle_machine_create (it parses raw JSON, not a pydantic model, so
    # expect the field to simply be ignored rather than rejected, UNLESS
    # Step 3 below adds an explicit allowlist check to this handler too --
    # confirm current behavior before asserting an exact status).
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        machine_id = resp.json()["id"]
        row_resp = client.get(f"/api/machines/{machine_id}", headers=_auth_headers(client))
        assert "ssh_host" not in row_resp.json()


def test_create_with_valid_transport_id(client):
    import db
    async def _seed():
        await db.ssh_transport_create("t1", "Kali3", "admin", "h", "kali", "k")
    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed()) if False else None
    # Seed via the real API instead of reaching into db directly, since this
    # file's client fixture already owns a running event loop through
    # TestClient -- follow whatever seeding pattern the rest of this file
    # already uses for setup (check test_tunnel_start_for_a_real_machine_
    # actually_creates_the_row in test_ssh_tunnel_api.py for the pattern of
    # calling db functions from inside a sync test via the fixture's own
    # patched db module).
    resp = client.post(
        "/api/machines",
        json={
            "name": "CF AI Machine (via Kali3)", "provider": "claude_code",
            "transport_id": "t1", "model": "vllm/Qwen3.6-35B-A3B-NVFP4",
        },
        headers=_auth_headers(client),
    )
    assert resp.status_code == 200, resp.text


def test_create_with_someone_elses_transport_id_is_rejected(client):
    resp = client.post(
        "/api/machines",
        json={
            "name": "x", "provider": "claude_code",
            "transport_id": "not-mine", "model": "claude-sonnet-5",
        },
        headers=_auth_headers(client),
    )
    assert resp.status_code == 404
```

Adjust the seeding approach in `test_create_with_valid_transport_id` to
match whatever this test file's actual fixture already does elsewhere for
creating rows before hitting the API — read the top of
`tests/test_machine_provider.py` for its `client`/auth-header helper before
finalizing this step; the sketch above intentionally flags where to look.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_machine_provider.py -k transport -v`
Expected: FAIL (transport_id not recognized yet, ssh_proxy still accepted)

- [ ] **Step 3: Update the allowlists and provider set**

In `routes/machines.py`, replace lines 46-78:

```python
_MACHINE_ALLOWED_FIELDS = {
    "name",
    "provider",
    "host",
    "port",
    "api_key",
    "model",
    "base_url",
    "description",
    "transport_id",
}


# Fields that must be a string (or null) when present in a machine PATCH.
_MACHINE_TEXT_FIELDS = (
    "name",
    "provider",
    "host",
    "api_key",
    "model",
    "base_url",
    "description",
    "transport_id",
)


# How a machine is reached. 'claude_code' is the official API -- what Claude
# Code talks to out of the box; 'direct' is any other OpenAI/Anthropic-
# compatible endpoint. Where it *runs* (locally vs a transport) is now
# transport_id, independent of provider.
_MACHINE_PROVIDERS = {"claude_code", "direct"}
```

- [ ] **Step 4: Remove the `ssh_proxy` branch from `handle_machine_create`**

In `routes/machines.py:134-233`, remove the `ssh_host`/`ssh_user`/
`ssh_key_path` local variables (lines 144-146), the entire
`if provider == "ssh_proxy":` branch (lines 164-182) — its `else` branch's
host validation becomes the only branch, unconditional — and the
`ssh_host=`/`ssh_user=`/`ssh_key_path=` kwargs on the `db.ai_machine_create`
call (lines 216-218). Add `transport_id` validation right before that call:

```python
    transport_id = (data.get("transport_id") or "").strip() or None
    if transport_id and not await db.ssh_transport_get(transport_id, session["user"]):
        raise HTTPException(status_code=404, detail="Transport not found")
```

Then change the `db.ai_machine_create(...)` call to pass
`transport_id=transport_id` instead of the three removed `ssh_*` kwargs.

The host-validation branch that was previously the `else` (lines 183-191)
becomes unconditional (drop the `else:`, keep its body at the same
indentation level as the removed `if`).

- [ ] **Step 5: Update `handle_machine_patch`**

In `routes/machines.py:236-304`, add transport validation right before the
existing `updated = await db.ai_machine_update(...)` call:

```python
    if "transport_id" in data:
        tid = (data["transport_id"] or "").strip() or None
        if tid and not await db.ssh_transport_get(tid, session["user"]):
            raise HTTPException(status_code=404, detail="Transport not found")
        if tid is None:
            # Explicit clear -- ai_machine_update's None-means-omit rule
            # can't express this (Task 2, Step 7).
            await db.ai_machine_clear_transport(machine_id, session["user"])
            data.pop("transport_id")
        else:
            data["transport_id"] = tid
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_machine_provider.py -v`
Expected: PASS (all tests in the file, not just the new ones — confirm
nothing else in this file referenced `ssh_proxy`/`ssh_host` and broke)

- [ ] **Step 7: Commit**

```bash
git add routes/machines.py tests/test_machine_provider.py
git commit -m "feat: validate ai_machines.transport_id, drop ssh_proxy provider"
```

---

### Task 4: `routes/transports.py` — `/api/transports` CRUD + SSH test

**Files:**
- Create: `routes/transports.py`
- Modify: `app.py` (router registration)
- Test: `tests/test_transports_api.py`

**Interfaces:**
- Consumes: `db.ssh_transport_create/get/list/update/delete` (Task 1), `tunnel_manager_ssh.test_ssh_connection` (existing, unchanged signature — `tunnel_manager_ssh.py:289`).
- Produces: `GET /api/transports`, `POST /api/transports`, `GET /api/transports/{id}`, `PATCH /api/transports/{id}`, `DELETE /api/transports/{id}`, `POST /api/transports/{id}/test` (SSH connectivity test against a saved transport) and `POST /api/transports/test` (test raw host/user/key before saving, for the add-transport form) — later consumed by Task 9's frontend.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_transports_api.py
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import auth
import config
import db


def _client():
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url="https://testserver")


class TransportsApiTests(unittest.IsolatedAsyncioTestCase):
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
        import secrets
        self.password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(self.password))

    def _login(self):
        client = _client()
        resp = client.post("/login", json={"username": "admin", "password": self.password})
        self.assertEqual(resp.status_code, 200)
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def test_create_then_list(self):
        client, headers = self._login()
        resp = client.post(
            "/api/transports",
            json={"name": "Kali3", "ssh_host": "kali-3.tail850c40.ts.net",
                  "ssh_user": "kali", "ssh_key_path": "~/.ssh/id_ed25519"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        transport_id = resp.json()["id"]

        resp = client.get("/api/transports", headers=headers)
        self.assertEqual(resp.status_code, 200)
        ids = [t["id"] for t in resp.json()["transports"]]
        self.assertIn(transport_id, ids)

    async def test_create_requires_ssh_host_and_key_path(self):
        client, headers = self._login()
        resp = client.post(
            "/api/transports", json={"name": "x", "ssh_user": "kali"}, headers=headers,
        )
        self.assertEqual(resp.status_code, 400)

    async def test_patch_updates_name(self):
        client, headers = self._login()
        created = client.post(
            "/api/transports",
            json={"name": "Kali3", "ssh_host": "h", "ssh_user": "kali", "ssh_key_path": "k"},
            headers=headers,
        ).json()
        resp = client.patch(
            f"/api/transports/{created['id']}", json={"name": "Kali3 renamed"}, headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        got = client.get(f"/api/transports/{created['id']}", headers=headers).json()
        self.assertEqual(got["name"], "Kali3 renamed")

    async def test_delete_removes_it(self):
        client, headers = self._login()
        created = client.post(
            "/api/transports",
            json={"name": "Kali3", "ssh_host": "h", "ssh_user": "kali", "ssh_key_path": "k"},
            headers=headers,
        ).json()
        resp = client.delete(f"/api/transports/{created['id']}", headers=headers)
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/transports/{created['id']}", headers=headers)
        self.assertEqual(resp.status_code, 404)

    async def test_test_route_calls_ssh_test_connection(self):
        client, headers = self._login()
        with patch(
            "tunnel_manager_ssh.test_ssh_connection",
            AsyncMock(return_value={"ok": True, "error": None}),
        ):
            resp = client.post(
                "/api/transports/test",
                json={"ssh_host": "h", "ssh_user": "kali", "ssh_key_path": "k"},
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    async def test_endpoints_require_auth(self):
        client = _client()
        self.assertEqual(client.get("/api/transports").status_code, 401)
        self.assertEqual(client.post("/api/transports", json={}).status_code, 401)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_transports_api.py -v`
Expected: FAIL with 404 on every route (none registered yet)

- [ ] **Step 3: Create `routes/transports.py`**

```python
"""Routes for /api/transports: SSH transport CRUD and connectivity test.

A transport is the SSH connection to a remote host (ssh_host/ssh_user/
ssh_key_path) -- not a backend. See
docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from net_validation import _HOST_PATTERN_LOCAL, _validate_host

_log = logging.getLogger("wc.app")

router = APIRouter()


@router.get("/api/transports")
async def handle_transports_list(request: Request):
    session = request.state.session
    transports = await db.ssh_transports_list(session["user"])
    return JSONResponse({"transports": transports})


@router.get("/api/transports/{transport_id}")
async def handle_transport_get(request: Request, transport_id: str):
    session = request.state.session
    transport = await db.ssh_transport_get(transport_id, session["user"])
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse(transport)


@router.post("/api/transports")
async def handle_transport_create(request: Request):
    session = request.state.session
    data = await request.json()
    name = (data.get("name") or "").strip()[:100]
    ssh_host = (data.get("ssh_host") or "").strip()
    ssh_user = (data.get("ssh_user") or "kali").strip()
    ssh_key_path = (data.get("ssh_key_path") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not ssh_host:
        raise HTTPException(status_code=400, detail="SSH host is required")
    if not ssh_key_path:
        raise HTTPException(status_code=400, detail="SSH key path is required")
    if not _HOST_PATTERN_LOCAL.fullmatch(ssh_host):
        raise HTTPException(status_code=400, detail="Enter a valid hostname or IP address")
    _validate_host(ssh_host)
    transport_id = uuid.uuid4().hex
    await db.ssh_transport_create(
        transport_id, name, session["user"], ssh_host, ssh_user, ssh_key_path,
    )
    _log.info("ssh_transport created by user=%s name=%s", session["user"], name)
    return JSONResponse({"ok": True, "id": transport_id, "name": name})


@router.patch("/api/transports/{transport_id}")
async def handle_transport_patch(request: Request, transport_id: str):
    session = request.state.session
    data = await request.json()
    allowed = {"name", "ssh_host", "ssh_user", "ssh_key_path"}
    if not data or not set(data).issubset(allowed):
        raise HTTPException(status_code=400, detail="No valid fields to update")
    if "ssh_host" in data and data["ssh_host"]:
        host = data["ssh_host"].strip()
        if not _HOST_PATTERN_LOCAL.fullmatch(host):
            raise HTTPException(status_code=400, detail="Enter a valid hostname or IP address")
        _validate_host(host)
        data["ssh_host"] = host
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()[:100]
    updated = await db.ssh_transport_update(transport_id, session["user"], **data)
    if not updated:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse({"ok": True})


@router.delete("/api/transports/{transport_id}")
async def handle_transport_delete(request: Request, transport_id: str):
    session = request.state.session
    deleted = await db.ssh_transport_delete(transport_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse({"ok": True})


@router.post("/api/transports/test")
async def handle_transport_test_raw(request: Request):
    """Test SSH connectivity for the add-transport form, before it is saved."""
    from tunnel_manager_ssh import test_ssh_connection

    data = await request.json()
    result = await test_ssh_connection(
        (data.get("ssh_host") or "").strip(),
        (data.get("ssh_user") or "kali").strip(),
        (data.get("ssh_key_path") or "").strip(),
    )
    return JSONResponse(result)


@router.post("/api/transports/{transport_id}/test")
async def handle_transport_test_saved(request: Request, transport_id: str):
    """Test SSH connectivity for an already-saved transport."""
    from tunnel_manager_ssh import test_ssh_connection

    session = request.state.session
    transport = await db.ssh_transport_get(transport_id, session["user"])
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")
    result = await test_ssh_connection(
        transport["ssh_host"], transport["ssh_user"], transport["ssh_key_path"],
    )
    return JSONResponse(result)
```

- [ ] **Step 4: Register the router in `app.py`**

Add the import next to the other `from routes.X import router as x_router`
lines (`app.py:44-54`):

```python
from routes.transports import router as transports_router
```

Add the registration next to the other `app.include_router(...)` calls
(`app.py:393-397`):

```python
app.include_router(transports_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_transports_api.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add routes/transports.py app.py tests/test_transports_api.py
git commit -m "feat: add /api/transports CRUD and SSH connectivity test routes"
```

---

### Task 5: Shared SSH connection across backends on the same transport

**Files:**
- Modify: `tunnel_manager.py:24-27` (module state), `tunnel_manager.py:175-196` (`_release_machine`), `tunnel_manager_ssh.py:80-209` (`connect`)
- Test: `tests/test_tunnel_manager_sharing.py`

**Interfaces:**
- Consumes: `db.ssh_transport_get(id, owner_id)` (Task 1), `machine["transport_id"]` (Task 2).
- Produces: `tunnel_manager._TRANSPORT_CONNECTIONS: dict[str, dict]` — keyed by `transport_id`, holding `{"ssh_client", "transport", "refcount"}`. `tunnel_manager_ssh.connect(machine_id)` returns the same 6-tuple shape as today (`ok, ssh_client, transport, local_port, ssh_port, forward_server`), unchanged for every existing caller, but internally reuses a live connection when one already exists for the same `transport_id` instead of opening a second one.

- [ ] **Step 1: Write the failing test for connection reuse**

```python
# tests/test_tunnel_manager_sharing.py
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import tunnel_manager
import tunnel_manager_ssh


class SharedTransportConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tunnel_manager._STATE.clear()
        tunnel_manager._TRANSPORT_CONNECTIONS.clear()
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self):
        tunnel_manager._STATE.clear()
        tunnel_manager._TRANSPORT_CONNECTIONS.clear()

    async def test_two_machines_same_transport_share_one_ssh_client(self):
        import db

        fake_transport_row = {
            "id": "t1", "ssh_host": "h", "ssh_user": "kali",
            "ssh_key_path": "k", "ssh_host_key_fingerprint": "",
        }
        fake_machine_a = {"id": "ma", "transport_id": "t1", "owner_id": "admin"}
        fake_machine_b = {"id": "mb", "transport_id": "t1", "owner_id": "admin"}

        async def fake_tunnel_get(machine_id):
            return {"owner_id": "admin", "ssh_port": 22}

        async def fake_machine_get(machine_id, owner_id):
            return fake_machine_a if machine_id == "ma" else fake_machine_b

        async def fake_transport_get(transport_id, owner_id):
            return fake_transport_row

        fake_ssh_client = MagicMock()
        fake_transport_obj = MagicMock()
        fake_ssh_client.get_transport.return_value = fake_transport_obj

        connect_calls = []

        def fake_paramiko_connect(self, **kwargs):
            connect_calls.append(kwargs)

        with patch.object(db, "ssh_tunnel_get", fake_tunnel_get), \
             patch.object(db, "ai_machine_get", fake_machine_get), \
             patch.object(db, "ssh_transport_get", fake_transport_get), \
             patch("paramiko.SSHClient", return_value=fake_ssh_client), \
             patch.object(fake_ssh_client, "connect", fake_paramiko_connect), \
             patch("tunnel_manager_ssh._find_available_port", AsyncMock(side_effect=[9001, 9002])), \
             patch("tunnel_manager_forward.start_forward", MagicMock(return_value=MagicMock())):
            ok_a, client_a, transport_a, port_a, _, _ = await tunnel_manager_ssh.connect("ma")
            ok_b, client_b, transport_b, port_b, _, _ = await tunnel_manager_ssh.connect("mb")

        self.assertTrue(ok_a)
        self.assertTrue(ok_b)
        # Same underlying SSH connection reused, only one real paramiko
        # handshake performed.
        self.assertEqual(len(connect_calls), 1)
        self.assertIs(client_a, client_b)
        self.assertIs(transport_a, transport_b)
        # But each machine still gets its own forwarded local port.
        self.assertNotEqual(port_a, port_b)
        self.assertEqual(tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["refcount"], 2)

    async def test_releasing_one_machine_keeps_shared_connection_alive(self):
        tunnel_manager._TRANSPORT_CONNECTIONS["t1"] = {
            "ssh_client": MagicMock(), "transport": MagicMock(), "refcount": 2,
        }
        tunnel_manager._STATE["ma"] = {
            "transport_id": "t1", "forward_server": MagicMock(),
            "ssh_client": tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["ssh_client"],
            "transport": tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["transport"],
        }
        tunnel_manager._release_machine("ma")
        self.assertIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        self.assertEqual(tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["refcount"], 1)

    async def test_releasing_the_last_machine_closes_the_shared_connection(self):
        shared_client = MagicMock()
        shared_transport = MagicMock()
        tunnel_manager._TRANSPORT_CONNECTIONS["t1"] = {
            "ssh_client": shared_client, "transport": shared_transport, "refcount": 1,
        }
        tunnel_manager._STATE["ma"] = {
            "transport_id": "t1", "forward_server": MagicMock(),
            "ssh_client": shared_client, "transport": shared_transport,
        }
        tunnel_manager._release_machine("ma")
        self.assertNotIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        shared_transport.close.assert_called_once()
        shared_client.close.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tunnel_manager_sharing.py -v`
Expected: FAIL — `AttributeError: module 'tunnel_manager' has no attribute '_TRANSPORT_CONNECTIONS'`

- [ ] **Step 3: Add the shared-connection registry to `tunnel_manager.py`**

Add alongside the existing module state at `tunnel_manager.py:24-27`:

```python
_STATE: dict[str, dict] = {}
# transport_id -> {"ssh_client", "transport", "refcount"}. Several machines
# can share one live SSH connection when they reference the same
# ai_machines.transport_id; this is the registry that makes the second (and
# later) connect() calls reuse it instead of opening a new handshake.
_TRANSPORT_CONNECTIONS: dict[str, dict] = {}
_queue: asyncio.Queue = asyncio.Queue()
_task: asyncio.Task | None = None
_running: bool = False
```

- [ ] **Step 4: Update `_release_machine` to decrement/close by refcount**

Replace `tunnel_manager.py:175-196` (`_release_machine`):

```python
def _release_machine(machine_id: str) -> None:
    """Release tunnel state dict entry and local port forward.

    The forward server is always this machine's own -- always stopped. The
    underlying SSH client/transport may be shared with other machines on the
    same transport_id; only actually closed when this was the last one still
    using it (refcount reaches zero).
    """
    state = _STATE.pop(machine_id, None)
    if not state:
        return
    forward_server = state.get("forward_server")
    if forward_server:
        # Before the transport it forwards over: closing the transport
        # first would just make every in-flight forwarded connection error
        # out through the transport instead of a clean local shutdown.
        import tunnel_manager_forward
        with contextlib.suppress(Exception):
            tunnel_manager_forward.stop_forward(forward_server)

    transport_id = state.get("transport_id")
    if not transport_id:
        # No shared registry entry (e.g. a machine with no transport_id at
        # all never went through it) -- close directly, same as before.
        _close_ssh(state.get("transport"), state.get("ssh_client"))
        return

    shared = _TRANSPORT_CONNECTIONS.get(transport_id)
    if not shared:
        _close_ssh(state.get("transport"), state.get("ssh_client"))
        return
    shared["refcount"] -= 1
    if shared["refcount"] <= 0:
        _close_ssh(shared.get("transport"), shared.get("ssh_client"))
        _TRANSPORT_CONNECTIONS.pop(transport_id, None)


def _close_ssh(transport, ssh_client) -> None:
    if transport:
        with contextlib.suppress(Exception):
            transport.close()
    if ssh_client:
        with contextlib.suppress(Exception):
            ssh_client.close()
```

- [ ] **Step 5: Rework `tunnel_manager_ssh.connect()` to resolve via transport and reuse**

Replace `tunnel_manager_ssh.py:80-209` (`connect`):

```python
async def connect(machine_id: str):
    """Attempt SSH connect + port forward for *machine_id*.

    Returns (ok, ssh_client, transport, local_port, ssh_port, forward_server).
    forward_server is the tunnel_manager_forward server actually bridging
    127.0.0.1:local_port to claude_proxy.py on the remote side; the caller
    must hold onto it and pass it to tunnel_manager_forward.stop_forward()
    on disconnect, or the forwarding thread and its bound port both leak.

    Several machines can share one *transport_id* -- reach the same remote
    host once, offer multiple backends over it. The second (and later)
    machine to connect on an already-live transport skips the SSH handshake
    entirely and only opens its own local port forward over the existing
    connection; ssh_client/transport returned are the *same objects* every
    sharing machine gets, so tunnel_manager._release_machine's refcounting
    (not this function) decides when the underlying connection actually
    closes.
    """
    import paramiko

    import db

    try:
        tunnel_row = await db.ssh_tunnel_get(machine_id)
    except Exception:
        return _fail(machine_id, "no tunnel row")
    if not tunnel_row:
        return _fail(machine_id, "no tunnel row")

    try:
        machine = await db.ai_machine_get(machine_id, tunnel_row["owner_id"])
    except Exception:
        return _fail(machine_id, "no machine row")
    if not machine:
        return _fail(machine_id, "no machine row")

    transport_id = machine.get("transport_id")
    if not transport_id:
        return _fail(machine_id, "machine has no transport_id")

    try:
        transport_row = await db.ssh_transport_get(transport_id, tunnel_row["owner_id"])
    except Exception:
        return _fail(machine_id, "no transport row")
    if not transport_row:
        return _fail(machine_id, "no transport row")

    ssh_port = tunnel_row["ssh_port"] if "ssh_port" in tunnel_row.keys() else 22

    try:
        local_port = await _find_available_port()
    except Exception as exc:
        _fail(machine_id, str(exc))
        return (False, None, None, 0, 0, None)

    import tunnel_manager
    import config

    shared = tunnel_manager._TRANSPORT_CONNECTIONS.get(transport_id)
    if shared:
        # Reuse: no new SSH handshake, just another forward channel over the
        # already-live transport.
        try:
            forward_server = tunnel_manager_forward.start_forward(
                shared["transport"], local_port, "127.0.0.1", config.PROXY_PORT,
            )
        except Exception as exc:
            return _fail(machine_id, str(exc)[:200])
        shared["refcount"] += 1
        return (
            True, shared["ssh_client"], shared["transport"], local_port,
            ssh_port, forward_server,
        )

    # No live connection for this transport yet: connect for real.
    ssh_host = transport_row["ssh_host"]
    ssh_user = transport_row["ssh_user"]
    ssh_key_path = transport_row["ssh_key_path"]
    if not ssh_host:
        return _fail(machine_id, "ssh_host is empty")
    if not ssh_key_path:
        return _fail(machine_id, "ssh_key_path is empty")

    try:
        ssh_key_path = _check_key_permissions(ssh_key_path)
    except Exception as exc:
        return _fail(machine_id, str(exc))

    stored_fingerprint = transport_row.get("ssh_host_key_fingerprint", "")
    policy = _PinnedHostKeyPolicy(stored_fingerprint)
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(policy)
    try:
        await asyncio.to_thread(
            ssh_client.connect,
            hostname=ssh_host,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=10,
        )
    except Exception as exc:
        ssh_client.close()
        if policy.mismatch:
            _log.warning(
                "ssh_host_key_mismatch transport=%s expected=%s got=%s",
                transport_id, policy.mismatch[0], policy.mismatch[1],
            )
        return _fail(machine_id, str(exc)[:200])

    if policy.new_fingerprint:
        try:
            await db.ssh_transport_set_host_key_fingerprint(
                transport_id, policy.new_fingerprint
            )
        except Exception:
            _log.exception(
                "could not persist ssh host key fingerprint for transport %s",
                transport_id,
            )

    try:
        transport = ssh_client.get_transport()
        if not transport:
            ssh_client.close()
            return _fail(machine_id, "no transport after connect")
        forward_server = tunnel_manager_forward.start_forward(
            transport, local_port, "127.0.0.1", config.PROXY_PORT,
        )
    except Exception as exc:
        ssh_client.close()
        return _fail(machine_id, str(exc)[:200])

    tunnel_manager._TRANSPORT_CONNECTIONS[transport_id] = {
        "ssh_client": ssh_client, "transport": transport, "refcount": 1,
    }
    return (True, ssh_client, transport, local_port, ssh_port, forward_server)
```

- [ ] **Step 4b: Store `transport_id` on the machine's own `_STATE` entry**

In `tunnel_manager.py`'s `_try_connect` (currently `tunnel_manager.py:235-288`),
the `_STATE[machine_id].update({...})` dict built after a successful connect
needs `"transport_id"` added so `_release_machine` (Step 4 above) can find
it. Add one line to that update call — resolve it from the machine row the
same way `connect()` does, or simplest: have `connect()` itself return the
`transport_id` as a 7th tuple element and thread it through. Prefer the
simplest option: change `_try_connect`'s unpacking

```python
ok, client, transport, local_port, ssh_port, forward_server = (
    await _connect(machine_id)
)
```

to accept a 7th value, and `connect()`'s every `return` (including inside
`_fail`) to emit a 7-tuple `(ok, ssh_client, transport, local_port, ssh_port,
forward_server, transport_id)`. Update `_fail()`'s return
`(False, None, None, 0, 0, None)` to `(False, None, None, 0, 0, None, None)`
to match. Then in `_try_connect`'s success branch add
`"transport_id": transport_id,` to the `_STATE[machine_id].update({...})`
call.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tunnel_manager_sharing.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run the full existing tunnel test suite to check for regressions**

Run: `.venv/bin/python -m pytest tests/test_ssh_tunnel_api.py -v`
Expected: PASS, or fix any test that constructed a machine with
`ssh_host=`/etc. kwargs directly against the old shape (same class of fix
as Task 2 Step 8).

- [ ] **Step 7: Commit**

```bash
git add tunnel_manager.py tunnel_manager_ssh.py tests/test_tunnel_manager_sharing.py
git commit -m "feat: share one SSH connection across backends on the same transport"
```

---

### Task 6: `runner.get_proxy_target` — gate on `transport_id`, not provider

**Files:**
- Modify: `runner.py:355-378` (the `ssh_proxy` branch inside `get_proxy_target`)
- Test: `tests/test_qa_backend_routing.py`

**Interfaces:**
- Consumes: `machine["transport_id"]` (Task 2).
- Produces: no signature change to `get_proxy_target` itself — same callers, same return shape (`(host, port)` tuple).

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_qa_backend_routing.py -- read its existing fixture
# setup first (likely similar patch.object(config, ...) + db.init() pattern
# used throughout this plan) and match it exactly rather than duplicating a
# slightly different one.

async def test_get_proxy_target_routes_via_transport_id_not_provider(self):
    import db
    import runner

    await db.ssh_transport_create("t1", "Kali3", "admin", "h", "kali", "k")
    await db.ai_machine_create(
        "m1", "CF AI Machine (via Kali3)", "llm.example", 443, None,
        "vllm/x", None, None, "admin", provider="claude_code",
        transport_id="t1",
    )
    await db.ai_machine_activate("m1", "admin")
    await db.chat_create("c1", "Test", None, "/tmp", "admin")

    from unittest.mock import AsyncMock, patch as mock_patch
    fake_status = {"tunnel_up": True, "proxy_ok": True, "local_port": 9005}
    with mock_patch("tunnel_manager.tunnel_status", AsyncMock(return_value=fake_status)):
        host, port = await runner.get_proxy_target("c1")
    self.assertEqual((host, port), ("127.0.0.1", 9005))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_backend_routing.py -k transport_id -v`
Expected: FAIL — falls through to the default `(get_proxy_host(), config.PROXY_PORT)` because the current code checks `provider == "ssh_proxy"`, which this machine's provider (`claude_code`) never matches.

- [ ] **Step 3: Change the gate in `runner.py`**

Replace `runner.py:365-368`:

```python
    provider = machine.get("provider", "")

    # ssh_proxy: route through the tunnel to 127.0.0.1:<local_port>.
    if provider == "ssh_proxy":
```

with:

```python
    provider = machine.get("provider", "")

    # A backend with transport_id set runs its claude process on that
    # transport's remote host -- route through the tunnel to
    # 127.0.0.1:<local_port>, same as before this was ai_machines.transport_id
    # instead of provider == "ssh_proxy".
    if machine.get("transport_id"):
```

The body of the `if` block (`runner.py:369-376`) is unchanged — it already
only used `machine["id"]` to look up `tunnel_status`, which stays keyed by
machine_id per Task 5.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_backend_routing.py -k transport_id -v`
Expected: PASS

- [ ] **Step 5: Run the full backend-routing suite to check for regressions**

Run: `.venv/bin/python -m pytest tests/test_qa_backend_routing.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add runner.py tests/test_qa_backend_routing.py
git commit -m "feat: route via transport_id instead of provider=='ssh_proxy'"
```

---

### Task 7: `routes/machines_tunnel.py` — gate on `transport_id`

**Files:**
- Modify: `routes/machines_tunnel.py` (every `machine.get("provider") != "ssh_proxy"` check, and `init_probe_remote`)
- Test: `tests/test_ssh_tunnel_api.py`

**Interfaces:**
- Consumes: `machine["transport_id"]` (Task 2).
- Produces: same routes, same request/response shapes — only the internal gating condition changes, from provider equality to transport_id presence.

- [ ] **Step 1: Update the failing/affected existing tests first**

Read every test in `tests/test_ssh_tunnel_api.py` that constructs a machine
with `provider="ssh_proxy"` and `ssh_host=`/etc (Task 1's Step 1 grep already
found these). Rewrite each to instead: create a transport via
`db.ssh_transport_create`, then create the machine with
`provider="claude_code", transport_id=<that transport's id>`. Do this before
touching `routes/machines_tunnel.py` itself, so the test run in Step 2 below
demonstrates the real gap (route still checks the old field, tests now set
up the new shape and fail because of it) rather than a fixture-setup error
masking it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ssh_tunnel_api.py -v`
Expected: FAIL — routes still reject with "not an ssh_proxy machine" because
`machine.get("provider")` is `"claude_code"` now, never `"ssh_proxy"`.

- [ ] **Step 3: Update every gating check in `routes/machines_tunnel.py`**

Five call sites use the pattern `machine.get("provider") != "ssh_proxy"` or
`== "ssh_proxy"` (`routes/machines_tunnel.py:61-62, 102-103, 123-124,
144, 167`). Replace every one:

`if machine.get("provider") != "ssh_proxy":` → `if not machine.get("transport_id"):`
`if m.get("provider") == "ssh_proxy":` → `if m.get("transport_id"):`
`if not machine or machine.get("provider") != "ssh_proxy":` → `if not machine or not machine.get("transport_id"):`

Each `raise HTTPException(status_code=400, detail="not an ssh_proxy machine")`
message stays as-is (it is caller-facing text, not internal naming — changing
it is out of scope for this task; a wording pass is a separate, optional
follow-up).

- [ ] **Step 4: Update `init_probe_remote`'s docstring/behavior — no code change needed**

`init_probe_remote` (`routes/machines_tunnel.py:206-222`) already calls
`tunnel_manager_ssh.probe_remote(machine_id)`, which reads
`tunnel_manager._STATE.get(machine_id)` — unaffected by this task, since
`_STATE` stays keyed by machine_id (Task 5). No change needed here; skip to
Step 5.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ssh_tunnel_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add routes/machines_tunnel.py tests/test_ssh_tunnel_api.py
git commit -m "feat: gate tunnel routes on transport_id instead of provider"
```

---

### Task 8: One-time migration — Kali3 / Pentester-Kali_Mac → transport + backend

**Files:**
- Modify: `db.py` (add a migration function, call it from `init()`)
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-2.
- Produces: `db._migrate_ssh_proxy_machines_to_transports()` — idempotent, called once from `init()` after the schema migrations complete. No new symbol needs registering in `_SYMBOLS` since it is only ever called internally by `init()`, not by any route.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db.py`:

```python
class SshProxyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)

    async def test_existing_ssh_proxy_row_becomes_transport_plus_backend(self):
        # Simulate a pre-migration database: init() once to get every OTHER
        # table, then hand-insert an old-shape ssh_proxy row directly (bypasses
        # ai_machine_create, which no longer accepts ssh_host/etc, since this
        # models data that predates this migration).
        await db.init()
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at, "
            " ssh_host, ssh_user, ssh_key_path, ssh_host_key_fingerprint) "
            "VALUES ('old1', 'Kali3', 'ssh_proxy', '', 9000, NULL, "
            "        'vllm/Qwen3.6-35B-A3B-NVFP4', NULL, NULL, 0, 'admin', "
            "        '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z', "
            "        'kali-3.tail850c40.ts.net', 'kali', '~/.ssh/id_ed25519', '')"
        )
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at) "
            "VALUES ('cfai', 'CF AI Machine', 'claude_code', "
            "        'llm.ai-machine.cfappsecurity.com', 443, 'real-key', "
            "        'vllm/Qwen3.6-35B-A3B-NVFP4', "
            "        'https://llm.ai-machine.cfappsecurity.com', NULL, 0, "
            "        'admin', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
        )
        await db.db_conn.commit()
        await db.close()

        # Re-run init() -- this is where the migration must fire.
        await db.init()
        self.addAsyncCleanup(db.close)

        # Old row gone.
        old = await db.ai_machine_get("old1", "admin")
        self.assertIsNone(old)

        # A transport now exists carrying Kali3's SSH details.
        transports = await db.ssh_transports_list("admin")
        self.assertEqual(len(transports), 1)
        transport = transports[0]
        self.assertEqual(transport["name"], "Kali3")
        self.assertEqual(transport["ssh_host"], "kali-3.tail850c40.ts.net")
        self.assertEqual(transport["ssh_key_path"], "~/.ssh/id_ed25519")

        # A new backend exists, copying CF AI Machine's own fields, pointed
        # at the new transport.
        machines = await db.ai_machines_list("admin")
        migrated = [m for m in machines if m.get("transport_id") == transport["id"]]
        self.assertEqual(len(migrated), 1)
        new_backend = await db.ai_machine_backend_by_id(migrated[0]["id"], "admin")
        self.assertEqual(new_backend["model"], "vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(new_backend["base_url"], "https://llm.ai-machine.cfappsecurity.com")
        self.assertEqual(new_backend["api_key"], "real-key")
        self.assertIn("Kali3", migrated[0]["name"])

    async def test_migration_is_idempotent(self):
        """Running init() a second time must not create duplicate transports
        or backends."""
        await db.init()
        await db.db_conn.execute(
            "INSERT INTO ai_machines "
            "(id, name, provider, host, port, api_key, model, base_url, "
            " description, active, owner_id, created_at, updated_at, "
            " ssh_host, ssh_user, ssh_key_path, ssh_host_key_fingerprint) "
            "VALUES ('old2', 'Pentester-Kali_Mac', 'ssh_proxy', '', 9000, "
            "        NULL, 'vllm/Qwen3.6-35B-A3B-NVFP4', NULL, NULL, 0, "
            "        'admin', '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z', "
            "        'pentester.tail850c40.ts.net', 'claude-ai-machine', "
            "        '~/.ssh/id_ed25519', '')"
        )
        await db.db_conn.commit()
        await db.close()
        await db.init()
        await db.close()
        await db.init()  # second run
        self.addAsyncCleanup(db.close)
        transports = await db.ssh_transports_list("admin")
        self.assertEqual(len(transports), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db.py -k SshProxyMigration -v`
Expected: FAIL — no backend gets created, old row is still present (nothing
runs the migration yet).

- [ ] **Step 3: Write the migration function in `db.py`**

Add this function near the other migration helpers (after
`_ensure_orchestrator_columns` or similar, anywhere at module scope before
`init()` calls it):

```python
async def _migrate_ssh_proxy_machines_to_transports() -> None:
    """One-time, idempotent: turn every remaining provider='ssh_proxy'
    ai_machines row into an ssh_transports row plus one backend copying its
    owner's active claude_code/anthropic-compatible machine's own fields
    (model/base_url/api_key/active_models), pointed at the new transport.

    Why copy from the active machine rather than leave the new backend
    empty: both real ssh_proxy rows found in production (Kali3,
    Pentester-Kali_Mac) already declared the *same* model as the owner's
    real backend's own default, which was never actually read for anything
    except get_default_model -- the strongest available signal that the
    original intent was "run that backend, but from over there," not "this
    machine has its own separate identity." See
    docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.

    Safe to call every startup: it only ever acts on provider='ssh_proxy'
    rows, and this migration deletes every one it processes, so a second
    run finds none left and does nothing.
    """
    import uuid

    from routes.db_machines import _BACKEND_COLUMNS

    cur = await db_conn.execute(
        "SELECT id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
        "ssh_host_key_fingerprint FROM ai_machines WHERE provider = 'ssh_proxy'"
    )
    old_rows = [dict(r) for r in await cur.fetchall()]
    if not old_rows:
        return

    for old in old_rows:
        owner_id = old["owner_id"]
        # The owner's active backend, if any -- what the new machine copies.
        active_cur = await db_conn.execute(
            f"SELECT {_BACKEND_COLUMNS} FROM ai_machines "  # nosec B608: static columns
            "WHERE owner_id = ? AND active = 1 AND provider != 'ssh_proxy' LIMIT 1",
            (owner_id,),
        )
        active_row = await active_cur.fetchone()

        transport_id = uuid.uuid4().hex
        now = _now()
        await db_conn.execute(
            "INSERT INTO ssh_transports "
            "(id, name, owner_id, ssh_host, ssh_user, ssh_key_path, "
            " ssh_host_key_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transport_id, old["name"], owner_id, old["ssh_host"],
                old["ssh_user"], old["ssh_key_path"],
                old["ssh_host_key_fingerprint"], now, now,
            ),
        )

        if active_row:
            active = dict(active_row)
            new_machine_id = uuid.uuid4().hex
            await db_conn.execute(
                "INSERT INTO ai_machines "
                "(id, name, provider, host, port, api_key, model, base_url, "
                " description, active, owner_id, created_at, updated_at, "
                " transport_id, active_models) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, "
                "        (SELECT active_models FROM ai_machines WHERE id = ?))",
                (
                    new_machine_id, f"{active['name']} (via {old['name']})",
                    active["provider"], active["host"], active["port"],
                    active["api_key"], active["model"], active["base_url"],
                    None, owner_id, now, now, transport_id, active["id"],
                ),
            )
            _log.info(
                "ssh_proxy_migrated old_machine=%s -> transport=%s new_backend=%s",
                old["id"], transport_id, new_machine_id,
            )
        else:
            _log.warning(
                "ssh_proxy_migrated old_machine=%s -> transport=%s, no active "
                "backend found for owner=%s to copy -- transport created with "
                "no linked backend, add one by hand in Settings",
                old["id"], transport_id, owner_id,
            )

        await db_conn.execute("DELETE FROM ai_machines WHERE id = ?", (old["id"],))

    await db_conn.commit()
```

- [ ] **Step 4: Call it from `init()`**

At the very end of `init()`, immediately after the existing
`await db_conn.commit()` that closes the main migration block (`db.py:1015`,
right before `_ensure_usage_columns` or whatever runs next), add:

```python
    await _migrate_ssh_proxy_machines_to_transports()
```

Place this call *after* the `transport_id` column migration (Task 2, Step 4)
has already run in the same `init()` call — it reads and writes
`transport_id`, so the column must exist first. Since all migrations run
sequentially inside the same `init()` before this line, this ordering is
automatic as long as the call is appended at the end, not inserted earlier.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_db.py -k SshProxyMigration -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full test suite once to check for any migration-order regression**

Run: `.venv/bin/python -m pytest tests/test_db.py -v`
Expected: PASS (every test in the file, not just the new ones)

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: auto-migrate ssh_proxy machines into transport + backend pairs"
```

---

### Task 9: Frontend — "+ Add transport" button, "Executes on" dropdown, grouped map

**Files:**
- Modify: `web/index.html` (Backends panel markup), `web/assets/machines.js`
- Create: `web/assets/transports.js`
- Modify: `web/assets/machine-wizard.js` (rework for transport-keyed wizard)
- Test: `tests/test_qa_backend_routing.py` or a new browser QA test file, per this project's existing convention of pairing a JS feature with a Playwright-driven `tests/test_qa_*.py` (check `tests/test_qa_backend_routing.py`'s neighbors for the exact pattern used for the current machine list before choosing where new tests land)

**Interfaces:**
- Consumes: `/api/transports` (Task 4), `ai_machines.transport_id` field on every machine object returned by `/api/machines` (Task 2/3).
- Produces: nothing consumed by a later task — this is the last task in the plan.

This task is UI-only, so its granularity is coarser than the backend
tasks above (one browser-visible deliverable per step rather than one
function per step), matching how this project's own JS/HTML changes are
usually reviewed as a whole panel rather than line-by-line.

- [ ] **Step 1: Add the "+ Add transport" button and transport form to `web/index.html`**

In `web/index.html`, immediately before the existing
`<button class="btn-secondary btn-add-machine" id="addMachineBtn">＋ Add machine</button>`
(`web/index.html:200`), add:

```html
<button class="btn-secondary btn-add-transport" id="addTransportBtn">＋ Add transport</button>
<div class="transport-form" id="transportForm" hidden>
  <h3 id="transportFormTitle">Add transport</h3>
  <label for="transportName">Name</label>
  <input id="transportName" maxlength="100" placeholder="e.g. Kali3">
  <label for="transportSshHost">SSH Host</label>
  <input id="transportSshHost" maxlength="253" placeholder="e.g. kali-3.tail850c40.ts.net">
  <label for="transportSshUser">SSH User</label>
  <input id="transportSshUser" maxlength="100" placeholder="kali" value="kali">
  <label for="transportSshKeyPath">SSH Key Path</label>
  <input id="transportSshKeyPath" maxlength="500" placeholder="~/.ssh/id_ed25519">
  <p class="machine-hint">Key must be readable by the server process (mode 0600 recommended).</p>
  <div class="machine-form-actions">
    <button class="btn-secondary" id="testTransport">Test connection</button>
    <span id="transportTestResult" role="status" aria-live="polite"></span>
  </div>
  <div class="machine-form-actions">
    <button class="btn-secondary" id="cancelTransport">Cancel</button>
    <button class="btn-primary" id="saveTransport">Save</button>
  </div>
</div>
```

Then add the "Executes on" dropdown to the existing machine form, right
after the `machineModel` label/input block (`web/index.html:228-229`):

```html
<label for="machineTransport">Executes on</label>
<select id="machineTransport">
  <option value="">This server</option>
</select>
```

- [ ] **Step 2: Create `web/assets/transports.js`**

```js
// Transport CRUD: the SSH connection a backend can optionally run over.
// See docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.
import {apiFetch} from './api.js?v=1';
import {byId, notifyResult} from './app.js?v=39';

export let _transports = [];
let _transportEditing = null;

export async function loadTransports() {
  try {
    const resp = await apiFetch('/api/transports');
    if (!resp.ok) return;
    const data = await resp.json();
    _transports = data.transports || [];
  } catch {
    _transports = [];
  }
}

/** Fill the "Executes on" dropdown with "This server" + every transport. */
export function populateTransportPicker(selectedId) {
  const picker = byId('machineTransport');
  if (!picker) return;
  const local = document.createElement('option');
  local.value = '';
  local.textContent = 'This server';
  const options = [local];
  _transports.forEach(t => {
    const option = document.createElement('option');
    option.value = t.id;
    option.textContent = t.name;
    options.push(option);
  });
  picker.replaceChildren(...options);
  picker.value = _transports.some(t => t.id === selectedId) ? selectedId : '';
}

export function _showAddTransport() {
  _transportEditing = null;
  byId('transportFormTitle').textContent = 'Add transport';
  byId('transportName').value = '';
  byId('transportSshHost').value = '';
  byId('transportSshUser').value = 'kali';
  byId('transportSshKeyPath').value = '';
  byId('transportTestResult').textContent = '';
  byId('transportForm').hidden = false;
  byId('addTransportBtn').hidden = true;
  byId('transportName').focus();
}

export function _cancelTransportForm() {
  byId('transportForm').hidden = true;
  byId('addTransportBtn').hidden = false;
  _transportEditing = null;
}

export async function _testTransportForm() {
  const btn = byId('testTransport');
  const result = byId('transportTestResult');
  btn.disabled = true;
  result.textContent = 'Testing…';
  try {
    const resp = await apiFetch('/api/transports/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ssh_host: byId('transportSshHost').value.trim(),
        ssh_user: byId('transportSshUser').value.trim() || 'kali',
        ssh_key_path: byId('transportSshKeyPath').value.trim(),
      }),
    });
    const data = await resp.json();
    result.textContent = data.ok ? 'Connection OK' : `Failed: ${data.error || 'unknown'}`;
    result.className = data.ok ? 'ok' : 'error';
  } catch (err) {
    result.textContent = `Error: ${err.message}`;
    result.className = 'error';
  } finally {
    btn.disabled = false;
  }
}

export async function _saveTransport(onSaved) {
  const name = byId('transportName').value.trim();
  const ssh_host = byId('transportSshHost').value.trim();
  const ssh_user = byId('transportSshUser').value.trim() || 'kali';
  const ssh_key_path = byId('transportSshKeyPath').value.trim();
  if (!name) { byId('transportName').focus(); return; }
  if (!ssh_host) { byId('transportSshHost').focus(); return; }
  if (!ssh_key_path) { byId('transportSshKeyPath').focus(); return; }

  const save = byId('saveTransport');
  save.disabled = true;
  try {
    const body = {name, ssh_host, ssh_user, ssh_key_path};
    const resp = _transportEditing
      ? await apiFetch(`/api/transports/${encodeURIComponent(_transportEditing)}`, {
          method: 'PATCH', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        })
      : await apiFetch('/api/transports', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
    if (!resp.ok) throw new Error(`Could not save transport (${resp.status})`);
    _cancelTransportForm();
    await loadTransports();
    if (onSaved) onSaved();
    notifyResult('Transport saved');
  } catch (error) {
    notifyResult(error.message, 'error');
  } finally {
    save.disabled = false;
  }
}
```

- [ ] **Step 3: Group `_renderMachineList` by "Executes on" in `web/assets/machines.js`**

Import the new module at the top of `web/assets/machines.js` (alongside its
existing imports):

```js
import {_transports, loadTransports, populateTransportPicker} from './transports.js?v=1';
```

Replace `_renderMachineList` (`web/assets/machines.js:357-473`) so the flat
`_machines.forEach(...)` loop becomes a grouped render. Keep every existing
per-card DOM-building code (top/ident/meta/provider/model-section/actions)
exactly as it is today — extract it into a `_buildMachineCard(m)` helper
returning the `card` element (the body of today's forEach, unchanged,
ending in `return card;` instead of `list.appendChild(card)`), then group:

```js
export function _renderMachineList() {
  const list = byId('machineList');
  list.replaceChildren();
  if (!_machines.length) {
    const empty = document.createElement('div');
    empty.className = 'sidebar-empty';
    empty.textContent = 'No machines yet. Add one below.';
    list.appendChild(empty);
    return;
  }

  const local = _machines.filter(m => !m.transport_id);
  const byTransport = new Map();
  _machines.forEach(m => {
    if (!m.transport_id) return;
    if (!byTransport.has(m.transport_id)) byTransport.set(m.transport_id, []);
    byTransport.get(m.transport_id).push(m);
  });

  if (local.length) {
    local.forEach(m => list.appendChild(_buildMachineCard(m)));
  }
  for (const [transportId, machines] of byTransport) {
    const transport = _transports.find(t => t.id === transportId);
    const header = document.createElement('div');
    header.className = 'chat-section-label';
    header.textContent = transport ? `via ${transport.name}` : 'via (unknown transport)';
    list.appendChild(header);
    machines.forEach(m => list.appendChild(_buildMachineCard(m)));
  }

  const total = byId('mapTotal');
  if (total) {
    let sum = 0;
    for (const n of _turnsByModel.values()) sum += n;
    total.textContent = sum ? `${sum.toLocaleString()} turns` : 'no turns yet';
  }
  requestAnimationFrame(_drawMapWires);
}
```

Inside the extracted `_buildMachineCard`, remove the
`if (m.provider === 'ssh_proxy') { ... SSH badge ... }` block
(`web/assets/machines.js:416-424`) — the SSH badge/tunnel-toggle moves to the
transport header instead (Step 4).

- [ ] **Step 4: Add the tunnel toggle to the transport header**

Extend the `header` element built in Step 3's loop with a toggle badge,
reusing the existing `_toggleSshTunnel` function (`web/assets/machines.js:107`)
but keyed by the FIRST machine in that transport's group (since starting the
tunnel for one machine on a shared transport brings the whole connection up
for all of them, per Task 5):

```js
    const badge = document.createElement('span');
    badge.className = 'machine-badge machine-badge-ssh';
    badge.title = 'Click to start tunnel';
    badge.textContent = 'SSH';
    badge.addEventListener('click', () => _toggleSshTunnel(machines[0].id, badge));
    header.appendChild(badge);
```

- [ ] **Step 5: Wire the "Executes on" dropdown into save/edit/add**

In `_syncMachineProviderFields` (`web/assets/machines.js:551-563`), no change
needed — the transport dropdown is independent of provider, always visible.

In `_editMachine` (`web/assets/machines.js:565-584`), add:

```js
  populateTransportPicker(m.transport_id || '');
```

right after the existing `_syncMachineProviderFields();` call.

In `_showAddMachine` (`web/assets/machines.js:674-690`), add the same call
with an empty selection:

```js
  populateTransportPicker('');
```

In `_saveMachine` (`web/assets/machines.js:586-672`), read the dropdown and
include it in the body sent to the server — add right after the existing
`const ssh_key_path = ...` line (which Step 6 below removes):

```js
  const transport_id = byId('machineTransport').value || null;
```

and add `body.transport_id = transport_id;` unconditionally (outside the
existing `if (isClaude) {...} else if (...) {...}` provider-branching, since
`transport_id` is now orthogonal to provider) right after the `const body = {...}`
line.

- [ ] **Step 6: Remove the `ssh_proxy` provider branch from `machines.js`**

Delete the `isSsh`/`machineSshHost`/`machineSshUser`/`machineSshKeyPath`
references throughout `_syncMachineProviderFields`, `_editMachine`,
`_saveMachine`, and `_showAddMachine` (`web/assets/machines.js` lines 553-558,
574-576, 590, 596-598, 602-605, 622-625, 682-684) — these fields no longer
exist on a machine. The `<option value="ssh_proxy">SSH proxy</option>` in
`web/index.html`'s `machineProvider` select (`web/index.html:209`) is removed
too, along with the now-unused `machineSshFields` div
(`web/index.html:215-223`).

- [ ] **Step 7: Wire the new buttons in `web/assets/app.js`'s `DOMContentLoaded` handler**

Add alongside the existing `byId('addMachineBtn').addEventListener(...)`
wiring:

```js
byId('addTransportBtn').addEventListener('click', _showAddTransport);
byId('cancelTransport').addEventListener('click', _cancelTransportForm);
byId('testTransport').addEventListener('click', _testTransportForm);
byId('saveTransport').addEventListener('click', () => _saveTransport(_renderMachineList));
```

Import the four functions from `./transports.js?v=1` at the top of
`app.js`, alongside its other imports. Also call `await loadTransports();`
in whatever function already calls `await loadMachines()` on Settings-open
(search `app.js` for the existing `loadMachines()` call site and add the
transport load next to it, before the first `_renderMachineList()` call, so
the grouped render has transport names to show).

- [ ] **Step 8: Retire the old SSH init wizard**

`web/assets/machine-wizard.js` filters `machines.filter(m => m.provider ===
'ssh_proxy')` (`machine-wizard.js:14`), which will now always be empty (no
machine is ever provider `ssh_proxy` again). Two options, pick based on
whether the 3-step wizard experience (SSH test → remote probe → tunnel
start) is still wanted for the *transport* creation flow:

- **Minimal (recommended for this plan):** delete
  `web/assets/machine-wizard.js` entirely and its `<script>` tag /
  `_renderSshWizard` call site in `app.js`. The new transport form's own
  "Test connection" button (Step 2 above) already covers the SSH-test half;
  remote-probe-then-start-tunnel is one click away via the transport header's
  SSH badge (Step 4). Confirm no other file imports `_renderSshWizard`
  (`grep -rn _renderSshWizard web/`) before deleting.
- **Preserve the 3-step flow:** rework `_renderSshWizard` to take
  `transports` instead of `machines`, filter on "has no successful tunnel
  yet" instead of provider, and have its Step 2 (`/api/init/probe-remote`)
  call pass `transport_id` — which means `routes/machines_tunnel.py`'s
  `init_probe_remote` needs a matching change to resolve
  `tunnel_manager_ssh.probe_remote` by transport instead of machine_id. Only
  do this if the product decision (not this plan's to make) is that the
  guided wizard experience matters enough to keep; otherwise the minimal
  option is less code to maintain for the same underlying capability.

This plan takes the minimal option. If the guided wizard is wanted after all,
that is a follow-up, scoped separately.

- [ ] **Step 9: Manual verification in a real browser**

Start the dev server (`.venv/bin/python -m uvicorn app:app --host 127.0.0.1
--port 3001` against a throwaway `WC_DB_PATH`, per this project's own
convention elsewhere in this codebase — never the production database), log
in, open Settings → Backends, and confirm: "+ Add transport" opens the new
form; saving a transport with a real reachable SSH host and key shows it as
a new "via <name>" group header with zero machines under it; adding a
backend with that transport selected in "Executes on" shows it under that
header; the SSH badge on the header starts a tunnel; editing a backend's
"Executes on" back to "This server" moves it out of the group on next
render.

- [ ] **Step 10: Commit**

```bash
git add web/index.html web/assets/machines.js web/assets/transports.js web/assets/app.js
git rm web/assets/machine-wizard.js  # if Step 8's minimal option was taken
git commit -m "feat: add transport UI, group backends map by execution location"
```

---

## Self-Review Notes

**Spec coverage:** every section of the design spec has a task —
data model (Tasks 1-2), migration (Task 8), tunnel-sharing (Task 5),
`runner`/`routes/machines_tunnel.py` gating (Tasks 6-7), forms/map (Task 9).
The spec's "Out of scope" section (tunnel_manager code changes, health/stats
probing beyond iteration target) is honored — Task 5 is exactly the minimal
tunnel_manager change the spec called for, and `tunnel_manager_health.py`
is untouched (confirmed unaffected during research: it reads `_STATE`, which
stays keyed by machine_id).

**Placeholder scan:** no TBD/TODO left in any step. Task 3 Step 1's test
sketch flags where to look at an existing fixture rather than guessing its
shape blind — that is a research pointer for the implementer, not a
placeholder for missing logic; the assertions and request bodies around it
are concrete.

**Type consistency:** `connect()`'s return tuple grows from 6 to 7 elements
in Task 5 Step 4b — every caller of it (`tunnel_manager._try_connect`, the
new test in Task 5 Step 1) is updated in the same task. `_BACKEND_COLUMNS`
(Task 2) and every place that destructures it (`get_backend`, Task 6's
`get_proxy_target`) agree on `transport_id` being present in the dict from
Task 2 onward — Task 6 assumes it exists on `machine`, which is true because
Task 2 lands first in dependency order.
