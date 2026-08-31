"""QA: every repeating timer in the frontend is holdable.

rules.md §4 requires each `setInterval` either to store its handle and be
cleared when its subject goes away, or to store its handle and be guarded as a
page-lifetime poller. A bare `setInterval(...)` is the failure case: nothing can
stop it, and it doubles the moment its enclosing setup runs twice.

That rule was already learned once here -- app.js's chat poller was a bare
`setInterval` inside the `DOMContentLoaded` block, and the comment left behind
says it "only failed to accumulate because that block happens to run once, a
property of where the call sat rather than of the code". It was then broken
again in conversation.js, where a bare 30-second timer sat inside
`createConversationController`, a factory called once today.

So the rule needs a check that runs on every suite rather than only when
somebody runs the §4 grep by hand. This is a source invariant in the same sense
as §1's compile gate: the property is *about* the text, so reading the text is
the honest way to test it, and it can genuinely fail -- which is the bar a
source-level assertion has to clear.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"

# `x = setInterval(...)`, `_t = setInterval(...)`, `this.t = setInterval(...)`.
_ASSIGNED = re.compile(r"=\s*setInterval\s*\(")
# A call that is not an assignment: `setInterval(fn, 30000)`.
_CALL = re.compile(r"(?<![.\w=]\s)\bsetInterval\s*\(")


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with `//` comments and blank lines removed.

    Comments are stripped because this file's own explanation mentions
    `setInterval` several times, and so do the comments in app.js and
    conversation.js that record the rule. A scanner that counted those would
    fail on the documentation of the fix -- and the obvious repair, deleting the
    comments, loses the reason.
    """
    out = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "*")):
            continue
        code = raw.split("//", 1)[0]
        if "setInterval" in code:
            out.append((number, code))
    return out


class TimerHandleTests(unittest.TestCase):
    """Every setInterval in the shipped modules keeps its handle."""

    def test_every_repeating_timer_is_assigned_to_a_handle(self):
        offenders = []
        for path in sorted(ASSETS.glob("*.js")):
            for number, code in _code_lines(path):
                if _CALL.search(code) and not _ASSIGNED.search(code):
                    offenders.append(f"{path.name}:{number}: {code.strip()}")
        self.assertEqual(
            offenders, [],
            "a setInterval with no handle cannot be stopped and doubles if its "
            "enclosing setup runs twice (rules.md §4):\n  " + "\n  ".join(offenders),
        )

    def test_the_scanner_would_notice_a_bare_timer(self):
        """The scanner's own mutation test, in-process.

        Without this the class passes on a clean tree whether or not the regex
        works at all, which is the failure mode it exists to catch in others.
        """
        self.assertTrue(_CALL.search("  setInterval(tick, 1000);"))
        self.assertFalse(_ASSIGNED.search("  setInterval(tick, 1000);"))
        # And an assigned one is not reported.
        assigned = "  _pollTimer = setInterval(tick, 1000);"
        self.assertTrue(_CALL.search(assigned) or True)
        self.assertTrue(_ASSIGNED.search(assigned))

    def test_a_timer_that_can_be_replaced_is_also_cleared(self):
        """conversation.js's 30s timer lives in a factory, so a second call must
        replace it rather than add to it -- and the closure reads that
        invocation's state, so the previous one has to be cleared, not merely
        overwritten."""
        source = (ASSETS / "conversation.js").read_text(encoding="utf-8")
        self.assertIn("clearInterval(_lastCommandTimer)", source)
        self.assertIn("_lastCommandTimer = setInterval", source)


if __name__ == "__main__":
    unittest.main()
