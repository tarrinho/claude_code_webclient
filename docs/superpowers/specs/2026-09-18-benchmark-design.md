# Benchmark — design

**Status:** design approved 2026-09-18, not yet implemented.
**Scope:** a `benchmark` functionality that measures every model against every
task type, records how long it takes, and offers its results to
`delegation_capability` as an explicit, reviewable step.

---

## 1. Why this exists

The measurement machinery already works. `bin/wc-bench.py` runs a
model × task matrix with repeats and verifiers; `bench/tasks.py` holds 34 tasks
across 7 task types. What has never existed is anything that **orchestrates a
full sweep** or **gets the results into the table that routing reads**.

On 2026-09-17 the capability table was filled by hand: measurements were read
out of `bench_results_*.json`, typed into `bin/wc-seed-delegation.py`'s `ROWS`
list, mirrored into §2.6's markdown table, and seeded. That happened five times
in one day. Two things went wrong that a tool would have prevented:

- **`azure_ai/gpt-5.6-sol` was measured across six task types and then not
  written anywhere.** The results sat in `bench_results` JSON while the
  Delegation page reported `planning` blocked on cost — a blocker those very
  measurements cleared ($1.936 → $0.724).
- **Measurements from different days were mixed in one column.**
  `azure_ai/gpt-5.6-luna` on `coding` is recorded at 12.8s from 2026-09-15 and
  re-measured at 7.33s on 2026-09-17, on the same six tasks. Every deadline and
  the whole latency ceiling derive from that column.

This design targets those two failures specifically. It is not a rewrite of the
benchmark harness.

## 2. Decisions

Taken by the operator on 2026-09-18, recorded with their reasoning because each
one closed off a cheaper alternative.

| Decision | Chosen | Rejected, and why it matters |
|---|---|---|
| Where it runs | **CLI now, read-only page later**, with the database as the progress store | A page-first design needs a job subsystem the console does not have. Staging it means the page is later a reader, not a rewrite. |
| Results flow | **Stored separately, promoted explicitly** | Writing straight into `delegation_capability` would re-order live ladders progressively across a 7-hour sweep, and leave a half-finished sweep in a state nobody chose. `coding` is operational as of 2026-09-18, so that is live routing. |
| Run scope | **Full matrix every time** | Incremental runs are cheaper but mix measurement days, which is the defect in §1. A run is a self-consistent snapshot taken in one window. |
| Orchestrator shape | **Thin loop, one subprocess per cell** | An in-process loop loses crash containment. See §7. |

## 3. What a run costs

Measured on 2026-09-17 over 22 real cells: **6.0 minutes per
(model × task type)**.

```
10 models × 7 task types = 70 cells
70 × 6.0 min             ≈ 7.0 hours, sequential, at 3 repeats
```

**"All models" needs defining, because there are two candidate lists and they
disagree.** `bin/wc-bench.py`'s `DEFAULT_MODELS` holds 10; `delegation_capability`
holds 8. The overlap is 7.

- `claude-fable-5` and `claude-haiku-4-5` are in the harness and have **no
  capability rows at all** — never measured on anything.
- `azure_ai/gpt-5.6-terra` has **6 measured rows** in the capability table and is
  **absent from `DEFAULT_MODELS`** — so a full sweep as currently configured
  would never re-measure a model that is a live rung on `long-context` and
  `multi-turn`.

**This design takes `DEFAULT_MODELS` as the source of truth** — it is the
harness's own list and the thing a run must be reproducible against — and treats
terra's absence from it as a **defect in that list to be fixed before the first
sweep**, not as a scoping decision. A benchmark that silently skips a routing
rung is worse than no benchmark.

**Sequential is not a tuning choice.** These runs record `median_latency_s`, and
two benchmarks against one gateway measure their own contention rather than the
model. Every sweep on 2026-09-17 was sequential for this reason.

Per-cell time is not uniform — observed range was roughly 1 minute to the 900s
timeout cap — so any projection must come from measured cells, never from a
constant.

## 4. Storage

Two tables, added through `db.py`'s existing migration dictionary.

```
benchmark_runs
    id            TEXT PRIMARY KEY     -- e.g. "2026-09-18T10-30-00Z"
    started_at    TEXT NOT NULL
    finished_at   TEXT                 -- NULL while running or interrupted
    status        TEXT NOT NULL        -- running | done | interrupted
    models        TEXT NOT NULL        -- JSON list, frozen at start
    task_types    TEXT NOT NULL        -- JSON list, frozen at start
    repeats       INTEGER NOT NULL
    cells_total   INTEGER NOT NULL

benchmark_cells
    run_id            TEXT NOT NULL
    model             TEXT NOT NULL
    task_type         TEXT NOT NULL
    status            TEXT NOT NULL    -- pending | ok | failed
    accuracy          REAL
    n                 INTEGER
    median_latency_s  REAL
    elapsed_s         REAL NOT NULL DEFAULT 0
    error             TEXT
    recorded_at       TEXT
    PRIMARY KEY (run_id, model, task_type)
```

**`models` and `task_types` are frozen into the run row at start**, not read
live per cell. This is what makes a run a snapshot: if `DEFAULT_MODELS` gains an
entry mid-sweep, the run must not silently become a matrix nobody requested.
Full-matrix-every-time was chosen for self-consistency, and reading the model
list live would give that away.

`elapsed_s` is load-bearing rather than informational: it is the only input to
the projection in §6.

## 5. CLI surface

```
bin/wc-benchmark.py                       start a full-matrix run
bin/wc-benchmark.py --resume <run-id>     finish an interrupted run
bin/wc-benchmark.py --status [<run-id>]   progress and timing; runs nothing
bin/wc-benchmark.py --estimate            projected duration; runs nothing
bin/wc-benchmark.py --promote <run-id>    diff against delegation_capability
bin/wc-benchmark.py --promote <run-id> --apply
```

`--models` and `--task-types` override the frozen defaults for a *new* run, for
the case where one model needs re-measuring on its own. The override is recorded
in the run row, so a partial run is never mistaken for a full snapshot.

**Resumability is required, not a nicety.** A 7-hour job will be interrupted:
the host OOM-killed a background task on 2026-09-17, and the service restarted
eleven times that day. Completed cells are written as they finish, so `--resume`
re-runs only `pending` cells. A *new* run still re-measures everything —
resume finishes an interrupted sweep, it does not skip work.

## 6. Timing, and the projection

Per cell, on stdout:

```
[14/70] azure_ai/gpt-5.6-luna / planning   done 5m42s   elapsed 1h18m   eta 5h36m (from 14 cells)
```

The ETA is computed from the cells **this run** has already measured, and states
how many it is based on. An ETA from two cells is not an ETA, and saying so is
cheaper than being quietly wrong.

`--estimate` reports the projected duration of a full sweep using the median
cell time of the **last completed run**. With no prior run it prints
`no prior run — no estimate` rather than seeding itself with a constant. The
2026-09-17 figure of 6.0 min/cell came from a different model mix and is
recorded in §3 as history, not as a default.

## 7. Failure handling

**A failing cell is recorded as `failed` with its error, and the sweep
continues.** This is the argument for one subprocess per cell, and it is not
hypothetical:

- `azure_ai/gpt-5.4-mini-copilot` failed 12 of 12 attempts on 2026-09-17 — the
  CLI's resolved backend does not serve it (CLAUDE.md §0.1). The sweep carried
  on.
- `azure_ai/gpt-5-mini` hit the 900s timeout on `planning`, and
  `vllm/Qwen3.6-35B-A3B-NVFP4` hit it on `reasoning`.

A timeout is recorded as a **failure carrying the cap**, never as a
measurement — `vllm`'s `reasoning` cell produced an n=2 median of 356s, which is
a truncation artefact and would be a lie in the capability table.

An interrupted run keeps `status = running` with cells still `pending`.
`--status` reports a run whose most recent cell is older than **four times the
harness timeout cap** as `likely interrupted`. Derived rather than chosen: the
cap is the longest a single cell can legitimately take (900s was hit twice on
2026-09-17), so anything beyond a small multiple of it is not slowness. Nothing
can distinguish a dead sweep from a slow one by state alone, so this is a
heuristic and is labelled as one — `--status` says "likely", never "dead".

## 8. Promote

`--promote <run-id>` prints a per-cell diff and **writes nothing**:

- rows that would be **added**,
- rows whose values would **change**, old → new,
- cells the run **failed** to measure, which are left alone.

`--apply` writes through the existing `db.delegation_row_set`. It refuses while
the run's status is `running`, and promotes only cells with status `ok`.

**One column is not blindly promotable.** The harness measures over the **CLI
transport**; `routes/voice.py` is §0's documented exception and speaks to an
OpenAI-compatible endpoint directly. Voice latencies measured over the CLI were
6.6–9.6s against the ~2.0s the voice path records, so promoting that figure
would corrupt §5.1's deadline derivation, which divides by exactly this column.
**Promote writes `accuracy` and `n` for `voice` but skips `median_latency_s`,
and prints the reason.**

`--apply` also prints the **resulting ladder changes**, not only the cell diff.
`coding` has been operational since 2026-09-18, so a promote changes which model
answers real tasks; a diff of five numeric cells does not make that visible, and
a ladder before/after does.

## 9. What this does not do

- **It does not replace `bin/wc-bench.py`.** That harness keeps its arguments,
  verifiers and repeat logic; this orchestrates it.
- **It does not measure what the CLI cannot reach.**
  `azure_ai/gpt-5.4-mini-copilot` — the model that actually serves voice — is
  unmeasurable by this harness, and a full sweep will record 7 failed cells for
  it every run — roughly 10% of the matrix spent confirming a known failure. Measuring it needs a second transport in the harness, which is
  its own design.
- **It does not update spec §2.6's markdown table.** That mirror stays manual,
  and `tests/test_qa_delegation_shipped_state.py` already fails when the table
  and the seed rows disagree.
- **It does not flip anything operational.**

## 10. Testing

Following the repo's `tests/test_qa_*.py` convention.

- The cell loop against a **fake subprocess**: a cell that succeeds, one that
  fails, one that times out — asserting the sweep continues past all three and
  records each correctly.
- **Resume**: an interrupted run re-runs only `pending` cells, and a new run
  re-runs everything.
- **The frozen matrix**: changing `DEFAULT_MODELS` mid-run must not change the
  cells a resumed run executes.
- **Promote, with no model calls at all** — the diff is pure, and it is the part
  that touches live routing.
- **The voice latency exclusion**, asserted directly: a promoted voice cell
  writes `accuracy` and `n` and leaves `median_latency_s` untouched.
- **Projection**: an ETA from one cell must say it is from one cell.

## 11. Open items

- **A second transport for the harness.** Until it exists, `voice` accuracy is
  measured over a transport voice does not use, and the copilot model cannot be
  measured at all. Both are recorded in §2.6 behind a `§` marker.
- **The read-only page.** Deliberately deferred. It reads `benchmark_runs` and
  `benchmark_cells`; no schema change should be needed for it, and if one is,
  this design got the storage wrong.
