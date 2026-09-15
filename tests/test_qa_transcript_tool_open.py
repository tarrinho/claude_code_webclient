"""QA: tool detail in the transcript viewer starts expanded.

`buildTool` in web/assets/transcript.js used to build its <details> with
`className = 'tx-tool tx-tool-open'` and never set the `open` property. The CSS
for that class only adjusts opacity (transcript.js:52), so nothing opened it:
every tool call shipped folded behind a disclosure triangle under a class name
promising the opposite.

Reported by Pedro on 2026-09-15 as actions from terminal-linked chats not
appearing in the console. They were there -- one click away each, across a
transcript holding 37,243 tool_use blocks in the six most recent files.

What this test can and cannot do, stated rather than implied. `buildTool` is
declared inside the `mountTranscriptViewer` closure and is not exported, so
there is no way to call it without mounting the whole viewer against a real
session and a real transcript. These are therefore source assertions: they
prove the assignment is written, not that a rendered <details> is open. That is
the same class of test as the rest of tests/test_frontend.py for this file, and
it is a detector rather than a preventer -- it catches the property being
deleted, not the CSS being changed underneath it.

The specific thing pinned is the *pairing*. The defect was not a missing line
in isolation; it was a class name and a property disagreeing, with only the
class present. So the assertion is that both appear in the same function.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPT_JS = ROOT / "web/assets/transcript.js"


def _build_tool_body() -> str:
    """The source of buildTool, bounded by its closing brace.

    Bounded rather than windowed by a character count: a fixed window silently
    stops meaning anything once the function grows past it, which is how three
    other source tests in this suite came to report defects that did not exist
    (rules.md registry #116, 2026-09-14).
    """
    text = TRANSCRIPT_JS.read_text(encoding="utf-8")
    opener = "function buildTool(block) {"
    assert opener in text, "buildTool is gone from transcript.js"
    after = text.split(opener, 1)[1]
    body, sep, _ = after.partition("\n  }")
    assert sep, "no closing brace found for buildTool"
    return body


class ToolDetailStartsOpenTests(unittest.TestCase):
    def test_the_details_element_is_opened_not_merely_classed(self):
        """The defect. The class alone does nothing."""
        body = _build_tool_body()
        self.assertIn("tx-tool-open", body,
                      "the class that names the intent is gone")
        self.assertRegex(
            body, r"\bbox\.open\s*=",
            "buildTool sets the tx-tool-open class but never assigns box.open, "
            "so the <details> renders collapsed -- the exact defect reported on "
            "2026-09-15. The CSS for that class only changes opacity.",
        )

    def test_the_default_is_open(self):
        """Closed-by-default would reintroduce the symptom for a new reader.

        The preference is read with `!== 'false'`, so an unset key -- every
        first visit -- yields true.
        """
        text = TRANSCRIPT_JS.read_text(encoding="utf-8")
        self.assertRegex(
            text, r"toolsOpenByDefault\s*=\s*\(\)\s*=>\s*_pref\([A-Z_]+\)\s*!==\s*'false'",
            "the default must be open: an unset preference has to read as true, "
            "not as false",
        )

    def test_the_preference_is_written_back(self):
        """A preference nothing writes is dead code, and would have read as a
        working feature in any report from here on."""
        body = _build_tool_body()
        self.assertIn("addEventListener('toggle'", body)
        self.assertRegex(body, r"_setPref\(\s*TOOLS_OPEN_KEY",
                         "collapsing a tool must persist the choice")

    def test_storage_access_is_guarded(self):
        """localStorage throws outright when storage is disabled, and this
        module runs before anything catches for it. app.js:50 guards the same
        way; this file cannot import that helper because it has no imports and
        is loaded as its own module from index.html, so pulling in app.js to
        reuse four lines would drag the whole module graph with it.
        """
        text = TRANSCRIPT_JS.read_text(encoding="utf-8")
        for name in ("_pref", "_setPref"):
            with self.subTest(helper=name):
                match = re.search(rf"const {name} = .*?;\n", text, re.DOTALL)
                self.assertIsNotNone(match, f"{name} is gone")
                self.assertIn("try", match.group(0),
                              f"{name} must not let a disabled localStorage throw")


if __name__ == "__main__":
    unittest.main()
