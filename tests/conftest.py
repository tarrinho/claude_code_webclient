"""Test-wide environment defaults, applied before anything imports config.

``config`` resolves ``LOG_FILE`` at import time, so this cannot be a fixture:
by the time a fixture runs, the module-level constant is already bound and the
rotating handler is already pointed at whatever it said. pytest imports
conftest before collecting test modules, which is the only hook early enough.

Why it exists: the suite was writing into the production log. That was fixed
once, for the servers the suite *spawns* -- the browser fixture passes
``WC_LOG_FILE`` into its subprocess -- and the fix was reported as done. But
four test modules drive the app with an in-process ``TestClient``, which imports
``app`` inside the pytest process itself and configures logging there, with
``WC_LOG_FILE`` unset. Those bypassed the subprocess fix entirely and kept
appending: ``ip=testclient``, ``user=alice``, ``chat_id=c1`` and a stored-XSS
probe all appear in logs/webconsole.log, interleaved with real traffic.

The test that was supposed to prevent this asserted the property only for the
redirected-subprocess path, so it passed throughout. A guard that covers one of
two routes reads as covered and is worse than none, because nobody looks again.
"""
from __future__ import annotations

import os
import pathlib
import tempfile

# Not overridden if the caller already set it: test_qa_log_path drives
# WC_LOG_FILE deliberately, and a harness or CI may point it somewhere it
# collects from.
if not os.environ.get("WC_LOG_FILE"):
    _log_dir = pathlib.Path(tempfile.gettempdir()) / "wc-test-logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    # A stable path rather than a fresh directory per run: several sessions run
    # this suite on one machine, and a per-run directory would leave a litter of
    # them. Interleaving between concurrent runs is fine here -- that is the
    # cost this file exists to keep *out* of the production log, not something
    # the test log needs to be protected from.
    os.environ["WC_LOG_FILE"] = str(_log_dir / "suite.log")


# ── Capability guard ─────────────────────────────────────────────────────────
#
# Refuses a run that would silently skip a whole layer of the suite because of
# the interpreter it was invoked with. See tests/capabilities.py for why that is
# narrower than "check the dependencies are installed".
#
# Four decisions worth stating, because each has an obvious wrong answer:
#
# 1. It fires on what was *collected*, not on what exists. `pytest
#    tests/test_qa_db.py` needs neither quickjs nor a browser, and a
#    session-wide refusal would train everyone to set the opt-out permanently --
#    at which point the guard is gone and the variable stays.
# 2. It fires only when the capability is missing *here* and present in the
#    venv. Absent from both is a fact about the machine, and the per-file
#    `skipUnless` guards report that correctly; escalating it would stop a
#    laptop with no Chromium from running the suite at all.
# 3. It does not replace those per-file skips. They answer "can this machine
#    do it"; this answers "did you invoke the wrong interpreter". Two questions,
#    two mechanisms, and only one of them is a mistake.
# 4. The opt-out prints what it is giving up. An escape hatch that silences the
#    count is the original defect (rules.md #50) with an extra step.
import os as _os

import pytest

_ALLOW_PARTIAL = "WC_ALLOW_PARTIAL_SUITE"


def _needs_capability(item, cap_name: str) -> bool:
    """Whether *item* belongs to a file that needs *cap_name*.

    By module path rather than by importing anything: at collection time the
    module is already imported, and asking it questions risks running the very
    import that is about to fail.
    """
    path = str(getattr(item, "fspath", "") or "")
    if cap_name == "quickjs":
        return path.endswith("test_frontend_syntax.py")
    if cap_name == "playwright-driver":
        module = getattr(item, "module", None)
        return module is not None and hasattr(module, "sync_playwright")
    return False


def pytest_collection_modifyitems(session, config, items):
    from . import capabilities

    wrong = capabilities.wrong_interpreter()
    if not wrong:
        for cap in capabilities.missing_everywhere():
            affected = sum(1 for i in items if _needs_capability(i, cap.name))
            if affected:
                print(
                    f"\nNOTE: {cap.name} is unavailable on this machine, so "
                    f"{affected} collected test(s) covering {cap.covers} will "
                    f"skip. This is a property of the machine, not of the "
                    f"invocation."
                )
        return

    blocking = []
    for cap, where in wrong:
        affected = sum(1 for i in items if _needs_capability(i, cap.name))
        if affected:
            blocking.append((cap, where, affected))
    if not blocking:
        return

    lines = [
        "",
        "This interpreter cannot run part of what you asked it to collect.",
        "",
    ]
    for cap, where, affected in blocking:
        lines.append(
            f"  {cap.name}: missing here, {where} -- {affected} collected "
            f"test(s) covering {cap.covers} would skip."
        )
    lines += [
        "",
        f"  Run:  .venv/bin/python -m pytest {' '.join(config.invocation_params.args)}",
        "",
        "  A skip reads as 'not applicable here', which is indistinguishable",
        "  from 'ran and passed' in a total. That is rules.md #50, and it has",
        "  since recurred against readers who had read the rule -- which is why",
        f"  this stops the run instead of warning. Set {_ALLOW_PARTIAL}=1 to",
        "  proceed anyway; the count above will be reported either way.",
        "",
    ]
    message = "\n".join(lines)
    if _os.environ.get(_ALLOW_PARTIAL):
        print(message)
        print(f"{_ALLOW_PARTIAL} is set -- continuing without the tests listed above.")
        return
    # UsageError, not SystemExit: raising SystemExit from a hook is reported
    # as INTERNALERROR with a traceback, which buries the actionable line
    # under noise that looks like a bug in the guard. UsageError is pytest's
    # own channel for "you invoked this wrong" and prints the message alone.
    raise pytest.UsageError(message)
