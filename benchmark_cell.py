# benchmark_cell.py -- measure one (model, task_type) pair.
#
# One subprocess per cell, for crash containment: a model that hangs or dies
# takes its own process with it and the sweep continues (spec 7).
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BENCH = REPO_ROOT / "bin" / "wc-bench.py"

_log = logging.getLogger(__name__)

#: Startup margin added on top of the per-run budget below: subprocess spawn,
#: interpreter import, and CLI login/session setup, none of which is a model
#: run and none of which bench/transports.py's own per-run cap accounts for.
_STARTUP_MARGIN_S = 120.0


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


def _timeout_for(tasks: list[str], repeats: int) -> float:
    """Derive the per-cell timeout from the work the cell actually does.

    A cell runs every task in `tasks` `repeats` times, and each individual
    run is already capped by the harness itself (bench/transports.py
    `TIMEOUT_S`, from `WC_BENCH_TIMEOUT_S`, default 1000s). The outer,
    per-cell budget has to be at least `runs * per_run_cap` or it fires on a
    cell that is working perfectly normally -- a fixed constant like the old
    1200s default breaks the moment a task_type gets more tasks (`coding`'s
    12 tasks * 3 repeats = 36 runs already exceeds it on its own). Reading
    the same env var the harness reads means the two figures cannot drift
    apart.
    """
    per_run_cap = float(os.environ.get("WC_BENCH_TIMEOUT_S", "1000"))
    runs = len(tasks) * max(repeats, 1)
    return runs * per_run_cap + _STARTUP_MARGIN_S


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
    # start_new_session=True puts the child in its own process group. Without
    # it, wc-bench.py's own grandchild -- the `claude` CLI it spawns via
    # subprocess.Popen (bench/transports.py:222) -- shares our process group,
    # and its own timeout guard (bench/transports.py:291, `proc.wait(timeout=
    # TIMEOUT_S)`) runs *inside* wc-bench.py. SIGKILLing only the direct child
    # on timeout kills that guard along with it and orphans `claude`: still
    # running, still holding its credentialed environment, still spending API
    # credit, with nothing watching it, and its temp workdir never cleaned up.
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already exited on its own between the timeout and here
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError((stderr or b"").decode(errors="replace")[:500])
    return proc.returncode


async def run_cell(model: str, task_type: str, repeats: int = 3,
                   timeout_s: float | None = None) -> CellResult:
    """Measure one cell. Never raises: every failure becomes a CellResult.

    `timeout_s=None` (the default) derives the budget from the task list and
    `repeats` via `_timeout_for`, so it scales with the work instead of being
    a constant that a bigger task_type can silently outgrow. Pass an explicit
    value to override -- tests do, so they do not have to wait out a real
    budget to exercise the timeout path.
    """
    tasks = _tasks_for(task_type)
    if not tasks:
        return CellResult("failed", None, None, None, 0.0,
                          f"no bench tasks for task_type={task_type}")

    if timeout_s is None:
        timeout_s = _timeout_for(tasks, repeats)

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
            # Logged before conversion to CellResult so a bug in this file
            # (a NameError, an AttributeError) leaves a traceback in the
            # journal instead of looking identical to an ordinary bad model
            # run in the morning. KeyboardInterrupt/SystemExit/CancelledError
            # are BaseException, not Exception, so they still propagate.
            _log.exception(
                "benchmark_cell: run_cell subprocess failed "
                "model=%s task_type=%s", model, task_type)
            return CellResult("failed", None, None, None,
                              time.monotonic() - started, str(exc)[:500])
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            _log.exception(
                "benchmark_cell: run_cell could not read bench output "
                "model=%s task_type=%s", model, task_type)
            return CellResult("failed", None, None, None,
                              time.monotonic() - started,
                              f"unreadable bench output: {exc}"[:500])

    result = parse_bench_payload(payload, task_type)
    elapsed = time.monotonic() - started
    return CellResult(result.status, result.accuracy, result.n,
                      result.median_latency_s, elapsed, result.error)
