"""A benchmark harness whose failures are distinguishable from a model's.

This exists because the previous one's failures were not. `bin/model_benchmark.py`
ranked Qwen3.6 last at 55.4% with "Code Quality 0", and every one of those
numbers was a property of the harness: a `max_tokens` cap calibrated on
non-reasoning models, an extractor that replaced a truncated thinking block with
the literal string `[thinking]`, and a 180s read timeout that no model slower
than the Azure five could clear. Three limits, one indistinguishable-from-broken
output, and a verdict that stood in a document for a day.

The design rule that follows from it: **a number this harness reports must name
what produced it.** Concretely —

* `transport` is recorded on every result, because the same model answers the
  water-jug task in 23.0s over the CLI and cannot finish it in 600s over HTTP.
  A speed figure that does not say which path it came from is not a
  measurement.
* `stop_reason` and `cap_headroom` are recorded, because a run that hit its
  token ceiling must not be scoreable as a wrong answer.
* correctness on code tasks comes from **executing** it, not from reading it.
* every task runs `--repeats` times, because `Consistency` was weighted at 9%
  of the old score and measured zero times.

Modules:

* `tasks`    — the task set, each with a mechanical verifier
* `verify`   — runs model-written code in a subprocess and scores it
* `transports` — HTTP and CLI paths, both streaming, both reporting TTFT
* `cost`     — per-token rates, and cost per *correct* answer
"""
from __future__ import annotations

__all__ = ["cost", "tasks", "transports", "verify"]
