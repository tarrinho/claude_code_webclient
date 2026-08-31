"""QA: every logger the application uses must reach the log file.

Logging was configured in three places that disagreed. logging.conf described a
rotating file handler; app.py's basicConfig sat inside an ``if __name__ ==
"__main__"`` block that uvicorn never runs; and runner.py logs through loguru,
whose sinks are a separate system that logging.conf cannot reach at all. The
result was that the turn-execution path -- launches, proxy connect failures,
handshake problems, timeouts -- went to stderr and was lost with whatever shell
started the server.

These tests assert the wiring rather than any particular message, because the
failure was never a missing log call: it was a log call with nowhere to go.
"""
from __future__ import annotations

import ast
import configparser
import unittest
from pathlib import Path

import app

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "logging.conf"


def _source_loggers() -> set[str]:
    """Logger names the source actually creates, by reading the source."""
    names = set()
    for path in ROOT.glob("*.py"):
        for line in path.read_text().splitlines():
            if 'getLogger("' in line:
                names.add(line.split('getLogger("')[1].split('"')[0])
    return names


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.parser = configparser.ConfigParser()
        self.parser.read(CONF)

    def test_config_file_exists_and_parses(self):
        """It was present but referenced by nothing for a while."""
        self.assertTrue(CONF.is_file())
        self.assertIn("loggers", self.parser)

    def test_app_loads_the_config_rather_than_ignoring_it(self):
        source = (ROOT / "app.py").read_text()
        self.assertIn("logging.config.fileConfig", source)

    def test_every_wc_logger_in_the_source_is_declared(self):
        """wc.auth and wc.db worked only by propagating to root, so their
        level could not be set independently and the dependency was invisible.
        """
        declared = {
            self.parser[s]["qualname"]
            for s in self.parser.sections()
            if s.startswith("logger_") and "qualname" in self.parser[s]
        }
        for name in sorted(n for n in _source_loggers() if n.startswith("wc.")):
            with self.subTest(logger=name):
                self.assertIn(name, declared)

    def test_the_file_handler_rotates(self):
        """An unbounded log on a self-hosted box eventually fills the disk."""
        self.assertIn("RotatingFileHandler", self.parser["handler_file"]["class"])
        # literal_eval, not eval: this is a config value, and parsing it must
        # not be able to execute anything even from a file we own.
        # raw=True: the filename is now the %(logfile)s token, supplied at
        # configure time from config.LOG_FILE. A plain get() would try to
        # interpolate it and raise, since nothing defines it in the file.
        args = ast.literal_eval(self.parser.get("handler_file", "args", raw=True))
        self.assertEqual(len(args), 4)   # (filename, mode, maxBytes, backupCount)
        self.assertGreater(args[2], 0)   # rotates by size
        self.assertGreater(args[3], 0)   # keeps backups


class LoguruForwardingTests(unittest.TestCase):
    """runner.py logs through loguru; the config only knows stdlib logging."""

    def test_a_forwarder_exists(self):
        self.assertTrue(hasattr(app, "_forward_loguru_to_logging"))

    def test_runner_records_arrive_on_the_wc_runner_logger(self):
        import runner

        app._forward_loguru_to_logging()
        with self.assertLogs("wc.runner", level="INFO") as caught:
            runner._log.info("probe {}", "value")
        self.assertIn("probe value", "\n".join(caught.output))

    def test_levels_survive_the_hop(self):
        import runner

        app._forward_loguru_to_logging()
        for method, level in (("warning", "WARNING"), ("error", "ERROR")):
            with self.subTest(level=level):
                with self.assertLogs("wc.runner", level=level) as caught:
                    getattr(runner._log, method)("probe-{}", level)
                self.assertIn(level, caught.output[0])

    def test_default_sink_is_removed_so_lines_are_not_doubled(self):
        """The stdlib console handler already prints; leaving loguru's own
        stderr sink in place printed every runner line twice."""
        source = (ROOT / "app.py").read_text()
        forwarder = source.split("def _forward_loguru_to_logging")[1].split("\ndef ")[0]
        self.assertIn("_loguru.remove()", forwarder)


class TurnTraceabilityTests(unittest.TestCase):
    """A turn that ran must leave a trace, whichever path it took."""

    def test_both_turn_paths_log_a_launch(self):
        source = (ROOT / "runner.py").read_text()
        # Proxy mode is the default, and it previously logged only failures --
        # so a successful turn left no record, and a hung one left nothing to
        # say where it stopped.
        self.assertIn('_log.info("proxy turn chat=', source)
        self.assertIn('_log.info("launch chat=', source)


if __name__ == "__main__":
    unittest.main()
