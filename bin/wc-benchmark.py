#!/usr/bin/env python3
"""Orchestrate benchmark sweeps over the model x task-type matrix.

Spec: docs/superpowers/specs/2026-09-18-benchmark-design-v2.md

Every form here does what it is told except --scheduled, which is the only
one that may decide to do nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def format_estimate(cell_seconds: list[float], night_hours: list[float],
                    cells_total: int) -> str:
    """Projected duration, stated as what it actually is.

    Measurement time and elapsed time are different quantities here, and the
    difference is nights. Reporting hours alone would read as "finishes
    tonight" for a sweep that takes three.
    """
    if not cell_seconds:
        return "no prior sweep — no estimate"

    median_cell = statistics.median(cell_seconds)
    hours = median_cell * cells_total / 3600.0
    provenance = f"from {len(cell_seconds)} cell" + ("" if len(cell_seconds) == 1 else "s")
    head = f"estimate: {cells_total} cells ≈ {hours:.1f}h measurement ({provenance})"

    if not night_hours:
        return (f"{head}\n"
                f"          nights unknown — no idle-time history yet; "
                f"excludes all idle time")

    per_night = statistics.median(night_hours)
    nights = max(1, int(hours / per_night + 0.999))
    return (f"{head}\n"
            f"          ≈ {nights} nights at {per_night:.1f}h/night "
            f"observed over the last {len(night_hours)} nights")


async def _amain(args) -> int:
    import db
    import benchmark_sweep
    from bench_models import sweep_models, sweep_task_types
    from routes import db_benchmark as store

    await db.init()
    try:
        if args.cell:
            # Shares routes/benchmark.py's measure_one_cell exactly (spec 9),
            # via benchmark_sweep.py where it lives -- a hand-rolled
            # run_cell + record_success/record_failure here used to omit
            # both the dormancy clear on a forced re-measure and the reorder
            # flag, since those only run inside measure_one_cell, after
            # record_success/record_failure return.
            model, task_type = args.cell
            outcome = await benchmark_sweep.measure_one_cell(model, task_type)
            if outcome["status"] == "ok":
                print(f"ok  {model} / {task_type}")
            else:
                print(f"failed  {model} / {task_type}  {outcome['error']}")
            return 0

        if args.status:
            run = await store.run_current()
            if run is None:
                print("no current sweep")
                return 0
            groups = await benchmark_sweep.classify_cells(run)
            print(f"{run['id']}  status={run['status']}  "
                  f"done={len(groups['done'])} pending={len(groups['pending'])} "
                  f"dormant={len(groups['dormant'])} of {run['cells_total']}")
            print(f"  started {run['started_at']}  expires {run['expires_at']}")
            return 0

        if args.estimate:
            cells_total = len(sweep_models()) * len(sweep_task_types())
            cell_seconds = []
            run = await store.run_current()
            if run:
                cell_seconds = [c["elapsed_s"]
                                for c in await store.cells_for_run(run["id"])
                                if c["elapsed_s"]]
            night_hours = await store.measured_hours_per_night()
            print(format_estimate(cell_seconds, night_hours,
                                  cells_total=cells_total))
            return 0

        if args.scheduled:
            action, run = await benchmark_sweep.scheduled_decision()
            if action == "cooling":
                print(f"cooling until {run['cooling_until']} — nothing to do")
                return 0
            if action == "start":
                run = await benchmark_sweep.start_sweep(
                    sweep_models(), sweep_task_types())
            outcome = await benchmark_sweep.run_night(run)
            print(f"{run['id']}: {outcome}")
            return 0

        print("nothing to do; see --help", file=sys.stderr)
        return 2
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true",
                        help="the timer's entry point")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--cell", nargs=2, metavar=("MODEL", "TASK_TYPE"))
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
