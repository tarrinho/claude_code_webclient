"""QA: a failing browser test must not take the rest of the suite with it.

The browser fixture acquires playwright, a browser and a page in setUp. When
those were released in tearDown, a setUp that raised -- a slow boot, a login
selector that never appeared -- skipped tearDown and leaked playwright. That is
not merely untidy: the sync API drives its asyncio loop through greenlets, so a
playwright that is never stopped leaves that loop flagged as the *running* loop
for the thread. Every unittest.IsolatedAsyncioTestCase afterwards then dies on
"Runner.run() cannot be called from a running event loop". One login timeout
turned 1350 passing tests into 865 failures that way, and the report blamed the
async tests rather than the browser test that actually broke.

These tests drive the fixture with a stub playwright, so they need neither a
browser nor the dev extras.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import tests.test_frontend_browser as fb


class _Case(fb._BrowserFixture):
    """A concrete case so setUp can be driven; setUpClass is never called.

    setUp checks the server is still alive before touching the browser, so it
    needs one here -- setUpClass, which would spawn the real uvicorn, is
    deliberately skipped. poll() returning None is "still running".
    """

    # pytest collects every TestCase subclass, ignoring python_classes, so
    # without this it ran _Case as a test: setUpClass spawned a real uvicorn
    # and rebound `server` to it, and the stub below never applied.
    __test__ = False

    server = MagicMock(**{"poll.return_value": None})

    def runTest(self):  # pragma: no cover - never executed
        pass


def _fixture(login):
    """A fixture instance wired to a stub playwright, plus that stub."""
    case = _Case()
    pw = MagicMock()
    with patch.object(fb, "sync_playwright", return_value=pw), \
            patch.object(_Case, "_login", login):
        try:
            case.setUp()
        except RuntimeError:
            pass
    return case, pw.start.return_value


class CleanupOnSetUpFailureTests(unittest.TestCase):
    @staticmethod
    def _boom(_self):
        raise RuntimeError("login timed out")

    def test_playwright_is_stopped_when_login_fails(self):
        """The whole point: no leaked loop, so later async tests still run."""
        case, pw = _fixture(self._boom)
        case.doCleanups()
        pw.stop.assert_called_once()

    def test_the_browser_is_closed_when_login_fails(self):
        case, pw = _fixture(self._boom)
        case.doCleanups()
        pw.chromium.launch.return_value.close.assert_called_once()

    def test_the_browser_closes_before_playwright_stops(self):
        """Cleanups run last-in-first-out; stopping first orphans the browser."""
        case, pw = _fixture(self._boom)
        order: list[str] = []
        pw.chromium.launch.return_value.close.side_effect = \
            lambda: order.append("close")
        pw.stop.side_effect = lambda: order.append("stop")
        case.doCleanups()
        self.assertEqual(order, ["close", "stop"])

    def test_a_successful_setup_still_releases_everything(self):
        case, pw = _fixture(lambda _self: None)
        case.doCleanups()
        pw.stop.assert_called_once()
        pw.chromium.launch.return_value.close.assert_called_once()


class SkipGuardTests(unittest.TestCase):
    """Every browser suite must skip, not error, where it cannot run.

    The guards are per-class decorators, so a new suite added to that file gets
    none of them by default -- which is a silent trap, because the machine that
    adds the class almost always has a browser. On a box without node,
    playwright's driver is missing and an unguarded class raises
    FileNotFoundError for every test in it while the guarded ones skip cleanly.
    That happened: the five tests added for the Server panel errored on a peer's
    machine while the other twenty-six skipped.
    """

    def _suites(self):
        return [
            (name, obj) for name, obj in vars(fb).items()
            if isinstance(obj, type)
            and issubclass(obj, fb._BrowserFixture)
            and obj is not fb._BrowserFixture
        ]

    def test_there_are_suites_to_check(self):
        """A find-nothing bug here would make every assertion below vacuous."""
        self.assertGreater(len(self._suites()), 1)

    def test_every_browser_suite_is_guarded(self):
        for name, suite in self._suites():
            with self.subTest(suite=name):
                # unittest records a class-level skip as these two attributes.
                self.assertTrue(
                    getattr(suite, "__unittest_skip__", False)
                    or not fb.DRIVER_OK
                    or fb.CHROMIUM is None
                    or _decorated(suite),
                    f"{name} runs unguarded: on a machine without playwright's "
                    "driver or a browser it errors instead of skipping",
                )


def _decorated(suite) -> bool:
    """Whether the class carries the guards, on a machine where they pass.

    skipUnless/skipIf only set __unittest_skip__ when the condition actually
    fires, so on a working machine a guarded and an unguarded class look
    identical at runtime. The source is the only place the difference survives.
    """
    import inspect
    source = inspect.getsource(fb)
    marker = f"class {suite.__name__}("
    head = source[:source.index(marker)]
    tail = head[head.rindex("\n\n"):] if "\n\n" in head else head
    return "DRIVER_OK" in tail and "CHROMIUM is None" in tail


class NoTearDownRelianceTests(unittest.TestCase):
    def test_the_fixture_defines_no_teardown_of_its_own(self):
        """A tearDown here would be the bug returning: it is skipped on failure."""
        self.assertNotIn("tearDown", vars(fb._BrowserFixture))

    def test_the_server_is_released_even_when_setupclass_raises(self):
        """setUpClass raises after Popen, and tearDownClass does not run then.

        Not addClassCleanup: unittest shares one class-cleanup list across every
        TestCase, so an entry registered by one class can be drained while
        another is still using its server -- which killed a live one mid-class.
        setUpClass owns the failure path itself instead.
        """
        class _Boom(fb._BrowserFixture):
            __test__ = False

            @classmethod
            def _start(cls):
                cls.server = MagicMock()
                raise RuntimeError("server did not start")

        with self.assertRaises(RuntimeError):
            _Boom.setUpClass()
        # Released on the way out, rather than left for a cleanup list that
        # another class might drain at the wrong moment.
        self.assertIsNone(_Boom.server)
        self.assertIn("tearDownClass", vars(fb._BrowserFixture))

    def test_release_is_idempotent_and_survives_a_partly_built_class(self):
        """It runs from the setUpClass failure path, where these may be None."""
        class _Partial(fb._BrowserFixture):
            __test__ = False
            tmp = None
            server = None
            log_handle = None

        _Partial._release()
        _Partial._release()


if __name__ == "__main__":
    unittest.main()
