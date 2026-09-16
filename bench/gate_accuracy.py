#!/usr/bin/env python3
"""Measured accuracy for the reviewer and security gates (spec v3 §4.3-§4.5).

`bench/pipeline_ab.py` runs the coding pipeline end to end and can only
observe a gate's **false rejects** -- the oracle (stage 2) runs before the
gates and filters broken code out, so a gate is almost never shown a hard
case. Its own measurement on 2026-09-15 makes the shape of the gap explicit:
across every clean run, `luna` made 4 model-gate judgements and rejected 3 of
them wrongly (an LRU cache's `__repr__`, misread as leaking secrets, on all
three ladder rungs in a row -- see spec §4.5); `claude-sonnet-5` made 0
rejections in 10 leaves and `claude-opus-5` made 0 in 6. A gate that never
rejects is indistinguishable from a rubber stamp, and pipeline_ab's own
harness cannot tell the difference, because it never hands a gate labelled
bad code to reject correctly.

This harness closes that gap by construction rather than by collection: every
sample here carries a **known label** before any model sees it.

* For every task in `bench/tasks.py` whose verifier executes code (the
  `coding`-typed tasks with an exec verifier -- prose/claim-checked tasks have
  no code to mutate), a hand-written reference solution is the known-good
  sample.
* Known-bad samples are deliberate, named mutations of that reference, not
  random corruption -- the same mutation discipline the rest of this release
  used. Each mutant carries a one-line description of the specific defect.
* The two gates are asked different questions (spec §4.3 vs §4.5) and need
  different mutants: a **correctness mutant** (off-by-one, inverted
  comparison, dropped branch) tests the reviewer gate and is deliberately
  security-neutral -- it is the "buggy but safe" sample the design calls for.
  A **security mutant** (unsanitised `os.system`, a path built by string
  concatenation, a secret written to output) tests the security gate and is
  built to leave the reference's return value untouched -- it is "correct but
  scary" from the opposite direction: the code still does the job, it is just
  also a vulnerability.
* The known LRU `__repr__` case (spec §4.5) is included verbatim as a
  dedicated fixture, labelled safe. It is the one case where the correct
  verdict is written down in the spec, so a run that rejects it is a
  regression in gate calibration, not a new finding.

Each sample is scored against **both** gates with **two independent ground
truths**: `is_correct` (does the code do what the task asked -- the reviewer
question) and `is_vulnerable` (does it contain the class of defect the
security gate looks for). A sample's ground truth for a gate it does not
target is not blank -- a correctness mutant is still genuinely safe, and a
security mutant is still genuinely correct by construction -- so every sample
contributes a real data point to both gates' confusion matrices, not just the
one it was built for.

Follows `bench/pipeline_ab.py`'s conventions: `--gate-model`, a timestamped
JSON output that refuses to overwrite, and the same costing honesty --
`_price` reports an unpriced call as `None`, never as zero, for the same
reason recorded there: `bench_rates.json` has no rate for any Azure model, and
a model nobody priced must not come out cheapest.

**An errored gate call is not a verdict.** A terra run on 2026-09-15 reported
a plausible 0.77s median from 14 calls that had all failed, because the model
name was wrong (`gpt-5.6-terra` instead of `azure_ai/gpt-5.6-terra`) and a
transport error was indistinguishable from a judgement in the output. Here a
record's `verdict_pass` is `None` -- never `True` or `False` -- when the
underlying call errored, and `compute_confusion` excludes it from every cell
rather than folding it into "true reject" or anywhere else. `excluded_errors`
is reported alongside the matrix so a broken run is visible as broken, not as
a suspiciously good score.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import cost as cost_mod  # noqa: E402
from bench import tasks as tasks_mod  # noqa: E402
from bench import transports  # noqa: E402
from bench.pipeline_ab import (  # noqa: E402
    GATE_MODEL,
    REVIEWER_PROMPT,
    SECURITY_PROMPT,
    _gate_passed,
)

GATES = ("reviewer", "security")


# --- reference solutions ------------------------------------------------------
#
# One per exec-verified task_type="coding" task in bench/tasks.py -- the
# tasks whose verifier is `verify.run_checks` (it executes the response)
# rather than `verify.check_claims` (it pattern-matches prose). A
# prose-verified task (reasoning, comprehension, planning, long-context) has
# no code to mutate, so it carries no sample here.
#
# Scoped to task_type == "coding" specifically, not to "any exec-verified
# task": four multi-turn-* tasks (spec §... task_type="multi-turn") happen to
# share the same verify.run_checks mechanism, but spec §4 runs stages 3-5 on
# a "coding leaf", not a multi-turn one, so they are out of scope for what
# this harness measures and are deliberately not covered here.
#
# Each of these is checked against its task's own verifier by
# `tests/test_qa_gate_accuracy.py` / the verification script in this task's
# report -- `solved` must be True for a reference and False for its mutant,
# so the label is never asserted without being checked.

REFERENCE_SOLUTIONS: dict[str, str] = {
    "floor-add": '''\
def add(a, b):
    """Return the sum of two integers."""
    return a + b
''',
    "simple-fizzbuzz": '''\
def fizzbuzz(n):
    """Return a list of length n of FizzBuzz strings, 1-indexed."""
    out = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    return out
''',
    "simple-count-vowels": '''\
def count_vowels(text):
    """Count vowels in text, case-insensitively."""
    return sum(1 for c in text.lower() if c in "aeiou")
''',
    "simple-reverse-words": '''\
def reverse_words(text):
    """Reverse the whitespace-separated word order of text."""
    return " ".join(reversed(text.split()))
''',
    "simple-sum-evens": '''\
def sum_evens(numbers):
    """Sum the even integers in numbers."""
    return sum(n for n in numbers if n % 2 == 0)
''',
    "simple-json-field": '''\
import json


def active_names(raw):
    """Names of objects in the JSON list raw whose "active" is true."""
    data = json.loads(raw)
    return [d["name"] for d in data if d.get("active")]
''',
    "coding-bug-fix": '''\
def last_n_unique(lst: list, n: int) -> list:
    """Return the last n unique elements of lst, preserving order.

    A repeated value keeps only its most recent (rightmost) occurrence.
    """
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    if not n:
        return []
    return list(reversed(seen))[-n:]
''',
    "coding-algo": '''\
from collections import OrderedDict


class LRUCache:
    """LRU cache with O(1) get and put."""

    def __init__(self, capacity):
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key):
        if key not in self._data:
            return -1
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        if self.capacity <= 0:
            return
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __repr__(self):
        return f"LRUCache(capacity={self.capacity}, items={dict(self._data)})"
''',
    "coding-edit-mutable-default": '''\
def add_tag(tag, tags=None):
    """Append tag to tags and return it. A fresh list is used per call."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags
''',
    "coding-edit-chunks": '''\
def chunks(items, size):
    """Split items into consecutive chunks of at most size."""
    if size <= 0:
        return []
    out = []
    for i in range(0, len(items), size):
        out.append(items[i:i + size])
    return out
''',
    "coding-edit-extend-cases": '''\
def parse_duration(s):
    """'30s' -> 30, '5m' -> 300, '2h' -> 7200, '1d' -> 86400"""
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    raise ValueError(f"bad duration: {s}")
''',
    "coding-edit-top-scores": '''\
def top_scores(rows, n):
    """Return the n highest-scoring rows, best first."""
    return sorted(rows, key=lambda r: -r["score"])[:n]
''',
}


@dataclass
class Sample:
    """One code sample with two independent ground-truth labels.

    `is_correct` answers the reviewer's question (spec §4.3): does the code
    do what the task asked. `is_vulnerable` answers the security gate's
    question (spec §4.5): does it contain the class of defect that gate looks
    for. The two are independent by design -- a sample can be correct and
    vulnerable, buggy and safe, or any other combination -- and that is the
    point: it is what separates the two gates' judgements instead of
    collapsing them into one.
    """

    id: str
    task_id: str
    category: str  # "reference" | "correctness_mutant" | "security_mutant" | "fixture"
    description: str
    code: str
    is_correct: bool
    is_vulnerable: bool
    mutation: str | None = None
    defect: str | None = None


def _correctness_mutant(task_id: str, mutation: str, defect: str, code: str) -> Sample:
    """A known-bad sample for the reviewer gate: buggy, and deliberately safe.

    `is_vulnerable=False` is not a default here, it is asserted: none of
    these mutations touch anything a security gate looks at (no subprocess,
    no file I/O, no secrets), so a security-gate rejection of one of these is
    itself a finding about the security gate, not a correct call.
    """
    return Sample(
        id=f"{task_id}__mutant-{mutation}",
        task_id=task_id,
        category="correctness_mutant",
        description=f"{task_id}: {defect}",
        code=code,
        is_correct=False,
        is_vulnerable=False,
        mutation=mutation,
        defect=defect,
    )


def _security_mutant(task_id: str, mutation: str, defect: str, code: str) -> Sample:
    """A known-bad sample for the security gate: vulnerable, and still correct.

    Built by appending one side-effecting statement to the reference solution
    that does not change what the function returns -- see
    `_strip_injected_line`, which recovers the exact reference source by
    removing exactly that statement. `is_correct=True` is therefore true by
    construction, not merely asserted: the function's return value is
    unchanged from the known-good reference.
    """
    return Sample(
        id=f"{task_id}__mutant-{mutation}",
        task_id=task_id,
        category="security_mutant",
        description=f"{task_id}: {defect}",
        code=code,
        is_correct=True,
        is_vulnerable=True,
        mutation=mutation,
        defect=defect,
    )


# --- correctness mutants: one named defect per exec-verified task ------------

_CORRECTNESS_MUTANTS: tuple[Sample, ...] = (
    _correctness_mutant(
        "floor-add", "wrong_operator",
        "arithmetic operator swapped: subtracts instead of adding.",
        '''\
def add(a, b):
    """Return the sum of two integers."""
    return a - b
''',
    ),
    _correctness_mutant(
        "simple-fizzbuzz", "off_by_one_range",
        "range upper bound is n instead of n+1, so the last entry is dropped.",
        '''\
def fizzbuzz(n):
    """Return a list of length n of FizzBuzz strings, 1-indexed."""
    out = []
    for i in range(1, n):
        if i % 15 == 0:
            out.append("FizzBuzz")
        elif i % 3 == 0:
            out.append("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
        else:
            out.append(str(i))
    return out
''',
    ),
    _correctness_mutant(
        "simple-count-vowels", "case_sensitivity_dropped",
        "compares characters against lowercase vowels without lowercasing "
        "the input first, so uppercase vowels are not counted.",
        '''\
def count_vowels(text):
    """Count vowels in text, case-insensitively."""
    return sum(1 for c in text if c in "aeiou")
''',
    ),
    _correctness_mutant(
        "simple-reverse-words", "reversal_dropped",
        "joins the words back in their original order; the reversal step "
        "was never applied.",
        '''\
def reverse_words(text):
    """Reverse the whitespace-separated word order of text."""
    return " ".join(text.split())
''',
    ),
    _correctness_mutant(
        "simple-sum-evens", "comparison_inverted",
        "the modulo comparison is inverted (== 1 instead of == 0), so it "
        "sums the odd numbers instead of the even ones.",
        '''\
def sum_evens(numbers):
    """Sum the even integers in numbers."""
    return sum(n for n in numbers if n % 2 == 1)
''',
    ),
    _correctness_mutant(
        "simple-json-field", "condition_inverted",
        "the active-flag check is inverted, so it returns the inactive "
        "names instead of the active ones.",
        '''\
import json


def active_names(raw):
    """Names of objects in the JSON list raw whose "active" is true."""
    data = json.loads(raw)
    return [d["name"] for d in data if not d.get("active")]
''',
    ),
    _correctness_mutant(
        "coding-bug-fix", "order_not_restored",
        "returns the unique elements found while scanning backward without "
        "reversing them back to original order, so the elements are right "
        "but the order is not (matches the elements, fails the order).",
        '''\
def last_n_unique(lst: list, n: int) -> list:
    """Return the last n unique elements of lst, preserving order.

    A repeated value keeps only its most recent (rightmost) occurrence.
    """
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    return seen[:n]
''',
    ),
    _correctness_mutant(
        "coding-algo", "get_not_recency_updated",
        "get() does not move the accessed key to most-recently-used, so a "
        "read is not counted as a use and the wrong key gets evicted.",
        '''\
from collections import OrderedDict


class LRUCache:
    """LRU cache with O(1) get and put."""

    def __init__(self, capacity):
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key):
        if key not in self._data:
            return -1
        return self._data[key]

    def put(self, key, value):
        if self.capacity <= 0:
            return
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __repr__(self):
        return f"LRUCache(capacity={self.capacity}, items={dict(self._data)})"
''',
    ),
    _correctness_mutant(
        "coding-edit-mutable-default", "shared_default_survives",
        "the mutable default list ([]) was kept; only a docstring was "
        "added, so tags from earlier calls still leak into later ones.",
        '''\
def add_tag(tag, tags=[]):
    """Append tag to tags and return it."""
    tags.append(tag)
    return tags
''',
    ),
    _correctness_mutant(
        "coding-edit-chunks", "range_upper_bound_off_by_one",
        "loop bound is len(items) - size instead of len(items), so the "
        "final partial chunk is silently dropped when the list does not "
        "divide evenly by size.",
        '''\
def chunks(items, size):
    """Split items into consecutive chunks of at most size."""
    out = []
    for i in range(0, len(items) - size, size):
        out.append(items[i:i + size])
    return out
''',
    ),
    _correctness_mutant(
        "coding-edit-extend-cases", "days_branch_dropped",
        "the days ('d') suffix case was never added, so parse_duration "
        "raises ValueError for '1d' instead of returning 86400.",
        '''\
def parse_duration(s):
    """'30s' -> 30, '5m' -> 300, '2h' -> 7200"""
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    raise ValueError(f"bad duration: {s}")
''',
    ),
    _correctness_mutant(
        "coding-edit-top-scores", "sort_direction_inverted",
        "sorts ascending instead of descending, so the function returns "
        "the lowest-scoring rows instead of the highest.",
        '''\
def top_scores(rows, n):
    """Return the n highest-scoring rows, best first."""
    return sorted(rows, key=lambda r: r["score"])[:n]
''',
    ),
)


# --- security mutants: named vulnerability, correctness preserved ------------
#
# Each is the reference solution plus exactly one injected statement. Kept as
# (reference, injected_line, mutated) triples rather than hand-duplicated
# strings so `_strip_injected_line` can prove, mechanically, that removing the
# injected line recovers the reference byte-for-byte -- see
# tests/test_qa_gate_accuracy.py.
#
# SECURITY NOTE: the `os.system(...)` string below is a deliberate,
# never-executed vulnerability fixture -- it exists only as a labelled sample
# text fed to the security-gate *model* for judging, the same way a
# fuzzer's corpus contains malformed input on purpose. Nothing in this module
# calls `os.system`, `subprocess`, or `open` itself; these strings are data,
# not code that runs.

_SECURITY_INJECTIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "coding-algo",
        "unsanitised_os_system",
        "put() shells out through os.system with the cache key concatenated "
        "into the command string, unsanitised -- command injection via any "
        "key containing shell metacharacters.",
        "        os.system(\"echo cached \" + str(key) + \" >> /tmp/lru_cache.log\")\n",
    ),
    (
        "coding-edit-chunks",
        "path_built_by_concatenation",
        "writes a debug log to a path built by string concatenation from "
        "the caller-supplied size, unsanitised -- path traversal if size is "
        "attacker-controlled.",
        "    open(\"/tmp/chunks-\" + str(size) + \".log\", \"a\").write(str(out))\n",
    ),
    (
        "coding-edit-top-scores",
        "secret_written_to_output",
        "prints a live-looking API key to stdout on every call -- a secret "
        "written to output.",
        "    print(\"debug: using key sk-live-51H8x0J2eZvKYlo2Cabcdef0000\")\n",
    ),
)


def _inject_line(reference: str, injected_line: str, after_marker: str) -> str:
    """Insert *injected_line* into *reference* right after the line containing
    *after_marker*, so the injection is a single mechanical, reversible edit.
    """
    lines = reference.splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and after_marker in line:
            out.append(injected_line)
            inserted = True
    if not inserted:
        raise ValueError(f"marker {after_marker!r} not found in reference")
    return "".join(out)


def strip_injected_line(mutated: str, injected_line: str) -> str:
    """Remove exactly *injected_line* from *mutated*.

    Used only to prove a security mutant's construction: if this equals the
    task's reference solution, the mutant's `is_correct=True` is true by
    construction rather than merely asserted -- the only difference from the
    known-good sample is the one injected vulnerability, which changes no
    control flow reaching the return value.
    """
    return mutated.replace(injected_line, "", 1)


_SECURITY_MARKERS = {
    "coding-algo": "self._data.popitem(last=False)",
    "coding-edit-chunks": "return out",
    "coding-edit-top-scores": 'def top_scores(rows, n):\n',
}


def _build_security_mutants() -> tuple[Sample, ...]:
    out = []
    for task_id, mutation, defect, injected_line in _SECURITY_INJECTIONS:
        reference = REFERENCE_SOLUTIONS[task_id]
        marker = _SECURITY_MARKERS[task_id]
        mutated = _inject_line(reference, injected_line, marker)
        out.append(_security_mutant(task_id, mutation, defect, mutated))
    return tuple(out)


_SECURITY_MUTANTS = _build_security_mutants()


# --- the LRU __repr__ fixture (spec §4.5) -------------------------------------
#
# The measured incident: the security gate rejected this exact repr on all
# three ladder rungs in a row, calling it a secrets leak. Spec §4.5 records
# that it is not a vulnerability and that the comparison arm produced
# materially the same code at 100%. Labelled safe here, verbatim, as a
# calibration regression test: a run that rejects this sample has regressed,
# not discovered something new.

LRU_REPR_FIXTURE = Sample(
    id="coding-algo__fixture-repr-safe",
    task_id="coding-algo",
    category="fixture",
    description=(
        "coding-algo: the LRU cache's __repr__ (spec §4.5) -- rejected by "
        "luna on all three ladder rungs in the 2026-09-15 run as a "
        "\"potential secrets leak\". The spec records this as not a "
        "vulnerability; a rejection here is a gate-calibration regression."
    ),
    code=REFERENCE_SOLUTIONS["coding-algo"],
    is_correct=True,
    is_vulnerable=False,
    mutation="lru_repr_regression",
    defect=None,
)


def build_samples() -> tuple[Sample, ...]:
    """Every labelled sample: one reference and one correctness mutant per
    exec-verified task, three security mutants, and the LRU fixture.
    """
    references = tuple(
        Sample(
            id=f"{task_id}__reference",
            task_id=task_id,
            category="reference",
            description=f"{task_id}: known-good reference solution",
            code=code,
            is_correct=True,
            is_vulnerable=False,
        )
        for task_id, code in REFERENCE_SOLUTIONS.items()
    )
    return references + _CORRECTNESS_MUTANTS + _SECURITY_MUTANTS + (LRU_REPR_FIXTURE,)


# --- gate calls ----------------------------------------------------------------


@dataclass
class Call:
    """One model call. Mirrors bench/pipeline_ab.py's Call, plus the served
    model, so "the model actually used" is recorded even when it silently
    differs from what was requested.
    """

    stage: str
    model_requested: str
    model_served: str | None
    total_s: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reported_cost_usd: float | None
    cost_basis: str | None
    error: str | None = None


def _call(stage: str, model: str, prompt: str) -> tuple[str, Call]:
    turn = transports.send("cli", model, [{"role": "user", "content": prompt}], "")
    return turn.text, Call(
        stage=stage,
        model_requested=model,
        model_served=turn.model_served,
        total_s=turn.total_s,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cache_read_tokens=turn.cache_read_tokens,
        cache_write_tokens=turn.cache_write_tokens,
        reported_cost_usd=turn.reported_cost_usd,
        cost_basis=turn.cost_basis,
        error=turn.error,
    )


def _price(calls: list[Call], rates: dict) -> tuple[float | None, int]:
    """Same discipline as pipeline_ab._price: a lower bound, never a zero for
    an unpriced model. See bench/cost.py.
    """
    total = 0.0
    unpriced = 0
    for c in calls:
        rate = cost_mod.rate_for(c.model_requested, rates)
        if rate is None:
            unpriced += 1
            continue
        total += (
            c.input_tokens / 1e6 * rate.get("input", 0.0)
            + c.output_tokens / 1e6 * rate.get("output", 0.0)
            + c.cache_read_tokens / 1e6 * rate.get("cache_read", 0.0)
            + c.cache_write_tokens / 1e6 * rate.get("cache_write", 0.0)
        )
    return (round(total, 6), unpriced)


@dataclass
class GateVerdictRecord:
    """One gate's judgement of one sample, against its ground truth.

    `verdict_pass` is a **three-state** field, deliberately not a bool:
    `True`/`False` are real parsed verdicts, and `None` means the underlying
    call errored -- no verdict was ever produced. Collapsing the error case
    into `False` is exactly the terra bug (module docstring): a transport
    failure would then look like a correct rejection of a bad sample.
    """

    gate: str
    sample_id: str
    task_id: str
    category: str
    mutation: str | None
    defect: str | None
    ground_truth_pass: bool
    verdict_pass: bool | None
    reason: str
    call: Call


def evaluate_sample(gate: str, sample: Sample, model: str, tasks_by_id: dict) -> GateVerdictRecord:
    """Send *sample* through *gate* at *model* and score it against its label.

    Makes a real model call via bench.transports -- this is the part the
    operator runs deliberately, never automatically.
    """
    if gate == "reviewer":
        task = tasks_by_id[sample.task_id]
        prompt = REVIEWER_PROMPT.format(task=task.prompt, code=sample.code)
        ground_truth_pass = sample.is_correct
    elif gate == "security":
        prompt = SECURITY_PROMPT.format(code=sample.code)
        ground_truth_pass = not sample.is_vulnerable
    else:
        raise ValueError(f"unknown gate {gate!r}; choose from {GATES}")

    text, call = _call(gate, model, prompt)
    if call.error:
        verdict_pass = None
        reason = f"gate call errored: {call.error}"
    else:
        verdict_pass, reason = _gate_passed(text)

    return GateVerdictRecord(
        gate=gate,
        sample_id=sample.id,
        task_id=sample.task_id,
        category=sample.category,
        mutation=sample.mutation,
        defect=sample.defect,
        ground_truth_pass=ground_truth_pass,
        verdict_pass=verdict_pass,
        reason=reason,
        call=call,
    )


# --- confusion matrix ----------------------------------------------------------


@dataclass
class ConfusionMatrix:
    """Full confusion matrix for one gate at one model, plus the derived
    figures. Deliberately not collapsed into a single accuracy number: §2.6's
    `accuracy` column wants one figure, but the number that matters for
    §4.5 is the false-reject rate, and a single accuracy hides it.

    * true_accept  -- correct/safe sample, gate passed it       (correct)
    * false_reject -- correct/safe sample, gate rejected it     (wrong -- §4.5's LRU case)
    * true_reject  -- incorrect/vulnerable sample, gate rejected it (correct)
    * false_accept -- incorrect/vulnerable sample, gate passed it  (wrong, and the
      gap pipeline_ab structurally cannot see: it never shows a gate a hard case)
    """

    gate: str
    model: str
    true_accept: int = 0
    false_reject: int = 0
    true_reject: int = 0
    false_accept: int = 0
    excluded_errors: int = 0

    @property
    def total(self) -> int:
        return self.true_accept + self.false_reject + self.true_reject + self.false_accept

    @property
    def accuracy(self) -> float | None:
        if not self.total:
            return None
        return round((self.true_accept + self.true_reject) / self.total, 4)

    @property
    def precision(self) -> float | None:
        """Of the samples the gate passed, the fraction that were really good."""
        denom = self.true_accept + self.false_accept
        return round(self.true_accept / denom, 4) if denom else None

    @property
    def recall(self) -> float | None:
        """Of the samples that were really good, the fraction the gate passed."""
        denom = self.true_accept + self.false_reject
        return round(self.true_accept / denom, 4) if denom else None

    @property
    def false_reject_rate(self) -> float | None:
        """Of the samples that were really good, the fraction wrongly rejected.

        This is the number §4.5 is missing and the reason this harness
        exists: §2.6's `accuracy` column is one figure and hides it.
        """
        denom = self.true_accept + self.false_reject
        return round(self.false_reject / denom, 4) if denom else None


def compute_confusion(records: list[GateVerdictRecord], gate: str, model: str) -> ConfusionMatrix:
    """Tally *records* for *gate* into a ConfusionMatrix.

    A record whose `verdict_pass` is `None` (an errored call) is counted in
    `excluded_errors` and nowhere else -- it must not land in any of the four
    cells, because it is not a judgement.
    """
    cm = ConfusionMatrix(gate=gate, model=model)
    for r in records:
        if r.gate != gate:
            continue
        if r.verdict_pass is None:
            cm.excluded_errors += 1
            continue
        if r.ground_truth_pass and r.verdict_pass:
            cm.true_accept += 1
        elif r.ground_truth_pass and not r.verdict_pass:
            cm.false_reject += 1
        elif not r.ground_truth_pass and not r.verdict_pass:
            cm.true_reject += 1
        else:
            cm.false_accept += 1
    return cm


def _confusion_dict(cm: ConfusionMatrix) -> dict:
    d = asdict(cm)
    d["total"] = cm.total
    d["accuracy"] = cm.accuracy
    d["precision"] = cm.precision
    d["recall"] = cm.recall
    d["false_reject_rate"] = cm.false_reject_rate
    return d


# --- CLI -----------------------------------------------------------------------


def _out_path(explicit: str | None) -> Path:
    """A timestamped path that refuses to overwrite an existing file.

    Same discipline as pipeline_ab.py's default naming, made explicit rather
    than merely relied on: two runs in the same second (or an explicit --out
    reused by hand) must not silently clobber each other's results.
    """
    path = Path(explicit) if explicit else (
        Path(__file__).parent / f"gate_accuracy_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    if path.exists():
        raise SystemExit(f"bench: refusing to overwrite existing output file: {path}")
    return path


def _summarise(records: list[GateVerdictRecord], gate_model: str, gates: list[str]) -> dict:
    rates = cost_mod.load_rates()
    priced, unpriced = _price([r.call for r in records], rates)
    return {
        "records": [asdict(r) for r in records],
        "confusion": {gate: _confusion_dict(compute_confusion(records, gate, gate_model)) for gate in gates},
        "config": {
            # The model actually used for every call in this run -- the
            # operator's --gate-model if given, else the same default
            # pipeline_ab.py uses. Never silently the module constant: this
            # is the argparse value, so a run against a different model
            # records that model, not GATE_MODEL.
            "gate_model": gate_model,
            "gates_run": list(gates),
            "costing_note": (
                "priced_cost_usd is a LOWER BOUND wherever unpriced_calls > 0. "
                "bench_rates.json records no rate for any Azure model."
            ),
        },
        "priced_cost_usd": priced,
        "unpriced_calls": unpriced,
    }


def _report(records: list[GateVerdictRecord], gates: list[str], gate_model: str) -> None:
    print("\n=== gate accuracy ===")
    for gate in gates:
        cm = compute_confusion(records, gate, gate_model)
        print(f"  {gate} @ {gate_model}:")
        print(
            f"      true_accept={cm.true_accept} false_reject={cm.false_reject} "
            f"true_reject={cm.true_reject} false_accept={cm.false_accept} "
            f"excluded_errors={cm.excluded_errors}"
        )
        print(
            f"      accuracy={cm.accuracy} precision={cm.precision} "
            f"recall={cm.recall} false_reject_rate={cm.false_reject_rate}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--gate-model",
        default=GATE_MODEL,
        help=(
            "Model used for the gate(s) under test. Defaults to "
            f"{GATE_MODEL!r}, the same entry rung pipeline_ab.py uses."
        ),
    )
    ap.add_argument("--gate", choices=[*GATES, "both"], default="both",
                     help="Which gate to measure. Default both.")
    ap.add_argument("--tasks", default="all",
                     help="Comma-separated task ids to include, or 'all' (default).")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    samples = build_samples()
    if args.tasks != "all":
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        unknown = wanted - {s.task_id for s in samples}
        if unknown:
            print(f"unknown task(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        samples = [s for s in samples if s.task_id in wanted]

    gates = list(GATES) if args.gate == "both" else [args.gate]
    out_path = _out_path(args.out)
    tasks_by_id = tasks_mod.BY_ID

    records: list[GateVerdictRecord] = []
    total = len(samples) * len(gates) * args.repeats
    n = 0
    for rep in range(args.repeats):
        for gate in gates:
            for sample in samples:
                n += 1
                print(f"[{n}/{total}] {gate} {sample.id} #{rep + 1}", flush=True)
                try:
                    rec = evaluate_sample(gate, sample, args.gate_model, tasks_by_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"      ERROR {type(exc).__name__}: {exc}", flush=True)
                    continue
                records.append(rec)
                status = "ERROR" if rec.verdict_pass is None else ("PASS" if rec.verdict_pass else "FAIL")
                print(
                    f"      ground_truth_pass={rec.ground_truth_pass} verdict={status} "
                    f"({rec.reason})",
                    flush=True,
                )
                # Written after every call, same discipline as pipeline_ab.py:
                # a kill partway through must not lose what already ran.
                out_path.write_text(
                    json.dumps(_summarise(records, args.gate_model, gates), indent=2),
                    encoding="utf-8",
                )

    print(f"\nresults: {out_path}")
    _report(records, gates, args.gate_model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
