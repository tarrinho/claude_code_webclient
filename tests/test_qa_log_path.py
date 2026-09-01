"""QA: a test server must not write into the production log.

logging.conf names the log file with an absolute path, so every server the
suite spawns inherited it and appended to logs/webconsole.log. The production
log ended up carrying interleaved lines from processes nobody was watching,
including future-stamped ones from tests that fake a clock. That is worse than
untidy: the log is the first artefact anyone opens during an incident, and it
was actively misleading about ordering -- it cost real time during the write
outage in registry #41 before the entries were recognised as foreign.

The path is now resolved by config.LOG_FILE and interpolated into the handler's
args through fileConfig's ``defaults``. That mechanism was chosen over
overriding the handler after the fact because it needs no second fileConfig
call: fileConfig closes every existing handler, which is what failed 794 tests
in registry #30.

Anything that resolves config or configures logging runs in a subprocess here,
for that same reason and because config reads the environment at import.
"""
from __future__ import annotations

import ast
import configparser
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "logging.conf"
PRODUCTION_LOG = ROOT / "logs" / "webconsole.log"


def resolve(env: dict[str, str] | None = None) -> str:
    """config.LOG_FILE as a freshly-imported config would resolve it."""
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.LOG_FILE)"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        check=True,
    )
    return result.stdout.strip()


class ResolutionTests(unittest.TestCase):
    def test_unset_env_resolves_to_the_production_path(self):
        """The case that matters most.

        A test writing somewhere odd is a nuisance. A *deployment* quietly
        logging where nobody looks is the real failure, and it is silent --
        the same shape as a health check pointed at the wrong database.
        """
        self.assertEqual(resolve(), str(PRODUCTION_LOG))

    def test_the_env_var_overrides_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "elsewhere.log"
            self.assertEqual(resolve({"WC_LOG_FILE": str(target)}), str(target))

    def test_the_parent_directory_is_created(self):
        """RotatingFileHandler will not create it, and fails at boot if absent.

        That failure surfaces as the server exiting during setUpClass, which is
        a confusing way to discover a missing directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "deeper" / "wc.log"
            resolve({"WC_LOG_FILE": str(target)})
            self.assertTrue(target.parent.is_dir())


class ConfWiringTests(unittest.TestCase):
    def test_the_conf_uses_the_token_rather_than_a_hardcoded_path(self):
        source = CONF.read_text(encoding="utf-8")
        self.assertIn("%(logfile)s", source)
        self.assertNotIn(str(PRODUCTION_LOG), source,
                         "the absolute path is back in the config file")

    def test_the_rotation_policy_is_still_declared(self):
        """Interpolating the filename must not lose the rotation arguments."""
        parser = configparser.ConfigParser()
        parser.read(CONF)
        # raw=True: the value now holds %(logfile)s, which a plain get() would
        # try to interpolate and fail on, since the token is supplied at
        # configure time rather than written in the file.
        args = ast.literal_eval(parser.get("handler_file", "args", raw=True))
        self.assertEqual(len(args), 4)
        self.assertEqual(args[0], "%(logfile)s")
        self.assertGreater(args[2], 0)
        self.assertGreater(args[3], 0)

    def test_app_supplies_the_resolved_path_as_a_default(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("defaults=", source)
        self.assertIn("config.LOG_FILE", source)

    def test_there_is_still_only_one_fileconfig_call(self):
        """A second call would close the first's handlers -- registry #30."""
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("logging.config.fileConfig("), 1)


class EndToEndTests(unittest.TestCase):
    """Configure logging for real, in a subprocess, and read the file back."""

    def _run(self, target: Path, marker: str = "xyz") -> subprocess.CompletedProcess:
        probe = (
            "import app, logging; "
            "app._configure_logging(); "
            f"logging.getLogger('wc.app').info('landed chat_id=%s', {marker!r}); "
            "logging.shutdown()"
        )
        return subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT,
            capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin", "WC_LOG_FILE": str(target)},
        )

    def test_records_land_where_the_env_var_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wc.log"
            result = self._run(target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file(), f"nothing written: {result.stderr}")
            self.assertIn("landed chat_id=xyz", target.read_text())

    def test_nothing_reaches_the_production_log(self):
        """The whole point, asserted directly rather than inferred.

        By marker, not by file size. This compared ``PRODUCTION_LOG.stat()``
        before and after, which measures *every* writer: the live server, the
        health timer and five other sessions all append to that file, so on the
        machine this project actually runs on the case failed for traffic that
        had nothing to do with the probe. It passed only on an idle box —
        registry #36's shape, where a test's verdict tracks the machine's mood
        rather than the code.

        A unique marker asserts the property the test is named for: *this*
        redirected process's records did not land in the production log. It is
        immune to concurrent writers, and it can still fail — remove the
        ``WC_LOG_FILE`` plumbing and the marker appears there.
        """
        marker = f"probe-{uuid.uuid4().hex}"
        before = ""
        if PRODUCTION_LOG.exists():
            before = PRODUCTION_LOG.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(marker, before, "the marker was not unique")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wc.log"
            result = self._run(target, marker=marker)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Prove the probe actually logged, or "absent from production" is
            # satisfied by a probe that wrote nothing anywhere.
            self.assertIn(marker, target.read_text(encoding="utf-8"),
                          "the probe never logged, so this proves nothing")

        after = ""
        if PRODUCTION_LOG.exists():
            after = PRODUCTION_LOG.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(
            marker, after,
            "a redirected server still appended to the real log",
        )

    def test_the_formatter_tokens_survive_interpolation(self):
        """%(asctime)s must not be eaten by the same interpolation pass.

        fileConfig reads format strings with raw=True, so it is not -- but the
        failure would be silent and cosmetic-looking, and a log without
        timestamps is exactly the log you cannot reconstruct an incident from.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wc.log"
            self._run(target)
            first = target.read_text().splitlines()[0]
            self.assertRegex(first, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")
            self.assertIn("wc.app", first)


class InProcessRedirectionTests(unittest.TestCase):
    """The other half of the leak, which the subprocess fix never covered.

    Four modules drive the app with an in-process TestClient. That imports
    ``app`` inside the pytest process and configures logging there, so it never
    saw the ``WC_LOG_FILE`` the browser fixture passes to its subprocess, and
    kept appending to the production log -- ``ip=testclient``, ``user=alice``,
    a stored-XSS probe -- for as long as the fix was believed complete.

    Asserted in-process on purpose. Every other case here shells out, which is
    exactly why they all passed while this route leaked: a subprocess with a
    controlled environment cannot observe what the test process itself does.
    """

    def test_this_process_does_not_log_to_production(self):
        import config
        self.assertNotEqual(
            config.LOG_FILE, str(PRODUCTION_LOG),
            "tests/conftest.py is missing or ran too late; in-process tests are "
            "appending to the production log",
        )

    def test_the_conftest_sets_it_before_config_is_imported(self):
        """A fixture cannot do this -- config binds LOG_FILE at import."""
        conftest = (ROOT / "tests" / "conftest.py")
        self.assertTrue(conftest.is_file(), "the redirect hook is gone")
        self.assertIn("WC_LOG_FILE", conftest.read_text(encoding="utf-8"))

    def test_an_explicit_setting_still_wins(self):
        """CI or a harness may point the log somewhere it collects from."""
        source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        self.assertIn('if not os.environ.get("WC_LOG_FILE")', source)


class FixtureRedirectionTests(unittest.TestCase):
    """The suite's own servers are the processes that were polluting the log."""

    def test_the_browser_fixture_redirects_its_server(self):
        source = (ROOT / "tests" / "test_frontend_browser.py").read_text()
        self.assertIn("WC_LOG_FILE", source)

    def test_it_points_inside_its_own_temp_directory(self):
        """Anywhere else and the directory may not exist when uvicorn starts."""
        source = (ROOT / "tests" / "test_frontend_browser.py").read_text()
        line = next(ln for ln in source.splitlines() if "WC_LOG_FILE" in ln)
        self.assertIn("tmp", line, line.strip())


if __name__ == "__main__":
    unittest.main()
