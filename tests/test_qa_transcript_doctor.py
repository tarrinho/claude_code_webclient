"""QA: the transcript doctor finds the session it was asked for, and repairs it.

Written after the tool failed silently in the one place that matters. Its usage
line has always said ``--fix cweb5   repair one, by name or id``; only name ever
worked, because ``discover()`` keyed its results by name alone. Asking for a
session id printed "no transcripts found" and exited 1.

That is not a cosmetic gap. ``bin/wc-claude.sh`` repairs a transcript before
resuming a session, passing whatever followed ``--resume`` -- and a session
started by ``claude -p`` is auto-named from its first prompt, so it is resumed
by id. The repair found nothing, and the session was resumed unrepaired. The
failure was observed end to end: an Anthropic conversation moved to a gateway
and back produced ``400 messages: text content blocks must be non-empty``,
"repaired" nothing, and failed again identically.

The module is loaded from its path because bin/ is not a package and the file
has a hyphenated name.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = ROOT / "bin" / "claude-transcript-doctor.py"

_spec = importlib.util.spec_from_file_location("transcript_doctor", DOCTOR_PATH)
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


def signed_thinking(text="reasoning"):
    return {"type": "thinking", "thinking": text, "signature": "sig-abc"}


def foreign_thinking(text="reasoning", signature=None):
    block = {"type": "thinking", "thinking": text}
    if signature is not None:
        block["signature"] = signature
    return block


def text(value="hello"):
    return {"type": "text", "text": value}


def record(uuid, parent, content, kind="assistant"):
    return {"type": kind, "uuid": uuid, "parentUuid": parent,
            "message": {"role": "assistant", "content": content}}


def write(path: Path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")


class PoisonTests(unittest.TestCase):
    """The discriminator is the signature, never the type."""

    def test_thinking_without_a_signature_is_poison(self):
        self.assertEqual(doctor.is_poison(foreign_thinking()), "foreign thinking")

    def test_an_empty_signature_is_poison_too(self):
        self.assertEqual(doctor.is_poison(foreign_thinking(signature="  ")),
                         "foreign thinking")

    def test_signed_thinking_is_kept(self):
        """Stripping these would discard real reasoning for no reason."""
        self.assertIsNone(doctor.is_poison(signed_thinking()))

    def test_empty_text_is_poison(self):
        self.assertEqual(doctor.is_poison(text("")), "empty text")
        self.assertEqual(doctor.is_poison(text("   ")), "empty text")

    def test_ordinary_text_and_tool_calls_are_kept(self):
        self.assertIsNone(doctor.is_poison(text("hello")))
        self.assertIsNone(doctor.is_poison({"type": "tool_use", "name": "Read"}))

    def test_a_non_dict_block_is_not_poison(self):
        """Transcripts hold odd shapes; the predicate must not raise on them."""
        for block in ("string", 42, None, []):
            with self.subTest(block=block):
                self.assertIsNone(doctor.is_poison(block))


class SelectionTests(unittest.TestCase):
    """The bug: a session id reached nothing."""

    SESSIONS: ClassVar[dict[str, tuple[Path, str]]] = {
        "cweb5": (Path("/tmp/a.jsonl"), "1111-aaaa"),
        "Remember this codeword: TANGERINE.": (Path("/tmp/b.jsonl"), "2222-bbbb"),
    }

    def test_a_name_selects_its_session(self):
        got = doctor.select(self.SESSIONS, ["cweb5"])
        self.assertEqual(list(got), ["cweb5"])

    def test_a_session_id_selects_its_session(self):
        """This returned {} before, and the caller carried on regardless."""
        got = doctor.select(self.SESSIONS, ["2222-bbbb"])
        self.assertEqual(len(got), 1)
        self.assertEqual(next(iter(got.values()))[1], "2222-bbbb")

    def test_an_auto_named_session_is_reachable_by_id(self):
        """Its name is a sentence from its first prompt -- nobody types that."""
        self.assertTrue(doctor.select(self.SESSIONS, ["2222-bbbb"]))

    def test_no_argument_selects_everything(self):
        self.assertEqual(len(doctor.select(self.SESSIONS, [])), 2)

    def test_an_unknown_identifier_selects_nothing(self):
        self.assertEqual(doctor.select(self.SESSIONS, ["nope"]), {})


class DiscoveryTests(unittest.TestCase):
    """Sessions are found through the two directories the CLI writes."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sessions = root / "sessions"
        self.projects = root / "projects" / "proj"
        self.sessions.mkdir(parents=True)
        self.projects.mkdir(parents=True)
        self._orig = (doctor.SESSIONS, doctor.PROJECTS)
        doctor.SESSIONS = self.sessions
        doctor.PROJECTS = root / "projects"
        self.addCleanup(self._restore)

    def _restore(self):
        doctor.SESSIONS, doctor.PROJECTS = self._orig
        self.tmp.cleanup()

    def _session(self, sid, name=None):
        meta = {"sessionId": sid}
        if name is not None:
            meta["name"] = name
        (self.sessions / f"{sid}.json").write_text(json.dumps(meta))
        write(self.projects / f"{sid}.jsonl", [record("u1", None, [text()])])

    def test_a_named_session_is_keyed_by_name(self):
        self._session("aaa-111", "cweb5")
        self.assertIn("cweb5", doctor.discover())

    def test_a_session_without_a_name_is_keyed_by_id(self):
        """It used to be dropped entirely, so it could never be repaired."""
        self._session("bbb-222")
        found = doctor.discover()
        self.assertIn("bbb-222", found)
        self.assertEqual(found["bbb-222"][1], "bbb-222")

    def test_every_entry_carries_its_session_id(self):
        """select() needs the id, and the live check matches on it too."""
        self._session("ccc-333", "named")
        self.assertEqual(doctor.discover()["named"][1], "ccc-333")

    def test_a_session_with_no_transcript_is_omitted(self):
        (self.sessions / "ddd.json").write_text(json.dumps(
            {"sessionId": "ddd-444", "name": "orphan"}))
        self.assertNotIn("orphan", doctor.discover())

    def test_unreadable_metadata_is_skipped_rather_than_fatal(self):
        (self.sessions / "broken.json").write_text("{not json")
        self._session("eee-555", "fine")
        self.assertIn("fine", doctor.discover())


class RepairTests(unittest.TestCase):
    """What repair removes, what it keeps, and what it refuses to do."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def _records(self):
        return [json.loads(line) for line
                in self.path.read_text().splitlines() if line.strip()]

    def test_a_record_of_only_poison_is_dropped(self):
        write(self.path, [record("u1", None, [text()]),
                          record("u2", "u1", [foreign_thinking()])])
        result = doctor.repair(self.path, apply=True)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual([r["uuid"] for r in self._records()], ["u1"])

    def test_poison_beside_real_content_is_trimmed_not_dropped(self):
        """A record is usually [thinking, text]; dropping it loses the answer."""
        write(self.path, [record("u1", None, [foreign_thinking(), text("answer")])])
        doctor.repair(self.path, apply=True)
        kept = self._records()[0]["message"]["content"]
        self.assertEqual([b["type"] for b in kept], ["text"])
        self.assertEqual(kept[0]["text"], "answer")

    def test_signed_thinking_survives_a_repair(self):
        write(self.path, [record("u1", None, [signed_thinking(), text("a")])])
        doctor.repair(self.path, apply=True)
        kept = self._records()[0]["message"]["content"]
        self.assertEqual([b["type"] for b in kept], ["thinking", "text"])

    def test_children_are_relinked_to_a_surviving_ancestor(self):
        """A dangling parentUuid breaks the chain the CLI walks."""
        write(self.path, [
            record("u1", None, [text("first")]),
            record("u2", "u1", [foreign_thinking()]),
            record("u3", "u2", [text("third")]),
        ])
        result = doctor.repair(self.path, apply=True)
        self.assertGreaterEqual(result["relinked"], 1)
        kept = {r["uuid"]: r["parentUuid"] for r in self._records()}
        self.assertEqual(kept["u3"], "u1", "u3 still points at a dropped record")

    def test_a_clean_transcript_is_left_alone(self):
        write(self.path, [record("u1", None, [signed_thinking(), text("a")])])
        before = self.path.read_text()
        result = doctor.repair(self.path, apply=True)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["trimmed"], 0)
        self.assertEqual(self.path.read_text(), before)

    def test_the_original_is_preserved_and_never_overwritten(self):
        """The .orig is the only copy of what was there before any repair."""
        write(self.path, [record("u1", None, [text("first")]),
                          record("u2", "u1", [foreign_thinking()])])
        first = self.path.read_text()
        doctor.repair(self.path, apply=True)
        orig = self.path.with_suffix(self.path.suffix + ".orig")
        self.assertTrue(orig.is_file())
        self.assertEqual(orig.read_text(), first)

        write(self.path, [record("u3", None, [foreign_thinking()]),
                          record("u4", "u3", [text("x")])])
        doctor.repair(self.path, apply=True)
        self.assertEqual(orig.read_text(), first,
                         "a second repair overwrote the true original")

    def test_a_dry_run_changes_nothing(self):
        write(self.path, [record("u1", None, [text("a")]),
                          record("u2", "u1", [foreign_thinking()])])
        before = self.path.read_text()
        result = doctor.repair(self.path, apply=False)
        self.assertFalse(result["installed"])
        self.assertEqual(self.path.read_text(), before)

    def test_inspect_counts_the_two_poisons_separately(self):
        write(self.path, [record("u1", None, [foreign_thinking(), text(""),
                                              signed_thinking(), text("ok")])])
        info = doctor.inspect(self.path)
        self.assertEqual(info["foreign"], 1)
        self.assertEqual(info["empty_text"], 1)
        self.assertEqual(info["native"], 1)


if __name__ == "__main__":
    unittest.main()
