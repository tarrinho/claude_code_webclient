"""QA coverage for db.read_claude_sessions -- the CLI session listing.

A name-level audit found read_claude_sessions itself untested end to end. Its
helpers are well covered by tests/test_cli_sessions.py (cweb2), but nothing
exercised the function that reads the directory: the glob, what it does with an
unreadable or malformed session file, the interactive/name filters, the
same-PID rule that has to keep WebConsole's own sync record, or the assembled
row shape the sidebar renders.

Every filter here fails silently. A session wrongly dropped just does not
appear in the sidebar, and a session wrongly kept looks like a real one.

Scope note: _dedupe_sessions, _session_rank, _session_is_live, _pid_is_running
and delete_claude_session_file are covered by tests/test_cli_sessions.py, and
model-extraction semantics by tests/test_model_settings.py::ModelExtractionTests.
This file covers read_claude_sessions and the _model_from_lines guards those
tests do not reach.

Covers:
* Directory handling — missing, unreadable, empty.
* Per-file resilience — malformed JSON and unreadable files are skipped.
* Filters — kind must be interactive; name or sessionId required.
* The same-PID rule — the live CLI is skipped, the webconsole record is kept.
* Row shape — id fallback, Untitled name, timestamps, live flag, file name.
* Model — taken from the record, else looked up from the transcript.
* _model_from_lines — non-dict message, wrong role, blank and malformed lines.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from tests.testing_model import TESTING_MODEL


def _session_file(root: Path, filename: str, **fields) -> Path:
    payload = {
        "sessionId": fields.pop("sessionId", "sess-1"),
        "name": fields.pop("name", "A session"),
        "kind": fields.pop("kind", "interactive"),
        "cwd": fields.pop("cwd", "/home/kali/projects"),
        "pid": fields.pop("pid", 999999),
        "startedAt": fields.pop("startedAt", 1_700_000_000_000),
        "updatedAt": fields.pop("updatedAt", 1_700_000_100_000),
    }
    payload.update(fields)
    path = root / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ReadClaudeSessionsTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._dir_patch = patch.object(db, "_CLAUDE_SESSIONS_DIR", self.root)
        self._dir_patch.start()
        # A dead pid by default, so `live` is deterministic.
        self._live_patch = patch.object(db, "_pid_is_running", return_value=False)
        self._live_patch.start()
        # Keep transcript lookups off the real ~/.claude tree.
        self._model_patch = patch.object(db, "_lookup_session_model", return_value=None)
        self._model_patch.start()

    def tearDown(self):
        self._model_patch.stop()
        self._live_patch.stop()
        self._dir_patch.stop()
        self.tmp.cleanup()

    # ── directory handling ──────────────────────────────────────────────

    async def test_missing_directory_is_empty(self):
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", Path("/nonexistent-sessions-xyz")):
            self.assertEqual(await db.read_claude_sessions(), [])

    async def test_empty_directory_is_empty(self):
        self.assertEqual(await db.read_claude_sessions(), [])

    async def test_unreadable_directory_is_empty_not_an_error(self):
        with patch.object(
            type(self.root), "is_dir", side_effect=PermissionError("denied")
        ):
            self.assertEqual(await db.read_claude_sessions(), [])

    async def test_only_json_files_are_read(self):
        _session_file(self.root, "real.json")
        (self.root / "notes.txt").write_text("ignore me")
        (self.root / "other.jsonl").write_text("{}")
        sessions = await db.read_claude_sessions()
        self.assertEqual(len(sessions), 1)

    # ── per-file resilience ─────────────────────────────────────────────

    async def test_malformed_json_is_skipped_and_the_rest_survive(self):
        """One corrupt file must not blank the whole sidebar."""
        (self.root / "broken.json").write_text("{not json")
        _session_file(self.root, "good.json", sessionId="sess-good")
        sessions = await db.read_claude_sessions()
        self.assertEqual([s["sessionId"] for s in sessions], ["sess-good"])

    async def test_unreadable_file_is_skipped(self):
        _session_file(self.root, "good.json", sessionId="sess-good")
        _session_file(self.root, "locked.json", sessionId="sess-locked")

        real_read = Path.read_text

        def _selective(self_path, *args, **kwargs):
            if self_path.name == "locked.json":
                raise OSError("permission denied")
            return real_read(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", _selective):
            sessions = await db.read_claude_sessions()
        self.assertEqual([s["sessionId"] for s in sessions], ["sess-good"])

    async def test_json_that_is_not_an_object_is_skipped(self):
        (self.root / "list.json").write_text("[1, 2, 3]")
        _session_file(self.root, "good.json", sessionId="sess-good")
        sessions = await db.read_claude_sessions()
        self.assertEqual([s["sessionId"] for s in sessions], ["sess-good"])

    # ── filters ─────────────────────────────────────────────────────────

    async def test_non_interactive_kind_is_excluded(self):
        _session_file(self.root, "batch.json", kind="print")
        self.assertEqual(await db.read_claude_sessions(), [])

    async def test_missing_kind_is_excluded(self):
        _session_file(self.root, "nokind.json", kind="")
        self.assertEqual(await db.read_claude_sessions(), [])

    async def test_record_without_name_or_session_id_is_excluded(self):
        _session_file(self.root, "anon.json", name="", sessionId="")
        self.assertEqual(await db.read_claude_sessions(), [])

    async def test_session_id_alone_is_enough(self):
        _session_file(self.root, "idonly.json", name="", sessionId="sess-x")
        sessions = await db.read_claude_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], "Untitled")

    async def test_name_alone_is_enough_and_id_falls_back_to_the_filename(self):
        _session_file(self.root, "named.json", name="Just a name", sessionId="")
        sessions = await db.read_claude_sessions()
        self.assertEqual(sessions[0]["id"], "named")

    # ── the same-PID rule ───────────────────────────────────────────────

    async def test_the_running_process_own_session_is_skipped(self):
        _session_file(self.root, "self.json", pid=os.getpid())
        self.assertEqual(await db.read_claude_sessions(), [])

    async def test_webconsole_record_survives_the_pid_filter(self):
        """WebConsole writes its sync record with its own PID; filtering by PID
        alone would hide the CLI-to-web link it exists to create."""
        _session_file(
            self.root, "sync.json", pid=os.getpid(), entrypoint="webconsole",
            sessionId="sess-sync",
        )
        sessions = await db.read_claude_sessions()
        self.assertEqual([s["sessionId"] for s in sessions], ["sess-sync"])
        self.assertEqual(sessions[0]["entrypoint"], "webconsole")

    async def test_non_numeric_pid_is_not_treated_as_this_process(self):
        _session_file(self.root, "weird.json", pid="not-a-pid", sessionId="sess-w")
        sessions = await db.read_claude_sessions()
        self.assertEqual(len(sessions), 1)

    async def test_missing_pid_is_not_treated_as_this_process(self):
        _session_file(self.root, "nopid.json", pid=None, sessionId="sess-n")
        sessions = await db.read_claude_sessions()
        self.assertEqual(len(sessions), 1)

    # ── row shape ───────────────────────────────────────────────────────

    async def test_row_shape(self):
        _session_file(
            self.root, "one.json", sessionId="sess-1", name="Build it",
            cwd="/home/kali/projects/thing",
        )
        row = (await db.read_claude_sessions())[0]
        self.assertEqual(row["id"], "sess-1")
        self.assertEqual(row["name"], "Build it")
        self.assertEqual(row["cwd"], "/home/kali/projects/thing")
        self.assertEqual(row["kind"], "interactive")
        self.assertEqual(row["file"], "one.json")
        self.assertFalse(row["live"])

    async def test_timestamps_are_formatted(self):
        _session_file(self.root, "ts.json")
        row = (await db.read_claude_sessions())[0]
        self.assertTrue(row["startedAt"].endswith("Z"), row["startedAt"])
        self.assertTrue(row["updatedAt"].endswith("Z"), row["updatedAt"])

    async def test_unparseable_timestamps_become_empty_strings(self):
        _session_file(self.root, "badts.json", startedAt="nope", updatedAt=None)
        row = (await db.read_claude_sessions())[0]
        self.assertEqual(row["startedAt"], "")
        self.assertEqual(row["updatedAt"], "")

    async def test_live_flag_follows_the_pid_probe(self):
        _session_file(self.root, "live.json")
        self._live_patch.stop()
        try:
            with patch.object(db, "_pid_is_running", return_value=True):
                row = (await db.read_claude_sessions())[0]
            self.assertTrue(row["live"])
        finally:
            self._live_patch.start()

    # ── model resolution ────────────────────────────────────────────────

    async def test_model_taken_from_the_record_without_a_transcript_read(self):
        _session_file(self.root, "m.json", model=TESTING_MODEL)
        self._model_patch.stop()
        try:
            with patch.object(db, "_lookup_session_model") as lookup:
                row = (await db.read_claude_sessions())[0]
            lookup.assert_not_called()
        finally:
            self._model_patch.start()
        self.assertEqual(row["model"], TESTING_MODEL)

    async def test_model_looked_up_from_the_transcript_when_absent(self):
        _session_file(self.root, "m.json", sessionId="sess-lookup")
        self._model_patch.stop()
        try:
            with patch.object(
                db, "_lookup_session_model", return_value=TESTING_MODEL
            ):
                row = (await db.read_claude_sessions())[0]
        finally:
            self._model_patch.start()
        self.assertEqual(row["model"], TESTING_MODEL)

    async def test_model_is_empty_when_nothing_knows_it(self):
        _session_file(self.root, "m.json")
        self.assertEqual((await db.read_claude_sessions())[0]["model"], "")

    # ── dedupe is applied ───────────────────────────────────────────────

    async def test_duplicate_records_are_collapsed(self):
        """The listing must go through _dedupe_sessions, or every resumed
        session appears twice -- once for the CLI file and once for the
        webconsole shadow."""
        _session_file(self.root, "cli.json", sessionId="sess-dup", name="Real name")
        _session_file(
            self.root, "sess-dup.json", sessionId="sess-dup", name="",
            entrypoint="webconsole",
        )
        sessions = await db.read_claude_sessions()
        self.assertEqual(len(sessions), 1)


class ModelFromLinesGuardTests(unittest.TestCase):
    """Guards in _model_from_lines that the transcript-level tests don't reach.

    tests/test_model_settings.py::ModelExtractionTests covers the semantics --
    synthetic ignored, last model wins, wrong session ignored -- through
    _extract_model_from_transcript. These are the malformed-record paths.
    """

    def test_blank_lines_ignored(self):
        line = json.dumps({
            "type": "assistant", "sessionId": "s1",
            "message": {"role": "assistant", "model": TESTING_MODEL},
        })
        self.assertEqual(
            db._model_from_lines(["", "   ", line], "s1"), TESTING_MODEL
        )

    def test_malformed_json_ignored(self):
        line = json.dumps({
            "type": "assistant", "sessionId": "s1",
            "message": {"role": "assistant", "model": TESTING_MODEL},
        })
        self.assertEqual(
            db._model_from_lines([line, "{not json"], "s1"), TESTING_MODEL
        )

    def test_non_dict_message_ignored(self):
        line = json.dumps({"type": "assistant", "sessionId": "s1", "message": "text"})
        self.assertIsNone(db._model_from_lines([line], "s1"))

    def test_message_missing_entirely_ignored(self):
        line = json.dumps({"type": "assistant", "sessionId": "s1"})
        self.assertIsNone(db._model_from_lines([line], "s1"))

    def test_user_role_inside_an_assistant_record_ignored(self):
        line = json.dumps({
            "type": "assistant", "sessionId": "s1",
            "message": {"role": "user", "model": TESTING_MODEL},
        })
        self.assertIsNone(db._model_from_lines([line], "s1"))

    def test_empty_model_string_ignored(self):
        line = json.dumps({
            "type": "assistant", "sessionId": "s1",
            "message": {"role": "assistant", "model": ""},
        })
        self.assertIsNone(db._model_from_lines([line], "s1"))

    def test_empty_input_returns_none(self):
        self.assertIsNone(db._model_from_lines([], "s1"))


if __name__ == "__main__":
    unittest.main()
