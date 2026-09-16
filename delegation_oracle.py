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

import os
import signal
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
    snippet into a command string. `timeout_s` bounds the whole call, not
    just the immediate child: the child is started as its own session
    leader (`start_new_session=True`), so it and anything it spawns share
    one process group, and on timeout the whole group is killed rather than
    just the process we launched directly. Without that, a candidate that
    spawns a grandchild and then hangs would time out at the top level while
    the grandchild kept running, reparented to init -- the timeout would
    have bounded the check's wall-clock time without bounding what it
    started.
    """
    if not code.strip():
        # A real benchmark outcome ("no extractable code in the response").
        # Passing it would score a non-answer as correct.
        return OracleVerdict(False, "no code produced")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidate.py"
        path.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return OracleVerdict(
                False,
                f"could not run the checker: {exc}",
                infrastructure_failure=True,
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Kill the whole process group the child leads, not just the
            # child, so anything it spawned dies with it. The child may
            # already have exited by the time we get here (it raced its own
            # death against the timeout) -- that races os.getpgid/os.killpg
            # into ProcessLookupError, which must not be read as "the kill
            # failed" or "this wasn't really a timeout": the verdict for a
            # timeout is unconditional, only the cleanup is best-effort.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()  # reap our own child by exit status, not by pipe EOF --
            # a grandchild that inherited the stdout/stderr pipes can hold
            # them open past our own child's death, so communicate() here
            # would block on *that*, not on anything we still need.
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            return OracleVerdict(
                False,
                f"timed out after {timeout_s}s",
                infrastructure_failure=True,
            )

    if proc.returncode == 0:
        return OracleVerdict(True, "compiles")
    detail = (stderr or stdout or "").strip()
    return OracleVerdict(
        False, detail.splitlines()[-1] if detail else "did not compile"
    )
