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


HEALTH_URL_SH = REPO / "bin" / "wc-health-url.sh"


@unittest.skipUnless(HEALTH_URL_SH.exists(), "bin/wc-health-url.sh not present")
class HealthUrlDerivationTests(unittest.TestCase):
    """Which URL the check builds when WC_HEALTH_URL is not set.

    Every test above injects WC_HEALTH_URL, so the derivation -- the part that
    was actually broken -- had no coverage at all, and shipped broken twice.
    It built https://<tailnet-ip>/login from whatever was listening on :443.
    Caddy fronts :443 with a single named site block and routes on SNI, so an
    address matches no site and answers 000 for ever; wc-health.sh acted on
    that by restarting webconsole.service every ~60s, and wc-deploy.sh skipped
    its gate entirely. Measured 2026-09-11 against the live server: IP form
    000, name form 200, app form 200.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _derive(self, tailscale_body: str, *, port: str | None = None) -> str:
        """Run the shared wc_health_url() against a stubbed `tailscale`."""
        bin_dir = self.dir / "stub-bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "tailscale"
        stub.write_text("#!/usr/bin/env bash\n" + tailscale_body)
        stub.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        # The fallback reads WC_PORT; drop any ambient value so the default is
        # what is under test unless a case sets one.
        env.pop("WC_PORT", None)
        if port is not None:
            env["WC_PORT"] = port

        proc = subprocess.run(
            ["bash", "-c", f'. "{HEALTH_URL_SH}"; wc_health_url'],
            cwd=REPO, env=env, capture_output=True, text=True,
            timeout=60, check=False,
        )
        return proc.stdout.strip()

    def test_the_url_is_built_from_the_host_name(self):
        url = self._derive(
            """echo '{"Self": {"DNSName": "test-host.example.ts.net."}}'\n""")
        self.assertEqual(url, "https://test-host.example.ts.net/login")

    def test_the_trailing_root_dot_is_stripped(self):
        """tailscale reports DNSName fully qualified: "...ts.net." with the dot."""
        url = self._derive(
            """echo '{"Self": {"DNSName": "kali-2.tail850c40.ts.net."}}'\n""")
        self.assertEqual(url, "https://kali-2.tail850c40.ts.net/login")
        self.assertNotIn("./login", url)

    def test_it_is_never_an_address(self):
        """The regression itself, across every way tailscale can let us down."""
        for label, body in (
            ("normal", """echo '{"Self": {"DNSName": "h.example.ts.net."}}'\n"""),
            ("tailscale missing or failing", "exit 1\n"),
            ("unparseable output", 'echo "not json"\n'),
            ("empty output", "true\n"),
            ("no DNSName key", """echo '{"Self": {}}'\n"""),
        ):
            with self.subTest(case=label):
                url = self._derive(body)
                self.assertNotRegex(
                    url, r"https://\d+\.\d+\.\d+\.\d+",
                    "an https URL built from an address matches no Caddy site "
                    "block and answers 000, which this check then acts on",
                )

    def test_it_falls_back_to_the_app_on_loopback(self):
        """No usable name: ask the app directly rather than another address.

        uvicorn binds 127.0.0.1:8080 behind Caddy, so this is reachable -- and
        it is the more honest question for a script whose only action is to
        restart webconsole.service.
        """
        self.assertEqual(
            self._derive("exit 1\n"), "http://127.0.0.1:8080/login")

    def test_the_fallback_port_follows_wc_port(self):
        self.assertEqual(
            self._derive("exit 1\n", port="9999"),
            "http://127.0.0.1:9999/login",
        )


if __name__ == "__main__":
    unittest.main()
