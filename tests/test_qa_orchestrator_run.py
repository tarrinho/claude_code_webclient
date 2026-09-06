"""QA coverage for a orchestrator actually running a task.

The feature had never once executed one. A live run against a real backend
found eight faults stacked on top of each other, and every one of them had to
be fixed before a single "create a file" goal could complete. These tests pin
each fault so the stack cannot reassemble itself.

The faults, in the order they surfaced:

1. **No backend reached the child at all.** The engine invents chat ids --
   ``uuid4().hex`` for planning, ``subtask_<id>`` per task -- and backend
   resolution is keyed on a ``chats`` row. So ``chat_routing`` found nothing,
   ``get_backend`` returned ``{}``, and the child ran with no base URL and no
   key while ``CLAUDE_CODE_SIMPLE=1`` also blocked the host login. Every turn
   died on "Not logged in - Please run /login".
2. **No model was chosen either.** ``None`` was passed, so the CLI used its own
   default and a gateway serving one local model answered 429 "No deployments
   available for selected model, Passed model=claude-opus-5" -- a routing fault
   dressed as a capacity one.
3. **The prompt contradicted the parser.** The system prompt showed ``<<PLAN``
   alone on a line; the request text asked for ``<<PLAN>>``. A model obeying
   either produced a block the parser could not find.
4. **A brace template invited a markdown table.** Shown ``{title}`` and
   ``{#{taskId}}``, the model reasonably replied with a table, which the
   line-based parser read as nothing.
5. **An unparsable plan was reported as success.** Zero tasks fell through to
   the scheduler, which found an empty graph, decided ``all_done()`` and set
   "done" at 0%. A goal that never ran looked exactly like one that worked.
6. **The task number became the title.** The lazy group stopped at the first
   ":", so rows were named "1" and "2".
7. **Task rows collided across supervisors.** ``PlanParser`` numbers from 1 per
   plan and ``orchestrator_tasks.id`` is a global PRIMARY KEY, so the second
   orchestrator's insert failed on the UNIQUE constraint -- swallowed by a bare
   warning, so the task ran to completion while the list stayed empty.
8. **The orchestrator's progress was never written.** Task rows carried theirs;
   the orchestrator row did not, so every finished run still displayed 0%.

Verified end to end after the fixes: status went running -> done at 100%, the
task reported done, and hello.txt existed with the right contents.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import config
import db
import runner
import orchestrator


class PlanParsingTests(unittest.TestCase):
    """The parser and the prompt must ask for the same thing."""

    def test_the_title_is_the_title_not_the_task_number(self):
        """Rows were displayed as "1" and "2" -- the number, not the name."""
        tasks = orchestrator.PlanParser.parse(
            "<<PLAN\nTask 1: Create the file - Write hello.txt\n>>")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Create the file")
        self.assertEqual(tasks[0].description, "Write hello.txt")

    def test_both_delimiters_the_prompt_has_asked_for_are_accepted(self):
        """The system prompt said <<PLAN; the request text said <<PLAN>>."""
        for opener in ("<<PLAN", "<<PLAN>>"):
            tasks = orchestrator.PlanParser.parse(
                f"{opener}\nTask 1: A title - a description\n>>")
            self.assertEqual(len(tasks), 1, f"{opener!r} produced no tasks")
            self.assertEqual(tasks[0].title, "A title")

    def test_a_placeholder_copied_from_the_instructions_is_not_a_model(self):
        """A model that copies the example emits [:model-name] verbatim.

        Passing that on produced 400 "Invalid model name passed in
        model=model-name" -- and where the gateway was tolerant, a silent
        substitution of the CLI default instead.
        """
        for placeholder in ("model-name", "model_id", "model"):
            tasks = orchestrator.PlanParser.parse(
                f"<<PLAN\nTask 1: T - d [:{placeholder}]\n>>")
            self.assertIsNone(tasks[0].model,
                              f"[:{placeholder}] must not become a model id")

    def test_a_real_model_suggestion_is_still_honoured(self):
        """The placeholder guard must not swallow a genuine choice."""
        tasks = orchestrator.PlanParser.parse(
            "<<PLAN\nTask 1: Big one - think hard [:claude-opus-5]\n>>")
        self.assertEqual(tasks[0].model, "claude-opus-5")

    def test_a_line_with_no_dash_is_all_title(self):
        """It used to raise AttributeError on a None description group."""
        tasks = orchestrator.PlanParser.parse("<<PLAN\nTask 1: Just do the thing\n>>")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Just do the thing")
        self.assertEqual(tasks[0].description, "")

    def test_a_markdown_table_yields_no_tasks(self):
        """Not a fix -- a fact the caller must handle.

        Given a brace template the model replies with a table, and the parser
        reads lines. This is pinned so the "zero tasks is an error" branch has
        a reason to exist.
        """
        table = (
            "<<PLAN>>\n"
            "| # | Task | Description |\n"
            "|---|------|-------------|\n"
            "| 1 | Create hello.txt | Write one word |\n"
            ">>"
        )
        self.assertEqual(orchestrator.PlanParser.parse(table), [])

    def test_the_prompt_and_the_parser_agree_on_the_delimiter(self):
        """Guards the cause rather than the symptom.

        The two instructions disagreed for as long as the feature existed, so
        the check is that every delimiter the prompt shows is one the parser
        accepts.
        """
        import re
        source = orchestrator.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for shown in set(re.findall(r"<<PLAN>?>?", text)):
            self.assertTrue(
                orchestrator._PLAN_START_RE.search(shown.rstrip(".") + "\n")
                or shown in ("<<PLAN>>..>>",),
                f"the source shows {shown!r} but the parser will not match it",
            )


class BackendFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The central fault: the engine could reach no backend at all."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", self.tmp.name)
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        # Keyword arguments throughout: the positional order is
        # (id, name, host, port, api_key, model, base_url, description, owner)
        # and getting it wrong silently created a machine with the model in the
        # key field, which no assertion here would have noticed.
        machine_id = uuid.uuid4().hex
        await db.ai_machine_create(
            machine_id=machine_id, name="Gateway", host="gw.example.com",
            port=443, api_key="k", model="vllm/Local-Model",
            base_url="https://gw.example.com", description="",
            owner_id="alice", provider="claude_code",
        )
        await db.ai_machine_activate(machine_id, "alice")

    async def asyncTearDown(self):
        try:
            await db.close()
        except Exception:  # noqa: BLE001,S110 -- must not mask the real failure
            pass
        self.root_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_an_id_that_is_not_a_conversation_resolves_nothing_without_an_owner(self):
        """The behaviour every existing caller relies on, unchanged."""
        self.assertEqual(await runner.get_backend(uuid.uuid4().hex), {})

    async def test_the_owner_fallback_finds_the_active_machine(self):
        backend = await runner.get_backend(uuid.uuid4().hex, "alice")
        self.assertEqual(backend.get("base_url"), "https://gw.example.com")
        self.assertEqual(backend.get("api_key"), "k")

    async def test_the_child_env_then_carries_credentials(self):
        """The property that actually failed: an unauthenticated subprocess.

        Without a base URL and key, CLAUDE_CODE_SIMPLE=1 also blocks the host
        login, so the turn died on "Not logged in".
        """
        env = runner._build_env(await runner.get_backend(uuid.uuid4().hex, "alice"))
        self.assertIn("ANTHROPIC_BASE_URL", env)
        self.assertIn("ANTHROPIC_API_KEY", env)

    async def test_the_model_falls_back_to_the_backends_own(self):
        """Passing None let the CLI pick a model the gateway does not serve."""
        self.assertEqual(
            await runner.get_default_model(owner="alice"), "vllm/Local-Model")

    async def test_another_owners_machine_is_not_used(self):
        """The fallback must stay owner-scoped."""
        self.assertEqual(await runner.get_backend(uuid.uuid4().hex, "bob"), {})


class TaskRowIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Task rows collided between supervisors, silently."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", self.tmp.name)
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        try:
            await db.close()
        except Exception:  # noqa: BLE001,S110
            pass
        self.root_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_the_schema_makes_a_plan_local_id_collide(self):
        """The fact the namespacing exists for: id is a global primary key.

        PlanParser numbers from 1 within a plan, so every orchestrator produces a
        t001. Writing that raw meant the second orchestrator's insert failed on
        the UNIQUE constraint -- caught by a bare warning, so its task ran to
        completion while the task list stayed empty and progress sat at 0%.
        """
        for name in ("first", "second"):
            await db.orchestrator_create(uuid.uuid4().hex, name, "", "alice")
        sups = await db.orchestrator_list("alice")
        await db.orchestrator_task_create(
            orchestrator_id=sups[0]["id"], task_id="t001", title="A",
            description="d", model=None, parent_task_id=None, depends_on=[])
        # The specific error, not bare Exception: this asserts the UNIQUE
        # constraint is what refuses the write. `Exception` also passes when the
        # call signature drifts and raises TypeError, which is the failure that
        # would quietly retire the test rather than the collision it names.
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            await db.orchestrator_task_create(
                orchestrator_id=sups[1]["id"], task_id="t001", title="B",
                description="d", model=None, parent_task_id=None, depends_on=[])
        self.assertIn("orchestrator_tasks.id", str(caught.exception))

    async def test_a_namespaced_id_lets_two_supervisors_both_have_task_one(self):
        for name in ("first", "second"):
            await db.orchestrator_create(uuid.uuid4().hex, name, "", "alice")
        sups = await db.orchestrator_list("alice")
        for sup in sups:
            await db.orchestrator_task_create(
                orchestrator_id=sup["id"], task_id=f"{sup['id'][:8]}_t001",
                title="Create the file", description="d", model=None,
                parent_task_id=None, depends_on=[])
        for sup in sups:
            rows = await db.orchestrator_tasks_get(sup["id"], "alice")
            self.assertEqual(len(rows), 1, "each orchestrator keeps its own task")
            self.assertEqual(rows[0]["title"], "Create the file")

    async def test_the_engine_namespaces_with_the_supervisor_id(self):
        """Pins the shape, so a future edit cannot go back to a raw t001."""
        source = Path(orchestrator.__file__).read_text(encoding="utf-8")
        self.assertIn("_row_id", source)
        self.assertIn("self.orchestrator_id[:8]", source)
        self.assertIn("task_id=node.id", source,
                      "the DB write must use the namespaced id the graph holds")


class ProgressAndReportingTests(unittest.TestCase):
    """A run that ends must say what happened, and how far it got."""

    def setUp(self):
        self.source = Path(orchestrator.__file__).read_text(encoding="utf-8")

    def test_the_supervisors_own_progress_is_persisted(self):
        """Task rows carried progress; the orchestrator row never did."""
        self.assertIn("_persist_progress", self.source)
        self.assertIn("progress_pct=self.graph.overall_progress()", self.source)

    def test_an_unparsable_plan_is_an_error_not_a_completed_run(self):
        """Zero tasks used to reach the scheduler and be reported as done."""
        self.assertIn("plan_unparsed", self.source)

    def test_a_failed_run_records_the_reason_for_the_user(self):
        """The reason used to exist only in the server log."""
        self.assertIn("Run failed:", self.source)

    def test_the_failure_path_imports_db_itself(self):
        """db is function-local throughout this module.

        The first version of the failure report reached for the try block's
        import, which had not run when the failure came before it, so it raised
        UnboundLocalError and swallowed the very message it existed to write.
        """
        block = self.source.split("await self._set_status(\"error\")", 1)[1][:900]
        self.assertIn("import db", block)

    def test_task_update_failures_are_no_longer_swallowed(self):
        """`except: pass` around the task writes hid fault 7 for weeks."""
        self.assertNotIn("# noqa: BLE001, S110\n                pass", self.source)


if __name__ == "__main__":
    unittest.main()
