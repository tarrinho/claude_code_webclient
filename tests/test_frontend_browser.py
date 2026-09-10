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

import json
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

    def _open_settings(self):
        """Click the settings button and wait for the dialog."""
        self.page.click("#settingsBtn")
        self.page.wait_for_selector("#settingsDialog", timeout=10_000)
        self.page.wait_for_timeout(800)

    def _close_settings_and_reopen(self):
        """Close the dialog, refresh, re-open — validates load flow."""
        self.page.click("#settingsCancel")
        self.page.wait_for_selector("#settingsDialog", is_hidden=True, timeout=5_000)
        self._load()  # fresh page load
        self._open_settings()

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

    def test_cross_session_inbound_setting(self):
        """The App tab offers a cross-session peer messages select with
        accept / prompt options, loads the server value, and persists it."""
        self._open_settings()
        self.page.click('[data-tab="app"]')
        self.page.wait_for_selector('#crossSessionInbound', timeout=5_000)
        select = self.page.query_selector('#crossSessionInbound')
        self.assertIsNotNone(select)
        values = select.evaluate(
            'el => [...el.options].map(o => o.value)'
        )
        self.assertIn("accept", values)
        self.assertIn("prompt", values)
        initial = select.evaluate('el => el.value')
        self.assertIn(initial, ("accept", "prompt"))
        # Change, save, reopen, and verify persisted.
        select.select_option("prompt")
        self.page.click("#settingsSave")
        self.page.wait_for_timeout(2_000)
        self._close_settings_and_reopen()
        self.page.click('[data-tab="app"]')
        self.page.wait_for_timeout(300)
        new_val = self.page.query_selector('#crossSessionInbound').evaluate(
            'el => el.value'
        )
        self.assertEqual(new_val, "prompt")

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
class TransportUIBrowserTests(_BrowserFixture):
    """The "+ Add transport" form and the grouped backends map.

    Before this feature, every backend rendered flat in the Backends map and
    ran on the same host as the console. The ssh-transport/backend-split plan
    lets a backend execute over an SSH transport instead, and groups the map
    by which transport (if any) each backend runs on. This is the only
    committed regression coverage for that grouping -- a static text read of
    machines.js/transports.js cannot see whether a transport's header
    actually appears in the DOM, whether the SSH badge is present only once a
    backend is assigned, or whether editing a backend back to "This server"
    actually moves its card in the rendered list.

    Each test creates its own uniquely-named transport (and, where needed, its
    own backend) through the real UI rather than seeding the database, so
    tests in this class do not depend on run order or on each other's leftover
    state -- matching this file's own convention elsewhere (e.g.
    BackendsTurnCountBrowserTests gets its own class rather than sharing
    BackendsPanelBrowserTests' fixture data).
    """

    def _add_transport(self, name: str) -> None:
        self.page.click("#addTransportBtn")
        self.page.wait_for_selector("#transportForm", state="visible", timeout=5_000)
        self.page.fill("#transportName", name)
        # A resolvable host is required -- the server validates it can be
        # looked up (SSRF-style guard) before ever attempting to connect, so
        # "localhost" gets past that check without needing a real remote box.
        # The key path stays fake: nothing here ever needs SSH to succeed.
        self.page.fill("#transportSshHost", "localhost")
        self.page.fill("#transportSshUser", "kali")
        self.page.fill("#transportSshKeyPath", "/tmp/does-not-exist-key")
        self.page.click("#saveTransport")
        # 20s, not 10s: several agent sessions share this host and a save that
        # normally lands in well under a second has been measured taking longer
        # than 10s under that contention -- observed twice here as a timeout in
        # this helper while every assertion it feeds was correct. The class runs
        # more saves than it used to, so it meets the contention more often.
        self.page.wait_for_selector("#transportForm", state="hidden", timeout=20_000)
        self.page.wait_for_timeout(500)

    def _add_backend_on_transport(self, machine_name: str, transport_name: str) -> None:
        self.page.click("#addMachineBtn")
        self.page.wait_for_selector("#machineForm", state="visible", timeout=5_000)
        self.page.fill("#machineName", machine_name)
        self.page.select_option("#machineProvider", "claude_code")
        options = self.page.eval_on_selector_all(
            "#machineTransport option", "opts => opts.map(o => [o.value, o.textContent])"
        )
        transport_value = next(val for val, text in options if transport_name in text)
        self.page.select_option("#machineTransport", transport_value)
        self.page.click("#saveMachine")
        # Same contention headroom as _add_transport above.
        self.page.wait_for_selector("#machineForm", state="hidden", timeout=20_000)
        self.page.wait_for_timeout(500)

    def _machine_list_dump(self) -> list[dict]:
        """{cls, text} for every direct child of #machineList, in render order."""
        return self.page.eval_on_selector_all(
            "#machineList > *",
            "(nodes) => nodes.map(n => ({cls: n.className, text: n.textContent}))",
        )

    # None of the tests below assert `self.errors == []`, unlike most of this
    # file's other classes. Two ambient, unrelated bugs are live in this
    # shared tree right now and would make that assertion fail regardless of
    # anything this class tests: (1) routes/misc.py's /api/settings handler
    # raises (`for row in cur.fetchall()` on an un-awaited coroutine), logged
    # as a console error on every page load; (2) app.js is currently loaded as
    # two separate module instances (index.html's <script> tag references
    # ?v=40, every importing module still references ?v=39), so every button
    # wired in its DOMContentLoaded handler -- including #saveTransport and
    # #saveMachine -- fires its click listener twice. Neither is caused by, or
    # fixable from, this file. The second one is also why the assertions below
    # tolerate more than one matching header/card turning up (a single click
    # can create two identically-named rows) rather than asserting exactly
    # one -- the point of this class is to catch a real regression in the
    # grouping logic, not to go red for an unrelated, already-tracked bug.

    def test_saving_a_transport_shows_an_empty_group_header(self):
        self._open_backends()
        self._add_transport("HeaderOnlyBox")
        dump = self._machine_list_dump()
        # A group header's own class moved from a bare "chat-section-label"
        # onto the outer transport-group-header div, with the label text now
        # in a child span (status badge, count, and actions as siblings, all
        # still inside this same element -- so its own textContent, checked
        # below, still carries everything a header contains). Matched by
        # substring rather than a fixed full string, since the real class
        # attribute also carries a status suffix, e.g.
        # "transport-group-header transport-status-uninitialized".
        headers = [
            item for item in dump
            if "transport-group-header" in item["cls"].split()
            and "HeaderOnlyBox" in item["text"]
        ]
        self.assertTrue(headers, f"expected a 'via HeaderOnlyBox' header, got: {dump}")
        for header in headers:
            self.assertNotIn(
                "SSH", header["text"],
                "a transport with no backend assigned yet must not show the "
                "tunnel-toggle badge -- there is no machine id to start a "
                "tunnel for",
            )

    def test_adding_a_backend_groups_it_under_its_transports_header(self):
        self._open_backends()
        self._add_transport("AssignBox")
        self._add_backend_on_transport("Assigned Backend", "AssignBox")

        dump = self._machine_list_dump()
        card_idx = next(
            i for i, item in enumerate(dump) if "Assigned Backend" in item["text"]
        )
        # Same class move as the test above: the header is the
        # transport-group-header div, not a "chat-section-label" element.
        header_idx = max(
            i for i in range(card_idx)
            if "transport-group-header" in dump[i]["cls"].split()
        )
        self.assertIn(
            "AssignBox", dump[header_idx]["text"],
            f"the backend's nearest preceding header must be its own "
            f"transport's, got: {dump}",
        )
        self.assertIn(
            "SSH", dump[header_idx]["text"],
            "a transport header must gain the tunnel-toggle badge once a "
            "backend is assigned to it",
        )

    def test_editing_back_to_this_server_moves_it_out_of_the_group(self):
        self._open_backends()
        self._add_transport("ReleaseBox")
        self._add_backend_on_transport("Released Backend", "ReleaseBox")

        cards = self.page.query_selector_all("#machineList .machine-card")
        target = next(c for c in cards if "Released Backend" in c.inner_text())
        target.query_selector(".machine-action:has-text('Edit')").click()
        self.page.wait_for_selector("#machineForm", state="visible", timeout=5_000)
        # Precondition: the picker must show the transport it was actually
        # saved with, not just the placeholder -- otherwise this test would
        # pass even if the edit path never populated the picker at all.
        options = self.page.eval_on_selector_all(
            "#machineTransport option", "opts => opts.map(o => [o.value, o.textContent])"
        )
        release_value = next(val for val, text in options if "ReleaseBox" in text)
        current = self.page.eval_on_selector("#machineTransport", "el => el.value")
        self.assertEqual(current, release_value, "precondition: picker preselects its transport")

        self.page.select_option("#machineTransport", "")
        self.page.click("#saveMachine")
        self.page.wait_for_selector("#machineForm", state="hidden", timeout=10_000)
        self.page.wait_for_timeout(500)

        dump = self._machine_list_dump()
        card_idx = next(
            i for i, item in enumerate(dump) if "Released Backend" in item["text"]
        )
        first_header_idx = next(
            (i for i, item in enumerate(dump) if item["cls"] == "chat-section-label"),
            None,
        )
        if first_header_idx is not None:
            self.assertLess(
                card_idx, first_header_idx,
                "a backend edited back to 'This server' must render in the "
                "local/ungrouped section, before every transport header",
            )

    # ── Editing and deleting an existing transport ───────────────────────
    #
    # PATCH and DELETE existed server-side from the start, and _saveTransport
    # already branched on `_transportEditing` to use PATCH -- but that variable
    # was only ever assigned `null`, in three places, so the branch was
    # unreachable and an added transport could not be changed or removed at
    # all. Nothing static could see it: the code reads as a complete
    # create/edit/delete feature. Only driving the header controls proves the
    # entry point exists and the round trip lands.

    def _transport_header(self, name: str) -> dict | None:
        return next(
            (item for item in self._machine_list_dump()
             if item["cls"] == "chat-section-label" and name in item["text"]),
            None,
        )

    def test_editing_a_transport_prefills_the_form_with_its_current_values(self):
        """A blank form would be worse than no form: PATCH sends all four
        fields, so saving one would blank the rest and the server would refuse
        it ("SSH host cannot be empty")."""
        self._open_backends()
        self._add_transport("PrefillBox")
        self.page.click('button[aria-label="Edit transport PrefillBox"]')
        self.page.wait_for_selector("#transportForm", state="visible", timeout=5_000)

        self.assertEqual(self.page.input_value("#transportName"), "PrefillBox")
        self.assertEqual(self.page.input_value("#transportSshHost"), "localhost")
        self.assertEqual(self.page.input_value("#transportSshUser"), "kali")
        self.assertEqual(
            self.page.input_value("#transportSshKeyPath"), "/tmp/does-not-exist-key",
        )
        self.assertEqual(
            self.page.inner_text("#transportFormTitle"), "Edit transport",
            "the form must say it is editing, not adding",
        )

    def test_an_edit_persists_and_renames_the_group_header(self):
        self._open_backends()
        self._add_transport("RenameFromBox")
        self.page.click('button[aria-label="Edit transport RenameFromBox"]')
        self.page.wait_for_selector("#transportForm", state="visible", timeout=5_000)
        self.page.fill("#transportName", "RenamedToBox")
        self.page.click("#saveTransport")
        self.page.wait_for_selector("#transportForm", state="hidden", timeout=10_000)
        self.page.wait_for_timeout(600)

        self.assertIsNotNone(
            self._transport_header("RenamedToBox"),
            "the edited name must appear as its group header -- if the PATCH "
            "branch is unreachable this silently creates nothing and the old "
            "name stays",
        )
        self.assertIsNone(
            self._transport_header("RenameFromBox"),
            "the old name must be gone: an edit must not leave the original "
            "behind, which is what a POST-instead-of-PATCH would do",
        )

    def test_deleting_an_unused_transport_removes_its_header(self):
        self._open_backends()
        self._add_transport("DeleteMeBox")
        self.assertIsNotNone(self._transport_header("DeleteMeBox"), "precondition")

        # Playwright dismisses dialogs unless told otherwise, and the delete is
        # behind a confirm() -- without accepting, the request never goes out
        # and the test would pass against a broken delete for the wrong reason.
        self.page.once("dialog", lambda d: d.accept())
        self.page.click('button[aria-label="Delete transport DeleteMeBox"]')
        self.page.wait_for_timeout(1200)

        self.assertIsNone(
            self._transport_header("DeleteMeBox"),
            "the transport's header must be gone after deleting it",
        )

    def test_a_transport_still_in_use_refuses_deletion_and_says_why(self):
        """The server answers 409 with a count, because ai_machines has no
        foreign key on transport_id -- deleting a referenced transport would
        leave those backends permanently unable to connect. The point of this
        test is that the reason reaches the user rather than a bare status."""
        self._open_backends()
        self._add_transport("InUseBox")
        self._add_backend_on_transport("BackendOnInUse", "InUseBox")

        self.page.once("dialog", lambda d: d.accept())
        self.page.click('button[aria-label="Delete transport InUseBox"]')
        self.page.wait_for_timeout(1200)

        self.assertIsNotNone(
            self._transport_header("InUseBox"),
            "a transport still referenced by a backend must survive the "
            "delete attempt",
        )
        # #settingsStatus is where notifyResult lands while Settings is open.
        # wait_for_function, not a fixed sleep: the message is written when the
        # 409 lands, and a sleep long enough to be safe was also long enough for
        # the bug this uncovered (a previous success's scheduled clear wiping it)
        # to erase it again before it could be read.
        self.page.wait_for_function(
            "() => (document.getElementById('settingsStatus').textContent || '')"
            ".includes('still use this transport')",
            timeout=8000)
        status = self.page.text_content("#settingsStatus")
        self.assertIn(
            "still use this transport", status,
            "the server's 409 explanation must be surfaced -- reporting only "
            "the status code leaves the user to guess why it refused",
        )


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
        return self.page.query_selector("#orchestratorPane")

    def test_the_topbar_control_opens_the_pane_not_a_new_page(self):
        """It used to navigate away, which cost the sidebar and a reload.

        Scoped to the pane deliberately: what loads *inside* the frame is the
        orchestrator's own page, with its own engine and SSE stream. Driving that
        from here made this suite depend on another subsystem's behaviour and
        destabilised every test after it.
        """
        self._load()
        before = self.page.url
        self.page.click("#orchestratorBtn")
        self.page.wait_for_selector("#orchestratorPane:not([hidden])", timeout=10_000)
        self.assertEqual(self.page.url, before, "it navigated instead of embedding")
        self.assertFalse(self.page.is_visible("#messagesWrap"),
                         "the conversation area is still showing behind it")
        self.assertIn(
            "orchestrator.html",
            self.page.query_selector("#orchestratorFrame").get_attribute("src"),
        )

    def test_the_embedded_frame_actually_fills_the_pane(self):
        """The frame must have real size, not just the right `src`.

        Reported as "the UI is bad and strangely small". The
        supervisor -> orchestrator rename reached index.html and
        orchestrator.js but not styles.css, so `#supervisorFrame{flex:1}`
        and `#supervisorPane.open{inset:0}` matched nothing. An unstyled
        iframe falls back to its HTML-default intrinsic size, 300x150 --
        and orchestrator.html lays itself out with `calc(100vh - 44px)`,
        which inside a frame measures the frame's box and not the window,
        so the entire page rendered into 150px of height with its 600px
        panels crushed to 300px.

        The test above passed throughout: `src` was always correct. Size is
        the property that was wrong, so size is what has to be asserted --
        a dead selector is invisible to every check that only reads
        attributes. Compared against the viewport rather than a pixel
        constant so the assertion does not depend on this fixture's window
        size.
        """
        self._load()
        self.page.click("#orchestratorBtn")
        self.page.wait_for_selector("#orchestratorPane:not([hidden])", timeout=10_000)
        self.page.wait_for_timeout(300)

        box = self.page.query_selector("#orchestratorFrame").bounding_box()
        viewport = self.page.viewport_size
        self.assertGreater(
            box["height"], viewport["height"] * 0.5,
            f"the frame is {box['height']}px tall in a "
            f"{viewport['height']}px viewport -- it is not being sized by "
            f"CSS at all (300x150 is the unstyled iframe default)",
        )
        self.assertGreater(
            box["width"], viewport["width"] * 0.5,
            f"the frame is only {box['width']}px wide",
        )
        self.assertEqual(self.errors, [])

    def test_the_sidebar_link_opens_the_same_pane(self):
        self._load()
        self.page.click(f"{self.DESKTOP} .orchestrator-open")
        self.page.wait_for_selector("#orchestratorPane:not([hidden])", timeout=10_000)

    def test_the_conversation_list_stays_visible_beside_it(self):
        """The reason for embedding: you can still see who is waiting."""
        self._load()
        self.page.click("#orchestratorBtn")
        self.page.wait_for_selector("#orchestratorPane:not([hidden])", timeout=10_000)
        self.assertTrue(self.page.is_visible(self.DESKTOP),
                        "the sidebar went away, which defeats the point")

    def test_closing_returns_to_the_conversation(self):
        self._load()
        self.page.click("#orchestratorBtn")
        self.page.wait_for_selector("#orchestratorPane:not([hidden])", timeout=10_000)
        self.page.click("#orchestratorPaneClose")
        # state="hidden": the default waits for *visible*, so asserting on a
        # hidden element that way can only ever time out -- the element was
        # correctly hidden the whole time.
        self.page.wait_for_selector("#orchestratorPane", state="hidden", timeout=10_000)
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
            const r = await fetch('/api/orchestrators', {method: 'POST',
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
            const r = await fetch('/api/orchestrators', {method: 'POST',
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
            "INSERT INTO orchestrators (id,title,description,owner_id,status,"
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


# SshWizardBrowserTests (machine-wizard.js's #sshWizard panel) was removed
# along with machine-wizard.js itself: the ssh-transport/backend-split plan's
# Task 9 retired the old provider='ssh_proxy' init wizard in favor of the
# transport form's own "Test connection" button plus the transport header's
# SSH tunnel badge. See web/assets/transports.js and machines.js's grouped
# _renderMachineList.


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class ChatRowMenuBrowserTests(_BrowserFixture):
    """The per-conversation ⋯ menu must open on a click and stay open.

    Reported as "the 3 dots next to each chat isn't working", and the failure
    mode is the reason this is a browser test rather than a static one: the
    menu opened and closed again within the same click, so every selector,
    every handler and every CSS rule read as correct in the source. A
    MutationObserver on the menu's class attribute showed `chat-menu open`
    followed immediately by `chat-menu`.

    The cause was two click listeners on the same list. app.js's
    DOMContentLoaded block calls createChatListController once, but index.html
    referenced `app.js?v=44` while nine importing modules still said `?v=43` --
    and the browser keys a module by its full URL, query string included, so
    app.js was evaluated twice and its whole setup block ran twice. The first
    listener opened the menu; the second read it as already open
    (`opening = false`), called closeMenus(), and did not reopen it.

    tests/test_qa_asset_module_versions.py guards the *cause* statically and
    was red at the time. This guards the *symptom*, because double-attachment
    has other routes -- any future re-invocation of that setup block would do
    the same thing with every version string in agreement. That version split
    has now recurred three times in one day (fixed in e292bc0 and ac28d11
    before this), which is what makes the behavioural half worth its runtime.
    """

    DESKTOP = "#chatListDesktop"

    def _seed_chat(self) -> str:
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"menu-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, "MenuRowChat", stamp, stamp),
        )
        con.commit()
        con.close()
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"]', timeout=20_000,
        )
        return chat_id

    def test_clicking_the_dots_opens_the_menu_and_it_stays_open(self):
        chat_id = self._seed_chat()
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"]'
        self.page.click(f'{row} button[data-action="menu"]')
        # A settle wait, deliberately: the bug closed the menu inside the same
        # click, so asserting immediately would have passed against it.
        self.page.wait_for_timeout(500)

        self.assertEqual(
            self.page.eval_on_selector_all(".chat-menu.open", "els => els.length"), 1,
            "the ⋯ menu is not open after clicking it -- if it opened and shut "
            "again within the click, two listeners are attached to the list "
            "and the second one closed what the first opened",
        )
        self.assertEqual(
            self.page.get_attribute(f'{row} button[data-action="menu"]',
                                    "aria-expanded"), "true",
        )
        # Rendered, not merely un-hidden: display:grid with real dimensions is
        # what "the menu works" means to the person clicking it.
        box = self.page.eval_on_selector(f'{row} .chat-menu',
                                         "el => el.getBoundingClientRect().toJSON()")
        self.assertGreater(box["width"], 100, f"menu has no width: {box}")
        self.assertGreater(box["height"], 50, f"menu has no height: {box}")
        self.assertEqual(self.errors, [])

    def test_the_menu_offers_the_row_actions(self):
        """Guards the menu's contents, so an empty popover cannot pass the
        open/size assertions above."""
        chat_id = self._seed_chat()
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"]'
        self.page.click(f'{row} button[data-action="menu"]')
        self.page.wait_for_timeout(400)

        actions = self.page.eval_on_selector_all(
            f'{row} .chat-menu button', "els => els.map(e => e.dataset.action)")
        for expected in ("rename", "fork", "export", "archive", "delete"):
            self.assertIn(expected, actions, f"{expected} missing from {actions}")

    def test_a_second_click_closes_it_again(self):
        """The toggle must still toggle: the fix must not turn 'always closed'
        into 'cannot be closed'."""
        chat_id = self._seed_chat()
        trigger = (f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"] '
                   f'button[data-action="menu"]')
        self.page.click(trigger)
        self.page.wait_for_timeout(400)
        self.assertEqual(
            self.page.eval_on_selector_all(".chat-menu.open", "els => els.length"), 1,
            "precondition: it opened",
        )

        self.page.click(trigger)
        self.page.wait_for_timeout(400)
        self.assertEqual(
            self.page.eval_on_selector_all(".chat-menu.open", "els => els.length"), 0,
            "clicking the ⋯ again must close the menu",
        )

    def test_clicking_elsewhere_closes_it(self):
        chat_id = self._seed_chat()
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"]'
        self.page.click(f'{row} button[data-action="menu"]')
        self.page.wait_for_timeout(400)
        self.assertEqual(
            self.page.eval_on_selector_all(".chat-menu.open", "els => els.length"), 1,
            "precondition: it opened",
        )

        self.page.click("#topbarTitle")
        self.page.wait_for_timeout(400)
        self.assertEqual(
            self.page.eval_on_selector_all(".chat-menu.open", "els => els.length"), 0,
            "a click outside .chat-actions must dismiss the menu",
        )

    def test_a_low_row_on_a_phone_opens_its_menu_on_screen(self):
        """The second half of the same report, and the half a desktop viewport
        cannot see.

        Measured at 390x680 before the fix: a row near the bottom of the
        sidebar opened its menu at y=637 with a height of 391, ending at 1028
        -- 348px below the fold. The menu was open and entirely off screen,
        which to the person tapping it is the same as broken. `.chat-menu` is
        now height-capped and chat-list.js flips it above the trigger when
        there is more room there.
        """
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        target = f"low-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        # Filler above it, so the row of interest sits low in the list. Ordering
        # is by recency, so the target is inserted last to land at the bottom.
        for i in range(8):
            con.execute(
                "INSERT INTO chats (id,title,description,work_dir,owner_id,"
                "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
                (f"{target}-f{i}", f"Filler {i}", stamp, stamp),
            )
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (target, "LowRowChat", stamp, stamp),
        )
        con.commit()
        con.close()

        self.page.set_viewport_size({"width": 390, "height": 680})
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(800)
        self.page.click("#menuBtn")
        self.page.wait_for_timeout(600)

        row = f'#chatList .chat-item[data-chat-id="{target}"]'
        self.page.wait_for_selector(row, timeout=20_000)
        self.page.click(f'{row} button[data-action="menu"]')
        self.page.wait_for_timeout(500)

        box = self.page.eval_on_selector(
            f'{row} .chat-menu', "el => el.getBoundingClientRect().toJSON()")
        viewport = self.page.viewport_size
        self.assertGreater(box["height"], 50, f"the menu did not render: {box}")
        self.assertLessEqual(
            round(box["bottom"]), viewport["height"] + 1,
            f"the menu ends {round(box['bottom']) - viewport['height']}px below "
            f"the fold on a {viewport['width']}x{viewport['height']} screen -- "
            f"it is open but cannot be reached: {box}",
        )
        self.assertGreaterEqual(
            round(box["top"]), -1,
            f"flipping it up pushed it off the top instead: {box}",
        )


# Both guards, matching every other class in this file. Without them this
# suite raised FileNotFoundError for each of its tests on a host with no
# playwright driver while the guarded suites skipped cleanly -- the exact
# failure tests/test_qa_browser_fixture.py exists to catch, and it was
# catching it.
@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class SupervisorMapBrowserTests(_BrowserFixture):
    """Browser tests for the supervisor map panel."""

    def _load(self):
        """Open the page on the test server — must login first."""
        self._login()
        self.page.goto(f"{self.base}", timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.wait_for_timeout(3000)  # let ES module listeners attach

    def test_panel_opens_closes(self):
        """Clicking the map icon opens the panel, close hides it."""
        self._load()
        self.page.wait_for_timeout(2000)  # let ES module listeners attach

        # Collect console messages and errors
        console_msgs = []
        self.page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
        self.errors.clear()

        btn = self.page.query_selector("#supervisorMapBtn")
        self.assertIsNotNone(btn, "supervisorMapBtn must exist")

        # Dispatch click directly to avoid Playwright click queuing issues
        self.page.evaluate("document.getElementById('supervisorMapBtn').click()")
        self.page.wait_for_timeout(4000)  # fetch + D3 render

        panel = self.page.query_selector("#supervisorMapPanel")
        self.assertIsNotNone(panel)

        is_hidden = panel.evaluate("el => el.hidden")
        if is_hidden:
            msgs = "\n".join(console_msgs[:10])
            self.fail(f"panel is still hidden. Page errors: {self.errors[:3]}. Console: {msgs}")

        close_btn = self.page.query_selector("#supervisorMapClose")
        self.assertIsNotNone(close_btn)
        close_btn.click()
        self.page.wait_for_timeout(300)

        self.assertTrue(panel.evaluate("el => el.hidden"),
                        "panel must be hidden after close")

    def test_zoom_controls_exist(self):
        """Zoom buttons are present in the panel header."""
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")

        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(2000)

        self.assertIsNotNone(
            self.page.query_selector("#mapFitBtn"),
            "fit-to-view button must exist",
        )
        self.assertIsNotNone(
            self.page.query_selector("#mapZoomOutBtn"),
            "zoom-out button must exist",
        )
        self.assertIsNotNone(
            self.page.query_selector("#mapZoomInBtn"),
            "zoom-in button must exist",
        )

    def test_drawer_element_exists(self):
        """The detail drawer DOM node is created."""
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")

        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(2000)

        self.assertIsNotNone(
            self.page.query_selector("#mapDetailDrawer"),
            "detail drawer must exist",
        )

    def test_svg_exists(self):
        """The D3 SVG container is created even when no agents exist."""
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")

        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(2000)

        self.assertIsNotNone(
            self.page.query_selector("#supervisorMapSvg"),
            "D3 SVG container must exist",
        )

    def test_no_script_errors(self):
        """Opening the map panel produces zero console errors."""
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.errors.clear()

        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(2000)

        self.assertEqual(self.errors, [],
                         f"supervisor map panel caused console errors: {self.errors}")

    def test_a_machine_node_shows_its_capacity_in_the_drawer(self):
        """Clicking a real machine node renders the pair the backend attaches
        to it -- not just that db_supervisor_map.py's dict has the keys
        (already pinned in tests/test_qa_supervisor_map.py), but that a click
        in a real browser reaches them through fetch, D3's data join, and
        showDetail()'s own string-building. No manual seeding needed: a fresh
        account already has one direct machine (see
        BackendsPanelBrowserTests.test_seeded_backend_renders_with_its_models),
        so its node is on the map as soon as the panel opens.
        """
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")

        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(2000)

        # Selects by node type, not by backend_kind()'s exact label string --
        # there is exactly one machine node on a fresh account, and depending
        # on that count is sturdier than depending on what that function
        # currently returns.
        machine_node = self.page.locator('g.node[aria-label^="machine"]').first
        machine_node.click()
        self.page.wait_for_timeout(300)

        meta = self.page.inner_text("#mapDetailMeta")
        self.assertIn(
            "Capacity:", meta,
            f"machine node detail did not show a capacity line: {meta!r}",
        )
        self.assertEqual(self.errors, [])

    def test_the_tree_is_rendered_at_a_visible_size(self):
        """The geometry regression this pins down, measured rather than
        reasoned about.

        d3.tree() was created without .size(), so it defaulted to size([1, 1])
        and every node's radius was a fraction of one unit: a machine node's
        own transform read `rotate(-61.35deg) translate(1,0)`. The whole tree
        rendered inside about one square pixel near the SVG's origin, and
        since the SVG carried no viewBox and the tree is laid out polar about
        (0,0), three of its four quadrants were at negative coordinates --
        outside the drawable area, with no working zoom or pan to bring them
        back. Every headless assertion still passed, because the elements
        existed and Playwright resolved them; only their rendered size was
        wrong.

        So this asserts pixels: nodes spread over a real distance, and each
        one lands inside the box the SVG occupies.
        """
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        box = self.page.evaluate(
            "() => document.getElementById('supervisorMapSvg')"
            ".getBoundingClientRect().toJSON()"
        )
        # The circles, not the `g.node` groups. A group's bounding box includes
        # its text label, which is tens of pixels wide whatever the layout
        # does -- measuring those made this test pass with the layout collapsed
        # to a single point, which is the exact regression it exists to catch.
        points = self.page.evaluate("""
          () => Array.from(
            document.querySelectorAll('#supervisorMapSvg g.node > circle')
          ).map(c => {
            const r = c.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2,
                    w: r.width};
          })
        """)
        self.assertGreaterEqual(
            len(points), 2, "a fresh account has a centre and at least one group",
        )
        for point in points:
            self.assertGreater(
                point["w"], 2,
                f"a node is {point['w']}px across -- on screen but too small "
                f"to click: {point}",
            )
            # 2px of tolerance: a stroke can sit a fraction outside the box.
            self.assertGreaterEqual(point["x"], box["left"] - 2, point)
            self.assertLessEqual(point["x"], box["right"] + 2, point)
            self.assertGreaterEqual(point["y"], box["top"] - 2, point)
            self.assertLessEqual(point["y"], box["bottom"] + 2, point)
        # Spread, not a cluster: with size() missing, every node sat within a
        # pixel or two of every other one.
        spread = max(
            abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])
            for a in points for b in points
        )
        self.assertGreater(
            spread, 40,
            f"the furthest two nodes are {spread:.1f}px apart -- the tree is "
            f"a dot, not a map: {points}",
        )

    def test_opening_the_map_does_not_blank_the_page(self):
        """`main.hidden = true` was a workaround for nodes that could not be
        clicked, commented as "main creates a stacking context that visually
        covers the panel's SVG nodes". The real causes were elsewhere -- the
        tree rendered about one pixel across, and the SVG had no viewBox -- so
        the workaround cost the whole conversation view for nothing.

        Both halves are asserted together on purpose: dropping it is only
        correct if nodes stay clickable without it, and asserting visibility
        alone would pass just as well with the map unusable underneath.
        """
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        self.assertFalse(
            self.page.evaluate("() => document.querySelector('main').hidden"),
            "opening the map hid the rest of the page",
        )
        self.page.locator('g.node[aria-label^="machine"]').first.click()
        self.page.wait_for_timeout(400)
        self.assertFalse(
            self.page.evaluate(
                "() => document.getElementById('mapDetailDrawer').hidden"
            ),
            "a node click did not reach the map with the page still visible",
        )

    def test_a_connection_error_does_not_outlive_the_failure(self):
        """The error text is written into the empty-state element, which
        nothing ever reset. One failed fetch and every later empty map read
        "Connection error." for the rest of the session -- reporting a
        connection problem that had already gone away."""
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")

        self.page.route("**/api/supervisor-map", lambda route: route.abort())
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(2000)
        self.assertEqual(
            self.page.inner_text("#mapStatusEmpty"), "Connection error.",
            "the failure path did not report the failure",
        )

        self.page.click("#supervisorMapClose")
        self.page.wait_for_timeout(300)
        self.page.unroute("**/api/supervisor-map")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        self.assertNotEqual(
            self.page.inner_text("#mapStatusEmpty"), "Connection error.",
            "the stale error message survived a successful load",
        )

    def test_the_map_refreshes_itself_but_not_under_an_open_drawer(self):
        """The map was fetched once on open and never again, so a panel whose
        whole job is showing what is running now went stale the moment it was
        drawn.

        The second half is the constraint that makes the first safe: a refresh
        rebuilds every node, so polling under an open drawer would replace the
        node the user is reading about mid-read. Both are asserted here
        because the interval is one mechanism -- splitting them would let a
        version that never polls at all pass the second test.
        """
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")

        hits = []
        self.page.on(
            "request",
            lambda r: hits.append(r.url) if "/api/supervisor-map" in r.url else None,
        )
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)
        after_open = len(hits)
        self.assertGreaterEqual(after_open, 1, "opening the map fetched nothing")

        # One poll interval is 10s; 13 covers it without depending on when in
        # the interval the click landed.
        self.page.wait_for_timeout(13_000)
        self.assertGreater(
            len(hits), after_open,
            "the map never refreshed itself while open",
        )

        self.page.locator('g.node[aria-label^="machine"]').first.click()
        self.page.wait_for_selector("#mapDetailDrawer", state="visible", timeout=5_000)
        with_drawer = len(hits)
        self.page.wait_for_timeout(13_000)
        self.assertEqual(
            len(hits), with_drawer,
            "the map refreshed under an open drawer, replacing the node being read",
        )

    def test_open_conversation_reaches_the_conversation(self):
        """The drawer was read-only: it could report a stuck conversation and
        offer no way to get to it. The button dispatches an event because
        app.js imports this module and importing selectChat back would be a
        cycle -- so this asserts the whole path, not the dispatch."""
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chat_id = f"map-{secrets.token_hex(4)}"
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at) VALUES (?,?,NULL,'/tmp','admin',?,?)",
            (chat_id, f"Map {chat_id}", stamp, stamp),
        )
        con.commit()
        con.close()

        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        node = self.page.locator(f'g.node[aria-label*="{chat_id}"]')
        if node.count() == 0:
            # Nodes are labelled by title, not id.
            node = self.page.locator(f'g.node[aria-label*="Map {chat_id}"]')
        node.first.click()
        self.page.wait_for_timeout(400)
        self.page.click("#mapDetailOpen")
        self.page.wait_for_timeout(1500)

        self.assertTrue(
            self.page.evaluate(
                "() => document.getElementById('supervisorMapPanel').hidden"
            ),
            "the map stayed open after asking to open a conversation",
        )
        self.assertEqual(self.errors, [])

    def test_stop_posts_to_the_conversation_and_reports_what_happened(self):
        """The Stop button's own request path.

        The tree is served from a stub rather than seeded, because the button
        only appears for a conversation whose turn is running -- and a running
        turn lives in the server's memory, not in a table a test can write to.
        Stubbing the tree keeps this test about the button; which nodes get
        the button at all is pinned in
        tests/test_qa_supervisor_map_geometry.py.
        """
        tree = json.dumps({
            "center": "You",
            "children": [{
                "id": "direct", "label": "Direct", "status": "running",
                "type": "transport",
                "children": [{
                    "id": "chat-running", "label": "Busy one",
                    "status": "running", "type": "chat",
                }],
            }],
        })
        self.page.route(
            "**/api/supervisor-map",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=tree,
            ),
        )
        posted = []

        def stop_handler(route):
            posted.append(route.request.url)
            route.fulfill(
                status=200, content_type="application/json",
                body='{"ok": true, "stopped": true, "held": 0}',
            )

        self.page.route("**/api/chats/*/stop", stop_handler)

        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        self.page.locator('g.node[aria-label*="Busy one"]').first.click()
        self.page.wait_for_selector("#mapDetailStop", state="visible", timeout=5_000)
        self.page.click("#mapDetailStop")
        self.page.wait_for_timeout(800)

        self.assertEqual(len(posted), 1, f"stop requests: {posted}")
        self.assertIn("/api/chats/chat-running/stop", posted[0])
        self.assertEqual(
            self.page.inner_text("#mapDetailActionStatus"), "Stopped.",
        )
        self.assertEqual(self.errors, [])

    def test_stop_does_not_claim_a_turn_that_had_already_finished(self):
        """`stopped: false` means the turn ended between the map being drawn
        and the button being pressed. Reporting "Stopped." there would claim
        an action that did not happen."""
        tree = json.dumps({
            "center": "You",
            "children": [{
                "id": "direct", "label": "Direct", "status": "running",
                "type": "transport",
                "children": [{
                    "id": "chat-done", "label": "Already finished",
                    "status": "running", "type": "chat",
                }],
            }],
        })
        self.page.route(
            "**/api/supervisor-map",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=tree,
            ),
        )
        self.page.route(
            "**/api/chats/*/stop",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body='{"ok": true, "stopped": false, "held": 0}',
            ),
        )

        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        self.page.locator('g.node[aria-label*="Already finished"]').first.click()
        self.page.wait_for_selector("#mapDetailStop", state="visible", timeout=5_000)
        self.page.click("#mapDetailStop")
        self.page.wait_for_timeout(800)

        self.assertEqual(
            self.page.inner_text("#mapDetailActionStatus"), "Nothing was running.",
        )

    def test_the_nodes_follow_the_theme(self):
        """The map was the one SVG here that ignored the theme: its status
        colours, its neutral machine fill and every node outline were
        literals, and the outline literal was `#fff` -- a white ring on a
        white panel in light mode.

        Asserted in a browser because the JS falls back to those same
        literals when a variable is unset, so a stylesheet that never defined
        them would leave every headless test passing and the light theme
        broken. This resolves the variables for real.
        """
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        read_stroke = (
            "() => { const c = document.querySelector("
            "'#supervisorMapSvg g.node circle[stroke]'); "
            "return c ? getComputedStyle(c).stroke : null; }"
        )
        dark = self.page.evaluate(read_stroke)
        self.assertIsNotNone(dark, "no outlined node circle to measure")

        self.page.click("#supervisorMapClose")
        self.page.wait_for_timeout(300)
        self.page.click("#themeToggle")
        self.page.wait_for_timeout(300)
        self.assertEqual(
            self.page.evaluate("() => document.documentElement.dataset.theme"),
            "light", "the toggle did not reach the light theme",
        )
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        light = self.page.evaluate(read_stroke)
        self.assertNotEqual(
            light, dark,
            f"node outlines are identical in both themes ({dark}) -- the map "
            f"is still drawing hardcoded colours",
        )

    def test_the_zoom_in_button_moves_the_view(self):
        """d3.zoom() was built with no `.on("zoom", ...)` handler, so nothing
        was subscribed to the transform it computed and every zoom control was
        inert. Asserted through the DOM the browser actually rendered, because
        the button, the zoom behaviour and the transform all existed before --
        what was missing was the one line connecting them."""
        self._load()
        self.page.wait_for_selector("#orchestratorBtn")
        self.page.click("#supervisorMapBtn")
        self.page.wait_for_timeout(3000)

        read = ("() => { const g = document.querySelector('#supervisorMapSvg "
                "g.map-viewport'); return g ? g.getAttribute('transform') : null; }")
        before = self.page.evaluate(read)
        self.assertIsNotNone(
            before, "there is no map-viewport group for zoom to transform",
        )

        self.page.click("#mapZoomInBtn")
        self.page.wait_for_timeout(1000)

        self.assertNotEqual(
            self.page.evaluate(read), before,
            "clicking zoom in left the viewport transform untouched",
        )
        self.assertEqual(self.errors, [])


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class TransportStatsPanelTests(_BrowserFixture):
    """The Server tab's transport table and charts, in a real browser.

    Worth a browser test rather than only a payload test: the whole feature is
    that opening the tab reads stored rows instead of collecting anything, and
    "did it render without collecting" is a property of the page, not of the
    endpoint. A fresh account has transports seeded by the fixture but no
    stored samples, which is also the case most likely to be got wrong -- a
    missing reading must print as "--", never as 0.
    """

    def test_the_consoles_own_host_leads_the_page(self):
        """Order asserted, because it was wrong once and reversing it is
        invisible to every other test here. This tab is opened to read the
        machine you are on; the transports follow, under the range and bucket
        controls that govern both."""
        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_selector("#serverBody", timeout=15_000)

        headings = self.page.locator(
            "#panelServer .server-section-heading").all_inner_texts()
        self.assertGreaterEqual(len(headings), 2, headings)
        self.assertIn("console", headings[0].lower(), headings)
        self.assertIn("transport", headings[1].lower(), headings)

        # And in document order, not merely in heading order: the host's own
        # charts must sit above the transport table.
        order = self.page.evaluate("""
          () => {
            const body = document.getElementById('serverBody');
            const table = document.getElementById('transportStats');
            if (!body || !table) return null;
            return body.compareDocumentPosition(table)
              & Node.DOCUMENT_POSITION_FOLLOWING ? 'host-first' : 'transports-first';
          }
        """)
        self.assertEqual(order, "host-first")

    def test_the_timeframe_controls_lead_the_panel(self):
        """First in the panel, and sticky. Position asserted by document
        order rather than by looking at the rendered offset: the panel is
        inside a scrolling container, and a control that is merely visually
        near the top is not the same as one the reader cannot lose."""
        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_selector("#serverTimeframe", timeout=15_000)

        # The FIRST element child, not the first one that happens to carry an
        # id. The initial version of this skipped id-less nodes, so moving the
        # toolbar below the heading and description still passed -- they have
        # no ids, and the toolbar was still the first thing with one.
        first = self.page.evaluate("""
          () => {
            const panel = document.getElementById('panelServer');
            const first = panel.firstElementChild;
            return first ? (first.id || first.tagName.toLowerCase()) : null;
          }
        """)
        self.assertEqual(
            first, "serverTimeframe",
            "the time-frame controls are not the first thing in the panel",
        )
        self.assertEqual(
            self.page.evaluate(
                "() => getComputedStyle("
                "document.getElementById('serverTimeframe')).position"),
            "sticky",
        )

    def test_changing_the_timeframe_redraws_the_transport_charts(self):
        """The controls govern every chart, not only the host's above them.

        Asserted through the request the change makes: both chart sets are
        built from one /api/system/series response, so if the new range does
        not reach that request the transports keep the old window while the
        host gets the new one -- two graphs side by side over different
        spans, which is worse than either being wrong.
        """
        import datetime
        import sqlite3
        now = datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO ssh_transports (id,name,owner_id,ssh_host,ssh_user,"
            "ssh_key_path,ssh_host_key_fingerprint,remote_path,created_at,"
            "updated_at) VALUES ('t-ranged','Ranged Node','admin',"
            "'ranged.example','kali','~/.ssh/id_ed25519','','~/wc-proxy',?,?)",
            (stamp, stamp),
        )
        for minutes in range(0, 50, 10):
            when = (now - datetime.timedelta(minutes=minutes)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            con.execute(
                "INSERT INTO system_samples (created_at,host_type,host_id,"
                "cpu_pct,mem_pct,disk_pct,load1,load5,load15) "
                "VALUES (?,'transport','t-ranged',?,?,?,?,?,?)",
                (when, 30, 40, 50, 0.5, 0.4, 0.3),
            )
        con.commit()
        con.close()

        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_selector("#transportCharts .transport-chart-block",
                                    timeout=15_000)

        series_calls = []
        self.page.on(
            "request",
            lambda r: series_calls.append(r.url)
            if "/api/system/series" in r.url else None,
        )
        self.page.select_option("#serverRange", "7")
        self.page.wait_for_timeout(2500)

        self.assertTrue(
            series_calls, "changing the range made no series request at all")
        self.assertTrue(
            any("days=7" in url for url in series_calls),
            f"the new range never reached the series request: {series_calls}",
        )

        # Both controls, not just the range. The first version of this test
        # exercised only the range, so deleting the bucket select's own
        # listener left every chart on a stale slot width and the test passed.
        series_calls.clear()
        self.page.select_option("#serverBucket", "day")
        self.page.wait_for_timeout(2500)
        self.assertTrue(
            any("bucket=day" in url for url in series_calls),
            f"the new bucket never reached the series request: {series_calls}",
        )
        # And the transport charts are still there afterwards -- a redraw that
        # drops them is not a redraw.
        self.assertGreaterEqual(
            self.page.locator(
                "#transportCharts .transport-chart-block").count(), 1)
        self.assertEqual(self.errors, [])

    def test_the_table_lists_transports_without_collecting_anything(self):
        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)

        calls = []
        self.page.on(
            "request",
            lambda r: calls.append(r.url) if "/api/system" in r.url else None,
        )
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_timeout(3000)

        self.assertTrue(calls, "the Server tab never asked for host stats")
        # One read of stored rows, not one SSH round-trip per transport.
        self.assertLessEqual(len(calls), 2, calls)

        rendered = self.page.inner_text("#transportStats")
        self.assertTrue(rendered.strip(), "the transport table rendered empty")
        # Either the header row (transports exist) or the explicit empty note.
        self.assertTrue(
            "Transport" in rendered or "No SSH transports" in rendered,
            f"unexpected transport table content: {rendered[:200]!r}",
        )
        self.assertEqual(self.errors, [])

    def test_a_transport_with_history_gets_its_own_charts(self):
        """The graphs, and that they are the host's graphs rather than a
        lighter lookalike: the same lineChart figure element, so the two sets
        can be read against each other."""
        import datetime
        import sqlite3
        now = datetime.datetime.now(datetime.UTC)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO ssh_transports (id,name,owner_id,ssh_host,ssh_user,"
            "ssh_key_path,ssh_host_key_fingerprint,remote_path,created_at,"
            "updated_at) VALUES ('t-charted','Charted Node','admin',"
            "'charted.example','kali','~/.ssh/id_ed25519','','~/wc-proxy',?,?)",
            (stamp, stamp),
        )
        # Several samples across the window, so the series has something to
        # draw rather than a single point.
        for minutes in range(0, 50, 10):
            when = (now - datetime.timedelta(minutes=minutes)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            con.execute(
                "INSERT INTO system_samples (created_at,host_type,host_id,"
                "cpu_pct,mem_pct,disk_pct,load1,load5,load15) "
                "VALUES (?,'transport','t-charted',?,?,?,?,?,?)",
                (when, 20 + minutes, 40 + minutes, 55, 0.5, 0.4, 0.3),
            )
        con.commit()
        con.close()

        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_selector("#transportCharts .transport-chart-block",
                                    timeout=15_000)

        block = self.page.locator(
            "#transportCharts .transport-chart-block", has_text="Charted Node").first
        self.assertEqual(
            block.locator(".transport-chart-title").inner_text(), "Charted Node")
        # The same figure element the host's own charts use.
        figures = block.locator("figure.stat-figure")
        self.assertGreaterEqual(
            figures.count(), 4,
            "expected CPU, memory, disk and load charts for the transport",
        )
        titles = block.inner_text()
        for expected in ("CPU over time", "Memory over time", "Disk over time",
                         "Load average over time"):
            self.assertIn(expected, titles)
        self.assertEqual(self.errors, [])

    def test_a_transport_with_no_history_gets_no_empty_axes(self):
        """An empty pair of axes says "we measured nothing" in the same shape
        a real measurement uses. The table's "--" already says it better."""
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO ssh_transports (id,name,owner_id,ssh_host,ssh_user,"
            "ssh_key_path,ssh_host_key_fingerprint,remote_path,created_at,"
            "updated_at) VALUES ('t-silent','Silent Node','admin',"
            "'silent.example','kali','~/.ssh/id_ed25519','','~/wc-proxy',?,?)",
            (stamp, stamp),
        )
        con.commit()
        con.close()

        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_selector("#transportStats .transport-stat-row",
                                    timeout=10_000)
        self.page.wait_for_timeout(1500)

        charts = self.page.locator(
            "#transportCharts .transport-chart-block", has_text="Silent Node")
        self.assertEqual(charts.count(), 0)
        # But it is still in the table, saying so.
        self.assertIn("Silent Node", self.page.inner_text("#transportStats"))

    def test_a_transport_with_no_reading_shows_a_dash_not_a_zero(self):
        """A 0 here reads as an idle host, which is precisely what the broken
        collector made every transport look like for a day."""
        import datetime
        import sqlite3
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        con = sqlite3.connect(str(Path(self.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO ssh_transports (id,name,owner_id,ssh_host,ssh_user,"
            "ssh_key_path,ssh_host_key_fingerprint,remote_path,created_at,"
            "updated_at) VALUES ('t-quiet','Quiet Node','admin','quiet.example',"
            "'kali','~/.ssh/id_ed25519','','~/wc-proxy',?,?)",
            (stamp, stamp),
        )
        con.commit()
        con.close()

        self._login()
        self.page.goto(self.base, timeout=10_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#settingsBtn", timeout=15_000)
        self.page.click("#settingsBtn")
        self.page.click('[data-tab="server"]')
        self.page.wait_for_selector("#transportStats .transport-stat-row",
                                    timeout=10_000)

        row = self.page.locator(
            "#transportStats .transport-stat-row", has_text="Quiet Node").first
        text = row.inner_text()
        self.assertIn("--", text, f"no-reading cells did not render a dash: {text!r}")
        self.assertIn("never", text)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
