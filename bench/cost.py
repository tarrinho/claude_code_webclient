"""Cost per *correct* answer, which is the unit that decides anything.

The old Delegation Score weighted Correctness 25, Completeness 15, Reasoning 15,
Code Quality 10, Constraint Handling 10, Consistency 9, Clarity 8 and Speed 8 --
and cost at zero. So it priced every backend the same and ranked the only free
one last. That is not a rounding error in the methodology; it is the methodology
being unable to express the question.

Two deliberate choices here:

* **Cost per correct answer, not cost per query.** A free model that needs two
  attempts is still free. A cheap model that is wrong once can cost more than
  an expensive model that is right first time, and per-query pricing cannot
  say so.
* **No cost weight is folded into a single score.** Burying the trade-off
  inside one number is what produced a 55.4% nobody could take apart. Cost is
  reported beside quality, and the choice between them is stated as a choice.

Rates are **not** hardcoded. They are per-deployment commercial facts that go
stale, and inventing them would be worse than leaving the column empty: a made
up number is indistinguishable from a measured one once it is in a table. Set
them in `bench_rates.json` at the repository root, or point `WC_BENCH_RATES` at
a file:

    {"azure_ai/gpt-5.6-luna": {"input": 1.25, "output": 10.0},
     "vllm/*":                {"input": 0.0,  "output": 0.0}}

Units are US dollars per million tokens. `*` suffix globs a prefix, so a
self-hosted family can be declared free in one line.
"""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_rates() -> dict[str, dict[str, float]]:
    """Rates from `WC_BENCH_RATES` or `bench_rates.json`; empty if absent.

    An empty mapping is a supported state and means "cost unknown", which is
    reported as such. It never becomes zero -- a model whose price nobody
    recorded must not appear free.
    """
    candidates = []
    if os.environ.get("WC_BENCH_RATES"):
        candidates.append(Path(os.environ["WC_BENCH_RATES"]))
    candidates.append(ROOT / "bench_rates.json")
    for path in candidates:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                # Underscore keys are documentation, not backends. The rates
                # file carries a long `_comment` explaining where the numbers
                # came from, and without this filter `rate_for("_comment")`
                # returns that comment as though it were a rate card.
                return {k: v for k, v in data.items()
                        if not k.startswith("_") and isinstance(v, dict)}
    return {}


def rate_for(model: str, rates: dict) -> dict[str, float] | None:
    """The rate entry for *model*, exact match first, then glob."""
    if model in rates:
        return rates[model]
    for pattern, entry in rates.items():
        if pattern.endswith("*") and fnmatch.fnmatch(model, pattern):
            return entry
    return None


@dataclass
class Spend:
    """What a set of runs cost, and what each correct answer cost."""

    model: str
    input_tokens: int
    output_tokens: int
    runs: int
    correct: int
    known: bool
    dollars: float | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: Where `dollars` came from. Printed, because a vendor-reported figure and
    #: one computed from a rate card deserve different amounts of trust.
    source: str | None = None
    #: The vendor's own basis, e.g. 'list'. Absent for a computed figure.
    basis: str | None = None
    detail: str | None = None

    @property
    def per_correct(self) -> float | None:
        """Dollars per correct answer.

        `None` when rates are unknown. `0.0` when the backend is free and it
        got something right -- which is the finding the old table could not
        express. When nothing was correct this is None rather than infinity,
        because "cost per correct answer" of a model that was never correct is
        undefined, and printing `inf` invites it being read as a big number
        rather than as no answer.
        """
        if not self.known or self.dollars is None or not self.correct:
            return None
        return round(self.dollars / self.correct, 6)

    @property
    def note(self) -> str:
        if not self.known:
            return "rate not recorded"
        if self.dollars == 0:
            return "free (self-hosted)"
        if not self.correct:
            return "no correct answers; cost per correct undefined"
        return ""


def summarise(model: str, results: list, rates: dict) -> Spend:
    """Total spend and cost-per-correct for one model's results.

    Two sources, in order of authority.

    **The vendor's own figure wins.** The CLI reports `total_cost_usd` per turn
    together with a `costBasis`, and where the basis is `list` that is the
    number being billed. Nothing computed here can beat it.

    **Otherwise, compute from the rate card -- including cache.** Costing a
    turn from `input_tokens` alone undercounts it badly: the CLI caches its
    system prompt and tool definitions, so opus-5 reported a constant 12,029
    input tokens for every task including the 30k-token long-context one, and
    haiku-4-5 reported 10. The real volume was 12,226 cache writes and 18,905
    cache reads on a single trivial turn. A cost table built on `input_tokens`
    would have understated the expensive models by an order of magnitude and
    published it as measured.
    """
    entry = rate_for(model, rates)
    inp = sum(r.get("input_tokens", 0) for r in results)
    out = sum(r.get("output_tokens", 0) for r in results)
    cache_r = sum(r.get("cache_read_tokens", 0) for r in results)
    cache_w = sum(r.get("cache_write_tokens", 0) for r in results)
    correct = sum(1 for r in results if r["correct"])

    reported = [r.get("reported_cost_usd") for r in results
                if r.get("reported_cost_usd") is not None]
    bases = {r.get("cost_basis") for r in results if r.get("cost_basis")}

    spend = Spend(
        model=model, input_tokens=inp, output_tokens=out,
        cache_read_tokens=cache_r, cache_write_tokens=cache_w,
        runs=len(results), correct=correct,
        known=bool(reported) or entry is not None,
    )
    if reported and len(reported) == len(results):
        spend.dollars = round(sum(reported), 6)
        spend.source = "reported by the CLI"
        spend.basis = "/".join(sorted(bases)) if bases else None
    elif entry is not None:
        spend.dollars = round(
            inp / 1_000_000 * float(entry.get("input", 0.0))
            + out / 1_000_000 * float(entry.get("output", 0.0))
            + cache_r / 1_000_000 * float(entry.get("cache_read", 0.0))
            + cache_w / 1_000_000 * float(entry.get("cache_write", 0.0)),
            6,
        )
        spend.source = "computed from bench_rates.json"
        if reported:
            # Partial vendor data: say so rather than mixing the two silently.
            spend.detail = (f"{len(reported)} of {len(results)} runs also "
                            "reported a cost; computed figure used for "
                            "consistency")
    return spend
