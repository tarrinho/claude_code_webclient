#!/usr/bin/env python3
"""Deterministic delegation score — no LLM calls needed.

Uses heuristics on the existing bench data to score each response on 6 axes.
Produces per-model aggregated scores matching the old Delegation Score weighting.

Weighting (old spec):
  Correctness 25 + Completeness 15 + Reasoning 15 + Code Quality 10
  + Constraint Handling 10 + Consistency 9 + Clarity 8 + Speed 8 = 100

Each axis score: 1-5. Normalized to 0-100 scale per response.
"""

import json
import re
import sys
from pathlib import Path


def load_responses(bench_dir: Path):
    repo_root = bench_dir.parent
    items = []
    for fpath in sorted(repo_root.glob("bench_*_2026*.json")):
        with open(fpath) as f:
            d = json.load(f)
        runs = d.get("runs", d.get("results", []))
        if isinstance(runs, dict):
            continue
        for r in runs:
            resp = r.get("response", "")
            if not isinstance(resp, str) or len(resp.strip()) < 10:
                continue
            items.append({
                "model": r.get("model", "?"),
                "task": r.get("task", "?"),
                "task_type": r.get("task_type", "?"),
                "correct": r.get("correct", False),
                "score": r.get("score", 0.0),
                "total_s": r.get("total_s", 0.0),
                "output_tokens": r.get("output_tokens", 0),
                "response": resp,
                "check_failures": r.get("check_failures", []),
                "file": fpath.name,
            })
    return items


# ── Scoring heuristics ──────────────────────────────────────────

def score_completeness(item):
    if item["correct"]:
        return 5  # fully correct by harness
    failures = item.get("check_failures", [])
    resp = item["response"]
    if not resp or len(resp) < 30:
        return 1
    if "def " in resp or "import " in resp:
        if len(failures) <= 3:
            return 3
        return 2
    if len(failures) <= 2:
        return 2
    return 1


def score_reasoning(item):
    resp = item["response"]
    if not isinstance(resp, str):
        return 1

    has_docstring = '"""' in resp or "'''" in resp
    has_typing = "->" in resp and ":" in resp and "def " in resp
    # Only has_typing and has_docstring feed the score below. Two further
    # signals (inline comments, and PEP-585 generics) used to be computed
    # here and never used, which read as though the rubric weighed them.
    # Adding them would change every historical benchmark number, so they
    # are gone rather than wired in.

    if item["correct"] and has_typing and has_docstring:
        return 5
    if item["correct"] and has_docstring:
        return 4
    if item["correct"]:
        return 3
    if "def " in resp and has_docstring:
        return 3
    if "def " in resp:
        return 2
    return 1


def score_code_quality(item):
    resp = item["response"]
    if not isinstance(resp, str):
        return 1

    if item["correct"]:
        has_typing = "->" in resp or "list[" in resp or "TypeVar" in resp
        has_docstring = '"""' in resp
        if has_typing and has_docstring:
            return 5
        if has_typing or has_docstring:
            return 4
        return 3

    has_typing = "->" in resp or "TypeVar" in resp or "list[" in resp
    has_docstring = '"""' in resp
    if has_typing and has_docstring and "def " in resp:
        return 3
    if "def " in resp and (has_typing or has_docstring):
        return 2
    if "def " in resp:
        return 2
    if len(resp) > 100:
        return 2
    return 1


def score_constraint_handling(item):
    failures = item.get("check_failures", [])
    if item["correct"]:
        return 5
    if not failures:
        return 2
    num_checks = len(failures)
    if num_checks <= 1:
        return 3
    if num_checks <= 3:
        return 2
    return 1


def score_consistency(item):
    resp = item["response"]
    if not isinstance(resp, str):
        return 3
    if "def " in resp and "return " in resp:
        returns = re.findall(r"return\s+(\w+)", resp)
        if returns and len(set(returns)) > 2:
            return 2
    if item["correct"]:
        return 5
    if "def " in resp:
        return 2
    return 1


def score_clarity(item):
    resp = item["response"]
    if not isinstance(resp, str):
        return 2
    if not item["correct"] and len(resp) < 50:
        return 1

    lines = resp.split("\n")
    has_explanation = any(
        len(l) > 20
        and not l.strip().startswith("def ")
        and not l.strip().startswith("import ")
        and not l.strip().startswith("```")
        and not l.strip().startswith("from ")
        and not l.strip().startswith("    ")
        for l in lines
        if l.strip()
    )

    has_docstring = '"""' in resp or "'''" in resp
    has_comments = resp.count("#") > 0

    if item["correct"] and has_explanation and has_docstring:
        return 5
    if item["correct"] and has_docstring:
        return 4
    if item["correct"]:
        return 3
    if has_explanation or has_comments:
        return 2
    return 1


def compute_delegation_score(item):
    axis = {
        "completeness": item["completeness"],
        "reasoning": item["reasoning"],
        "code_quality": item["code_quality"],
        "constraint_handling": item["constraint_handling"],
        "consistency": item["consistency"],
        "clarity": item["clarity"],
    }

    ts = item.get("total_s", 0)
    if ts <= 0:
        speed = 3
    elif ts < 5:
        speed = 5
    elif ts > 120:
        speed = 1
    else:
        speed = round(5 - 4 * ((ts - 5) / 115), 1)

    weights = {
        "completeness": 15,
        "reasoning": 15,
        "code_quality": 10,
        "constraint_handling": 10,
        "consistency": 9,
        "clarity": 8,
    }

    correctness_score = 5 if item["correct"] else 0
    max_raw = 100  # sum of all weights

    weighted_raw = (
        sum(axis[k] / 5 * weights[k] for k in weights)
        + correctness_score / 5 * 25
        + speed / 5 * 8
    )
    normalized = round(weighted_raw / max_raw * 100, 1)

    return {
        "axis": axis,
        "speed": speed,
        "correctness": correctness_score,
        "delegation_score": normalized,
    }


def main():
    items = load_responses(Path(__file__).parent)
    print(f"Loaded {len(items)} responses", file=sys.stderr)

    for item in items:
        item["completeness"] = score_completeness(item)
        item["reasoning"] = score_reasoning(item)
        item["code_quality"] = score_code_quality(item)
        item["constraint_handling"] = score_constraint_handling(item)
        item["consistency"] = score_consistency(item)
        item["clarity"] = score_clarity(item)

    results = []
    for item in items:
        d = compute_delegation_score(item)
        row = {
            "model": item["model"],
            "task": item["task"],
            "task_type": item["task_type"],
            "correct": item["correct"],
            "total_s": item["total_s"],
            "output_tokens": item["output_tokens"],
            "exec_score": item["score"],
            "file": item["file"],
            "axes": d["axis"],
            **d,
        }
        results.append(row)

    out_file = Path(__file__).parent / "judge_delegation_deterministic_20260904.json"
    with open(out_file, "w") as f:
        json.dump({
            "method": "deterministic",
            "total_responses": len(results),
            "results": results,
        }, f, indent=2)

    print(f"Results written to {out_file}", file=sys.stderr)

    # Per-model aggregation
    by_model = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)

    sep = "=" * 95
    print(sep, file=sys.stderr)
    hdr = (
        f"{'Model':<40} {'N':>3} {'COR':>4} {'Deleg.':>6} "
        f"{'Comp':>5} {'Reas':>5} {'Code':>5} {'Cnstr':>5} {'Con':>5} {'Clar':>5} {'Spd':>5}"
    )
    print(hdr, file=sys.stderr)
    print("-" * len(hdr), file=sys.stderr)

    for model in sorted(by_model):
        mr = by_model[model]
        n = len(mr)
        cc = sum(1 for r in mr if r["correct"])
        avg_d = sum(r["delegation_score"] for r in mr) / n
        avg_a = {
            k: sum(r["axes"][k] for r in mr) / n
            for k in ("completeness", "reasoning", "code_quality",
                      "constraint_handling", "consistency", "clarity")
        }
        avg_s = sum(r["speed"] for r in mr) / n

        print(
            f"{model:<40} {n:>3} {cc:>4} {avg_d:>6.1f} "
            f"{avg_a['completeness']:>5.1f} {avg_a['reasoning']:>5.1f} "
            f"{avg_a['code_quality']:>5.1f} {avg_a['constraint_handling']:>5.1f} "
            f"{avg_a['consistency']:>5.1f} {avg_a['clarity']:>5.1f} "
            f"{avg_s:>5.1f}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
