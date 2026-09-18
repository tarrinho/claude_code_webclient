# Benchmark — design

**Status:** design approved 2026-09-18, extended and re-approved 2026-09-18, not
yet implemented.
**Scope:** a `benchmark` functionality that measures every model against every
task type, records how long it takes, writes its results into
`delegation_capability`, runs itself on a monthly schedule in a quiet window,
and can be forced on a single cell from the Delegation page.

**This document is the single source of truth for benchmarking.** Spec v3
§10.2 ("Scheduled re-benchmark") is the older, shorter statement of the same
subsystem; where the two differ, this one governs and §10.2 is amended to match.
The amendments are named explicitly in §12.

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
| Where it runs | **CLI, a scheduled job, and one interactive control on the Delegation page** | A full job subsystem is not needed: the schedule is a systemd timer and the page control drives one cell. See §13 and §15. |
| Results flow | **Written automatically when a unit completes; rung flips flagged, not gated** | Holding results for explicit promotion keeps the table clean but leaves it stale between human visits, and the flag already makes a reordering visible. Reverses this document's original decision — see §2.2. |
| Run scope | **Full matrix every time** for a sweep; **one cell** for a forced re-measure | Incremental sweeps mix measurement days, which is the defect in §1. A sweep is a self-consistent snapshot; a forced cell is explicitly not one, and is marked as such (§14). |
| Cadence | **Monthly**, attempted nightly | Quarterly (spec v3 §10.2) is slower than this gateway changes — models were renamed and added within days on 2026-09-03, and a stale row routes to a model that may no longer serve it. See §13. |
| Contention | **Quiet window; pause rather than measure a busy box** | Measuring under load records contention rather than the model, and `median_latency_s` is the column every deadline and the latency ceiling derive from. See §13.2. |
| Orchestrator shape | **Thin loop, one subprocess per cell** | An in-process loop loses crash containment. See §7. |

## 2.1 What the capability table currently controls

An earlier draft of this document asserted that promoting a measurement changes
which model answers real work, on the grounds that `coding` was flipped
operational on 2026-09-18. **That was wrong, and the error is recorded here
rather than quietly deleted, because it is the kind of claim this design is
otherwise built on.**

The delegation ladder is not reachable from a conversation turn:

- `ModelRouter.assign_model` has no production caller. `self.router` is
  constructed at `orchestrator.py:525`.
- `app.state.capability_table` is written at `app.py:629` by the startup
  validation and is read nowhere.
- An ordinary turn picks its model in `runner.get_default_model` — the chat's
  own model, then the chat's backend default, then the `default_model` setting,
  then `config.MODEL_NAME`. The capability table is not consulted.

So release 0.19.0's stated scope — machinery complete, nothing routes — is still
literally true, and flipping a task type operational changed nothing about which
model serves a turn.

**What follows for this design, and what does not.** The decision to store
results separately and promote them explicitly **stands unchanged**: a
half-finished 7-hour sweep left in the table is a state nobody chose, whether or
not anything reads it, and the table is the artefact routing will read the
moment it is wired. What does *not* follow is urgency. A promote today is a
change to a data table, not to production behaviour, and this document must not
be cited as evidence that it is more than that.

**This is written as a property, not as a line count, so it does not go stale.**
Shadow-mode recording of routing decisions is in progress in a parallel session
and will give `self.router` a consumer. Recording what the ladder *would* decide
is not routing, so the statement above survives it. The claim to re-check before
relying on this section is the specific one that the capability table reaches
the model actually spawned for a turn — not whether any particular symbol still
has zero references.

## 2.2 The reversed decision, and why it is recorded rather than deleted

This document originally decided that results would be **stored separately and
promoted explicitly**, and argued it at length: a 7-hour sweep writing straight
into `delegation_capability` would re-order ladders progressively, and a
half-finished sweep would leave the table in a state nobody chose.

**That decision was overturned by the operator on 2026-09-18.** Results are now
written automatically. The reasoning is kept here because a reader who finds
only the new decision cannot tell whether the old objection was answered or
merely forgotten. It was answered, in two parts:

- **The progressive-reordering objection is answered by §14's write rule**, not
  by human review. A sweep writes nothing until it completes, so the table never
  holds a mixture of old and new rows.
- **The half-finished-sweep objection is answered by the same rule.** An
  interrupted sweep writes nothing at all.

What the reversal genuinely costs is the human check between measurement and
routing. Spec v3 §10.2 accepted that cost deliberately — "the flag exists to
make a silent reordering visible, **not to gate it**" — and this document now
agrees with it.

**The risk is low today and will not stay low.** Per §2.1 the capability table
reaches no live turn: `ModelRouter.assign_model` has no production caller. So an
automatic write currently changes a data table and nothing else. Whoever wires
routing to the ladder is also arming this: from that commit onward, an unattended
overnight job can change which model serves real work. That is the trade the
operator accepted, and it belongs in the commit message of whichever change
wires routing.

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

Two further columns carry **provenance**, and they exist on both
`benchmark_cells` and `delegation_capability`. A number is not self-describing:
7.33s measured on an idle box at 03:00 and 7.33s measured while three agent
sessions were running mean different things, and only the second is suspect.

```
    trigger               TEXT NOT NULL DEFAULT 'scheduled'
                              -- scheduled | manual | cli
    measured_under_load   INTEGER NOT NULL DEFAULT 0
```

`benchmark_runs` gains three columns for the schedule and the flag:

```
    trigger         TEXT NOT NULL DEFAULT 'scheduled'
    paused_at       TEXT              -- set when the box went busy, cleared on resume
    rung_flips      TEXT              -- JSON list, written at completion; see §16
```

**Provenance never gates a write.** `delegation_capability` is plain
last-write-wins: a later scheduled measurement overwrites a hand-forced one with
no special casing, and a hand-forced one overwrites a scheduled one the same
way. The columns exist so a reader can tell which kind of number they are
looking at, and so §16's flip report can say "this reordering came from a cell
measured under load" — not so the writer can argue with itself.

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
bin/wc-benchmark.py --cell <model> <task-type>   measure one cell and write it
bin/wc-benchmark.py --scheduled           the timer's entry point; see §13
bin/wc-benchmark.py --promote <run-id>    diff an old run; writes nothing
bin/wc-benchmark.py --promote <run-id> --apply
```

`--scheduled` is the only form the timer invokes, and it is the only form that
may decide to do nothing (§13). Every other form does what it is told.

`--cell` is the CLI equivalent of §15's page control and shares its
implementation — the page must not be the only way to reach it, or the behaviour
becomes untestable without a browser.

**`--promote` survives only as an escape hatch.** With §14's write rule it is no
longer part of the normal path: a completed run has already written. It remains
for inspecting or re-applying an older run's cells by hand, and `--apply` still
refuses while a run's status is `running`.

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

## 8. Promote — retained as an escape hatch

Since the 2026-09-18 reversal (§2.2), a completed run writes its own results and
there is nothing left to promote in the normal course. This command stays for
one job: inspecting or re-applying an **older** run by hand — after a bad sweep,
or to compare two runs.

`--promote <run-id>` prints a per-cell diff and **writes nothing**:

- rows that would be **added**,
- rows whose values would **change**, old → new,
- cells the run **failed** to measure, which are left alone.

`--apply` writes through the same writer §14 defines — not a second write path.
It refuses while the run's status is `running`, and applies only cells with
status `ok`. Because it shares the writer, the voice-latency exclusion (§14.1)
applies to it automatically; that exclusion used to live here and no longer
does.

`--apply` also prints the **resulting ladder changes**, not only the cell diff.
A diff of five numeric cells does not show that a rung moved; a ladder
before/after does. Per §2.1 this is not yet a change to production behaviour —
the ladder is unreachable from a turn — so the ladder print is here to make the
change legible, not to gate a live incident.

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
- **Promote, with no model calls at all** — the diff is pure.
- **The voice latency exclusion**, asserted directly and through **both** entry
  points: a voice cell written by a completed sweep and one written by
  `--promote --apply` must each write `accuracy` and `n` and leave
  `median_latency_s` untouched. Asserting one path only would pass while the
  other corrupts the column.
- **Projection**: an ETA from one cell must say it is from one cell.

Added by the 2026-09-18 extension:

- **The write rule (§14)**, in its failing direction: a sweep interrupted after
  some cells succeeded must leave `delegation_capability` **byte-identical**.
  Asserting that a completed sweep writes is the easy half; asserting that an
  incomplete one writes nothing is the half that catches a per-cell write
  sneaking back in.
- **Busy detection (§13.2)**, both directions: a busy box pauses the sweep
  without recording a measurement, and a quiet box does not pause. A pause test
  that never exercises the quiet case would also pass for a detector that always
  reports busy.
- **Resume across windows**: a sweep paused on night one and resumed on night
  two re-runs only `pending` cells and writes once, at completion.
- **The monthly gate (§13.1)**: `--scheduled` starts a new sweep when the last
  completed run is older than the interval, resumes when one is in progress, and
  **exits without running** otherwise. All three branches asserted — the third is
  the one that stops a nightly timer becoming a nightly sweep.
- **Forced cell (§15)**: writes immediately, stamps `trigger='manual'` and
  `measured_under_load=1`, and is refused only when a sweep is mid-measurement on
  that same cell. The refusal case must be asserted against the *same* cell and
  the permitted case against a *different* cell; a test using one cell for both
  cannot tell the rule from "always refuse while a sweep runs".
- **Rung-flip flag (§16)**: a run that reorders two rungs raises the flag; a run
  that changes numbers without reordering does not. Both directions, per spec v3
  §11's own verification row for this behaviour.
- **Provenance is not a gate**: a scheduled measurement overwrites a
  `measured_under_load=1` row, and a manual one overwrites a scheduled row.
  Last-write-wins, asserted in both directions, so nobody later "improves" the
  writer into refusing one of them.

## 11. Open items

- **A second transport for the harness.** Until it exists, `voice` accuracy is
  measured over a transport voice does not use, and the copilot model cannot be
  measured at all. Both are recorded in §2.6 behind a `§` marker.
- **A run history page.** Still deferred. It reads `benchmark_runs` and
  `benchmark_cells`; no schema change should be needed for it, and if one is,
  this design got the storage wrong. §15 adds only the per-cell control, not a
  history view.
- **`DEFAULT_MODELS` is missing `azure_ai/gpt-5.6-terra`** (§3). This must be
  fixed before the first sweep, or every sweep silently skips a live routing
  rung. It is the one prerequisite outside this design's own code.

## 12. What this amends in spec v3

Spec v3 §10.2 is the older statement of this subsystem. Three of its claims are
superseded here and must be edited there, so the two documents do not disagree:

| v3 §10.2 says | This document says | Why |
|---|---|---|
| re-benchmarked **quarterly** | **monthly**, attempted nightly | the gateway changes faster than a quarter; models were renamed and added within days on 2026-09-03 |
| the run writes results into the §2.6 table | unchanged — but **only on completion**, never per cell | §14; it is what makes automatic writing safe |
| (silent on contention) | sweeps pause rather than measure a busy box | §13.2; `median_latency_s` is the column every deadline derives from |

The rung-flip flag is **not** amended: v3's "the flag exists to make a silent
reordering visible, not to gate it" is adopted verbatim (§16).

## 13. The schedule

### 13.1 A nightly timer doing monthly work

A systemd timer follows the existing `systemd/webconsole-health.timer` pattern
rather than introducing a scheduler. It fires **nightly at 02:00** and runs
`bin/wc-benchmark.py --scheduled`, which decides among exactly three outcomes:

1. **A run is in progress** (status `running`, cells still `pending`) — resume
   it.
2. **No run in progress, and the last completed run finished more than
   `BENCHMARK_INTERVAL_DAYS` ago** (30) — start a new full sweep.
3. **Otherwise** — exit, having done nothing.

Nightly firing with a monthly gate is deliberate, and it is what makes the pause
in §13.2 affordable. **A sweep does not fit in one night and is not expected
to:** ~7.0 hours of measurement (§3) against a window of at most four (02:00 to
06:00, §13.2), so a clean sweep takes **at least two nights**, and more if the
box is used during them. A box that gets busy at 04:00 would otherwise strand
the sweep for a month; here it continues the next night.

This is the reason the model list is frozen into the run row at start (§4).
Spanning nights is the normal case, not an exception, so a sweep must stay one
self-consistent snapshot across days — which it cannot do if it reads
`DEFAULT_MODELS` live.

It also bounds how stale a monthly table gets: 30 days between sweeps plus two
to four nights to complete one. Anything more urgent than that is what §15's
forced cell is for.

The 02:00 start and the 30-day interval are constants in the spec, not settings.
A cadence knob was considered and rejected: it is one more control to keep
tested, and changing a constant is a commit either way.

### 13.2 What "busy" means, and what pausing does

Before **each cell**, the scheduled job checks whether the box is in use. Busy
if any of:

- a turn is in flight (the runner's concurrency semaphore is held),
- a voice session is active,
- any message was written in the last **10 minutes**.

Busy means **pause**: record `paused_at` on the run, measure nothing, and
re-check every **60 seconds**. It never means "measure anyway and note it" — a
contended measurement of `median_latency_s` is not a worse number, it is a
number about the wrong thing, and §1's whole complaint is numbers that do not
mean what the column says.

**The night's attempt ends at 06:00** whether or not the sweep finished. The run
keeps status `running` with its remaining cells `pending`, writes nothing (§14),
and the next night's timer resumes it. Without a stop time a sweep that started
at 02:00 would still be measuring during the morning's first turns, which is the
contention this section exists to avoid — it would simply arrive by running long
instead of by starting busy.

So a night contributes between zero and four hours of measurement, and a sweep
takes as many nights as it needs. `02:00`, `06:00`, the 60-second re-check and
the 10-minute idle margin are constants in the spec.

The 10-minute idle margin exists because a turn that has just finished leaves
the gateway still draining. It is a constant, chosen rather than measured, and
labelled here as such.

**This check is why the forced cell in §15 needs no lock.** Pressing the button
makes the box busy by definition, the sweep's own detector sees it and pauses,
and the forced cell measures alone. Sequential measurement (§3) holds without
two subsystems negotiating.

## 14. When results are written

**The atomic unit is the completed unit.**

- A **sweep** writes every one of its `ok` cells into `delegation_capability`
  when, and only when, it reaches status `done`. One transaction.
- A **forced cell** (§15) writes when that cell finishes. One cell is already
  atomic.

An interrupted, paused, or failed sweep writes **nothing**. This is what answers
the objection §2.2 records: the live table never holds a mixture of rows from
two different sweeps, so automatic writing cannot produce the progressive
reordering that explicit promotion was invented to prevent.

Cells with status `failed` are never written. A failure leaves the previous
measurement in place — an old number is worth more than no number, and §7
already refuses to record a timeout as a measurement.

### 14.1 The voice latency exclusion, now in the writer

This rule used to live in §8 and applied at promote time. With promotion no
longer on the normal path it **moves into the writer**, and every path that
writes a capability row goes through that one function.

The harness measures over the **CLI transport**. `routes/voice.py` is CLAUDE.md
§0's documented exception and speaks to an OpenAI-compatible endpoint directly.
Voice latencies measured over the CLI were 6.6–9.6s against the ~2.0s the voice
path records.

**For `task_type = 'voice'`, the writer writes `accuracy` and `n` and leaves
`median_latency_s` untouched**, logging the reason once per run.

If this does not move with the write, automatic writing silently corrupts the
exact column spec v3 §5.1's deadline derivation divides by — and unlike the old
design, no human sees a diff first. It is the single most load-bearing line in
this extension.

## 15. Forcing one cell from the Delegation page

Each cell of the capability matrix gains a **Re-measure** control. Pressing it
measures that one `(model, task_type)` pair at the standard repeat count and
writes the result.

**Behaviour:**

- Runs **immediately** (~6.0 min at 3 repeats, §3), streaming progress into the
  row. It is not queued for the quiet window; the point of the control is an
  answer while you are looking at the page.
- The result is stamped `trigger='manual'` and **`measured_under_load=1`**,
  because pressing it means someone is using the box. A later scheduled sweep
  overwrites it with a cleaner number and needs no special rule to do so (§4).
- **Refused only when a sweep is mid-measurement on that same cell** — the
  narrow case where two subprocesses would measure one pair at once. Any other
  cell proceeds; the sweep pauses itself via §13.2.
- Requires an authenticated session, like every other Delegation control. No new
  permission concept.

**Endpoints**, following the existing `routes/delegation.py` conventions:

```
POST /api/delegation/benchmark/cell   {model, task_type}  -> {cell_run_id}
GET  /api/delegation/benchmark/cell/<cell_run_id>         -> {status, elapsed_s, result}
```

The page polls the second while a cell is running. Polling is chosen over a
stream because one cell finishing in minutes does not justify a second transport
in the console, and the existing Delegation panel already refreshes on a timer.

`bin/wc-benchmark.py --cell` (§5) shares this implementation exactly, so the
behaviour is testable without a browser.

**Implementation note, not a design decision:** `web/assets/delegation.js` was
being edited by a parallel session on 2026-09-18 for an unrelated hide/unhide
control. Whoever implements §15 coordinates on that file rather than assuming it
is free.

## 16. The rung-flip flag

When a sweep completes and its writes land, the job regenerates the ladders and
compares them against the ladders implied by the previous values. For every task
type where **two rungs changed relative order**, it records an entry in the run's
`rung_flips` column: the task type, the two models, and their old and new
positions.

Flips are **surfaced, never gating** — spec v3 §10.2's wording is adopted as-is.
They appear in the Delegation page status band and in `--status` output.

A number changing is routine and is not a flip. A reordering is the thing that
changes an escalation order, and it is the only event worth an operator's
attention. Where a flip came from a cell with `measured_under_load=1`, the entry
says so, because that is the flip most likely to be an artefact.
