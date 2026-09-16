#!/usr/bin/env python3
"""Seed the delegation capability table from the spec's 2.6 snapshot.

The markdown table in the spec is a snapshot of this database table, not a
second source of truth (section 2.6). This puts the measured rows in so the
settings page has something to show; it flips NOTHING operational, because
that is a decision and section 12 has not made it.

Usage
-----
    .venv/bin/python bin/wc-seed-delegation.py --db-path /path/to/some.db

`--db-path` is required and has no default, on purpose: the default database
this repository ships (`config.DB_PATH`, `data/webconsole.db` next to
`config.py`) is the one a running deployment actually uses, and a seed script
that fell back to it silently would eventually be run once with no arguments
by someone who meant to point it at a scratch database and forgot to. This
script refuses that path outright -- pass a throwaway or a staging database,
not the production one. There is no override flag; if the production table
genuinely needs seeding, run this against a copy and swap it in by hand, so
the write happens by a deliberate deploy step rather than a CLI default.

Idempotent. The table is keyed on `(model, task_type)` and `delegation_row_set`
upserts (`ON CONFLICT(model, task_type) DO UPDATE`), so running this twice
against the same database leaves it in the same state as running it once --
the second run overwrites each row with the same values rather than
duplicating or erroring.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402

#: (model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, max_context)
#: None means TBD, which is not zero -- see CapabilityRow's docstring.
ROWS = [
    ("vllm/Qwen3.6-35B-A3B-NVFP4", "coding", 0.66, 44, 0.0, 26.8, 229376),
    ("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context", 1.0, 10, 0.0, None, 229376),
    ("azure_ai/gpt-5.6-luna", "coding", 1.0, 24, 0.0285, 12.8, 922000),
    ("azure_ai/gpt-5.6-luna", "long-context", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "comprehension", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "reasoning", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "voice", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.6-luna", "reviewer-gate", None, 9, 0.0285, 11.1, 922000),
    ("azure_ai/gpt-5.4-mini", "coding", None, None, 0.5261, None, 1050000),
    ("azure_ai/gpt-5.4-mini", "reasoning", 0.86, None, 0.5261, None, 1050000),
    ("claude-sonnet-5", "coding", 1.0, 24, 1.5709, 15.5, 1000000),
    ("claude-sonnet-5", "comprehension", 1.0, 2, 1.5709, None, 1000000),
    ("claude-sonnet-5", "reasoning", 0.75, 2, 1.5709, None, 1000000),
    ("claude-sonnet-5", "voice", None, None, 1.5709, None, 1000000),
    ("claude-sonnet-5", "reviewer-gate", None, None, 1.5709, None, 1000000),
    ("claude-opus-5", "comprehension", 0.5, 2, 3.6082, None, 1000000),
    ("claude-opus-5", "reasoning", None, None, 3.6082, None, 1000000),
]


def _default_db_path() -> str:
    """The path config.py falls back to with no WC_DB_PATH set -- the one a
    real deployment is using unless told otherwise."""
    return str(Path(__file__).resolve().parent.parent / "data" / "webconsole.db")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the delegation_capability table (spec 2.6).",
    )
    parser.add_argument(
        "--db-path", required=True,
        help="SQLite database to seed. Required, with no default, so this "
             "can never land on the production database by omission.",
    )
    args = parser.parse_args(argv)

    target = Path(args.db_path).resolve()
    default = Path(_default_db_path()).resolve()
    if target == default:
        parser.error(
            f"refusing to seed {target} -- that is the default production "
            "database path (config.DB_PATH with WC_DB_PATH unset). Point "
            "--db-path at a throwaway or staging database instead."
        )

    config.DB_PATH = str(target)
    print(f"seeding delegation_capability rows in {target}")

    await db.init()
    try:
        for model, task_type, accuracy, n, cost, latency, context in ROWS:
            await db.delegation_row_set(
                model, task_type, accuracy=accuracy, n=n,
                cost_per_1m_tokens=cost, median_latency_s=latency,
                max_context=context)
        print(f"seeded {len(ROWS)} capability rows; nothing is operational")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
