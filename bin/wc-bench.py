#!/usr/bin/env python3
"""Run the benchmark. Every number it prints names what produced it.

    bin/wc-bench.py --models azure_ai/gpt-5.6-luna --repeats 3
    bin/wc-bench.py --models vllm/Qwen3.6-35B-A3B-NVFP4 --transports http,cli
    bin/wc-bench.py --models m1,m2 --tasks coding-bug-fix,coding-algo
    bin/wc-bench.py --list

What this fixes about its predecessor, all of which cost a working model its
ranking:

* results are written **after every run**, not after a whole model, so a kill
  at task five does not lose the first four
* `--out` defaults to a timestamped file and never overwrites, because the old
  script wrote `model_benchmark_results.json` into the current directory and
  clobbered the only copy of the baseline
* a run that hit its token ceiling is reported as truncated, never scored as
  wrong
* code tasks are scored by executing the code
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import cost as cost_mod
from bench import tasks as tasks_mod
from bench import transports

#: All ten backends this deployment can reach, in one list.
#:
#: The four Anthropic models were missing from the comparison entirely, on the
#: grounds that "Anthropic models are NOT accessible through this gateway" --
#: true, and not the same statement as unreachable. They answer over the CLI
#: path against the host's own `claude` login, verified 2026-09-02. Leaving them
#: out meant the most expensive tier in the stack had no measured quality to
#: justify its price, which is the comparison anyone actually needs.
DEFAULT_MODELS = [
    # self-hosted, free
    "vllm/Qwen3.6-35B-A3B-NVFP4",
    # Azure, paid
    "azure_ai/gpt-5.6-luna",
    "azure_ai/gpt-5.6-sol",
    "azure_ai/gpt-5.4-mini-copilot",
    "azure_ai/gpt-5.4-mini",
    "azure_ai/gpt-5-mini",
    # Anthropic, most expensive -- CLI transport only
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-haiku-4-5",
]

#: Transports tried when `--transports` is not given.
#:
#: Both, not just http. With http alone the four Anthropic models are skipped
#: on every run and the table quietly reverts to six -- the original omission,
#: reintroduced as a default.
DEFAULT_TRANSPORTS = "http,cli"


def run_one(task, model: str, transport: str, key: str) -> dict:
    """One task, one model, one transport, one repeat."""
    messages: list[dict] = [{"role": "user", "content": task.prompt}]
    session = uuid.uuid4()
    if transport == "cli":
        messages[0]["_session_id"] = str(session)

    turn = transports.send(transport, model, messages, key)
    replies = [turn]

    if task.followup and not turn.error:
        # Second turn in the *same* session. Over HTTP that means replaying the
        # exchange; over the CLI it means --resume, which is the path the
        # console actually uses (CLAUDE.md §6) and the reason a real UUID
        # matters -- the hex form has no dashes and --resume rejects it.
        if transport == "cli":
            follow_messages = [{"role": "user", "content": task.followup,
                                "_resume": str(session)}]
        else:
            follow_messages = [
                {"role": "user", "content": task.prompt},
                {"role": "assistant", "content": turn.text or "(no reply)"},
                {"role": "user", "content": task.followup},
            ]
        replies.append(transports.send(transport, model, follow_messages, key))

    answer = replies[-1].text
    verdict = task.verifier(answer)
    last = replies[-1]

    return {
        "task": task.id,
        "task_type": task.task_type,
        "model": model,
        "transport": transport,
        "turns": len(replies),
        # Correct means every *core* check passed and the run was not
        # truncated or errored. Edge checks are reported separately and do not
        # gate: an LRU scoring 26 of 27 for raising on capacity=0 used to sit
        # in the same column as a response containing no code at all, and those
        # are not the same outcome.
        "correct": verdict.solved and not last.hit_cap and not last.error,
        "score": verdict.score,
        "core_score": verdict.core_score,
        "core": f"{verdict.core_passed}/{verdict.core_total}",
        "edge": f"{verdict.edge_passed}/{verdict.edge_total}",
        "verified_by": verdict.kind,
        "checks_passed": verdict.passed,
        "checks_total": verdict.total,
        "check_failures": verdict.detail,
        "no_answer": verdict.no_code,
        "ttft_s": last.ttft_s,
        "total_s": round(sum(r.total_s for r in replies), 2),
        "input_tokens": sum(r.input_tokens for r in replies),
        "cache_read_tokens": sum(r.cache_read_tokens for r in replies),
        "cache_write_tokens": sum(r.cache_write_tokens for r in replies),
        "reported_cost_usd": (
            sum(r.reported_cost_usd for r in replies)
            if all(r.reported_cost_usd is not None for r in replies) else None),
        "cost_basis": next((r.cost_basis for r in replies if r.cost_basis), None),
        "output_tokens": sum(r.output_tokens for r in replies),
        "stop_reason": last.stop_reason,
        "hit_cap": last.hit_cap,
        "cap_headroom": last.cap_headroom,
        "had_text_block": last.had_text_block,
        "thinking_chars": sum(r.thinking_chars for r in replies),
        "files_written": [f for r in replies for f in r.files_written],
        "model_served": last.model_served,
        "error": last.error,
        "detail": [d for r in replies for d in r.detail],
        "response": answer[:4000],
    }


def aggregate(runs: list[dict]) -> dict:
    """Per (model, task, transport): the spread, not a single number.

    Consistency was weighted 9% of the old score and measured zero times,
    because every task ran once. With repeats it becomes a fact: `pass_rate`
    below is how often the same prompt got the same verdict.
    """
    out: dict[str, dict] = {}
    for run in runs:
        key = f"{run['model']}|{run['task']}|{run['transport']}"
        out.setdefault(key, []).append(run)
    summary = {}
    for key, group in out.items():
        times = [r["total_s"] for r in group if not r["error"]]
        ttfts = [r["ttft_s"] for r in group if r["ttft_s"] is not None]
        scores = [r["score"] for r in group]
        summary[key] = {
            "repeats": len(group),
            "pass_rate": round(sum(1 for r in group if r["correct"]) / len(group), 3),
            "score_median": round(statistics.median(scores), 1) if scores else None,
            "score_spread": [min(scores), max(scores)] if scores else None,
            "total_s_median": round(statistics.median(times), 1) if times else None,
            "ttft_s_median": round(statistics.median(ttfts), 2) if ttfts else None,
            "truncated": sum(1 for r in group if r["hit_cap"]),
            "errors": sum(1 for r in group if r["error"]),
            "verified_by": group[0]["verified_by"],
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--tasks", default="", help="default: all")
    parser.add_argument("--difficulty", default="",
                        help="comma-separated: floor,simple,hard. Default all. "
                             "floor-add is a control every model must pass; a "
                             "failure there means a broken invocation")
    parser.add_argument("--transports", default=DEFAULT_TRANSPORTS,
                        help=f"comma-separated (default: {DEFAULT_TRANSPORTS}). "
                             "Anthropic models are reachable over cli only")
    parser.add_argument("--repeats", type=int, default=3,
                        help="runs per task (default: 3); 1 makes Consistency "
                             "unmeasurable, which is how the old score carried "
                             "a 9%% weight for it without ever measuring it")
    parser.add_argument("--out", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for task in tasks_mod.TASKS:
            kind = task.verifier("").kind
            extra = f"  [{', '.join(task.tags)}]" if task.tags else ""
            print(f"  {task.id:24} {task.difficulty:7} {kind:5}  "
                  f"{task.description}{extra}")
        return 0

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    paths = [t.strip() for t in args.transports.split(",") if t.strip()]
    selected = ([tasks_mod.BY_ID[t.strip()] for t in args.tasks.split(",") if t.strip()]
                if args.tasks else list(tasks_mod.TASKS))
    if args.difficulty:
        want = {d.strip() for d in args.difficulty.split(",") if d.strip()}
        selected = [t for t in selected if t.difficulty in want]
        if not selected:
            raise SystemExit(f"bench: no tasks with difficulty in {sorted(want)}")

    out_path = Path(args.out) if args.out else Path(
        f"bench_results_{time.strftime('%Y%m%d-%H%M%S')}.json")
    if out_path.exists():
        # The old script overwrote its results file in the current directory,
        # which nearly destroyed the only copy of the six-model baseline.
        raise SystemExit(f"bench: {out_path} exists; refusing to overwrite it")

    key = transports.gateway_key() if "http" in paths else ""
    runs: list[dict] = []
    total = len(models) * len(paths) * len(selected) * args.repeats
    done = 0

    skipped: list[str] = []
    for model in models:
        for transport in paths:
            if not transports.reachable(model, transport):
                # Skipped with a reason, and *not* recorded as a run. An
                # unreachable path is a fact about the deployment; writing it
                # into the results as a failure is how a model ends up scored
                # for something the harness could not do.
                reason = transports.why_unreachable(model, transport)
                skipped.append(reason)
                print(f"  skip {model} over {transport}: {reason}",
                      file=sys.stderr, flush=True)
                total -= len(selected) * args.repeats
                continue
            for task in selected:
                for attempt in range(1, args.repeats + 1):
                    done += 1
                    print(f"[{done}/{total}] {model} {transport} {task.id} "
                          f"#{attempt}", file=sys.stderr, flush=True)
                    run = run_one(task, model, transport, key)
                    run["attempt"] = attempt
                    runs.append(run)
                    flag = ("ERROR" if run["error"] else
                            "TRUNCATED" if run["hit_cap"] else
                            "ok" if run["correct"] else "partial")
                    print(f"      {flag} score={run['score']} "
                          f"core={run['core']} edge={run['edge']} "
                          f"({run['verified_by']}) {run['total_s']}s "
                          f"out={run['output_tokens']}",
                          file=sys.stderr, flush=True)
                    for line in run["check_failures"][:3]:
                        print(f"        - {line}", file=sys.stderr, flush=True)
                    # After every run, not every model: a kill at task five
                    # used to lose the first four.
                    _write(out_path, runs, models, paths, args.repeats)

    rates = cost_mod.load_rates()
    # Built field by field rather than with vars(): `per_correct` and `note`
    # are properties, so vars() silently omits the two values the cost table is
    # for, and the printer below would raise KeyError on the first row.
    spend = {}
    for model in models:
        s = cost_mod.summarise(model, [r for r in runs if r["model"] == model], rates)
        spend[model] = {
            "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
            "cache_read_tokens": s.cache_read_tokens,
            "cache_write_tokens": s.cache_write_tokens,
            "runs": s.runs, "correct": s.correct, "known": s.known,
            "dollars": s.dollars, "per_correct": s.per_correct, "note": s.note,
            "source": s.source, "basis": s.basis, "detail": s.detail,
        }
    _write(out_path, runs, models, paths, args.repeats, spend, skipped)

    print("\n=== summary ===", file=sys.stderr)
    for key_, agg in aggregate(runs).items():
        print(f"  {key_}: pass_rate={agg['pass_rate']} "
              f"score={agg['score_median']} spread={agg['score_spread']} "
              f"median={agg['total_s_median']}s ttft={agg['ttft_s_median']}s "
              f"truncated={agg['truncated']} errors={agg['errors']} "
              f"[{agg['verified_by']}]", file=sys.stderr)
    print("\n=== cost ===", file=sys.stderr)
    if not rates:
        print("  no rates recorded; set bench_rates.json or WC_BENCH_RATES. "
              "Cost is reported as unknown rather than as zero.", file=sys.stderr)
    for model, s in spend.items():
        src = f" [{s['source']}"+(f", basis={s['basis']}" if s['basis'] else "")+"]" \
              if s['source'] else ""
        print(f"  {model}: {s['correct']}/{s['runs']} correct, "
              f"dollars={s['dollars']} per_correct={s['per_correct']}"
              f"{src} {s['note']}".rstrip(), file=sys.stderr)
    if skipped:
        print("\n=== skipped (unreachable, not scored) ===", file=sys.stderr)
        for reason in skipped:
            print(f"  {reason}", file=sys.stderr)
    print(f"\nresults: {out_path}", file=sys.stderr)
    return 0


def _write(path: Path, runs: list[dict], models, paths, repeats,
           spend=None, skipped=None) -> None:
    payload = {
        "runs": runs,
        "aggregate": aggregate(runs),
        "config": {
            "models": models, "transports": paths, "repeats": repeats,
            "max_tokens": transports.MAX_TOKENS,
            "timeout_s": transports.TIMEOUT_S,
            "gateway": transports.GATEWAY_URL,
        },
        # Kept in the artefact, not just printed. A reader of the JSON has to
        # be able to tell "this model was not measured on this path" from
        # "this model scored nothing on this path".
        "skipped": skipped or [],
    }
    if spend is not None:
        payload["spend"] = spend
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
