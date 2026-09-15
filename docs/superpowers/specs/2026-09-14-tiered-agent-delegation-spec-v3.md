# Tiered agent delegation — implementation spec v3

**Date:** 2026-09-14
**Status:** design complete, not implemented.

This document is standalone. The base design's measurement tables from the original design are still the evidence; everything an implementer needs to *build* is below.

---

## 1. Scope

Today `ModelRouter.assign_model` returns `config.ANTHROPIC_MODEL` on both branches — complexity is computed and discarded. This design fills that seam.

What must be **built**: the duplicate-branch fix in `ModelRouter.assign_model` so the computed complexity reaches the routing decision instead of being discarded (§3), the task classifier (§2), the coding oracle (§4.2), the three review gates (§4.3–4.5), the blast-radius check (§4.6), the settings page (§9.2). Everything else is configuration over existing mechanisms.

### 1.1 Startup validation

At config load the system checks six invariants and fails loudly if any are broken:

| invariant | condition |
|---|---|
| no blank fields | every row in the benchmark table from §2.6 has a value in every column; TBD is not permitted for an **operational** task type |
| model resolution | every model name named in any ladder (§3) resolves to a valid backend-and-model pair available in the model combo box (§9.3) |
| no empty ladder | after applying the cost-ceiling filter (§2.7), every **operational** task type must have at least one rung; no operational task type is left with an empty ladder |
| every rung is backed by a row | every `(model, task_type)` pair appearing in the §3 ladder snapshot has a row in the §2.6 table. A rung named in the snapshot with no backing row means the snapshot and the generator disagree, and the generator silently wins |
| the ceiling fits the budget | for every **operational** task type, the computed worst-case path (§5.1) is at or below the combined latency ceiling. A ceiling below it kills leaves that passed every per-attempt deadline, after paying for them |
| the ladder fits the budget | for every **operational** task type, the expected tree cost of its ladder (§2.7 — leaves × tokens × Σ reach-probability × rate) is at or below `BUDGET_USD`. A ladder that cannot afford its own rungs will exhaust the tree budget mid-run, which is a worse failure than refusing to start |

**Bootstrap exemption.** §2.6 ships mostly unmeasured, so a check that refused every TBD would mean the system could never start for the first time. Each task type therefore carries an `operational` flag, default **false**. The checks above apply only to task types flagged operational; a non-operational task type may hold TBD in any column.

A non-operational task type is **not routed**. Work classified into it falls back to today's routing (`config.ANTHROPIC_MODEL`), exactly as if the kill switch (§9.1) were off for that type alone, and each such fallback is logged so the gap is visible rather than silent.

Flipping a task type to operational is the act that submits it to validation: at that moment every one of its rows must be complete and every rung must resolve, or the system refuses to start. This is the only way a task type becomes routable, so no type can go live on unmeasured data.

If any check fails the system refuses to start. The error lists every broken invariant, naming the task type and the column, so the operator can fix the data before deployment.

**Validation runs on every write, not only at load.** The benchmark table is live-editable (§9.2) and the ladders regenerate at runtime from it, so a check that only ran at startup would let an operator clear a `median_latency_s` on an operational row at 15:00, see routing carry on unchanged, and discover at the next restart — possibly weeks later, possibly mid-incident — that the system will not boot. A validation gap whose blast radius is delayed by an arbitrary interval is worse than one that fails immediately.

So the same six checks run at three moments, with the same code path and the same error text:

| moment | on failure |
|---|---|
| config load | refuse to start |
| any write to the benchmark table or to a versioned config value (§9) | reject the write, name the broken invariant and the column, leave the stored value unchanged |
| flipping a task type to `operational` (§9.2) | reject the flip |

A write is rejected only against the invariants of task types that are *already* operational, plus the type being written. Editing a non-operational type's rows stays free — that is the bootstrap path, and gating it would make the table impossible to fill in.

### 1.2 Readiness, measured 2026-09-15

**No task type can currently go operational, so with the kill switch on, nothing routes.** That is the designed bootstrap state (§1.1), not a fault, but it means the first deliverable is measurement rather than code. Audited against §2.6:

| task_type | rows | accuracy | n | cost | latency | max_context |
|---|---|---|---|---|---|---|
| coding | 4 | 0 | 0 | **4** | 3 | 0 |
| comprehension | 3 | 2 | 2 | **3** | 0 | 0 |
| long-context | 3 | 1 | 1 | **3** | 0 | 0 |
| multi-turn | 2 | 0 | 0 | **2** | 0 | 0 |
| planning | 2 | 0 | 0 | **2** | 0 | 0 |
| reasoning | 4 | 2 | 1 | **4** | 0 | 0 |
| reviewer-gate | 2 | 0 | 0 | **2** | 1 | 0 |
| split-decision | 1 | 0 | 0 | **1** | 0 | 0 |
| voice | 2 | 0 | 0 | **2** | 0 | 0 |

Cost is complete (23/23) as of today. Latency is 4/23, accuracy 5/23, and **`max_context` is 0/23**.

**`max_context` is the cheapest column in the table and nobody has filled any of it.** It needs no benchmark run — it is a published property of each model — and §3 singles it out as the field that decides whether a rung can take a task at all, since a cheaper and more accurate model is still wrong if the input does not fit. This project has already lost time to exactly that failure: a gateway model with a 32,000-token window raised `ContextWindowExceededError` on an obviously small prompt because the *output* budget consumed the whole window. Filling this column is an afternoon of lookups and it unblocks one sixth of every readiness check.

**Shortest path to one operational task type is `coding`**, which already holds 4/4 cost and 3/4 latency: it needs accuracy plus accuracy-`n` for its rows, one more latency figure, and its context windows.

**One scoping decision is unresolved and it changes how much of this is required.** §1.1 demands every *row* of a task type be complete, but §2.6 makes a row with TBD accuracy ineligible — not a ladder candidate at all. Requiring completeness of rows that can never be rungs is work with no consequence. Scoping the invariant to **ladder rows, plus a requirement that at least one exists**, would preserve every guarantee it currently makes while removing that. It is left as written here because narrowing a validity check is a deliberate decision, not a cleanup.

---

## 2. Classifier

`orchestrator.COMPLEXITY_PATTERNS` is extended to emit `(task_type, score, mutates)`.

| pattern | score | task_type | mutates |
|---|---|---|---|
| `architect\|design.*system\|create.*framework` | 4 | planning | False |
| `implement.*multiple\|coordinate.*agent\|orchestrate` | 5 | planning | True |
| `debug.*complex\|trace.*error.*chain\|performance.*bottleneck` | 4 | reasoning | False |
| `write.*test.*suite\|integration.*test\|e2e.*test` | 3 | coding | True |
| `analyze.*code.*review\|refactor.*large\|migrate.*database` | 4 | coding | True |
| `write.*doc.*umentation\|create.*tutorial\|explain.*concept` | 2 | comprehension | True |
| `research.*api.*document\|find.*replacement\|evaluate.*option` | 3 | comprehension | False |
| `read.*file\|list.*directory\|grep.*pattern\|summarize.*log` | 1 | long-context | False |
| `simple\|small\|quick\|minor\|fix.*typo` | 1 | coding | True |

### 2.1 Multi-pattern conflicts

Patterns are ranked by **specificity**, not list order.

**Specificity is defined, not judged.** For a matched alternative (one branch of a pattern's `|`), specificity is the count of **literal characters** in it — every character that is not a regex metacharacter (`.` `*` `+` `?` `|` `(` `)` `[` `]` `\` `^` `$`). So `refactor.*large` scores 15 and `quick` scores 5, and the first wins on the text `quick refactor large module`. Ties resolve by longer matched span in the input; a remaining tie resolves by table order, so the result is always deterministic.

Resolution, per field:

- `task_type` — the most specific matching alternative wins, by the rule above.
- `score` — the **highest** score among all matched patterns wins, not the winning pattern's own. Score feeds the size factor (§5.1) and therefore the deadline; under-budgeting a deadline manufactures a false timeout escalation, which costs a rung. This follows §2.2's doctrine: fail toward the expensive branch.
- `mutates` — if any two matched patterns disagree, the answer is **`True`**.
- Every multi-match logs a warning with the matched pattern set, the computed specificity of each, and the field-by-field winners, so pattern refinement is driven by production data rather than guesswork.

`task_type` and `score` may therefore come from different patterns. That is intended: the type decides which ladder is walked, the score decides how long each rung is given, and the safe answer differs for the two.

### 2.2 Defaults

- Text matching no pattern → `task_type = comprehension`, `mutates = True`.
- Both defaults fail toward the expensive/safe branch.

### 2.3 `mutates` is three-valued

| value | meaning | transport eligible? |
|---|---|---|
| `False` | reads only | yes |
| `side_effecting_read` | changes nothing local, but spends money or consumes external rate limit (e.g. a paid API call in a dry run) | **no** |
| `True` | edits files, git, or database | no |

`side_effecting_read` is blocked from transports for the same reason as `True`, but is **tagged and cost-accounted separately** so a future decision about whether it deserves its own gate has data behind it.

### 2.4 Child nodes are re-classified

A decomposed child is always classified independently. It never inherits `task_type` or `mutates` from its parent. A coding parent may produce a comprehension child, and that child gets comprehension's ladder — and comprehension's oracle coverage, which is none.

### 2.5 Cost basis — production actuals, not benchmark extrapolation

The base design priced models from a benchmark's mean **output** tokens against published rates. Production MTD data supersedes that. Blended rate = MTD spend ÷ MTD tokens, across input, output and cache together.

| model | requests | tokens | MTD € | €/1M | share |
|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 24,544 | 2,250.58M | 0.00 | **0.000** | 87.6% |
| `azure_ai/gpt-5.6-luna` | 1,996 | 209.37M | 5.52 | **0.026** | 8.1% |
| `nvidia/Qwen3.6-35B-A3B-NVFP4` | 933 | 93.51M | — | — | 3.6% |
| `azure_ai/gpt-5.4-mini` (incl. copilot) | 301 | 8.59M | 4.18 | **0.487** | 0.33% |
| `vllm/Qwen3.5-0.8B` | 913 | 7.47M | 0.00 | 0.000 | 0.29% |
| `vllm/Qwen3-0.6B` | 3,208 | 5.39M | 0.00 | 0.000 | 0.21% |
| `azure_ai/gpt-5-mini` | 127 | 0.83M | — | — | 0.03% |
| `azure_ai/gpt-5.6-sol` | 76 | 0.66M | 0.10 | 0.152 | 0.03% |
| (unattributed) | 3,778 | 0.0005M | — | — | ~0% |

#### Correction, measured 2026-09-15: the Anthropic rows exist, and they are the only authoritative ones

An earlier revision of this section stated that Anthropic models carry no production rows and cannot be blended. **That is false, and it inverted this section's conclusion.** Queried against `usage_events` for September:

| model | requests | rows with `cost_basis='list'` | MTD USD | USD/request |
|---|---|---|---|---|
| `claude-sonnet-5` | 25,168 | 77 | 36.68 | **0.00146** |
| `claude-opus-5` | 24,249 | 74 | 98.36 | **0.00406** |

They are the two highest-volume models in the table, and `cost_basis='list'` is the CLI's own reported list price — the only figure in this database that is real billing.

**Every other cost figure in `usage_events` is fictional, and the direction of the error is not conservative.** Of 167,412 rows, **156** carry `cost_basis='list'` — 0.09%. The rest are Anthropic list pricing applied to whichever backend actually served the turn. Sorted by recorded cost per request, the result is absurd on its face:

| model | requests | `list` rows | recorded USD/request | real? |
|---|---|---|---|---|
| `azure_ai/gpt-5.6-luna` | 3 | 0 | 0.554 | no |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 112 | 0 | 0.471 | **no — this model is free** |
| `azure_ai/gpt-5.4-mini` | 9 | 0 | 0.275 | no |
| `claude-opus-5` | 24,249 | 74 | 0.00406 | yes |
| `claude-sonnet-5` | 25,168 | 77 | 0.00146 | yes |

A self-hosted model with no marginal per-token charge is booked at $0.47/request. `bench/bench_rates.json` documents this same failure independently and refuses to price an unrated model rather than call it free.

**So the Anthropic models are the cheapest per request that anyone here has actually measured** — Sonnet at roughly one tenth of the $0.015/request that §2.7 uses to exclude mini. Any argument that they are the expensive option needs a source that is not this table.

#### The Azure source, named (2026-09-15)

The Azure figures come from **gateway billing, not from `usage_events`** — which is why they do not reconcile with this database and why `bench_rates.json`, which only knows public rate cards, records no rate for them. That is the source this section previously failed to name. For luna, MTD: **209,613,193 tokens for €5.52.**

```
luna = €5.52 / 209.613193M tokens = €0.026334 /1M = $0.028472 /1M
     = €0.002766 /request over 1,996 requests = $0.00299 /request
     at 105,017 tokens per request
```

**The currency conversion is now stated rather than implied.** §2.5.1's `$2.99 per 1,000 requests` against €5.52 over 1,996 requests implies **EUR→USD = 1.0812**, and every USD figure derived from a euro one in this document uses that rate. It is an assumption with a date on it, not a constant; a materially different rate changes the ceiling comparisons below.

`bench_rates.json` still holds no Azure rate and should keep refusing to invent one — its scope is public rate cards. The gateway billing figures belong in the §2.6 table, which is where the ladder generator reads from.

The list rates remain correct as list rates: opus 5.00/25.00, sonnet 2.00/10.00, haiku 1.00/5.00, fable 10.00/50.00 USD per 1M in/out. **Comparing a blended Azure rate to an Anthropic list output rate is not like-for-like** and must not be done without saying so — but note that the reverse comparison, the one this section previously made, was worse: it compared a fictional Azure figure against a real Anthropic one.

#### 2.5.1 Real requests are ~200× larger than benchmark tasks

| model | tokens per request | USD per 1,000 requests |
|---|---|---|
| `vllm/Qwen3.6-35B` | 91,696 | **0.00** |
| `azure_ai/gpt-5.6-luna` | 104,895 | **2.99** |
| `azure_ai/gpt-5.4-mini` | 28,538 | **15.00** |
| `azure_ai/gpt-5.6-sol` | 8,684 | 1.42 |

The base design's `$0.23 per 1,000 tasks` for luna assumed 495 output tokens per response. Production luna requests average **104,895 tokens**. The per-task cost figures in the original design are therefore not wrong arithmetic — they price a task shape that does not occur here, and orchestrator leaves will resemble production, not the benchmark.

**Consequence for the ladder:** the ordering survives — free is free, luna is ~5× cheaper per request than mini — but the *magnitudes* do not. `BUDGET_USD = 1.00` per **tree** (§5 — one pool shared by every leaf, not one budget per goal) buys roughly **335 luna requests or 67 mini requests** across the whole tree, not the thousands the original figures implied. Re-check that cap before implementation.

#### 2.5.2 Attribution gaps to close first

- **€1.13 of MTD spend (10%) matches no model above** — €0.83 of `gpt-4o-mini` meters plus €0.30 "Others".
- **`nvidia/Qwen3.6-35B` carries 93.51M tokens with no cost attribution.** The design treats only `vllm/*` as free; if this deployment is billed, it belongs in the cost model.
- **`azure_ai/gpt-5-mini` has 127 requests and no billing line.**
- **3,778 requests (10.5%) are unattributed**, carrying 510 tokens between them — requests logging without token attribution. §10's entire measurement story runs on `usage_events`; this gap must be closed before the ≥70% target in §10.1 can be trusted.

### 2.6 Model benchmark table — each model against each task type

Holding each model against each task type, with columns: measured accuracy, sample size, cost per request, latency, and context window. A row per model per task type.

| model | task_type | accuracy | n | cost_per_1M_tokens | median_latency_s | max_context |
|---|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | coding | TBD | 6* | 0.0000 | 22.4 | TBD |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | long-context | 100% | 10 | 0.0000 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | coding | TBD | 3* | 0.0285 | 12.8 | TBD |
| `azure_ai/gpt-5.6-luna` | long-context | TBD | — | 0.0285 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | comprehension | TBD | — | 0.0285 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | reasoning | TBD | TBD | 0.0285 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | voice | TBD | — | 0.0285 | TBD | TBD |
| `azure_ai/gpt-5.4-mini` | coding | TBD | — | 0.5261 | TBD | TBD |
| `azure_ai/gpt-5.4-mini` | reasoning | 86% | TBD | 0.5261 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | multi-turn | TBD | — | 0.0285 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | planning | TBD | — | 0.0285 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | reviewer-gate | TBD | 9* | 0.0285 | 11.1 | TBD |
| `claude-sonnet-5` | coding | TBD | 7* | 1.5709 | 10.7 | TBD |
| `claude-sonnet-5` | long-context | TBD | — | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | comprehension | 100% | 2 | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | reasoning | 75% | 2 | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | voice | TBD | — | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | multi-turn | TBD | — | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | planning | TBD | — | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | split-decision | TBD | — | 1.5709 | TBD | TBD |
| `claude-sonnet-5` | reviewer-gate | TBD | — | 1.5709 | TBD | TBD |
| `claude-opus-5` | comprehension | 50% | 2 | 3.6082 | TBD | TBD |
| `claude-opus-5` | reasoning | TBD | TBD | 3.6082 | TBD | TBD |

**An `n` marked with `*` is a latency sample, not an accuracy sample.** Four rows carry measured `median_latency_s` from `bench/pipeline_ab.py` (2026-09-15) while their `accuracy` is still TBD. The `n` column means *accuracy* sample size everywhere else, and §2.6's blocking constraint on Opus depends on that reading, so the two must not be confused: **no row in this table yet carries a measured accuracy sample size for coding.** A row needs both before its task type can go operational.

**Provenance of the `cost_per_1M_tokens` column, which comes from three different places.** Anthropic figures (sonnet 1.5709, opus 3.6082) are blended from `usage_events` rows carrying `cost_basis='list'`. Azure figures (luna 0.0285, mini 0.5261) come from **gateway billing**, which is a separate source from this database and the reason they never reconciled with it (§2.5). `0.0000` for the self-hosted model is a property of the deployment, not a measurement. Every one of them is a **rate**, independent of request size — which is the whole point of the unit change, since the previous per-request column silently encoded how large each model's historical jobs happened to be (§2.7).

Azure values carry one dependency the others do not: the **EUR→USD rate of 1.0812** stated in §2.5. A materially different rate moves luna and mini against the Anthropic figures and can reorder a ladder, so the rate is versioned with the table (§9).

#### Ladder eligibility — one definition, used everywhere

A model is **ladder-eligible** for a task type when all three hold:

1. it has a row for that `(model, task_type)` pair;
2. that row's `accuracy` is a measured value, **not TBD**;
3. it survives the cost ceiling (§2.7).

Everything downstream is expressed in terms of this one predicate — the ladder generator (§3), the reasoning constraint below, and the model-speed multiplier's reference set (§5.1). There is no second notion of "candidate".

**An unmeasured row is not a candidate.** The generator's rule is "skip any model measured worse than the current rung"; a TBD accuracy is not *measured worse*, it is not measured at all, and the two must not be conflated. Treating TBD as pass-through would let cheapest-first put an unmeasured model at rung 0 — the precise failure the reasoning constraint below exists to prevent, reintroduced generically. So a TBD accuracy removes the row from consideration outright.

The consequence is deliberate and worth stating plainly: **a task type whose rows are all TBD has an empty ladder, and an empty ladder means the type cannot be operational** (§1.1). It falls back to today's routing until something is measured. The table above ships mostly TBD, so on day one almost nothing routes. That is the intended bootstrap state, not a defect — measurement is what turns routing on, one task type at a time.

**A model with no row for a task type is not a candidate either** — condition 1 above. This is the mechanism, not a separate rule, behind "only `coding` and `long-context` start free" (§3, §4.1): the free model holds rows for exactly those two types, so it is not a candidate anywhere else. Adding a `vllm/*` row for `planning` with a measured accuracy would make the free model planning's rung 0 on the next read, with no other edit. That is intended, and it is the only way it can happen.

**Blocking constraint:** do not accept Opus as the reasoning entry rung until `n` is recorded for every other **ladder-eligible** model on the reasoning task type. Opus's own reasoning row is unmeasured, so promoting it would rank an unknown against small samples that may not survive re-measurement. The constraint is scoped to ladder-eligible models on purpose: a cost-excluded model (mini, §2.7) can never be a rung, so blocking Opus on measuring it would make the constraint unsatisfiable by anything that matters.

**Storage.** This table is a **database table**, not a constant in source. One row per `(model, task_type)` pair, with the five measured columns plus `updated_at`. It is what §9.2's editable matrix reads and writes, it is what the benchmark job (§10.2) writes its results into, and it is what the ladder generator (§3) reads at runtime. There is no second copy: the markdown above is a snapshot of the table's contents at the time of writing, not the source of truth. A benchmark run that writes results anywhere else has not landed them.

Every row is a first-class datum. Ladders are generated at runtime by walking this table — sort cheapest-first per task_type, skip any model measured worse than the current rung on that task_type. The table is the source of truth; §3 is just the output. Cost per 1M tokens is precomputed from each model's own billing source (§2.5) so the ladder algorithm is a two-field sort-and-filter. It is per token, not per request: a per-request figure sorts the historical workload mix rather than the models (§2.7).

### 2.7 Per-model cost ceiling

A configurable threshold automatically excludes any model whose cost per request crosses it.

**As previously applied:** mini at ~$0.015/request exceeded the ceiling implied by the $1.00 tree budget and expected leaves per tree (§5), so mini was excluded from all ladders, leaving coding and long-context three-rung (`vllm → luna → sonnet`) and removing mini from multi-turn, planning, voice and reasoning. **That exclusion does not survive the correction below** — measured per token, mini is cheaper than Sonnet, which the same ladders keep. The shapes in §3 still assume the exclusion and must be re-derived once the threshold is restated.

#### Cost per request measures the workload, not the model

Every model here is now priced (§2.5). With the figures on one basis, the ceiling's defect is visible, and it is not a missing number — it is the **unit**.

| model | $/1M tokens | $/request | tokens/request | one 12,000-token call |
|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 0.0000 | 0.00000 | 91,696 | $0.00000 |
| `azure_ai/gpt-5.6-luna` | **0.0285** | 0.00299 | 105,017 | **$0.00034** |
| `azure_ai/gpt-5.4-mini` | **0.5261** | 0.01501 | 28,538 | **$0.00631** |
| `claude-sonnet-5` | **1.5709** | 0.00146 | 928 | **$0.01885** |
| `claude-opus-5` | **3.6082** | 0.00406 | 1,124 | **$0.04330** |

The two orderings disagree, and they disagree about the decision this section exists to make:

- **By cost per request:** sonnet (0.00146) < luna (0.00299) < mini (0.01501). Mini is the most expensive, so mini is excluded.
- **By cost per token:** luna (0.0285) < mini (0.5261) < sonnet (1.5709) < opus (3.6082). **Mini is 2.99x cheaper than Sonnet**, which the ladders keep as a rung on six task types.

**The per-request ordering is an artifact of who sent what.** Mini's historical requests average 28,538 tokens; Sonnet's average 928 — 31x smaller. Mini looks expensive per request because it was handed large jobs, not because it charges more. Sonnet looks cheap per request because it was handed small ones. Neither fact says anything about what either model will cost on an orchestrator leaf, because a leaf's size is set by the task, not by the model that happens to take it.

**So the ceiling is evaluated per token, normalised to the task type's own expected size:**

```
effective_cost_per_task(model, task_type)
    = cost_per_1M_tokens(model)
    x expected_tokens(task_type)                 # per-call floor (§2.7) + task size

cost_per_1M_tokens =
    blended over rows with cost_basis='list'     Anthropic models
    gateway billing MTD spend / MTD tokens       Azure models (§2.5)
    0.00                                         self-hosted
    otherwise: UNPRICED -> ineligible (§2.6)
```

A model with no rate from any of those sources is still ineligible, still not estimated into eligibility, and still not treated as free. That principle was right; only its unit was wrong.

**Consequences, and two of them change the ladders:**

1. **`vllm → luna → sonnet` is correct** and survives unchanged. Per token the order is 0 → 0.0285 → 1.5709, monotonically increasing, which is what cheapest-first requires.
2. **The mini exclusion is backwards.** Mini is cheaper per token than a model the design keeps. Whether mini belongs in a ladder is now an *accuracy* question (its only measured row is reasoning, 86%, n unrecorded) — not a cost one. The exclusion must be re-derived or withdrawn, and §3's ladder shapes depend on which.
3. **The threshold itself must be restated in per-token terms.** `$0.015/request` is not a threshold that can be applied to the table above; it is a number that only sorts one historical workload mix.

#### The re-derived ceiling: a rate alone is not the question, the rung is

A single per-model threshold cannot express the constraint this section exists to enforce, and that is why every version of it has felt arbitrary. **The budget is per tree (§5); a model's contribution to it depends on how often it is reached.** An expensive model at the top of a ladder is cheap because it rarely runs; a cheap model at rung 0 runs on every leaf. A scalar `$X per model` throws away the one variable that decides the answer.

So the ceiling is a check on expected tree cost:

```
tree_cost = leaves_per_tree
          x tokens_per_leaf
          x SUM over rungs of [ P(reach rung) x rate(model at that rung) ]

admissible <=> tree_cost <= BUDGET_USD
```

Measured inputs, 2026-09-15 (`bench/pipeline_ab.py`, 6 leaves):

| input | value | confidence |
|---|---|---|
| `tokens_per_leaf` | 59,460 | measured, benchmark-shaped tasks |
| `P(reach rung 1)` | 0.50 | measured, **n=6** |
| `P(reach rung 2)` | 0.17 | measured, **n=6** |
| `leaves_per_tree` | 40 | **assumed** — this is `MAX_NODES`, an upper bound nobody has measured |
| `BUDGET_USD` | 1.00 | §5 |

Cost of placing each model at each rung, for a whole tree:

| model | $/1M | as rung 0 | as rung 1 | as rung 2 | affordable at |
|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B` | 0.0000 | $0.000 | $0.000 | $0.000 | any |
| `azure_ai/gpt-5.6-luna` | 0.0285 | $0.068 | $0.034 | $0.011 | any |
| `azure_ai/gpt-5.4-mini` | 0.5261 | $1.251 | $0.626 | $0.209 | **rung 1 or 2** |
| `claude-sonnet-5` | 1.5709 | $3.736 | $1.868 | $0.623 | **rung 2 only** |
| `claude-opus-5` | 3.6082 | $8.582 | $4.291 | $1.430 | **none** |

**This replaces the `$0.015/request` threshold.** Note what it does to the mini question: mini is not excluded and never should have been on cost alone — it is admissible at rung 1 or 2, and its place in a ladder is decided by accuracy, exactly as §2.7 concluded above.

**Three ladder shapes in §3 do not survive this, and they are the ones that put an expensive model low:**

| ladder | position that fails | tree cost at that position |
|---|---|---|
| multi-turn, planning, voice, reviewer-gate | `claude-sonnet-5` at **rung 1** | $1.868 |
| comprehension | `claude-sonnet-5` at **rung 0** | $3.736 |
| comprehension | `claude-opus-5` at **rung 1** | $4.291 |
| split-decision | `claude-sonnet-5` at **rung 0** | $3.736 |

`coding` and `long-context` — the two three-rung ladders, and the only two that start free — are the only ones that fit unchanged. **Comprehension and split-decision have no affordable ladder at all** on these inputs, and under §1.1 that makes them non-operational until one of the assumptions changes or a cheap rung is measured for them. Luna already holds a comprehension row awaiting accuracy, which is the cheapest way to fix it.

**Opus is affordable at no rung of any ladder.** Given §2.6's blocking constraint already refuses it the reasoning entry rung on evidence grounds, and its only measured row is comprehension at 50% (n=2), the case for Opus appearing anywhere in this design is now weak on both counts.

**What would change these conclusions, in order of leverage:**

1. **`leaves_per_tree`.** It is the only pure assumption here and it scales everything linearly. At 10 leaves rather than 40, Sonnet becomes admissible at rung 1 and comprehension's ladder survives. **Measuring actual leaves per tree is the single highest-value number still missing**, and it is cheaper to obtain than any benchmark row.
2. **`tokens_per_leaf`.** Measured on benchmark tasks; §2.5.1 warns production requests run far larger (luna's average 105,017 tokens against the ~17,000/call measured here). A production-shaped leaf makes every figure above worse, not better.
3. **The reach probabilities**, at n=6, are the weakest evidence in the table and the easiest to improve.

#### The per-call floor: a gate is not cheap because its rung is

Measured 2026-09-15 (`bench/pipeline_ab.py`): **every CLI call carries ~12,000 input tokens of system prompt and tool definitions before the task is appended**, and gateway models return `cache_read = 0` where Anthropic models return 22k–54k cached. A five-stage leaf therefore pays that floor five to nine times.

In the A/B run, the pipeline arm moved **348,278 input tokens across 27 calls** against the single-call arm's **75,882 across 6** — 4.6x the volume for work that was measured *less* correct (5/6 against 6/6).

So `cost_per_1M_tokens` in §2.6 is a rate, **not** the cost of a stage. The cost of a leaf is:

```
leaf_cost ≈ sum over every stage of (per_call_floor + task_tokens) x rate(stage model)
```

A design that routes generation to a free model and then runs three gates on a paid one has moved the spend, not removed it. Any budget arithmetic (§5) that counts only generation understates a five-stage leaf by roughly the number of gates, and the free-rung target (§10.1) measures the stage whose cost was already zero.

---

## 3. Escalation ladders

Ladders are generated at runtime by walking the benchmark table from §2.6. The generator:

1. takes the **ladder-eligible** models for the task type (§2.6) — a row exists, its accuracy is measured, and it survives the cost ceiling;
2. sorts them cheapest-first on `effective_cost_per_task` — the model's rate times the task type's expected size (§2.7), never a per-request figure;
3. walks that order, skipping any model **measured worse** than the current rung on that task type.

Step 1 is what makes step 3 well defined: by the time the generator compares accuracies, every candidate has one. An unmeasured row never reaches the comparison, so "measured worse" is never asked of a TBD.

The table below is a **snapshot of what this computation is expected to produce once the table is measured** — it is not itself input, and it is not reachable from today's data. Every accuracy marked TBD in §2.6 removes that model from eligibility now, so most of these ladders are empty until measurement lands and their task types stay non-operational (§1.1) in the meantime.

| task_type | attempt 1 | attempt 2 | attempt 3 |
|---|---|---|---|
| coding | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` |
| long-context | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` |
| multi-turn | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | — |
| planning | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | — |
| comprehension | `claude-sonnet-5` | `claude-opus-5` | — |
| voice | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | — |
| reasoning | TBD — **Luna must be benchmarked on reasoning** before a rung is set | — | — |
| split-decision | `claude-sonnet-5` | — | — |
| **reviewer-gate** | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` (see §4.3) | — |

**All three review gates share the `reviewer-gate` ladder** — stages 3, 4 and 5 climb the same rungs, so there is no separate `security-gate` task type and the table keeps nine rows. What differs is what happens at the top: a security rejection surviving the gate's top rung goes to a human rather than failing the leaf (§4.5).

Reasoning has no rung until Luna is benchmarked on that type. The existing 86%/75% accuracy figures are from a small sample (n=—) and must be re-verified against production request shapes before any rung is set. The comprehension ladder is correct as-is: sonnet → opus → —, because no measured model beats sonnet on comprehension.

**Voice is latency-bound, not accuracy-bound.** A spoken exchange is the most latency-sensitive path in the product, so the voice ladder starts at Luna and climbs only to Sonnet; Opus is not a voice rung at any accuracy. Voice stays non-operational (§1.1) until both its rows carry a measured `median_latency_s`, because a ladder ordered on cost alone is the wrong ordering for the one task type where latency is the binding constraint.

Only `coding` and `long-context` start free (§4.1).

**Hover tooltip on chosen model:** each rung in the settings page displays the model name with a hover tooltip showing all five measured fields for the current task type — accuracy, sample size (`n`), cost per request, median latency, and **context window** (`max_context`). This lets a reviewer see why a model was chosen without leaving the page. Context window is in the tooltip because it is the field that decides whether a rung can take the task at all: a model that is cheaper and more accurate is still the wrong rung if the input does not fit.

**Excluded outright:** `vllm/Qwen3.5-0.8B` (23% correct), `claude-haiku-4-5` (dearer and slower than Sonnet), `claude-fable-5` ($23.15/1k). **Excluded by cost ceiling (§2.7):** `azure_ai/gpt-5.4-mini` (and by extension all variants).

---

## 4. The coding pipeline — five stages

A coding leaf above the trivial floor passes through five stages in order. Each is an independent pass/fail gate.

### 4.1 Stage 1 — Generation

Produced at whatever rung the ladder currently points to. Attempt 1 for `coding` and `long-context` is the free model with `TIER0_DEADLINE`.

### 4.2 Stage 2 — Oracle check

Execution verification only: does the produced code parse, import, compile, or pass the test it was asked to satisfy. **QA and regression testing are not part of this stage** — they are stage 4 (§4.4).

**The stage requires executable output, and is skipped when there is none.** A task whose product is prose — a log summary, a directory listing, a research answer — has nothing to parse or compile, so there is no verification to perform. It is skipped rather than run, because an oracle that cannot fail is worse than an absent one: it reports a pass that the pipeline then treats as evidence. See §4.7 for what this means on read-only tasks, where it is why stage 3 becomes the only real gate.

Failure of the check escalates the generator one rung.

**Oracle infrastructure failure** (sandbox crash, harness error) is a distinct signal from "code failed verification". It escalates the same way but is tagged separately in the usage row, so production accuracy metrics are not polluted by tooling failures.

### 4.3 Stage 3 — Reviewer gate

A model checks the generated code against the original task description and acceptance criteria for **intent match**, not syntactic correctness.

- Entry rung: `azure_ai/gpt-5.6-luna` — the floor, not a fixed assignment.
- On rejection, **the generator escalates one rung**; the reviewer stays at Luna and re-reviews the new output.
- The reviewer itself only climbs (`luna → sonnet`) if it keeps rejecting output from the generator's **top** rung — that indicates reviewer miscalibration, not bad code.

### 4.4 Stage 4 — QA / functional regression gate

Runs the relevant regression or functional suites. Separate from stage 2 so a regression failure and a compile failure are distinguishable signals.

Failure escalates the generator, same path as the reviewer gate.

### 4.5 Stage 5 — Security review gate

Checks for command injection, unsafe file operations, secrets exposure, and similar.

**Failure does not simply escalate to a smarter model.** It routes back through the pipeline with the specific vulnerability flagged, because a security defect needs a targeted fix, not a blind rewrite from a larger model.

**Security re-runs are capped at 2 cycles** (generation → security review → fix → security review). After that the leaf fails with a human-flag. A security vulnerability fix can introduce a new vulnerability (replacing an unsanitized `os.system()` with an unsanitized `subprocess.run()` is common); infinite recursion through the pipeline is prevented by the cap, not by the tree depth or node limits which do not track cycles.

#### The gate climbs too, and this one is measured

**A miscalibrated security gate cannot be escaped by escalating the generator, because escalating changes who wrote the code and not who is judging it.** §4.3 already gives the reviewer a climb path for exactly this reason. Stage 5 previously had none, and that gap loses correct work.

Measured 2026-09-15 (`bench/pipeline_ab.py`, task `coding-algo`): the security gate rejected the output of **all three rungs in succession** — the free model, luna, and Sonnet — with the same objection each time, that an LRU cache's `__repr__` "exposes cached keys and values, potentially leaking secrets." That is not a vulnerability. The single-call arm produced materially the same code and it passed execution verification at 100%. The leaf burned its entire ladder and failed **holding correct code**.

So stage 5 takes the same rule as stage 3, with one addition that stage 3 does not need:

- The gate's entry rung is the floor, not a fixed assignment.
- On rejection, the **generator** escalates one rung and the gate re-reviews, as before.
- **If the gate rejects output from the generator's top rung, the gate itself climbs one rung and re-reviews that same output.** Rejecting the best generator available is evidence about the gate, not about the code.
- **A security rejection that survives the gate's own top rung is escalated to a human, not recorded as a failed leaf.** This is the addition: a false reject here is indistinguishable from a true one without judgement, and discarding correct work silently is the worse of the two errors. §10's circuit breaker is the aggregate form of the same signal; this is the per-leaf form.

A gate that rejects at every rung is reported with the rejection text at each rung attached, so the miscalibration is legible from the record rather than requiring a re-run to reproduce.

### 4.6 Blast-radius check

The classifier is a first pass on free-text. A task matching `simple|small|quick|minor|fix.*typo` can still produce a large patch if the target file is big, or cascade through imports, or change config that affects other modules.

After generation completes, a post-generation check counts the number of files modified (or the diff size, whichever the harness exposes). The thresholds are configured in the pipeline settings:

| threshold | effect |
|---|---|
| `≤ MAX_FILES_TRIVIAL` (default 3) | stages 3–5 skipped (original trivial bypass) |
| `> MAX_FILES_TRIVIAL` | stages 3–5 **run** regardless of classifier score |

A write task that modifies more than the trivial count always passes through the full pipeline — the classifier's score-1 match is overridden by measured blast radius, not guessed at from the prompt text.

### 4.7 Stages 3–5 on read-only tasks

**A task with `mutates=False` runs stages 1–3 and skips stages 4 and 5.**

| stage | runs on `mutates=False`? | why |
|---|---|---|
| 1 — generation | yes | the work itself |
| 2 — oracle check | **only if the output is executable** | §4.2 defines the oracle as parse / import / compile / pass-the-test. A task whose output is prose — a log summary, a directory listing, a research answer — has nothing to execute, so the stage is skipped rather than run as a no-op that always passes |
| 3 — reviewer gate | **yes, always** | intent match is where a read-only task fails: a wrong answer, confidently delivered, is the whole risk |
| 4 — QA / regression | **no** | nothing was changed, so there is no regression surface to test |
| 5 — security review | **no** | the defects this gate looks for — command injection, unsafe file writes, secrets written out — all require a write |

The reasoning is that stages 4 and 5 both check for consequences of *changing* something. A task that changes nothing cannot produce them, so running those gates spends two model calls per leaf to confirm an invariant that already holds structurally.

**Stage 3 is the floor for a read-only task, and no other rule may remove it.** The failure mode of a read-only task is entirely a stage-3 failure mode: it returns something plausible and wrong, and nothing downstream catches it. Stage 2 cannot catch it — where it runs at all, it checks that an answer executes, not that it answers the question asked, and on prose output it does not run. Stages 4 and 5 are structurally inapplicable. So stage 3 is not one gate among several here; it is the **only** gate. A read-only leaf that reaches the end with stage 3 skipped has been through no verification whatsoever, and the cheapest way to produce that outcome is to let another stage-subtraction rule reach it first — see the precedence rule in §4.8.

This rule applies to `mutates=False` only. **`side_effecting_read` is not covered by it** — it takes the full five stages, the same as `True`. It spends money or consumes an external rate limit, so it has real consequences to review even though it writes no local file, and §2.3 already refuses it a transport for that reason.

### 4.8 Trivial-task bypass

A score-1 task runs a reduced pipeline; the full five stages apply to anything scoring above that floor. Note that **two patterns score 1, and they differ in `mutates`** — `simple|small|quick|minor|fix.*typo` is a write, `read.*file|list.*directory|grep.*pattern|summarize.*log` is a read — so the bypass must say which it means:

| score-1 task | stages skipped | stages run |
|---|---|---|
| `mutates=True` (a small write) | 3, 4, 5 | 1, 2 |
| `mutates=False` (a read) | 4, 5 | 1, 3 (and 2 if output is executable) |

**The bypass is scoped to writes.** It removes stages 3–5 from a task with `mutates=True`. On a task with `mutates=False` it removes stages 4 and 5 only, and **never stage 3** (§4.7).

This scoping is the whole rule, and without it the design has a hole big enough to swallow its most common read task. The classifier's only `long-context` pattern — `read.*file|list.*directory|grep.*pattern|summarize.*log` — is **score 1 and `mutates=False` simultaneously**, so it matches the trivial bypass and the read-only rule at once. Under an unscoped bypass it would run stage 1, then a stage 2 that does not execute on prose output (§4.7), then nothing: a leaf with no verification at all, reached by the most frequent read pattern in the table. The bypass exists because a typo fix is cheap to verify by oracle — that argument is about writes, and it does not transfer to a read whose oracle is vacuous.

**Precedence between §4.6, §4.7 and §4.8.** Three rules can each subtract stages, so the order they resolve in is fixed:

1. **Blast radius (§4.6) first.** It is the only rule based on what the task *did* rather than what its text predicted, so it overrides the trivial bypass: a score-1 task that modified more than `MAX_FILES_TRIVIAL` files runs stages 3–5.
2. **Trivial bypass (§4.8) next**, if blast radius did not override it — stages 3–5 skipped on a write, stages 4 and 5 only on a read.
3. **Read-only (§4.7) last**, applied to whatever survives — stages 4 and 5 removed for `mutates=False`, and stage 3 restored if any earlier rule took it.

The net effect:

| task | stages run |
|---|---|
| non-trivial write | 1, 2, 3, 4, 5 |
| trivial write | 1, 2 |
| trivial write, blast radius over threshold | 1, 2, 3, 4, 5 |
| non-trivial read, executable output | 1, 2, 3 |
| non-trivial read, prose output | 1, 3 |
| trivial read, prose output | 1, 3 |

Stage 3 appears in every read row. That is the invariant: **no combination of rules produces a read-only leaf without a reviewer gate.** Step 3 is phrased as "restored" rather than "not removed" so the invariant holds no matter what order a future rule is added in.

Rule 1 never fires on a read-only task — its blast radius is zero by definition — so the override exists only for writes that were misclassified as trivial.

### 4.9 Context passed to every gate

Stages 3, 4 and 5 each receive the **full** bundle, never a subset:

1. the original task description
2. the acceptance criteria
3. the code under review
4. every prior gate's rejection reason

A gate judging code without intent context reproduces the oracle's blind spot.

---

## 5. Attempts, timeouts, and caps

| control | value | scope |
|---|---|---|
| `MAX_ATTEMPTS` (generation) | 3 | per leaf |
| `MAX_ATTEMPTS` (each review gate) | 1 | per gate, per leaf |
| `MAX_DEPTH` | 3 | tree |
| `MAX_CHILDREN` | 4 | per node |
| `MAX_NODES` | 40 | tree |
| `BUDGET_USD` | 1.00 | **tree** (all leaves share one pool) |
| `MAX_SUBAGENTS_PER_LEAF` | 12 | whole pipeline |
| combined latency ceiling | 600 — **placeholder, see §5.1** | per leaf, all five stages |

Attempt counts are **per gate, not shared pipeline-wide**. A review gate rejecting repeatedly points at bad generation, so its own cap is 1 — the retry happens at the generator, not at the reviewer.

### 5.1 Timeouts

The deadline for one attempt is three factors multiplied together:

```
effective_deadline = per_type_baseline × size_factor × model_speed_multiplier
```

None of the three is a hardcoded table of every combination — each is derived from something already recorded, which is what keeps this from becoming a deadline × model × size matrix nobody maintains.

**Per-type baseline.** What the task type costs on a mid-sized instance of that task, on the fastest model measured for it.

```
TIER0_DEADLINE = {
    "long-context": 45,   # seconds
    "coding":       90,
}
```

A type absent from this map never uses the free rung. An unknown type must receive the **longest** deadline, never the shortest.

**Size factor** — from the classifier's complexity score (§2), which is the only size signal available before generation starts. Score 3 is the reference point, so a mid-sized task gets exactly the baseline:

| complexity score | size factor |
|---|---|
| 1 | 0.5 |
| 2 | 0.75 |
| 3 | 1.0 |
| 4 | 1.5 |
| 5 | 2.0 |

**Model-speed multiplier** — **derived from the measured latency in the §2.6 benchmark table, never set by hand.** For a given task type, take the lowest `median_latency_s` recorded across the **ladder-eligible** models for that task type (§2.6); that model is the reference and its multiplier is **1.0**. Every other model's multiplier is its own `median_latency_s` divided by that reference:

```
multiplier(model, task_type) = median_latency_s(model, task_type)
                             ÷ min(median_latency_s(ladder-eligible, task_type))
```

**The reference set is ladder-eligible models, not every row.** A model that is cost-excluded or unmeasured can never be a rung, so letting it set the 1.0 reference would shrink the deadline of every model that *can* be a rung — a model nobody will ever run would be quietly setting the clock for the ones that do. The same predicate that builds the ladder defines its reference, so the two cannot disagree.

So the fastest model on a task type always gets exactly the baseline, and a model measured three times slower gets three times the time. Because the input is the same table the ladders are generated from, a re-benchmark (§10.2) updates the deadlines in the same act that updates the rung order, and the two can never drift apart.

A task type with any unmeasured `median_latency_s` cannot compute this, which is one of the reasons such a type stays non-operational (§1.1).

**Gates carry deadlines too, from the same formula.** The three model-backed gates (§4.3–4.5) are sub-agent calls like any other, so each gets `per_type_baseline × size_factor × model_speed_multiplier` computed with **the gate model's** multiplier and the leaf's own task type and score. Stage 2 is execution, not a model call, so it carries the sandbox's own timeout rather than this formula. Without gate deadlines the combined ceiling below has nothing to be reconciled against, which is how it came to be set independently of the work it bounds.

#### The combined ceiling and the attempt budget must be reconciled

`effective_deadline` governs a single attempt. The combined latency ceiling governs all five stages of a leaf together. **As currently specified the two contradict each other, and the ceiling loses.**

The worst-case path for a leaf is every generation attempt running to its deadline, then every gate running to its own:

```
worst_case = baseline x size_factor x [ sum(m_rung) over MAX_ATTEMPTS
                                      + sum(m_gate) over the 3 model gates ]
```

For a `coding` leaf at score 4 — `analyze.*code.*review`, `refactor.*large`, `migrate.*database`, none of them exotic — the base unit is `90 x 1.5 = 135s`, and there are six deadline-bearing steps: three generation attempts plus three model gates. **Even under the most generous possible assumption, that every model is exactly as fast as the fastest and every multiplier is 1.0, the worst case is `135 x 6 = 810s`.** The ceiling is 600. Real multipliers are above 1.0 for every rung above the free one, so the true figure is higher.

The consequence is not a slow leaf; it is a leaf killed after spending on five stages and producing no verdict, on a task type the classifier routes by default. A score-3 coding leaf fits at `90 x 6 = 540s`; score 4 and above does not. The ceiling therefore truncates exactly the large refactors and migrations that most need the full pipeline.

**So the ceiling is not an independent constant.** One of three must hold, and the choice is recorded here rather than left to whoever notices first:

1. **Derive the ceiling from the budget** — set it to the computed worst case for the most expensive operational task type, rounded up. This is the default and keeps every leaf that passes its per-attempt deadlines.
2. **Shrink the attempt budget** — reduce `MAX_ATTEMPTS` for the affected task type until the worst case fits a fixed ceiling. Costs a rung of escalation.
3. **Accept truncation deliberately**, with the ceiling documented as a hard spend cap that will cut long leaves short, and the rate of such cuts monitored (§10).

`600` in the table above is a **placeholder that satisfies none of the three**, and it is marked as such until latencies are measured and the arithmetic can be run for real.

#### Run for real, 2026-09-15 — and the ceiling is too low

Coding latencies are now measured (§2.6), so the worst case above is no longer hypothetical. **The free model is the slowest thing in the ladder**, which is the fact that decides this:

| model | measured `median_latency_s` | multiplier |
|---|---|---|
| `claude-sonnet-5` | 10.7 | **1.00** (reference — fastest ladder-eligible) |
| `azure_ai/gpt-5.6-luna` | 12.8 | 1.20 |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 22.4 | **2.09** |
| luna as a gate | 11.1 | 1.04 |

```
multiplier sum = generation (2.09 + 1.20 + 1.00) + 3 gates (3 x 1.04)
               = 4.29 + 3.11 = 7.40

score 3 (size 1.0):  90 x 1.0 x 7.40 =   666s   vs 600s ceiling — over by 66s
score 4 (size 1.5):  90 x 1.5 x 7.40 =   999s   vs 600s ceiling — over by 399s
```

**Both fail.** A `coding` leaf at score 3 — the ordinary case, `write.*test.*suite` — cannot complete its worst-case path inside the ceiling, and score 4 misses by two thirds. Option 1 of the three above therefore sets the ceiling at **≥1,000s** to cover score 4, or option 2 cuts `MAX_ATTEMPTS` to 2 for coding, which brings score 3 to 478s and score 4 to 717s.

Note what drives it: the free rung is **2.09x slower than the model it exists to avoid**. Its multiplier alone spends 188s of a score-3 leaf's budget. The free tier buys cost, and it is charged for in latency — §10.1's free-rung target measures the cost side of that trade while the ceiling enforces the other, and the two have never been reconciled against one set of numbers until now.

**Startup validation (§1.1) checks this.** For every operational task type, the computed worst case is compared against the ceiling, and a ceiling below it fails the same way a blank field does — at load, naming the task type and both numbers. The check needs measured `median_latency_s` values, which an operational task type already guarantees.

**How the ceiling is enforced.** It is evaluated **before each stage starts**, never mid-stage. If the elapsed time plus the next stage's deadline would exceed the ceiling, the leaf stops there. Interrupting a stage in flight would pay for a model call and discard its verdict, which is the most expensive possible way to save time.

A leaf stopped this way emits a **distinct signal, `latency_ceiling_exhausted`** — not a timeout (§6). A timeout says a model was too slow and escalating to a different rung may help; a ceiling exhaustion says the leaf ran out of total budget and escalating cannot help, because a higher rung is slower. Conflating them would make the system respond to a budget problem by spending more.

Every sub-agent call additionally carries a **strict hard timeout independent of the gate cap**, so a hung sub-agent cannot silently stall a leaf.

### 5.2 Termination guard

A child's complexity score must be **strictly less** than its parent's. A decomposition returning children scoring equal or higher forces them to execute instead of decomposing. Without this the tree grows to `MAX_DEPTH` spending the budget on planning and executing nothing.

---

## 6. Failure signals

Any one of these escalates one rung:

| signal | source | note |
|---|---|---|
| `{"type": "error", ...}` frame | `runner.py:171-177` | **does not raise** — code that only catches exceptions reads it as a successful empty turn |
| model id is `<synthetic>` | `classification.py:611` | no real completion |
| empty text | `runner.py:277` | observed specifically on small gateway models |
| deadline expiry | `effective_deadline` (§5.1: baseline × size factor × model-speed multiplier) | slowness only |
| oracle rejection | §4.2 | must be built |
| oracle infrastructure error | §4.2 | tagged separately |
| reviewer / QA / security rejection | §4.3–4.5 | each tagged with its own gate |

**None of these catch a well-formed wrong answer.** That is why the free rung is scoped to `coding` (which has an oracle) and `long-context` (measured 100%).

### 6.1 Signals that terminate rather than escalate

Two conditions end a leaf instead of moving it up a rung. They are listed apart from the table above because treating either as an escalation makes the situation worse, not better:

| signal | source | why it does not escalate |
|---|---|---|
| `latency_ceiling_exhausted` | §5.1 | the leaf is out of total time budget. Every higher rung is **slower** than the one that just ran, so escalating spends more wall-clock against a ceiling that has already been reached |
| `MAX_SUBAGENTS_PER_LEAF` reached | §7 | a hard failure surfaced to a human, a different class from an exhausted escalation |
| `security_reject_at_gate_top` | §4.5 | the security gate rejected the generator's top rung *and* survived its own climb. Escalating further cannot help: both ladders are exhausted. Goes to a human **holding the code**, because a false reject and a true one are indistinguishable here without judgement, and this signal was measured rejecting correct code at all three rungs |

`latency_ceiling_exhausted` must stay distinct from `deadline expiry` in the table above. They look alike in a log — both are "it took too long" — and the correct response is opposite: deadline expiry means *this model* was too slow and another rung may be faster per token of quality; ceiling exhaustion means *the leaf* is finished regardless of which model runs next.

---

## 7. Sub-agent lifecycle

- Sub-agents are **temporary**: they close when their gate finishes. Not kept alive, not resumable.
- Their full record — input, output, verdict — is **logged permanently**, tied to the parent leaf ID and tagged with the gate it served.
- `MAX_SUBAGENTS_PER_LEAF` caps total spawns across the whole pipeline. Hitting it is a **hard failure surfaced to a human**, not a silent give-up — a different failure class from an exhausted escalation.

---

## 8. Placement

| gate | condition |
|---|---|
| transport eligible | `mem_pct <= 75` from `system_latest_by_host()` |
| local spawn permitted | `resource_guard.check().ok` |
| sample older than 120s | treated as unknown; unknown is refused |

`mutates` of `True` **or** `side_effecting_read` forces local placement unconditionally, regardless of transport headroom. `resource_guard.check()` still gates whether it can spawn at all; with no host available the node queues rather than failing.

CPU is not gated anywhere. The gateway is an external HTTPS endpoint absent from `system_samples`, so the free tier has **no capacity gate** — a gate there could never open.

---

## 9. Configuration and control

All of the following are **one versioned unit**, so any production run ties to the exact configuration that produced it:

- the ladders (§3)
- per-type baselines and the size-factor table (§5.1)
- attempt and spawn caps (§5)
- the global kill switch (§9.1)
- the per-task-type `operational` flags (§1.1)
- circuit-breaker thresholds (§10)
- the cost ceiling (§2.7)
- the benchmark table (§2.6)
- the **EUR→USD rate** used to convert gateway billing into the table's units (§2.5). It is an input, not a constant: it sets luna's and mini's position relative to every Anthropic model, so a run cannot be tied to its ladder without it

Model-speed multipliers are **not** in this list: they are derived from the benchmark table (§5.1), so versioning the table versions them. Recording a derived value alongside its input is how the two drift apart.

### 9.1 One global kill switch

The entire design — classifier, all five stages, voice ladder, placement rules — ships behind **a single global kill switch**. Old routing or new routing, nothing in between. No per-gate switches, no phased ramp, no percentage-of-traffic rollout.

Per-gate toggles are rejected deliberately: each one multiplies the number of reachable states, and every combination is a configuration nobody has tested. One switch has two states, both of which can be verified.

**Rollback must be clean.** Flipping back to off is one action leaving no side effects: no leaf stuck mid-pipeline, no orphaned sub-agent, no dangling config. **The off path is tested and confirmed clean before the switch is ever turned on in production.**

### 9.2 Multi-agent settings page

A dedicated settings page in the web console surfaces the kill switch, the tunables from §5 and §10, the cost ceiling (§2.7), the per-task-type `operational` flag (§1.1), and the full benchmark table from §2.6 as an editable matrix. Each cell (`accuracy`, `n`, `cost_per_1M_tokens`, `median_latency_s`, `max_context`) is inline-editable so the operator can update measurements without code changes. The ladders in §3 are regenerated at runtime from whatever data is in the table — editing it is live. Changes take effect without a deploy.

Because the matrix writes to the same database table the ladder generator reads (§2.6), an edit changes routing for the next leaf with no deploy and no restart. That is exactly why **every write is validated before it is stored** (§1.1), not only the `operational` flip: a live-editable table checked once at startup can be broken at any time and will not say so until the next restart. A write that would break an invariant of an operational task type is rejected with the offending column named, and the stored value is left as it was.

Hover tooltips on rung values in the settings page show all five measured fields for the current task type: accuracy, sample size (`n`), cost per request, median latency, and **context window** (`max_context`) — the same five the §3 tooltip shows, from the same row.

### 9.3 Compatibility with existing backend/model selection

The console exposes two combo boxes: **backend** (Claude Code CLI, or direct API to the LLM gateway) and **model**.

The router does not replace or bypass this. At every stage and every escalation it programmatically sets the same backend-and-model pair a person would set manually, **through the identical invocation path**. There is no second way to invoke a model.

A model is never selected as a bare string. Every routing decision returns **`(model, machine)`** together — a model chosen without its machine reaches a gateway that does not serve it and returns `429 "No deployments available"`, a routing failure wearing a capacity error's clothes.

**Startup validation (§1.1):** every model named in any ladder is checked against the valid options in the model combo box at config load. Combined with the benchmark-table completeness check, this ensures no blank fields, every model resolves to a valid backend-and-model pair, every rung is backed by a §2.6 row, no task type is left with an empty ladder after cost-ceiling exclusion, no operational task type has a worst-case path exceeding the combined latency ceiling (§5.1), and no operational ladder has an expected tree cost above `BUDGET_USD` (§2.7). All six checks fail loudly if broken.

---

## 10. Observability

- Every attempt writes a usage row with `origin="orchestrator"`, **including failures**. A failed rung-0 attempt that writes no row makes the free tier look better the more it fails.
- Each gate writes **its own row with its own signal tag**.
- Every escalation records **which gate** rejected it, so repeated attempts are traceable to generation quality versus review calibration.
- **Elapsed time versus deadline** is logged on every attempt, success included. These measurements are what the §2.6 `median_latency_s` column is refreshed from, and the model-speed multipliers (§5.1) follow automatically — the multiplier is never tuned by hand against this log.
- **Cost per leaf end to end**, across every sub-agent call in every stage. The base design's per-1000-task table is generation-only and now understates a five-stage leaf.
- **Circuit breaker per gate:** a gate whose rejection rate crosses its threshold raises an alert rather than burning retries. A gate rejecting nearly everything is miscalibrated, not surrounded by bad code. **Threshold: 60% of the last 20 leaves** through that gate.
- **Human spot-check sampling:** a percentage of leaves that passed every gate are sampled for manual review — All gates agreeing is itself an unverified assumption. **Rate: 2%** of leaves that cleared all gates.

Both values are provisional and are re-examined at each quarterly re-benchmark (§10.2). They are configuration (§9), so changing them is an edit, not a deploy.

### 10.1 Free-tier target and its review trigger

**Target: ≥70% of orchestrator leaves complete on the free rung**, measured from `usage_events`, not asserted in a unit test. Below 70%, read the timeout rate first: a deadline set too tight and a gateway genuinely too slow need opposite fixes.

**Trigger for widening the free rung:** once `usage_events` shows **≥50 escalations** on `comprehension`, `multi-turn`, `planning` or `reasoning` where the paid rung's answer materially differed from the free model's, that threshold greenlights benchmarking a Sonnet-tier judge against the exec-verified labels. It replaces "someday" with a query.

### 10.2 Scheduled re-benchmark

The ladders are re-benchmarked **quarterly, as a scheduled job**, not as a prose reminder. The run writes its results into the §2.6 database table — that is the only place results land — and the ladders and the model-speed multipliers both regenerate from the new rows on the next read.

The run compares fresh numbers against the previous ones and **flags any task type where the ranking between two rungs has flipped** — that is the signal an escalation order needs rewriting, not merely a number updating. A flip changes routing the moment it is written, so the flag exists to make a silent reordering visible, not to gate it.

---

## 11. Verification

The router is a **pure function** of `(task_type, score, resource snapshot)` returning `(model, machine, deadline)`. No model calls, no I/O. The whole policy is testable by table without spawning anything.

| what | the trap |
|---|---|
| tier selection | asserting the model string alone — assert `(model, machine)` together |
| escalation ladder | asserting the final outcome — assert the *sequence* and the cap |
| all ladders in §3 | testing only the types named in prose — assert all nine rows (coding, long-context, multi-turn, planning, comprehension, **voice**, reasoning, split decision, reviewer gate), including the three that break positional order |
| voice ladder exists and stops at Sonnet | omitting voice because §3's prose says less about it — assert voice has a rung 0 and rung 1 and that **Opus is not a voice rung at any accuracy** |
| rung 0 is scoped | asserting the free model appears somewhere — assert it is attempt 1 for `coding` and `long-context` and **attempt 1 for nothing else** |
| `TIER0_DEADLINE` contents | asserting the two present keys — also assert the other types are **absent** |
| unknown task type | letting `KeyError` escape, or defaulting to the shortest deadline — assert it gets the **longest** |
| deadline is configuration | hardcoding the number in router and test so both agree and neither tracks the map — change a value in the test and assert the router follows |
| size factor applied | asserting the baseline only — assert a score-1 and a score-5 task of the same type get **0.5× and 2.0×** the baseline |
| multiplier is derived, not stored | asserting a multiplier constant — assert the fastest model on a task type computes to exactly **1.0**, and that changing a `median_latency_s` in the table changes the deadline without any other edit |
| three factors compose | asserting each factor alone — assert one case where all three are non-default and the product is the deadline used |
| each failure signal | testing only the deadline — assert all signals in §6 independently. An error frame **does not raise** |
| oracle rejects bad output | asserting only that good code passes — assert non-parsing output escalates, **attributed to the oracle**, not to a timeout |
| oracle infra error is distinct | letting a sandbox crash count as a code failure — assert the separate tag |
| each review gate escalates | testing the reviewer only — assert reviewer, QA and security each escalate independently |
| reviewer stays at Luna | asserting a rejection escalates *something* — assert the **generator** moved and the reviewer did not |
| security failure routes back | asserting it escalates like the others — assert it re-enters the pipeline with the vulnerability flagged |
| trivial bypass is scoped to writes | asserting a typo fix succeeds — assert a score-1 **write** skips 3–5, and that a score-1 **read** skips only 4–5 |
| blast-radius check | asserting a small-prompt task stays trivial — assert a task matching `simple|typo` that produces >3 file changes **still runs stages 3–5** |
| security re-run cap | asserting infinite recursion is impossible — assert a leaf that cycles through generation→security exactly 2 times **fails with human-flag on the 3rd** |
| `mutates` gates placement | asserting a read-only and a writing coding task take the same path — assert the writing one is refused a transport **while a transport has headroom** |
| all comprehension rungs | asserting comprehension only uses sonnet — assert rung 0 is sonnet, rung 1 is opus, rung 2 is absent; **rung 2 is empty by design** |
| read-only runs 1–3 | asserting a read-only task "skips the gates" — assert stage 3 **ran** and stages 4 and 5 **did not** |
| stage 3 is the read-only floor | testing §4.6, §4.7 and §4.8 in isolation — assert the **`read.*file` pattern**, which is score 1 and `mutates=False` at once, still runs stage 3. This is the case an unscoped bypass leaves with no verification at all |
| no read-only leaf is ungated | asserting each rule separately — enumerate **every** combination of trivial / non-trivial, prose / executable output, and blast radius, and assert stage 3 appears in every `mutates=False` row of §4.8's table |
| oracle skipped on prose | asserting stage 2 always runs — assert a `summarize.*log` task **does not run stage 2**, rather than running it as a no-op that always passes |
| gates carry deadlines | asserting only the generation deadline — assert each model gate gets `baseline x size_factor x` **its own model's** multiplier |
| ceiling is checked before a stage | asserting a leaf stops at the ceiling — assert it stops **between** stages with the next stage never started, not mid-call |
| ceiling exhaustion does not escalate | folding it into deadline expiry — assert `latency_ceiling_exhausted` **terminates** the leaf and that no higher rung is attempted |
| ceiling vs attempt budget | asserting the ceiling is enforced — assert startup **fails** for an operational task type whose computed worst case (`baseline x size x [sum(m_rung) + sum(m_gate)]`) exceeds the ceiling; a score-4 coding type at 810s against a 600s ceiling must not load |
| `side_effecting_read` takes all five | folding it into the read-only rule because it also writes nothing — assert it runs stages 4 and 5 while `mutates=False` does not |
| `side_effecting_read` | folding it into `False` — assert it is refused a transport and tagged separately |
| `mutates` default | testing only the nine explicit patterns — assert unmatched text returns `True` |
| multi-pattern conflict | testing single matches only — assert a conflicting pair returns `mutates=True` and logs |
| child re-classification | asserting the parent's type — assert a coding parent's comprehension child gets comprehension's ladder |
| kill switch off | asserting new behaviour only — assert routing is **byte-identical to today** with the switch off |
| rollback leaves no state | asserting the switch flips — assert no leaf, sub-agent or config survives the flip |
| startup validation | asserting valid config loads — assert an invalid model name **fails at load**, not at first use |
| validation on every write | asserting load-time validation only — assert a write that blanks a `median_latency_s` on an **operational** type is **rejected and the stored value unchanged**, and that the same write against a non-operational type succeeds |
| bootstrap exemption | asserting TBD always fails — assert a **non-operational** task type with TBD rows starts fine, and the same type flagged operational **refuses to start** |
| §3 is generated, not written | asserting the §3 table's contents as constants, or regenerating from the *shipped* table — build a **fully-measured fixture**, regenerate, and assert it equals §3. The shipped table is mostly TBD, so §3 is unreachable from it by design (§2.6); a test that regenerated from live data would assert the snapshot is wrong |
| TBD is not a candidate | asserting only that a worse model is skipped — assert a row with **TBD accuracy is excluded outright**, and that it is excluded *before* any accuracy comparison runs, not by losing one |
| unpriced model is ineligible | asserting mini is excluded — assert a model with **no rate from any of the three sources** is refused a ladder place outright, and is neither estimated into eligibility nor treated as free |
| ceiling is per token, not per request | asserting the ceiling excludes something — build a fixture where model X is cheaper per *request* than model Y only because X's historical requests are smaller, and assert the ceiling **ranks them by rate**, not by that artifact. This is the mini/Sonnet inversion: mini is 2.99x cheaper per token and was excluded while Sonnet was kept |
| cost is normalised by task size | asserting a model's rate alone — assert `effective_cost_per_task` multiplies the rate by the task type's expected tokens, so the same model costs more on a larger task type |
| ceiling is position-dependent | asserting a per-model threshold — assert the **same model** is admissible at rung 2 and refused at rung 0, because reach probability differs; a scalar threshold cannot express this and must fail the test |
| ladder fits the budget | asserting rungs resolve — assert a ladder whose expected tree cost exceeds `BUDGET_USD` **fails at load**, naming the task type and the offending rung. Comprehension with sonnet at rung 0 ($3.736) must not load |
| opus is affordable nowhere | asserting opus is merely expensive — assert it is refused at **every** rung of every ladder on the measured inputs |
| leaves_per_tree is an input | hardcoding 40 — assert lowering it to 10 makes sonnet admissible at rung 1 with no other edit, proving the assumption is what drives the exclusions |
| EUR→USD is an input | hardcoding converted figures — assert changing the stored rate moves luna and mini against the Anthropic rows, and that a ladder reorder follows from it with no other edit |
| cost basis is authoritative or absent | costing from `usage_events.cost_usd` — assert only rows with `cost_basis='list'` contribute to a blended figure; a model whose rows are all `unknown` must read as unpriced, not as its recorded number |
| per-call floor is counted | costing a leaf as one call per stage times a rate — assert a five-stage leaf's projected cost includes the **per-call token floor for every stage**, and that a free-generation leaf with three paid gates is not costed at zero |
| security gate climbs | asserting the generator escalates on a security reject — assert that after the generator's **top** rung is rejected, the **gate** moves up a rung and re-reviews the same code |
| security reject at both tops goes to a human | asserting the leaf fails — assert a rejection surviving the gate's own top rung raises `security_reject_at_gate_top` **with the code retained**, and is not recorded as a failed leaf |
| gate rejection text is retained per rung | asserting only the final verdict — assert each rung's rejection reason is stored, so a gate rejecting everything is legible without a re-run |
| empty ladder from ceiling | asserting exclusions happen — assert a task type whose every rung is excluded goes **non-operational and falls back**, rather than routing to an excluded model |
| no row means not a candidate | asserting the free model is absent from planning — **add a measured `vllm/*` planning row in the fixture and assert it becomes planning's rung 0**, proving absence is what excluded it |
| specificity is computed | asserting a hand-picked winner — assert `refactor.*large` beats `quick` on `"quick refactor large module"` **by literal-character count**, and that a tie falls to longer match span then table order |
| score takes the maximum | asserting the winning pattern's own score — assert a text matching score 1 and score 4 patterns yields **score 4**, and that its size factor is 1.5 not 0.5 |
| type and score may differ | asserting one pattern supplies both — assert a case where `task_type` comes from the most specific pattern and `score` from a different, higher-scoring one |
| multiplier reference set | asserting the fastest row overall is 1.0 — assert a **cost-excluded** model that is fastest on a task type **does not** set the reference, and the fastest ladder-eligible model computes to exactly 1.0 |
| non-operational type is not routed | asserting it merely fails validation — assert work classified into it falls back to today's routing **and logs the fallback** |
| free tier has no capacity gate | asserting behaviour when samples exist — assert routing is unchanged when `system_latest_by_host()` returns nothing |
| usage recording | asserting successes — a failed and a timed-out attempt must each produce a row |
| budget ceiling | asserting per node — spend accumulates across the tree; assert the tree total |
| termination guard | asserting intent — assert node count when children score ≥ parent |
| admission control | mocking `resource_guard` to always return ok — feed a fake `/proc/meminfo`; `read_meminfo()` already takes a path |

Every test is mutation-checked: break the ladder order, the guard, the termination rule, and confirm a specific test fails for each.

---

## 12. Open items

- `claude_proxy.py` drift on the pentester transport (`bb117e85…` vs HEAD `691fe393…`) — a deployment decision to settle before any wholesale transport sync. **Do not sync transports until it is decided.**
