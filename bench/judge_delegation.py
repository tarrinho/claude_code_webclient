"""LLM-judge delegation score: grade all stored responses on 6 subjective axes.

No additional model queries — only reads bench_*_*.json response files.
Uses claude-haiku-4-5 as judge. Outputs per-response axis scores and
per-model aggregated Delegation Scores (weighted exactly as the old spec).

Old weighting (verbatim from bench/cost.py docstring):
  Correctness 25 + Completeness 15 + Reasoning 15 + Code Quality 10
  + Constraint Handling 10 + Consistency 9 + Clarity 8 + Speed 8 = 100
  Cost weighted 0 (known fatal flaw).

We keep Correctness from the harness (exec-verified) and grade the
remaining 7 axes.  Speed is measured from total_s in the result.

Cost per correct answer stays as its own number (not folded in).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

# ── configuration ──────────────────────────────────────────────

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
JUDGE_MODEL = "vllm/Qwen3.6-35B-A3B-NVFP4"
MAX_RETRIES = 2
RETRY_DELAY = 3

# Output
OUT_FILE = Path(__file__).parent / "judge_delegation_20260904.json"

# ── response loader ────────────────────────────────────────────

def load_responses(bench_dir: Path = Path(__file__).parent):
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
            if not isinstance(resp, str) or len(resp.strip()) < 20:
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
                "file": fpath.name,
            })
    return items


TASK_DESC = {
    "floor-add": "def add(a,b): return a+b",
    "simple-fizzbuzz": "fizzbuzz(n): list 1..n, 3->Fizz, 5->Buzz, 15->FizzBuzz",
    "simple-count-vowels": "count_vowels(s): count aeiou case-insensitive",
    "simple-reverse-words": "reverse_words(s): reverse word order",
    "simple-sum-evens": "sum_evens(lst): sum of even ints",
    "simple-json-field": "extract_field(json_str, field): list of field values from JSON array",
    "coding-bug-fix": "Fix a buggy function to pass tests",
    "coding-algo": "Implement an algorithm with constraints",
    "reasoning-puzzle": "Logic puzzle requiring multi-step reasoning",
    "reasoning-math": "Math problem requiring derivation",
    "comprehension-read": "Read document and answer questions",
    "planning-task": "Plan multi-step workflow with constraints",
    "long-context-needle": "Find info in long text",
    "multi-turn-resume": "Multi-turn: remember context, resume after interruption",
}


# ── judge ──────────────────────────────────────────────────────

JUDGE_SYSTEM = (
    "You are an impartial code-quality judge. Score each response on 6 axes (1-5)."
    " 1=broken/wrong, 3=adequate, 5=excellent."
    " Output ONLY a JSON object with keys: completeness, reasoning, code_quality,"
    " constraint_handling, consistency, clarity."
    " No other text. No markdown."
)

PROMPT_TEMPLATE = (
    "Task: {task} ({task_type})\n"
    "Correct by harness: {correct}\n"
    "Response:\n{response}\n"
    "Score this response on completeness, reasoning, code_quality,"
    " constraint_handling, consistency, clarity (1-5 each). Output JSON only."
)


def _extract_json(text: str):
    """Find and parse a JSON object/array from the model output."""
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first { ... } or [ ... ]
    for start, end, ch in [(0, '{', '}'), (0, '[', ']')]:
        i = text.find(ch, start)
        if i >= 0:
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                pass
        # Fallback: find matching close
        depth = 0
        i = text.find(ch)
        if i >= 0:
            for j in range(i, len(text)):
                if text[j] == ch:
                    depth += 1
                elif text[j] == (']' if ch == '[' else '}'):
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j+1])
                        except json.JSONDecodeError:
                            pass
                        break
    # Last resort: strip quotes and parse
    for ch in ('{', '['):
        i = text.find(ch)
        if i >= 0:
            cleaned = re.sub(r'(?<!"):("[^"]+)":(?!=)', r'\1:', text[i:])
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
    return None


def call_proxy(prompt_text: str, max_tokens: int = 2048) -> str:
    """Call the local LiteLLM proxy (OpenAI-compatible endpoint)."""
    headers = {
        "x-api-key": API_KEY,
        "content-type": "application/json",
    }
    sys_msg = {"role": "system", "content": JUDGE_SYSTEM} if JUDGE_SYSTEM else None
    payload = {
        "model": JUDGE_MODEL,
        "max_tokens": max_tokens,
        "messages": [sys_msg, {"role": "user", "content": prompt_text}]
        if sys_msg else [{"role": "user", "content": prompt_text}],
        "stop": ["\n\n", "}\n", "```"],
    }
    resp = httpx.post(f"{BASE_URL}/v1/messages", headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise Exception(f"API {resp.status_code}: {resp.text[:500]}")
    body = resp.json()

    content_parts = body.get("content", [])
    if isinstance(content_parts, list) and content_parts:
        for part in content_parts:
            if isinstance(part, dict):
                # Qwen3 on this proxy puts everything in "thinking"
                if "thinking" in part and isinstance(part["thinking"], str):
                    return part["thinking"]
                if part.get("type") == "text":
                    return part["text"]
                if "content" in part and isinstance(part["content"], str):
                    return part["content"]
        # Fallback: join all text-like fields
        texts = []
        for part in content_parts:
            if isinstance(part, dict):
                for v in (part.get("text"), part.get("content"), part.get("thinking")):
                    if isinstance(v, str):
                        texts.append(v)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts) if texts else ""
    return ""


def call_proxy_safe(prompt_text: str, max_calls: int = 3) -> str:
    """Retry call_proxy until we get a non-trivial response (proxy warmup guard)."""
    for i in range(max_calls):
        text = call_proxy(prompt_text)
        if len(text) > 50 and "Here's a thinking process:" != text.strip():
            return text
        time.sleep(2)
    # Last resort: just return whatever we got
    return call_proxy(prompt_text)


def grade_one(item: dict):
    """Grade a single response. Returns axis scores dict."""
    prompt = PROMPT_TEMPLATE.format(
        task=item["task"],
        task_type=item["task_type"],
        correct=item["correct"],
        response=item["response"][:1500],
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            text = call_proxy_safe(prompt)
            result = _extract_json(text)
            if result and all(k in result for k in ("completeness","reasoning","code_quality",
                                                       "constraint_handling","consistency","clarity")):
                return result
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    # Fallback: zeros
    print(f"  Fallback to zeros for {item['model']}/{item['task']}: {e if 'e' in dir() else 'parse failed'}", file=sys.stderr)
    return {"completeness":0,"reasoning":0,"code_quality":0,
            "constraint_handling":0,"consistency":0,"clarity":0}


# ── aggregation ─────────────────────────────────────────────────

OLD_WEIGHTS = {
    "completeness": 15,
    "reasoning": 15,
    "code_quality": 10,
    "constraint_handling": 10,
    "consistency": 9,
    "clarity": 8,
}


def compute_delegation_score(item, judge_scores):
    axis_scores = {k: judge_scores.get(k, 0) for k in OLD_WEIGHTS}
    axis_scores["correctness"] = 5 if item["correct"] else 0

    ts = item.get("total_s", 0)
    if ts <= 0:
        speed = 3
    elif ts < 5:
        speed = 5
    elif ts > 120:
        speed = 1
    else:
        speed = round(5 - 4 * ((ts - 5) / 115), 1)

    scores = {**axis_scores, "speed": speed}
    weighted = sum(scores[k] * OLD_WEIGHTS[k] for k in OLD_WEIGHTS) + scores["correctness"] * 25 + speed * 8
    return {
        "axis": axis_scores,
        "speed": speed,
        "correctness": scores["correctness"],
        "delegation_score": round(weighted, 1),
    }


# ── main ───────────────────────────────────────────────────────

def main():
    items = load_responses()
    print(f"Loaded {len(items)} responses with content (>20 chars)", file=sys.stderr)

    by_model = {}
    for item in items:
        by_model.setdefault(item["model"], []).append(item)

    results = []
    total_calls = 0
    parsed = 0
    fallback = 0

    for model in sorted(by_model):
        model_items = by_model[model]
        print(f"\nGrading {model}: {len(model_items)} responses", file=sys.stderr)

        for i, item in enumerate(model_items):
            print(f"  [{i+1}/{len(model_items)}] {item['task']}", file=sys.stderr, end=" ", flush=True)
            try:
                scores = grade_one(item)
                total_calls += 1
                if any(v > 0 for v in scores.values()):
                    parsed += 1
                else:
                    fallback += 1
            except Exception as e:
                print(f"ERR {e}", file=sys.stderr, flush=True)
                scores = {k: 0 for k in OLD_WEIGHTS}
                fallback += 1

            delegated = compute_delegation_score(item, scores)
            results.append({
                "model": item["model"],
                "task": item["task"],
                "task_type": item["task_type"],
                "correct": item["correct"],
                "total_s": item["total_s"],
                "output_tokens": item["output_tokens"],
                "exec_score": item["score"],
                "file": item["file"],
                "axes": scores,
                **delegated,
            })

    # Write output
    out = {
        "judge_model": JUDGE_MODEL,
        "total_responses": len(results),
        "total_api_calls": total_calls,
        "parsed": parsed,
        "fallback": fallback,
        "results": results,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n\nResults written to {OUT_FILE}", file=sys.stderr)

    # Per-model aggregation
    print("\n" + "=" * 100, file=sys.stderr)
    header = (f"{'Model':<40} {'N':>3} {'COR':>4} {'Deleg.':>7} "
              f"{'Comp':>5} {'Reas':>5} {'Code':>5} {'Cnstr':>5} {'Con':>5} {'Clar':>5} {'Spd':>5}")
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)

    for model in sorted(by_model):
        mr = [r for r in results if r["model"] == model]
        avg_d = sum(r["delegation_score"] for r in mr) / len(mr)
        avg_a = {k: sum(r["axis"].get(k, 0) for r in mr) / len(mr) for k in OLD_WEIGHTS}
        avg_s = sum(r["speed"] for r in mr) / len(mr)
        cc = sum(1 for r in mr if r["correct"])
        print(
            f"{model:<40} {len(mr):>3} {cc:>4} {avg_d:>7.1f} "
            f"{avg_a.get('completeness',0):>5.1f} {avg_a.get('reasoning',0):>5.1f} "
            f"{avg_a.get('code_quality',0):>5.1f} {avg_a.get('constraint_handling',0):>5.1f} "
            f"{avg_a.get('consistency',0):>5.1f} {avg_a.get('clarity',0):>5.1f} "
            f"{avg_s:>5.1f}", file=sys.stderr
        )

    print(f"\nTotal calls: {total_calls}, Parsed: {parsed}, Fallback(0): {fallback}", file=sys.stderr)


if __name__ == "__main__":
    main()
