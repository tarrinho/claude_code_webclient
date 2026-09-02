"""QA coverage for the supervisor page reopening what you had open.

Pedro sent requests to a supervisor and could not see them on the page. The
requests had arrived -- six user messages in the database -- and the page simply
never asked for them.

Nothing selected a supervisor on load. ``selectSupervisor`` was reachable only
from a click on a list row, or immediately after creating one, so opening
/supervisor.html always showed the start screen. And ``showActiveSupervisor``
is what fetches the messages, so with nothing selected they were never
requested, let alone rendered. An existing conversation was invisible until you
happened to click the right row in a list whose entries were all called "New
Supervisor".

The main application has restored its last conversation from ``wc_last_chat``
since it was written; this page had no equivalent.

Also covered: the message load used to swallow its own failure with
``catch { chatMessages = []; }`` and no re-render, so a failed fetch was
indistinguishable from a supervisor that had never been asked anything.

The behaviour is driven in a browser rather than read, because "does this
select anything" is not a question source inspection answers.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR_JS = REPO / "web" / "assets" / "supervisor" / "main.js"
# `web/supervisor.js` is being split into ES modules under this directory.
# Both are read, so this harness keeps finding the code either side of the move
# instead of inspecting a file that no longer holds it.
SUPERVISOR_MODULES = REPO / "web" / "assets" / "supervisor"
SUPERVISOR_HTML = REPO / "web" / "supervisor.html"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def supervisor_source() -> str:
    """Every line of the supervisor page's script, wherever it now lives.

    Reading the single legacy path would not fail when the split lands -- it
    would find a file that still exists and no longer contains what is being
    asserted, which is the quiet version of a broken test.
    """
    parts: list[str] = []
    if SUPERVISOR_JS.is_file():
        parts.append(SUPERVISOR_JS.read_text(encoding="utf-8"))
    if SUPERVISOR_MODULES.is_dir():
        parts.extend(
            path.read_text(encoding="utf-8")
            for path in sorted(SUPERVISOR_MODULES.glob("*.js"))
        )
    if not parts:
        raise AssertionError(
            f"no supervisor script found at {SUPERVISOR_JS} or "
            f"{SUPERVISOR_MODULES}/*.js -- if it moved again, add the new "
            "location here rather than letting these tests pass on nothing"
        )
    return "\n".join(parts)


def run_page(html: str, budget_ms: int = 6000) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             # Chromium writes a ~126 MB profile per launch. Without this it
             # picks its own /tmp/org.chromium.Chromium.scoped_dir.* and
             # leaves it behind, so a single run of this file leaked 11 of
             # them and filled a 1.9 GB tmpfs -- after which every browser
             # test in the suite fails on a timeout and leaks another.
             f"--user-data-dir={page.parent}/chrome-profile",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>([^<]*)</title>", result.stdout)
    return match.group(1) if match else ""


def lifted() -> str:
    """restoreOpen, rememberOpen and the key, verbatim from the page."""
    source = supervisor_source()
    if "function restoreOpen()" not in source:
        # This used to skip, reading a missing string as "the feature is not
        # written yet". It has been written since, so the same absence now means
        # the code moved and this harness is looking in the wrong place -- and a
        # skip reports that as success. A guard stays honest only while its
        # precondition means what it meant when it was written.
        raise AssertionError(
            "restoreOpen() is not in the supervisor script. It is implemented, "
            "so this means the source moved -- point supervisor_source() at "
            "its new location"
        )
    start = source.find("const LAST_OPEN_KEY")
    end = source.find("function selectSupervisor(")
    if start < 0 or end <= start:
        # Slicing between two markers assumes both live in one file, in this
        # order. Concatenated modules need not preserve either, and an inverted
        # pair yields an empty string -- a page with no script under test, which
        # fails for a reason that names nothing.
        raise AssertionError(
            "LAST_OPEN_KEY and selectSupervisor() are no longer a contiguous "
            f"block in that order (start={start}, end={end}); lift them from "
            "their own modules instead of slicing one file"
        )
    return source[start:end].replace("\n  ", "\n")


class SourceWiringTests(unittest.TestCase):
    """The call has to happen where the list becomes known."""

    def setUp(self):
        self.source = supervisor_source()

    @staticmethod
    def _extract_block(source: str, func_name: str) -> str:
        """Return the body of *func_name*, from its opening brace to the
        matching closing brace.  Indent-aware so helper IIFEs and nested
        closures above the function do not confuse the parser.
        """
        start = source.index("function " + func_name)
        brace_start = source.index("{", start)
        depth = 1
        pos = brace_start + 1
        while depth > 0:
            ch = source[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1
        return source[brace_start + 1:pos - 1]

    def test_the_list_load_restores_the_open_supervisor(self):
        """Called after renderSupervisorList, or `supervisors` is still empty.

        Uses brace-depth parsing so top-level IIFEs (like setSupervisorSort's
        loadSavedSort) do not get mistaken for the function's own closing brace.
        """
        block = self._extract_block(self.source, "loadSupervisors")
        render = block.find("renderSupervisorList();")
        restore = block.find("restoreOpen()")
        self.assertNotEqual(render, -1, "loadSupervisors no longer renders")
        self.assertNotEqual(restore, -1, "loadSupervisors no longer restores")
        self.assertLess(render, restore,
                        "restoreOpen runs before the list exists")

    def test_selecting_remembers_it(self):
        block = self.source.split("function selectSupervisor(", 1)[1][:300]
        self.assertIn("rememberOpen(id)", block)

    def test_the_message_failure_is_no_longer_silent(self):
        """`catch { chatMessages = []; }` with no re-render hid the whole thing.
        The code now uses Promise.allSettled and re-renders on failure.
        """
        block = self.source.split("Promise.allSettled", 1)[1][:900]
        catch = block.split("} else {", 1)[1][:400]
        self.assertIn("renderChatMessages()", catch,
                      "a failed load must re-render, or the panel keeps stale content")
        self.assertIn("Could not load", catch,
                      "a failed load must say so rather than showing nothing")

    def test_the_script_tag_was_cache_busted(self):
        """A cached script would mask the fix entirely."""
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        # Matches the module path rather than the old `supervisor.js`: the
        # script moved to web/assets/supervisor/ in 0.10.0 so StaticFiles could
        # serve it. The property under test is unchanged -- the page must name
        # its script with a cache-buster -- so only the filename moved.
        match = re.search(r"supervisor/main\.js\?v=(\d+)", html)
        self.assertIsNotNone(
            match, "the supervisor module must carry a version query")
        self.assertGreaterEqual(int(match.group(1)), 4)


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class RestoreBehaviourTests(unittest.TestCase):
    """Five states the page can open in."""

    HARNESS = """
    <body><script>
    // defineProperty, not assignment: window.localStorage is read-only, so a
    // plain assignment is silently ignored and the real store is used -- which
    // made a correct case look broken while writing this. Same trap as
    // document.cookie on a file:// origin.
    const store = %(store)s;
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = v; },
      },
      configurable: true,
    });
    let activeSupervisorId = %(active)s;
    let supervisors = %(supervisors)s;
    const picked = [];
    function selectSupervisor(id) { activeSupervisorId = id; picked.push(id); rememberOpen(id); }
    %(lifted)s
    restoreOpen();
    document.title = picked.length ? picked.join(",") : "(none)";
    </script></body>
    """

    def _run(self, supervisors, remembered=None, active="null"):
        import json
        store = {"wc_last_supervisor": remembered} if remembered else {}
        return run_page(self.HARNESS % {
            "store": json.dumps(store),
            "active": active,
            "supervisors": json.dumps([{"id": s} for s in supervisors]),
            "lifted": lifted(),
        })

    def test_with_nothing_remembered_the_most_recent_opens(self):
        """An empty centre panel beside a populated list reads as broken."""
        self.assertEqual(self._run(["newest", "older"]), "newest")

    def test_the_remembered_one_reopens(self):
        """The property that answers "where did my conversation go"."""
        self.assertEqual(self._run(["newest", "older"], remembered="older"), "older")

    def test_a_remembered_one_that_was_deleted_falls_back(self):
        self.assertEqual(
            self._run(["newest", "older"], remembered="gone"), "newest")

    def test_an_empty_list_selects_nothing(self):
        """A first run has no supervisor; selecting one must not be invented."""
        self.assertEqual(self._run([]), "(none)")

    def test_an_already_open_supervisor_is_not_overridden(self):
        """The 30s list refresh calls this; it must not yank the user away."""
        self.assertEqual(
            self._run(["a", "b"], remembered="b", active='"mine"'), "(none)")


if __name__ == "__main__":
    unittest.main()
