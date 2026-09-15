#!/usr/bin/env python3
"""A/B experiment for the tiered agent delegation spec (v3).

The spec is 656 lines of mechanism resting on one untested hypothesis: that a
free model plus a gate stack reaches the quality of a single expensive call,
cheaply enough to be worth the extra wall-clock. Nothing in the spec's own
observability section tests that -- section 10.1 measures when to *widen* the
free rung, never whether the gate stack works at all.

This runs both arms over the same tasks and reports the three numbers the
design bets on:

    A (baseline)  one claude-sonnet-5 call
    B (pipeline)  free generation -> oracle -> reviewer gate -> security gate,
                  escalating the generator one rung on any rejection

    per arm: exec-verified correctness, total wall-clock, total cost,
             number of model calls, and which rung finished the work

The escalation rate this produces is the single input the spec's budget
arithmetic turns on: at 40 leaves against a $1.00 tree budget, only ~6% of
leaves can escalate before the tree is over budget, while section 10.1 targets
70% free-rung (i.e. 30% escalation).

COSTING IS DELIBERATELY INCOMPLETE, and that is the honest result rather than a
gap in this script. `bench_rates.json` records no rate for any Azure model,
because nobody recorded what this deployment pays and the names look like
internal deployment aliases. Of 167,412 rows in `usage_events`, 156 carry
`cost_basis='list'` -- every other cost figure is Anthropic list pricing applied
to whichever backend actually served the turn, including $57.13 booked against a
self-hosted model that is free. So a gate running on luna has a real cost that
this script reports as `None`, never as zero: a model nobody priced must not
come out cheapest. See bench/cost.py, which already refuses the same way.

Stage 4 (QA / regression) is not a separate call here. For these benchmark
tasks the regression suite *is* the exec check of stage 2, so counting it twice
would inflate arm B's cost and latency with a call that does no additional work.
That collapse is a property of the task set, not of the design, and it is
recorded per-run as `stage4_collapsed`.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import cost as cost_mod  # noqa: E402
from bench import tasks as tasks_mod  # noqa: E402
from bench import transports  # noqa: E402

LADDER = ["vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5.6-luna", "claude-sonnet-5"]
BASELINE_MODEL = "claude-sonnet-5"
GATE_MODEL = "azure_ai/gpt-5.6-luna"

REVIEWER_PROMPT = """You are a reviewer gate. Judge INTENT MATCH only: does the \
code below do what the task asked? Do not judge style, and do not re-check \
whether it compiles -- that is already verified.

TASK:
{task}

CODE:
{code}

Answer with exactly one line, either:
PASS
or
FAIL: <one short sentence naming what the code does not do that the task asked for>"""

SECURITY_PROMPT = """You are a security review gate. Look only for: command \
injection, unsafe file operations, secrets written into output, unsafe \
deserialisation, or unsanitised subprocess use.

CODE:
{code}

Answer with exactly one line, either:
PASS
or
FAIL: <one short sentence naming the specific vulnerability>"""


@dataclass
class Call:
    """One model call inside an arm."""

    stage: str
    model: str
    total_s: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reported_cost_usd: float | None
    cost_basis: str | None
    error: str | None = None


@dataclass
class ArmResult:
    arm: str
    task: str
    task_type: str
    correct: bool
    score: float
    wall_s: float
    calls: list[Call] = field(default_factory=list)
    final_rung: str | None = None
    rung_index: int | None = None
    escalated: bool = False
    gate_rejections: list[str] = field(default_factory=list)
    stage4_collapsed: bool = True
    priced_cost_usd: float | None = None
    unpriced_calls: int = 0
    note: str = ""


def _rate_lookup(model: str, rates: dict) -> dict | None:
    return cost_mod.rate_for(model, rates)


def _price(calls: list[Call], rates: dict) -> tuple[float | None, int]:
    """Total cost across calls, and how many calls could not be priced.

    Returns (priced_total, unpriced_count). A model with no recorded rate
    contributes nothing to the total and increments the unpriced count -- the
    total is therefore a LOWER BOUND whenever unpriced_count > 0, and callers
    must not present it as the cost.
    """
    total = 0.0
    unpriced = 0
    for c in calls:
        rate = _rate_lookup(c.model, rates)
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


def _call(stage: str, model: str, prompt: str) -> tuple[str, Call]:
    turn = transports.send("cli", model, [{"role": "user", "content": prompt}], "")
    return turn.text, Call(
        stage=stage,
        model=model,
        total_s=turn.total_s,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cache_read_tokens=turn.cache_read_tokens,
        cache_write_tokens=turn.cache_write_tokens,
        reported_cost_usd=turn.reported_cost_usd,
        cost_basis=turn.cost_basis,
        error=turn.error,
    )


def _gate_passed(text: str) -> tuple[bool, str]:
    """Parse a gate verdict. An unparseable verdict is a FAIL, not a pass.

    Defaulting to pass would make a broken gate invisible and would flatter
    arm B exactly where the experiment is trying to measure it.
    """
    if not text:
        return (False, "empty gate response")
    head = text.strip().splitlines()[0].strip()
    if re.match(r"^\W*PASS\b", head, re.I):
        return (True, "")
    m = re.match(r"^\W*FAIL\b[:\-\s]*(.*)$", head, re.I)
    if m:
        return (False, m.group(1).strip() or "no reason given")
    if re.search(r"\bPASS\b", text[:400], re.I) and not re.search(r"\bFAIL\b", text[:400], re.I):
        return (True, "")
    return (False, f"unparseable verdict: {head[:80]}")


def run_baseline(task, rates: dict) -> ArmResult:
    t0 = time.time()
    text, call = _call("generation", BASELINE_MODEL, task.prompt)
    verdict = task.verifier(text)
    wall = time.time() - t0
    priced, unpriced = _price([call], rates)
    return ArmResult(
        arm="A-baseline",
        task=task.id,
        task_type=task.task_type,
        correct=bool(verdict.solved),
        score=float(verdict.score),
        wall_s=round(wall, 2),
        calls=[call],
        final_rung=BASELINE_MODEL,
        rung_index=0,
        priced_cost_usd=priced,
        unpriced_calls=unpriced,
        stage4_collapsed=True,
    )


def run_pipeline(task, rates: dict, max_attempts: int = 3) -> ArmResult:
    t0 = time.time()
    calls: list[Call] = []
    rejections: list[str] = []
    correct = False
    score = 0.0
    final_rung = None
    rung_index = None

    for idx, rung in enumerate(LADDER[:max_attempts]):
        final_rung, rung_index = rung, idx

        # Stage 1 -- generation
        text, gen_call = _call("generation", rung, task.prompt)
        calls.append(gen_call)
        if gen_call.error:
            rejections.append(f"rung {idx} ({rung}) transport error: {gen_call.error}")
            continue

        # Stage 2 -- oracle (execution verification). Stage 4 collapses into
        # this for these tasks; see module docstring.
        verdict = task.verifier(text)
        score = float(verdict.score)
        if not verdict.solved:
            why = "no extractable code" if verdict.no_code else f"core {verdict.core_passed}/{verdict.core_total}"
            rejections.append(f"rung {idx} ({rung}) oracle: {why} score={verdict.score}")
            continue

        code = text

        # Stage 3 -- reviewer gate (intent match)
        rtext, rcall = _call("reviewer", GATE_MODEL, REVIEWER_PROMPT.format(task=task.prompt, code=code))
        calls.append(rcall)
        ok, reason = _gate_passed(rtext)
        if not ok:
            rejections.append(f"rung {idx} ({rung}) reviewer: {reason}")
            continue

        # Stage 5 -- security gate
        stext, scall = _call("security", GATE_MODEL, SECURITY_PROMPT.format(code=code))
        calls.append(scall)
        ok, reason = _gate_passed(stext)
        if not ok:
            rejections.append(f"rung {idx} ({rung}) security: {reason}")
            continue

        correct = True
        break

    wall = time.time() - t0
    priced, unpriced = _price(calls, rates)
    return ArmResult(
        arm="B-pipeline",
        task=task.id,
        task_type=task.task_type,
        correct=correct,
        score=score,
        wall_s=round(wall, 2),
        calls=calls,
        final_rung=final_rung,
        rung_index=rung_index,
        escalated=bool(rung_index),
        gate_rejections=rejections,
        priced_cost_usd=priced,
        unpriced_calls=unpriced,
        stage4_collapsed=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="coding-bug-fix,coding-algo")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--arms", default="A,B")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    rates = cost_mod.load_rates()
    wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
    by_id = {t.id: t for t in tasks_mod.TASKS}
    missing = [w for w in wanted if w not in by_id]
    if missing:
        print(f"unknown task(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    selected = [by_id[w] for w in wanted]
    arms = [a.strip().upper() for a in args.arms.split(",")]

    out_path = Path(args.out) if args.out else (
        Path(__file__).parent / f"pipeline_ab_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )

    results: list[ArmResult] = []
    total = len(selected) * args.repeats * len(arms)
    n = 0
    for rep in range(args.repeats):
        for task in selected:
            for arm in arms:
                n += 1
                print(f"[{n}/{total}] {arm} {task.id} #{rep + 1}", flush=True)
                try:
                    r = run_baseline(task, rates) if arm == "A" else run_pipeline(task, rates)
                except Exception as exc:  # noqa: BLE001
                    print(f"      ERROR {type(exc).__name__}: {exc}", flush=True)
                    continue
                results.append(r)
                unp = f" unpriced_calls={r.unpriced_calls}" if r.unpriced_calls else ""
                print(
                    f"      correct={r.correct} score={r.score} wall={r.wall_s}s "
                    f"calls={len(r.calls)} rung={r.rung_index}{unp}",
                    flush=True,
                )
                for rej in r.gate_rejections:
                    print(f"        - {rej}", flush=True)
                # Written after every run, same discipline as wc-bench.py: a
                # kill partway through must not lose what already ran.
                out_path.write_text(json.dumps(_summarise(results), indent=2), encoding="utf-8")

    print(f"\nresults: {out_path}")
    _report(results)
    return 0


def _summarise(results: list[ArmResult]) -> dict:
    return {
        "runs": [asdict(r) for r in results],
        "aggregate": _aggregate(results),
        "config": {
            "ladder": LADDER,
            "baseline": BASELINE_MODEL,
            "gate_model": GATE_MODEL,
            "costing_note": (
                "priced_cost_usd is a LOWER BOUND wherever unpriced_calls > 0. "
                "bench_rates.json records no rate for any Azure model."
            ),
        },
    }


def _aggregate(results: list[ArmResult]) -> dict:
    out: dict = {}
    for arm in sorted({r.arm for r in results}):
        rs = [r for r in results if r.arm == arm]
        escalated = [r for r in rs if r.escalated]
        out[arm] = {
            "runs": len(rs),
            "correct": sum(1 for r in rs if r.correct),
            "pass_rate": round(sum(1 for r in rs if r.correct) / len(rs), 3),
            "wall_s_median": round(statistics.median(r.wall_s for r in rs), 2),
            "calls_median": statistics.median(len(r.calls) for r in rs),
            "escalation_rate": round(len(escalated) / len(rs), 3),
            "priced_cost_usd_total": round(sum(r.priced_cost_usd or 0.0 for r in rs), 6),
            "unpriced_calls_total": sum(r.unpriced_calls for r in rs),
        }
    return out


def _report(results: list[ArmResult]) -> None:
    agg = _aggregate(results)
    print("\n=== aggregate ===")
    for arm, a in agg.items():
        print(
            f"  {arm}: pass_rate={a['pass_rate']} ({a['correct']}/{a['runs']}) "
            f"wall_median={a['wall_s_median']}s calls_median={a['calls_median']} "
            f"escalation_rate={a['escalation_rate']}"
        )
        bound = ">=" if a["unpriced_calls_total"] else "="
        print(
            f"      cost {bound} ${a['priced_cost_usd_total']}"
            + (f"  ({a['unpriced_calls_total']} call(s) have no recorded rate)" if a["unpriced_calls_total"] else "")
        )


if __name__ == "__main__":
    raise SystemExit(main())
