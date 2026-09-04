"""QA: renaming a chat must not read `dialogChat.id` after it goes null.

Pedro reported "Cannot read properties of null (reading 'id')" after renaming a
chat. `closeDialog()` sets `dialogChat = null`. The edit branch of
`saveChatDialog` used to call `closeDialog()` and then read `dialogChat.id`
twice on the next lines -- a null dereference, reproduced verbatim with node:

    let dialogChat = {id: 'c1'};
    function closeDialog() { dialogChat = null; }
    closeDialog();
    dialogChat.id;
    // TypeError: Cannot read properties of null (reading 'id')

The delete branch already avoided this by capturing `deletedId = dialogChat.id`
before calling `closeDialog()`. The edit branch did not follow the same
pattern, so it crashed exactly when a rename succeeded -- the one moment the
old value is still needed, right after the request that renamed it.

Fixed by capturing `editedId` before `closeDialog()`, mirroring the working
delete branch. This test reads the source rather than driving a browser,
because the defect is about *order of statements* relative to a state mutation,
which a static check can catch directly and cheaply.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "web" / "assets" / "app.js"


def _save_chat_dialog_body() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function saveChatDialog")
    brace = source.index("{", start)
    depth, pos = 1, brace + 1
    while depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


class ChatRenameNullDerefTests(unittest.TestCase):
    def test_saveChatDialog_is_still_found(self):
        """Guards the guard: if the function is renamed or moved, the body
        extraction above returns garbage and every assertion below passes on
        nothing."""
        body = _save_chat_dialog_body()
        self.assertIn("dialogMode", body)
        self.assertGreater(len(body), 200)

    def test_dialogChat_id_is_never_read_after_closeDialog_in_the_same_branch(self):
        """closeDialog() nulls dialogChat. Any `dialogChat.id` appearing later
        in the same statement sequence is the exact crash Pedro hit."""
        body = _save_chat_dialog_body()
        for match in re.finditer(r"closeDialog\(\);", body):
            after = body[match.end():]
            # Stop at the next branch/function boundary so a read belonging to
            # a different `if`/`else` arm is not mistaken for the same one.
            end = len(after)
            for boundary in ("\n    } else", "\n    }\n  } catch", "\n  } catch"):
                idx = after.find(boundary)
                if idx != -1:
                    end = min(end, idx)
            same_branch = after[:end]
            self.assertNotIn(
                "dialogChat.id", same_branch,
                "dialogChat.id read after closeDialog() nulled it -- capture "
                "the id in a local variable before calling closeDialog()",
            )

    def test_the_edit_branch_captures_editedId_before_closing(self):
        """Pins the actual fix, not just the absence of the crash pattern."""
        body = _save_chat_dialog_body()
        self.assertIn("const editedId = dialogChat.id;", body)
        capture = body.index("const editedId = dialogChat.id;")
        close = body.index("closeDialog();", capture)
        self.assertLess(
            capture, close,
            "editedId must be captured before closeDialog() nulls dialogChat",
        )


if __name__ == "__main__":
    unittest.main()
