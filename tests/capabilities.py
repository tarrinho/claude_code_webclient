"""What this interpreter can actually run, and whether another one could.

`conftest.py` uses this to refuse a run that would silently skip whole layers of
the suite. The distinction it draws is the entire point, so it lives in its own
module with its own tests rather than inside a hook.

**A missing capability is not automatically a problem.** A machine with no
Chromium genuinely cannot run browser tests, and skipping them there is honest
reporting. The failure this guards is narrower and nastier: the machine *can*,
and the interpreter you typed cannot. `python3 -m pytest` reports
`1 skipped in 0.14s` for a test that fails in 6.7s under `.venv/bin/python`, and
a session called a tree "green and settled" eleven times on that basis
(rules.md #64). Before that, 43 tests -- the JS syntax gate and every browser
case -- were absent from every total quoted for an evening (#50).

So each capability answers two questions, not one: *is it available here*, and
*is it available to the venv interpreter*. Only the combination
`missing here, present there` is an error. Missing in both is a fact about the
machine, and the per-file skips already say so accurately.

#50's remedy was a paragraph in rules.md §14. It has since failed twice against
readers who had read it, which is the argument for a check that runs rather than
one that is remembered.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

# How long the sibling-interpreter probe may take before it is abandoned. It is
# a bare import in a subprocess; if it has not answered by then something is
# wrong with that interpreter and we would rather not block collection on it.
PROBE_TIMEOUT_S = 20


@dataclass(frozen=True)
class Capability:
    """One thing the suite needs, and how to find out whether it is here."""

    name: str
    #: Runs in *this* interpreter. True when the capability is usable.
    probe: str
    #: What is lost when it is missing, for the message.
    covers: str


#: The two capabilities whose absence has actually hidden tests here.
#:
#: `chromium` is deliberately absent from this list. Its absence is a real
#: property of a machine and the per-file `skipUnless(CHROMIUM)` guards report
#: it correctly; adding it here would make a laptop without a browser unable to
#: run the suite at all, which is a worse failure than the one being prevented.
CAPABILITIES = (
    Capability(
        name="quickjs",
        probe="import quickjs",
        covers="the JavaScript syntax gate (tests/test_frontend_syntax.py)",
    ),
    Capability(
        name="playwright-driver",
        probe=(
            "from playwright._impl._driver import compute_driver_executable as c;"
            "import pathlib,sys;"
            "p=c();p=p[0] if isinstance(p,(list,tuple)) else p;"
            "sys.exit(0 if pathlib.Path(p).exists() else 1)"
        ),
        covers="every browser test (tests/test_frontend_browser.py and the "
               "orchestrator UI suites)",
    ),
)


def _available_here(cap: Capability) -> bool:
    """Whether *cap* works in the interpreter running this code."""
    try:
        exec(compile(cap.probe, "<probe>", "exec"), {})  # noqa: S102 - fixed source
    except SystemExit as exit_:
        return not exit_.code
    except Exception:  # noqa: BLE001 -- any failure means unusable
        return False
    return True


def _available_in_venv() -> dict[str, bool]:
    """The same probes, run once in the venv interpreter.

    One subprocess for all capabilities rather than one each: this runs at
    collection time on every invocation, and the answer is only used to phrase
    an error message.

    Returns an empty mapping when the venv cannot be asked -- no venv, no
    permission, a timeout. An unknown answer must never become an accusation,
    so callers treat "not in the mapping" as "no evidence", not as "absent".
    """
    # `sys.prefix`, not `sys.executable`. A venv built on the system
    # interpreter symlinks its `bin/python` straight at it, so comparing
    # resolved executable paths says "we are already the venv" for *both*
    # interpreters -- and the probe that would have caught the wrong one never
    # runs. Measured here: `python3` and `.venv/bin/python` resolve to the same
    # binary; their prefixes are `/usr` and the project's `.venv`. The thing
    # that differs is which site-packages are in play, and prefix is what names
    # it. Comparing a proxy for the property instead of the property is the
    # mistake this whole module exists to catch, so it is worth a paragraph.
    if not VENV_PYTHON.is_file():
        return {}
    if Path(sys.prefix).resolve() == (ROOT / ".venv").resolve():
        return {}
    script = (
        "import json\n"
        "out = {}\n"
        + "".join(
            f"try:\n"
            f"    {cap.probe}\n"
            f"    out[{cap.name!r}] = True\n"
            f"except SystemExit as e:\n"
            f"    out[{cap.name!r}] = not e.code\n"
            f"except Exception:\n"
            f"    out[{cap.name!r}] = False\n"
            for cap in CAPABILITIES
        )
        + "print(json.dumps(out))\n"
    )
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), "-c", script],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False,
        )
        return json.loads(result.stdout.strip() or "{}")
    except Exception:  # noqa: BLE001 -- an unaskable venv is not a finding
        return {}


def wrong_interpreter() -> list[tuple[Capability, str]]:
    """Capabilities missing here that the venv interpreter has.

    This -- and only this -- is the condition worth stopping a run for. A
    capability missing from both interpreters is a fact about the machine.
    """
    in_venv = _available_in_venv()
    out = []
    for cap in CAPABILITIES:
        if _available_here(cap):
            continue
        if in_venv.get(cap.name) is True:
            out.append((cap, "present in .venv"))
    return out


def missing_everywhere() -> list[Capability]:
    """Capabilities absent here *and* not known to be available elsewhere.

    Reported, never fatal. The per-file skips describe these accurately; this
    exists so a run can say out loud what it is not covering, because a total
    that hides unrun tests is worse than no total.
    """
    in_venv = _available_in_venv()
    return [
        cap for cap in CAPABILITIES
        if not _available_here(cap) and in_venv.get(cap.name) is not True
    ]
