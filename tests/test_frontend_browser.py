"""Drive the real UI in a real browser.

Everything else in the frontend suite reads app.js as text. That cannot catch a
handler that throws, a selector that matches the wrong element, or state that
fails to reach the DOM -- and all three have happened here. This starts the app
against a throwaway database and drives Chromium at it.

It found the bug it now guards: the conversation model picker offered the
global default model even when the active backend had been told not to offer
it, because populateModelPicker merged _modelOptions in unconditionally. Every
text-level assertion passed while the picker was wrong on screen.

Skipped unless playwright and a Chromium binary are both present, so a machine
without them still runs the rest of the suite.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised only without the dev deps
    sync_playwright = None


def _driver_status() -> tuple[bool, str]:
    """Whether playwright's own node driver can start, and why not if it can't.

    Importing playwright proves nothing: it shells out to a node binary it
    ships itself, and where that is missing it falls back to /usr/bin/node and
    dies at launch. Guarding on the import alone made every test here raise
    FileNotFoundError on machines without a system node instead of skipping.

    compute_driver_executable is private and returned a bare string in older
    releases, hence the isinstance check -- do not "simplify" it to one path.
    The reason is returned so the skip message says `playwright install`
    rather than something unactionable.
    """
    try:
        from playwright._impl._driver import compute_driver_executable
    except Exception as exc:  # noqa: BLE001 -- any import failure means unusable
        return False, f"playwright not importable: {exc.__class__.__name__}"
    try:
        parts = compute_driver_executable()
    except Exception as exc:  # noqa: BLE001
        return False, f"driver path unresolvable: {exc.__class__.__name__}"
    for path in (parts if isinstance(parts, (list, tuple)) else [parts]):
        if not os.path.exists(path):
            return False, f"driver missing: {path} (try: playwright install)"
    return True, "ok"


DRIVER_OK, DRIVER_WHY = _driver_status()

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
BOOT_TIMEOUT_S = 30


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _BrowserFixture(unittest.TestCase):
    """Server + browser lifecycle. No tests of its own.

    Kept separate so a second suite can reuse it: subclassing a class that has
    test methods re-runs every one of them under the new name.
    """

    @classmethod
    def setUpClass(cls):
        # Explicit try/except rather than addClassCleanup. The boot loop below
        # raises and tearDownClass does not run when setUpClass does, so the
        # server and temp dir would leak -- but addClassCleanup is the wrong
        # cure here: unittest keeps class cleanups in a list shared by every
        # TestCase, so entries registered by one class can be drained while
        # another is still running. That terminated a live server mid-class,
        # which showed up as SIGTERM (exit -15) in the middle of a passing run.
        # Releasing them here, on the one path that can leak, keeps the
        # lifetime owned by this class alone.
        cls.tmp = None
        cls.server = None
        cls.log_handle = None
        try:
            cls._start()
        except BaseException:
            cls._release()
            raise

    @classmethod
    def _start(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "projects").mkdir()
        # A home of its own. The orchestrator does not read only its database: it
        # merges the live Claude CLI sessions under ~/.claude, so a server
        # pointed at the real home reports whatever the agents on this machine
        # happen to be doing. The alert tests assert on a *rise* in the waiting
        # count, and an unrelated session answering a question in the same poll
        # window cancels the rise -- which is how a correct notification test
        # spent 90 seconds waiting for a notification that had already been
        # netted out. This also keeps the suite from reading the developer's
        # own transcripts.
        home = tmp / "home"
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "projects").mkdir(parents=True)
        cls.port = _free_port()
        cls.password = secrets.token_urlsafe(12)
        env = {
            **os.environ,
            "HOME": str(home),
            # Its own application log. logging.conf names an absolute path, so
            # without this every server started here appends to the production
            # log -- which is how that file came to hold interleaved and
            # future-stamped lines from tests nobody was watching. Inside tmp,
            # which already exists: RotatingFileHandler will not create a
            # missing directory and the server would exit during setUpClass.
            "WC_LOG_FILE": str(tmp / "app.log"),
            # Defaults to the home above, which the projects root is not under.
            "WC_PROJECTS_ROOT_BASE": str(tmp),
            "WC_DB_PATH": str(tmp / "wc.db"),
            "WC_PROJECTS_ROOT": str(tmp / "projects"),
            "WC_SESSION_SECRET": secrets.token_urlsafe(32),
            "WC_ADMIN_PASSWORD": cls.password,
            # No proxy: this exercises the UI, never a real turn.
            "WC_PROXY_ENABLED": "0",
            # The test server is plain HTTP on loopback.
            "WC_COOKIE_ALLOW_INSECURE": "1",
        }
        # A file, not a PIPE. Nothing here ever reads the server's output, and
        # an unread pipe holds only 64K -- past that uvicorn blocks forever on
        # its own access log and stops answering, which surfaces as a browser
        # test failing on a server that looks alive. A file also survives the
        # process, so _server_log below can say what happened.
        cls.log_path = tmp / "server.log"
        cls.log_handle = cls.log_path.open("wb")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, env=env,
            stdout=cls.log_handle, stderr=subprocess.STDOUT,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + BOOT_TIMEOUT_S
        while time.time() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError(f"server exited during boot:\n{cls._server_log()}")
            try:
                urllib.request.urlopen(f"{cls.base}/login", timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        else:  # pragma: no cover - boot failure path
            raise RuntimeError("server did not start")

    @classmethod
    def _server_log(cls, lines: int = 40) -> str:
        try:
            if cls.log_handle is not None:
                cls.log_handle.flush()
        except (ValueError, OSError):  # already closed by _release
            pass
        try:
            tail = cls.log_path.read_text(errors="replace").splitlines()[-lines:]
        except OSError as exc:
            return f"<no server log: {exc}>"
        return "\n".join(tail) or "<server log empty>"

    @classmethod
    def tearDownClass(cls):
        cls._release()

    @classmethod
    def _release(cls):
        """Give back whatever setUpClass actually got. Idempotent, and safe on
        a partly-built class: it runs both from tearDownClass and from the
        failure path in setUpClass, where any of these may still be None."""
        if cls.server is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                cls.server.kill()
            cls.server = None
        if cls.log_handle is not None:
            cls.log_handle.close()
            cls.log_handle = None
        if cls.tmp is not None:
            cls.tmp.cleanup()
            cls.tmp = None

    def setUp(self):
        # addCleanup, not tearDown: unittest skips tearDown when setUp raises,
        # and _login below raises on any slow boot. A leaked playwright is not
        # merely untidy -- its greenlet loop stays flagged as the *running*
        # asyncio loop for this thread, so every IsolatedAsyncioTestCase that
        # follows dies on "Runner.run() cannot be called from a running event
        # loop". One login timeout took out 850 unrelated tests that way.
        # Cleanups run last-registered-first, hence stop() before close().
        # Say so plainly when the server has died. Otherwise every remaining
        # test in the class fails with ERR_CONNECTION_REFUSED, which names the
        # port and nothing else.
        if self.server.poll() is not None:
            self.fail(
                f"the test server exited with code {self.server.returncode}:\n"
                f"{self._server_log()}"
            )
        self.errors: list[str] = []
        self._pw = sync_playwright().start()
        self.addCleanup(self._pw.stop)
        self.browser = self._pw.chromium.launch(
            executable_path=CHROMIUM, args=["--no-sandbox"]
        )
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error"
            else None,
        )
        self._login()

    def _login(self):
        page = self.page
        # domcontentloaded, not networkidle: this app polls the orchestrator, the
        # chat list and pending questions, and the orchestrator pane holds an SSE
        # stream open -- so the network is never idle and that wait can only
        # ever time out. The app-shell wait below is the real readiness signal.
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        # By id: the theme toggle is also type=submit and comes first in the DOM.
        page.click("#submitBtn")
        # networkidle resolves before the module renders the toolbar, and the
        # login page has its own template -- a query here would silently run
        # against the wrong document.
        page.wait_for_selector("#settingsBtn", timeout=15_000)

    def _open_backends(self):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=10_000)
        self.page.wait_for_timeout(800)

    def _picker(self) -> list[str]:
        return self.page.eval_on_selector_all(
            "#conversationModel option", "els => els.map(e => e.value)"
        )

    def _model_rows(self) -> dict[str, bool]:
        return {
            row.query_selector(".model-item-id").inner_text():
                row.query_selector("input[type=checkbox]").is_checked()
            for row in self.page.query_selector_all(".machine-models .model-item")
        }

    def _activate_if_needed(self):
        button = self.page.query_selector(
            ".machine-actions button:has-text('Activate')"
        )
        if button:
            button.click()
            self.page.wait_for_timeout(1200)

    def _reset_to_offer_all(self):
        """Tick every model, which the UI stores as "offer everything".

        One server and one database serve the whole class, so a selection made
        by an earlier test would otherwise decide what a later one sees. Doing
        it through the checkboxes rather than the API keeps the reset on the
        same path the tests exercise.
        """
        for _ in range(10):
            unticked = [
                row for row in self.page.query_selector_all(".machine-models .model-item")
                if not row.query_selector("input[type=checkbox]").is_checked()
            ]
            if not unticked:
                return
            unticked[0].query_selector("input[type=checkbox]").click()
            self.page.wait_for_timeout(900)
        self.fail("could not restore every model to offered")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class BackendsPanelBrowserTests(_BrowserFixture):
    """The Backends tab, exercised the way a person exercises it."""

    def test_settings_opens_without_script_errors(self):
        self._open_backends()
        self.assertTrue(self.page.is_visible("#panelBackends"))
        self.assertEqual(self.errors, [])

    def test_tabs_are_the_merged_set(self):
        self._open_backends()
        tabs = [t.inner_text() for t in self.page.query_selector_all(".settings-tab")]
        self.assertEqual(
            tabs, ["Backends", "Usage", "Statistics", "Server", "Skills", "App"]
        )
        # The old standalone Models tab is what got merged into Backends; a
        # backend and the models it serves are one thing.
        self.assertIsNone(self.page.query_selector("#panelModels"))

    def test_seeded_backend_renders_with_its_models(self):
        self._open_backends()
        cards = self.page.query_selector_all(".machine-card")
        self.assertEqual(len(cards), 1, "a fresh account gets the Anthropic entry")
        self.assertTrue(self._model_rows(), "the card lists models")

    def test_status_line_says_the_list_is_a_fallback(self):
        """Without an API key the endpoint cannot be asked, and the UI must say
        so rather than presenting built-in ids as the real list."""
        self._open_backends()
        status = self.page.query_selector(".models-status").inner_text()
        self.assertIn("built-in", status.lower())

    # ── the feature itself ───────────────────────────────────────────────

    def test_every_model_offered_when_none_are_singled_out(self):
        """An empty active list means all served models are offered.

        That is what a machine starts with, and what the UI stores when every
        box is ticked -- the feature is opt-in and cannot leave an empty picker.
        """
        self._open_backends()
        self._activate_if_needed()
        self._reset_to_offer_all()
        rows = self._model_rows()
        self.assertTrue(rows)
        self.assertTrue(all(rows.values()))
        for model in rows:
            self.assertIn(model, self._picker())

    def test_deactivating_a_model_removes_it_from_the_picker(self):
        """The regression this file was written for.

        populateModelPicker merged the global default in unconditionally, so a
        model the backend had been told not to offer stayed selectable. Text
        assertions could not see it; the browser could.
        """
        self._open_backends()
        self._activate_if_needed()
        self._reset_to_offer_all()
        rows = self.page.query_selector_all(".machine-models .model-item")
        target = rows[1].query_selector(".model-item-id").inner_text()
        self.assertIn(target, self._picker(), "precondition: it starts offered")

        rows[1].query_selector("input[type=checkbox]").click()
        self.page.wait_for_timeout(1500)

        self.assertNotIn(target, self._picker())
        self.assertFalse(self._model_rows()[target])
        # The others must survive: unticking one must not clear the rest.
        still_offered = [m for m, on in self._model_rows().items() if on]
        self.assertTrue(still_offered)
        for model in still_offered:
            self.assertIn(model, self._picker())
        self.assertEqual(self.errors, [])

    def test_selection_survives_a_reload(self):
        self._open_backends()
        self._activate_if_needed()
        self._reset_to_offer_all()
        rows = self.page.query_selector_all(".machine-models .model-item")
        target = rows[1].query_selector(".model-item-id").inner_text()
        rows[1].query_selector("input[type=checkbox]").click()
        self.page.wait_for_timeout(1500)

        self.page.reload(wait_until="domcontentloaded")
        self._open_backends()
        self.assertFalse(self._model_rows()[target])

    def test_refresh_is_not_one_of_the_card_actions(self):
        """It sits in the models toolbar. Sharing .machine-actions made it the
        first action button in the card, so "the first action" found Refresh
        instead of Activate."""
        self._open_backends()
        actions = [
            b.inner_text()
            for b in self.page.query_selector_all(".machine-actions button")
        ]
        self.assertNotIn("Refresh", actions)
        self.assertTrue(self.page.query_selector(".models-refresh"))


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class BackendsTurnCountBrowserTests(_BrowserFixture):
    """The Turns column in the Backends model grid.

    Reported: it always read "never", for every model, no matter how much
    traffic actually ran. loadTurnCounts() -- app.js, populates _turnsByModel
    from GET /api/usage -- called a helper, _bareModel(), that exists only in
    machines.js and was never imported into app.js. The ReferenceError this
    threw on the first loop iteration was swallowed by loadTurnCounts()'s own
    catch block, which resets _turnsByModel to an empty Map on any failure --
    so the fetch always succeeded, the response always carried the right
    numbers, and the column always showed "never" anyway, with nothing in the
    console to say why.

    Own class rather than added to BackendsPanelBrowserTests: this seeds real
    usage_events rows directly into the class's shared database once, in
    setUpClass, and every other Backends test in that class asserts against
    the traffic-free state a fresh account starts in. Sharing a class would
    make this test's fixture data leak into theirs.
    """

    #: Matches the built-in fallback list every fresh Anthropic machine offers
    #: (see BackendsPanelBrowserTests.test_status_line_says_the_list_is_a_fallback).
    SEEDED_MODEL = "claude-opus-5"
    SEEDED_TURNS = 5

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sqlite3
        con = sqlite3.connect(str(Path(cls.tmp.name) / "wc.db"))
        try:
            chat_id = f"turns-{secrets.token_hex(4)}"
            con.execute(
                "INSERT INTO chats (id, title, description, work_dir, "
                "owner_id, created_at, updated_at) VALUES (?,?,NULL,'/tmp',"
                "'admin','2026-09-01T08:00:00Z','2026-09-01T08:00:00Z')",
                (chat_id, f"Turns fixture {chat_id}"),
            )
            for _ in range(cls.SEEDED_TURNS):
                con.execute(
                    "INSERT INTO usage_events (chat_id, owner_id, model, "
                    "provider, created_at) VALUES (?, 'admin', ?, 'anthropic', "
                    "'2026-09-02T09:00:00Z')",
                    (chat_id, cls.SEEDED_MODEL),
                )
            con.commit()
        finally:
            con.close()

    def _turns_column(self) -> dict[str, str]:
        """{model id -> the Turns cell's text}, in the order rendered."""
        rows = self.page.query_selector_all(".machine-models .model-item")
        return {
            row.query_selector(".model-item-id").inner_text():
                row.query_selector(".model-turns").inner_text()
            for row in rows
        }

    def test_a_model_with_recorded_turns_shows_its_count(self):
        self._open_backends()
        turns = self._turns_column()
        self.assertIn(self.SEEDED_MODEL, turns, "precondition: the seeded model is listed")
        self.assertEqual(
            turns[self.SEEDED_MODEL], str(self.SEEDED_TURNS),
            "the fixture recorded 5 real usage_events rows for this model, "
            "so the column showing anything else -- especially the "
            "no-traffic placeholder -- means loadTurnCounts() failed and was "
            "silently swallowed",
        )
        self.assertEqual(self.errors, [])

    def test_a_model_with_no_turns_still_reads_never(self):
        """The other half: a real zero must still say so in words, not '0'."""
        self._open_backends()
        turns = self._turns_column()
        untouched = [m for m in turns if m != self.SEEDED_MODEL]
        self.assertTrue(untouched, "precondition: more than one model is offered")
        for model in untouched:
            self.assertEqual(turns[model], "never")

    def test_the_seeded_models_bar_is_the_longest(self):
        """Traffic is rendered as a bar scaled against the busiest model
        (_peakTurns in machines.js). With one model actually used, its bar
        must be the only one with any width -- a bug that always used the
        model's OWN count as the peak (rather than the max across all of
        them) would make every non-zero bar read as 100% regardless of how
        it compares to the others, which text alone cannot catch."""
        import re
        self._open_backends()
        rows = self.page.query_selector_all(".machine-models .model-item")
        widths = {}
        for row in rows:
            model_id = row.query_selector(".model-item-id").inner_text()
            fill = row.query_selector(".model-bar i")
            style = fill.get_attribute("style") or ""
            widths[model_id] = float(re.search(r"([\d.]+)%", style).group(1)) if "%" in style else 0.0
        self.assertEqual(
            widths[self.SEEDED_MODEL], max(widths.values()),
            f"widths were {widths}",
        )
        untouched_widths = [w for m, w in widths.items() if m != self.SEEDED_MODEL]
        self.assertTrue(all(w == 0 for w in untouched_widths))


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorBrowserTests(_BrowserFixture):
    """The orchestrator section, driven the way the user drives it.

    Inherits the server/browser fixture. The seeded conversation ends on an
    assistant reply that was never read, so it must appear as waiting.
    """

    # Scoped to the desktop list: the markup renders two sidebars, and the
    # mobile one is hidden -- an unscoped selector finds its rows first and
    # every click times out on an invisible element.
    DESKTOP = "#chatListDesktop"

    def _seed_waiting_chat(self):
        """Insert a conversation ending on an unread assistant reply.

        A fresh id and a current timestamp per test: the class shares one
        database, so a fixed id would inherit the read mark left by whichever
        test ran first and the row would never be waiting.
        """
        import datetime
        import sqlite3
        now = datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.chat_id = f"sup-{secrets.token_hex(4)}"
        self.chat_title = f"Waiting {self.chat_id}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (self.chat_id, self.chat_title, stamp, stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (self.chat_id, "user", "do it", stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (self.chat_id, "assistant", "Which way do you want it?", stamp),
        )
        con.commit()
        con.close()

    def _supervisor_rows(self):
        return self.page.query_selector_all(f"{self.DESKTOP} .orchestrator-item")

    def _badge(self):
        node = self.page.query_selector(f"{self.DESKTOP} .orchestrator-badge")
        return int(node.inner_text()) if node else 0

    def _load(self):
        self._seed_waiting_chat()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .orchestrator-item", timeout=15_000)

    def test_supervisor_sits_above_the_other_sections(self):
        self._load()
        labels = [
            e.inner_text().split("·")[0].split("\n")[0].strip()
            for e in self.page.query_selector_all(f"{self.DESKTOP} .chat-section-label")
        ]
        self.assertTrue(labels, "the sidebar rendered no sections")
        self.assertEqual(labels[0].lower(), "orchestrator")
        self.assertEqual(self.errors, [])

    def test_badge_counts_the_waiting_agents(self):
        self._load()
        self.assertGreaterEqual(self._badge(), 1)
        self.assertEqual(self._badge(), len(self._supervisor_rows()))

    def test_a_waiting_row_shows_what_the_agent_last_said(self):
        self._load()
        row = next(
            r for r in self._supervisor_rows()
            if self.chat_title in r.query_selector(".chat-title").inner_text()
        )
        self.assertIn("web", row.query_selector(".chat-meta").inner_text())
        self.assertEqual(
            row.query_selector(".chat-snippet").inner_text().strip(),
            "Which way do you want it?",
        )

    def test_jumping_opens_the_conversation(self):
        """Get me there in one tap.

        The badge deliberately does NOT clear here: opening a conversation is
        not answering its question, and a highlight that vanished on a glance
        was the complaint that changed this rule.
        """
        self._load()
        before = self._badge()
        row = next(
            r for r in self._supervisor_rows()
            if self.chat_title in r.query_selector(".chat-title").inner_text()
        )
        row.query_selector(".chat-open").click()
        self.page.wait_for_timeout(3500)

        # The conversation name lives in the workspace strip, not the topbar.
        self.assertEqual(self.page.query_selector("#workspaceName").inner_text(), self.chat_title)
        self.assertIn("Which way do you want it?", self.page.query_selector("#messagesArea").inner_text())
        self.assertEqual(
            self._badge(), before,
            "an unanswered question must survive being looked at",
        )
        self.assertEqual(self.errors, [])

    def test_the_heading_offers_a_link_to_the_supervisor(self):
        """Present, labelled, and pointing at the orchestrator page."""
        self._load()
        heading = self.page.query_selector(f"{self.DESKTOP} .orchestrator-label")
        self.assertIsNotNone(heading, "the Orchestrator section did not render")
        link = heading.query_selector(".orchestrator-open")
        self.assertIsNotNone(link, "no link to the orchestrator in its own section")
        self.assertEqual(link.get_attribute("aria-label"), "Open the orchestrator")

    def test_clearing_leaves_the_link_reachable(self):
        """Nothing waiting is exactly when you want to go and look."""
        self._load()
        clear = self.page.query_selector(f"{self.DESKTOP} .orchestrator-clear")
        if clear:
            clear.click()
            self.page.wait_for_timeout(2000)
        self.assertIsNotNone(
            self.page.query_selector(f"{self.DESKTOP} .orchestrator-open"),
            "the link vanished once the queue emptied",
        )

    def _pane(self):
        return self.page.query_selector("#supervisorPane")

    def test_the_topbar_control_opens_the_pane_not_a_new_page(self):
        """It used to navigate away, which cost the sidebar and a reload.

        Scoped to the pane deliberately: what loads *inside* the frame is the
        orchestrator's own page, with its own engine and SSE stream. Driving that
        from here made this suite depend on another subsystem's behaviour and
        destabilised every test after it.
        """
        self._load()
        before = self.page.url
        self.page.click("#supervisorBtn")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)
        self.assertEqual(self.page.url, before, "it navigated instead of embedding")
        self.assertFalse(self.page.is_visible("#messagesWrap"),
                         "the conversation area is still showing behind it")
        self.assertIn(
            "orchestrator.html",
            self.page.query_selector("#supervisorFrame").get_attribute("src"),
        )

    def test_the_sidebar_link_opens_the_same_pane(self):
        self._load()
        self.page.click(f"{self.DESKTOP} .orchestrator-open")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)

    def test_the_conversation_list_stays_visible_beside_it(self):
        """The reason for embedding: you can still see who is waiting."""
        self._load()
        self.page.click("#supervisorBtn")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)
        self.assertTrue(self.page.is_visible(self.DESKTOP),
                        "the sidebar went away, which defeats the point")

    def test_closing_returns_to_the_conversation(self):
        self._load()
        self.page.click("#supervisorBtn")
        self.page.wait_for_selector("#supervisorPane:not([hidden])", timeout=10_000)
        self.page.click("#supervisorPaneClose")
        # state="hidden": the default waits for *visible*, so asserting on a
        # hidden element that way can only ever time out -- the element was
        # correctly hidden the whole time.
        self.page.wait_for_selector("#supervisorPane", state="hidden", timeout=10_000)
        self.assertTrue(self.page.is_visible("#messagesWrap"))

    def test_supervisor_rows_are_not_draggable(self):
        """They point at conversations owned by other sections; a drop here
        would ask the reorder handler to reorder a container it does not own."""
        self._load()
        for row in self._supervisor_rows():
            self.assertNotEqual(row.get_attribute("draggable"), "true")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class DeviceAlertBrowserTests(_BrowserFixture):
    """Alerting the device, which is the point of viewing this on a phone.

    Three levels: the tab title (always), a system notification (permission),
    and a vibration (Android). Only the title is testable without granting
    permission, so this grants it and captures the Notification constructor.
    """

    DESKTOP = "#chatListDesktop"

    def _seed(self, role="assistant", text="Which way do you want it?"):
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"al-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,created_at,"
            "updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Alert {chat_id}", stamp, stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, role, text, stamp),
        )
        con.commit()
        con.close()
        return chat_id

    def test_tab_title_carries_the_count_without_any_permission(self):
        """The one level that always works, on any device, with no prompt."""
        self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .orchestrator-item", timeout=15_000)
        self.assertRegex(self.page.title(), r"^\(\d+\) WebConsole$")

    def _answer(self, chat_id):
        """Reply as the user, which is what actually retires a question."""
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, "user", "the second one", stamp),
        )
        con.commit()
        con.close()

    def test_the_count_survives_merely_looking_at_it(self):
        """Opening a conversation is not answering its question.

        Clearing on read let an unanswered question be dismissed by glancing
        at it, which is the opposite of what the badge is for.
        """
        chat_id = self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .orchestrator-item", timeout=15_000)
        before = self.page.title()
        self.assertRegex(before, r"^\(\d+\) WebConsole$")

        row = next(
            r for r in self.page.query_selector_all(f"{self.DESKTOP} .orchestrator-item")
            if chat_id[-8:] in r.query_selector(".chat-title").inner_text()
            or "Alert" in r.query_selector(".chat-title").inner_text()
        )
        row.query_selector(".chat-open").click()
        self.page.wait_for_timeout(3000)
        self.assertRegex(self.page.title(), r"^\(\d+\) WebConsole$",
                         "the badge cleared just from opening the conversation")

    def test_the_count_clears_once_the_question_is_answered(self):
        chat_id = self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .orchestrator-item", timeout=15_000)
        self.assertRegex(self.page.title(), r"^\(\d+\) WebConsole$")

        before = int(self.page.title().split(")")[0].lstrip("("))
        self._answer(chat_id)
        # The class shares a database, so other tests' unanswered questions are
        # legitimately still waiting: assert this one left, not that the queue
        # emptied. Waits out a poll rather than assuming an instant refresh.
        self.page.wait_for_function(
            "(n) => { const m = /^\\((\\d+)\\)/.exec(document.title);"
            "  return m ? Number(m[1]) < n : true; }",
            arg=before, timeout=40_000,
        )

    def test_the_alert_toggle_is_offered(self):
        self.assertFalse(self.page.query_selector("#alertToggle").is_hidden())

    def test_a_notification_fires_for_a_new_waiting_agent(self):
        """Granting permission and capturing the constructor, so this asserts a
        notification was really raised rather than that the code looks right."""
        self.browser.contexts[0].grant_permissions(["notifications"])
        # An alert fires on a *rise* in the waiting count, so the first poll has
        # to have landed before the seed below -- otherwise the seed becomes the
        # baseline and no rise ever happens. Waiting a fixed beat cannot detect
        # that: with an empty queue the title reads "WebConsole" both before the
        # poll and after it. Seeding one question first makes the baseline
        # visible, so the count in the title is proof the poll ran.
        # A real question: the classifier counts only those, which is exactly
        # what test_routine_output_raises_no_alert_at_all pins from the other
        # side. A statement here would never reach the badge at all.
        self._seed(text="Shall I start with the first one?")
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_function(
            "() => /^\\(\\d+\\)/.test(document.title)", timeout=40_000
        )
        # Record every Notification the page constructs.
        self.page.evaluate("""() => {
            window.__notes = [];
            const Real = window.Notification;
            window.Notification = function (title, opts) {
                window.__notes.push({title, body: (opts || {}).body});
            };
            window.Notification.permission = 'granted';
            window.Notification.requestPermission = () => Promise.resolve('granted');
        }""")
        self.page.evaluate("() => localStorage.setItem('wc_alerts', 'on')")
        # The page must not be the focused thing, or alerting would be noise.
        self.page.evaluate("() => Object.defineProperty(document, 'hasFocus', {value: () => false})")
        self._seed()
        # The orchestrator polls every 15s and three browser suites contend for
        # this machine, so allow several cycles rather than assuming the first
        # one lands promptly.
        self.page.wait_for_function(
            "() => window.__notes && window.__notes.length > 0", timeout=90_000
        )
        notes = self.page.evaluate("() => window.__notes")
        self.assertTrue(notes)
        self.assertEqual(notes[0]["title"], "WebConsole")
        self.assertIn("needs an answer", notes[0]["body"])

    def test_no_notification_while_the_page_is_in_front_of_you(self):
        """Being looked at already counts as being told."""
        self.browser.contexts[0].grant_permissions(["notifications"])
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.evaluate("""() => {
            window.__notes = [];
            window.Notification = function (t, o) { window.__notes.push({t, o}); };
            window.Notification.permission = 'granted';
        }""")
        self.page.evaluate("() => localStorage.setItem('wc_alerts', 'on')")
        self.page.evaluate("() => Object.defineProperty(document, 'hasFocus', {value: () => true})")
        self._seed()
        self.page.wait_for_timeout(20_000)
        self.assertEqual(self.page.evaluate("() => window.__notes.length"), 0)

    def test_routine_output_raises_no_desktop_notification(self):
        """Pedro's rule: only when information is required or important.

        Split from a single case that also asserted the tab title did not move.
        The two are different instruments and the rule change separated them:

        * A **desktop notification** interrupts someone who is not looking at
          this machine, so it fires only for a row that needs a person -- an
          ask, a blocker, a failure. "Done. Suite is green" raising one on an
          unfocused laptop is the case where surfacing completions earns least
          and costs most.
        * The **tab title** is ambient. Under the new rule an ended action is
          worth surfacing, and the title is the mildest way to do it, so a
          completion does move it. That half now has its own case below.

        Found by cweb2 at the browser level after I had put the same tension to
        Pedro as a list-of-rows question; an OS notification is where it stops
        being arguable.
        """
        self.browser.contexts[0].grant_permissions(["notifications"])
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.evaluate("""() => {
            window.__notes = [];
            window.Notification = function (t, o) { window.__notes.push({t, o}); };
            window.Notification.permission = 'granted';
        }""")
        self.page.evaluate("() => localStorage.setItem('wc_alerts', 'on')")
        self.page.evaluate("() => Object.defineProperty(document, 'hasFocus', {value: () => false})")
        # Wait out the first orchestrator poll before reading the baseline. Taken
        # straight after domcontentloaded it is the static title from the HTML,
        # so the poll landing -- not this test's seed -- moved the count, and
        # the comparison below failed whenever an earlier test in the class had
        # left a question waiting.
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_timeout(1500)
        before = self.page.title()
        self._seed(text="Done. Suite is green, ruff clean.")
        self.page.wait_for_timeout(20_000)
        self.assertEqual(
            self.page.evaluate("() => window.__notes.length"), 0,
            "a routine completion interrupted a machine nobody was looking at",
        )
        # The title is asserted in its own case below, not here: it now moves,
        # and asserting both properties in one test is what made a deliberate
        # product change look like a regression.
        self.assertNotEqual(before, None)

    def test_routine_output_does_reach_the_tab_title(self):
        """The other half: an ended action is surfaced, ambiently.

        Guards the notification case above from becoming vacuous. If completions
        stopped being surfaced at all, that test would still pass -- zero
        notifications is also what "nothing happened" looks like -- so something
        has to assert the row arrived.
        """
        self.page.goto(f"{self.base}/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_timeout(1500)
        before = self.page.title()
        self._seed(text="Done. Suite is green, ruff clean.")
        self.page.wait_for_timeout(20_000)
        self.assertNotEqual(
            self.page.title(), before,
            "a finished agent was not surfaced anywhere, so the promotion is inert",
        )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class ServerPanelBrowserTests(_BrowserFixture):
    """The Server tab refreshes itself, and stops when you leave it.

    Asserted by counting the requests the page actually makes, because the
    thing that was wrong was not visible in the markup: the panel rendered
    correctly and then never changed, so a reading from when the tab was opened
    sat there looking current. The poll is 30s, so these wait a cycle out.
    """

    POLL_S = 30

    def setUp(self):
        super().setUp()
        self.system_calls: list[str] = []
        self.page.on(
            "request",
            lambda r: self.system_calls.append(r.url)
            if "/api/system" in r.url
            else None,
        )

    def _open_server_tab(self):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector("#tabServer", timeout=10_000)
        self.page.click("#tabServer")
        self.page.wait_for_selector("#panelServer:not([hidden])", timeout=10_000)

    def test_opening_the_tab_reads_the_host(self):
        self._open_server_tab()
        self.page.wait_for_function(
            "() => document.querySelector('#serverBody .srv-cards')", timeout=15_000
        )
        self.assertTrue(self.system_calls, "the panel never asked for host stats")

    def test_the_reading_refreshes_without_being_asked(self):
        self._open_server_tab()
        self.page.wait_for_timeout(2000)
        before = len(self.system_calls)
        self.assertGreater(before, 0)
        self.page.wait_for_timeout((self.POLL_S + 8) * 1000)
        self.assertGreater(
            len(self.system_calls), before,
            "the panel made no further request: it is frozen at whatever it "
            "read when the tab was opened",
        )

    def test_a_refresh_leaves_the_reading_on_screen(self):
        """No skeleton flash: a panel that updates must not look like it broke."""
        self._open_server_tab()
        self.page.wait_for_function(
            "() => document.querySelector('#serverBody .srv-cards')", timeout=15_000
        )
        self.page.wait_for_timeout((self.POLL_S + 8) * 1000)
        self.assertIsNotNone(self.page.query_selector("#serverBody .srv-cards"))
        self.assertIsNone(self.page.query_selector("#serverBody .skill-skeleton"))

    def test_leaving_the_tab_stops_the_polling(self):
        """Otherwise every settings visit leaves another interval behind."""
        self._open_server_tab()
        self.page.wait_for_timeout(2000)
        self.page.click("#tabBackends")
        self.page.wait_for_selector("#panelBackends:not([hidden])", timeout=10_000)
        self.page.wait_for_timeout(1500)
        settled = len(self.system_calls)
        self.page.wait_for_timeout((self.POLL_S + 8) * 1000)
        self.assertEqual(len(self.system_calls), settled,
                         "the panel kept polling after it was closed")

    def test_the_controls_default_to_a_day_in_half_hours(self):
        """The 24h view exists to show evolution; by-day would be one bar."""
        self._open_server_tab()
        self.assertEqual(self.page.input_value("#serverRange"), "1")
        self.assertEqual(self.page.input_value("#serverBucket"), "halfhour")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorStreamBrowserTests(_BrowserFixture):
    """Switching supervisors must close the stream it leaves behind.

    connectSSE() used to construct `new EventSource(url, {signal})` and tear
    the previous stream down with `AbortController.abort()`. EventSource's init
    dictionary accepts only `withCredentials`; a `signal` member is ignored, so
    the teardown was inert -- every switch left a stream open on both ends and
    the stale one kept delivering into handleSSEEvent for an orchestrator the user
    had left.

    Nothing about that is visible in the source: the code reads as if it tears
    down. readyState is the only thing that settles it, which is why this test
    drives a browser rather than reading the file.
    """

    def _make_supervisor(self, title: str) -> str:
        csrf = next(c["value"] for c in self.page.context.cookies()
                    if c["name"] == "wc_csrf")
        return self.page.evaluate("""async ([csrf, title]) => {
            const r = await fetch('/api/supervisors', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                body: JSON.stringify({title})});
            return (await r.json()).id;
        }""", [csrf, title])

    def _open_page_with_two(self):
        self._make_supervisor("First orchestrator")
        self._make_supervisor("Second orchestrator")
        self.page.goto(f"{self.base}/orchestrator", wait_until="domcontentloaded")
        self.page.wait_for_selector(".orchestrator-list-item", timeout=15_000)
        rows = self.page.locator(".orchestrator-list-item")
        # A locator, not element handles: the list re-renders on every refresh
        # and on selection, which detaches any handle taken beforehand.
        self.assertGreaterEqual(rows.count(), 2, "both supervisors should be listed")
        return rows

    def test_switching_closes_the_previous_stream(self):
        """readyState 2 is CLOSED. It stayed at 1 with the AbortController."""
        rows = self._open_page_with_two()
        rows.nth(0).click()
        self.page.wait_for_function(
            "() => window._supervisorSSE", timeout=10_000
        )
        self.page.evaluate("() => { window.__first = window._supervisorSSE; }")

        rows.nth(1).click()
        self.page.wait_for_function(
            "() => window._supervisorSSE && window._supervisorSSE !== window.__first",
            timeout=10_000,
        )
        first_state = self.page.evaluate("() => window.__first.readyState")
        self.assertEqual(
            first_state, 2,
            "the stream for the orchestrator we left is still open: every switch "
            "leaks a connection and keeps delivering its events",
        )

    def test_the_new_stream_is_the_live_one(self):
        rows = self._open_page_with_two()
        rows.nth(0).click()
        self.page.wait_for_function("() => window._supervisorSSE", timeout=10_000)
        rows.nth(1).click()
        self.page.wait_for_timeout(1500)
        self.assertIn(
            self.page.evaluate("() => window._supervisorSSE.readyState"), (0, 1),
            "the current orchestrator has no live stream",
        )

    def test_the_list_refreshes_with_nothing_selected(self):
        """The 30s refresh used to be gated on having a selection.

        That is the state the page opens in, so an orchestrator created anywhere
        else never appeared until a manual reload -- and staleness you cannot
        see is worse than a list that never claims to be current.
        """
        source = (ROOT / "web" / "assets" / "orchestrator" / "main.js").read_text(encoding="utf-8")
        start = source.index("setInterval(")
        body = source[start:source.index("30000", start)]
        self.assertIn("loadSupervisors()", body)
        guard = body.find("if (activeSupervisorId)")
        self.assertTrue(
            guard == -1 or body.index("loadSupervisors()") < guard,
            "loadSupervisors() is inside the has-a-selection guard again",
        )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorDismissBrowserTests(_BrowserFixture):
    """Every highlighted agent can be taken out of the highlights on its own.

    The heading's clear-all was the only control: dealing with one agent meant
    silencing every other, including questions still unanswered. These drive
    the button rather than reading the markup, because what matters is that the
    row goes away and the others stay.
    """

    DESKTOP = "#chatListDesktop"

    def _seed(self, text="Which way do you want it?"):
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"dis{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,created_at,"
            "updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Waiting {chat_id}", stamp, stamp),
        )
        con.execute(
            "INSERT INTO messages (chat_id,role,content,created_at) VALUES (?,?,?,?)",
            (chat_id, "assistant", text, stamp),
        )
        con.commit()
        con.close()
        return chat_id

    def _rows(self):
        return self.page.locator(f"{self.DESKTOP} .orchestrator-item")

    def _load_with(self, count):
        for _ in range(count):
            self._seed()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .orchestrator-item", timeout=15_000)
        return self._rows()

    def test_every_highlighted_row_offers_one(self):
        rows = self._load_with(2)
        self.assertGreaterEqual(rows.count(), 2)
        for index in range(rows.count()):
            with self.subTest(row=index):
                self.assertEqual(
                    rows.nth(index).locator(".orchestrator-dismiss").count(), 1,
                    "a highlighted agent with no way to dismiss it on its own",
                )

    def test_it_is_labelled_for_a_screen_reader(self):
        """"✕" alone announces as nothing useful."""
        rows = self._load_with(1)
        label = rows.nth(0).locator(".orchestrator-dismiss").get_attribute("aria-label")
        self.assertIn("highlights", (label or "").lower())

    def test_clicking_it_removes_that_row(self):
        rows = self._load_with(2)
        before = rows.count()
        self.assertGreaterEqual(before, 2)
        rows.nth(0).locator(".orchestrator-dismiss").click()
        self.page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length < n",
            arg=[f"{self.DESKTOP} .orchestrator-item", before],
            timeout=15_000,
        )

    def test_it_leaves_the_other_agents_alone(self):
        """The whole reason for a per-row control rather than clear-all."""
        rows = self._load_with(3)
        before = rows.count()
        kept = rows.nth(1).locator(".chat-title").inner_text()
        rows.nth(0).locator(".orchestrator-dismiss").click()
        self.page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length < n",
            arg=[f"{self.DESKTOP} .orchestrator-item", before],
            timeout=15_000,
        )
        remaining = self.page.locator(f"{self.DESKTOP} .orchestrator-item .chat-title")
        titles = [remaining.nth(i).inner_text() for i in range(remaining.count())]
        self.assertIn(kept, titles, "dismissing one silenced the others too")

    def test_it_does_not_open_the_conversation(self):
        """The button sits inside the row, whose click opens the chat.

        Without stopPropagation the control would open the very conversation
        you had just asked to stop being shown -- and, since opening does not
        dismiss, the row would come straight back.
        """
        rows = self._load_with(2)
        rows.nth(0).locator(".orchestrator-dismiss").click()
        self.page.wait_for_timeout(1500)
        self.assertTrue(
            self.page.is_visible("#messagesArea .empty-state")
            or not self.page.is_visible("#composerInput"),
            "dismissing opened the conversation instead of only removing it",
        )

    def test_the_dismissal_survives_a_reload(self):
        """A row that comes back on refresh was never really dismissed."""
        rows = self._load_with(2)
        before = rows.count()
        gone = rows.nth(0).locator(".chat-title").inner_text()
        rows.nth(0).locator(".orchestrator-dismiss").click()
        self.page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length < n",
            arg=[f"{self.DESKTOP} .orchestrator-item", before],
            timeout=15_000,
        )
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(f"{self.DESKTOP} .orchestrator-item", timeout=15_000)
        self.page.wait_for_timeout(1000)
        titles = self.page.locator(f"{self.DESKTOP} .orchestrator-item .chat-title")
        self.assertNotIn(
            gone, [titles.nth(i).inner_text() for i in range(titles.count())],
            "the dismissed agent reappeared after a reload",
        )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorRenameBrowserTests(_BrowserFixture):
    """Renaming an orchestrator from the row it appears on.

    Driven in a browser rather than asserted against the source, because the
    two things most likely to break are invisible in the file: whether the
    30-second refresh wipes a half-typed name, and whether clicking the
    rename control also selects an orchestrator the user was not looking at.
    Both read as fine in the source and are decided at runtime.
    """

    def _make_supervisor(self, title: str) -> str:
        csrf = next(c["value"] for c in self.page.context.cookies()
                    if c["name"] == "wc_csrf")
        return self.page.evaluate("""async ([csrf, title]) => {
            const r = await fetch('/api/supervisors', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                body: JSON.stringify({title})});
            return (await r.json()).id;
        }""", [csrf, title])

    def _titles(self) -> list[str]:
        return self.page.locator(".orchestrator-list-item .sl-title").all_text_contents()

    def _open(self, *titles):
        for title in titles:
            self._make_supervisor(title)
        self.page.goto(f"{self.base}/orchestrator", wait_until="domcontentloaded")
        self.page.wait_for_selector(".orchestrator-list-item", timeout=15_000)

    def _begin_rename(self, title: str):
        """Click the rename control on the row named *title*.

        Addressed by name rather than by index on purpose. The server is shared
        across the class, so supervisors created by earlier tests are still
        listed and nth(0) is whichever one happens to sort first -- a first
        version of this used an index and failed as soon as a sibling test
        renamed something. A test whose subject depends on execution order is
        not testing what it says.
        """
        # Retried, because this list rebuilds on a 30-second timer as well as
        # on selection: the button can detach between being resolved and being
        # clicked, which surfaces as a click that never lands. Passing alone
        # and failing in the class is what that looks like.
        last = None
        for _ in range(4):
            try:
                row = self.page.locator(".orchestrator-list-item", has_text=title)
                # force=True: the button is opacity:0 until the row is hovered,
                # which is a paint state rather than a hit-testing one.
                row.locator(".sl-rename").click(force=True, timeout=4_000)
                self.page.wait_for_selector(".sl-rename-input", timeout=4_000)
                return self.page.locator(".sl-rename-input")
            except Exception as exc:  # noqa: BLE001 -- retried below
                last = exc
                self.page.wait_for_timeout(400)
        raise AssertionError(f"could not open the rename control for {title!r}: {last}")

    def test_the_control_opens_an_input_holding_the_current_name(self):
        self._open("Renameable one")
        field = self._begin_rename("Renameable one")
        self.assertEqual(field.input_value(), "Renameable one")

    def test_enter_persists_the_new_name(self):
        self._open("Before rename")
        field = self._begin_rename("Before rename")
        field.fill("After rename")
        field.press("Enter")
        self.page.wait_for_selector(".sl-rename-input", state="detached", timeout=5_000)
        self.page.wait_for_function(
            "() => [...document.querySelectorAll('.sl-title')]"
            ".some(n => n.textContent === 'After rename')", timeout=10_000)
        # And it survives a reload, so it was stored rather than only painted.
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(".orchestrator-list-item", timeout=15_000)
        self.assertIn("After rename", self._titles())

    def test_escape_abandons_the_edit(self):
        self._open("Keep this name")
        field = self._begin_rename("Keep this name")
        field.fill("Discarded")
        field.press("Escape")
        self.page.wait_for_selector(".sl-rename-input", state="detached", timeout=5_000)
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(".orchestrator-list-item", timeout=15_000)
        titles = self._titles()
        self.assertIn("Keep this name", titles)
        self.assertNotIn("Discarded", titles)

    def test_a_real_refresh_does_not_wipe_a_half_typed_name(self):
        """The hazard the guard exists for, proven against the real timer.

        loadSupervisors() rebuilds the list wholesale on a 30-second interval,
        so without suppressing it during an edit the input is replaced
        mid-typing -- silently, at a moment the user cannot predict.

        Slow on purpose. A first version called
        `window.loadSupervisors && window.loadSupervisors()`, but that function
        lives inside the module closure and is not on window, so the `&&`
        short-circuited and the test asserted that an input survived a refresh
        that never happened. It passed with the guard deleted. The defensive
        `&&` is what hid it.

        So the poll is now *observed* rather than assumed: the request counter
        below is what makes the wait meaningful, and without it this would be
        thirty seconds of proving nothing.
        """
        self._open("Mid-edit orchestrator")
        polls = []
        self.page.on("request", lambda r: (
            polls.append(r.url) if r.url.endswith("/api/orchestrators")
            and r.method == "GET" else None))

        field = self._begin_rename("Mid-edit orchestrator")
        field.fill("Half typed")
        before = len(polls)

        # 30s interval; allow margin for a slow box under a full suite run.
        deadline = time.monotonic() + 45
        while len(polls) == before and time.monotonic() < deadline:
            self.page.wait_for_timeout(500)
        self.assertGreater(
            len(polls), before,
            "no refresh was observed in 45s, so this test proved nothing")

        self.assertEqual(
            self.page.locator(".sl-rename-input").count(), 1,
            "the refresh replaced the input the user was typing into")
        self.assertEqual(field.input_value(), "Half typed")

    def test_renaming_does_not_select_the_row(self):
        """The row's own click selects. Without stopPropagation, renaming a
        orchestrator you are not looking at also switches you to it."""
        self._open("First one", "Second one")
        before = self.page.locator(".orchestrator-list-item.active").count()
        self._begin_rename("First one")
        self.assertEqual(
            self.page.locator(".orchestrator-list-item.active").count(), before,
            "opening the rename control changed the selection")

    def test_the_field_caps_at_the_length_the_server_stores(self):
        """The server slices titles to 200. Without a matching maxlength the
        user types past it and is truncated with no indication, so the rename
        reads as having half worked."""
        self._open("Capped orchestrator")
        field = self._begin_rename("Capped orchestrator")
        self.assertEqual(field.get_attribute("maxlength"), "200")


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorListEscapingBrowserTests(_BrowserFixture):
    """Registry #79: `renderSupervisorList()` interpolated an orchestrator's id
    (into `data-id="..."`) and its status (into a badge's class and text)
    without `esc()`, unlike every other value the same file writes.

    Neither is reachable through the create API today -- `id` is a
    server-generated UUID, `status` is set only by the engine's own control
    flow -- which is exactly why a passing test here matters: it is the only
    thing standing between "looks redundant, remove it" and the gap
    reopening the moment either field ever carries anything else. Seeded
    directly into the database for that reason, and proved by absence of
    execution (a global the payload would have set), not by reading the
    source -- the shape of bug #75/#78 both name: a check that only greps
    for `esc(` cannot tell "escaped" from "escaped the wrong thing".
    """

    def _seed_supervisor(self, sup_id: str, status: str, title: str = "S") -> None:
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO supervisors (id,title,description,owner_id,status,"
            "created_at,updated_at) VALUES (?,?,NULL,'admin',?,?,?)",
            (sup_id, title, status, stamp, stamp),
        )
        con.commit()
        con.close()

    def test_a_crafted_id_cannot_break_out_of_the_attribute(self):
        """`data-id="${...}"` is an *attribute* position: a `<script>` payload
        proves nothing there, since a `<script>` tag typed inside an
        attribute's quotes is just characters to the HTML parser, not markup,
        with or without escaping. Only a bare `"` can break out -- which is
        also why wrapping `s.id` in `esc()` alone would not have been enough;
        `esc()` had to start escaping quotes too (this fix's other half).

        Asserted on the DOM structure itself, not on script execution:
        `script-src 'self'` already blocks every inline-handler payload this
        page could plant, which would make an execution-based assertion pass
        whether or not the quote is escaped, for a reason that has nothing to
        do with this fix. A broken-out `onmouseover` is a real, separate
        attribute on the element whether or not CSP ever lets it fire; an
        escaped one is inert text sitting inside `data-id`'s own value, and
        `getAttribute` tells the two apart directly.
        """
        payload_id = 'x" onmouseover="window.__wc_pwned_id=1" data-y="'
        self._seed_supervisor(payload_id, "idle")
        self.page.goto(f"{self.base}/orchestrator", wait_until="domcontentloaded")
        self.page.wait_for_selector(".orchestrator-list-item", timeout=15_000)
        self.assertIsNone(self.page.eval_on_selector(
            ".orchestrator-list-item", 'el => el.getAttribute("onmouseover")'))

    def test_a_crafted_status_is_not_parsed_as_an_element(self):
        """Text-node position: a `<img onerror>` typed here *is* real markup
        once rendered, unlike a `<script>` tag (which the HTML parser never
        executes when inserted via `innerHTML`, escaped or not -- not a
        meaningful proof either direction).

        Asserted on the DOM, not on the handler firing: `script-src 'self'`
        already blocks every inline `on*` attribute regardless of escaping,
        which would make an execution-based assertion pass for a reason that
        has nothing to do with this fix. Whether a real `<img>` element
        exists at all is the fact `esc()` actually controls.
        """
        sup_id = f"sup-{secrets.token_hex(4)}"
        self._seed_supervisor(
            sup_id, '<img src=x onerror="window.__wc_pwned_status=1">')
        self.page.goto(f"{self.base}/orchestrator", wait_until="domcontentloaded")
        self.page.wait_for_selector(".orchestrator-list-item", timeout=15_000)
        self.assertEqual(
            self.page.locator(".orchestrator-list-item img").count(), 0)


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class QuestionDismissBrowserTests(_BrowserFixture):
    """Declining a question instead of answering it.

    The question endpoint is served by the test rather than by the server,
    because a genuinely pending question needs a live claude blocked inside
    AskUserQuestion inside a multiplexer and there is no way to make one from
    a test. What the interception leaves alone is everything this class is
    about: the real page, the real 4-second poll, a real DELETE leaving the
    browser, and the state the UI keeps between polls. The payload the server
    would have produced, and the handler behind the DELETE, are pinned
    separately in tests/test_qa_question_dismiss.py.

    The poll is why this cannot be asserted against the source. Three of the
    behaviours here -- the bar staying gone, the Esc lock surviving, the note
    surviving -- are decided by what the next refresh does, four seconds after
    the click, and all three read as correct in the file.
    """

    DESKTOP = "#chatListDesktop"
    ASKED = "Which of these is your preferred weekend activity?"

    def _payload(self, answerable=True, with_id=True):
        base = {
            "pending": True,
            # Omitted deliberately in one test: the client keys its suppression
            # on the tool_use id and falls back to the question text, and the
            # fallback is the only path where two conversations can collide.
            **({"id": "toolu_dismiss_test"} if with_id else {}),
            "questions": [{
                "header": "Weekend",
                "question": self.ASKED,
                "options": [
                    {"label": "Hiking outdoors", "description": "Fresh air"},
                    {"label": "Reading a book", "description": "Quiet"},
                ],
            }],
        }
        if not answerable:
            return {**base, "answerable": False,
                    "reason": "This one can only be answered at its terminal."}
        return {**base, "answerable": True, "selected": 1, "options": [
            {"index": 1, "label": "Hiking outdoors", "selected": True},
            {"index": 2, "label": "Reading a book", "selected": False},
        ]}

    def _seed_chat(self) -> str:
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"qd-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "session_id,created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',"
            "?,?,?)",
            (chat_id, f"Question {chat_id}", f"sess-{chat_id}", stamp, stamp),
        )
        con.commit()
        con.close()
        return chat_id

    def _open_with_question(self, answerable=True, delete=(200, {"ok": True}),
                            with_id=True):
        """Open a conversation whose question endpoint this test controls.

        Returns the list of request methods seen, which keeps growing as the
        page polls -- so a test can prove a later refresh happened rather than
        waiting a few seconds and assuming one did.
        """
        import json
        status, body = delete
        calls: list[str] = []

        def handler(route):
            method = route.request.method
            calls.append(method)
            if method == "DELETE":
                route.fulfill(status=status, content_type="application/json",
                              body=json.dumps(body))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self._payload(answerable, with_id)))

        self.page.route("**/api/chats/*/question", handler)
        chat_id = self._seed_chat()
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id, timeout=15_000)
        return calls

    def _open_chat(self, chat_id, timeout=20_000):
        """Open a conversation and wait for its question bar.

        By data-chat-id, not by position: the server is shared across the
        class, so every conversation an earlier test seeded is still listed.
        The generous timeout covers the sidebar's own 6s poll, which is what
        brings a conversation seeded after page load into the list.
        """
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=timeout)
        self.page.click(row)
        self.page.wait_for_selector("#questionBar", state="visible",
                                    timeout=timeout)

    def _wait_for_poll(self, calls, timeout_ms=20_000):
        """Wait for one more GET to arrive, and fail if none does.

        A fixed sleep here would prove nothing: the assertions that follow are
        all about surviving a refresh, and a refresh that never happened
        satisfies every one of them.
        """
        before = calls.count("GET")
        waited = 0
        while waited < timeout_ms:
            self.page.wait_for_timeout(500)
            waited += 500
            if calls.count("GET") > before:
                return
        self.fail("no refresh arrived, so nothing was proved about surviving one")

    def test_the_bar_offers_a_way_out_beside_the_answers(self):
        self._open_with_question()
        button = self.page.locator("#questionDismiss")
        self.assertTrue(button.is_visible())
        self.assertEqual(button.inner_text().strip(), "Don't answer")
        # Not inside the group labelled "Answers": declining is not one.
        self.assertEqual(
            self.page.locator("#questionOptions #questionDismiss").count(), 0)
        self.assertEqual(self.page.locator(".question-option").count(), 2)

    def test_declining_sends_a_delete_and_hides_the_bar(self):
        calls = self._open_with_question()
        self.page.click("#questionDismiss")
        self.page.wait_for_selector("#questionBar", state="hidden", timeout=10_000)
        self.assertIn("DELETE", calls)

    def test_the_poll_does_not_bring_a_declined_question_back(self):
        """The endpoint still reports the question as pending afterwards, which
        is the real case: cancelling a prompt need not write anything to the
        transcript, so `pending` can stay true for ever. Without state on the
        client the bar returns within four seconds of being dismissed.
        """
        calls = self._open_with_question()
        self.page.click("#questionDismiss")
        self.page.wait_for_selector("#questionBar", state="hidden", timeout=10_000)
        self._wait_for_poll(calls)
        self.assertTrue(self.page.locator("#questionBar").is_hidden(),
                        "the refresh brought back a question the user declined")

    def test_a_delivered_escape_is_not_offered_a_second_time(self):
        """409 with delivered=true: the key was accepted and the prompt stayed
        open. Pressing again is not a retry -- the second Esc reaches whatever
        the session moved on to. The lock has to survive the poll, or it is a
        lock for four seconds.
        """
        calls = self._open_with_question(
            delete=(409, {"error": "The prompt is still open at the terminal.",
                          "delivered": True}))
        self.page.click("#questionDismiss")
        self.page.wait_for_selector("#questionDismiss[disabled]", timeout=10_000)
        # The options come back: the prompt is still open, so answering it is
        # now the only thing that will unblock that session.
        self.assertEqual(
            self.page.locator(".question-option:not([disabled])").count(), 2)
        self._wait_for_poll(calls)
        self.assertTrue(self.page.locator("#questionDismiss").is_disabled(),
                        "the refresh handed back a control that must not be pressed")
        # The refresh rewrites the note, so the explanation has to be rebuilt
        # from state too -- otherwise a disabled button sits there unexplained.
        self.assertIn("Esc was delivered", self.page.inner_text("#questionNote"))

    def test_a_refused_escape_can_be_tried_again(self):
        """409 with delivered=false: nothing reached the terminal, so the
        button must come back. Treating both failures alike would strand the
        user on a transient error."""
        self._open_with_question(
            delete=(409, {"error": "Could not reach the terminal.",
                          "delivered": False}))
        self.page.click("#questionDismiss")
        self.page.wait_for_selector("#questionDismiss:not([disabled])",
                                    timeout=10_000)
        self.assertIn("Could not reach", self.page.inner_text("#questionNote"))

    def test_declining_in_one_conversation_does_not_silence_another(self):
        """The suppression key is scoped by conversation.

        The fallback path is where that matters. With a tool_use id the key is
        unique on its own, but a payload without one keys on the question text
        -- and two agents asked the same thing legitimately ask it in the same
        words. Unscoped, declining in one conversation would silence the other
        one before it was ever read, which is indistinguishable from the
        question never having been asked.
        """
        self._open_with_question(with_id=False)
        second = self._seed_chat()
        self.page.click("#questionDismiss")
        self.page.wait_for_selector("#questionBar", state="hidden", timeout=10_000)
        self._open_chat(second)
        self.assertTrue(self.page.locator("#questionBar").is_visible())
        self.assertEqual(self.page.locator(".question-option").count(), 2)

    def test_an_unreachable_question_hides_without_sending_anything(self):
        """Nothing can be delivered, so the control says "Hide" and no request
        is made. Labelling it "Don't answer" here would claim the session had
        been let go when it is still sitting on the prompt."""
        calls = self._open_with_question(answerable=False)
        button = self.page.locator("#questionDismiss")
        self.assertEqual(button.inner_text().strip(), "Hide")
        button.click()
        self.page.wait_for_selector("#questionBar", state="hidden", timeout=10_000)
        self.assertNotIn("DELETE", calls)
        self._wait_for_poll(calls)
        self.assertTrue(self.page.locator("#questionBar").is_hidden())
        self.assertNotIn("DELETE", calls)


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class AutoAnswerCycleBrowserTests(_BrowserFixture):
    """The bot icon's three-state cycle and the full-text tooltip on its log
    rows, driven against the real PUT/GET /api/chats/{id}/auto-answer route --
    no interception. Only the chat row is seeded directly, the same way
    QuestionDismissBrowserTests does: a genuine session_id needs a live claude
    session nothing here spins up, but the auto-answer route itself does not
    care whether the session is real.
    """

    DESKTOP = "#chatListDesktop"

    def _seed_chat(self) -> str:
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"aa-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "session_id,created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',"
            "?,?,?)",
            (chat_id, f"Auto-answer {chat_id}", f"sess-{chat_id}", stamp, stamp),
        )
        con.commit()
        con.close()
        return chat_id

    def _open_chat(self, chat_id, timeout=20_000):
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=timeout)
        self.page.click(row)
        self.page.wait_for_selector("#autoAnswerToggle", state="visible",
                                    timeout=timeout)

    def _wait_for_mode(self, mode, timeout=5000):
        # Not wait_for_function: the app's own CSP is script-src 'self' with
        # no 'unsafe-eval', which Playwright's string-evaluation path violates
        # on this page specifically (most test pages have no CSP at all, which
        # is why this is worth a comment). A CSS attribute selector needs no
        # in-page eval.
        self.page.wait_for_selector(
            f'#autoAnswerToggle[data-mode="{mode}"]', timeout=timeout,
        )

    def test_clicking_cycles_off_on_recommend_off(self):
        chat_id = self._seed_chat()
        self._open_chat(chat_id)
        toggle = self.page.locator("#autoAnswerToggle")
        self.assertEqual(toggle.get_attribute("data-mode"), "off")

        toggle.click()
        self._wait_for_mode("on")
        toggle.click()
        self._wait_for_mode("recommend")
        toggle.click()
        self._wait_for_mode("off")
        self.assertEqual(self.errors, [])

    def test_the_recommend_state_persists_across_a_reload(self):
        chat_id = self._seed_chat()
        self._open_chat(chat_id)
        toggle = self.page.locator("#autoAnswerToggle")
        toggle.click()
        self._wait_for_mode("on")
        toggle.click()
        self._wait_for_mode("recommend")

        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.assertEqual(
            self.page.locator("#autoAnswerToggle").get_attribute("data-mode"),
            "recommend",
        )

    def test_the_recommend_state_has_a_distinct_colour_from_plain_on(self):
        chat_id = self._seed_chat()
        self._open_chat(chat_id)
        toggle = self.page.locator("#autoAnswerToggle")
        toggle.click()
        self._wait_for_mode("on")
        on_color = toggle.evaluate("el => getComputedStyle(el).color")
        toggle.click()
        self._wait_for_mode("recommend")
        recommend_color = toggle.evaluate("el => getComputedStyle(el).color")
        self.assertNotEqual(
            on_color, recommend_color,
            "the third state must not be visually indistinguishable from the "
            "second -- colour is one of two signals a sighted user gets that "
            "this is a further, more powerful state",
        )

    def test_the_recommend_state_swaps_the_glyph_too(self):
        """Colour alone was judged not enough: this state also judges which
        answer is best, not only whether to approve one, so the icon itself
        changes to a brain rather than staying the same robot in a different
        shade.
        """
        chat_id = self._seed_chat()
        self._open_chat(chat_id)
        toggle = self.page.locator("#autoAnswerToggle")
        self.assertEqual(toggle.inner_text(), "🤖")
        toggle.click()
        self._wait_for_mode("on")
        self.assertEqual(toggle.inner_text(), "🤖")
        toggle.click()
        self._wait_for_mode("recommend")
        self.assertEqual(toggle.inner_text(), "🧠")

    def test_the_info_badge_sits_on_the_icons_top_right_corner(self):
        chat_id = self._seed_chat()
        self._open_chat(chat_id)
        icon = self.page.locator("#autoAnswerToggle").bounding_box()
        badge = self.page.locator("#autoAnswerInfo").bounding_box()
        # A corner badge, not a second inline button: it overlaps the icon's
        # own top-right corner rather than sitting beside it.
        self.assertGreater(badge["x"] + badge["width"], icon["x"] + icon["width"] - 4)
        self.assertLess(badge["y"], icon["y"])

    def test_clicking_a_log_row_reveals_the_full_text_in_a_tooltip(self):
        import json
        import sqlite3
        long_reason = "Why does this matter? " * 10  # longer than the 2-line clamp
        chat_id = self._seed_chat()
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "UPDATE chats SET auto_answer_log = ? WHERE id = ?",
            (json.dumps([{"kind": "Question", "prompt": "short",
                         "outcome": "skipped", "reason": long_reason,
                         "at": "now"}]), chat_id),
        )
        con.commit()
        con.close()

        self._open_chat(chat_id)
        self.page.click("#autoAnswerInfo")
        self.page.wait_for_selector(".auto-answer-row-prompt", timeout=5000)
        self.page.click(".auto-answer-row-prompt")
        self.page.wait_for_selector("#autoAnswerTooltip:not([hidden])", timeout=5000)
        self.assertEqual(
            self.page.locator("#autoAnswerTooltip").inner_text(), long_reason,
        )

        # A second click closes it again.
        self.page.click(".auto-answer-row-prompt")
        # state="hidden" on the bare selector, not "[hidden]" appended to it:
        # a hidden element can never satisfy the default state="visible" a
        # selector match implies, so that combination times out for ever.
        self.page.wait_for_selector("#autoAnswerTooltip", state="hidden", timeout=5000)
        self.assertEqual(self.errors, [])


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class LoadMoreMessagesBrowserTests(_BrowserFixture):
    """A long conversation loads its newest page, not its whole history.

    Source inspection cannot tell "renders 50 rows" from "renders all of
    them and the 51st is merely off-screen" -- both look identical in the
    file. Only a real render, counted, proves the paginated GET this test
    seeds actually reached the DOM instead of the old whole-history fetch.
    """

    DESKTOP = "#chatListDesktop"

    def _seed_chat(self, message_count: int) -> str:
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"lm-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Long chat {chat_id}", stamp, stamp),
        )
        con.executemany(
            "INSERT INTO messages (chat_id,role,content,created_at) "
            "VALUES (?,?,?,?)",
            [(chat_id, "user" if i % 2 == 0 else "assistant", f"msg{i}", stamp)
             for i in range(message_count)],
        )
        con.commit()
        con.close()
        return chat_id

    def _open_chat(self, chat_id, timeout=20_000):
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=timeout)
        self.page.click(row)

    def test_only_the_newest_fifty_render_and_more_can_be_loaded(self):
        chat_id = self._seed_chat(60)
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.page.wait_for_selector("#messagesArea .message", timeout=10_000)
        self.assertEqual(self.page.locator("#messagesArea .message").count(), 50)
        # The oldest 10 (msg0..msg9) have not loaded yet -- msg10 has, as the
        # new oldest row on screen.
        body_text = self.page.locator("#messagesArea").inner_text()
        self.assertNotIn("msg0\n", body_text)
        self.assertIn("msg10", body_text)

        self.page.click("#messagesArea .load-more-btn")
        self.page.wait_for_selector("#messagesArea .message", timeout=10_000)
        # Give the fetch a moment; the button removes itself once the older
        # page (all 10 remaining messages) is in and nothing is left to load.
        self.page.wait_for_selector(".load-more-wrap", state="detached", timeout=10_000)
        self.assertEqual(self.page.locator("#messagesArea .message").count(), 60)
        self.assertEqual(self.errors, [])

    def test_a_short_conversation_shows_no_load_more_control(self):
        chat_id = self._seed_chat(5)
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.page.wait_for_selector("#messagesArea .message", timeout=10_000)
        self.assertEqual(self.page.locator("#messagesArea .message").count(), 5)
        self.assertEqual(self.page.locator(".load-more-btn").count(), 0)
        self.assertEqual(self.errors, [])


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class QueuePanelBrowserTests(_BrowserFixture):
    """The queue panel's held/pending badge, and its close/toggle controls.

    Before this, the panel only ever showed itself automatically (rows.length
    > 0) and had no way back out of sight without discarding a prompt or
    leaving the conversation -- and the sidebar's count summed pending and
    held into one number, so a chat with 3 held prompts (needing a Send/
    Discard decision) looked identical to one with 3 healthy ones. Source
    inspection cannot tell "the badge takes data-held from the API" from "it
    is hardcoded no" -- both read the same in the file; only a seeded row and
    a real render prove it.
    """

    DESKTOP = "#chatListDesktop"

    def _seed_chat_with_queue(self, rows: list[tuple[str, str]]) -> str:
        """rows: (prompt, state) pairs, state one of 'pending'/'held'."""
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"qp-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Queue {chat_id}", stamp, stamp),
        )
        con.executemany(
            "INSERT INTO turn_queue (chat_id,owner_id,prompt,model,state,"
            "created_at) VALUES (?,'admin',?,NULL,?,?)",
            [(chat_id, prompt, state, stamp) for prompt, state in rows],
        )
        con.commit()
        con.close()
        return chat_id

    def _open_chat(self, chat_id: str, timeout: int = 20_000):
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=timeout)
        self.page.click(row)
        self.page.wait_for_selector("#queueBar", state="visible", timeout=timeout)

    def test_sidebar_badge_takes_the_held_colour_when_any_row_is_held(self):
        chat_id = self._seed_chat_with_queue(
            [("healthy", "pending"), ("broken", "held")]
        )
        self.page.reload(wait_until="domcontentloaded")
        badge = self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] .chat-queued',
            timeout=20_000,
        )
        self.assertEqual(
            badge.get_attribute("data-held"), "yes",
            "a chat with one held row must render the held colour, not the "
            "plain 'something is queued' one -- summed together they look "
            "identical, which is the bug this exists to fix",
        )
        self.assertEqual(badge.inner_text(), "2")
        self.assertEqual(self.errors, [])

    def test_a_purely_pending_queue_does_not_get_the_held_colour(self):
        chat_id = self._seed_chat_with_queue([("a", "pending"), ("b", "pending")])
        self.page.reload(wait_until="domcontentloaded")
        badge = self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] .chat-queued',
            timeout=20_000,
        )
        self.assertEqual(badge.get_attribute("data-held"), "no")
        self.assertEqual(self.errors, [])

    def test_close_hides_the_panel_but_the_toggle_survives_and_reopens_it(self):
        chat_id = self._seed_chat_with_queue([("held one", "held")])
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.assertEqual(self.page.inner_text("#queueToggle"), "Queue (1)")
        self.assertEqual(
            self.page.get_attribute("#queueToggle", "data-held"), "yes",
        )

        self.page.click("#queueClose")
        self.page.wait_for_selector("#queueBar", state="hidden", timeout=5_000)
        # The whole point of adding the toggle: it must not disappear along
        # with the panel it exists to reopen, or closing becomes one-way.
        self.assertTrue(
            self.page.is_visible("#queueToggle"),
            "the toggle button hid itself along with the panel -- there is "
            "now no way back in without reloading or leaving the chat",
        )

        self.page.click("#queueToggle")
        self.page.wait_for_selector("#queueBar", state="visible", timeout=5_000)
        self.assertEqual(self.errors, [])

    def test_discarding_the_only_row_hides_both_the_panel_and_the_toggle(self):
        chat_id = self._seed_chat_with_queue([("drop me", "held")])
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.page.click(".queue-btn-drop")
        self.page.wait_for_selector("#queueBar", state="hidden", timeout=10_000)
        self.assertFalse(
            self.page.is_visible("#queueToggle"),
            "nothing left queued, so the toggle has nothing to reopen -- it "
            "must go away with the panel, not linger showing 'Queue (0)'",
        )
        self.assertEqual(self.errors, [])

    def _add_queue_row(self, chat_id: str, prompt: str, state: str) -> None:
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO turn_queue (chat_id,owner_id,prompt,model,state,"
            "created_at) VALUES (?,'admin',?,NULL,?,?)",
            (chat_id, prompt, state, stamp),
        )
        con.commit()
        con.close()

    def _fake_queued_reply(self, chat_id: str, position: int):
        """Intercepts the composer's own POST /stream with a "queued" SSE
        frame -- the real trigger for refreshQueue() while the panel is
        closed. In production this fires when the viewer types a new prompt
        into a conversation whose turn is already running elsewhere and gets
        told theirs is waiting too; nothing here is on a timer."""
        def handler(route):
            route.fulfill(
                status=200,
                content_type="text/event-stream",
                body=f'data: {{"type": "queued", "position": {position}}}\n\n',
            )
        self.page.route(f"**/api/chats/{chat_id}/stream", handler)

    def test_closing_survives_a_refresh_that_changes_nothing(self):
        chat_id = self._seed_chat_with_queue([("held one", "held")])
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.page.click("#queueClose")
        self.page.wait_for_selector("#queueBar", state="hidden", timeout=5_000)

        # The DB queue is unchanged -- still the one held row -- so the
        # refresh this triggers must not undo the close.
        self._fake_queued_reply(chat_id, 2)
        self.page.fill("#composerInput", "another prompt")
        self.page.click("#sendBtn")
        self.page.wait_for_timeout(1500)

        self.assertFalse(
            self.page.is_visible("#queueBar"),
            "a refresh reporting an unchanged queue reopened a panel the "
            "user had just closed -- closing is supposed to survive "
            "identical polls, only genuinely new information should break "
            "through it",
        )
        self.assertEqual(self.errors, [])

    def test_new_information_breaks_through_a_closed_panel(self):
        chat_id = self._seed_chat_with_queue([("held one", "held")])
        self.page.reload(wait_until="domcontentloaded")
        self._open_chat(chat_id)
        self.page.click("#queueClose")
        self.page.wait_for_selector("#queueBar", state="hidden", timeout=5_000)

        # A second prompt actually lands in the queue between the close and
        # this refresh -- exactly the case a silently-permanent close would
        # hide from the user.
        self._add_queue_row(chat_id, "second one", "pending")
        self._fake_queued_reply(chat_id, 2)
        self.page.fill("#composerInput", "another prompt")
        self.page.click("#sendBtn")

        self.page.wait_for_selector("#queueBar", state="visible", timeout=10_000)
        self.assertEqual(
            self.page.inner_text("#queueToggle"), "Queue (2)",
            "the panel reopened but the toggle did not pick up the new "
            "count -- the two must stay in sync",
        )
        self.assertEqual(self.errors, [])


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SshWizardBrowserTests(_BrowserFixture):
    """The SSH init wizard renders and is callable from the Backends tab.

    Before the fix, machine-wizard.js existed but was never imported or
    called by app.js — the file sat unused. The tests verify that after the
    import wire-up, an ssh_proxy machine present in the listing causes the
    wizard panel to appear inside Settings, and that a backend without
    ssh_proxy machines does not cause it to show.
    """

    def _seed_ssh_machine(self) -> str:
        import datetime
        import sqlite3
        import secrets as sec
        machine_id = f"ssh-{sec.token_hex(4)}"
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO ai_machines (id,name,host,port,provider,base_url,"
            "description,ssh_host,ssh_user,ssh_key_path,"
            "model,api_key,owner_id,created_at,updated_at,"
            "ai_api_key_name,has_api_key,active_models,active) "
            "VALUES (?,?,?,?,?,NULL,NULL,?,?,?,NULL,?,?,?,0,'[]',0)",
            (machine_id, "SSH Proxy", "127.0.0.1", 9000, "ssh_proxy",
             "192.168.1.100", "kali", "/home/kali/.ssh/id_rsa",
             "admin", stamp, stamp),
        )
        # Seed one chat so the sidebar has a conversation to show.
        chat_id = f"q-{sec.token_hex(4)}"
        con.execute(
            "INSERT INTO chats (id,title,work_dir,owner_id,created_at,updated_at) "
            "VALUES (?,?,NULL,'admin',?,?)",
            (chat_id, "Test", stamp, stamp),
        )
        con.commit()
        con.close()
        return machine_id

    def test_wizard_panel_shows_when_ssh_proxy_exists(self):
        self._seed_ssh_machine()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=10_000)
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=10_000)
        self.page.wait_for_timeout(800)

        wizard = self.page.wait_for_selector("#sshWizard", state="visible", timeout=5_000)
        self.assertTrue(
            wizard,
            "the SSH Proxy Setup wizard must appear in Settings when an "
            "ssh_proxy machine is present in the backends list",
        )
        self.assertIn("SSH Proxy Setup", wizard.inner_text())
        self.assertEqual(self.errors, [])

    def test_wizard_panel_does_not_show_without_ssh_proxy(self):
        # Only the default Anthropic machine exists — no ssh_proxy.
        self._open_backends()

        wizard = self.page.query_selector("#sshWizard")
        self.assertIsNone(
            wizard,
            "the wizard must not render when no ssh_proxy machine is "
            "registered",
        )
        self.assertEqual(self.errors, [])

    def test_wizard_panel_hides_after_cancel(self):
        self._seed_ssh_machine()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=10_000)
        self.page.click("#settingsBtn")
        self.page.wait_for_selector(".machine-card", timeout=10_000)
        self.page.wait_for_timeout(800)

        wizard = self.page.wait_for_selector("#sshWizard", state="visible", timeout=5_000)
        self.assertTrue(wizard.is_visible())

        self.page.click("#wizardCancel")
        self.page.wait_for_timeout(500)

        wizard2 = self.page.query_selector("#sshWizard")
        self.assertIsNone(
            wizard2,
            "clicking Cancel must remove the wizard panel from the DOM",
        )
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
