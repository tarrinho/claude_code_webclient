"""QA: the rolling recording of the most recent conversation.

Two things are being asserted, and they fail in different ways:

  * **The behaviour.** One file, overwritten each time, holding the latest
    conversation -- and, critically, NOT a retention change: the `chats` and
    `messages` rows must all still be there afterwards. A recorder that
    quietly pruned history would satisfy every "the file holds the latest
    conversation" assertion.

  * **The handling.** The operator's brief was "this file will contain raw
    audio/text on disk, gitignore it and set restrictive file permissions by
    default". Permissions are asserted by reading them back off the
    filesystem, never by checking that the code asked for them -- `os.makedirs`
    and `open` both subtract the process umask, so a requested mode and an
    effective mode are different claims. Every permission test below runs
    under a deliberately permissive umask for exactly that reason: under the
    default 022 a naive implementation would look correct.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import conversation_recording as cr
import db
from routes.db_users import setting_set

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class _PermissiveUmask:
    """Run a block under umask 0, so a mode that is merely *requested* is not
    accidentally corrected into looking right by the ambient umask."""

    def __enter__(self):
        self._old = os.umask(0)
        return self

    def __exit__(self, *exc):
        os.umask(self._old)
        return False


CHAT = {"id": "c1", "title": "t", "model": "m", "voice_mode": 0,
        "owner_id": "u1", "created_at": 1, "updated_at": 2}
MESSAGES = [{"role": "user", "content": "hello", "created_at": 1},
            {"role": "assistant", "content": "hi", "created_at": 2}]


class RecordingShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_it_writes_the_conversation(self):
        path = cr.write_recording(CHAT, MESSAGES, self.root)
        data = json.loads(Path(path).read_text())
        self.assertEqual(data["chat"]["id"], "c1")
        self.assertEqual(data["message_count"], 2)
        self.assertEqual([m["content"] for m in data["messages"]],
                         ["hello", "hi"])

    def test_the_owner_is_recorded(self):
        """This console is multi-user, so "the most recent conversation" is
        whichever happened last, which is not necessarily the reader's own.
        The file must say whose it is rather than leave that to be assumed."""
        path = cr.write_recording(CHAT, MESSAGES, self.root)
        self.assertEqual(json.loads(Path(path).read_text())["chat"]["owner_id"],
                         "u1")

    def test_a_second_conversation_overwrites_the_first(self):
        """The whole point: one recording, not an accumulating set. A
        directory listing is asserted, not just the file's content -- an
        implementation that wrote `last-conversation.1.json` beside it would
        pass a content check and fail the requirement."""
        cr.write_recording(CHAT, MESSAGES, self.root)
        second = dict(CHAT, id="c2", title="second")
        cr.write_recording(second, [{"role": "user", "content": "later",
                                     "created_at": 9}], self.root)
        data = json.loads(cr.recording_path(self.root).read_text())
        self.assertEqual(data["chat"]["id"], "c2")
        self.assertEqual(data["message_count"], 1)
        self.assertEqual(sorted(p.name for p in cr.recording_dir(self.root).iterdir()),
                         [cr.RECORDING_FILENAME])

    def test_no_temp_file_is_left_behind(self):
        """The atomic write uses a temp file in the same directory. Leaving it
        would leave a second copy of the conversation on disk, which is the
        opposite of what the handling requirement asks for."""
        cr.write_recording(CHAT, MESSAGES, self.root)
        self.assertEqual([p.name for p in cr.recording_dir(self.root).iterdir()],
                         [cr.RECORDING_FILENAME])

    def test_a_stale_temp_file_does_not_block_a_write(self):
        """A crash between open and replace leaves the temp file behind. The
        next write must recover rather than fail forever on O_EXCL."""
        cr.ensure_recording_dir(self.root)
        stale = cr.recording_path(self.root).with_name(
            cr.RECORDING_FILENAME + ".tmp")
        stale.write_text("junk")
        cr.write_recording(CHAT, MESSAGES, self.root)
        self.assertFalse(stale.exists())
        self.assertEqual(
            json.loads(cr.recording_path(self.root).read_text())["chat"]["id"],
            "c1")


class PermissionTests(unittest.TestCase):
    """The handling requirement, read back off the filesystem."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_the_file_is_owner_only(self):
        with _PermissiveUmask():
            path = cr.write_recording(CHAT, MESSAGES, self.root)
        self.assertEqual(_mode(path), 0o600, oct(_mode(path)))

    def test_the_directory_is_owner_only(self):
        with _PermissiveUmask():
            directory = cr.ensure_recording_dir(self.root)
        self.assertEqual(_mode(directory), 0o700, oct(_mode(directory)))

    def test_nothing_is_readable_by_group_or_other(self):
        """Stated as the property rather than as a number, so a future change
        to a different-but-still-private mode does not read as a regression
        while a genuinely wider one does."""
        with _PermissiveUmask():
            path = cr.write_recording(CHAT, MESSAGES, self.root)
        for target in (path, cr.recording_dir(self.root)):
            with self.subTest(target=target):
                self.assertFalse(
                    _mode(target) & (stat.S_IRWXG | stat.S_IRWXO),
                    f"{target} is {oct(_mode(target))}")

    def test_an_existing_wide_directory_is_tightened(self):
        """The case that matters on a real host: a directory created earlier
        under a laxer umask, or by another tool, keeps its mode forever unless
        something tightens it -- and then the recording is protected by its
        file mode alone."""
        directory = cr.recording_dir(self.root)
        directory.mkdir(parents=True)
        os.chmod(directory, 0o777)
        cr.write_recording(CHAT, MESSAGES, self.root)
        self.assertEqual(_mode(directory), 0o700, oct(_mode(directory)))

    def test_an_existing_wide_file_is_replaced_by_a_private_one(self):
        """Overwriting must not inherit the old file's permissions. `os.replace`
        keeps the SOURCE's mode, which is what makes this hold -- an
        implementation that opened the destination in place would keep 0666."""
        with _PermissiveUmask():
            path = cr.write_recording(CHAT, MESSAGES, self.root)
            os.chmod(path, 0o666)
            cr.write_recording(dict(CHAT, id="c2"), MESSAGES, self.root)
        self.assertEqual(_mode(path), 0o600, oct(_mode(path)))

    def test_the_content_is_never_wider_than_the_file_at_any_point(self):
        """The reason the mode is set at `os.open` rather than by a later
        `chmod`: a chmod-after-write leaves a window in which the conversation
        exists at the umask's mode. Asserted by watching the temp file's mode
        at the moment it is created."""
        seen = {}
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **kw):
            fd = real_open(path, flags, mode, *a, **kw)
            if str(path).endswith(".tmp"):
                seen["mode"] = stat.S_IMODE(os.fstat(fd).st_mode)
            return fd

        with _PermissiveUmask(), patch.object(os, "open", spy):
            cr.write_recording(CHAT, MESSAGES, self.root)
        self.assertEqual(seen.get("mode"), 0o600, seen)


class DefaultLocationTests(unittest.TestCase):
    """Where the recording lives when nothing says otherwise."""

    def test_it_lives_beside_the_database_not_beside_the_code(self):
        """`bin/wc-deploy.sh` exports each commit to its own release directory
        and prunes old ones, so a recording written next to this module would
        be replaced by an empty directory on the next deploy and deleted when
        that release aged out. "Always keeps a recording of the most recent
        conversation" would silently mean "until the next deploy".

        Asserted against `config.DB_PATH`'s directory rather than a literal
        path, because that is what the systemd unit aims with `WC_DB_PATH`."""
        self.assertEqual(cr.default_root(),
                         Path(config.DB_PATH).resolve().parent)
        self.assertNotEqual(cr.default_root(),
                            Path(cr.__file__).resolve().parent)

    def test_an_explicit_root_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cr.recording_dir(tmp),
                             Path(tmp) / cr.RECORDING_DIRNAME)


class GitignoreTests(unittest.TestCase):
    def test_the_recordings_directory_is_ignored(self):
        """Asserted through git itself rather than by grepping .gitignore --
        an entry can be present and still not match, and what matters is
        whether git would track the file."""
        result = subprocess.run(
            ["git", "check-ignore", "-q",
             f"{cr.RECORDING_DIRNAME}/{cr.RECORDING_FILENAME}"],
            cwd=REPO_ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0,
                         "the recording is not gitignored")

    def test_a_stray_file_in_the_directory_is_ignored_too(self):
        """The entry covers the directory, not one filename -- so a second
        recording, a temp file, or an audio blob added later is private by
        default rather than by remembering to add another rule."""
        result = subprocess.run(
            ["git", "check-ignore", "-q",
             f"{cr.RECORDING_DIRNAME}/anything-else.wav"],
            cwd=REPO_ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0)


class NotARetentionChangeTests(unittest.IsolatedAsyncioTestCase):
    """The requirement this feature must NOT satisfy."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.root = f"{self.tmp.name}/rec"
        # These go through `messages_append`, which records with the DEFAULT
        # root. Without this the suite writes a real conversation into the
        # working tree -- gitignored and 0600, but still a live recording
        # created by a test run.
        p = patch.object(cr, "default_root", lambda: Path(self.root))
        p.start()
        self.addCleanup(p.stop)
        # Recording is opt-in per benchmark run and off by default, so every
        # fixture that expects a file has to switch it on explicitly. That the
        # default is off is asserted separately, in BenchmarkGateTests.
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "1")

    async def _chat(self, chat_id, owner="u1", voice=True, parent=None):
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "voice_mode, parent_chat_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, chat_id, owner, "/tmp", chat_id,
             1 if voice else 0, parent, 1, 1))
        await db.db_conn.commit()

    async def test_recording_a_new_conversation_keeps_the_old_rows(self):
        """The heart of it. Two conversations exist; the second is recorded;
        the first must still be in the database untouched. A retention change
        -- the thing this deliberately is NOT -- would delete it."""
        from routes import db_chats
        await self._chat("c1")
        await self._chat("c2")
        await db_chats.messages_append("c1", "user", "first")
        await db_chats.messages_append("c2", "user", "second")

        rows = await (await db.db_conn.execute(
            "SELECT chat_id, content FROM messages ORDER BY id")).fetchall()
        self.assertEqual([r["chat_id"] for r in rows], ["c1", "c2"])
        chats = await (await db.db_conn.execute(
            "SELECT COUNT(*) c FROM chats")).fetchone()
        self.assertEqual(chats["c"], 2)

    async def test_the_recording_follows_the_latest_conversation(self):
        from routes import db_chats
        await self._chat("c1")
        await self._chat("c2")
        await db_chats.messages_append("c1", "user", "first")
        await cr.record_conversation("c1")
        first = json.loads(cr.recording_path().read_text())
        await db_chats.messages_append("c2", "user", "second")
        await cr.record_conversation("c2")
        second = json.loads(cr.recording_path().read_text())
        self.assertEqual(first["chat"]["id"], "c1")
        self.assertEqual(second["chat"]["id"], "c2")

    async def test_a_recording_failure_never_breaks_the_message_write(self):
        """The message is the thing that matters; the recording is a copy. A
        full disk must not stop the console storing conversations."""
        from routes import db_chats
        await self._chat("c1")
        with patch.object(cr, "write_recording",
                          side_effect=OSError("No space left on device")):
            msg_id = await db_chats.messages_append("c1", "user", "kept")
        self.assertIsNotNone(msg_id)
        row = await (await db.db_conn.execute(
            "SELECT content FROM messages WHERE id = ?", (msg_id,))).fetchone()
        self.assertEqual(row["content"], "kept")

    async def test_a_text_chat_does_not_overwrite_the_recording(self):
        """Voice only. Text chats are kept forever already, and letting one
        overwrite the recording would mean the "most recent conversation" was
        routinely a text chat this feature was never about."""
        from routes import db_chats
        await self._chat("voice1", voice=True)
        await self._chat("text1", voice=False)
        await db_chats.messages_append("voice1", "user", "spoken")
        await db_chats.messages_append("text1", "user", "typed")
        # Both conversations END; only the voice one may be written.
        await cr.record_conversation("voice1")
        await cr.record_conversation("text1")
        data = json.loads(cr.recording_path().read_text())
        self.assertEqual(data["chat"]["id"], "voice1")

    async def test_an_unknown_chat_records_nothing_rather_than_raising(self):
        self.assertIsNone(await cr.record_conversation("no-such-chat",
                                                        self.root))


class SurvivesTheHandoffDeletionTests(unittest.IsolatedAsyncioTestCase):
    """The reason this feature exists.

    `routes/voice.voice_handoff` summarises a voice chat into its parent and
    then calls `db.chat_delete`, which removes the chat, its messages and
    their FTS entries. It does that on ALL THREE of its exit paths, so a voice
    conversation is destroyed as a matter of course and only a 2-4 sentence
    summary survives -- and on two of the three, not even that.

    Measured on the live database 2026-09-17: a four-turn voice conversation
    that afternoon left four `voice_turn_timing` rows (model and latency only,
    no `chat_id`) and nothing else. No transcript, no chat, no audio.

    So these tests assert the recording survives the deletion, on every path.
    A recorder wired only to the success path would look correct in normal use
    and lose exactly the conversations whose handoff failed.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.root = f"{self.tmp.name}/rec"
        p = patch.object(cr, "default_root", lambda: Path(self.root))
        p.start()
        self.addCleanup(p.stop)
        # Recording is opt-in per benchmark run and off by default, so every
        # fixture that expects a file has to switch it on explicitly. That the
        # default is off is asserted separately, in BenchmarkGateTests.
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "1")

    async def _voice_chat_with_turns(self):
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "voice_mode, parent_chat_id, created_at, updated_at) "
            "VALUES ('v1','v1','u1','/tmp','v1',1,'p1',1,1)")
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "created_at, updated_at) VALUES ('p1','p1','u1','/tmp','p1',1,1)")
        await db.db_conn.commit()
        from routes import db_chats
        await db_chats.messages_batch(
            "v1", [("user", "what is the deploy status"),
                   ("assistant", "it is live")])

    async def test_the_conversation_survives_a_successful_handoff(self):
        from routes import voice as voice_routes
        await self._voice_chat_with_turns()
        await voice_routes._record_voice_conversation("v1", "A summary.")
        await db.chat_delete("v1", "u1")

        gone = await (await db.db_conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE chat_id='v1'")).fetchone()
        self.assertEqual(gone["c"], 0, "the chat really was deleted")

        data = json.loads(cr.recording_path().read_text())
        self.assertEqual(data["chat"]["id"], "v1")
        self.assertEqual([m["content"] for m in data["messages"]],
                         ["what is the deploy status", "it is live"])
        self.assertEqual(data["handoff_summary"], "A summary.")

    async def test_the_conversation_survives_a_handoff_with_no_summary(self):
        """Two of the three deletion paths produce no summary at all -- the
        missing-backend fallback and the exception handler. Those are the
        cases where the recording is the ONLY thing that will survive, so
        `handoff_summary` being None must not stop the write."""
        from routes import voice as voice_routes
        await self._voice_chat_with_turns()
        await voice_routes._record_voice_conversation("v1", None)
        await db.chat_delete("v1", "u1")
        data = json.loads(cr.recording_path().read_text())
        self.assertEqual(data["message_count"], 2)
        self.assertIsNone(data["handoff_summary"])

    async def test_every_deletion_path_records_first(self):
        """Structural: each `chat_delete` in `voice_handoff` must be preceded
        by a recording call. Asserted against the source because the three
        paths are hard to drive end to end -- one needs a missing backend, one
        a live model call, one an exception mid-call -- and a path that
        deletes without recording is silent until a conversation is lost."""
        source = (REPO_ROOT / "routes" / "voice.py").read_text()
        body = source[source.index("async def voice_handoff"):]
        deletes = body.count("await db.chat_delete(chat_id, owner)")
        records = body.count("await _record_voice_conversation(")
        self.assertEqual(deletes, 3, "voice_handoff's delete paths changed")
        self.assertEqual(records, deletes,
                         "a chat_delete path has no recording before it")

    async def test_a_recording_failure_does_not_block_the_teardown(self):
        """Losing the recording must not leave voice chats undeleted and
        accumulating -- that would trade a privacy problem for a storage one.
        """
        from routes import voice as voice_routes
        await self._voice_chat_with_turns()
        with patch.object(cr, "write_recording",
                          side_effect=OSError("No space left on device")):
            await voice_routes._record_voice_conversation("v1", "s")
        await db.chat_delete("v1", "u1")
        gone = await (await db.db_conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE id='v1'")).fetchone()
        self.assertEqual(gone["c"], 0)


class ReplayCompletenessTests(unittest.IsolatedAsyncioTestCase):
    """Enough to REPRODUCE a voice turn, not merely to read it back.

    `stream_voice_turn` sends the model a system prompt, plus -- when the chat
    has a parent -- a structured context block distilled at call time from the
    parent's last 12 messages by an inline heuristic. The `messages` table
    stores only the user's prompt and the reply. A benchmark replaying from
    those alone would send different input and score the difference as a model
    result, which is the specific failure these tests exist to prevent.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.root = f"{self.tmp.name}/rec"
        p = patch.object(cr, "default_root", lambda: Path(self.root))
        p.start()
        self.addCleanup(p.stop)
        # Recording is opt-in per benchmark run and off by default, so every
        # fixture that expects a file has to switch it on explicitly. That the
        # default is off is asserted separately, in BenchmarkGateTests.
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "1")
        cr.forget_turns("v1")
        self.addCleanup(cr.forget_turns, "v1")
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "voice_mode, ai_machine_id, created_at, updated_at) "
            "VALUES ('v1','v1','u1','/tmp','v1',1,'machine-7',1,1)")
        await db.db_conn.commit()

    def _turn(self, **over):
        turn = {
            "recorded_at": "2026-09-17T19:47:12Z",
            "model_requested": "azure_ai/gpt-5.4-mini-copilot",
            "model_served": "gpt-5.4-mini-copilot",
            "ai_machine_id": "machine-7",
            "messages_sent": [
                {"role": "system", "content": "You are a voice assistant."},
                {"role": "user", "content": "STRUCTURED CONTEXT: GOAL: ship"},
                {"role": "user", "content": "what is the status"},
            ],
            "assistant_text": "It is live.",
            "params": {"stream": True, "temperature": None,
                       "max_tokens": None},
            "input_tokens": 120, "output_tokens": 8,
            "ttft_ms": 900, "total_ms": 1633, "failed": False,
        }
        turn.update(over)
        return turn

    async def _record(self):
        """Drive a whole conversation and then END it, which is when the file
        is written. There is no per-turn write any more: the file must hold
        the PREVIOUS conversation while a new one is in progress."""
        from routes import db_chats
        cr.note_turn("v1", self._turn())
        await db_chats.messages_append("v1", "user", "what is the status")
        await cr.record_conversation("v1")
        return json.loads(cr.recording_path().read_text())

    async def test_the_exact_messages_sent_are_recorded(self):
        """The heart of it: the system prompt and the structured context block
        are in the file, not just the user's prompt. Those two are what a
        replay cannot rebuild."""
        data = await self._record()
        sent = data["turns"][0]["messages_sent"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "user"])
        self.assertIn("voice assistant", sent[0]["content"])
        self.assertIn("STRUCTURED CONTEXT", sent[1]["content"])

    async def test_the_stored_messages_alone_are_not_enough(self):
        """States the gap explicitly, so the reason for `turns` survives
        someone deciding it is redundant with `messages`."""
        data = await self._record()
        stored = " ".join(m["content"] for m in data["messages"])
        self.assertNotIn("voice assistant", stored)
        self.assertNotIn("STRUCTURED CONTEXT", stored)

    async def test_both_the_requested_and_served_model_are_recorded(self):
        """A gateway can answer with a different model than the one asked
        for. A replay compared against the wrong model is worse than none."""
        data = await self._record()
        turn = data["turns"][0]
        self.assertEqual(turn["model_requested"],
                         "azure_ai/gpt-5.4-mini-copilot")
        self.assertEqual(turn["model_served"], "gpt-5.4-mini-copilot")

    async def test_generation_settings_and_measurements_are_recorded(self):
        """Params are written out rather than assumed: nothing sets a
        temperature or max_tokens on this path, so the gateway's defaults
        apply -- and "unset" is itself a fact a replay needs."""
        data = await self._record()
        turn = data["turns"][0]
        self.assertIn("temperature", turn["params"])
        self.assertIsNone(turn["params"]["temperature"])
        for field in ("input_tokens", "output_tokens", "ttft_ms", "total_ms"):
            with self.subTest(field=field):
                self.assertIsNotNone(turn[field])

    async def test_the_backend_is_identified_without_its_url_or_key(self):
        """`routes/voice.py` warns twice that base_url and api_key must never
        be logged, and a file on disk is a log by another name. The backend is
        named by its opaque row id instead -- enough to know WHICH backend,
        with nothing to reach it.

        This drives the PRODUCTION builder, `voice._note_replay_turn`, rather
        than this class's own fixture. An earlier version asserted against the
        fixture and so could not fail: adding a `base_url` to the real record
        left it green, which is the one place in this file a can't-fail test
        is least affordable.
        """
        from routes import db_chats, voice as voice_routes
        voice_routes._note_replay_turn(
            chat_id="v1",
            chat={"id": "v1", "ai_machine_id": "machine-7"},
            sent_messages=[{"role": "user", "content": "hi"}],
            assistant_text="hello",
            model_requested="azure_ai/gpt-5.4-mini-copilot",
            model_served="gpt-5.4-mini-copilot",
            ttft_ms=900, total_ms=1633,
            input_tokens=120, output_tokens=8, failed=False)
        await db_chats.messages_append("v1", "user", "hi")
        await cr.record_conversation("v1")
        data = json.loads(cr.recording_path().read_text())

        self.assertEqual(data["turns"][0]["ai_machine_id"], "machine-7")
        blob = json.dumps(data)
        for forbidden in ("base_url", "api_key", "https://", "http://",
                          "Bearer ", "sk-"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    async def test_the_production_builder_records_every_replay_field(self):
        """The same builder, checked for completeness rather than for what it
        must not contain -- so a field quietly dropped from the real record
        fails here instead of surfacing as an unreplayable benchmark."""
        from routes import db_chats, voice as voice_routes
        voice_routes._note_replay_turn(
            chat_id="v1", chat={"id": "v1", "ai_machine_id": "machine-7"},
            sent_messages=[{"role": "system", "content": "sys"},
                           {"role": "user", "content": "hi"}],
            assistant_text="hello",
            model_requested="m-req", model_served="m-served",
            ttft_ms=900, total_ms=1633,
            input_tokens=120, output_tokens=8, failed=False)
        await db_chats.messages_append("v1", "user", "hi")
        await cr.record_conversation("v1")
        turn = json.loads(cr.recording_path().read_text())["turns"][0]
        for field in ("recorded_at", "model_requested", "model_served",
                      "ai_machine_id", "messages_sent", "assistant_text",
                      "params", "input_tokens", "output_tokens",
                      "ttft_ms", "total_ms", "failed"):
            with self.subTest(field=field):
                self.assertIn(field, turn)
        self.assertEqual([m["role"] for m in turn["messages_sent"]],
                         ["system", "user"])
        self.assertEqual(turn["model_served"], "m-served")

    async def test_turns_accumulate_across_a_conversation(self):
        from routes import db_chats
        cr.note_turn("v1", self._turn())
        cr.note_turn("v1", self._turn(assistant_text="Second."))
        await db_chats.messages_append("v1", "user", "again")
        await cr.record_conversation("v1")
        data = json.loads(cr.recording_path().read_text())
        self.assertEqual(len(data["turns"]), 2)
        self.assertEqual(data["turns"][1]["assistant_text"], "Second.")

    async def test_the_turn_buffer_is_bounded(self):
        """A chat that is never torn down must not grow without limit in a
        long-lived service."""
        for i in range(cr.MAX_RECORDED_TURNS + 25):
            cr.note_turn("v1", self._turn(assistant_text=str(i)))
        self.assertEqual(len(cr._TURNS["v1"]), cr.MAX_RECORDED_TURNS)
        self.assertEqual(cr._TURNS["v1"][-1]["assistant_text"],
                         str(cr.MAX_RECORDED_TURNS + 24))

    async def test_turns_are_released_when_the_chat_is_torn_down(self):
        cr.note_turn("v1", self._turn())
        self.assertIn("v1", cr._TURNS)
        cr.forget_turns("v1")
        self.assertNotIn("v1", cr._TURNS)

    async def test_a_chat_with_no_turns_records_an_empty_list(self):
        """Never a missing key: a consumer reading `turns` must not have to
        tell "no turns" apart from "this recording predates the field"."""
        from routes import db_chats
        await db_chats.messages_append("v1", "user", "hi")
        await cr.record_conversation("v1")
        data = json.loads(cr.recording_path().read_text())
        self.assertEqual(data["turns"], [])


class BenchmarkGateTests(unittest.IsolatedAsyncioTestCase):
    """Scoped to voice-BENCHMARK conversations, not to voice in general.

    A `voice_mode` filter alone catches every voice conversation anyone has,
    which is broader than the brief ("not a general recording feature for all
    chats") and leaves a file holding raw conversation content being written
    continuously. Recording is opt-in per benchmark run and off by default.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.root = f"{self.tmp.name}/rec"
        p = patch.object(cr, "default_root", lambda: Path(self.root))
        p.start()
        self.addCleanup(p.stop)
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "voice_mode, created_at, updated_at) "
            "VALUES ('v1','v1','u1','/tmp','v1',1,1,1)")
        await db.db_conn.commit()
        from routes import db_chats
        await db_chats.messages_append("v1", "user", "spoken")

    def test_the_default_is_off(self):
        self.assertFalse(cr.BENCHMARK_RECORDING_DEFAULT)

    async def test_nothing_is_written_while_it_is_off(self):
        """The property that makes this not a general recording feature: a
        real voice conversation, ended properly, writes no file at all."""
        self.assertIsNone(await cr.record_conversation("v1"))
        self.assertFalse(cr.recording_path().exists())

    async def test_it_records_once_switched_on(self):
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "1")
        self.assertIsNotNone(await cr.record_conversation("v1"))
        self.assertTrue(cr.recording_path().exists())

    async def test_switching_it_off_again_stops_new_writes(self):
        """Turning it off must actually stop recording, not merely stop
        starting -- a benchmark run that ended should not keep writing."""
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "1")
        await cr.record_conversation("v1")
        first = cr.recording_path().read_text()
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "0")
        from routes import db_chats
        await db_chats.messages_append("v1", "user", "later")
        self.assertIsNone(await cr.record_conversation("v1"))
        self.assertEqual(cr.recording_path().read_text(), first)

    async def test_a_malformed_setting_is_off_not_on(self):
        """A file holding raw conversation content must never start being
        written because a settings row could not be parsed."""
        for raw in ("", "true", "yes", "01", " ", "None"):
            with self.subTest(raw=raw):
                await setting_set(cr.BENCHMARK_RECORDING_SETTING, raw)
                self.assertFalse(await cr.benchmark_recording_enabled())


class EveryEndingPathRecordsTests(unittest.IsolatedAsyncioTestCase):
    """A voice conversation ends three ways, and all three must write.

    "Agree" and "Summarize Only" both POST to /voice/handoff, which records
    before it deletes. "Reject" (web/assets/voice-handoff.js) calls
    DELETE /api/chats/{id} and bypasses that path entirely -- and rejection is
    the ordinary ending for a voice chat with no parent to hand off to. It is
    how the 2026-09-17 20:47 conversation ended, and with the per-turn write
    removed it is the path that would otherwise lose a benchmark conversation.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.root = f"{self.tmp.name}/rec"
        p = patch.object(cr, "default_root", lambda: Path(self.root))
        p.start()
        self.addCleanup(p.stop)
        await setting_set(cr.BENCHMARK_RECORDING_SETTING, "1")
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "voice_mode, created_at, updated_at) "
            "VALUES ('v1','v1','u1','/tmp','v1',1,1,1)")
        await db.db_conn.commit()
        from routes import db_chats
        await db_chats.messages_batch("v1", [("user", "spoken"),
                                             ("assistant", "heard")])

    async def test_the_reject_path_records_before_it_deletes(self):
        from routes import chats as chat_routes
        await chat_routes._record_voice_chat_before_delete("v1")
        await db.chat_delete("v1", "u1")

        gone = await (await db.db_conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE chat_id='v1'")).fetchone()
        self.assertEqual(gone["c"], 0, "the chat really was deleted")
        data = json.loads(cr.recording_path().read_text())
        self.assertEqual(data["chat"]["id"], "v1")
        self.assertEqual([m["content"] for m in data["messages"]],
                         ["spoken", "heard"])

    async def test_the_reject_path_leaves_a_text_chat_alone(self):
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, owner_id, work_dir, session_id, "
            "voice_mode, created_at, updated_at) "
            "VALUES ('t1','t1','u1','/tmp','t1',0,1,1)")
        await db.db_conn.commit()
        from routes import chats as chat_routes, db_chats
        await db_chats.messages_append("t1", "user", "typed")
        await chat_routes._record_voice_chat_before_delete("t1")
        self.assertFalse(cr.recording_path().exists())

    async def test_the_reject_path_never_blocks_the_delete(self):
        from routes import chats as chat_routes
        with patch.object(cr, "record_conversation",
                          side_effect=OSError("No space left on device")):
            await chat_routes._record_voice_chat_before_delete("v1")
        await db.chat_delete("v1", "u1")
        gone = await (await db.db_conn.execute(
            "SELECT COUNT(*) c FROM chats WHERE id='v1'")).fetchone()
        self.assertEqual(gone["c"], 0)

    async def test_every_delete_path_in_the_codebase_records_first(self):
        """Structural, like the voice_handoff check: the generic hard-delete
        route must record before it deletes, or the Reject button silently
        loses the conversation."""
        source = (REPO_ROOT / "routes" / "chats.py").read_text()
        body = source[source.index("async def handle_chat_delete"):]
        body = body[:body.index("def render_chat_markdown")]
        self.assertIn("_record_voice_chat_before_delete", body)
        self.assertLess(body.index("_record_voice_chat_before_delete"),
                        body.index("await db.chat_delete"),
                        "the recording must happen BEFORE the delete")


if __name__ == "__main__":
    unittest.main()
