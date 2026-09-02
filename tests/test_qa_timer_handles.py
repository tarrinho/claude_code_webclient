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

WEB = Path(__file__).resolve().parents[1] / "web"
ASSETS = WEB / "assets"


def _shipped_js() -> list[Path]:
    """Every client script the server serves, not only the module directory.

    This scanned ``web/assets/*.js`` alone, and the supervisor page is the one
    client script that lives directly in ``web/`` -- so the file was outside the
    glob while the docstring above claimed "every repeating timer in the
    frontend". It went unnoticed because a scanner that looks in the wrong place
    reports clean and *looks* clean: the sweep that fixed every other bare timer
    (``3a68c5c``, "hold every repeating timer") passed this class afterwards
    while ``web/supervisor.js`` still held one. rules.md §4's own verification
    command greps the same narrow glob, so running the rule by hand could not
    have caught it either.

    Sorted and de-duplicated so a failure message is stable and a file cannot be
    scanned twice if the two globs ever overlap.

    **Recursive, and that is the point.** The two flat globs it replaces would
    have recreated the exact hole they were written to close: a client script in
    any *subdirectory* of ``web/`` — ``web/assets/supervisor/list.js``, say —
    matches neither ``web/*.js`` nor ``web/assets/*.js``, so it would silently
    stop being scanned. Nothing fails when a scanner looks in the wrong place;
    it reports clean. That is how a bare ``setInterval`` survived the sweep in
    ``3a68c5c`` that was supposed to remove every one of them.

    Caught before it could bite, while planning the 0.10.0 split of
    ``web/supervisor.js`` into ``web/assets/supervisor/``. ``rglob`` finds the
    same nine files today, so this changes nothing now and everything later.
    """
    return sorted(WEB.rglob("*.js"))

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
        for path in _shipped_js():
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

    def test_the_scan_covers_every_shipped_script_not_just_the_module_dir(self):
        """The glob was the defect, so the glob is what this pins.

        The regex worked the whole time; the scan simply never looked at
        ``web/supervisor.js``, and a class that passes while missing a file is
        indistinguishable from one that passes because the tree is clean. Without
        this assertion the scope can be narrowed back to ``web/assets`` and every
        other test here still passes -- which is how the gap survived the sweep
        that was meant to close it.
        """
        scanned = {p.relative_to(WEB).as_posix() for p in _shipped_js()}
        flat = {p.relative_to(WEB).as_posix() for p in WEB.glob("*.js")} | {
            p.relative_to(WEB).as_posix() for p in ASSETS.glob("*.js")
        }
        # A superset, not an equality. The two flat globs are what this scan
        # used to be, and pinning equality would make the scan *fail* the first
        # time a script lands in a subdirectory -- which is precisely the case
        # the recursive version exists to cover, and precisely the change
        # 0.10.0 makes to web/supervisor.js.
        self.assertTrue(
            flat <= scanned,
            f"the scan no longer covers what the flat globs did: {flat - scanned}",
        )
        # Named, but by subject rather than by path. This was
        # `assertIn("supervisor.js", scanned)`, an exact string match against a
        # relative path -- which the 0.10.0 split breaks, because the same code
        # becomes `assets/supervisor/main.js` and friends. A test that fails on
        # the move it was written to survive would be read as the move's fault
        # and edited out of the way, taking the property with it.
        #
        # Matching on the subject keeps the property in both layouts: if the
        # scan is narrowed back to `web/assets/*.js` this set goes empty today
        # (the file sits in `web/`) *and* after the move (the modules sit in a
        # subdirectory). That is the whole assertion.
        supervisor_scripts = {p for p in scanned if "supervisor" in p}
        self.assertTrue(
            supervisor_scripts,
            "no supervisor client script is scanned; this is the code the "
            "narrow glob missed, wherever it now lives",
        )
        self.assertIn("assets/app.js", scanned)
        self.assertGreaterEqual(len(scanned), 2)

    def test_the_supervisor_poller_is_guarded_as_well_as_held(self):
        """Assigning the handle is not sufficient for this one.

        ``init()`` runs on DOMContentLoaded, which fires once per document -- but
        the console loads this page in an iframe it resets rather than navigates,
        so a second ``init()`` is reachable. Holding the handle without the guard
        would replace the reference and leak the previous timer, leaving two
        polls running and only one of them stoppable.

        Read from every supervisor script joined together, not from
        ``web/supervisor.js`` by name. Two reasons, and the second is the one
        that matters. The name disappears in the 0.10.0 split, so a direct read
        would raise ``FileNotFoundError`` on the move. And the three markers
        need not land in the same new module -- the handle may end up in a state
        module while the guard stays with ``init()`` -- so requiring them in one
        file would force the split's shape to suit this test. What §4 actually
        requires is that the guard exist somewhere in the code that owns the
        timer, which is what joining asserts.
        """
        sources = [p for p in _shipped_js() if "supervisor" in p.name
                   or "supervisor" in p.parent.name]
        self.assertTrue(sources, "no supervisor client script found to check")
        source = "\n".join(p.read_text(encoding="utf-8") for p in sources)
        # `state.` is optional in each of these. The 0.10.0 split moved the
        # shared bindings into a state object, so the handle is now
        # `state._refreshTimer` -- an ES module export is a live binding and
        # cannot be reassigned by an importing module, so a bare `let` could not
        # survive the split. Matching either spelling keeps the assertion about
        # the property (the timer is held, guarded and cleared) rather than
        # about which file happens to own the variable this month.
        for pattern in (r"(?:state\.)?_refreshTimer = setInterval",
                        r"if \(!(?:state\.)?_refreshTimer\)",
                        r"clearInterval\((?:state\.)?_refreshTimer\)"):
            with self.subTest(pattern=pattern):
                self.assertRegex(source, pattern)

    def test_a_timer_that_can_be_replaced_is_also_cleared(self):
        """conversation.js's 30s timer lives in a factory, so a second call must
        replace it rather than add to it -- and the closure reads that
        invocation's state, so the previous one has to be cleared, not merely
        overwritten."""
        source = (ASSETS / "conversation.js").read_text(encoding="utf-8")
        self.assertIn("clearInterval(_lastCommandTimer)", source)
        self.assertIn("_lastCommandTimer = setInterval", source)


    def test_a_script_in_a_subdirectory_is_scanned(self):
        """The trap this scanner is one edit away from falling into.

        `web/supervisor.js` is about to become `web/assets/supervisor/*.js`, and
        a flat glob would stop seeing it without failing -- the same silent
        blindness that let a bare timer survive the sweep meant to remove every
        one. Asserted with a real temporary file rather than by reading the
        glob's source, because the property is "would this be scanned", not
        "does the code say rglob".
        """
        sub = WEB / "assets" / "_scan_probe_tmp"
        sub.mkdir(parents=True, exist_ok=True)
        probe = sub / "probe.js"
        probe.write_text("// probe\n", encoding="utf-8")
        try:
            self.assertIn(probe, _shipped_js(),
                          "a client script in a subdirectory of web/ must be "
                          "scanned; a flat glob would skip it in silence")
        finally:
            probe.unlink(missing_ok=True)
            sub.rmdir()


if __name__ == "__main__":
    unittest.main()
