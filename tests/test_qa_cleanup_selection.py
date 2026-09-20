"""QA: per-process selection for the Server tab's process cleanup.

This endpoint terminates other people's running agents, so the tests here are
weighted towards what must NOT happen rather than towards the happy path.

Two shipped defects are pinned below, both found on 2026-09-20:

* ``_collect_all_processes`` annotated a row with its Claude session name one
  statement before the row was created, so ``raw[pid]["session_name"] = ...``
  raised ``KeyError`` on any host where a session file named a live claude
  PID. ``/api/system/cleanup/preview`` answered 500 and the panel reported
  "Could not scan processes". Reproduced against the deployed release as
  ``KeyError 1820``.

* ``execute`` tested ``if pid_filter:``, so an empty list took the *unfiltered*
  branch and killed everything the preview returned. The route reaches that
  case whenever every submitted PID has gone stale -- which is precisely the
  stale-page scenario the PID validation was added to defend against. A page
  left open while its PIDs recycled would have terminated every reclaimable
  process on the host, including peer sessions' live agents. The distinction
  the code now relies on is ``pid_filter is not None``, and
  ``test_an_empty_filter_kills_nothing`` is what holds it in place.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
from routes import sys_cleanup


HTTPS = "https://testserver"


def _proc(pid, kind, rss_mb=100.0, pgid=None, name="claude", age_s=9999):
    """One row shaped like ``_collect_all_processes`` produces."""
    return {
        "pid": pid, "name": name, "kind": kind,
        "rss_mb": rss_mb, "age_s": age_s,
        "cmdline": f"claude --resume {pid}", "pgid": pgid if pgid is not None else pid,
    }


def _preview_of(*rows):
    """A ``preview()`` payload holding *rows*, grouped the way preview does."""
    out = {"zombie": [], "claude": [], "chrome": [], "python": []}
    for row in rows:
        out[row["kind"]].append(row)
    out["counts"] = {k: len(out[k]) for k in ("zombie", "claude", "chrome", "python")}
    out["total_estimated_mb"] = round(sum(r["rss_mb"] for r in rows), 1)
    return out


class KillRecorder:
    """Stands in for the two kill paths, recording instead of signalling."""

    def __init__(self):
        self.single = []
        self.groups = []

    def kill_single(self, pid):
        self.single.append(pid)
        return True, ""

    def kill_group(self, pgid):
        self.groups.append(pgid)
        return True, ""

    @property
    def everything(self):
        return sorted(self.single + self.groups)


class ExecuteFilterTests(unittest.TestCase):
    """``execute``'s whitelist, which is the guard on an irreversible action."""

    def setUp(self):
        self.recorder = KillRecorder()
        for target, replacement in (
            ("_kill_single", self.recorder.kill_single),
            ("_kill_group", self.recorder.kill_group),
            ("_clear_swap", lambda: 0.0),
        ):
            patcher = patch.object(sys_cleanup, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.rows = [
            _proc(1001, "claude"),
            _proc(1002, "claude"),
            _proc(2001, "chrome", name="chrome"),
            _proc(3001, "zombie", name="defunct", rss_mb=0.0),
        ]
        patcher = patch.object(
            sys_cleanup, "preview", lambda: _preview_of(*self.rows)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_empty_filter_kills_nothing(self):
        """The defect this file exists for: [] must not mean "no filter".

        Under `if pid_filter:` this killed all four rows.
        """
        result = sys_cleanup.execute(pid_filter=[])
        self.assertEqual(self.recorder.everything, [],
                         "an empty selection must terminate nothing at all")
        self.assertEqual(result["killed"], [])
        self.assertEqual(result["freed_mb"], 0.0)

    def test_only_the_listed_pid_is_killed(self):
        sys_cleanup.execute(pid_filter=[2001])
        self.assertEqual(self.recorder.single, [2001])
        self.assertEqual(self.recorder.groups, [])

    def test_unlisted_claude_sessions_survive(self):
        """Selecting one agent must not take its neighbour with it."""
        sys_cleanup.execute(pid_filter=[1001])
        self.assertEqual(self.recorder.groups, [1001])
        self.assertNotIn(1002, self.recorder.everything)

    def test_a_pid_not_in_the_preview_is_ignored(self):
        sys_cleanup.execute(pid_filter=[1001, 424242])
        self.assertEqual(self.recorder.everything, [1001])

    def test_none_still_kills_everything(self):
        """The documented back-compatible default, kept distinct from []."""
        sys_cleanup.execute(pid_filter=None)
        self.assertEqual(self.recorder.everything, [1001, 1002, 2001, 3001])

    def test_freed_mb_counts_only_what_was_selected(self):
        result = sys_cleanup.execute(pid_filter=[2001])
        self.assertEqual(result["freed_mb"], 100.0)


class SessionNameAnnotationTests(unittest.TestCase):
    """The KeyError regression, and the annotation it was carrying."""

    def _collect(self, session_names, kinds):
        """Run ``_collect_all_processes`` over a fabricated /proc.

        Everything that touches the real host is replaced: the walk up the
        parent chain reads ``stat``, so ``_proc_read`` returning "" makes it
        stop at the first step rather than protecting real PGIDs.
        """
        pids = sorted(kinds)
        patches = {
            "_own_pid": lambda: 999999,
            "_own_uid": lambda: 1000,
            "_proc_read": lambda pid, filename: "",
            "_proc_uids": lambda pid: 1000,
            "_proc_status_name": lambda pid: "claude",
            "_proc_cmdline": lambda pid: f"claude --resume {pid}",
            "_detect_kind": lambda pid, name, cmdline: kinds[pid],
            "_proc_rss": lambda pid: 1024,
            "_proc_create_time": lambda pid: 0,
            "_collect_session_names": lambda: session_names,
        }
        for target, replacement in patches.items():
            patcher = patch.object(sys_cleanup, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        listdir = patch.object(
            sys_cleanup.os, "listdir", lambda path: [str(p) for p in pids]
        )
        # Each PID is its own process group, so none collides with the
        # caller's and nothing is skipped by the self-protection check.
        getpgid = patch.object(sys_cleanup.os, "getpgid", lambda pid: pid)
        listdir.start()
        getpgid.start()
        self.addCleanup(listdir.stop)
        self.addCleanup(getpgid.stop)
        return sys_cleanup._collect_all_processes()

    def test_a_named_session_does_not_raise(self):
        """The shipped crash: annotating before the row existed."""
        rows = self._collect({1001: "cweb6"}, {1001: "claude"})
        self.assertIn(1001, rows)

    def test_the_session_name_reaches_the_row(self):
        rows = self._collect({1001: "cweb6"}, {1001: "claude"})
        self.assertEqual(rows[1001]["session_name"], "cweb6")

    def test_an_unnamed_claude_process_has_no_session_name(self):
        rows = self._collect({}, {1001: "claude"})
        self.assertNotIn("session_name", rows[1001])

    def test_only_claude_rows_are_annotated(self):
        """A chrome PID that happens to match a session file is not a session."""
        rows = self._collect({2001: "cweb6"}, {2001: "chrome"})
        self.assertNotIn("session_name", rows[2001])

    def test_a_session_file_for_a_dead_pid_is_harmless(self):
        rows = self._collect({424242: "gone"}, {1001: "claude"})
        self.assertNotIn("session_name", rows[1001])


class SessionNameParsingTests(unittest.TestCase):
    """``_collect_session_names`` reads whatever is on disk, so it must not
    raise on anything it finds there."""

    def _names_in(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sessions = tmp.name + "/.claude/sessions"
        import os as _os
        _os.makedirs(sessions)
        for filename, body in files.items():
            with open(f"{sessions}/{filename}", "w", encoding="utf-8") as fh:
                fh.write(body)
        with patch.object(sys_cleanup.Path, "home", staticmethod(
                lambda: sys_cleanup.Path(tmp.name))):
            return sys_cleanup._collect_session_names()

    def test_a_well_formed_file_is_read(self):
        names = self._names_in({"1.json": '{"pid": 42, "name": "cweb6"}'})
        self.assertEqual(names, {42: "cweb6"})

    def test_malformed_json_is_skipped_not_raised(self):
        names = self._names_in({
            "bad.json": "{not json",
            "good.json": '{"pid": 42, "name": "cweb6"}',
        })
        self.assertEqual(names, {42: "cweb6"})

    def test_a_missing_directory_yields_no_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(sys_cleanup.Path, "home", staticmethod(
                lambda: sys_cleanup.Path(tmp.name))):
            self.assertEqual(sys_cleanup._collect_session_names(), {})

    def test_an_empty_name_is_not_recorded(self):
        names = self._names_in({"1.json": '{"pid": 42, "name": ""}'})
        self.assertEqual(names, {})


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class CleanupRouteTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/system/cleanup/execute -- what the browser can ask for."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (
            ("DB_PATH", f"{self.tmp.name}/db"),
            ("PROJECTS_ROOT", f"{self.tmp.name}/p"),
        ):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))

        self.recorder = KillRecorder()
        for target, replacement in (
            ("_kill_single", self.recorder.kill_single),
            ("_kill_group", self.recorder.kill_group),
            ("_clear_swap", lambda: 0.0),
        ):
            patcher = patch.object(sys_cleanup, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.rows = [_proc(1001, "claude"), _proc(2001, "chrome", name="chrome")]
        patcher = patch.object(
            sys_cleanup, "preview", lambda: _preview_of(*self.rows)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def _execute(self, body):
        client, headers = self._login()
        return client.post(
            "/api/system/cleanup/execute", json=body, headers=headers
        )

    def test_a_selected_pid_is_killed(self):
        response = self._execute({"pids": [2001]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.recorder.everything, [2001])

    def test_an_unselected_pid_survives(self):
        self._execute({"pids": [2001]})
        self.assertNotIn(1001, self.recorder.everything)

    def test_an_all_stale_selection_is_refused(self):
        """The dangerous request, refused rather than widened.

        Every PID has recycled since the page was drawn. The validation
        filters all of them out, and what is left must not be executed.
        """
        response = self._execute({"pids": [424242, 424243]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.recorder.everything, [],
                         "a stale page must never terminate anything")

    def test_an_empty_selection_is_refused(self):
        response = self._execute({"pids": []})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.recorder.everything, [])

    def test_a_body_without_pids_is_refused(self):
        """An old client that posts nothing must not mean "kill everything"."""
        response = self._execute({})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.recorder.everything, [])

    def test_a_non_list_pids_value_is_refused(self):
        response = self._execute({"pids": "all"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.recorder.everything, [])

    def test_true_is_not_accepted_as_pid_one(self):
        """`True == 1` in Python, and PID 1 is init."""
        response = self._execute({"pids": [True]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.recorder.everything, [])

    def test_the_endpoint_requires_a_session(self):
        response = _client().post(
            "/api/system/cleanup/execute", json={"pids": [2001]}
        )
        self.assertIn(response.status_code, (401, 403), response.text)
        self.assertEqual(self.recorder.everything, [])


class CleanupPreviewRouteTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/system/cleanup/preview -- the call that was answering 500."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (
            ("DB_PATH", f"{self.tmp.name}/db"),
            ("PROJECTS_ROOT", f"{self.tmp.name}/p"),
        ):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    def test_preview_reports_a_named_session(self):
        rows = [_proc(1001, "claude")]
        rows[0]["session_name"] = "cweb6"
        with patch.object(sys_cleanup, "preview", lambda: _preview_of(*rows)):
            response = self._login().get("/api/system/cleanup/preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["claude"][0]["session_name"], "cweb6")

    def test_preview_kills_nothing(self):
        """It is the half of the two-step that must have no side effect."""
        recorder = KillRecorder()
        with patch.object(sys_cleanup, "_kill_single", recorder.kill_single), \
             patch.object(sys_cleanup, "_kill_group", recorder.kill_group), \
             patch.object(sys_cleanup, "preview",
                          lambda: _preview_of(_proc(1001, "claude"))):
            response = self._login().get("/api/system/cleanup/preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(recorder.everything, [])


class CleanupPanelSourceTests(unittest.TestCase):
    """Front-end invariants, read off the module the browser loads.

    Source-level assertions in the style of test_qa_server_refresh.py: the
    defects being pinned were all "a control exists but nothing is wired to
    it", which no server-side test can see.
    """

    from pathlib import Path as _Path
    SERVER_STATS = (
        _Path(__file__).resolve().parents[1] / "web" / "assets" / "server-stats.js"
    ).read_text()
    # Comments in this file quote the broken forms they replaced, so a search
    # over the raw text finds the prose describing a defect and reports it as
    # the defect. Assertions about what the browser RUNS read this instead.
    CODE = "\n".join(
        line.split("//")[0] if line.lstrip().startswith("//") else line
        for line in SERVER_STATS.splitlines()
    )

    def test_execute_sends_the_selected_pids(self):
        """A bare POST would mean "kill everything" to the old endpoint."""
        self.assertIn("'/api/system/cleanup/execute'", self.SERVER_STATS)
        self.assertIn("JSON.stringify({pids: selectedPids})", self.SERVER_STATS)

    def test_the_kill_button_is_gated_on_a_selection(self):
        self.assertIn("Select processes above", self.SERVER_STATS)
        self.assertIn("selected (~", self.SERVER_STATS)

    def test_every_process_row_carries_a_checkbox(self):
        self.assertIn("cleanup-pid-check", self.SERVER_STATS)
        self.assertIn("cleanup-cat-check", self.SERVER_STATS)

    def test_a_process_row_can_show_its_session_name(self):
        self.assertIn("p.session_name", self.SERVER_STATS)

    def test_no_handler_is_bound_through_a_global(self):
        """`window._cleanupRescan()` named an export that never reached
        `window`, so "Scan again" after a failed cleanup did nothing."""
        self.assertNotIn("window._cleanupRescan", self.CODE)
        self.assertNotIn('onclick="', self.CODE)

    def test_both_failure_paths_survive_the_poll(self):
        """The 30s poll resets any panel without the marker, so an error
        written without it erased itself within seconds."""
        self.assertEqual(
            self.SERVER_STATS.count("setAttribute('data-cleaned-up', 'true')"), 3,
            "expected the marker on the preview, scan-failure and "
            "cleanup-failure paths",
        )

    def test_the_poll_checks_the_marker_not_the_button(self):
        """It used to look for #cleanupScanBtn, which a rendered result has
        no reason to contain -- so every tick wiped the results."""
        self.assertIn("hasAttribute('data-cleaned-up')", self.SERVER_STATS)

    def test_the_server_reason_is_shown_when_execute_is_refused(self):
        self.assertIn(".detail", self.SERVER_STATS)


if __name__ == "__main__":
    unittest.main()
