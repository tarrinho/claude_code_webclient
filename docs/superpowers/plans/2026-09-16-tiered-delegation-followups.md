# Tiered delegation — follow-ups after 0.19.0

Release 0.19.0 built the machinery of
`docs/superpowers/specs/2026-09-14-tiered-agent-delegation-spec-v3.md` and
deliberately left it switched off. Nothing routes: no production code imports
the pipeline or the oracle, `assign_model` has no callers, and
`app.state.capability_table` is written at startup and never read.

These are the items found during implementation and review that were **not**
fixed, with the reason. They are ordered by when they must be dealt with, not
by size.

## Closed since this list was written (2026-09-16, later the same day)

| item | resolution | commit |
|---|---|---|
| F6 — `GateResult.gate` unconstrained | `GATE_REVIEWER`/`GATE_QA`/`GATE_SECURITY` constants; an unrecognised name now **raises** instead of silently taking the reviewer path | `5bc9546` |
| F7 — `reasoning` hold had no guard | added to `_OPERATIONAL_FLIP_BLOCKED`, which became a `dict` so each held type states its own blocker | `9426854` |
| F8 — five columns declared four times | `delegation.js` now derives its list from the `editable_columns` the API already returned and it ignored | `c2763dc` |
| ARCHITECTURE §9 stale | ~40 counts corrected, `routes/supervisors.py` (nonexistent) removed, `delegation.js` added, subtotals recomputed | `4219490` |
| F1 — partial cache became a boot refusal | completeness is now positive evidence: strict membership only when every `ai_machines` row has a populated `models_list`; otherwise fall back and log which machine to refresh | `39b747f` |
| F5 — two notions of "the gate model" | `ladder("reviewer-gate")[0]` when the ladder yields a rung, cheapest-priced otherwise, recording which path fired | `39b747f` |
| coverage lost to F1's own fix | `_seed_machine_serving` now populates `models_list`, so the two tests exercise strict membership again rather than the fallback | `38936a7` |

That last row is worth keeping visible. F1's ruling silently downgraded two passing tests to a weaker code path while their docstrings still claimed the stronger one — the tenth instance in this release of a test that asserts a correct value while not exercising what it names, and the first caused by a fix rather than found in existing code. It was proven by reverting only the test file with the mutation still applied and watching both tests pass.

**The §5.1 ceiling contradiction (F4 below) has since been ruled on and measured** — see §5.1's "Decided 2026-09-16" subsection and §2.6's `claude-sonnet-5` × `reviewer-gate` row at 3.675s over n=20. The worst case is now 1,398.2s against the 1,500s ceiling. It remains a lower bound because §4.5's security re-run term is still unpriced, which is the §12 dependency below.

## Must close before the first `operational` flip

### F1 — a partially populated model cache becomes a boot refusal

`delegation_startup.py`, `routes/machines.py:known_backend_models()`,
`tiered_delegation.py:_resolution_problems`.

`live_known_models()` degrades correctly when the lookup *raises*: it returns
`None` and validation falls back to a shape-only check. But
`known_backend_models()` cannot realistically raise — it unions
`config.KNOWN_MODELS` with on-disk state and always returns a non-empty set.
`models_list` is populated only by a force-refresh through the Backends UI, so a
machine never visited there contributes just its default `model` and
`active_models`.

Failure: an operator flips a type operational while the cache is warm. Later the
machine row is edited or replaced, clearing `models_list`. The next restart
reports `rung ... is not in the model combo box (spec 9.3)`, raises
`DelegationConfigError`, and the console does not start — with the settings
page, the only repair tool, unreachable because it needs the app running.

The degradation path exists for a thrown exception but not for the far likelier
partially-populated cache. Closing this needs a decision about cache freshness —
how stale a model list may be before it is treated as unknown rather than
authoritative — not a quick patch.

### F2-adjacent — the oracle's remaining hardening

`delegation_oracle.py`. The process-group leak, `cwd` and environment scrubbing
were fixed in `c4f6df9`. What remains is a judgement call nobody has made: this
module executes model-produced code with a subprocess and a timeout, and no
sandbox, container or resource limit beyond that. Before anything routes through
it, decide whether that is sufficient for the code it will actually be running.

### F6 — `GateResult.gate` is an unconstrained string

`delegation_pipeline.py:214-224`, `:354`.

Every other multi-way value in this release is a named constant —
`MUTATES_FALSE/SIDE_EFFECTING_READ/TRUE`,
`LEAF_FAILED/FAILED_HUMAN_FLAGGED/ESCALATED_TO_HUMAN`. `gate` is the exception,
and `gate_exhaustion_outcome` does `if gate != "security": return LEAF_FAILED`.

A future caller constructing `GateResult(gate="Security")`, `"security_gate"` or
the stage number `5` gets the **safe-looking** answer: every security exhaustion
silently becomes an ordinary failed leaf and §4.5's human-escalation rule is off
entirely. No test can catch it, because the failure lives in a caller that does
not exist yet.

Fix: `GATE_REVIEWER` / `GATE_QA` / `GATE_SECURITY` constants, and reject an
unrecognised gate name rather than defaulting.

### F5 — two notions of "the gate model" that diverge when §12 resolves

`tiered_delegation.py:328-389`.

`_gate_latency_s` picks the cheapest **priced, non-excluded** `reviewer-gate`
row and deliberately does not require a measured accuracy — correctly, so the
invariant does not pre-decide §12. But once `reviewer-gate` becomes
ladder-eligible its real rung 0 is `ladder("reviewer-gate")[0]`: the cheapest row
that *also* survives the accuracy walk.

Today's data is exactly the divergent shape — luna's `reviewer-gate` row is the
cheapest and has no accuracy. Measure sonnet's gate accuracy but not luna's, and
`_gate_latency_s` returns luna's 11.1s while the gate actually runs on sonnet, so
§1.1's latency invariant is computed against a model that is not the gate's rung
0.

## Needs an operator ruling, not a patch

### F4 — §5.1 contradicts §4.3/§4.5 on the worst-case path

`tiered_delegation.py:402-449`.

`_worst_case` sums three gate calls, each run once, each at the floor — which is
what §5.1's formula says. But this release implemented
`delegation_pipeline.next_gate_rung`, letting a gate climb a rung and re-review,
and `SECURITY_RERUN_CAP = 2`, permitting two generation→review→fix→review
cycles. Neither appears in the sum.

One extra gate call at the floor costs `90 × 2.0 × (11.1/12.8)` = **156.1s**
against a published margin of **257s**. Two extra calls put the path at
**1,555s**, over the 1,500s ceiling.

The contradiction is between spec sections and predates the branch. What changed
is that 0.19.0 is the first to implement *both* halves and the first to make
§1.1 recompute the figure and compare it to the ceiling on every boot — so
§1.2's `ceiling fits the budget | pass` for `coding` is computed on a pipeline
model the gate code contradicts. Decide which section is authoritative.

### The `side_effecting_read` trivial-bypass reservation

Spec §4.8, amended by `af5ac83`.

That amendment put a trivial `side_effecting_read` on the write row, reasoning
that giving it a reviewer gate an actual write does not get would make it
stricter than `True`. That argument is about the *gate*. §4.8's hole argument is
about *reads* — and `side_effecting_read` is a read.

Under the write row a trivial `side_effecting_read` gets stages `[1, 2]`, and
§4.2/§4.7 skip stage 2 on prose output, so a prose-producing trivial
`side_effecting_read` finishes with **no verification at all** — the exact hole
§4.8's scoping rule was written to close, reproduced for the third value.

Unreachable today: no `PATTERNS` row declares `side_effecting_read`, and
`resolve_mutates` only yields it under unanimity. Schedule a ruling rather than
patching either way.

### F7 — `reasoning`'s hold has no code guard while `coding`'s does

Spec amendment `b782e4d` added: "Until it is either enforced in code or lifted
by measurement, `reasoning` must stay non-operational (§1.1)."
`routes/delegation.py:71`'s `_OPERATIONAL_FLIP_BLOCKED` contains only `coding`.

The ruling that created that guard reasoned the constraint "was being held only
by nobody having clicked". The identical argument applies to `reasoning`, and
nobody re-applied it because the amendment landed after Task 9 closed.

Mitigating: the flip is refused today by data anyway — `claude-sonnet-5`'s
reasoning row has no `median_latency_s`, and its one-rung tree cost is $3.736
against `BUDGET_USD` 1.00. So it is a decision-versus-data gap, not an open
door — but the hold is about a 75% n=2 accuracy figure, and a latency-and-cost
measurement could clear both data blocks without touching it.

## Housekeeping

### F8 — the five measured columns are declared four times

`tiered_delegation.py:70` (`_REQUIRED_COLUMNS`), `routes/db_delegation.py:15`
(`_COLUMNS`), `routes/delegation.py:62` (`_EDITABLE`),
`web/assets/delegation.js:17` (`COLUMNS`) — identical content and order, with one
test asserting the first three agree and nothing covering the fourth.

§11 lists "cost basis" as a still-uncovered column; adding it needs four
coordinated edits with nothing red if one is missed. The mechanism that would
make the JS copy derived already exists and is ignored: `GET /api/delegation`
returns `editable_columns` and `delegation.js` never reads it.

### ARCHITECTURE.md's file inventory needs a structural pass, not more spot fixes

The named errors are fixed in `4219490` — counts corrected, the nonexistent
`routes/supervisors.py` removed, a duplicate `routes/misc.py` entry that
shadowed the real nested one removed, `web/assets/delegation.js` added,
subtotals recomputed so they sum to their entries.

That pass surfaced drift far larger than the review knew about, and it was
deliberately **not** attempted — it is a restructuring of the table rather than
a correction of it, and two agents were editing the tree at the time, so any
count taken would have gone stale before it committed:

| entry | stated | actual |
|---|---|---|
| `routes/` | 5 files itemised | **29 files** |
| `web/assets/` | 10 itemised | **29 files** |
| `db.py` | 3,600 lines | **1,631** — a chunk moved into `routes/db_*.py` |
| `tests/` | 109 files, 2,481 cases, 39,825 lines | **297 files, 86,633 lines** |

The `tests/` line additionally needs a pytest collection run to refresh its case
count, skipped to avoid load on a host already below the memory preflight.

`bin/`'s total stays untouched by decision, not oversight: it reconstructs from
no obvious methodology (raw `wc -l` over `bin/*.py` and `bin/*.sh` gives 12,717
against a stated 3,280, later 3,417), so it has been incremented by new files'
own counts rather than recomputed on a guess. **Two separate passes have now
failed to derive it.** Either find the original rule and write it down beside
the number, or replace the figure with one whose methodology is stated.

**Why this is worth scheduling rather than leaving:** an inventory's whole value
is that a reader can trust it is complete. One listing 5 of 29 files in a
directory is not a partial inventory, it is a misleading one — the same
reasoning that made a false docstring worth fixing earlier in this release.

### A pre-existing test failure, unrelated to this release

`tests/test_qa_usage_origin.py::RoutedAttributionQA::test_an_imported_row_records_what_the_session_was_asked_to_run`
fails on this host, and fails identically at `f8fcffe` — the commit this branch
started from — so it is not caused by 0.19.0.

Root cause is environmental: the fixture patches the requested model to
`claude-opus-5` and asserts it differs from `TESTING_MODEL`, but `TESTING_MODEL`
is also `claude-opus-5` here, so the two cannot diverge and the fixture's own
assertion message says so. It is a can't-diverge fixture, the same family of
defect this release found eight times in its own tests.

## Verification gap at merge

The full suite was never run as a single `pytest tests/` invocation: rules.md §0
requires 1 GB free memory and §0b 2 GB, and this host had ~620 MB free with the
live console holding 833 MB. That gate exists because a full run previously
SIGKILLed `webconsole.service`.

Instead the 284 non-browser test files were run in ten sequential batches of 30,
with a free-memory floor checked before each: **4,761 passed, 6 skipped, 1
failed** — the failure being the pre-existing one above. Six skips matches the
documented signature of a trustworthy run on this host. The four browser files
were excluded; `tests/test_frontend_browser.py` passed separately at 115 passed,
2 failed, both being the flake its own helper docstring documents.
