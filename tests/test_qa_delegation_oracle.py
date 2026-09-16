"""QA: spec 4.2, stage 2 -- execution verification only.

The distinction this file exists to hold: "the code failed verification" and
"the harness broke" are different signals. Section 4.2 says the second is
tagged separately so production accuracy metrics are not polluted by tooling
failures, and a test that only checks `passed` cannot tell them apart. This
file is the coding oracle's own test -- nothing here routes to `coding` and
nothing here makes `coding` operational (spec 12 keeps that closed).
"""
from __future__ import annotations

import unittest

import delegation_oracle as oracle


class OracleTests(unittest.TestCase):
    def test_code_that_parses_and_runs_passes(self):
        verdict = oracle.check_python("def f():\n    return 1\n")
        self.assertTrue(verdict.passed)
        self.assertFalse(verdict.infrastructure_failure)

    def test_code_that_does_not_parse_fails(self):
        verdict = oracle.check_python("def f(:\n")
        self.assertFalse(verdict.passed)
        self.assertIn("Syntax", verdict.reason)

    def test_a_syntax_failure_is_not_an_infrastructure_failure(self):
        """The whole point of the second field. Bad code is the model's
        problem; a broken sandbox is ours, and averaging them together hides
        both."""
        verdict = oracle.check_python("def f(:\n")
        self.assertFalse(verdict.infrastructure_failure)

    def test_empty_output_fails_rather_than_passing_vacuously(self):
        """'No extractable code' was a real benchmark outcome. Passing it
        would score a non-answer as correct."""
        verdict = oracle.check_python("")
        self.assertFalse(verdict.passed)

    def test_a_timeout_is_an_infrastructure_failure(self):
        verdict = oracle.check_python("while True:\n    pass\n", timeout_s=0.5)
        self.assertFalse(verdict.passed)
        self.assertTrue(verdict.infrastructure_failure)

    def test_syntax_failure_and_timeout_are_distinguished_by_field_not_string(self):
        """The property that matters most: a code failure and an
        infrastructure failure must never collapse into the same signal.
        This asserts the two failure modes land on opposite sides of
        `infrastructure_failure` -- not by matching text in `reason`, which
        could be made to say anything without fixing the underlying
        conflation. If `infrastructure_failure` were hardcoded to a constant,
        or copied from `passed`, exactly one of these two assertions would
        fail."""
        bad_code = oracle.check_python("def f(:\n")
        slow_code = oracle.check_python("while True:\n    pass\n", timeout_s=0.5)

        self.assertFalse(bad_code.passed)
        self.assertFalse(slow_code.passed)

        self.assertFalse(bad_code.infrastructure_failure)
        self.assertTrue(slow_code.infrastructure_failure)

    def test_verdict_is_frozen(self):
        verdict = oracle.check_python("def f():\n    return 1\n")
        with self.assertRaises(Exception):
            verdict.passed = False

    def test_timeout_kills_the_child_rather_than_leaving_it_running(self):
        """A timeout must not leave the child process alive. We can't
        inspect the internals of check_python from here, but we can prove
        the call returns promptly and does not hang the test runner even
        though the snippet loops forever -- if the subprocess call failed to
        enforce/kill on timeout, this test would hang past its own timeout
        rather than complete."""
        import time

        start = time.monotonic()
        verdict = oracle.check_python("while True:\n    pass\n", timeout_s=0.3)
        elapsed = time.monotonic() - start

        self.assertTrue(verdict.infrastructure_failure)
        # Generous upper bound -- the point is "didn't hang", not exact timing.
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
