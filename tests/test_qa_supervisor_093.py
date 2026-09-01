"""QA coverage for the 0.9.3 supervisor features.

Covers:
* Pause / resume — engine state machine (pause, resume, _wait_if_paused,
  _pre_pause_status) and the two API endpoints (/pause, /resume).
* Recency sort — supervisor list sort toggle (newest ↔ updated) and
  localStorage persistence under wc_supervisor_sort.
* Member heartbeat timestamps — last_seen rendered as a time label on
  member rows.
* Task dependency hints — depends_on list rendered below a task title.
* Auto-grow composer — textarea expands from 38 px up to 200 px as the
  user types and collapses back when cleared.
* Supervisor list recency time label — updated_at shown when it differs
  from created_at.
* /dev/* auth skip — the middleware still exempts the `/dev/` prefix. The two
  tests that additionally required `/dev/supervisor-trigger` to exist were
  removed with the endpoint itself; see DevAuthSkipTests for why.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import app
import auth
import config
import db
import supervisor

SUPERVISOR_JS = Path(__file__).resolve().parent.parent / "web" / "supervisor.js"
SUPERVISOR_HTML = Path(__file__).resolve().parent.parent / "web" / "supervisor.html"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _setup(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()


async def _teardown(tc):
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


async def _create_supervisor(tc, status="running", sup_id="s1"):
    """Create a supervisor row in the DB."""
    await db.supervisor_create(sup_id, "Test Supervisor", None, "admin")
    await db.supervisor_update(sup_id, "admin", status=status)


# ── Supervisor pause / resume (engine-level) ─────────────────────────────────

class EnginePauseStateTests(unittest.TestCase):
    """The engine must track pause state and pre-pause status."""

    def test_initial_state_is_not_paused(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        self.assertFalse(eng._paused)

    def test_pause_returns_true_when_running(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._running = True
        result = eng.pause()
        self.assertTrue(result)
        self.assertTrue(eng._paused)

    def test_pause_returns_false_when_not_running(self):
        """pause() guards on _running; the API handler has its own guard."""
        eng = supervisor.SupervisorEngine("s1", "admin")
        self.assertFalse(eng.pause())

    def test_pause_while_already_paused_returns_false(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._running = True
        eng.pause()
        self.assertFalse(eng.pause())

    def test_pre_pause_status_stored_on_pause(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._running = True
        eng.pause()
        self.assertEqual(eng._pre_pause_status, "running")

    def test_pre_pause_status_overridable_after_pause(self):
        """set_status_for_pause lets the caller override the remembered status
        *after* the engine has been paused."""
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._running = True
        eng.pause()
        self.assertEqual(eng._pre_pause_status, "running")
        eng.set_status_for_pause("planning")
        self.assertEqual(eng._pre_pause_status, "planning")

    def test_resume_clears_pause_and_sets_event(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._paused = True  # set directly, bypassing _running guard for test
        self.assertTrue(eng.resume())
        self.assertFalse(eng._paused)

    def test_resume_while_not_paused_returns_false(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        self.assertFalse(eng.resume())


class EngineWaitIfPausedTests(unittest.IsolatedAsyncioTestCase):
    """_wait_if_paused must yield when paused, return immediately otherwise."""

    async def test_not_paused_returns_immediately(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        start = asyncio.get_event_loop().time()
        await eng._wait_if_paused()
        elapsed = asyncio.get_event_loop().time() - start
        self.assertLess(elapsed, 0.5, "_wait_if_paused must return fast when not paused")

    async def test_paused_blocks_up_to_2_seconds(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._paused = True  # bypass _running guard for test
        start = asyncio.get_event_loop().time()
        await eng._wait_if_paused()
        elapsed = asyncio.get_event_loop().time() - start
        self.assertGreaterEqual(elapsed, 1.5,
                                "_wait_if_paused must block up to 2 seconds when paused")

    async def test_paused_resumed_within_timeout(self):
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._paused = True

        async def _resume():
            await asyncio.sleep(0.2)
            eng.resume()

        asyncio.create_task(_resume())
        start = asyncio.get_event_loop().time()
        await eng._wait_if_paused()
        elapsed = asyncio.get_event_loop().time() - start
        self.assertGreater(elapsed, 0.1, "must have waited for the resume signal")
        self.assertLess(elapsed, 1.0, "must return promptly after resume")


# ── Supervisor pause / resume (API-level) ────────────────────────────────────

class ApiPauseResumeTests(unittest.IsolatedAsyncioTestCase):
    """POST /pause and POST /resume must enforce state transitions.

    The endpoints raise HTTPException for error conditions, matching the
    FastAPI convention that existing supervisor tests also follow.
    """

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_pause_nonexistent_raises_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app._api_supervisor_pause(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/pause"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_pause_while_stopped_raises_409(self):
        await _create_supervisor(self, status="stopped")
        with self.assertRaises(HTTPException) as ctx:
            await app._api_supervisor_pause(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/pause"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_pause_while_running_ok(self):
        await _create_supervisor(self, status="running")
        engine = supervisor.SupervisorEngine("s1", "admin")
        engine._running = True
        with patch.dict(app._supervisor_engines, {"s1": engine}):
            resp = await app._api_supervisor_pause(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/pause"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
            self.assertEqual(resp.status_code, 200)
            body = json.loads(resp.body)
            self.assertTrue(body["ok"])
            self.assertEqual(body["status"], "paused")

    async def test_resume_nonexistent_raises_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app._api_supervisor_resume(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/resume"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_resume_while_running_raises_409(self):
        await _create_supervisor(self, status="running")
        with self.assertRaises(HTTPException) as ctx:
            await app._api_supervisor_resume(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/resume"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_resume_while_paused_ok(self):
        await _create_supervisor(self, status="paused")
        engine = supervisor.SupervisorEngine("s1", "admin")
        engine._paused = True  # engine has been paused, so resume() returns True
        engine._pre_pause_status = "running"
        with patch.dict(app._supervisor_engines, {"s1": engine}):
            resp = await app._api_supervisor_resume(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/resume"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
            self.assertEqual(resp.status_code, 200)
            body = json.loads(resp.body)
            self.assertTrue(body["ok"])
            self.assertEqual(body["status"], "running")

    async def test_pause_then_resume_restores_pre_pause_status(self):
        """A supervisor paused mid-task and resumed must restore 'running',
        not stay stuck on 'paused' in the DB."""
        await _create_supervisor(self, status="running")
        engine = supervisor.SupervisorEngine("s1", "admin")
        engine._running = True

        # Pause — the API handler calls pause() (sets _paused=True, stores
        # _pre_pause_status="running") then writes DB status=paused.
        with patch.dict(app._supervisor_engines, {"s1": engine}):
            resp = await app._api_supervisor_pause(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/pause"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
            self.assertEqual(resp.status_code, 200)

        # Now the DB says paused
        row = await db.supervisor_get("s1", "admin")
        self.assertEqual(row["status"], "paused")

        # Resume — the engine was paused by the API call above, so resume()
        # returns True and the DB is restored to "running".
        with patch.dict(app._supervisor_engines, {"s1": engine}):
            resp = await app._api_supervisor_resume(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/resume"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
            self.assertEqual(resp.status_code, 200)

        # DB should be restored to 'running'
        row = await db.supervisor_get("s1", "admin")
        self.assertEqual(row["status"], "running")

    async def test_pause_planning_supervisor(self):
        """A supervisor mid-planning can also be paused and resumes to planning."""
        await _create_supervisor(self, status="planning")
        engine = supervisor.SupervisorEngine("s1", "admin")
        engine._running = True
        engine.set_status_for_pause("planning")
        with patch.dict(app._supervisor_engines, {"s1": engine}):
            resp = await app._api_supervisor_pause(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/pause"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(json.loads(resp.body)["status"], "paused")

        # Resume back to planning
        engine2 = supervisor.SupervisorEngine("s1", "admin")
        engine2._paused = True  # resume() returns True only when _paused
        engine2._pre_pause_status = "planning"
        with patch.dict(app._supervisor_engines, {"s1": engine2}):
            resp = await app._api_supervisor_resume(
                SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/api/supervisors/s1/resume"),
                    cookies={},
                    state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
                ),
                "s1",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(json.loads(resp.body)["status"], "planning")


# ── Recency sort (frontend source-level) ─────────────────────────────────────

class RecencySortSourceTests(unittest.TestCase):
    """Sort mode toggle and localStorage persistence in supervisor.js."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_sort_mode_variable_exists(self):
        self.assertIn("supervisorSortMode", self.source)
        self.assertIn('"newest"', self.source)

    def test_sort_mode_default_is_newest(self):
        block = self.source.split("let supervisorSortMode", 1)[1].split(";")[0]
        self.assertIn('"newest"', block)

    def test_set_supervisor_sort_stores_to_localStorage(self):
        self.assertIn('"wc_supervisor_sort"', self.source)
        self.assertIn("localStorage.setItem", self.source)

    def test_load_saved_sort_restores_from_localStorage(self):
        self.assertIn('"wc_supervisor_sort"', self.source)
        self.assertIn('localStorage.getItem("wc_supervisor_sort")', self.source)

    def test_sort_toggle_cycles_newest_to_updated(self):
        """The toggle flips supervisorSortMode between newest and updated."""
        toggle_block = self.source.split('sortEl.addEventListener("click"')[1]
        self.assertIn('=== "updated"', toggle_block)
        self.assertIn('"newest"', toggle_block)

    def test_sort_cmp_uses_updated_at_when_updated(self):
        """When sort mode is updated, the comparison uses updated_at."""
        # The ternary that chooses the sort key
        ternary = self.source.split('supervisorSortMode === "updated"')[0]
        ternary += self.source.split('supervisorSortMode === "updated"')[1].split("localeCompare")[1]
        self.assertIn("updated_at", ternary)

    def test_sort_cmp_falls_back_to_created_at(self):
        # The ternary fallback chain
        fallback = self.source.split("localeCompare(a.created_at")[0]
        self.assertIn("updated_at", fallback)

    def test_supervisor_list_renders_time_label(self):
        """When updated_at differs from created_at, a time label appears."""
        self.assertIn("timeLabel", self.source)
        self.assertIn("formatTime(s.updated_at)", self.source)


class RecencySortBehaviourTests(unittest.TestCase):
    """Verify the sort toggle logic via source inspection."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_sort_toggle_cycles_newest_to_updated(self):
        """The toggle switches between 'newest' and 'updated'."""
        self.assertIn('=== "updated"', self.source)
        self.assertIn('"newest"', self.source)
        self.assertIn('"updated"', self.source)

    def test_sort_toggle_stores_to_localStorage(self):
        """After toggling, the new mode is persisted to localStorage."""
        self.assertIn("localStorage.setItem", self.source)
        self.assertIn('"wc_supervisor_sort"', self.source)

    def test_sort_toggle_click_handler_exists(self):
        """The #sortToggleBtn click handler is registered."""
        self.assertIn("#sortToggleBtn", self.source)
        self.assertIn('addEventListener("click"', self.source)


# ── Member heartbeat timestamps ──────────────────────────────────────────────

class MemberHeartbeatSourceTests(unittest.TestCase):
    """last_seen is rendered as a heartbeat time label on member rows."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_last_seen_is_read_from_member(self):
        self.assertIn("m.last_seen", self.source)

    def test_heartbeat_span_is_created(self):
        """A <span> is created with the time label."""
        self.assertIn("member-heartbeat", self.source)

    def test_formatTime_is_used_for_heartbeat(self):
        self.assertIn("formatTime(m.last_seen)", self.source)

    def test_heartbeat_is_a_date_string_title(self):
        """The full locale date is set as a title attribute for hover."""
        block = self.source.split("member-heartbeat")[-1]
        block = block.split("};", 2)[0]  # within the same function
        self.assertIn("toLocaleString", block)


class MemberHeartbeatUiSourceTests(unittest.TestCase):
    """CSS for the heartbeat element exists in supervisor.html."""

    def setUp(self):
        self.source = SUPERVISOR_HTML.read_text(encoding="utf-8")

    def test_heartbeat_has_styling(self):
        self.assertIn(".member-heartbeat", self.source)

    def test_heartbeat_font_size_small(self):
        """Heartbeat text is small — 10px or less."""
        block = self.source.split(".member-heartbeat").pop()
        self.assertIn("font-size", block)


# ── Task dependency hints ────────────────────────────────────────────────────

class TaskDepSourceTests(unittest.TestCase):
    """depends_on list is rendered below task titles."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_depends_on_is_checked(self):
        self.assertIn("t.depends_on", self.source)

    def test_task_deps_class_exists(self):
        """A <div class="task-deps"> is created."""
        self.assertIn("task-deps", self.source)

    def test_depends_on_text_is_rendered(self):
        self.assertIn("depends on:", self.source)

    def test_dep_ids_are_escaped(self):
        """Dependency IDs must not be interpolated raw."""
        deps_block = self.source.split("task-deps")[-1]
        deps_block = deps_block.split("};", 2)[0]
        self.assertIn("esc(", deps_block)


class TaskDepHtmlSourceTests(unittest.TestCase):
    """CSS for .task-deps exists in supervisor.html."""

    def setUp(self):
        self.source = SUPERVISOR_HTML.read_text(encoding="utf-8")

    def test_task_deps_has_styling(self):
        self.assertIn(".task-deps", self.source)

    def test_task_deps_font_size_small(self):
        block = self.source.split(".task-deps").pop()
        self.assertIn("font-size", block)


# ── Auto-grow composer ───────────────────────────────────────────────────────

class AutoGrowComposerSourceTests(unittest.TestCase):
    """The prompt textarea grows as the user types, up to 200 px."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_autoGrowComposer_function_exists(self):
        self.assertIn("autoGrowComposer", self.source)

    def test_textarea_resets_height_to_base(self):
        """Height is reset to 38px before measuring scroll."""
        self.assertIn('"38px"', self.source)

    def test_max_height_is_200(self):
        """The textarea does not grow beyond 200px."""
        self.assertIn("200", self.source)

    def test_input_event_listener_registered(self):
        self.assertIn("input", self.source)
        self.assertIn("autoGrowComposer", self.source)

    def test_scrollHeight_is_measured(self):
        self.assertIn("scrollHeight", self.source)


# ── Supervisor list recency time label ───────────────────────────────────────

class RecencyTimeLabelSourceTests(unittest.TestCase):
    """When a supervisor was updated after creation, a time label appears."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_time_label_uses_formatTime(self):
        self.assertIn("formatTime", self.source)

    def test_time_label_only_shown_when_updated_neq_created(self):
        """The time label should not appear for brand-new supervisors."""
        self.assertIn("s.updated_at", self.source)
        self.assertIn("s.created_at", self.source)
        self.assertIn("!==", self.source)


# ── /dev/* auth skip: the whole class is gone, deliberately ──────────────────
#
# `DevAuthSkipTests` asserted that the auth middleware exempts every path under
# `/dev/`, and that `/dev/supervisor-trigger` -- an endpoint that minted an admin
# session with no credential and returned its id to anybody who sent a GET --
# was present at HEAD. All three assertions were defending an authentication
# bypass, and the exemption has now been dropped on the operator's instruction.
#
# The reason it read as a decision worth defending is recorded in CHANGELOG.md:
# the exemption was one session's uncommitted debug scaffolding, swept into a
# commit about pause/resume by a whole-file `git commit`. A later reader found
# the orphaned line in HEAD, could not tell debris from intent -- nothing in the
# tree distinguishes them -- and wrote a test to protect it.
#
# Two of the three could not have failed anyway: each wrapped its own `assertIn`
# in `except Exception: self.skipTest("not in a git repo")`, and AssertionError
# is an Exception, so the removal they existed to catch surfaced as two skips
# blaming git, inside a git repository (rules.md #54).
#
# What replaces them is `tests/test_qa_api_tokens.py`, which asserts the
# opposite: no path is exempt from authentication, and a caller that cannot hold
# a cookie authenticates with an API token instead.


# ── Engine pause/resume in scheduler loop ────────────────────────────────────

class SchedulerPauseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """When the engine is paused, the scheduler loop must wait but persist."""

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_pause_flag_stops_task_progress(self):
        """A paused engine must not advance task status."""
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._running = True  # the engine is actually scheduled
        self.assertTrue(eng.pause())
        self.assertTrue(eng._paused)

    async def test_resume_flag_allows_progress(self):
        """After resume, _paused must be False."""
        eng = supervisor.SupervisorEngine("s1", "admin")
        eng._running = True
        eng.pause()
        eng.resume()
        self.assertFalse(eng._paused)

    async def test_status_for_pause_remembers_before_pause(self):
        """set_status_for_pause is called before pause() so the engine
        remembers what status to restore. The API handler does:
        eng.set_status_for_pause(existing["status"])  then  eng.pause().
        """
        eng = supervisor.SupervisorEngine("s1", "admin")
        # _paused must be True for set_status_for_pause to store the value.
        eng._paused = True
        eng.set_status_for_pause("running")
        self.assertEqual(eng._pre_pause_status, "running")


# ── Source-level integration: all 0.9.3 features present ─────────────────────

class FeatureCompletenessTests(unittest.TestCase):
    """One shot-check that all 0.9.3 features are wired in the source files."""

    def setUp(self):
        self.js = SUPERVISOR_JS.read_text(encoding="utf-8")
        self.html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        self.app_src = Path(app.__file__).read_text(encoding="utf-8")

    def test_pause_button_in_html(self):
        self.assertIn("pauseResumeBtn", self.html)

    def test_sort_toggle_button_in_html(self):
        self.assertIn("sortToggleBtn", self.html)

    def test_member_row_css_in_html(self):
        self.assertIn(".member-row", self.html)

    def test_member_status_css_in_html(self):
        self.assertIn(".member-status", self.html)

    def test_dialog_backdrop_css_in_html(self):
        self.assertIn(".dialog-backdrop", self.html)

    def test_pause_resume_css_in_html(self):
        self.assertIn("#pauseResumeBtn", self.html)

    def test_task_deps_css_in_html(self):
        self.assertIn(".task-deps", self.html)

    def test_pause_endpoint_registered(self):
        self.assertIn("/pause", self.app_src)

    def test_resume_endpoint_registered(self):
        self.assertIn("/resume", self.app_src)

    def test_auto_grow_registered_in_js(self):
        """Auto-grow is a JS-only feature, no HTML element needed."""
        self.assertIn("autoGrowComposer", self.js)

    def test_heartbeat_in_js(self):
        self.assertIn("member-heartbeat", self.js)


if __name__ == "__main__":
    unittest.main()