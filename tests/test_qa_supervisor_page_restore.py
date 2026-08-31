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
SUPERVISOR_JS = REPO / "web" / "supervisor.js"
SUPERVISOR_HTML = REPO / "web" / "supervisor.html"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def run_page(html: str, budget_ms: int = 6000) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>([^<]*)</title>", result.stdout)
    return match.group(1) if match else ""


def lifted() -> str:
    """restoreOpen, rememberOpen and the key, verbatim from the page."""
    source = SUPERVISOR_JS.read_text(encoding="utf-8")
    if "function restoreOpen()" not in source:
        raise unittest.SkipTest("restoreOpen not implemented")
    start = source.index("const LAST_OPEN_KEY")
    end = source.index("function selectSupervisor(")
    return source[start:end].replace("\n  ", "\n")


class SourceWiringTests(unittest.TestCase):
    """The call has to happen where the list becomes known."""

    def setUp(self):
        self.source = SUPERVISOR_JS.read_text(encoding="utf-8")

    def test_the_list_load_restores_the_open_supervisor(self):
        """Called after renderSupervisorList, or `supervisors` is still empty."""
        block = self.source.split("renderSupervisorList();", 1)[1][:120]
        self.assertIn("restoreOpen()", block)

    def test_selecting_remembers_it(self):
        block = self.source.split("function selectSupervisor(", 1)[1][:300]
        self.assertIn("rememberOpen(id)", block)

    def test_the_message_failure_is_no_longer_silent(self):
        """`catch { chatMessages = []; }` with no re-render hid the whole thing."""
        block = self.source.split("Load chat messages", 1)[1][:900]
        catch = block.split("} catch", 1)[1][:400]
        self.assertIn("renderChatMessages()", catch,
                      "a failed load must re-render, or the panel keeps stale content")
        self.assertIn("Could not load", catch,
                      "a failed load must say so rather than showing nothing")

    def test_the_script_tag_was_cache_busted(self):
        """A cached script would mask the fix entirely."""
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        match = re.search(r"supervisor\.js\?v=(\d+)", html)
        self.assertIsNotNone(match, "supervisor.js must carry a version query")
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
