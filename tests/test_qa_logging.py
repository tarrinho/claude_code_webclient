"""QA coverage for the application log actually being written.

Every ``_log.info`` in the codebase was silently discarded for the whole life
of the project. The server runs as ``python3 -m uvicorn app:app``, so app.py is
imported rather than executed, and the ``logging.basicConfig`` call sat inside
``if __name__ == "__main__"``. Uvicorn configures only its own loggers, which
left root with no handler, and logging's last-resort fallback emits WARNING and
above -- so every INFO record vanished.

Nothing looked wrong from outside: logs/webconsole.log kept growing, because it
was the shell's stdout redirect collecting uvicorn's access lines. It held not
one wc.* record, so the login, chat-creation and turn-timeout logging that §3
(A09) of rules.md requires did not exist.

Covers: that importing app configures the wc.* loggers, that an INFO record
survives the trip, that logging.conf is honoured when present, and that a
broken config degrades to a working handler instead of stopping the server.
"""
from __future__ import annotations

import configparser
import logging
import logging.config
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class Collector(logging.Handler):
    """Records what actually reached a handler, rather than what was emitted."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class ImportTimeConfigurationTests(unittest.TestCase):
    """The configuration has to happen on import; nothing runs __main__."""

    def test_the_wc_logger_can_emit_at_info(self):
        """The failure was INFO being below the effective level, so check that."""
        self.assertTrue(
            logging.getLogger("wc.app").isEnabledFor(logging.INFO),
            "wc.app must accept INFO or every _log.info call is discarded",
        )

    def test_a_handler_exists_to_receive_the_record(self):
        """isEnabledFor alone is not enough -- a record still needs somewhere to go."""
        logger = logging.getLogger("wc.app")
        found = []
        while logger:
            found.extend(logger.handlers)
            if not logger.propagate:
                break
            logger = logger.parent
        self.assertTrue(found, "no handler on wc.app or any parent: records go nowhere")

    def test_an_info_record_reaches_a_handler(self):
        """The property that actually matters, asserted end to end."""
        collector = Collector()
        logger = logging.getLogger("wc.app")
        logger.addHandler(collector)
        try:
            logger.info("probe chat_id=%s", "abc")
        finally:
            logger.removeHandler(collector)
        self.assertEqual(len(collector.records), 1)
        self.assertEqual(collector.records[0].getMessage(), "probe chat_id=abc")

    def test_configuring_is_not_hidden_behind_main(self):
        """A fresh interpreter that only imports app must still be configured.

        This is the regression proper: the old code configured logging inside
        ``if __name__ == "__main__"``, which an import never reaches.
        """
        probe = (
            "import logging, app; "
            "lg = logging.getLogger('wc.app'); "
            "print(lg.isEnabledFor(logging.INFO), "
            "bool(lg.handlers or logging.getLogger().handlers))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("True True", result.stdout,
                      f"import alone left logging unconfigured: {result.stdout!r}")


class LoggingConfFileTests(unittest.TestCase):
    """logging.conf holds the rotation policy, so it must be the source used."""

    def test_the_repo_ships_a_readable_config(self):
        conf = REPO / "logging.conf"
        self.assertTrue(conf.is_file(), "logging.conf is referenced by _configure_logging")
        parser = configparser.ConfigParser()
        parser.read(conf)
        self.assertIn("handler_file", parser.sections())

    def test_records_land_in_the_file_the_config_names(self):
        """Read back the file rather than trusting that a handler was built.

        Runs in a subprocess because ``fileConfig`` closes every existing
        handler, pytest's log-capture handlers included. Calling it in-process
        left the rest of the run writing to closed streams -- 794 tests failed
        on the first attempt at this file, none of them for a real reason.
        """
        source = (REPO / "logging.conf").read_text(encoding="utf-8")
        old = "/home/kali/projects/claude-code-webconsole/logs/webconsole.log"
        self.assertIn(old, source, "the configured path moved; update this test")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wc.log"
            conf = Path(tmp) / "logging.conf"
            conf.write_text(source.replace(old, str(target)), encoding="utf-8")
            probe = (
                "import logging, logging.config; "
                f"logging.config.fileConfig({str(conf)!r}, disable_existing_loggers=False); "
                "logging.getLogger('wc.app').info('landed chat_id=%s', 'xyz'); "
                "logging.shutdown()"
            )
            result = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            written = target.read_text(encoding="utf-8")
        self.assertIn("landed chat_id=xyz", written)
        self.assertIn("wc.app", written, "the formatter must name the logger")
        self.assertEqual(written.count("landed chat_id=xyz"), 1,
                         "one record must not be written twice")

    def test_uvicorn_loggers_are_not_disabled(self):
        """disable_existing_loggers would silence the access log we rely on.

        Uvicorn's loggers are created before this module is imported, so the
        default of True would switch them off and trade one blind spot for
        another.
        """
        source = (REPO / "app.py").read_text(encoding="utf-8")
        self.assertIn("disable_existing_loggers=False", source)


class BrokenConfigTests(unittest.TestCase):
    """Losing log formatting must never stop the server from booting."""

    def test_a_malformed_config_still_leaves_logging_usable(self):
        """A file with no section headers is the likeliest corruption.

        fileConfig raises RuntimeError for that, not configparser.Error, which
        the first version of _configure_logging did not catch -- the server
        would have refused to import. Subprocessed for the same reason as
        above: this reconfigures logging.
        """
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "logging.conf"
            broken.write_text("this is not an ini file at all", encoding="utf-8")
            probe = (
                "import logging, unittest.mock, app; "
                f"unittest.mock.patch.object(app, '__file__', {str(Path(tmp) / 'app.py')!r}).start(); "
                "app._configure_logging(); "
                "print('usable', logging.getLogger('wc.app').isEnabledFor(logging.INFO))"
            )
            result = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
            )
        self.assertEqual(result.returncode, 0,
                         f"a broken logging.conf must not stop the app importing:\n"
                         f"{result.stderr[-2000:]}")
        self.assertIn("usable True", result.stdout)


if __name__ == "__main__":
    unittest.main()
