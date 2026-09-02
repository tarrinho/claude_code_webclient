"""Score a model's answer by running it, not by reading it.

Every coding score in the 2026-09-02 comparison came from a human reading the
response. That is how `capacity=0` raising `KeyError` in three different models'
LRU caches was recorded as a ⚠️ for two of them, a 0 for one, and noticed at all
only because someone traced it by hand. A test case finds it in every model, on
every run, identically, and costs nothing to re-run.

So verification here is of two kinds and they are labelled differently:

* ``exec`` — the response's code is extracted and run against assertions. The
  score is the fraction that pass. This is a measurement.
* ``claim`` — the response is prose, and the checks are string or numeric
  assertions against it. This is weaker: it can confirm a stated answer but not
  that the reasoning behind it holds. Marked as such in the output so nobody
  reads one as the other.

**Model-written code is executed.** It runs in a subprocess, in a throwaway
directory, under a wall-clock timeout, with the parent's environment stripped to
almost nothing. That is prudence about accidents — an infinite loop, a stray
`open(...,'w')`, a recursive delete written in good faith — and it is not a
sandbox. Do not point this at a model whose output you would not run yourself.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: How long a single verification subprocess may run. Model code that loops
#: forever is a real outcome and must fail rather than hang the suite.
EXEC_TIMEOUT_S = 20

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class Verdict:
    """The outcome of verifying one response.

    Checks are split into **core** and **edge**, because a single all-or-nothing
    verdict over a long check list lets one strict corner case decide
    everything. The first Anthropic run showed exactly that: an LRU scoring
    26 of 27 was recorded as "not correct" for raising on `capacity=0`, sitting
    in the same column as a response that contained no code at all. Those are
    not the same outcome and a benchmark that cannot tell them apart is not
    measuring what anyone wants to know.

    So: **core** is "did it solve the stated problem" and decides `correct`.
    **edge** is "did it handle the corners", reported beside it and never
    gating. Both still contribute to `score`, so granularity is not lost.
    """

    kind: str                      # "exec" | "claim"
    passed: int
    total: int
    detail: list[str] = field(default_factory=list)
    #: True when the response contained no extractable code at all. Kept
    #: separate from "code that failed": an empty answer and a wrong answer are
    #: different findings, and conflating them is the original defect.
    no_code: bool = False
    #: Of `passed`/`total`, how many were core. When a verifier declares no
    #: edge checks these equal `passed`/`total`.
    core_passed: int = 0
    core_total: int = 0

    @property
    def score(self) -> float:
        """0-100 over every check, comparable with the old score dimensions."""
        if not self.total:
            return 0.0
        return round(100.0 * self.passed / self.total, 1)

    @property
    def core_score(self) -> float:
        if not self.core_total:
            return 0.0
        return round(100.0 * self.core_passed / self.core_total, 1)

    @property
    def solved(self) -> bool:
        """Every core check passed: the stated problem was solved."""
        return bool(self.core_total) and self.core_passed == self.core_total

    @property
    def edge_passed(self) -> int:
        return self.passed - self.core_passed

    @property
    def edge_total(self) -> int:
        return self.total - self.core_total


def extract_code(response: str) -> str:
    """The Python in *response*, from fences if present, else the whole text.

    Fences are preferred but not required. "Return valid Python code only" is
    one of the prompts, and a model that complies literally emits no fence --
    penalising that would score instruction-following as a syntax error.
    """
    blocks = _FENCE.findall(response or "")
    if blocks:
        # Longest block: models sometimes precede the answer with a short
        # snippet of the buggy original.
        return max(blocks, key=len).strip()
    text = (response or "").strip()
    if not text:
        return ""
    try:
        ast.parse(text)
    except SyntaxError:
        return ""
    return text


def run_checks(code: str, checks: str, edge_checks: str = "") -> Verdict:
    """Run *checks* against *code* in a subprocess, one statement at a time.

    *checks* is Python source that may use anything *code* defines. Each
    top-level statement is run independently so one failure does not hide the
    rest -- a class that gets eviction right and `capacity=0` wrong should score
    partial, not zero.

    *edge_checks* are run the same way but do not count toward `solved`. They
    are for corners a competent answer may reasonably miss: `n=0`,
    `capacity=0`, and the like. Both sonnet-5 and haiku-4-5 miss the `n=0` case
    on the bug-fix task, and calling that "incorrect" tells you nothing useful
    about either model while hiding the difference between them and a model
    that returned no code.
    """
    if not code.strip():
        return Verdict(kind="exec", passed=0, total=1,
                       detail=["no extractable code in the response"],
                       no_code=True, core_passed=0, core_total=1)

    try:
        core_statements = ast.parse(checks).body
        edge_statements = ast.parse(edge_checks).body if edge_checks.strip() else []
    except SyntaxError as exc:  # pragma: no cover -- our own checks are fixed
        raise ValueError(f"check source is not valid Python: {exc}") from exc
    core_sources = [ast.unparse(node) for node in core_statements]
    sources = core_sources + [ast.unparse(node) for node in edge_statements]
    n_core = len(core_sources)

    runner = _RUNNER_TEMPLATE.format(count=len(sources))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "candidate.py").write_text(code, encoding="utf-8")
        (root / "_checks.py").write_text(
            "CHECKS = [\n" + "".join(f"    {s!r},\n" for s in sources) + "]\n",
            encoding="utf-8",
        )
        (root / "_run.py").write_text(runner, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "_run.py"],
                cwd=root, capture_output=True, text=True,
                timeout=EXEC_TIMEOUT_S, check=False,
                # Almost nothing: the code under test is not ours and has no
                # business reading this process's environment.
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return Verdict(kind="exec", passed=0, total=len(sources),
                           core_passed=0, core_total=n_core,
                           detail=[(f"timed out after {EXEC_TIMEOUT_S}s "
                                    "(model code did not terminate)")])

    passed, core_passed, detail = 0, 0, []
    for line in proc.stdout.splitlines():
        if line.startswith("PASS "):
            passed += 1
            # The runner prints the check's index, so core and edge results are
            # attributable without a second channel.
            try:
                if int(line[5:].strip()) < n_core:
                    core_passed += 1
            except ValueError:
                pass
        elif line.startswith("FAIL "):
            label = "edge" if _index_of(line) >= n_core else "core"
            detail.append(f"[{label}] {line[5:]}")
    if not proc.stdout.strip():
        # The candidate module itself blew up on import, so no check ran.
        detail.append("candidate failed to import: "
                      + (proc.stderr.strip().splitlines() or ["<no output>"])[-1])
    return Verdict(kind="exec", passed=passed, total=len(sources), detail=detail,
                   core_passed=core_passed, core_total=n_core)


def _index_of(fail_line: str) -> int:
    """The check index out of `FAIL check 12: ...`, or -1."""
    match = re.search(r"check (\d+)", fail_line)
    return int(match.group(1)) if match else -1


#: Runs each check in its own try/except and prints one line per check, so a
#: single failure cannot mask the others. Kept as a template rather than a file
#: so the harness has no import-path dependency on where it is run from.
_RUNNER_TEMPLATE = '''\
import sys, traceback
sys.path.insert(0, ".")
from _checks import CHECKS
try:
    import candidate
    ns = {{"candidate": candidate}}
    ns.update({{k: v for k, v in vars(candidate).items() if not k.startswith("__")}})
except Exception:
    traceback.print_exc(file=sys.stderr)
    raise SystemExit(1)
for i, src in enumerate(CHECKS):
    try:
        exec(src, ns)
    except Exception as exc:
        print("FAIL check %d: %s: %s" % (i, type(exc).__name__, exc))
    else:
        print("PASS %d" % i)
'''


def check_claims(response: str, claims: list[tuple[str, str]]) -> Verdict:
    """Score prose against ``(label, pattern)`` regexes, case-insensitively.

    Deliberately weaker than `run_checks` and labelled ``claim`` for it. A
    regex can confirm that a response states the right answer; it cannot
    confirm the reasoning that produced it, and a document that presents the
    two as one number is the thing this harness replaces.
    """
    text = response or ""
    passed, detail = 0, []
    for label, pattern in claims:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            passed += 1
        else:
            detail.append(f"missing: {label}")
    return Verdict(kind="claim", passed=passed, total=len(claims), detail=detail,
                   no_code=not text.strip(),
                   # Claim checks have no edge tier: each one is a distinct
                   # question the prompt asked, so all of them are core.
                   core_passed=passed, core_total=len(claims))
