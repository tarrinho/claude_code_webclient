#!/usr/bin/env python3
"""Benchmark models via the WebConsole gateway."""
import os, sys, json, time, urllib.request, urllib.error

BASE_URL = "https://llm.ai-machine.cfappsecurity.com/v1/messages"
# Resolved at run time, never stored here. This was a hardcoded literal, which
# put a live gateway key into a file inside the git working tree -- untracked and
# not gitignored, so one `git add -A` from a public remote. Same order the rest of
# the tooling uses: the environment first, then the active machine in the
# WebConsole database, which is where wc-claude.sh reads it from.
def _api_key() -> str:
    from os import environ
    for var in ("WC_BENCH_API_KEY", "ANTHROPIC_API_KEY"):
        if environ.get(var):
            return environ[var]
    # Read-only: opening this database read-write from a second process is what
    # took the production write path down for 37 minutes (registry #41).
    import sqlite3
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "data" / "webconsole.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT api_key FROM ai_machines WHERE active = 1 "
                "AND api_key IS NOT NULL AND api_key != '' LIMIT 1").fetchone()
        finally:
            con.close()
        if row and row[0]:
            return row[0]
    raise SystemExit(
        "model_benchmark: no API key. Set WC_BENCH_API_KEY, or activate a "
        "machine that has one in the WebConsole database.")


API_KEY = _api_key()
# A reasoning model spends this budget on thinking before it emits any answer.
# At 4096, Qwen3.6 hit the cap on three of six tasks -- output_tokens was exactly
# 4096 each time -- and produced no text block at all, which the extractor below
# recorded as the literal string "[thinking]". The cap was the cause; the
# placeholder was only how it looked.
MAX_TOKENS = 16384
# Raising MAX_TOKENS to 16384 moved Qwen3.6's failure rather than fixing it: the
# three tasks that used to truncate at 4096 now spend longer thinking and hit
# this read timeout instead, so the harness still records nothing. 180s was
# never a considered value -- the baseline run measured Qwen at 123-177s per
# task, which is to say every one of its successes landed inside 3s of the
# limit. Any model slower than the five Azure ones was going to fail here on
# arrival.
#
# So this is a cap on the *harness*, not a property of a model, and it belongs
# in the environment where a slow backend can be measured rather than
# disqualified.
#
# 600s was not enough either. At that ceiling Qwen3.6 completed coding-bug-fix
# in 290.8s (6097 output tokens) and coding-algo in 287.6s (6411), but
# reasoning-puzzle still timed out -- the same task the CLI transport answers
# correctly in 23.0s. A 26x gap between two callers of the same model is a
# property of the path, not the model, so this ceiling exists to stop the
# harness disqualifying a backend before that gap is understood, not to paper
# over it.
TIMEOUT = int(os.environ.get("WC_BENCH_TIMEOUT_S", "1000"))

TASKS = {
    "coding-bug-fix": {
        "description": "Fix: return last n unique elements preserving order",
        "task_type": "coding",
        "prompt": """Fix this Python function. It's supposed to return the last 'n' unique elements from a list, preserving order. It currently has a bug that drops the last element:

```python
def last_n_unique(lst, n):
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    return seen[:n]
```

Also add a docstring and type hints. Keep the function name. Return valid Python code only."""
    },
    "reasoning-puzzle": {
        "description": "Water jug: reach (4,0,4) in minimum pours",
        "task_type": "reasoning",
        "prompt": """You have three containers: A=5L, B=3L, C=8L (full). A and B are empty. Pour between containers until X is empty OR Y is full.

Find the minimum number of pours to reach (4, 0, 4). Show each step as: step N: X→Y → (a, b, c). Only show the steps. Give the final count."""
    },
    "coding-algo": {
        "description": "Implement LRU cache O(1)",
        "task_type": "coding",
        "prompt": """Implement an LRU (Least Recently Used) cache in Python with:
- __init__(capacity: int)
- get(key: int) -> int  # returns -1 if not found
- put(key: int, value: int) -> None
Both operations must be O(1) average case.
Include a __repr__ that shows (key, value) pairs in access order. Return valid Python code only."""
    },
    "reasoning-math": {
        "description": "Birthday paradox: P(at least one match) with 23 people",
        "task_type": "reasoning",
        "prompt": """A room has 23 people. Birthdays uniformly distributed across 365 days (ignore leap years). What is the probability that at least two people share a birthday?

Show the full calculation as a product formula, then give the numeric answer rounded to 4 decimal places. Only the calculation and answer."""
    },
    "comprehension-read": {
        "description": "Explain a Python function and find edge cases",
        "task_type": "comprehension",
        "prompt": """Explain what this function does and answer the questions:

```python
def process(data: list[dict]) -> dict:
    result = {}
    for item in data:
        k = item.get('category', 'unknown')
        if k not in result:
            result[k] = []
        result[k].append(item.get('value', 0))
    return {k: sum(v) / len(v) for k, v in result.items()}
```

1. What does it return?
2. What happens if data contains an empty dict?
3. What happens if a category has zero items?
4. What edge case could cause a ZeroDivisionError?

Number your answers 1-4."""
    },
    "planning-task": {
        "description": "Design dark mode toggle for FastAPI app",
        "task_type": "planning",
        "prompt": """Design a plan to add a 'dark mode toggle' to a FastAPI web application with no existing theme support. Stack: FastAPI + Jinja2 HTML templates, Vanilla JS, SQLite for user preferences.

Provide:
1. Database schema change (SQL)
2. API endpoints needed (URL + method)
3. Frontend components (HTML/CSS/JS) - what each does
4. Default/inheritance behavior
5. One sentence on cross-device persistence

Use numbered lists."""
    },
}

def query_model(model_name, prompt):
    """Send a prompt to a model and return (response_text, metrics_dict)."""
    payload = json.dumps({
        "model": model_name,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(BASE_URL, data=payload, method="POST")
    req.add_header("x-api-key", API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
        elapsed = time.time() - start

        # Text blocks are the answer. Thinking blocks are kept as a *fallback*
        # rather than discarded: a model that spends its whole budget reasoning
        # emits no text block at all, and replacing that with a placeholder threw
        # away the only output there was. Preferring text keeps a clean answer
        # clean, and the fallback means a truncated run still yields something
        # scoreable instead of a ten-character string.
        text_parts, thinking_parts = [], []
        for block in data.get("content", []):
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
        full_text = "\n".join(x for x in text_parts if x).strip()
        if not full_text:
            full_text = "\n".join(x for x in thinking_parts if x).strip()

        usage = data.get("usage", {})
        metrics = {
            "elapsed_s": round(elapsed, 2),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "error": None,
            # Recorded so truncation is legible in the data. Without it a run
            # capped at max_tokens looks like a short answer.
            "stop_reason": data.get("stop_reason"),
            "had_text_block": bool([x for x in text_parts if x]),
        }
        # Check if the model field indicates a different model than requested
        actual_model = data.get("model", model_name)
        metrics["actual_model"] = actual_model

        return full_text, metrics
    except Exception as e:
        elapsed = time.time() - start
        return "", {
            "elapsed_s": round(elapsed, 2),
            "input_tokens": 0,
            "output_tokens": 0,
            "error": str(e),
        }

def main():
    if len(sys.argv) < 2:
        print("Usage: model_benchmark.py [--models m1,m2,... | --all | --task TASK_ID]", file=sys.stderr)
        print("Models: vllm/Qwen3.6-35B-A3B-NVFP4, azure_ai/gpt-5.6-luna, azure_ai/gpt-5.6-sol, azure_ai/gpt-5.4-mini-copilot, azure_ai/gpt-5.4-mini, azure_ai/gpt-5-mini", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]

    if "--models" in args:
        idx = args.index("--models")
        model_list = args[idx+1].split(",") if idx+1 < len(args) else []
    elif "--all" in args:
        model_list = ["vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5.6-luna", "azure_ai/gpt-5.6-sol",
                       "azure_ai/gpt-5.4-mini-copilot", "azure_ai/gpt-5.4-mini", "azure_ai/gpt-5-mini"]
    elif "--task" in args:
        idx = args.index("--task")
        if idx+1 < len(args):
            model_list = ["vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5.6-luna", "azure_ai/gpt-5.6-sol",
                           "azure_ai/gpt-5.4-mini-copilot", "azure_ai/gpt-5.4-mini", "azure_ai/gpt-5-mini"]
        else:
            print("Error: --task requires a task ID", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: use --all, --models m1,m2, or --task TASK_ID", file=sys.stderr)
        sys.exit(1)

    results = {}
    errors = {}

    for model in model_list:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"MODEL: {model}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        model_results = {}
        model_errors = []

        for task_id, task_cfg in TASKS.items():
            print(f"  Running task: {task_cfg['description']}...", file=sys.stderr)
            response, metrics = query_model(model, task_cfg["prompt"])

            if metrics.get("error"):
                model_errors.append({"task": task_id, "error": metrics["error"]})
                print(f"    ERROR: {metrics['error']}", file=sys.stderr)
            else:
                model_results[task_id] = {
                    "response": response,
                    "metrics": metrics,
                    "task_type": task_cfg["task_type"],
                }
                print(f"    OK: {metrics['elapsed_s']}s, {metrics['output_tokens']} output tokens", file=sys.stderr)

        results[model] = model_results
        if model_errors:
            errors[model] = model_errors

        # Save progress after each model
        output = {"results": results, "errors": errors}
        with open("model_benchmark_results.json", "w") as f:
            json.dump(output, f, indent=2)
        print(f"  Saved progress to model_benchmark_results.json", file=sys.stderr)

    # Save final results
    output = {"results": results, "errors": errors, "models_tested": model_list}
    with open("model_benchmark_results.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n\n=== SUMMARY ===", file=sys.stderr)
    for model in model_list:
        r = results.get(model, {})
        e = errors.get(model, [])
        n_tasks = len(r)
        n_errors = len(e)
        print(f"  {model}: {n_tasks} tasks completed, {n_errors} errors", file=sys.stderr)
        if e:
            for err in e:
                print(f"    {err['task']}: {err['error'][:100]}", file=sys.stderr)

    print(f"\nResults saved to model_benchmark_results.json", file=sys.stderr)

if __name__ == "__main__":
    main()
