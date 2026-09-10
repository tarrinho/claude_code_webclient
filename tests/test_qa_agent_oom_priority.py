"""QA: an interactive agent makes itself the OOM victim, not webconsole.service.

This is a decision test as much as a logic one. The kernel was picking the
wrong process by a wide margin -- measured 2026-09-10, webconsole.service sat
at oom_score 813 on 141 MB while seven interactive CLIs sat at 698-709 on
167-366 MB each. The service using the least memory was first in line, because
the user manager's DefaultOOMScoreAdjust=200 applies to services and not to
shell children. It was SIGKILLed twice in ten minutes, and each kill
cold-starts the transcript caches, whose first scans spike memory and invite
the next one.

The service cannot correct this from its own side, which is why the fix lives
in the wrapper: a unit can ask for OOMScoreAdjust=0, but the user manager runs
at adj 100 and lowering below its own value needs CAP_SYS_RESOURCE, so the
kernel silently clamps to 100 (813 -> 746, still above the CLIs). Raising your
*own* score is always permitted, so the agent side is where the ordering can
actually be fixed.

Why this needs a test rather than a comment: raising your own kill priority
reads like a mistake. Someone tidying the wrapper will reasonably remove it,
or "fix" it to lower the score instead -- which cannot work, and would restore
exactly the inversion. The trade is deliberate and it is the right way round:
a killed CLI resumes from its transcript, while a killed webconsole takes
every session's UI down at once and starts the cold-start loop. Recoverable
beats shared.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "wc-claude.sh"


def _run_wrapper(env_extra: dict[str, str]) -> str:
    """Run the real wrapper with /bin/cat standing in for the CLI, so what it
    prints is the oom_score_adj the exec'd process actually inherited.

    WC_DB_PATH points at nothing so the wrapper takes its "no WebConsole
    database" branch, which is a bare `exec "$CLAUDE_BIN" "$@"` -- reached
    after the block under test, and without needing a real backend.
    """
    env = {
        **os.environ,
        "WC_DB_PATH": "/nonexistent-db-for-this-test",
        "WC_CLAUDE_PATH": "/bin/cat",
        "WC_RESOURCE_GUARD": "off",
        **env_extra,
    }
    proc = subprocess.run(
        ["bash", str(WRAPPER), "/proc/self/oom_score_adj"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc.stdout.strip()


@unittest.skipUnless(
    Path("/proc/self/oom_score_adj").exists(),
    "needs a Linux /proc to have an oom_score_adj at all",
)
class AgentOomPriorityTests(unittest.TestCase):
    def test_a_session_started_through_the_wrapper_raises_its_own_score(self):
        """The property, end to end: not that the script contains a line, but
        that the process it execs actually carries the raised value."""
        self.assertEqual(_run_wrapper({}), "200")

    def test_the_raise_survives_exec(self):
        """oom_score_adj is a process attribute that exec preserves, which is
        what lets one assignment at the top of the wrapper cover all four of
        its exec paths and the hot-swap loop's child. If it did not survive,
        the value read here would be the inherited 0."""
        self.assertNotEqual(
            _run_wrapper({}), "0",
            "the exec'd process inherited nothing -- the raise is being lost",
        )

    def test_it_can_be_opted_out_of(self):
        """Opting out means the wrapper writes nothing, so the exec'd process
        keeps whatever it inherited -- which is this test runner's own score,
        not a literal 0.

        This assertion used to be `== "0"`, and it passed until the running
        CLIs on this host had their scores backfilled to 200. Pytest is a child
        of one of those sessions, so it now inherits 200 and hands that to the
        wrapper. The old form was really asserting "the parent happens to be at
        0", which says nothing about the opt-out and fails wherever the wrapper
        is actually in use. Compare against the parent instead; see
        test_opting_out_leaves_an_inherited_score_alone for the same property
        proven against a deliberately raised parent.
        """
        inherited = Path("/proc/self/oom_score_adj").read_text().strip()
        self.assertEqual(_run_wrapper({"WC_AGENT_OOM_ADJ": "0"}), inherited)

    def test_opting_out_leaves_an_inherited_score_alone(self):
        """Opting out has to mean "do not touch it", not "force it to 0".
        Those are indistinguishable when the inherited value is already 0,
        which is why this launches the wrapper from a parent that has raised
        its own score first: writing 0 unconditionally would clobber it, and
        the caller who set WC_AGENT_OOM_ADJ=0 asked for the opposite."""
        env = {
            **os.environ,
            "WC_DB_PATH": "/nonexistent-db-for-this-test",
            "WC_CLAUDE_PATH": "/bin/cat",
            "WC_RESOURCE_GUARD": "off",
            "WC_AGENT_OOM_ADJ": "0",
        }
        # The parent raises to 150, then hands off to the wrapper.
        script = (
            'printf "%s" 150 > /proc/self/oom_score_adj || exit 77; '
            f'exec bash {WRAPPER} /proc/self/oom_score_adj'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if proc.returncode == 77:
            self.skipTest("could not raise oom_score_adj in the parent")
        self.assertEqual(
            proc.stdout.strip(), "150",
            "opting out overwrote an inherited score instead of leaving it "
            "alone",
        )

    def test_a_custom_value_is_honoured(self):
        self.assertEqual(_run_wrapper({"WC_AGENT_OOM_ADJ": "350"}), "350")

    def test_the_wrapper_raises_rather_than_lowers(self):
        """The direction is the whole point, and getting it backwards is the
        likeliest well-meaning edit: a negative adjustment would protect the
        agent and leave the service as the victim, and would silently fail
        anyway without CAP_SYS_RESOURCE."""
        adj = int(_run_wrapper({}))
        self.assertGreater(
            adj, 0,
            "a session must be a *more* attractive OOM victim than the shared "
            "service, not less",
        )

    def test_a_session_outscores_the_service_it_protects(self):
        """The ordering that matters, stated in the units the kernel uses.
        The service is clamped to adj 100 by its manager; a session at 200 is
        above it, so the kernel reaches for the session first."""
        service_clamped_adj = 100   # measured: unit asks 0, kernel gives 100
        self.assertGreater(int(_run_wrapper({})), service_clamped_adj)


if __name__ == "__main__":
    unittest.main()
