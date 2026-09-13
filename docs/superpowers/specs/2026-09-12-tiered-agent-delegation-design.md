# Tiered agent delegation: routing a goal across models by capability, cost and host load

**Date:** 2026-09-12
**Status:** design, complete and implementable; not implemented. Every figure it
routes on is now measured; no outstanding measurement blocks starting.
**Amended:** 2026-09-13 — three changes, all in response to review by Pedro.
(1) §3.2 added: write-capable tasks stay local, reconciled in from
`AGENT-MODELS-DECISION.md`, which is deleted in the same commit so this is the
single spec for model and agent routing. (2) §2 rewritten: the free model is
now rung 0 for every task type with no capacity gate, since the gate the
previous amendment left in place could never open (§2.1). (3) §1.4 records the
`109.1% CPU` misreading that motivated that gate, so it is not rebuilt.

**Amended again:** 2026-09-13, after a full re-check of every figure and
citation against the code and the benchmark data. One decision-changing finding:
the 45s deadline and the ≥70% target are inconsistent with each other on the
only data that exists — 45s yields **65.2%** free-tier completion on coding, the
type this model is measured best at (§2, "What the deadline actually costs").
The deadline is raised to **90s**. Two smaller fixes: §1.1 now carries the
rung-0 model's own row rather than omitting it, and two stale code references
are corrected.

**Measured and re-tuned:** 2026-09-13. The one outstanding measurement was taken:
`vllm/Qwen3.6-35B-A3B-NVFP4` ran all six task types for the first time
(`bench/qwen35b_alltypes_20260913b.json`, 78 responses). Rung 0 turns out **not**
to be uniformly strong — 100% on long-context down to 67% on planning and
reasoning — and the 96% the policy had been built on was a coding-only figure.
`TIER0_DEADLINE` becomes **per task type** (45s to 240s) rather than one global
90s: that serves every correct answer the model produced (80.8% vs 78.2%) while
cutting the worst observed reasoning runaway from 1903s to a bounded 240s. §1.1,
§2 and §4 carry the new numbers.

**Completed:** 2026-09-13. Two gaps that would have stopped an implementer are
closed. §2.2 writes out all six escalation ladders — `MAX_ATTEMPTS = 3` requires
three named rungs per task type, and the third was not derivable from the entry
rung. §3.1's table gains the `mutates` column that §3.2 requires; without it
every pattern would have fallen to §3.2's `True` default and no task could ever
have been placed on a transport, which is the same shape of bug as the capacity
gate that could never open. §4 and §5 follow both through.

A goal is decomposed recursively into sub-agents, and every node is routed to
the cheapest model measured capable of its task type, placed on a host with the
memory to run it, and escalated up a ladder only when it fails.

The policy is derived from measurements already in this repository, not from
published model marketing. Every table below is reproducible from the files
named beside it.

---

## 1. The measurements

### 1.1 Capability and latency by task type

From `bench/judge_delegation_deterministic_20260904.json` — 445 graded
responses, exec-verified correctness, collected 2026-09-04. Cells are
`accuracy / median latency`.

| model | coding | comprehension | long-context | multi-turn | planning | reasoning |
|---|---|---|---|---|---|---|
| `azure_ai/gpt-5.4-mini` | 100% / 3s | **25%** / 8s | 100% / 3s | 100% / 6s | 100% / 22s | 86% / 8s |
| `azure_ai/gpt-5.6-luna` | 100% / 4s | **25%** / 10s | 100% / 3s | 100% / 6s | 100% / 23s | 50% / 8s |
| `claude-sonnet-5` | 100% / 4s | 100% / 7s | 100% / 8s | 100% / 7s | 100% / 18s | 75% / 9s |
| `claude-opus-5` | 100% / 6s | 50% / 22s | 100% / 8s | 100% / 10s | 100% / 51s | **100% / 27s** |
| `claude-haiku-4-5` | 100% / 7s | 100% / 21s | 100% / 4s | 100% / 13s | 100% / 16s | 75% / 85s |
| `vllm/Qwen3.6-35B-A3B-NVFP4` † | 88% / 12s | 75% / 18s | 100% / 17s | 75% / 17s | 67% / 70s | 67% / 123s |
| `vllm/Qwen3.5-0.8B` | 26% / 13s | – | – | – | – | 0% / 226s |

† **The 35B row is from a different run**, and it matters which: this model is
rung 0 for every task type in §2, and until 2026-09-13 it had been measured on
coding only — all 23 of its responses in the 2026-09-04 run were coding tasks,
so the five other cells were empty and §2 called them provisional. They are no
longer. The figures above come from
`bench/qwen35b_alltypes_20260913b.json`: 78 responses, 26 tasks, three repeats
each, same CLI transport, collected 2026-09-13. Its aggregate row below (96%,
n=23) is the older coding-only number, kept for comparison. **Where the two
disagree, the six-type figures are the ones the policy is built on** — the
coding cell itself moved from 96% to 88% on the larger sample, which is the
scale of drift a 23-response estimate was always carrying.

Aggregate across all types, same source:

| model | n | correct | median | p90 |
|---|---|---|---|---|
| `claude-fable-5` | 28 | 100% | 9.1s | 16.4s |
| `claude-opus-5` | 28 | 96% | 8.3s | 40.3s |
| `claude-sonnet-5` | 28 | 96% | 6.0s | 17.5s |
| `claude-haiku-4-5` | 28 | 96% | 10.6s | 42.0s |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 23 | 96% | 8.5s | **161.4s** |
| `azure_ai/gpt-5.4-mini` | 55 | 93% | 4.4s | 12.7s |
| `azure_ai/gpt-5-mini` | 56 | 91% | 20.3s | 61.8s |
| `azure_ai/gpt-5.4-mini-copilot` | 56 | 89% | 4.2s | 15.1s |
| `azure_ai/gpt-5.6-luna` | 56 | 88% | 4.7s | 16.5s |
| `azure_ai/gpt-5.6-sol` | 56 | 88% | 8.4s | 23.5s |
| `vllm/Qwen3.5-0.8B` | 31 | **23%** | 14.3s | 83.7s |

**Read these with the sample size in mind.** Each cell in the first table holds
4–8 responses, except the 35B row, whose cells hold 6–24. A 100% means "no
observed failures", not proof of reliability, and the difference between 88% and
93% aggregate is a handful of answers.

Two gaps are wide enough to design around with confidence, and one of them is
newer than this table's original text. The **25% comprehension cliff** on luna
and mini is the first. The second is that **rung 0 is weak on planning and
reasoning (67% each) in a way it is not on long-context (100%)** — a 33-point
spread on 12 and 6 responses respectively, large enough to route on. Note these
two findings point opposite ways about "cheap models and comprehension": luna
and mini collapse to 25% there, while rung 0 holds 75%. Cheapness is not the
variable; the specific model is.

### 1.2 Cost

Anthropic rates from `bench_rates.json`, US dollars per million tokens. That
file's opus-5 row was recovered by least squares from 11 turns in
`usage_events` carrying `cost_basis='list'`, fits all 11 to the cent, and then
matched the published card exactly.

| model | input | output | cache read | cache write |
|---|---|---|---|---|
| `claude-opus-5` | 5.00 | 25.00 | 0.50 | 10.00 |
| `claude-sonnet-5` | 2.00 | 10.00 | 0.20 | 4.00 |
| `claude-fable-5` | 10.00 | 50.00 | 1.00 | 20.00 |
| `claude-haiku-4-5` | 1.00 | 5.00 | 0.10 | 2.00 |
| `vllm/*` | 0.00 | 0.00 | 0.00 | 0.00 |

Azure rates were **absent from `bench_rates.json` by deliberate choice** — that
file warns that "a model nobody priced must not come out cheapest", and
`bench/cost.py:117` returns `"rate not recorded"` rather than zero. They were
supplied by Pedro on 2026-09-12 from the billing line items, in EUR per million
tokens:

| billing line | EUR / 1M |
|---|---|
| 5.6 luna ShortCo Cd Inp Std Gl | 2.77 |
| 5.4 mini Opt Gl | 2.53 |
| 5.6 luna ShortCo Cd Wr Std Gl | 2.32 |
| 5.4 mini Inp Gl | 1.15 |
| gpt-4o-mini-0718-Inp-glbl | 0.75 |
| 5.4 mini cd Inp Gl | 0.50 |
| 5.6 luna ShortCo Opt Std Gl | 0.43 |
| 5.6 sol ShortCo Cd Wr Std Gl | 0.10 |
| gpt 4o mini 0718 cached Inp glbl | 0.08 |

Read as `Inp` = input, `Opt` = output, `Cd Inp` = cached input, `Cd Wr` = cache
write. Confirmed by Pedro. This reading is load-bearing: luna's output rate of
€0.43 is what makes it 25× cheaper than Sonnet, and it is *below its own cached
input rate* of €2.77, which is unusual enough to be worth re-checking if the
policy ever behaves unexpectedly.

### 1.3 Cost per 1000 tasks

Measured mean output tokens per response × output rate. EUR converted at
1 EUR = 1.08 USD.

| model | $/MTok out | mean output tok | **$/1000 tasks** | vs Sonnet | correct |
|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 0.00 | 1827 | **0.00** | — | 96% |
| `azure_ai/gpt-5.6-luna` | 0.46 | 495 | **0.23** | 25× cheaper | 88% |
| `azure_ai/gpt-5.4-mini` | 2.73 | 414 | **1.13** | 5× cheaper | 93% |
| `claude-sonnet-5` | 10.00 | 568 | **5.68** | — | 96% |
| `claude-haiku-4-5` | 5.00 | 2027 | **10.13** | 1.8× dearer | 96% |
| `claude-opus-5` | 25.00 | 690 | **17.25** | 3.0× dearer | 96% |
| `claude-fable-5` | 50.00 | 463 | **23.15** | 4.1× dearer | 100% |

**These are output-side only.** The benchmark recorded `output_tokens` and no
input or cache figures, so every number is a lower bound. Input costs
proportionally more for Claude (2.00–5.00/MTok) than for luna, so including it
would widen the gap rather than narrow it — the ranking is safe, the absolute
values are not.

**`claude-haiku-4-5` costs more than `claude-sonnet-5` despite half the rate.**
It emits 2027 output tokens where Sonnet uses 568. A cheaper per-token price,
1.8× the bill. It is also slower at equal or worse accuracy on every axis
measured, including 85s against Sonnet's 9s on reasoning. It earns no tier.

### 1.4 Production usage this month

Supplied by Pedro, 2026-09-12.

| model | requests | tokens | svc CPU | svc RAM |
|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` (self-hosted) | 20,304 | 1,906,774,856 | **109.1%** | 33.1 GB |
| `azure_ai/gpt-5.6-luna` | 1,996 | 209,370,676 | — | — |
| `nvidia/Qwen3.6-35B-A3B-NVFP4` | 933 | 93,509,080 | — | — |
| `azure_ai/gpt-5.4-mini` | 166 | 7,885,210 | — | — |
| `azure_ai/gpt-5.4-mini-copilot` | 135 | 700,306 | — | — |
| `azure_ai/gpt-5-mini` | 113 | 817,435 | — | — |
| `azure_ai/gpt-5.6-sol` | 76 | 658,087 | — | — |

**Qwen is the workhorse, by an order of magnitude.** 20,304 requests against
luna's 1,996 — about 87% of this month's traffic already runs on the free
model. Any policy that ends up routing most work to paid models is a
regression against what the system does today, not an improvement.

**The 109.1% figure was misread in an earlier draft, and the misreading is
recorded here so it is not repeated.** It was taken as "already
oversubscribed" and used to justify a capacity gate that kept the free tier
closed. It is `svc CPU` for the serving process — top-style percent-of-one-core
— so 109.1% alongside 33.1 GB RSS describes roughly one core busy on a large
inference box: a healthy, working service, not a saturated host. It says
nothing about whether that host has room, and nothing readable by this policy
in any case: the gateway is an external HTTPS endpoint, absent from
`system_samples` entirely (§2.1). CPU is no longer gated anywhere in this
design.

Its price is still not literally zero — it is paid in GPU occupancy shared with
every other user of that gateway — but that cost is not ours to meter, and the
per-type deadline is what bounds our share of it.

### 1.5 Host resources

From `system_samples` (`host_id`, `host_type`, sampled every 30s), read
2026-09-12 ~22:00Z.

| host | cpu | mem used | load1 |
|---|---|---|---|
| local | 33.6% | **87.5%** | 2.03 / 4 cores |
| transport `f6f52152…` | 50.6% | 18.4% | 2.73 |
| transport `11f0d67a…` | 2.1% | 51.5% | 3.55 |
| transport `ba872597…` | 13.1% | 31.5% | 0.12 |

`resource_guard.capacity()` on the local host at the same moment:

```
{'existing': 6, 'total': 6, 'cost_mb': 350, 'floor_mb': 400, 'available_mb': 683}
check: ok=False — "333 MB would remain after a 350 MB start, floor is 400 MB"
       6 interactive agents holding 2072 MB; the console holds 588 MB
```

**Local capacity for additional agents is zero.** The local host is the scarce
resource by a wide margin; the transports have headroom.

---

## 2. Tier policy

**Rung 0, for every task type, is `vllm/Qwen3.6-35B-A3B-NVFP4` at $0.00, with a
per-type deadline of 45s to 240s.** The table below is the *fallback* ladder: where a leaf goes
when the free rung times out or fails.

| task type | first paid rung | $/1k tasks | why this one |
|---|---|---|---|
| coding | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 4s — same accuracy as Opus at 75× less |
| long-context | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 3s |
| multi-turn | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 6s |
| planning | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 23s against Sonnet's 100% at 18s — 5s slower, 25× cheaper |
| comprehension | `claude-sonnet-5` | 5.68 | luna and mini both measure 25%; the one cliff |
| reasoning | `azure_ai/gpt-5.4-mini` → `claude-opus-5` | 3.54 expected | see below |
| split decision | `claude-sonnet-5` | 5.68 | judging scope is comprehension work, where cheap models fail |

**The split decision is the one exception to rung 0.** Deciding whether to
decompose is comprehension work, the type where cheap models collapse to 25%,
and a wrong split is not caught by any deadline — it silently shapes the whole
subtree. It goes straight to Sonnet, no free attempt.

**Each cell is an *entry rung*, not a fixed assignment.** A leaf that falls off
rung 0 starts here and escalates upward through the remaining rungs,
capped at `MAX_ATTEMPTS`. A type whose entry rung is already the top has one
attempt and no escalation.

Reasoning is the case where this pays. mini is 86% correct at $1.13/1k; Opus is
the only model at 100%, at $17.25/1k. Entering at mini and escalating the
failures costs

```
0.86 x 1.13  +  0.14 x (1.13 + 17.25)  =  $3.54 per 1000
```

against $17.25 for always-Opus — 4.9x cheaper for the same final accuracy,
paying the Opus price only on the 14% that need it. The trade is latency on that
14%: 8s then 27s, rather than 27s once.

**Free tier — the default entry rung for every task type.**
`vllm/Qwen3.6-35B-A3B-NVFP4` is tried *first*, for all six task types, with no
capacity gate in front of it. The paid ladder exists to catch what it drops,
not to be reached first.

The deadline is the entire backpressure mechanism. A node placed on the free
tier is abandoned when it expires and retried on the paid rung its task type
names. So a slow gateway costs one deadline's worth of wasted seconds per
affected leaf, never a wrong answer kept and never a stall.

#### What the deadline actually costs

An earlier draft set this at 45s and justified it as "comfortably above the 8.5s
coding median, far below the 161s p90." Both halves of that sentence are true and
the conclusion drawn from them was wrong, because **the latency distribution is
bimodal and the median describes only the lower mode.** The 23 measured
latencies, in seconds:

```
4.1 4.6 4.8 4.8 4.8 5.2 5.2 5.6 6.3 7.8 7.9 8.5
        <- nothing at all between 8.5 and 31.8 ->
31.8 32.7 37.6 38.4 56.0 79.9 96.5 161.4 284.8 367.9 498.5
```

Twelve turns finish under 8.5s; the rest start at 31.8s and run to 498.5s.
Nothing lands in between, so any deadline from 9s to 31s selects the same twelve
turns, and the median is not evidence about where the tail begins.

Completion rate against deadline, counting only turns that finished *and* were
correct — this is exactly the quantity §2's target is stated in:

| deadline | finish within it | correct within it |
|---|---|---|
| 30s | 12/23 (52.2%) | 47.8% |
| **45s** | 16/23 (69.6%) | **65.2%** |
| 60s | 17/23 (73.9%) | 69.6% |
| **90s** | 18/23 (78.3%) | **73.9%** |
| 180s | 20/23 (87.0%) | 82.6% |

**At 45s the target of ≥70% is unreachable on the only data that exists**, on
the one task type this model is measured best at. All seven turns that exceeded
45s returned *correct* answers — the deadline would have discarded good work and
paid for a second rung to redo it. 60s still misses. **90s was the smallest round
deadline clearing 70% on this data** — superseded the next day by the per-type
values below, which keep 90s for coding and move the other five.

That reasoning was sound on the data it had, and the data it had was coding
only. The six-type run replaced it the next day.

#### The deadline is per task type, because the types are not alike

`bench/qwen35b_alltypes_20260913b.json`, 78 runs. **A single global 90s serves
78.2% of leaves free, which clears the ≥70% target** — so the global value was
not wrong. It was just leaving work on the table at both ends, being loose for
four types and tight for two.

Per type, the deadline that retains *every* correct answer observed:

| task type | accuracy | median | slowest **correct** | fastest **wrong** | `TIER0_DEADLINE` |
|---|---|---|---|---|---|
| long-context | 100.0% | 17.3s | 22.6s | — (none wrong) | **45s** |
| multi-turn | 75.0% | 16.9s | 19.6s | 18.7s | **45s** |
| comprehension | 75.0% | 17.8s | 34.8s | 12.9s | **45s** |
| coding | 87.5% | 11.6s | 70.8s | 19.9s | **90s** |
| planning | 66.7% | 70.4s | 95.7s | 57.7s | **120s** |
| reasoning | 66.7% | 123.3s | 216.4s | 1168.2s | **240s** |

Each value is the slowest correct run rounded up to the next round number. The
whole table is worth more than the sum of its rows for two reasons.

**Reasoning is the case the global deadline handled worst, and per-type handles
best.** Its three `reasoning-puzzle` repeats ran 216s (correct), 1168s (wrong,
24,758 output tokens, hit the cap) and 1903s (wrong, 38,461 tokens, hit the
cap); `reasoning-math` ran 16–30s and was correct all three times. A 90s
deadline throws away the one correct puzzle answer *and* still waits 90s on the
two runaways. A 240s deadline keeps the correct answer and kills each runaway
after 240s instead of 1903s. **Here the deadline discriminates perfectly: every
correct run is under it, every wrong run is far above it.** Worst-case wasted
time per reasoning leaf drops from 1903s observed to 240s bounded.

**Planning is the opposite case, and the deadline is nearly useless there.** Its
correct runs span 32.2s to 95.7s and its wrong runs span 57.7s to 75.5s — the
two distributions interleave, so no deadline separates them. 120s is chosen to
stop discarding correct work (90s was cutting the 95.7s one), not because it
filters anything. Planning's 66.7% is a capability limit, and the escalation
ladder is what addresses it.

Per-type deadlines serve **63/78 = 80.8%** free — every correct answer the model
produced — against 78.2% for a global 90s, while *also* bounding the reasoning
tail. Both improve at once because the two types pulling in opposite directions
stop being averaged together.

**Two types individually miss the ≥70% target**: planning at 66.7% and reasoning
at 66.7%. That is a capability result, not a deadline result — it survives any
deadline, because those are simply the fractions the model gets right. §2's
target is a fleet-wide number and the fleet clears it at 80.8%; these two rows
are where the paid ladder earns its place.

**Why no gate:** an earlier draft gated this on the gateway host's
`cpu_pct`/`load1` via `system_latest_by_host()`. That gate could never open.
The gateway is `https://llm.ai-machine.cfappsecurity.com`, an external HTTPS
endpoint; `system_samples` only ever contains `local` and the three SSH
transports (`f6f52152…`, `11f0d67a…`, `ba872597…`). With no sample for the
gateway, §2.1's staleness rule resolved it to *unknown*, and unknown was
refused — so the free tier was closed permanently, by construction, and the
policy would have routed 100% of work to paid models. See §1.4 for the
misreading that motivated the gate.

**It is free and it is already the workhorse.** Production this month:
20,304 requests against luna's 1,996 — roughly 87% of real traffic already
runs here (§1.4). The target below is not aspirational; it is the status quo
that the gated draft would have switched off.

**But 87% is measured without a deadline, and that is the whole difference.**
Those 20,304 production requests ran to completion however long they took;
nothing abandoned the slow ones. This design adds an abandonment rule that
production has never had, so 87% is evidence the gateway can carry the volume —
not evidence the target survives the deadline. On the benchmark data it does not
survive 45s, which is why the deadline moved. Treat 87% as the ceiling the
deadline spends down from, not as a floor it inherits.

**Target: ≥70% of orchestrator leaves completed on the free tier.** Measurable
from `usage_events` — rows carrying `origin="orchestrator"`, grouped by model,
counting attempts that completed within deadline against total leaves. If it
falls below 70%, the deadline or the ladder is wrong and should be re-tuned,
not the target.

**The five formerly unmeasured types were measured on 2026-09-13.** Every one of
the 23 responses behind the old 96% figure was a coding task, so five of the six
types this policy routes on had zero evidence behind them. That run exists now —
`bench/qwen35b_alltypes_20260913b.json`, 78 responses across all six types — and
its numbers are in §1.1 and §2's deadline table.

What it changed, in short: rung 0 is **not** uniformly strong. It is perfect on
long-context (100%), good on coding (88%), adequate on comprehension and
multi-turn (75% each), and genuinely weak on planning and reasoning (67% each).
The single 96% figure the policy was built on was a coding number wearing an
aggregate's clothes.

What it did **not** change: trying rung 0 first is still right. 80.8% of leaves
complete free and correct under the per-type deadlines, comfortably past the
≥70% target, and the two weak types are exactly the ones the paid ladder exists
to catch.

One genuinely unexpected result, recorded because it contradicts a premise used
elsewhere in this document: **rung 0 scores 75% on comprehension, against luna's
and mini's 25%.** The comprehension cliff §2 designs around is a cliff for the
cheap Azure models specifically, not a property of cheap models in general. That
does not change comprehension's entry rung — Sonnet's 100% still beats 75% — but
it does mean the free attempt is worth making there rather than skipped, which
is what rung 0 already does.

### 2.1 Placement gates

Placement is about *where an agent process runs* — local box or SSH transport.
It is orthogonal to model choice: selecting a gateway-served model such as
Qwen or luna costs no local RAM beyond the CLI process itself.

| gate | condition | source |
|---|---|---|
| transport eligible for placement | `mem_pct <= 75` | `system_latest_by_host()` |
| local spawn permitted | `resource_guard.check().ok` | `/proc/meminfo`, live |
| any sample older than 120s | treated as **unknown**, and unknown is not eligible | `created_at` |

Samples arrive every 30s, so 120s is four missed intervals. Unknown is refused
rather than assumed healthy, for the reason `routes/db_supervisor_map.py:356` already
gives about the hub glow: "nothing measured and nothing happening must not look
alike". That rule is sound for hosts we actually sample; it is precisely what
made the deleted gateway gate unopenable, since the gateway is not a host we
sample at all.

RAM is the real local constraint — a 4 GB box running six agents — which is why
`resource_guard` stays. CPU is not gated anywhere.

**Escalation ladder:** `vllm → luna → mini → sonnet → opus`, one retry per rung,
**≤3 attempts per leaf**, then the node reports failed. The target is that
**≥70% of leaves stop at rung 0** and cost nothing; most of the remainder should
stop at the first paid rung, which is 100% on four of the six task types.

**A leaf gets at most three rungs, so the ladder is never walked end to end.**
`MAX_ATTEMPTS` is what stops a pathological leaf from spending five models'
worth of budget before failing.

### 2.2 The full ladder, written out

`MAX_ATTEMPTS = 3` means every task type needs exactly three rungs named, and
an implementer cannot derive the third from the entry rung alone. All six:

| task type | attempt 1 | attempt 2 | attempt 3 |
|---|---|---|---|
| coding | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `azure_ai/gpt-5.4-mini` |
| long-context | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `azure_ai/gpt-5.4-mini` |
| multi-turn | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `azure_ai/gpt-5.4-mini` |
| planning | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `azure_ai/gpt-5.4-mini` |
| comprehension | `vllm/Qwen3.6-35B-A3B-NVFP4` | `claude-sonnet-5` | `claude-opus-5` |
| reasoning | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.4-mini` | `claude-opus-5` |
| split decision | `claude-sonnet-5` | — | — |

**The generating rule:** start at the type's entry rung from §2's table, then
walk the global order `vllm → luna → mini → sonnet → opus`, **skipping any model
measured worse on this task type than the one it is replacing.** Two rows come
out of that rule rather than out of position, and both are worth stating because
both look like mistakes otherwise.

**Reasoning skips Sonnet.** The global order puts Sonnet between mini and Opus,
but Sonnet measures 75% on reasoning against mini's 86% — escalating into it is
a downgrade. mini goes straight to Opus, which is the 100% the §2 cost
arithmetic already prices (`0.86 × 1.13 + 0.14 × (1.13 + 17.25) = $3.54/1k`).

**Comprehension escalates into a model measured worse, deliberately.** Opus is
50% on comprehension against Sonnet's 100%, so the skip rule says stop at Sonnet
and give the type no third attempt. It gets one anyway, for three reasons: those
cells are n=2 and n=2, far too thin to rank two models 50 points apart; Opus at
50% still doubles luna and mini at 25%, so it is not a *cheap* model in
disguise; and comprehension is the type most likely to need a retry, so leaving
it with no escalation trades a well-evidenced cost for a badly-evidenced one.
**This is the weakest cell in the table.** If a re-run with real sample sizes
confirms Opus below Sonnet on comprehension, delete the third rung rather than
reordering it — there is nothing else measured above Sonnet on this type.

**Where every 100% ladder is really a provider change, not a capability
change.** For coding, long-context, multi-turn and planning, all four of vllm,
luna, mini and Sonnet measure 100%. Escalating there buys nothing in capability
and is not meant to: the second and third attempts exist to survive a transient
gateway failure or a timeout, which is why they cross from the self-hosted model
to Azure and then to a second Azure deployment. Read those three rows as
*retries on independent infrastructure*, and do not "optimise" them by
collapsing to a single model — that is the property they are buying.

**Excluded outright:** `vllm/Qwen3.5-0.8B` (23% correct, 0% on reasoning),
`claude-haiku-4-5` (dearer and slower than Sonnet), `claude-fable-5` (100% but
$23.15/1k, 4.1× Sonnet). Note that Haiku measures 100% on comprehension and is
still excluded — the exclusion is on cost and latency, so if comprehension's
third rung is ever reopened, Haiku is the candidate to re-price first.

---

## 3. Node lifecycle

Five gates. Only the second ever spends a model call on *deciding* anything.

1. **Score** — `PlanParser._score_complexity(text)` (`orchestrator.py:253`), free, returns 1–5.
2. **Split** — 1–2 execute now; 4–5 decompose now; exactly 3 costs one Sonnet
   call. A worker never judges its own scope: the models best at executing
   (luna, 100% on coding at 4s) are the worst at judging (25% on
   comprehension). This is the one decision that skips rung 0 (§2).
3. **Placement** — `resource_guard.check()` before every spawn. When local is
   full, select a `proxy`-provider machine whose `transport_id` host has
   headroom per `system_latest_by_host()`. `runner.py:370` states the mechanism:
   "A backend with transport_id set runs its claude process on that" transport.
   With no host available, the node queues rather than failing. **A
   write-capable task type is never offered a transport at all, regardless of
   headroom** — see §3.2.
4. **Execute** — attempt 1 is always rung 0 (`vllm/Qwen3.6-35B-A3B-NVFP4`,
   `TIER0_DEADLINE`); later attempts take model and machine from the tier
   table, with no deadline beyond the runner's own.
5. **Escalate** — timeout or failure moves one rung. Usage is recorded per
   attempt with `origin="orchestrator"`, per CLAUDE.md §5, **including
   failures**: a turn that ran to its deadline and then timed out has been paid for, and
   recording only successes makes the cheapest-looking tier the one that fails
   most.

### 3.1 The missing classifier

The tier table is keyed by `task_type`, and **nothing in the codebase computes
one.** `_score_complexity` yields 1–5, not a type; the benchmark's types were
labelled by hand in the harness.

Extend `orchestrator.COMPLEXITY_PATTERNS` into a map emitting
`(type, score, mutates)` together — the third field is required by §3.2, which
is why it is filled in here rather than left to the implementer. Its keys
already read like types:

| existing pattern | score | type it implies | `mutates` |
|---|---|---|---|
| `architect\|design.*system\|create.*framework` | 4 | planning | False |
| `implement.*multiple\|coordinate.*agent\|orchestrate` | 5 | planning | **True** |
| `debug.*complex\|trace.*error.*chain\|performance.*bottleneck` | 4 | reasoning | False |
| `write.*test.*suite\|integration.*test\|e2e.*test` | 3 | coding | **True** |
| `analyze.*code.*review\|refactor.*large\|migrate.*database` | 4 | coding | **True** |
| `write.*doc.*umentation\|create.*tutorial\|explain.*concept` | 2 | comprehension | **True** |
| `research.*api.*document\|find.*replacement\|evaluate.*option` | 3 | comprehension | False |
| `read.*file\|list.*directory\|grep.*pattern\|summarize.*log` | 1 | long-context | False |
| `simple\|small\|quick\|minor\|fix.*typo` | 1 | coding | **True** |

**How the `mutates` column was assigned.** The test is whether the *verb* in the
pattern produces a changed file, a git operation, or a database write — not
whether the task sounds difficult. `architect` and `design.*system` produce a
document by way of a decision, so they read; `implement.*multiple` and
`orchestrate` produce code, so they write. `write.*test.*suite`,
`refactor.*large` and `migrate.*database` are unambiguous writes —
`migrate.*database` is the one on this list where a wrong answer is least
recoverable, which is the whole reason §3.2 exists. `write.*doc.*umentation`
writes a file even though its type is comprehension, and that pairing is the
point: **`mutates` is orthogonal to `task_type`**, so a comprehension task can
be write-capable and a coding task (`research.*api.*document`-style lint or
review) can be read-only. `debug.*complex` reads to find the cause; the fix that
follows arrives as its own node and matches a writing pattern then.

Five of nine are `True`. That is the expected shape, not a sign the test is too
loose — §3.2's default is `True`, so the question this column answers is only
"which patterns are safe to *exempt*," and four is a defensible number of
exemptions out of nine.

That last row is the awkward one, and it is listed rather than dropped because
dropping it is what an earlier version of this table did. It is the only key in
`COMPLEXITY_PATTERNS` that names a *difficulty* rather than a kind of work, so it
has no honest type; `coding` is assigned because `fix.*typo` is the only concrete
thing in it. Leaving it out of the map does not leave it unclassified — it makes
it fall through to the default below, which sends "fix a typo" to Sonnet at
$5.68/1k. Re-check this row first if cheap tasks start arriving on expensive
rungs.

**Unmatched text defaults to `comprehension`**, deliberately. That is the type
where cheap models collapse to 25%, so an unclassifiable task routes to Sonnet.
Guessing wrong toward the capable model costs $5.68 per thousand; guessing wrong
toward the cheap one costs a wrong answer.

### 3.2 Write-capable tasks stay local

Every gate above routes on capability, cost, and host load. None of them asks
whether a task *mutates* anything — so as written, a task that edits files,
runs `git`, or touches the database is exactly as eligible for transport
placement as one that only reads. That is a gap, not a decision: this project
already treats read vs. write on a transport as different trust tiers
everywhere else it appears (`transports.js`'s own Check/Init split: "everything
else the console does to a transport reads, so the one operation that writes
gets its own deliberate click" — a transport is a host this console does not
own, and a write landing there is a different risk than a read failing there).
Placement should carry the same asymmetry.

**The classifier extension in §3.1 gains a third field.** `(type, score,
mutates)`, not just `(type, score)`. A task whose pattern implies file edits,
git operations, or any other state change is `mutates=True`; everything else
(`read.*file|list.*directory|grep.*pattern|summarize.*log` and similar) is
`mutates=False`.

**Placement rule:** `mutates=True` forces local placement unconditionally —
`resource_guard.check()` still gates whether it can spawn at all, but it is
never offered a transport regardless of headroom. `mutates=False` is placed
exactly as §3 already describes (local first, transport on headroom, queue
otherwise). This is orthogonal to task_type and to the tier table: a `coding`
task that only reads (e.g. a lint pass) is transport-eligible; a `coding` task
that edits is not, even though both route to the same model on the same tier.

**Unmatched or ambiguous mutation intent defaults to `mutates=True`** — the
same reasoning as §3.1's comprehension default: guessing wrong toward "stays
local" costs queue time; guessing wrong toward "eligible for a transport"
risks an edit or a delete landing on a host this console does not own, with no
way to undo it from here.

---

## 4. Data model and caps

`TaskNode` gains `depth`, `attempt`, `tier`, `machine_id`, `deadline_s`,
`task_type`, `mutates`, and begins actually using `parent_id` — already plumbed
through `TaskGraph` and the API payload at `orchestrator.py:417`, but `None` at
the only construction site (`orchestrator.py:754`), which is why the hierarchy
is flat today.

`task_type` and `mutates` are both outputs of §3.1's classifier and both have to
be stored, not recomputed: `task_type` keys the ladder in §2.2 and is needed
again on every escalation, and `mutates` gates placement in §3.2 and must not be
re-derived from a prompt that a retry may have reworded. `deadline_s` is set
only for attempt 1 — it carries `TIER0_DEADLINE` on rung 0 and is `None` on
every paid rung, per §3 gate 4.

```
MAX_DEPTH      = 3      # goal -> sub -> sub
MAX_CHILDREN   = 4      # per node
MAX_NODES      = 40     # whole tree
BUDGET_USD     = 1.00   # per goal, checked before each spawn
TIER0_DEADLINE = {         # seconds, free tier only -- derived in §2
    "long-context":    45,
    "multi-turn":      45,
    "comprehension":   45,
    "coding":          90,
    "planning":       120,
    "reasoning":      240,
}
MAX_ATTEMPTS   = 3      # per leaf, across the ladder
```

**The termination guard matters more than the depth cap.** A child's complexity
score must be strictly less than its parent's; a decomposition returning
children scoring the same or higher forces them to execute instead. Without it,
a task the scorer reads as 5 decomposes into children it also reads as 5, and
the tree grows until `MAX_DEPTH` stops it — bounded on paper, and in practice a
runaway that spends the budget planning and never executes anything.

### 4.1 What must not be routed around

`ModelRouter.assign_model` currently ends:

```python
if complexity >= 4:
    return config.ANTHROPIC_MODEL
return config.ANTHROPIC_MODEL
```

Both branches are identical, so complexity is computed at line 238 and
discarded, and `DEFAULT_RULES` is `.* -> ANTHROPIC_MODEL`. That dead branch is
the seam this design fills; it is not a second routing system alongside it.

A model is never selected as a bare string. Every routing decision returns
**`(model, machine)`** together, because `runner.run_turn` takes a model string
while `get_backend(chat_id, owner)` resolves the backend from the chat's pinned
machine — so a model chosen without its machine reaches a gateway that does not
serve it and returns `429 "No deployments available for selected model"`, a
routing failure wearing a capacity error's clothes. CLAUDE.md §0.1 states this;
`orchestrator.py:836` carries a comment recording it happening.

---

## 5. Verification

The router is a **pure function** of `(task_type, score, resource snapshot)`
returning `(model, machine, deadline)` — no model calls, no I/O. The whole
policy is then testable by table without spawning anything, which matters
because the alternative needs five live backends to run a unit test.

| what | the trap in testing it |
|---|---|
| tier selection per task type | asserting the model string alone; it must assert `(model, machine)` together, or §0.1's 429 is untested |
| escalation ladder | asserting only the final outcome; assert the *sequence* and the ≤3 cap |
| admission control | mocking `resource_guard` to always return ok, which passes against a design that would OOM the host — feed a fake `/proc/meminfo` instead, since `read_meminfo()` already takes a path |
| termination guard | asserting intent; assert node count for a decomposition whose children score ≥ parent |
| budget ceiling | asserting per node; spend accumulates across the tree, so assert the tree total |
| usage recording | asserting successes only; a failed and a timed-out attempt must each produce a row |
| rung 0 is always tried first | asserting the free model appears *somewhere* in the ladder; assert it is attempt 1 for every task type except `split decision` |
| the free tier has no capacity gate | asserting behaviour only when samples exist; assert routing is unchanged when `system_latest_by_host()` returns nothing at all, which is the real gateway case |
| the deadline is configuration, not a literal | hardcoding a number in the router and again in the test, so both agree and neither tracks `TIER0_DEADLINE`. These values are known-provisional and will be re-tuned from production (§2) — assert the router reads the mapping, by setting a type's value to something else in the test and checking the deadline follows |
| the deadline is per task type | asserting one value and assuming the rest. Assert all six, and assert specifically that reasoning gets 240s and long-context 45s — those are the two ends, and collapsing the mapping back to a single value is the regression this row exists to catch |
| a task type missing from `TIER0_DEADLINE` | letting a `KeyError` reach the turn, or silently defaulting to the shortest value. Assert the fallback explicitly — an unknown type must get the *longest* deadline, not the shortest, for the same fail-safe reason §3.1 defaults unmatched text to comprehension |
| the full ladder (§2.2) | testing only the two types named in prose. Assert all six three-rung sequences by table, including the two that break positional order: reasoning skips Sonnet, comprehension ends on a model measured worse than its own rung 2 |
| `mutates` gates placement independently of `task_type` | asserting a read-only `coding` task and a writing `coding` task take the same path. They must not: assert the writing one is refused a transport *while a transport has headroom*, which is the only condition under which the rule does anything |
| the `mutates` default | testing only the nine patterns in §3.1's table, all of which have an explicit value. Assert that text matching *no* pattern comes back `mutates=True`, since that default is the safety property |

The last two are the ones most likely to pass vacuously. A test that never puts
a transport in the pool proves nothing about a rule whose entire job is to
decline one.

Every test is mutation-checked before it is claimed to work: break the ladder
order, break the guard, break the termination rule, and confirm a specific test
fails for each.

**The ≥70% target is measured in production, not asserted in a unit test.** It
is a property of real traffic, and no fixture can establish it. Query
`usage_events` for `origin="orchestrator"`, group by model, and compare leaves
completed on `vllm/Qwen3.6-35B-A3B-NVFP4` against total leaves. Below 70%, read
the timeout rate before changing the policy: a deadline set too tight and a
gateway genuinely too slow need opposite fixes, and only the per-attempt rows
tell them apart.

---

## 6. Consequences accepted

**This design does not create capacity.** With local `capacity()` at
`existing 6, total 6`, a recursive tree runs today only by placing work on
transports. If none is available it degrades to a single serialised agent slot:
correct, bounded, and no faster than doing the work in one conversation. What it
buys is cost — the entry rung is free, and what escapes it lands on a paid rung
25× cheaper than Sonnet.

**The free tier is unmetered, not unlimited.** Nothing in this design measures
the gateway's load, and nothing can: it is external and unsampled. The per-type
deadlines are the only thing bounding what we ask of it. If the gateway degrades
under someone else's load, the symptom here is leaves timing out and escalating
— more spend and more latency, never a stall — and the fix is to re-tune the
deadline, not to invent a capacity signal we cannot read.

**Roughly a fifth of leaves will pay the deadline twice over.** A leaf that
times out on the free tier has spent 90 seconds and produced nothing before the
paid rung even starts. On the measured coding data that is 5 of 23 leaves, 21.7%
-- a minority, but a larger one than "some share" suggested, and the arithmetic
still favours trying free first — but it is a real latency cost, not a free
option, and it is why the target is measured rather than assumed.

**The cost figures will drift.** They are output-side, from a single benchmark
run on 2026-09-04, with 4–8 samples per cell. They are sound enough to rank
models 25× apart and not sound enough to rank models 20% apart. Re-measure
before treating any near-tie as settled.

**Parent-to-child messaging across a transport exists, and its remote half is
not deployed.** A node placed on a transport still reports results through
`runner.run_turn` over the proxy, which works — so the design does not depend on
this. But out-of-band messaging between agents does have a real implementation:
`POST /api/chats/{id}/agent-reply` tries `transcripts.agent_reply_to` locally,
then walks live (`tunnel_up=1`) transports running
`transcripts.build_remote_reply_command`, which base64-encodes `{to, text}`,
executes `python3 -c` inside the transport's `remote_path`, and calls the *same*
`agent_reply_to` on the far host. It carries a 5-minute per-target cooldown, an
audit row per attempt in `agent_reply_log`, a wake-up turn through the
transport's own tunnel, and treats remote stdout strictly as data
(`json.loads`, never `eval`). Design: `2026-09-08-transport-aware-agent-reply-design.md`.

It had never once run, for two separate reasons, both since addressed.

**The code was broken.** `build_remote_reply_command` inserted `remote_path`
into `sys.path` and then called `transcripts.agent_reply_to(...)` without ever
importing `transcripts`, so every remote relay died with
`NameError: name 'transcripts' is not defined`. Four tests covered that function
and all four passed, because each inspects the command as a string and none had
ever executed it. Fixed in 031e337 with a test that runs the generated script.

**The prerequisite was unmet.** `~/wc-proxy` is not a checkout — on the
pentester transport it held four files (`claude_proxy.py`, `backend_env.py`,
`proxy.env`, `proxy_token.txt`), a purpose-built proxy deployment. Reaching a
successful relay needed `transcripts.py`, `db.py`, `config.py` and
`routes/db_sessions.py` (the last reached lazily through `db.__getattr__`).

**Verified end to end on 2026-09-12**: the relay delivered to session
`9cdf80ea-060d-4e97-b8ba-f7cd32cbe1c6` on that host, and the
`<cross-session-message>` record is in its transcript. That is the first
message this path has ever carried.

**Current deployed state, recorded so it is not drift.** `~/wc-proxy` on the
pentester transport now holds the original four files plus those five modules,
placed there by hand during that verification. `claude_proxy.py` and
`backend_env.py` were deliberately not touched — the remote proxy imports only
stdlib and `backend_env`, so the additions change nothing it loads — and the
remote `claude_proxy.py` differs from HEAD (`bb117e85…` against `691fe393…`),
so a full sync would replace the proxy that host is running. **Resolving that
drift is a deployment decision, not a side effect of provisioning**, and it
should be settled before any transport is synced wholesale.

Provisioning is at least safe now: until 0d3f6ac, `transport_sync` took its
manifest from git and its *bytes* from disk, so syncing a transport shipped
whatever six sessions had half-saved and then recorded `last_synced_sha` as
though the transport matched that commit.

Note also that the socket-based channel (`SendMessage`, `/run/user/1000/cc-socks`)
is **local only** and cannot be used for this: a unix domain socket is a
filesystem object and does not cross a host. The remote agents on pentester hold
their sockets at `/tmp/cc-socks/`, reachable from that host and nowhere else.
The two channels are unrelated, and only the file-based one routes.

---

## 7. Decisions taken with Pedro

| decision | chosen | rejected |
|---|---|---|
| flow shape | recursive: a worker may decompose further, to a depth cap | fixed 3-role plan/work/verify pipeline |
| split trigger | hybrid: score gates, Sonnet decides only the ambiguous band (score 3) | deterministic score alone; the worker judging itself |
| failure handling | retry once on the next tier up, ≤3 attempts per leaf | route by task type with no retry; escalate to the parent for re-planning |
| free vs fast | free first, abandon at a deadline and escalate | free only for background work; cheapest-that-works ignoring latency |
| Azure pricing | use the supplied billing lines, `Opt` read as output | leave Azure unpriced, as `bench_rates.json` had it |
| write-capable placement (2026-09-13) | forced local, unconditionally, independent of headroom | route by task_type/cost/load alone, same as a read |
| free-tier scope (2026-09-13) | rung 0 for every task type, no capacity gate, a deadline as the only backpressure | coding-only; or any-type but gated on gateway CPU — a gate that could never open |
| gateway capacity signal (2026-09-13) | none — accept it is unmeasurable and bound exposure with the deadline | invent a proxy signal, or keep refusing the free tier when unknown |
| success criterion (2026-09-13) | ≥70% of leaves complete on rung 0, measured from `usage_events` | leave "mostly free" as an untested assumption |
| rung-0 deadline (2026-09-13, re-check) | 90s — the smallest round value that clears the ≥70% target on the coding-only data then available | 45s, which yields 65.2% and so shipped a policy predicting its own failure; 60s, which yields 69.6% and still misses |
| rung-0 deadline (2026-09-13, after the six-type run) | per task type, 45s to 240s, each the slowest correct run rounded up | keeping one global 90s, which discards correct planning and reasoning answers at one end and waits needlessly on three fast types at the other |
| planning and reasoning below target (2026-09-13) | accept 67% on both; it is a capability limit no deadline changes, and the paid ladder is what catches it | tune the deadline until those rows clear 70%, which would be fitting the gate to the metric rather than to the work |
| escalation order (2026-09-13, re-check) | all six ladders written out in §2.2, generated by walking `vllm → luna → mini → sonnet → opus` and skipping any model measured worse on that type | leaving the third rung to be inferred from the entry rung, which is not derivable and would have put reasoning on Sonnet at 75% against mini's 86% |
| comprehension's third rung (2026-09-13, re-check) | Opus, despite measuring 50% against Sonnet's 100%, because both cells are n=2 and the type most needs a retry | stopping at Sonnet with no escalation — correct on the skip rule, but trading a well-evidenced cost for a badly-evidenced one |
| `mutates` per pattern (2026-09-13, re-check) | assigned explicitly for all nine patterns in §3.1, five True | leaving §3.2's rule stated but unspecified, so every pattern would have hit the `True` default and no task could ever reach a transport |
