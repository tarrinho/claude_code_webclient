# Benchmark — design v2

**Status:** design approved 2026-09-18, not yet implemented.
**Scope:** a `benchmark` functionality that measures every model against every
task type, writes each result into `delegation_capability` as it is produced,
runs itself on a continuous cycle in the box's idle hours, and can be forced on
a single cell from the Delegation page.

This document is the single source of truth for benchmarking. Where spec v3
§10.2 ("Scheduled re-benchmark") differs, this one governs.

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

| Decision | Chosen | Rejected, and why it matters |
|---|---|---|
| Where it runs | **CLI, a nightly job, and one interactive control on the Delegation page** | A job subsystem is not needed: the schedule is a systemd timer and the page control drives one cell. |
| Results flow | **Each cell writes as it completes; no review step** | Holding results for human promotion leaves the table stale between visits, and there is no operator in the loop at 03:00. See §11 for the one safeguard this leaves standing. |
| Run scope | **Full matrix per sweep**; **one cell** for a forced re-measure | Incremental sweeps mix measurement days, which is the defect in §1. A forced cell is explicitly not a snapshot and is marked as such (§9). |
| Cadence | **Continuous**: a new sweep begins two days after the last one finished | A long fixed interval lets the table drift past what this gateway does — models were renamed and added within days on 2026-09-03, and a stale row routes to a model that may no longer serve it. |
| Contention | **Idle hours only; stop the moment the box is in use** | Measuring under load records contention rather than the model, and `median_latency_s` is the column every deadline and the latency ceiling derive from. |
| Orchestrator shape | **Thin loop, one subprocess per cell** | An in-process loop loses crash containment. See §7. |

## 2.1 What the capability table currently controls

The delegation ladder is not reachable from a conversation turn:

- `ModelRouter.assign_model` has no production caller. `self.router` is
  constructed at `orchestrator.py:525`.
- `app.state.capability_table` is written at `app.py:629` by the startup
  validation and is read nowhere.
- An ordinary turn picks its model in `runner.get_default_model` — the chat's
  own model, then the chat's backend default, then the `default_model` setting,
  then `config.MODEL_NAME`. The capability table is not consulted.

Release 0.19.0's stated scope — machinery complete, nothing routes — is still
literally true, and flipping a task type operational changed nothing about which
model serves a turn.

**This bounds the blast radius of everything below, and it will not hold
forever.** Today an unattended write changes a data table and nothing else.
Whoever wires routing to the ladder is also arming this design: from that commit
onward, a job running at 03:00 can change which model serves real work, with no
human between the measurement and the routing decision. That belongs in the
commit message of whichever change wires routing.

**This is stated as a property, not a reference count.** Shadow-mode recording of
routing decisions gives `self.router` a consumer without making the ladder
reachable — recording what the ladder *would* decide is not routing. The claim
to re-check before relying on this section is that the capability table reaches
the model actually spawned for a turn.

## 3. What a run costs

Measured on 2026-09-17 over 22 real cells: **6.0 minutes per
(model × task type)**.

```
10 models × 7 task types = 70 cells
70 × 6.0 min             ≈ 7.0 hours of measurement, sequential, at 3 repeats
```

That 7.0 hours is *measurement* time, not elapsed time. A sweep runs only while
the box is idle and stops the moment it is not (§8), so it spans several nights.
§10 covers how to state that honestly.

**"All models" needs defining, because there are two candidate lists and they
disagree.** `bin/wc-bench.py`'s `DEFAULT_MODELS` holds 10;
`delegation_capability` holds 8. The overlap is 7.

- `claude-fable-5` and `claude-haiku-4-5` are in the harness and have **no
  capability rows at all** — never measured on anything.
- `azure_ai/gpt-5.6-terra` has **6 measured rows** in the capability table and is
  **absent from `DEFAULT_MODELS`** — so a full sweep as currently configured
  would never re-measure a model that is a live rung on `long-context` and
  `multi-turn`.

**This design takes `DEFAULT_MODELS` as the source of truth** — it is the
harness's own list and the thing a run must be reproducible against — and treats
terra's absence from it as a **defect in that list to be fixed before the first
sweep**. A benchmark that silently skips a routing rung is worse than no
benchmark.

**Sequential is not a tuning choice.** These runs record `median_latency_s`, and
two benchmarks against one gateway measure their own contention rather than the
model. Every sweep on 2026-09-17 was sequential for this reason.

Per-cell time is not uniform — observed range was roughly 1 minute to the 900s
timeout cap — so any projection must come from measured cells, never from a
constant.

## 4. Storage

Two new tables, added through `db.py`'s existing migration dictionary, plus
columns on `delegation_capability`.

```
benchmark_runs
    id            TEXT PRIMARY KEY     -- e.g. "2026-09-18T02-00-00Z"
    started_at    TEXT NOT NULL
    expires_at    TEXT NOT NULL        -- started_at + 10 days; see 8.4
    finished_at   TEXT                 -- NULL while running or abandoned
    cooling_until TEXT                 -- finished_at + 2 days; NULL until done
    status        TEXT NOT NULL        -- running | done | expired
    models        TEXT NOT NULL        -- JSON list, frozen at start
    task_types    TEXT NOT NULL        -- JSON list, frozen at start
    repeats       INTEGER NOT NULL
    cells_total   INTEGER NOT NULL
    cells_dormant INTEGER NOT NULL DEFAULT 0   -- dormant now; rises mid-sweep, see 12.1
    trigger       TEXT NOT NULL DEFAULT 'scheduled'   -- scheduled | cli

benchmark_cells
    run_id            TEXT NOT NULL
    model             TEXT NOT NULL
    task_type         TEXT NOT NULL
    status            TEXT NOT NULL    -- ok | failed
    accuracy          REAL
    n                 INTEGER
    median_latency_s  REAL
    elapsed_s         REAL NOT NULL DEFAULT 0
    error             TEXT
    recorded_at       TEXT NOT NULL
    PRIMARY KEY (run_id, model, task_type)
```

`benchmark_cells` is the **measurement history**: what was attempted, what it
cost, and why it failed. It is not the resume ledger — §6 explains why the
capability table is.

`models` and `task_types` are **frozen into the run row at start**, not read
live per cell. A sweep spans days (§8), so a mid-sweep change to
`DEFAULT_MODELS` must not silently turn it into a matrix nobody requested.

`elapsed_s` is load-bearing rather than informational: it is the only input to
the projection in §10.

### 4.1 Columns on `delegation_capability`

```
    measured_at           TEXT       -- when this row's numbers were produced
    trigger               TEXT NOT NULL DEFAULT 'scheduled'  -- scheduled | manual | cli
    measured_under_load   INTEGER NOT NULL DEFAULT 0
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
    dormant               INTEGER NOT NULL DEFAULT 0
    reorder_flagged       INTEGER NOT NULL DEFAULT 0
    reorder_seen_at       TEXT       -- when the reordering was last observed
    reorder_acked_at      TEXT       -- when a human last acknowledged it
```

`measured_at` is the spine of this design. It drives resume (§6), it is what
makes a sweep's progress durable without a second ledger, and it is the only
thing that distinguishes a cell this sweep has done from one it has not.

`trigger` and `measured_under_load` carry **provenance**. A number is not
self-describing: 7.33s measured on an idle box at 03:00 and 7.33s measured while
three agent sessions were running mean different things, and only the second is
suspect.

**Provenance never gates a write.** The capability table is plain
last-write-wins: a scheduled measurement overwrites a hand-forced one and a
hand-forced one overwrites a scheduled one, with no special casing. The columns
exist so a reader can tell which kind of number they are looking at.

## 5. CLI surface

```
bin/wc-benchmark.py                       start a full-matrix sweep
bin/wc-benchmark.py --resume <run-id>     continue a sweep
bin/wc-benchmark.py --status [<run-id>]   progress and timing; runs nothing
bin/wc-benchmark.py --estimate            projected duration; runs nothing
bin/wc-benchmark.py --cell <model> <task-type>   measure one cell and write it
bin/wc-benchmark.py --scheduled           the timer's entry point; see §8
```

`--scheduled` is the only form the timer invokes. Every other form does what it
is told.

`--cell` is the CLI equivalent of §9's page control and shares its
implementation — the page must not be the only way to reach it, or the behaviour
becomes untestable without a browser.

`--models` and `--task-types` override the frozen defaults for a *new* sweep,
for the case where one model needs re-measuring on its own. The override is
recorded in the run row, so a partial sweep is never mistaken for a full one.

## 6. Writing, and resuming

**Each cell writes into `delegation_capability` the moment it completes**,
stamped with `measured_at`. There is no batch, no end-of-run transaction, and no
review step. A sweep that stops for any reason — the box got busy, the host
rebooted, the run expired — keeps every cell it had already measured.

**A failed cell writes no capability row, and always writes a
`benchmark_cells` row.** The two are separate acts and only the first is
skipped. The capability row is left alone because an old number is worth more
than no number, and §7 already refuses to record a timeout as a measurement.

The `benchmark_cells` row is not optional bookkeeping: it is one of the three
consecutive failures §12 counts, and dormancy is computed by reading those rows
per sweep. A failure that left no row would be invisible to the counter, and the
unreachable copilot cells would be retried forever. `benchmark_cells` holding
history rather than resume state changes what its `status` column is *used for*;
it does not change which rows exist.

**Resume reads the capability table, not a ledger.** For a sweep with
`started_at = T`, a cell is **done** if its `delegation_capability` row has
`measured_at >= T`, and **pending** otherwise. That is the whole rule.

Deriving resume from the written result rather than from a separate status
column means the two can never disagree. A ledger saying `ok` beside a
capability row that was never written is a class of bug this design does not
have.

Three consequences worth stating, because each is a behaviour someone will
otherwise find surprising:

- A cell measured by a **forced re-measure** (§9) after the sweep started counts
  as done for that sweep. This is correct: the number is fresh, and measuring it
  twice in one sweep buys nothing.
- A **failed** cell has no new `measured_at`, so it stays pending and the sweep
  retries it on a later night — until dormancy (§12) takes it out.
- A **dormant** cell is neither done nor pending; it is skipped, and does not
  hold a sweep open.

A sweep reaches `done` when no cell is pending.

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

## 8. The schedule

### 8.1 Two outcomes, nightly

A systemd timer follows the existing `systemd/webconsole-health.timer` pattern
rather than introducing a scheduler. It fires **nightly at 02:00** and runs
`bin/wc-benchmark.py --scheduled`, which decides between exactly two outcomes:

1. **A sweep is current** — advance it. If it has pending cells and the box is
   idle, measure. If it is within its post-completion cooling period (§8.2),
   there is nothing to advance and the job exits.
2. **No sweep is current** — start a new one.

A sweep is *current* from the moment it starts until two days after it finishes.
Nothing else is a case: there is no "wait for the interval" branch, because the
interval is a property of the sweep rather than a state of the scheduler.

**"Current" is a stored fact, not a computed one.** `expires_at` is written when
the sweep starts and `cooling_until` when it reaches `done` (§4). The scheduler
asks for the one current run — a row with `status = 'running'`, or
`status = 'done'` with `cooling_until` in the future — and acts on what it finds.
It does not re-derive eligibility from timestamps scattered across rows each
time it wakes.

That matters for testing more than for runtime: a stored window can be set to
any value in a fixture, so "a sweep inside its cooling period" and "a sweep whose
cooling has elapsed" are both one `UPDATE` away. A computed window would make
every scheduler test a clock test.

### 8.2 Cadence

**A new sweep begins two days after the previous one reached `done`.** The table
is continuously re-measured, with a two-day gap so the box is not permanently
benchmarking.

An **expired** sweep (§8.4) does not serve its cooling period. The next nightly
run starts a fresh sweep immediately, because an expired sweep is by definition
one that failed to keep the table current.

### 8.3 The nightly window

Measurement starts at 02:00 and **ends when the box becomes busy**. There is no
fixed end time.

Before each cell, the job checks whether the box is in use. Busy if either:

- a turn is in flight (the runner's concurrency semaphore is held), or
- any message was written in the last **10 minutes**.

Busy means **stop for the night**: finish nothing further, leave the sweep
current, and let the next night's timer continue it. It never means "measure
anyway and note it" — a contended measurement of `median_latency_s` is not a
worse number, it is a number about the wrong thing, and §1's whole complaint is
numbers that do not mean what the column says.

**Voice sessions are not part of the busy check.** `routes/voice.py` is
CLAUDE.md §0's documented exception and speaks to an OpenAI-compatible endpoint
directly, so a voice conversation does not contend with the harness's CLI
transport.

The 10-minute idle margin exists because a turn that has just finished leaves
the gateway still draining. It is a constant, chosen rather than measured, and
labelled here as such.

**This check is why the forced cell in §9 needs no lock.** Pressing the button
makes the box busy by definition, the nightly job sees it and stops, and the
forced cell measures alone. Sequential measurement (§3) holds without two
subsystems negotiating.

### 8.4 Expiry

**A sweep expires ten days after it starts.** On expiry it is marked `expired`,
abandoned, and the next nightly run begins a fresh sweep.

Every cell an expired sweep wrote stays in the capability table. Expiry
abandons the *sweep*, not its results — the numbers are real measurements and
deleting them would trade a partial snapshot for no snapshot.

Ten days bounds how far apart one sweep's measurements can be taken. A sweep
that cannot finish in ten nights is one the box is too busy to support, and
letting it crawl on for a month would reproduce §1's second failure — one column
holding numbers from widely separated days — inside a single run.

## 9. Forcing one cell from the Delegation page

Each cell of the capability matrix gains a **Re-measure** control. Pressing it
measures that one `(model, task_type)` pair at the standard repeat count and
writes the result.

- Runs **immediately** (~6.0 min at 3 repeats, §3), streaming progress into the
  row. It is not queued for the night; the point of the control is an answer
  while you are looking at the page.
- The result is stamped `trigger='manual'` and **`measured_under_load=1`**,
  because pressing it means someone is using the box. A later nightly
  measurement overwrites it with a cleaner number and needs no special rule to
  do so (§4.1).
- **Clears dormancy** (§12) for that cell, resetting `consecutive_failures` to 0
  and `dormant` to 0, whether the forced measurement succeeds or fails.
- **Refused only when the nightly job is mid-measurement on that same cell** —
  the narrow case where two subprocesses would measure one pair at once. Any
  other cell proceeds; the nightly job stops itself via §8.3.
- Requires an authenticated session, like every other Delegation control. No new
  permission concept.

**Endpoints**, following the existing `routes/delegation.py` conventions:

```
POST /api/delegation/benchmark/cell   {model, task_type}  -> {cell_run_id}
GET  /api/delegation/benchmark/cell/<cell_run_id>         -> {status, elapsed_s, result}
POST /api/delegation/capability/ack   {model, task_type}  -> acknowledge a reorder (§13)
```

The page polls the second while a cell is running. Polling is chosen over a
stream because one cell finishing in minutes does not justify a second transport
in the console, and the existing Delegation panel already refreshes on a timer.

`bin/wc-benchmark.py --cell` (§5) shares this implementation exactly, so the
behaviour is testable without a browser.

## 10. Timing, and the projection

Per cell, on stdout:

```
[14/63] azure_ai/gpt-5.6-luna / planning   done 5m42s   measured 1h18m
        remaining ≈5h36m measurement (from 14 cells)   (7 dormant of 70)
```

The denominator is the attemptable count and the dormant count rides alongside
it, per §12.1.

The remaining figure is computed from the cells **this sweep** has already
measured, and states how many it is based on. A projection from two cells is not
a projection, and saying so is cheaper than being quietly wrong.

**Every projection is measurement time, and must never be presented as elapsed
time.** A sweep runs only in idle hours and stops when the box is used (§8.3),
so 5h36m of remaining measurement is not five and a half hours away.

`--estimate` reports a full sweep's projected duration **in nights**, using two
inputs from history: the median cell time of the last completed sweep, and the
median measurement hours per night actually achieved across recent nights.

```
estimate: 70 cells ≈ 7.0h measurement
          ≈ 3 nights at 2.4h/night observed over the last 6 nights
```

With no nightly history, it reports measurement time only and says what it is
excluding:

```
estimate: 70 cells ≈ 7.0h measurement
          nights unknown — no idle-time history yet; excludes all idle time
```

With no prior sweep at all it prints `no prior sweep — no estimate` rather than
seeding itself with a constant. The 2026-09-17 figure of 6.0 min/cell came from
a different model mix and is history, not a default.

`--status` reports a current sweep's cells done, pending and dormant, its
`started_at`, and its expiry date.

## 11. The writer, and the one column it must protect

Every path that writes a capability row — the nightly job, `--cell`, and the
page control — goes through **one writer function**. There is no second write
path.

**For `task_type = 'voice'`, the writer writes `accuracy` and `n` and leaves
`median_latency_s` untouched**, logging the reason once per sweep.

The harness measures over the **CLI transport**. `routes/voice.py` speaks to an
OpenAI-compatible endpoint directly, and voice latencies measured over the CLI
were 6.6–9.6s against the ~2.0s the voice path records. Writing that figure
would corrupt the column spec v3 §5.1's deadline derivation divides by.

**Nothing is reviewed before a write (§6), so this exclusion is the only
safeguard standing between a mismeasured transport and every voice deadline in
the system.** It has its own test (§14), asserted through every entry point
rather than one.

## 12. Dormancy

**A cell that fails on three consecutive sweeps goes dormant and is no longer
attempted.**

`consecutive_failures` increments when a sweep records a `failed` cell and
resets to 0 on any successful measurement. At 3 it sets `dormant = 1`.

Dormant cells are **skipped** by nightly sweeps: not measured, not counted as
pending, and not able to hold a sweep open or push it toward expiry.

This exists because a known-unmeasurable cell is pure cost.
`azure_ai/gpt-5.4-mini-copilot` cannot be reached by this harness at all
(CLAUDE.md §0.1) and would otherwise burn 7 failing cells on every sweep —
roughly 10% of the matrix spent re-confirming a known failure, at up to the 900s
timeout cap each.

Dormant cells **stay visible** on the Delegation page, marked dormant, with the
last error. A silently skipped cell is indistinguishable from a cell nobody
thought to measure, and that is exactly the §1 failure this design exists to
stop.

**A manual re-measure clears dormancy** (§9). That is the only way back: it takes
a human deciding the underlying problem is fixed, which is the judgment the
counter cannot make.

### 12.1 Dormant cells and progress reporting

**`cells_total` counts every cell in the frozen matrix, including dormant ones.**
It is the size of the matrix the sweep was defined over, and it does not change
while the sweep runs.

Progress is reported against the **attemptable** count, with the dormant count
named alongside rather than folded away:

```
[14/63] azure_ai/gpt-5.6-luna / planning   done 5m42s   (7 dormant of 70)
```

`cells_dormant` is recorded on the run row at start (§4) so a finished sweep
still says how much of the matrix it was never going to attempt. Without it, a
sweep that completed 63 of 70 cells and one that skipped 7 unmeasurable ones are
the same two numbers.

Progress against 70 would stall at 90% forever and read as a stuck sweep;
progress against 63 with no dormant count visible would read as a full matrix
and quietly hide that a tenth of it is unmeasured. Both failures are the §1
failure — a number that does not mean what it appears to mean — so the report
carries both figures.

A cell that goes dormant **mid-sweep** moves from attemptable to dormant at that
moment: the denominator drops and `cells_dormant` rises. `cells_total` does not
move.

### 12.2 A sweep whose remainder is entirely dormant

**It completes immediately.** §6's rule is that a sweep reaches `done` when no
cell is pending, and a dormant cell is not pending. So a sweep whose every
remaining cell has gone dormant has nothing left to do and finishes on the spot,
setting `finished_at` and `cooling_until` like any other completed sweep.

It does not sit waiting for something to change, and it does not run to its
ten-day expiry. Both alternatives were considered and are worse:

- **Sitting open** would block the next sweep for ten days, and the next sweep is
  the only scheduled event that would retry anything. A cell cannot leave
  dormancy on its own — only a manual re-measure clears it (§9) — so waiting
  cannot improve the outcome.
- **Expiring** would mark a sweep `expired` that did everything asked of it, and
  §8.4 exists for sweeps the box was too busy to support, not for sweeps that
  finished.

The degenerate case is a matrix where **every** cell is dormant. That sweep
starts and completes in the same run, writes nothing, and enters cooling. This is
correct rather than a defect: the system has nothing it is permitted to measure,
and it says so through `cells_dormant == cells_total` on the run row and seven
dormant markers on the page. It stays in that state until a human clears a
dormancy, which is exactly the intended escalation.

## 13. Reordering highlights

When a new measurement would change the **relative order of two rungs** in a
task type's ladder, the affected capability row is marked: `reorder_flagged = 1`
and `reorder_seen_at` set. The Delegation page renders marked cells highlighted.

A number changing is routine. A reordering changes an escalation order, and it
is the only event worth an operator's attention.

**The highlight is never gating.** The measurement is already written (§6); the
mark reports what happened, it does not hold it back.

**The highlight clears only on explicit acknowledgement** — the `ack` endpoint
in §9, from the page. It does not clear on the next sweep, on a page reload, or
on time passing. An operator who has not looked has not acknowledged, and a mark
that ages out silently is worse than no mark.

**A later measurement that still reorders resets the acknowledgement.**
`reorder_seen_at` is updated and `reorder_acked_at` cleared, so the cell
highlights again. Acknowledging a reordering acknowledges *that* reordering, not
the cell forever.

## 14. Testing

Following the repo's `tests/test_qa_*.py` convention. Each case names the
direction that makes it able to fail.

- **The cell loop against a fake subprocess**: a cell that succeeds, one that
  fails, one that times out — the sweep continues past all three and records each
  correctly.
- **Per-cell writes are durable**: a sweep stopped after three of ten cells
  leaves exactly those three rows written, with `measured_at` set. The failing
  direction is a batched writer, which would leave zero.
- **Resume from `measured_at`**: a cell with `measured_at >= started_at` is not
  re-measured, and one with an older timestamp is. Both directions — asserting
  only the first would pass for an implementation that re-measures nothing.
- **A failed cell stays pending** across nights and does not overwrite the
  previous capability row.
- **A forced cell counts as done** for a sweep in progress.
- **Busy detection**, both directions: a busy box stops the night without
  recording a measurement, and an idle box does not stop. A stop-only test would
  pass for a detector that always reports busy.
- **Voice is not busy**: an active voice session alone does not stop the night.
- **Two scheduler outcomes**: a current sweep is advanced, and no current sweep
  starts a new one. A sweep inside its two-day cooling period is current and
  produces no measurement.
- **Cadence**: a sweep that finished less than two days ago does not trigger a
  new one; one that finished more than two days ago does.
- **Expiry**: a sweep ten days old is marked `expired`, its written cells remain
  in the capability table, and the next scheduled run starts a fresh sweep
  without waiting two days.
- **Dormancy**, all three transitions: three consecutive failures set `dormant`,
  a success before the third resets the counter, and a dormant cell is skipped by
  a sweep without being counted pending.
- **A failed cell writes a `benchmark_cells` row** and no capability row. The
  failing direction is an implementation that skips both writes, which would make
  dormancy uncountable while every other test still passed.
- **A manual re-measure clears dormancy**, including when the forced measurement
  itself fails.
- **Progress arithmetic (§12.1)**: with 7 dormant cells of 70, `cells_total`
  stays 70, progress counts against 63, and the run row carries
  `cells_dormant = 7` after completion. A cell going dormant mid-sweep drops the
  denominator and leaves `cells_total` alone.
- **A sweep whose remaining cells are all dormant completes immediately**, with
  `finished_at` and `cooling_until` set — it neither waits nor expires. The
  all-dormant matrix completes in one run and writes no capability rows.
- **The cooling window is read, not recomputed**: a run row with `cooling_until`
  in the future is current and blocks a new sweep; the same row with
  `cooling_until` in the past is not, and a new sweep starts. Both set by fixture
  rather than by waiting on a clock.
- **The voice latency exclusion**, asserted through **every** entry point — the
  nightly job, `--cell`, and the page endpoint. Each must write `accuracy` and
  `n` and leave `median_latency_s` untouched. Asserting one path only would pass
  while another corrupts the column.
- **Reorder highlight**: a measurement that reorders two rungs marks the row; one
  that changes a number without reordering does not.
- **Acknowledgement**: an acknowledged mark stays clear across a sweep that
  changes nothing, and a later reordering clears the acknowledgement and
  re-marks the row.
- **Provenance is not a gate**: a scheduled measurement overwrites a
  `measured_under_load=1` row and a manual one overwrites a scheduled row. Both
  directions, so nobody later "improves" the writer into refusing one.
- **Projection**: an estimate from one cell says it is from one cell, and an
  estimate with no nightly history states that it excludes idle time.
- **The frozen matrix**: changing `DEFAULT_MODELS` mid-sweep does not change the
  cells a resumed sweep executes.

## 15. What this does not do

- **It does not replace `bin/wc-bench.py`.** That harness keeps its arguments,
  verifiers and repeat logic; this orchestrates it.
- **It does not measure what the CLI cannot reach.**
  `azure_ai/gpt-5.4-mini-copilot` — the model that actually serves voice — is
  unmeasurable by this harness. §12 stops it costing a sweep more than three
  attempts.
- **It does not update spec §2.6's markdown table.** That mirror stays manual,
  and `tests/test_qa_delegation_shipped_state.py` already fails when the table
  and the seed rows disagree.
- **It does not flip anything operational.**
- **It does not add a run-history page.** §9 adds the per-cell control only.

## 16. Open items

- **A second transport for the harness.** Until it exists, `voice` accuracy is
  measured over a transport voice does not use, and the copilot model cannot be
  measured at all. Both are recorded in §2.6 behind a `§` marker.
- **`DEFAULT_MODELS` is missing `azure_ai/gpt-5.6-terra`** (§3). This must be
  fixed before the first sweep, or every sweep silently skips a live routing
  rung. It is the one prerequisite outside this design's own code.
