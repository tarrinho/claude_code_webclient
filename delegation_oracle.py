# delegation_oracle.py -- spec 4.2, stage 2 of the coding pipeline.
#
# Execution verification only: does the produced code parse, import, compile?
# QA and regression testing are stage 3 and deliberately not here -- section
# 4.2 separates them so a compile failure and a regression failure are
# distinguishable signals rather than one undifferentiated "it failed".
#
# Nothing in this module is wired into the delegation pipeline yet, and it
# must not be: spec section 12 keeps `coding` un-flipped from operational
# until its two open items (the free-rung target, the gate-type dependency)
# are resolved. This is the oracle stage in isolation, callable but unused.
from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OracleVerdict:
    passed: bool
    reason: str
    #: True when the harness failed rather than the code. Section 4.2 keeps
    #: this separate so tooling failures do not pollute accuracy metrics --
    #: a sandbox crash counted as a wrong answer makes a model look worse
    #: every time the box is busy.
    infrastructure_failure: bool = False


def check_python(code: str, timeout_s: float = 10.0) -> OracleVerdict:
    """Run *code* in a subprocess and report what happened.

    A subprocess rather than `compile()`/`exec()` in-process: the point is
    to find out whether the produced code runs, and executing it here would
    run it in the server's own interpreter. A static syntax check alone is
    not enough -- "parse, import, compile" in section 4.2 means the
    module's top level actually executes (as it would on `import`), which is
    the only way an infinite loop in the candidate surfaces as a hang for
    this stage to catch, rather than something stage 2 would wave through.

    The snippet is written to a file inside a temporary directory and the
    child is invoked with an argument vector (`shell=False`, the
    `subprocess` default) naming that file -- never by concatenating the
    snippet into a command string. `timeout_s` is enforced by the
    `subprocess.run` call itself; on expiry, `subprocess.run` kills the
    child before raising `TimeoutExpired`, so nothing is left running.
    """
    if not code.strip():
        # A real benchmark outcome ("no extractable code in the response").
        # Passing it would score a non-answer as correct.
        return OracleVerdict(False, "no code produced")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return OracleVerdict(
                False,
                f"timed out after {timeout_s}s",
                infrastructure_failure=True,
            )
        except OSError as exc:
            return OracleVerdict(
                False,
                f"could not run the checker: {exc}",
                infrastructure_failure=True,
            )

    if result.returncode == 0:
        return OracleVerdict(True, "compiles")
    detail = (result.stderr or result.stdout or "").strip()
    return OracleVerdict(
        False, detail.splitlines()[-1] if detail else "did not compile"
    )
