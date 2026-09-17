#!/usr/bin/env python3
"""Seed the delegation capability table from the spec's 2.6 snapshot.

The markdown table in the spec is a snapshot of this database table, not a
second source of truth (section 2.6). This puts the measured rows in so the
settings page has something to show; it flips NOTHING operational, because
that is a decision and section 12 has not made it.

Usage
-----
    .venv/bin/python bin/wc-seed-delegation.py --db-path /path/to/some.db

`--db-path` is required and has no default, on purpose: `config.DB_PATH` is
the database a running deployment actually uses (it honours `WC_DB_PATH`,
which is how `systemd/webconsole.service` points it at the real production
file), and a seed script that fell back to it silently would eventually be
run once with no arguments by someone who meant to point it at a scratch
database and forgot to. This script refuses `config.DB_PATH` by default --
pass a throwaway or a staging database, not the production one.

Production's `delegation_capability` table can still be seeded, but only
through `--yes-this-is-production`. That flag is an affirmation, not a mode:
it is inert against anything that is not `config.DB_PATH` -- naming it changes
no behaviour unless `--db-path` also resolves to production, so seeding
production still takes two deliberate things done together (naming the
database *and* affirming what it is), never one flag alone and never a
default. The alternative is real: spec 2.6's 23 rows are measurements taken
on this deployment, not generic defaults, and the only sanctioned fallback
(section 9.2) is typing all 115 cells into the settings page by hand, which
is not a substitute anyone should be reaching for when this script already
has the values. When the flag does cross the guard, the script says so
loudly, naming the database and the time, so a scrollback shows unambiguously
that production was seeded and when.

The refusal compares against `config.DB_PATH` itself, after environment
resolution -- never against a second computation of what that default
"should" be. An earlier version of this script guessed the default from its
own directory (`Path(__file__).resolve().parent.parent / "data" /
"webconsole.db"`), which matches `config.DB_PATH` only when `WC_DB_PATH` is
unset. `systemd/webconsole.service` sets `WC_DB_PATH` explicitly and runs
from a release-snapshot `WorkingDirectory`, so the script's self-relative
guess pointed somewhere other than the real production file, and passing
that real path via `--db-path` sailed straight through the guard. Both sides
of the comparison are canonicalised with `os.path.realpath` so a relative
path, a `..` segment or a symlink cannot slip past either.

Idempotent. The table is keyed on `(model, task_type)` and `delegation_row_set`
upserts (`ON CONFLICT(model, task_type) DO UPDATE`), so running this twice
against the same database leaves it in the same state as running it once --
the second run overwrites each row with the same values rather than
duplicating or erroring.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402

#: (model, task_type, accuracy, n, cost_per_1m_tokens, median_latency_s, max_context)
#: None means TBD, which is not zero -- see CapabilityRow's docstring.
#: All 23 (model, task_type) pairs of spec 2.6's table, in its row order.
ROWS = [
    ("vllm/Qwen3.6-35B-A3B-NVFP4", "coding", 0.66, 44, 0.0, 26.8, 229376),
    ("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context", 0.833, 12, 0.0, 13.9, 229376),
    ("azure_ai/gpt-5.6-luna", "coding", 1.0, 24, 0.0285, 12.8, 922000),
    ("azure_ai/gpt-5.6-luna", "long-context", 1.0, 12, 0.0285, 6.3, 922000),
    ("azure_ai/gpt-5.6-luna", "comprehension", 0.583, 12, 0.0285, 9.2, 922000),
    ("azure_ai/gpt-5.6-luna", "reasoning", 0.5, 6, 0.0285, 16.5, 922000),
    ("azure_ai/gpt-5.6-luna", "voice", None, None, 0.0285, None, 922000),
    ("azure_ai/gpt-5.4-mini", "coding", None, None, 0.5261, None, 1050000),
    ("azure_ai/gpt-5.4-mini", "reasoning", 0.86, None, 0.5261, None, 1050000),
    ("azure_ai/gpt-5.6-luna", "multi-turn", 0.917, 12, 0.0285, 13.5, 922000),
    ("azure_ai/gpt-5.6-luna", "planning", 0.75, 12, 0.0285, 40.7, 922000),
    # The gate rows, measured 2026-09-17 by bench/gate_accuracy.py at n=28 per
    # model per gate, and SPLIT into two task types by operator decision the
    # same day. Pooled into one `reviewer-gate` row they averaged two
    # materially different behaviours: luna is the best reviewer (0.9643, and
    # it false-accepted nothing) while opus is the best security gate
    # (0.9643); sonnet is the worst reviewer of the three and waved through 3
    # real defects. See spec 4.3/4.5 and 2.6.
    ("azure_ai/gpt-5.6-luna", "reviewer-gate", 0.9643, 28, 0.0285, 6.07, 922000),
    ("claude-sonnet-5", "reviewer-gate", 0.7857, 28, 1.5709, 4.555, 1000000),
    ("claude-opus-5", "reviewer-gate", 0.8929, 28, 3.6082, 5.025, 1000000),
    ("azure_ai/gpt-5.6-luna", "security-gate", 0.8571, 28, 0.0285, 5.83, 922000),
    ("claude-sonnet-5", "security-gate", 0.9286, 28, 1.5709, 4.635, 1000000),
    ("claude-opus-5", "security-gate", 0.9643, 28, 3.6082, 4.8, 1000000),
    ("claude-sonnet-5", "coding", 1.0, 24, 1.5709, 15.5, 1000000),
    ("claude-sonnet-5", "long-context", 0.667, 12, 1.5709, 4.2, 1000000),
    ("claude-sonnet-5", "comprehension", 1.0, 12, 1.5709, 6.0, 1000000),
    ("claude-sonnet-5", "reasoning", 0.834, 6, 1.5709, 24.6, 1000000),
    ("claude-sonnet-5", "voice", None, None, 1.5709, None, 1000000),
    # The model that ACTUALLY serves voice on this deployment, added
    # 2026-09-17. It had no row at all until then, so no 1.1 invariant could
    # see it while section 3's voice ladder named two models that have never
    # served a voice turn. accuracy is TBD because bench/tasks.py has no voice
    # tasks; latency and n are real (46 voice_turn_timing rows); the rate is
    # blended from usage_events and is marked with a double dagger in 2.6 --
    # 3.5167/1M for a "mini" model is not credible and needs a real price.
    ("azure_ai/gpt-5.4-mini-copilot", "voice", None, 46, 3.5167, 2.002, None),
    ("claude-sonnet-5", "multi-turn", 1.0, 12, 1.5709, 7.8, 1000000),
    ("claude-sonnet-5", "planning", 0.833, 12, 1.5709, 17.5, 1000000),
    ("claude-sonnet-5", "split-decision", None, None, 1.5709, None, 1000000),
    ("claude-opus-5", "comprehension", 1.0, 12, 3.6082, 11.9, 1000000),
    ("claude-opus-5", "reasoning", 1.0, 6, 3.6082, 10.2, 1000000),
    # azure_ai/gpt-5.6-terra, added 2026-09-17. accuracy/n/median_latency_s are
    # measured (bin/wc-bench.py --repeats 3); max_context is measured from the
    # gateway's own /model/info (922000, same as luna). cost_per_1m_tokens is
    # NOT measured -- it is luna's own rate, assumed onto terra by operator
    # decision (2026-09-17) pending real gateway billing. See spec 2.6's `†`
    # marker and prose note: this column cannot carry that distinction, so
    # every ladder position and tree cost derived from these six rows is
    # provisional until the real rate lands.
    ("azure_ai/gpt-5.6-terra", "coding", 1.0, 18, 0.0285, 7.2, 922000),
    ("azure_ai/gpt-5.6-terra", "long-context", 1.0, 12, 0.0285, 6.3, 922000),
    ("azure_ai/gpt-5.6-terra", "multi-turn", 1.0, 12, 0.0285, 12.4, 922000),
    ("azure_ai/gpt-5.6-terra", "planning", 0.667, 12, 0.0285, 26.9, 922000),
    ("azure_ai/gpt-5.6-terra", "comprehension", 0.5, 12, 0.0285, 9.8, 922000),
    ("azure_ai/gpt-5.6-terra", "reasoning", 0.5, 6, 0.0285, 8.9, 922000),
]


def _canonical(path: str | os.PathLike[str]) -> Path:
    """Resolve *path* to a canonical, comparable form.

    ``os.path.realpath`` (not just ``Path.resolve()``) so a relative path, a
    ``..`` segment, or a symlink all normalise to the same string as the
    real target they point at -- the guard below compares strings, and any
    of those left unresolved would let a disguised production path through.
    """
    return Path(os.path.realpath(str(path)))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the delegation_capability table (spec 2.6).",
    )
    parser.add_argument(
        "--db-path", required=True,
        help="SQLite database to seed. Required, with no default, so this "
             "can never land on the production database by omission.",
    )
    parser.add_argument(
        "--yes-this-is-production", action="store_true",
        help="Affirm that --db-path is meant to be config.DB_PATH, the "
             "database a real deployment uses, and seed it anyway. Has no "
             "effect unless --db-path also resolves to config.DB_PATH -- it "
             "is an affirmation, not a mode, and does not by itself pick "
             "production or change behaviour against a throwaway database. "
             "Naming the database and affirming what it is are both "
             "required; neither alone seeds production.",
    )
    args = parser.parse_args(argv)

    target = _canonical(args.db_path)
    # config.DB_PATH is the database a real deployment is actually using --
    # it already honours WC_DB_PATH (systemd/webconsole.service sets it),
    # so this is not a second guess at what "the default" is; it is the
    # live value, read fresh at call time.
    production = _canonical(config.DB_PATH)
    if target == production:
        if not args.yes_this_is_production:
            parser.error(
                f"refusing to seed {target} -- that is config.DB_PATH, the "
                "database a real deployment uses (WC_DB_PATH honoured). "
                "Point --db-path at a throwaway or staging database "
                "instead, or pass --yes-this-is-production if you mean to "
                "seed production, deliberately, right now."
            )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(
            f"PRODUCTION SEED [{stamp}]: --yes-this-is-production was "
            f"passed and --db-path resolves to {target}, which is "
            "config.DB_PATH -- writing spec 2.6's rows to the real "
            "deployment database now."
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
