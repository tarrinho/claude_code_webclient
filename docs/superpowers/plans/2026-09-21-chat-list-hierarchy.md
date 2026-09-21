# Chat List Hierarchy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show which conversation produced which in the chat sidebar, by nesting
children inside their parent's "family card".

**Architecture:** One new table (`chat_subagents`) for the only relationship not
already recorded — Task-tool subagents, which run inside the CLI process and are
observable only in the transcript. Voice handoff (`chats.parent_chat_id`) and
orchestrator membership (`orchestrator_members`) are read as-is, never migrated.
A pure composer merges the three into a `children` array on `GET /api/chats`;
the sidebar renders a bordered card per family.

**Tech Stack:** Python 3.13 / FastAPI / aiosqlite; vanilla ES modules on the
front end; pytest (`unittest.TestCase`) plus behavioural JS tests executed under
node.

**Spec:** `docs/superpowers/specs/2026-09-21-chat-list-hierarchy-design.md`

## Global Constraints

- **Do not reimplement the orchestrator design.** Task chats existing as real
  chats (its Tasks 1–2) and the `chat-list.js:910` predicate switch to
  `parent_chat_id IS NULL` (its Task 8) belong to
  `docs/superpowers/specs/2026-09-20-orchestrator-chats-all-the-way-down-design.md`.
  This plan consumes them.
- **Ordering dependency:** that design's Tasks 1–2 and 8 must land before Task 7
  of this plan, or a task chat that asks a question has no row to carry its
  marker. See spec §10b.
- **§10a is not planned here.** Hiding orchestrator task chats from the flat root
  list awaits Pedro's ruling. No task in this plan hides a chat.
- **`commitOrder` persists by walking the DOM.** Children must be excluded from
  the id list it sends. Assert on the id list, never on rendered markup.
- **Re-apply the `waiting` filter.** `GET /api/orchestrator`'s `waiting` bucket is
  filtered client-side in `chat-list.js`'s `setSupervisor`, not in the endpoint:
  `entry.kind === 'chat' && entry.id && entry.reason !== 'done'`. Measured
  2026-09-21: 61 of 64 conversations were in the bucket and 54 were `done`.
- **Secondary-index rule:** subagent recording runs *after* `messages_batch`. A
  failure there must never cost the turn's transcript write.
- **One nesting level only.** The composer refuses to recurse deeper.
- **`node` is v24.19.0** and `chat-list.js` has no top-level imports, so pure JS
  functions are tested by executing the module, not by matching its source.
- **Shared checkout:** stage by pathspec. Never `git add -A`.

---

## File Structure

| File | Responsibility |
|---|---|
| `db.py` | `chat_subagents` schema + migration entry (follows the `parent_chat_id` ALTER pattern at `db.py:1471`) |
| `routes/db_subagents.py` (new) | The table's accessors. Mirrors `routes/db_images.py` — extracted so the turn-completion path need not import all of `db.py` |
| `transcripts.py` | `_scan_tasks_sync` — full-file Task-block scan with a size-keyed cache, mirroring `_scan_questions_sync` (line 708) |
| `routes/chats.py` | Post-turn capture call, beside `generated_image_record`; `children` in the list payload |
| `chat_tree.py` (new) | `build_chat_tree` — the pure composer. No DB, no DOM, no network |
| `web/assets/chat-list.js` | Family card rendering; child rows excluded from `commitOrder` |
| `web/assets/styles.css` | Card styles |

---

### Task 1: Schema for `chat_subagents`

**Files:**
- Modify: `db.py` (CREATE TABLE block, and the migration dict at `db.py:1471`)
- Test: `tests/test_qa_chat_subagents.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: table `chat_subagents(id, chat_id, tool_use_id, agent_type, description, status, started_at, ended_at)` with `UNIQUE(chat_id, tool_use_id)` and index `idx_chat_subagents_chat`.

- [ ] **Step 1: Write the failing test**

```python
"""QA: the chat_subagents table — schema and its idempotency guarantee."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class ChatSubagentsSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_the_table_exists_with_every_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(chat_subagents)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertEqual(cols, {
            "id", "chat_id", "tool_use_id", "agent_type", "description",
            "status", "started_at", "ended_at",
        })

    async def test_the_same_tool_use_id_cannot_be_recorded_twice(self):
        """The idempotency guarantee. A transcript is re-scanned for reasons
        unrelated to this feature, so a second scan must be a no-op."""
        for _ in range(2):
            await db.db_conn.execute(
                "INSERT OR IGNORE INTO chat_subagents "
                "(chat_id, tool_use_id, agent_type, description, status, started_at) "
                "VALUES ('c1', 'tu_1', 'code-review', 'd', 'running', '2026-09-21T00:00:00Z')")
        await db.db_conn.commit()
        cur = await db.db_conn.execute("SELECT COUNT(*) FROM chat_subagents")
        self.assertEqual((await cur.fetchone())[0], 1)

    async def test_the_same_tool_use_id_in_another_chat_is_a_separate_row(self):
        for chat in ("c1", "c2"):
            await db.db_conn.execute(
                "INSERT OR IGNORE INTO chat_subagents "
                "(chat_id, tool_use_id, agent_type, description, status, started_at) "
                f"VALUES ('{chat}', 'tu_1', 'x', 'd', 'running', '2026-09-21T00:00:00Z')")
        await db.db_conn.commit()
        cur = await db.db_conn.execute("SELECT COUNT(*) FROM chat_subagents")
        self.assertEqual((await cur.fetchone())[0], 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py -v`
Expected: FAIL — `PRAGMA table_info` returns no rows, so `cols` is `set()`.

- [ ] **Step 3: Add the schema**

In `db.py`, beside the other `CREATE TABLE IF NOT EXISTS` statements:

```sql
        -- One row per Task-tool subagent a conversation spawned. Display-only:
        -- there is no transcript and nothing to open. Subagents run INSIDE the
        -- CLI process (CLAUDE.md §0), so the console cannot hook their spawn --
        -- they are observable only in the transcript, and this table is where
        -- that observation is kept.
        --
        -- UNIQUE(chat_id, tool_use_id) with INSERT OR IGNORE is the idempotency
        -- rule, the same one generated_images uses: a transcript gets re-scanned
        -- for reasons that have nothing to do with this feature.
        CREATE TABLE IF NOT EXISTS chat_subagents (
            id           INTEGER PRIMARY KEY,
            chat_id      TEXT NOT NULL,
            tool_use_id  TEXT NOT NULL,
            agent_type   TEXT,
            description  TEXT,
            status       TEXT NOT NULL DEFAULT 'running',
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            UNIQUE(chat_id, tool_use_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_subagents_chat
            ON chat_subagents(chat_id);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_qa_chat_subagents.py
git commit -m "feat: chat_subagents table for display-only subagent nodes"
```

---

### Task 2: Accessors in `routes/db_subagents.py`

**Files:**
- Create: `routes/db_subagents.py`
- Test: `tests/test_qa_chat_subagents.py` (extend)

**Interfaces:**
- Consumes: the Task 1 table.
- Produces:
  - `async def subagent_record(chat_id: str, rows: list[dict]) -> None` — each dict has keys `tool_use_id`, `agent_type`, `description`, `status`, `started_at`, `ended_at`.
  - `async def subagents_for_chats(chat_ids: list[str]) -> dict[str, list[dict]]` — keyed by `chat_id`, each list ordered by `started_at` ascending.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_qa_chat_subagents.py`:

```python
class SubagentAccessorTests(ChatSubagentsSchemaTests):
    """Reuses the schema fixture's asyncSetUp for its database."""

    async def test_record_then_read_back_grouped_by_chat(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        await subagent_record("c1", [
            {"tool_use_id": "tu_2", "agent_type": "test-writer",
             "description": "write tests", "status": "running",
             "started_at": "2026-09-21T00:00:02Z", "ended_at": None},
            {"tool_use_id": "tu_1", "agent_type": "code-review",
             "description": "review", "status": "done",
             "started_at": "2026-09-21T00:00:01Z",
             "ended_at": "2026-09-21T00:00:09Z"},
        ])
        got = await subagents_for_chats(["c1"])
        self.assertEqual([r["tool_use_id"] for r in got["c1"]], ["tu_1", "tu_2"],
                         "must be ordered by started_at, not insertion order")
        self.assertEqual(got["c1"][0]["status"], "done")

    async def test_recording_the_same_rows_twice_changes_nothing(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        row = {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
               "status": "running", "started_at": "2026-09-21T00:00:01Z",
               "ended_at": None}
        await subagent_record("c1", [row])
        await subagent_record("c1", [row])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(len(got["c1"]), 1)

    async def test_a_rescan_promotes_running_to_done(self):
        """The tool_result arrives in a later record, so the second scan of the
        same transcript must be able to finish a row it already inserted."""
        from routes.db_subagents import subagent_record, subagents_for_chats
        await subagent_record("c1", [
            {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
             "status": "running", "started_at": "2026-09-21T00:00:01Z",
             "ended_at": None}])
        await subagent_record("c1", [
            {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
             "status": "done", "started_at": "2026-09-21T00:00:01Z",
             "ended_at": "2026-09-21T00:00:09Z"}])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(len(got["c1"]), 1)
        self.assertEqual(got["c1"][0]["status"], "done")
        self.assertEqual(got["c1"][0]["ended_at"], "2026-09-21T00:00:09Z")

    async def test_an_empty_row_list_is_a_no_op(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        await subagent_record("c1", [])
        self.assertEqual(await subagents_for_chats(["c1"]), {})

    async def test_an_empty_chat_id_list_queries_nothing(self):
        from routes.db_subagents import subagents_for_chats
        self.assertEqual(await subagents_for_chats([]), {})

    async def test_only_the_requested_chats_come_back(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        for chat in ("c1", "c2"):
            await subagent_record(chat, [
                {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
                 "status": "running", "started_at": "2026-09-21T00:00:01Z",
                 "ended_at": None}])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(set(got), {"c1"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py::SubagentAccessorTests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'routes.db_subagents'`

- [ ] **Step 3: Write the module**

```python
# db_subagents.py — the chat_subagents table: one row per Task-tool subagent a
# conversation spawned, recorded when routes/chats.py's post-turn scan first
# finds it in the transcript. Extracted from db.py the same way
# routes/db_images.py is, so the turn-completion path does not need the full
# database module.
from __future__ import annotations

from typing import Any

import db


@db.write
async def subagent_record(chat_id: str, rows: list[dict[str, Any]]) -> None:
    """Record subagents discovered for one chat. Idempotent.

    INSERT OR IGNORE on (chat_id, tool_use_id), then an UPDATE for the
    status/ended_at pair. Both halves are needed and neither is sufficient:
    the insert alone could never finish a row, because a Task's `tool_result`
    arrives in a LATER transcript record than its `tool_use`, so the scan that
    first sees a subagent almost always sees it as `running`. The update alone
    would silently drop a subagent nobody had inserted yet.

    Narrowed to the running -> done direction: a row already `done` is never
    reopened, so a re-scan of an older transcript region cannot un-finish work.
    """
    if not rows:
        return
    await db.db_conn.executemany(
        "INSERT OR IGNORE INTO chat_subagents "
        "(chat_id, tool_use_id, agent_type, description, status, "
        " started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(chat_id, r["tool_use_id"], r.get("agent_type"), r.get("description"),
          r.get("status") or "running", r["started_at"], r.get("ended_at"))
         for r in rows],
    )
    await db.db_conn.executemany(
        "UPDATE chat_subagents SET status = ?, ended_at = ? "
        "WHERE chat_id = ? AND tool_use_id = ? AND status != 'done'",
        [(r.get("status") or "running", r.get("ended_at"), chat_id,
          r["tool_use_id"]) for r in rows],
    )
    await db.db_conn.commit()


async def subagents_for_chats(
    chat_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Subagents for *chat_ids*, grouped by chat, oldest first.

    One query for the whole sidebar rather than one per chat: the list endpoint
    renders every conversation, and a query per row is what makes a sidebar
    slow in proportion to how much work you have done.

    Ordered by `started_at` because spawn order is the only order that means
    anything for a subagent -- it has no recency of its own and no manual
    placement.
    """
    if not chat_ids:
        return {}
    marks = ", ".join("?" * len(chat_ids))
    cursor = await db.db_conn.execute(
        "SELECT chat_id, tool_use_id, agent_type, description, status, "
        "       started_at, ended_at "
        f"FROM chat_subagents WHERE chat_id IN ({marks}) "  # nosec B608: parameterised
        "ORDER BY started_at ASC, id ASC",
        tuple(chat_ids),
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in await cursor.fetchall():
        out.setdefault(row["chat_id"], []).append({
            "tool_use_id": row["tool_use_id"],
            "agent_type": row["agent_type"],
            "description": row["description"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/db_subagents.py tests/test_qa_chat_subagents.py
git commit -m "feat: chat_subagents accessors, idempotent on (chat_id, tool_use_id)"
```

---

### Task 3: `_scan_tasks_sync` — find Task blocks in a transcript

**Files:**
- Modify: `transcripts.py` (beside `_scan_questions_sync`, line 708)
- Test: `tests/test_qa_transcript_task_scan.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `def _scan_tasks_sync(path: Path) -> list[dict[str, Any]]`, each dict having `tool_use_id`, `agent_type`, `description`, `status` (`'running'`|`'done'`), `started_at`, `ended_at`.

- [ ] **Step 1: Write the failing test**

```python
"""QA: finding Task-tool subagents in a transcript.

Subagents run inside the CLI process, so the console cannot hook their spawn
(CLAUDE.md §0). The transcript is the only place they are observable, as
`tool_use` blocks whose name is `Task`. Status comes from pairing a `tool_use`
with its `tool_result`, the same way _scan_questions_sync pairs questions.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import transcripts


def _write(records: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    for record in records:
        tmp.write(json.dumps(record) + "\n")
    tmp.close()
    return Path(tmp.name)


def _task_use(tool_use_id, subagent_type, description, ts):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": "Task",
         "input": {"subagent_type": subagent_type, "description": description}},
    ]}}


def _task_result(tool_use_id, ts):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
    ]}}


class TaskScanTests(unittest.TestCase):

    def setUp(self):
        transcripts._task_scan_cache.clear()

    def test_an_unfinished_task_is_running(self):
        path = _write([_task_use("tu_1", "code-review", "review the diff",
                                 "2026-09-21T10:00:00Z")])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tool_use_id"], "tu_1")
        self.assertEqual(got[0]["agent_type"], "code-review")
        self.assertEqual(got[0]["description"], "review the diff")
        self.assertEqual(got[0]["status"], "running")
        self.assertEqual(got[0]["started_at"], "2026-09-21T10:00:00Z")
        self.assertIsNone(got[0]["ended_at"])

    def test_a_paired_task_is_done_and_carries_its_end_time(self):
        path = _write([
            _task_use("tu_1", "code-review", "review", "2026-09-21T10:00:00Z"),
            _task_result("tu_1", "2026-09-21T10:00:30Z"),
        ])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(got[0]["status"], "done")
        self.assertEqual(got[0]["ended_at"], "2026-09-21T10:00:30Z")

    def test_a_result_arriving_before_its_use_is_still_paired(self):
        """Records are read in file order, and nothing guarantees a result
        cannot be read first after a compaction or a partial write."""
        path = _write([
            _task_result("tu_1", "2026-09-21T10:00:30Z"),
            _task_use("tu_1", "code-review", "review", "2026-09-21T10:00:00Z"),
        ])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(got[0]["status"], "done")

    def test_other_tools_are_ignored(self):
        path = _write([{"message": {"content": [
            {"type": "tool_use", "id": "tu_9", "name": "Bash",
             "input": {"command": "ls"}}]}}])
        self.addCleanup(path.unlink)
        self.assertEqual(transcripts._scan_tasks_sync(path), [])

    def test_askuserquestion_is_not_a_subagent(self):
        """The other scanner's tool must not leak into this one."""
        path = _write([{"message": {"content": [
            {"type": "tool_use", "id": "tu_8", "name": "AskUserQuestion",
             "input": {"questions": []}}]}}])
        self.addCleanup(path.unlink)
        self.assertEqual(transcripts._scan_tasks_sync(path), [])

    def test_several_tasks_come_back_in_start_order(self):
        path = _write([
            _task_use("tu_1", "a", "first", "2026-09-21T10:00:00Z"),
            _task_use("tu_2", "b", "second", "2026-09-21T10:00:05Z"),
        ])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual([r["tool_use_id"] for r in got], ["tu_1", "tu_2"])

    def test_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(
            transcripts._scan_tasks_sync(Path("/nonexistent/x.jsonl")), [])

    def test_a_malformed_line_does_not_lose_the_rest(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        tmp.write("{not json\n")
        tmp.write(json.dumps(
            _task_use("tu_1", "a", "d", "2026-09-21T10:00:00Z")) + "\n")
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink)
        self.assertEqual(len(transcripts._scan_tasks_sync(path)), 1)

    def test_a_task_with_no_input_fields_still_yields_a_row(self):
        """A subagent that named neither type nor description is still a
        subagent; dropping it would hide real work."""
        path = _write([{"timestamp": "2026-09-21T10:00:00Z", "message": {
            "content": [{"type": "tool_use", "id": "tu_1", "name": "Task",
                         "input": {}}]}}])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["agent_type"])

    def test_the_cache_is_keyed_on_size_like_the_question_scan(self):
        path = _write([_task_use("tu_1", "a", "d", "2026-09-21T10:00:00Z")])
        self.addCleanup(path.unlink)
        transcripts._scan_tasks_sync(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                _task_use("tu_2", "b", "d2", "2026-09-21T10:00:05Z")) + "\n")
        self.assertEqual(len(transcripts._scan_tasks_sync(path)), 2,
                         "a grown file must be re-read, not served from cache")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_transcript_task_scan.py -v`
Expected: FAIL — `AttributeError: module 'transcripts' has no attribute '_task_scan_cache'`

- [ ] **Step 3: Write the scanner**

In `transcripts.py`, beside `_question_scan_cache` and `_scan_questions_sync`:

```python
#: (size, rows) per transcript path, keyed the same way _question_scan_cache is
#: and for the same measured reason: every caller polls, and re-reading a
#: 16 MB transcript on each poll spends disk and CPU finding the answer it
#: already had.
_task_scan_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}

_TASK_TOOL: Final[str] = "Task"


def _scan_tasks_sync(path: Path) -> list[dict[str, Any]]:
    """Quick-scan the full transcript for Task-tool subagent blocks.

    Task subagents run INSIDE the CLI process, so the console never sees them
    spawn (CLAUDE.md §0). This is the only place they are observable: a
    `tool_use` block whose name is `Task`, carrying `subagent_type` and
    `description` in its input.

    Status is a pairing, not a second source -- a `tool_use` with a matching
    `tool_result` is done, one without is running. Exactly what
    `_scan_questions_sync` does for AskUserQuestion, and for the same reason:
    the transcript already records both halves, so nothing else has to.

    Both passes complete before anything is returned, so a `tool_result` read
    before its `tool_use` still pairs. Nothing guarantees file order after a
    compaction or a partial write.

    Unlike the question scan, finished rows are NOT skipped: a subagent that
    has completed is still worth showing, with the fact that it finished. The
    caller's UNIQUE index, not this filter, is what keeps the import idempotent.
    """
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    cached = _task_scan_cache.get(key)
    if cached is not None and cached[0] == size:
        return list(cached[1])

    try:
        raw = path.read_bytes()
    except OSError:
        return []

    started: dict[str, dict[str, Any]] = {}
    ended: dict[str, str | None] = {}

    for line in raw.split(b"\n"):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        stamp = str(record.get("timestamp") or "")
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use" and block.get("name") == _TASK_TOOL:
                use_id = str(block.get("id") or "")
                if not use_id:
                    continue
                payload = block.get("input")
                payload = payload if isinstance(payload, dict) else {}
                agent_type = payload.get("subagent_type")
                description = payload.get("description")
                started[use_id] = {
                    "tool_use_id": use_id,
                    "agent_type": str(agent_type) if agent_type else None,
                    "description": str(description) if description else None,
                    "started_at": stamp,
                }
            elif kind == "tool_result":
                result_id = str(block.get("tool_use_id") or "")
                if result_id:
                    ended[result_id] = stamp or None

    rows: list[dict[str, Any]] = []
    for use_id, row in started.items():
        finished = use_id in ended
        rows.append({
            **row,
            "status": "done" if finished else "running",
            "ended_at": ended.get(use_id) if finished else None,
        })

    _task_scan_cache[key] = (size, list(rows))
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_transcript_task_scan.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add transcripts.py tests/test_qa_transcript_task_scan.py
git commit -m "feat: scan transcripts for Task-tool subagents, paired for status"
```

---

### Task 4: Capture subagents after a turn

**Files:**
- Modify: `routes/chats.py` (the post-turn block containing `generated_image_record`, around line 2224)
- Test: `tests/test_qa_chat_subagents.py` (extend)

**Interfaces:**
- Consumes: `transcripts._scan_tasks_sync` (Task 3), `routes.db_subagents.subagent_record` (Task 2).
- Produces: rows in `chat_subagents` after a turn completes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_qa_chat_subagents.py`:

```python
class CaptureOrderingTests(unittest.TestCase):
    """The capture call's placement, asserted on source.

    Placement is the whole property here: the comment already in this block
    says a failure must never risk the turn's transcript write, because that
    is the primary artifact and a subagent row is a secondary index. A
    behavioural test cannot see ordering; this can.
    """

    SOURCE = __import__("pathlib").Path("routes/chats.py").read_text()

    def test_capture_runs_after_the_transcript_write(self):
        batch = self.SOURCE.index("await db.messages_batch(")
        capture = self.SOURCE.index("await db.subagent_record(")
        self.assertLess(batch, capture,
                        "subagent capture must not precede messages_batch")

    def test_capture_sits_with_the_other_secondary_index(self):
        """Next to generated_image_record, which is the same kind of thing
        and already carries the rule in a comment."""
        images = self.SOURCE.index("await db.generated_image_record(")
        capture = self.SOURCE.index("await db.subagent_record(")
        self.assertLess(abs(images - capture), 1200,
                        "the two secondary indexes should stay adjacent")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py::CaptureOrderingTests -v`
Expected: FAIL — `ValueError: substring not found` for `await db.subagent_record(`

- [ ] **Step 3: Add the capture call**

In `routes/chats.py`, immediately after the `generated_image_record` block:

```python
        # Subagents this turn spawned, from the same transcript the turn just
        # wrote. Recorded here for the same reason and under the same rule as
        # the images above: this is a secondary index, and a failure must never
        # risk the turn's transcript write.
        #
        # The scan is over the whole file rather than the new bytes, and that is
        # deliberate -- a Task's `tool_result` lands in a LATER record than its
        # `tool_use`, so a subagent first seen as running is finished by a later
        # pass over a region already read. The size-keyed cache in
        # _scan_tasks_sync keeps that cheap, and the UNIQUE index makes the
        # re-record a no-op.
        if session_id:
            try:
                task_path = transcripts.transcript_path(session_id)
                if task_path is not None:
                    subagents = await asyncio.to_thread(
                        transcripts._scan_tasks_sync, task_path)
                    if subagents:
                        await db.subagent_record(chat_id, subagents)
            except Exception:
                # Logged, never raised. The turn is already stored; losing a
                # sidebar node is not worth failing the request for.
                _log.warning("subagent_capture_failed chat_id=%s", chat_id,
                             exc_info=True)
```

Then export it from `db.py`'s accessor map beside the other `routes.db_*`
entries (the pattern at `db.py:328`), adding:

```python
        "subagent_record": "routes.db_subagents",
        "subagents_for_chats": "routes.db_subagents",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/chats.py db.py tests/test_qa_chat_subagents.py
git commit -m "feat: record a turn's Task subagents, after the transcript write"
```

---

### Task 5: `build_chat_tree` — the pure composer

**Files:**
- Create: `chat_tree.py`
- Test: `tests/test_qa_chat_tree.py` (create)

**Interfaces:**
- Consumes: nothing at runtime — a pure function over already-fetched rows.
- Produces: `def build_chat_tree(chats, subagents, member_of) -> list[dict]`, where `chats` is the list of chat dicts the endpoint already built, `subagents` is `{chat_id: [row, ...]}` from Task 2, and `member_of` is `{chat_id: orchestrator_chat_id}`. Each returned chat gains `children: list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
"""QA: composing the sidebar tree from three unrelated sources.

`build_chat_tree` is deliberately pure -- no database, no DOM, no network.
The three relations have different shapes and different failure modes, and a
composer that fetched its own inputs could not be tested without standing up
all three.
"""
from __future__ import annotations

import unittest

from chat_tree import build_chat_tree


def _chat(chat_id, **kw):
    row = {"id": chat_id, "title": chat_id, "parent_chat_id": None}
    row.update(kw)
    return row


def _sub(tool_use_id, status="running"):
    return {"tool_use_id": tool_use_id, "agent_type": "x", "description": "d",
            "status": status, "started_at": "2026-09-21T10:00:00Z",
            "ended_at": None}


class BuildChatTreeTests(unittest.TestCase):

    def test_a_lone_chat_is_a_root_with_no_children(self):
        out = build_chat_tree([_chat("a")], {}, {})
        self.assertEqual([c["id"] for c in out], ["a"])
        self.assertEqual(out[0]["children"], [])

    def test_children_is_always_present(self):
        """The client renders what the server composed and never infers a
        relation, so the key must not be conditional."""
        out = build_chat_tree([_chat("a")], {}, {})
        self.assertIn("children", out[0])

    def test_a_subagent_becomes_a_child_of_its_chat(self):
        out = build_chat_tree([_chat("a")], {"a": [_sub("tu_1")]}, {})
        self.assertEqual(len(out[0]["children"]), 1)
        self.assertEqual(out[0]["children"][0]["kind"], "subagent")
        self.assertEqual(out[0]["children"][0]["tool_use_id"], "tu_1")

    def test_a_voice_child_nests_under_its_parent_and_leaves_the_roots(self):
        out = build_chat_tree(
            [_chat("parent"), _chat("kid", parent_chat_id="parent")], {}, {})
        self.assertEqual([c["id"] for c in out], ["parent"])
        kids = out[0]["children"]
        self.assertEqual([k["id"] for k in kids], ["kid"])
        self.assertEqual(kids[0]["kind"], "chat")

    def test_an_orchestrator_member_nests_under_the_orchestrator(self):
        out = build_chat_tree(
            [_chat("orch"), _chat("task1")], {}, {"task1": "orch"})
        self.assertEqual([c["id"] for c in out], ["orch"])
        self.assertEqual([k["id"] for k in out[0]["children"]], ["task1"])

    def test_orchestrator_membership_wins_over_a_voice_parent(self):
        """§8's precedence: orchestrator member -> voice parent -> root. A chat
        appears exactly once, so a tie must resolve, not duplicate."""
        out = build_chat_tree(
            [_chat("orch"), _chat("vparent"),
             _chat("both", parent_chat_id="vparent")],
            {}, {"both": "orch"})
        placed = {c["id"]: [k["id"] for k in c["children"]] for c in out}
        self.assertEqual(placed.get("orch"), ["both"])
        self.assertEqual(placed.get("vparent"), [])

    def test_a_chat_appears_exactly_once(self):
        out = build_chat_tree(
            [_chat("orch"), _chat("vparent"),
             _chat("both", parent_chat_id="vparent")],
            {}, {"both": "orch"})
        seen = [c["id"] for c in out] + [
            k["id"] for c in out for k in c["children"] if k["kind"] == "chat"]
        self.assertEqual(sorted(seen), ["both", "orch", "vparent"])

    def test_an_orphan_renders_as_a_root_rather_than_vanishing(self):
        """Losing a conversation because its parent was archived, deleted or
        filtered out by a search would be worse than showing it unnested."""
        out = build_chat_tree([_chat("kid", parent_chat_id="gone")], {}, {})
        self.assertEqual([c["id"] for c in out], ["kid"])

    def test_an_orphaned_orchestrator_member_also_renders_as_a_root(self):
        out = build_chat_tree([_chat("task1")], {}, {"task1": "missing"})
        self.assertEqual([c["id"] for c in out], ["task1"])

    def test_nesting_stops_at_one_level(self):
        """A grandchild attaches to the nearest present ancestor rather than
        creating a second indent level. A malformed parent_chat_id must not be
        able to make the sidebar recurse."""
        out = build_chat_tree(
            [_chat("a"), _chat("b", parent_chat_id="a"),
             _chat("c", parent_chat_id="b")], {}, {})
        self.assertEqual([c["id"] for c in out], ["a"])
        kids = {k["id"] for k in out[0]["children"]}
        self.assertEqual(kids, {"b", "c"})

    def test_a_self_referencing_parent_is_a_root_not_a_hang(self):
        out = build_chat_tree([_chat("a", parent_chat_id="a")], {}, {})
        self.assertEqual([c["id"] for c in out], ["a"])
        self.assertEqual(out[0]["children"], [])

    def test_a_two_chat_cycle_terminates(self):
        out = build_chat_tree(
            [_chat("a", parent_chat_id="b"), _chat("b", parent_chat_id="a")],
            {}, {})
        self.assertEqual(len(out), 2)

    def test_subagents_and_chat_children_share_one_list(self):
        out = build_chat_tree(
            [_chat("a"), _chat("kid", parent_chat_id="a")],
            {"a": [_sub("tu_1")]}, {})
        kinds = sorted(k["kind"] for k in out[0]["children"])
        self.assertEqual(kinds, ["chat", "subagent"])

    def test_root_order_is_preserved(self):
        """The endpoint already sorted by favourites, placement and recency.
        The composer must not resort."""
        out = build_chat_tree([_chat("z"), _chat("a"), _chat("m")], {}, {})
        self.assertEqual([c["id"] for c in out], ["z", "a", "m"])

    def test_the_input_rows_are_not_mutated(self):
        rows = [_chat("a")]
        build_chat_tree(rows, {}, {})
        self.assertNotIn("children", rows[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_tree.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'chat_tree'`

- [ ] **Step 3: Write the composer**

```python
"""chat_tree.py -- compose the sidebar's one-level hierarchy.

Three relations, three shapes, one output. Kept a pure function over rows the
caller already fetched: the sources have different failure modes, and a
composer that fetched its own inputs could not be tested without standing up
all three of them.

Design: docs/superpowers/specs/2026-09-21-chat-list-hierarchy-design.md §8
"""
from __future__ import annotations

from typing import Any

#: One level, and no more. The cap is not a style preference: a malformed
#: `parent_chat_id` -- a cycle, or a chain -- must not be able to make the
#: sidebar recurse, and a grandchild is attached to the nearest present
#: ancestor instead of creating a second indent level the card cannot draw.
_MAX_DEPTH = 1


def build_chat_tree(
    chats: list[dict[str, Any]],
    subagents: dict[str, list[dict[str, Any]]],
    member_of: dict[str, str],
) -> list[dict[str, Any]]:
    """Return *chats* as roots, each carrying a `children` list.

    `member_of` maps a chat id to the orchestrator chat that owns it.

    Precedence when a chat could nest two ways: orchestrator member, then
    voice parent, then root. A chat appears exactly ONCE in the output, so a
    tie has to resolve rather than duplicate -- a conversation rendered twice
    would be two rows that open the same thing and disagree about their status.

    A child whose parent is not in *chats* is returned as a ROOT, never
    dropped. The parent may be archived, deleted, or filtered out by a search,
    and losing a conversation because of that would be worse than showing it
    unnested.
    """
    present = {chat["id"] for chat in chats}

    def _parent_of(chat: dict[str, Any]) -> str | None:
        """The one parent this chat nests under, or None for a root."""
        owner = member_of.get(chat["id"])
        if owner and owner in present and owner != chat["id"]:
            return owner
        voice = chat.get("parent_chat_id")
        if voice and voice in present and voice != chat["id"]:
            return voice
        return None

    # Resolved once for every chat, before anything is attached: attaching as
    # we go would make the result depend on input order for a cycle.
    parent_by_id = {chat["id"]: _parent_of(chat) for chat in chats}

    def _returns_to_itself(chat_id: str) -> bool:
        """Whether following parents from *chat_id* comes back to it.

        A chat inside a cycle has no sensible parent, so it is treated as a
        root. Deciding this up front is what makes the output independent of
        input order: an earlier draft attached as it walked, so for a two-chat
        cycle whichever row came first became the parent of the other -- the
        same malformed data producing two different trees depending on the
        endpoint's sort.
        """
        seen = {chat_id}
        current = chat_id
        while True:
            parent = parent_by_id.get(current)
            if parent is None:
                return False
            if parent == chat_id:
                return True
            if parent in seen:
                return False   # a cycle, but one this chat is not part of
            seen.add(parent)
            current = parent

    roots = {
        chat["id"] for chat in chats
        if parent_by_id[chat["id"]] is None or _returns_to_itself(chat["id"])
    }

    def _host_for(chat_id: str) -> str | None:
        """The nearest ancestor that is itself a root, or None.

        A chain deeper than one level flattens onto the root it reaches rather
        than nesting further -- the card draws one level, so a grandchild joins
        its grandparent's card instead of creating a level with nowhere to go.
        The bounded loop is what guarantees termination regardless of the data.
        """
        current = parent_by_id.get(chat_id)
        for _ in range(_MAX_DEPTH + 2):
            if current is None:
                return None
            if current in roots:
                return current
            current = parent_by_id.get(current)
        return None

    out: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for chat in chats:
        if chat["id"] in roots:
            node = {**chat, "children": []}
            index[chat["id"]] = node
            out.append(node)

    # Subagents first, so a family's own work precedes the conversations it
    # handed off to. Both lists are already in a deliberate order -- spawn
    # order for subagents, the endpoint's order for chats -- so neither is
    # resorted here.
    for node in out:
        for row in subagents.get(node["id"], []):
            node["children"].append({"kind": "subagent", **row})

    for chat in chats:
        if chat["id"] in roots:
            continue
        host_id = _host_for(chat["id"])
        host = index.get(host_id) if host_id else None
        if host is None:
            # Its whole chain is absent. Promote rather than drop.
            node = {**chat, "children": []}
            index[chat["id"]] = node
            out.append(node)
            continue
        host["children"].append({
            "kind": "chat",
            "id": chat["id"],
            "title": chat.get("title"),
            "relation": "orchestrator" if member_of.get(chat["id"])
                        else "voice",
        })

    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_tree.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add chat_tree.py tests/test_qa_chat_tree.py
git commit -m "feat: build_chat_tree, a pure one-level sidebar composer"
```

---

### Task 6: `children` on `GET /api/chats`

**Files:**
- Modify: `routes/chats.py` (`handle_chats_list`, from line 304)
- Test: `tests/test_qa_chat_tree_route.py` (create)

**Interfaces:**
- Consumes: `build_chat_tree` (Task 5), `subagents_for_chats` (Task 2).
- Produces: every chat in the response carries `children: list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
"""QA: GET /api/chats carries the composed tree."""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class ChatsListTreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))
        row = await db.user_get_by_name("alice")
        self.owner = row["id"]
        import pathlib
        pathlib.Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.chat_create("parent", "Parent", None, f"{self.tmp.name}/p",
                             self.owner)

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password})
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    def test_every_chat_carries_a_children_list(self):
        response = self._login().get("/api/chats")
        self.assertEqual(response.status_code, 200, response.text)
        chats = response.json()["chats"]
        self.assertTrue(chats)
        for chat in chats:
            self.assertIn("children", chat)
            self.assertIsInstance(chat["children"], list)

    async def test_a_recorded_subagent_appears_as_a_child(self):
        await db.subagent_record("parent", [
            {"tool_use_id": "tu_1", "agent_type": "code-review",
             "description": "review", "status": "done",
             "started_at": "2026-09-21T10:00:00Z",
             "ended_at": "2026-09-21T10:00:30Z"}])
        response = self._login().get("/api/chats")
        parent = next(c for c in response.json()["chats"] if c["id"] == "parent")
        self.assertEqual(len(parent["children"]), 1)
        self.assertEqual(parent["children"][0]["kind"], "subagent")
        self.assertEqual(parent["children"][0]["agent_type"], "code-review")

    async def test_a_voice_child_is_nested_and_not_also_a_root(self):
        await db.chat_create("kid", "Voice", None, f"{self.tmp.name}/p",
                             self.owner)
        # chat_update's signature is (chat_id, owner_id, **fields) -- the
        # owner is positional and not optional.
        await db.chat_update("kid", self.owner, parent_chat_id="parent")
        response = self._login().get("/api/chats")
        chats = response.json()["chats"]
        self.assertNotIn("kid", [c["id"] for c in chats])
        parent = next(c for c in chats if c["id"] == "parent")
        self.assertEqual([k["id"] for k in parent["children"]], ["kid"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_tree_route.py -v`
Expected: FAIL — `KeyError: 'children'` / the assertion that `children` is present.

- [ ] **Step 3: Compose in the handler**

In `routes/chats.py`'s `handle_chats_list`, after the list of chat dicts is
built and before it is returned:

```python
    # One query for every conversation's subagents rather than one per row: a
    # sidebar that costs a query per chat gets slower in proportion to how much
    # work you have done, which is backwards.
    chat_ids = [row["id"] for row in items]
    subagents = await db.subagents_for_chats(chat_ids)
    member_of = await db.orchestrator_member_owners(chat_ids)
    items = chat_tree.build_chat_tree(items, subagents, member_of)
```

Add `import chat_tree` to the module's imports.

Add to `routes/db_orchestrator.py` (or wherever the orchestrator accessors
live), exported through `db.py`'s map the same way Task 4 exported the
subagent accessors:

```python
async def orchestrator_member_owners(chat_ids: list[str]) -> dict[str, str]:
    """Map a member chat id to the orchestrator's OWN chat id.

    Returns `{}` for an empty input rather than querying: the sidebar asks on
    every load, and `IN ()` is not valid SQL.
    """
    if not chat_ids:
        return {}
    marks = ", ".join("?" * len(chat_ids))
    cursor = await db.db_conn.execute(
        "SELECT m.chat_id AS member, o.chat_id AS owner "
        "FROM orchestrator_members m "
        "JOIN orchestrators o ON o.id = m.orchestrator_id "
        f"WHERE m.chat_id IN ({marks})",  # nosec B608: parameterised
        tuple(chat_ids),
    )
    return {r["member"]: r["owner"] for r in await cursor.fetchall()
            if r["owner"]}
```

> **Gated on the orchestrator design.** `orchestrators.chat_id` is introduced by
> its Task 1. If that column does not exist yet, this accessor returns `{}` and
> the orchestrator branch of the tree is simply empty — the voice and subagent
> branches work regardless. Do not add the column here.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_tree_route.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/chats.py routes/db_orchestrator.py db.py tests/test_qa_chat_tree_route.py
git commit -m "feat: GET /api/chats composes a children array per chat"
```

---

### Task 7: The family card in the sidebar

**Files:**
- Modify: `web/assets/chat-list.js` (`renderSection` around line 299; `commitOrder` around line 172)
- Modify: `web/assets/styles.css`
- Test: `tests/test_qa_chat_card.py` (create)

**Interfaces:**
- Consumes: `children` from Task 6.
- Produces: `export function childRowsFor(chat, expanded)` — a pure function returning the child descriptors to render, so the collapse rule is testable under node.

> **Do this task only after the orchestrator design's Tasks 1–2 and 8 have
> landed.** Their Task 8 hides non-root chats from the flat list; this task is
> where those chats become visible again, inside their card. Shipping this
> first is harmless; shipping *their* Task 8 first without this loses the
> question marker for a task chat, which is the gap spec §10b names.

- [ ] **Step 1: Write the failing test**

```python
"""QA: the family card, and the ordering rule it exists to protect.

The behavioural half runs the real module under node -- node is v24.19.0 here
and chat-list.js has no top-level imports, so it can be imported directly. A
source-text assertion cannot tell a correct collapse rule from a broken one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_LIST = ROOT / "web" / "assets" / "chat-list.js"
NODE = shutil.which("node")


def _child_rows(children, expanded):
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const m = await import({json.dumps(module.as_uri())});
        const out = m.childRowsFor(
            {{id: 'a', children: {json.dumps(children)}}},
            {json.dumps(expanded)});
        process.stdout.write(JSON.stringify(out));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout)


def _sub(name):
    return {"kind": "subagent", "tool_use_id": name, "agent_type": name,
            "description": "d", "status": "running",
            "started_at": "2026-09-21T10:00:00Z"}


def _age(stamp, now):
    """childAge(stamp) with Date.now() pinned, so the assertion is stable.

    Without pinning, an age test is a clock test: it passes today and drifts
    tomorrow.
    """
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const fixed = new Date({json.dumps(now)}).getTime();
        Date.now = () => fixed;
        const m = await import({json.dumps(module.as_uri())});
        process.stdout.write(JSON.stringify(m.childAge({json.dumps(stamp)})));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout)


@unittest.skipIf(NODE is None, "node is required to execute the module")
class ChildRowsTests(unittest.TestCase):

    def test_no_children_renders_nothing(self):
        self.assertEqual(_child_rows([], False), [])

    def test_a_small_family_renders_in_full_while_collapsed(self):
        """Five or fewer is the threshold: a typical two-or-three-subagent
        turn should be readable without a click."""
        kids = [_sub(f"s{n}") for n in range(5)]
        self.assertEqual(len(_child_rows(kids, False)), 5)

    def test_a_large_family_collapses_by_default(self):
        kids = [_sub(f"s{n}") for n in range(6)]
        self.assertEqual(_child_rows(kids, False), [])

    def test_a_large_family_expands_on_request(self):
        kids = [_sub(f"s{n}") for n in range(6)]
        self.assertEqual(len(_child_rows(kids, True)), 6)

    def test_a_running_subagent_reports_its_age(self):
        """§7: a stuck `running` row must read as stale rather than as active
        work. There is no timeout sweeper, so the age is the only truth on
        offer -- if it is missing, a dead subagent looks busy for ever."""
        out = _age("2026-09-21T10:00:00Z", "2026-09-21T10:45:00Z")
        self.assertEqual(out, "45m")

    def test_an_absent_timestamp_yields_no_age_rather_than_NaN(self):
        self.assertEqual(_age(None, "2026-09-21T10:45:00Z"), "")

    def test_a_malformed_timestamp_yields_no_age(self):
        self.assertEqual(_age("not-a-date", "2026-09-21T10:45:00Z"), "")

    def test_exactly_the_threshold_still_renders(self):
        """Boundary: 5 renders, 6 collapses. Asserted because an off-by-one
        here is invisible until someone runs exactly five subagents."""
        self.assertEqual(len(_child_rows([_sub(f"s{n}") for n in range(5)],
                                         False)), 5)
        self.assertEqual(_child_rows([_sub(f"s{n}") for n in range(6)],
                                     False), [])


class CommitOrderExclusionTests(unittest.TestCase):
    """The regression that would silently corrupt stored `position`.

    commitOrder builds what it persists by walking the DOM. A child row inside
    a card is not a root, so it must never reach the id list sent to
    PUT /api/chats/order. Asserted on the id list, never on the markup.
    """

    SOURCE = CHAT_LIST.read_text()

    def test_child_rows_are_excluded_from_the_persisted_order(self):
        start = self.SOURCE.index("function commitOrder(")
        body = self.SOURCE[start:start + 1600]
        self.assertIn("dataset.child !== '1'", body)

    def test_child_rows_are_marked_when_rendered(self):
        self.assertIn("dataset.child = '1'", self.SOURCE)

    def test_child_rows_are_not_draggable(self):
        self.assertIn("draggable = false", self.SOURCE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_card.py -v`
Expected: FAIL — node reports `m.childRowsFor is not a function`.

- [ ] **Step 3: Implement the card**

In `web/assets/chat-list.js`, beside `groupChats` and `floatActive`:

```javascript
/** How many children a card shows before it collapses itself.
 *
 *  Five shows a typical two-or-three-subagent turn in full while refusing to
 *  let a ten-way fan-out push every other conversation off screen. A supervisor
 *  fan-out is exactly when the tree is most useful and exactly when it is most
 *  crowded, so the default has to favour the reader over completeness.
 */
export const CHILD_COLLAPSE_AT = 5;

/** How long ago a subagent started, for the `running` label.
 *
 *  A local copy rather than an import of server-stats.js's `_agoText`, and the
 *  duplication is deliberate: `chat-list.js` has NO top-level imports, which
 *  is what lets it be imported and executed directly under node. Adding an
 *  import here to save nine lines would cost the ability to test this file
 *  behaviourally at all, and `_stale` above is already a local helper for the
 *  same reason (server.js keeps its own copy of `card()` on the same grounds).
 */
export function childAge(stamp) {
  if (!stamp) return '';
  const then = new Date(/Z$|[+-]\d\d:?\d\d$/.test(stamp) ? stamp : `${stamp}Z`);
  if (Number.isNaN(then.getTime())) return '';
  const seconds = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
  if (seconds < 90) return `${seconds}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/** The child rows to render for one chat.
 *
 *  Pure, and exported, so the collapse rule is testable by execution rather
 *  than by matching source -- a text assertion cannot tell a correct threshold
 *  from an off-by-one.
 */
export function childRowsFor(chat, expanded) {
  const children = chat.children || [];
  if (!children.length) return [];
  if (children.length > CHILD_COLLAPSE_AT && !expanded) return [];
  return children;
}
```

In `renderSection`, after a chat's own row is built:

```javascript
      // A chat with children becomes a card: the parent row, then its children
      // at full width inside a bordered container. Containment rather than
      // indentation -- see the design's §5. Indentation costs horizontal space
      // in a 380px sidebar, and subagent names are the text that identifies
      // which subagent is which.
      const kids = chat.children || [];
      if (kids.length) {
        item.classList.add('chat-family-head');
        const family = document.createElement('div');
        family.className = 'chat-family';
        family.appendChild(item);

        const shown = childRowsFor(chat, expandedFamilies.has(chat.id));
        if (kids.length > CHILD_COLLAPSE_AT && !shown.length) {
          const more = document.createElement('button');
          more.type = 'button';
          more.className = 'chat-family-more';
          more.textContent = `${kids.length} children`;
          more.onclick = () => {
            expandedFamilies.add(chat.id);
            render();
          };
          family.appendChild(more);
        }
        for (const kid of shown) {
          family.appendChild(_childRow(kid, currentId));
        }
        list.appendChild(family);
        continue;
      }
```

And the child row builder:

```javascript
  /** One row inside a family card.
   *
   *  A display-only subagent is deliberately not a link and carries no menu:
   *  there is nothing to open. A child CHAT is clickable like any other.
   *
   *  Every child row carries data-child="1", which is what keeps it out of
   *  commitOrder's id list. That exclusion is the whole reason this design
   *  nests with a card rather than an indent: commitOrder builds what it
   *  persists by walking the DOM, so a child left in that walk would have its
   *  position written as if it were a root.
   */
  function _childRow(kid, currentId) {
    const row = document.createElement('div');
    row.className = 'chat-child';
    row.dataset.child = '1';
    row.draggable = false;

    if (kid.kind === 'subagent') {
      const mark = document.createElement('span');
      mark.className = 'chat-child-mark';
      row.appendChild(mark);
      const label = document.createElement('span');
      label.className = 'chat-child-label';
      label.textContent = `subagent · ${kid.agent_type || 'agent'}`;
      row.appendChild(label);
      const state = document.createElement('span');
      state.className = 'chat-child-state';
      // A running subagent carries its age, not just the word "running".
      // The CLI can die without ever writing a tool_result, so a row can sit
      // `running` for ever -- and there is deliberately no timeout sweeper,
      // because inventing a "probably dead" threshold would report a guess as
      // a fact. The age is the truth that is actually available, and it makes
      // a stale row visible as stale rather than as active work.
      state.textContent = kid.status === 'done'
        ? 'done'
        : `running ${childAge(kid.started_at)}`;
      row.appendChild(state);
      return row;
    }

    row.dataset.chatId = kid.id;
    row.classList.add('chat-child-chat');
    if (kid.id === currentId) row.classList.add('active');
    const dot = document.createElement('span');
    dot.className = 'chat-child-dot';
    // Re-applied here, not inherited: the `waiting` bucket is filtered in
    // setSupervisor and NOT in the endpoint, and it carries three reasons.
    // Measured 2026-09-21: 61 of 64 conversations were in it and 54 were
    // "done", so a second consumer that trusts the bucket marks nearly
    // everything.
    if (waitingIds.has(kid.id)) dot.classList.add('chat-child-asks');
    row.appendChild(dot);
    const label = document.createElement('span');
    label.className = 'chat-child-label';
    label.textContent = kid.title || kid.id;
    row.appendChild(label);
    const rel = document.createElement('span');
    rel.className = 'chat-child-state';
    rel.textContent = kid.relation === 'orchestrator' ? 'task' : 'voice';
    row.appendChild(rel);
    row.onclick = () => onOpen(kid.id);
    return row;
  }
```

Declare the expansion state beside `unreadIds` and `endedIds`:

```javascript
  // Which families the user has expanded. Per-browser, like unreadIds and
  // endedIds: it is a viewing preference, not a property of the conversation,
  // so it does not belong in the database.
  let expandedFamilies = new Set();
```

And in `commitOrder`, extend the filter chain:

```javascript
      .filter(node => node.dataset.child !== '1')
```

- [ ] **Step 4: Add the card styles**

In `web/assets/styles.css`:

```css
.chat-family{border:1px solid var(--line);border-radius:7px;background:var(--panel2);margin:4px 0;overflow:hidden;position:relative}
.chat-family::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.chat-family-head{background:transparent}
.chat-child{display:flex;align-items:center;gap:6px;padding:4px 10px 4px 20px;font-size:12px;color:var(--muted)}
.chat-child-chat{cursor:pointer;color:var(--fg)}
.chat-child-chat:hover{background:var(--panel)}
.chat-child-chat.active{background:var(--panel)}
.chat-child-mark{width:9px;height:9px;border-radius:2px;background:#2d3444;border:1.5px solid var(--muted);flex:0 0 auto}
.chat-child-dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex:0 0 auto}
.chat-child-asks{background:var(--warn)}
.chat-child-label{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-child-state{font-size:10px;color:var(--muted);flex:0 0 auto}
.chat-family-more{display:block;width:100%;text-align:left;padding:4px 10px 4px 20px;font-size:11px;color:var(--accent);background:none;border:0;cursor:pointer}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_card.py -v`
Expected: PASS (11 tests — 8 `ChildRowsTests`, 3 `CommitOrderExclusionTests`)

- [ ] **Step 6: Sync the asset cache-busters**

Run: `.venv/bin/python bin/wc-asset-versions.py && .venv/bin/python bin/wc-asset-versions.py --check`
Expected: `all N asset references match their content`

- [ ] **Step 7: Commit**

```bash
git add web/assets/chat-list.js web/assets/styles.css web/index.html web/assets/app.js tests/test_qa_chat_card.py
git commit -m "feat: family card in the sidebar, children excluded from stored order"
```

---

### Task 8: Delete subagents with their parent chat

**Files:**
- Modify: `routes/db_chats.py` (the chat-delete path)
- Test: `tests/test_qa_chat_subagents.py` (extend)

**Interfaces:**
- Consumes: the Task 1 table.
- Produces: no orphan `chat_subagents` rows after a chat is deleted.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_qa_chat_subagents.py`:

```python
class SubagentRetentionTests(ChatSubagentsSchemaTests):

    async def test_deleting_a_chat_removes_its_subagents(self):
        """A subagent row's only lifetime is its chat's. There is no
        independent expiry: a row is small, and 'probably dead' is a guess."""
        import pathlib
        from routes.db_subagents import subagent_record, subagents_for_chats
        pathlib.Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.user_create("bob", None, "x")
        owner = (await db.user_get_by_name("bob"))["id"]
        await db.chat_create("c1", "C", None, f"{self.tmp.name}/p", owner)
        await subagent_record("c1", [
            {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
             "status": "running", "started_at": "2026-09-21T10:00:00Z",
             "ended_at": None}])
        await db.chat_delete("c1", owner)
        self.assertEqual(await subagents_for_chats(["c1"]), {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py::SubagentRetentionTests -v`
Expected: FAIL — the row survives, so `subagents_for_chats` returns one entry.

- [ ] **Step 3: Delete the rows with the chat**

In `routes/db_chats.py`'s delete path, beside the other owned-row cleanups:

```python
    # Subagent nodes are owned by their chat and have no independent lifetime.
    # Deliberately a real delete rather than a soft one: the row carries no
    # history worth keeping once the conversation that spawned it is gone.
    await db.db_conn.execute(
        "DELETE FROM chat_subagents WHERE chat_id = ?", (chat_id,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_chat_subagents.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/db_chats.py tests/test_qa_chat_subagents.py
git commit -m "feat: delete subagent rows with the chat that owns them"
```

---

### Task 9: Full-suite check and deploy

**Files:** none

- [ ] **Step 1: Run every test this plan touched**

```bash
.venv/bin/python -m pytest -q -p no:randomly \
  tests/test_qa_chat_subagents.py tests/test_qa_transcript_task_scan.py \
  tests/test_qa_chat_tree.py tests/test_qa_chat_tree_route.py \
  tests/test_qa_chat_card.py tests/test_qa_chat_order.py \
  tests/test_frontend.py tests/test_qa_asset_versions_match_content.py
```

Expected: all pass. `test_qa_chat_order.py` is in the list on purpose — it is
the existing contract for manual placement, and it must be untouched by this
work.

- [ ] **Step 2: Check the resource guard before the full suite**

```bash
.venv/bin/python -m resource_guard --cost-mb 700
```

If it refuses, wait rather than overriding: it exists because a suite run
alongside several agents SIGKILLed the console at a 720 MB peak, and this host
hard-locked once from memory pressure.

- [ ] **Step 3: Run the chunked suite**

```bash
WC_SUITE_OUT=/tmp/wc-suite-hierarchy bash bin/run-suite-chunked.sh
```

A single-process run is OOM-killed on this box; that is why this runner exists.
Read its NEEDS ATTENTION list and attribute any failure before proceeding — a
chunked green does not license "the whole suite is green".

- [ ] **Step 4: Commit and deploy**

```bash
git push origin main
bash bin/wc-deploy.sh
```

- [ ] **Step 5: Verify live rather than assume**

```bash
systemctl --user is-active webconsole.service
curl -s -o /dev/null -w "%{http_code}\n" https://kali-2.tail850c40.ts.net/login
```

Expected: `active`, `200`. Then open the sidebar and confirm a chat with
children renders as a card, drag a family, reload, and confirm the order held.

---

## Deferred — gated on Pedro's §10a ruling

Not planned above, and not to be implemented without his decision:

- Hiding orchestrator task chats from the flat root list (spec §10a). If he
  approves it, the edit is spec §4.3 and §8, and in code it is the orchestrator
  design's Task 8 predicate plus one line in `build_chat_tree` — the composer
  already places members under their orchestrator, so hiding is the absence of
  the promote-to-root fallback for that case, not new logic.
- Re-parenting or detaching by hand (spec §11).
