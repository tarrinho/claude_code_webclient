"""QA: keeping a voice recording so the next conversation cannot take it.

The rolling recording holds exactly one conversation and is overwritten at the
start of the next. That is the feature as specified. It also means a
conversation worth benchmarking against survives only until somebody next
speaks to the console, so keeping one is a deliberate act with its own command.

Two properties carry the weight here and both are asserted against the
filesystem rather than against a return value:

* the archive is `0600` inside a `0700` directory, for the same reason the
  rolling file is -- these are the user's own words on disk;
* archiving twice produces one file, because an operator will do it twice.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import conversation_recording as cr


def _recording(chat_id="b15c262a8e6a49f3aa0c26177f03e0b3",
               created_at="2026-09-17T21:30:33Z", turns=1, text="hello"):
    return {
        "chat": {"id": chat_id, "title": "voice-chat-app",
                 "model": "azure_ai/gpt-5.4-mini-copilot", "voice_mode": 1,
                 "created_at": created_at},
        "messages": [{"role": "user", "content": text,
                      "created_at": created_at}],
        "message_count": 1,
        "turns": [{"model_served": "azure_ai/gpt-5.4-mini-copilot",
                   "ttft_ms": 2282, "total_ms": 2320, "failed": False}] * turns,
        "handoff_summary": None,
    }


class _ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _write_rolling(self, payload=None):
        cr.ensure_recording_dir(self.root)
        path = cr.recording_path(self.root)
        cr._write_private_atomic(
            path, json.dumps(payload if payload is not None else _recording()))
        return path


class ArchiveCurrentTests(_ArchiveCase):
    def test_nothing_to_archive_returns_none(self):
        self.assertIsNone(cr.archive_current(self.root))

    def test_it_copies_the_rolling_recording(self):
        self._write_rolling()
        target = cr.archive_current(self.root)
        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(target.read_text())["chat"]["id"],
                         _recording()["chat"]["id"])

    def test_the_rolling_file_is_left_in_place(self):
        """A copy, not a move: the console's own 'last conversation' view still
        reads the rolling file, and keeping a sample must not blank it."""
        source = self._write_rolling()
        cr.archive_current(self.root)
        self.assertTrue(source.exists())

    def test_archiving_twice_keeps_one_file(self):
        self._write_rolling()
        first = cr.archive_current(self.root)
        second = cr.archive_current(self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(cr.archived_samples(self.root)), 1)

    def test_a_different_recording_of_the_same_chat_is_kept_separately(self):
        """A resumed conversation keeps its id AND its `created_at`, so those
        two alone cannot tell a longer later recording from an earlier one.
        The content hash in the name is what separates them -- without it the
        second archive would silently collide with the first."""
        self._write_rolling(_recording(turns=1))
        first = cr.archive_current(self.root)
        self._write_rolling(_recording(turns=4))
        second = cr.archive_current(self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(len(cr.archived_samples(self.root)), 2)

    def test_the_name_carries_the_conversation_start_not_the_archive_time(self):
        """So the on-disk order is the order the conversations happened."""
        self._write_rolling(_recording(created_at="2026-09-17T21:30:33Z"))
        target = cr.archive_current(self.root)
        self.assertTrue(target.name.startswith("20260917T213033Z-"), target.name)
        self.assertIn("b15c262a", target.name)

    def test_a_corrupt_rolling_file_is_still_kept(self):
        """Refusing here would mean a half-written recording is lost to the
        next conversation, which is strictly worse than keeping it unparsed."""
        cr.ensure_recording_dir(self.root)
        cr._write_private_atomic(cr.recording_path(self.root), "{not json")
        target = cr.archive_current(self.root)
        self.assertIsNotNone(target)
        self.assertEqual(target.read_text(), "{not json")


class ArchivePermissionTests(_ArchiveCase):
    def test_the_archive_file_is_0600(self):
        self._write_rolling()
        target = cr.archive_current(self.root)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), cr.FILE_MODE)

    def test_the_archive_directory_is_0700(self):
        self._write_rolling()
        cr.archive_current(self.root)
        directory = cr.archive_dir(self.root)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), cr.DIR_MODE)

    def test_a_pre_existing_wide_archive_directory_is_tightened(self):
        """The half that matters on a real host: a directory created earlier
        under a laxer umask keeps its mode forever otherwise, and the samples
        inside would be protected by the file mode alone."""
        directory = cr.archive_dir(self.root)
        directory.mkdir(parents=True)
        os.chmod(directory, 0o755)
        cr.ensure_archive_dir(self.root)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), cr.DIR_MODE)

    def test_the_archive_lives_under_the_gitignored_recordings_directory(self):
        """`/data/recordings/` is already gitignored. Putting the archive
        anywhere else would need a second ignore rule, and the failure mode of
        forgetting it is committing somebody's transcript."""
        self.assertEqual(cr.archive_dir(self.root).parent,
                         cr.recording_dir(self.root))


class ArchivedSamplesTests(_ArchiveCase):
    def test_empty_when_nothing_kept(self):
        self.assertEqual(cr.archived_samples(self.root), [])

    def test_oldest_first(self):
        for stamp in ("2026-09-17T21:30:33Z", "2026-09-16T08:00:00Z",
                      "2026-09-18T12:00:00Z"):
            self._write_rolling(_recording(created_at=stamp))
            cr.archive_current(self.root)
        names = [p.name for p in cr.archived_samples(self.root)]
        self.assertEqual(names, sorted(names))
        self.assertTrue(names[0].startswith("20260916"), names)
        self.assertTrue(names[-1].startswith("20260918"), names)

    def test_it_ignores_non_json_files(self):
        cr.ensure_archive_dir(self.root)
        (cr.archive_dir(self.root) / "notes.txt").write_text("x")
        self._write_rolling()
        cr.archive_current(self.root)
        self.assertEqual(len(cr.archived_samples(self.root)), 1)


class ArchiveCliTests(_ArchiveCase):
    """The command, driven through `main`."""

    def _cli(self):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "bin" / "wc-archive-voice-sample.py"
        spec = importlib.util.spec_from_file_location("wc_archive_voice_sample", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_exits_1_when_there_is_no_recording(self):
        module = self._cli()
        original = cr.default_root
        cr.default_root = lambda: self.root
        self.addCleanup(lambda: setattr(cr, "default_root", original))
        self.assertEqual(module.main([]), 1)

    def test_it_keeps_the_recording_and_exits_0(self):
        module = self._cli()
        original = cr.default_root
        cr.default_root = lambda: self.root
        self.addCleanup(lambda: setattr(cr, "default_root", original))
        self._write_rolling()
        self.assertEqual(module.main([]), 0)
        self.assertEqual(len(cr.archived_samples(self.root)), 1)

    def test_list_changes_nothing(self):
        module = self._cli()
        original = cr.default_root
        cr.default_root = lambda: self.root
        self.addCleanup(lambda: setattr(cr, "default_root", original))
        self._write_rolling()
        self.assertEqual(module.main(["--list"]), 0)
        self.assertEqual(cr.archived_samples(self.root), [],
                         "--list archived something")


class NoCredentialsInASampleTests(_ArchiveCase):
    def test_an_archived_sample_carries_no_base_url_or_api_key(self):
        """`routes/voice.py` deliberately omits both from every turn record.
        Asserted here too, on the artefact that actually persists, because this
        is the file that now outlives the conversation and could be copied
        somewhere less careful."""
        self._write_rolling()
        target = cr.archive_current(self.root)
        blob = target.read_text().lower()
        for forbidden in ("base_url", "api_key", "authorization", "bearer"):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
