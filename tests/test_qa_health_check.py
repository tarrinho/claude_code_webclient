"""QA coverage for bin/wc-health.sh -- the thing that can restart production.

A health check is the one component whose bugs are self-inflicted outages.
Registry #34 was a check that restarted a *healthy* server every 30 seconds,
which is strictly worse than having none; proving §17 case 1 this evening
turned one crash into two stop/start cycles, because the check raced systemd's
own Restart=always and interrupted the boot it had just triggered.

So the branches that must *not* restart get as much coverage here as the ones
that must. The script takes its systemctl through a `WC_SYSTEMCTL` seam, which
lets these tests observe the decision without touching the live server and
without shadowing a real binary on PATH.

Covers:
* No action when systemd is not managing the server, or has been told to stop
  it, or is already mid-restart.
* The boot grace, including that it fails safe when the age is unknowable.
* Deferring to systemd when the main process has already gone.
* Restarting a wedged server that is alive and answering nothing.
* The write-path check: stale restarts, fresh does not, and a probe reading a
  different database than the server declines to act.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "wc-health.sh"

# Nothing listens here, so curl fails immediately rather than waiting out its
# timeout three times.
DEAD_URL = "http://127.0.0.1:1/login"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@unittest.skipUnless(SCRIPT.exists(), "bin/wc-health.sh not present")
class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.restarts = self.dir / "restarts.log"

    def _fake_systemctl(
        self,
        *,
        enabled: bool = True,
        active: str = "active",
        sub: str = "running",
        pid: str | None = None,
    ) -> Path:
        """A systemctl double that records restarts instead of performing them."""
        path = self.dir / "fake-systemctl"
        path.write_text(textwrap.dedent(f"""\
            case "$*" in
              *is-enabled*)  exit {0 if enabled else 1} ;;
              *ActiveState*) echo {active} ;;
              *SubState*)    echo {sub} ;;
              *MainPID*)     echo {pid if pid is not None else 1} ;;
              *restart*)     echo "RESTART $*" >> {self.restarts} ;;
            esac
            exit 0
        """))
        return path

    def _db(self, newest: str | None) -> Path:
        path = self.dir / "wc.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE system_samples (created_at TEXT)")
        if newest:
            conn.execute("INSERT INTO system_samples VALUES (?)", (newest,))
        conn.commit()
        conn.close()
        return path

    def _run(self, systemctl: Path, *, url: str = DEAD_URL, db: Path | None = None,
             grace: str = "0", extra: dict[str, str] | None = None):
        env = {
            **os.environ,
            "WC_SYSTEMCTL": f"bash {systemctl}",
            "WC_HEALTH_URL": url,
            "WC_HEALTH_ATTEMPTS": "1",
            "WC_HEALTH_GAP": "0",
            "WC_HEALTH_BOOT_GRACE": grace,
        }
        if db is not None:
            env["WC_DB_PATH"] = str(db)
        else:
            env["WC_HEALTH_SKIP_WRITE_CHECK"] = "1"
        env.update(extra or {})
        proc = subprocess.run(
            ["bash", str(SCRIPT)], cwd=REPO, env=env,
            capture_output=True, text=True, timeout=120, check=False,
        )
        return proc

    def _restarted(self) -> bool:
        return self.restarts.exists() and "RESTART" in self.restarts.read_text()

    # ── The branches that must not act ────────────────────────────────────────

    def test_does_nothing_when_systemd_is_not_managing_the_server(self):
        """Otherwise a developer running launch.sh by hand gets their server
        restarted underneath them by a timer they forgot was installed."""
        self._run(self._fake_systemctl(enabled=False))
        self.assertFalse(self._restarted())

    def test_an_intentional_stop_stays_stopped(self):
        self._run(self._fake_systemctl(active="inactive"))
        self.assertFalse(self._restarted())

    def test_does_not_pile_on_while_systemd_is_already_restarting(self):
        """Restart=always is mid-cycle. A second restart on top of its first is
        how one crash became two stop/start cycles."""
        self._run(self._fake_systemctl(active="activating", sub="auto-restart"))
        self.assertFalse(self._restarted())

    def test_a_booting_server_is_left_alone(self):
        """Boot here takes longer than ATTEMPTS x GAP, so without this the
        check interrupts the very startup it triggered."""
        # This process, which is seconds old -- pid 1 is older than any
        # grace and would exercise the opposite branch.
        self._run(self._fake_systemctl(pid=str(os.getpid())), grace="99999")
        self.assertFalse(self._restarted())

    def test_the_grace_fails_safe_when_the_age_is_unknowable(self):
        """A pid that does not exist has no age. Acting on that is what made
        the first version race systemd; not acting costs 30 seconds."""
        self._run(self._fake_systemctl(pid="999999999"), grace="45")
        self.assertFalse(self._restarted())

    def test_a_dead_main_process_is_never_restarted_by_this_script(self):
        """The wedged case is a process alive and answering nothing. One that
        has already gone is systemd's job, and doing it here preempts the
        scheduled restart -- which is how one crash became two stop/start
        cycles.

        Two guards cover this, and which one fires depends on timing: the boot
        grace catches a pid that is already gone when the run starts, and the
        check just before the restart catches one that dies mid-run. The
        invariant is the same either way, so it is asserted rather than the
        path taken."""
        self._run(self._fake_systemctl(pid="999999998"), grace="0")
        self.assertFalse(self._restarted())

    # ── The branches that must act ───────────────────────────────────────────

    def test_a_wedged_server_is_restarted(self):
        """Alive, past grace, answering nothing: the gap systemd cannot see."""
        proc = self._run(self._fake_systemctl(), grace="0")
        self.assertTrue(self._restarted(), proc.stdout)
        self.assertIn("unhealthy after", proc.stdout)

    # ── The write path ──────────────────────────────────────────────────────

    def test_a_stalled_write_path_is_restarted_despite_a_healthy_page(self):
        """Registry #41. The server answers 200 and writes nothing; a probe
        that only reads cannot tell that from health."""
        db = self._db("2020-01-01T00:00:00Z")
        proc = self._run(self._fake_systemctl(), url=self._serve_ok(), db=db)
        self.assertTrue(self._restarted(), proc.stdout)
        self.assertIn("write path unhealthy", proc.stdout)

    def test_a_writing_server_is_not_restarted(self):
        db = self._db(_now())
        proc = self._run(self._fake_systemctl(), url=self._serve_ok(), db=db)
        self.assertFalse(self._restarted(), proc.stdout)

    def test_an_unsampled_database_is_not_treated_as_a_fault(self):
        db = self._db(None)
        proc = self._run(self._fake_systemctl(), url=self._serve_ok(), db=db)
        self.assertFalse(self._restarted(), proc.stdout)

    def test_the_write_check_can_be_disabled(self):
        db = self._db("2020-01-01T00:00:00Z")
        proc = self._run(
            self._fake_systemctl(), url=self._serve_ok(), db=db,
            extra={"WC_HEALTH_SKIP_WRITE_CHECK": "1"},
        )
        self.assertFalse(self._restarted(), proc.stdout)

    # ── Helper: a local server that really answers 200 ──────────────────────

    def _serve_ok(self) -> str:
        """A one-shot HTTP server, so the 200 path is exercised for real.

        Stubbing curl would test the stub. The write-path branch only runs
        after a genuine 200, so there has to be something genuinely serving.
        """
        import http.server
        import threading

        class Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # BaseHTTPRequestHandler names it
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/login"


if __name__ == "__main__":
    unittest.main()
