"""QA: the model you choose is the model that is used, and nothing else pins it.

`runner.get_default_model` resolves in this order:

    the conversation's own model -> its backend's default -> the global
    setting -> config.MODEL_NAME

That order is right, and it made a write-back after every turn actively harmful.
Both turn paths recorded whatever model had just served the turn onto
`chats.model`, which is the field at the top of that list -- so a conversation
ran once, was silently pinned to the model that happened to answer, and from
then on ignored the active machine and the global default. Nobody chose it and
nothing in the interface said it had happened.

The evidence was in production data rather than in a test: three conversations
pinned to `azure_ai/gpt-5.6-luna`, which no configured backend serves. They were
still asking for it.

Nothing is lost by not recording it. The served model is in `usage_events` per
turn, it is returned in the turn response, and the UI has a label for it that is
separate from the picker.
"""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import runner

ROOT = Path(__file__).resolve().parents[1]


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    """What actually gets asked for, given what is configured."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = [patch.object(config, "DB_PATH", f"{self.tmp.name}/db"),
                   patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")]
        for p in self._p:
            p.start()
        Path(config.PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
        await db.init()
        await db.ai_machine_create("m-gw", "Gateway", "", 0, "k",
                                   "gateway/model", "https://gw.invalid", "",
                                   "admin", provider="anthropic")
        await db.ai_machine_activate("m-gw", "admin")
        await db.setting_set("default_model", "global/model")
        wd = Path(config.PROJECTS_ROOT) / "c1"
        wd.mkdir(parents=True, exist_ok=True)
        await db.chat_create("c1", "chat", None, str(wd), "admin")

    async def asyncTearDown(self):
        await db.close()
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    async def test_an_unpinned_chat_follows_the_active_machine(self):
        """The case the user actually configured: change the machine, get it."""
        self.assertEqual(await runner.get_default_model("c1", "admin"),
                         "gateway/model")

    async def test_an_explicit_choice_wins(self):
        """Choosing a model must still stick -- that is the feature."""
        await db.chat_update("c1", "admin", model="chosen/model")
        self.assertEqual(await runner.get_default_model("c1", "admin"),
                         "chosen/model")

    async def test_clearing_the_choice_returns_to_the_machine(self):
        """"Automatic" in the picker has to mean automatic again."""
        await db.chat_update("c1", "admin", model="chosen/model")
        await db.chat_update("c1", "admin", model="")
        self.assertEqual(await runner.get_default_model("c1", "admin"),
                         "gateway/model")

    async def test_the_global_default_is_used_when_the_machine_names_none(self):
        await db.ai_machine_update("m-gw", "admin", model="")
        # ai_machine_update ignores empty values, so clear it directly.
        await db.db_conn.execute("UPDATE ai_machines SET model = '' WHERE id = ?",
                                 ("m-gw",))
        await db.db_conn.commit()
        self.assertEqual(await runner.get_default_model("c1", "admin"),
                         "global/model")


class WriteBackTests(unittest.TestCase):
    """The turn paths must not pin a conversation to what happened to answer."""

    def _turn_paths(self) -> str:
        return (ROOT / "app.py").read_text(encoding="utf-8")

    def test_no_turn_path_writes_the_served_model_back(self):
        """Parsed, not grepped: a comment mentioning it must not count.

        Both call sites are being removed *and* described in comments right
        where they were, so a substring search for `chat_set_model` finds the
        explanation and reports the bug still present.
        """
        tree = ast.parse(self._turn_paths())
        calls = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "chat_set_model"
        ]
        self.assertEqual(
            calls, [],
            f"app.py calls db.chat_set_model at lines {calls}; a turn that "
            "records its own model pins the conversation to it",
        )

    def test_the_setter_still_exists_for_an_explicit_choice(self):
        """Removing the calls must not be mistaken for removing the ability."""
        self.assertTrue(hasattr(db, "chat_set_model"))
        self.assertIn("model", db._ALLOWED_CHAT_FIELDS)

    def test_the_served_model_is_still_reported(self):
        """It is still returned to the client and recorded per turn."""
        source = self._turn_paths()
        self.assertIn('"model": model', source)
        self.assertIn("_record_turn_usage", source)


if __name__ == "__main__":
    unittest.main()
