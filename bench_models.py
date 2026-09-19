# bench_models.py -- what a full sweep covers.
#
# DEFAULT_MODELS is the harness's own list and the thing a sweep must be
# reproducible against, so it is the source of truth rather than whatever
# delegation_capability happens to hold.
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def sweep_models() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "wc_bench_models", REPO_ROOT / "bin" / "wc-bench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.DEFAULT_MODELS)


def sweep_task_types() -> list[str]:
    from bench.tasks import TASKS
    seen: list[str] = []
    for task in TASKS:
        if task.task_type not in seen:
            seen.append(task.task_type)
    return seen
