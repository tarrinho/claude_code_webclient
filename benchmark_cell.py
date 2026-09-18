# benchmark_cell.py -- measure one (model, task_type) pair.
#
# One subprocess per cell, for crash containment: a model that hangs or dies
# takes its own process with it and the sweep continues (spec 7).
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BENCH = REPO_ROOT / "bin" / "wc-bench.py"


@dataclass(frozen=True)
class CellResult:
    status: str                       # "ok" | "failed"
    accuracy: float | None
    n: int | None
    median_latency_s: float | None
    elapsed_s: float
    error: str | None


def _tasks_for(task_type: str) -> list[str]:
    """Task ids for a task_type, resolvable through bin/wc-bench.py's --tasks
    (bin/wc-bench.py:234 looks them up in bench.tasks.BY_ID, which is keyed by
    ``id``, not by any ``name`` field -- Task has no such field)."""
    from bench.tasks import TASKS
    return [t.id for t in TASKS if t.task_type == task_type]


def parse_bench_payload(payload: dict, task_type: str) -> CellResult:
    """Fold wc-bench.py's per-task summary into one capability row.

    Pure, so the three outcomes the sweep must survive are table tests rather
    than subprocess tests.

    A task whose runs all errored contributes to `accuracy` (it scored zero)
    but not to the latency median -- there is no latency to take. A cell where
    EVERY task errored has nothing to write and is a failure.
    """
    summary = payload.get("summary") or {}
    if not summary:
        return CellResult("failed", None, None, None, 0.0,
                          f"no tasks measured for task_type={task_type}")

    rates, latencies, repeats = [], [], 0
    for entry in summary.values():
        rates.append(float(entry.get("pass_rate") or 0.0))
        repeats += int(entry.get("repeats") or 0)
        median = entry.get("total_s_median")
        if median is not None:
            latencies.append(float(median))

    if not latencies:
        return CellResult("failed", None, None, None, 0.0,
                          "every task errored; no latency to record")

    return CellResult(
        status="ok",
        accuracy=round(sum(rates) / len(rates), 3),
        n=repeats,
        median_latency_s=round(statistics.median(latencies), 2),
        elapsed_s=0.0,
        error=None,
    )


async def _run_subprocess(argv: list[str], timeout_s: float) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError((stderr or b"").decode(errors="replace")[:500])
    return proc.returncode


async def run_cell(model: str, task_type: str, repeats: int = 3,
                   timeout_s: float = 1200.0) -> CellResult:
    """Measure one cell. Never raises: every failure becomes a CellResult."""
    tasks = _tasks_for(task_type)
    if not tasks:
        return CellResult("failed", None, None, None, 0.0,
                          f"no bench tasks for task_type={task_type}")

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.json"
        argv = [
            sys.executable, str(BENCH),
            "--models", model,
            "--tasks", ",".join(tasks),
            "--repeats", str(repeats),
            "--out", str(out),
        ]
        try:
            await _run_subprocess(argv, timeout_s)
        except TimeoutError:
            return CellResult("failed", None, None, None,
                              time.monotonic() - started,
                              f"timeout after {timeout_s:.0f}s")
        except Exception as exc:                      # noqa: BLE001
            return CellResult("failed", None, None, None,
                              time.monotonic() - started, str(exc)[:500])
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            return CellResult("failed", None, None, None,
                              time.monotonic() - started,
                              f"unreadable bench output: {exc}"[:500])

    result = parse_bench_payload(payload, task_type)
    elapsed = time.monotonic() - started
    return CellResult(result.status, result.accuracy, result.n,
                      result.median_latency_s, elapsed, result.error)
