#!/usr/bin/env python3
"""Build the comparison tables from result files, so the document is generated.

    bin/wc-bench-report.py bench_*.json

The comparison document was written by hand and it showed: numbers were
transcribed from one run into prose, corrected in one table and not another,
and a withdrawn verdict survived four sections below the withdrawal. Every
figure this prints comes from a result file and names the file it came from.

Merges several runs on the assumption that a (model, transport, task, attempt)
key appears once. Where it appears twice the later file wins and the collision
is reported, because silently preferring one measurement over another is how a
stale number outlives its correction.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import cost as cost_mod
from bench import tasks as tasks_mod

TIERS = {
    "claude-": "Anthropic",
    "azure_ai/": "Azure",
    "vllm/": "self-hosted",
}


def tier(model: str) -> str:
    for prefix, name in TIERS.items():
        if model.startswith(prefix):
            return name
    return "other"


def is_void(run: dict) -> bool:
    """A run that produced nothing and reported no reason.

    The signature of the gateway's Azure streaming crash before it was
    surfaced: no error, no stop_reason, no output tokens, empty response. There
    are 16 such runs in the killed full-run file, all Azure over http, and they
    are not measurements of anything -- averaging them in would put five
    working models at 0/16. Dropped and counted, never silently.
    """
    return (not run.get("error")
            and run.get("stop_reason") is None
            and not run.get("output_tokens")
            and not (run.get("response") or "").strip())


def load(paths: list[Path]) -> tuple[list[dict], list[str], list[str]]:
    runs: dict[tuple, dict] = {}
    collisions, voids = [], []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping {path.name}: {exc}", file=sys.stderr)
            continue
        for run in data.get("runs", []):
            key = (run["model"], run["transport"], run["task"], run.get("attempt", 1))
            if is_void(run):
                voids.append(f"{run['model']}/{run['transport']}/{run['task']}"
                             f" ({path.name})")
                continue
            if key in runs:
                collisions.append(f"{'/'.join(map(str, key))}: "
                                  f"{runs[key]['_source']} -> {path.name}")
            run["_source"] = path.name
            runs[key] = run
    return list(runs.values()), collisions, voids


def main() -> int:
    paths = [Path(a) for a in sys.argv[1:]]
    if not paths:
        print(__doc__)
        return 1
    runs, collisions, voids = load(paths)
    if not runs:
        print("no runs found", file=sys.stderr)
        return 1

    print("# Sources\n")
    by_source: dict[str, int] = defaultdict(int)
    for run in runs:
        by_source[run["_source"]] += 1
    for name, n in sorted(by_source.items()):
        print(f"* `{name}` — {n} runs")
    if voids:
        print(f"\n**{len(voids)} void runs dropped** — no output, no error, no "
              f"stop_reason. The signature of the gateway's Azure streaming "
              f"crash before it was surfaced; not measurements of anything:")
        by_void: dict[str, int] = defaultdict(int)
        for v in voids:
            by_void[v.rsplit("/", 1)[0]] += 1
        for k, n in sorted(by_void.items()):
            print(f"  * {k} — {n}")
    if collisions:
        print(f"\n**{len(collisions)} duplicate keys**, later file preferred:")
        for c in collisions[:10]:
            print(f"  * {c}")

    legs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run in runs:
        legs[(run["model"], run["transport"])].append(run)

    rates = cost_mod.load_rates()

    print("\n# Per model and transport\n")
    print("| Tier | Model | Transport | Runs | Correct | Median s | "
          "Median TTFT | Median out tok | Truncated | Errors | $ total | $/correct |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for (model, transport), group in sorted(
            legs.items(), key=lambda kv: (tier(kv[0][0]), kv[0][0], kv[0][1])):
        times = [r["total_s"] for r in group if not r["error"]]
        ttfts = [r["ttft_s"] for r in group if r.get("ttft_s") is not None]
        outs = [r["output_tokens"] for r in group]
        spend = cost_mod.summarise(model, group, rates)
        ok = sum(1 for r in group if r["correct"])
        dollars = f"${spend.dollars:.4f}" if spend.dollars is not None else "n/r"
        per = f"${spend.per_correct:.4f}" if spend.per_correct is not None else "n/r"
        ttft = f"{statistics.median(ttfts):.2f}" if ttfts else "—"
        secs = f"{statistics.median(times):.1f}" if times else "—"
        print(f"| {tier(model)} | `{model}` | {transport} | {len(group)} "
              f"| {ok}/{len(group)} | {secs} | {ttft} "
              f"| {statistics.median(outs):.0f} "
              f"| {sum(1 for r in group if r['hit_cap'])} "
              f"| {sum(1 for r in group if r['error'])} | {dollars} | {per} |")

    print("\n# Per task, score medians\n")
    models = sorted({r["model"] for r in runs}, key=lambda m: (tier(m), m))
    task_order = [t.id for t in tasks_mod.TASKS]
    header = "| Task | Difficulty | Verified by | " + " | ".join(
        m.replace("claude-", "").replace("azure_ai/", "").replace("vllm/", "")
        for m in models) + " |"
    print(header)
    print("|---" * (len(models) + 3) + "|")
    for task_id in task_order:
        task = tasks_mod.BY_ID.get(task_id)
        cells = []
        for model in models:
            scores = [r["score"] for r in runs
                      if r["task"] == task_id and r["model"] == model]
            cells.append(f"{statistics.median(scores):.0f}" if scores else "—")
        kind = task.verifier("").kind if task else "?"
        print(f"| `{task_id}` | {task.difficulty if task else '?'} | {kind} | "
              + " | ".join(cells) + " |")

    print("\n# Instability: same prompt, different score\n")
    for (model, transport), group in sorted(legs.items()):
        by_task: dict[str, list[float]] = defaultdict(list)
        for run in group:
            by_task[run["task"]].append(run["score"])
        unstable = {t: (min(s), max(s)) for t, s in by_task.items()
                    if len(s) > 1 and min(s) != max(s)}
        total = sum(1 for s in by_task.values() if len(s) > 1)
        if total:
            detail = ", ".join(f"{t} {lo:.0f}/{hi:.0f}"
                               for t, (lo, hi) in sorted(unstable.items()))
            print(f"* `{model}` {transport}: **{len(unstable)} of {total}** "
                  + (f"— {detail}" if detail else "— none"))

    print("\n# Thinking volume, the mechanism behind the transport gap\n")
    print("| Model | Transport | Median thinking chars | Max |")
    print("|---|---|---|---|")
    for (model, transport), group in sorted(legs.items()):
        th = [r.get("thinking_chars", 0) for r in group]
        if any(th):
            print(f"| `{model}` | {transport} | {statistics.median(th):.0f} | "
                  f"{max(th)} |")

    print("\n# Answers delivered as files rather than inline\n")
    n = sum(1 for r in runs if r.get("files_written"))
    print(f"{n} of {len(runs)} runs. Only possible on the CLI path, which has "
          f"file tools; the http path must answer inline.")

    print("\n# Streaming fallbacks\n")
    fb = [r for r in runs
          if any("streaming failed" in d for d in r.get("detail", []))]
    print(f"{len(fb)} of {len(runs)} runs could not stream and were re-run "
          f"unstreamed.")
    by_model: dict[str, int] = defaultdict(int)
    for r in fb:
        by_model[r["model"]] += 1
    for model, count in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"* `{model}` — {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
