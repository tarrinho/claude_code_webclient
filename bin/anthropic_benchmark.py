#!/usr/bin/env python3
"""Benchmark all Anthropic Claude models via the WebConsole gateway."""
import datetime
import json
import os
import subprocess
import sys
import time

WC_CLAUDE = "/home/kali/projects/claude-code-webconsole/bin/wc-claude.sh"
DB = "/home/kali/projects/claude-code-webconsole/data/webconsole.db"
OUTPUT = "/home/kali/projects/claude-code-webconsole/anthropic_benchmark_results.json"

def restore_gateway():
    import sqlite3
    conn = sqlite3.connect(f"file:{DB}?mode=rw", uri=True)
    cur = conn.cursor()
    cur.execute('UPDATE ai_machines SET active = 0 WHERE name = "Anthropic API"')
    cur.execute('UPDATE ai_machines SET active = 1 WHERE name = "Current AI Machine"')
    cur.execute("UPDATE settings SET value='vllm/Qwen3.6-35B-A3B-NVFP4', updated_at=? WHERE key='default_model'",
                (datetime.datetime.now().isoformat(),))
    conn.commit()
    conn.close()
    print("Gateway config restored", file=sys.stderr)

def run_claude(model, prompt):
    """Run a single model query. Returns (text, elapsed, error)."""
    env = os.environ.copy()
    env["PATH"] = "/home/kali/.local/bin:" + env.get("PATH", "")

    cmd = [
        WC_CLAUDE,
        "--model", model,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        prompt
    ]

    start = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
            stdin=subprocess.DEVNULL,
        )
        elapsed = time.time() - start
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # Extract response from result block at end of stream
        result_text = ""
        result_block = None
        for line in stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                obj_type = obj.get("type", "")
                if obj_type == "result":
                    result_block = obj
                    break
            except:
                continue

        if result_block and "result" in result_block:
            result_text = result_block["result"]
        elif result_block and "response" in result_block:
            result_text = result_block.get("response", "")
        else:
            # Fallback: check text messages
            for line in stdout.split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant" and "message" in obj:
                        msg = obj["message"]
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                result_text += block.get("text", "")
                                break
                except:
                    continue

        error = None
        if proc.returncode != 0:
            error = stderr[-500:] if stderr else "non-zero exit"
            result_text = stderr if stderr else ""
        elif not result_text and "Error" in stderr:
            error = stderr[-200:]

        return result_text, elapsed, error
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return "", elapsed, "timeout after 300s"
    except Exception as e:
        elapsed = time.time() - start
        return "", elapsed, str(e)

TASKS = {
    "coding-bug-fix": (
        "coding",
        "Fix this Python function. It returns the last n unique elements from a list, "
        "preserving order. It has a bug that drops the last element: "
        "def last_n_unique(lst, n):\n    seen = []\n    for x in reversed(lst):\n        "
        "if x not in seen:\n            seen.append(x)\n    return seen[:n] "
        "Add a docstring and type hints. Keep the function name. Return valid Python code only."
    ),
    "reasoning-puzzle": (
        "reasoning",
        "Three containers: A=5L, B=3L, C=8L (full). A and B empty. Pour between until "
        "X empty or Y full. Find minimum pours to reach (4,0,4). Show each step as: step N: X→Y → (a,b,c). Give final count."
    ),
    "coding-algo": (
        "coding",
        "Implement LRU cache in Python: __init__(capacity), get(key)->int (returns -1 if not found), "
        "put(key, value). Both O(1). Include __repr__ showing (key, value) in access order. Return valid Python code only."
    ),
    "reasoning-math": (
        "reasoning",
        "23 people, birthdays uniform across 365 days. P(at least two share birthday)? "
        "Show full calculation as product formula, then numeric answer rounded to 4 decimals. Only calculation and answer."
    ),
    "comprehension-read": (
        "comprehension",
        "Explain and answer questions about this function:\n"
        "def process(data: list[dict]) -> dict:\n"
        "    result = {}\n"
        "    for item in data:\n"
        "        k = item.get('category', 'unknown')\n"
        "        if k not in result: result[k] = []\n"
        "        result[k].append(item.get('value', 0))\n"
        "    return {k: sum(v)/len(v) for k, v in result.items()}\n\n"
        "1. What does it return? 2. Empty dict in data? 3. Zero items per category? "
        "4. What edge case causes ZeroDivisionError? Number answers 1-4."
    ),
    "planning-task": (
        "planning",
        "Design dark mode toggle for FastAPI+Jinja2+VanillaJS+SQLite. Provide: "
        "1) SQL schema change, 2) API endpoints (URL+method), 3) Frontend components, "
        "4) Default/inheritance behavior, 5) One sentence on cross-device persistence. Numbered lists."
    ),
}

MODELS = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-haiku-4-5",
]

def save_progress(results, errors):
    """Write results to disk. Call after every single query."""
    output = {"results": results, "errors": errors, "models_tested": MODELS}
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)

def main():
    results = {}
    errors = {}

    for model in MODELS:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"MODEL: {model}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        model_results = {}
        model_errors = []

        for task_id, (task_type, prompt) in TASKS.items():
            print(f"  Running: {task_id}...", file=sys.stderr)
            response, elapsed, error = run_claude(model, prompt)

            if error:
                model_errors.append({"task": task_id, "error": error})
                print(f"    ERROR: {error[:200]}", file=sys.stderr)
            else:
                model_results[task_id] = {
                    "response": response[:10000],
                    "metrics": {
                        "elapsed_s": round(elapsed, 2),
                        "output_chars": len(response),
                    }
                }
                print(f"    OK: {elapsed:.1f}s, {len(response)} chars", file=sys.stderr)

            # Save after every query — partial results survive session death
            results[model] = model_results
            if model_errors:
                errors[model] = model_errors
            save_progress(results, errors)

        print(f"  Saved progress to {OUTPUT}", file=sys.stderr)

    # Restore gateway
    restore_gateway()

    # Print summary
    print("\n\n=== SUMMARY ===", file=sys.stderr)
    for model in MODELS:
        r = results.get(model, {})
        e = errors.get(model, [])
        print(f"  {model}: {len(r)}/{len(MODELS)*len(TASKS)} tasks", file=sys.stderr)

    print(f"\nResults saved to {OUTPUT}", file=sys.stderr)

if __name__ == "__main__":
    main()