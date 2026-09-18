# Tiered agent delegation — implementation spec v3

**Date:** 2026-09-14
**Status:** design complete, not implemented.

This document is standalone. The base design's measurement tables from the original design are still the evidence; everything an implementer needs to *build* is below.

---

## 1. Scope

Today `ModelRouter.assign_model` returns `config.ANTHROPIC_MODEL` on both branches — complexity is computed and discarded. This design fills that seam.

What must be **built**: the duplicate-branch fix in `ModelRouter.assign_model` so the classifier's output reaches the routing decision instead of being discarded — the **task type** selects the ladder (§3) and the **score** sets the deadlines (§5.1), per §2.1's division; the score is deliberately not an input to model choice, because §3 escalates on failed attempts rather than on an upfront size guess — the task classifier (§2), the coding oracle (§4.2), the three review gates (§4.3–4.5), the blast-radius check (§4.6), the settings page (§9.2). Everything else is configuration over existing mechanisms.

### 1.1 Startup validation

At config load the system checks six invariants and fails loudly if any are broken:

| invariant | condition |
|---|---|
| no blank fields | for every **operational** task type: at least one **ladder-eligible** row exists, and every ladder-eligible row has a value in every column. A row that is not ladder-eligible (§2.6 — no measured accuracy, or excluded by the cost ceiling) is exempt from this check **and** excluded from ladder generation. It is never one without the other: exemption and ineligibility are the same fact stated twice |
| model resolution | every model name named in any ladder (§3) resolves to a valid backend-and-model pair available in the model combo box (§9.3) |
| no empty ladder | after applying the cost-ceiling filter (§2.7), every **operational** task type must have at least one rung; no operational task type is left with an empty ladder |
| every rung is backed by a row | every `(model, task_type)` pair appearing in the §3 ladder snapshot has a row in the §2.6 table. A rung named in the snapshot with no backing row means the snapshot and the generator disagree, and the generator silently wins |
| the ceiling fits the budget | for every **operational** task type, the computed worst-case path (§5.1) is at or below the combined latency ceiling. A ceiling below it kills leaves that passed every per-attempt deadline, after paying for them |
| the ladder fits the budget | for every **operational** task type, the expected tree cost of its ladder (§2.7 — leaves × tokens × Σ reach-probability × rate) is at or below `BUDGET_USD`. A ladder that cannot afford its own rungs will exhaust the tree budget mid-run, which is a worse failure than refusing to start |

**Bootstrap exemption.** §2.6 ships mostly unmeasured, so a check that refused every TBD would mean the system could never start for the first time. Each task type therefore carries an `operational` flag, default **false**. The checks above apply only to task types flagged operational; a non-operational task type may hold TBD in any column.

A non-operational task type is **not routed**. Work classified into it falls back to today's routing (`config.ANTHROPIC_MODEL`), exactly as if the kill switch (§9.1) were off for that type alone, and each such fallback is logged so the gap is visible rather than silent.

Flipping a task type to operational is the act that submits it to validation: at that moment every **ladder-eligible** row must be complete and every rung must resolve, or the system refuses to start. This is the only way a task type becomes routable, so no type can go live on unmeasured data.

**Why the check is scoped to ladder-eligible rows** (decided 2026-09-15). The invariant originally demanded that *every* row of an operational task type be complete. That is stricter without being safer, because §2.6 already refuses a row with no measured accuracy as a ladder candidate — so the stricter form demanded measurements of models that can never be rungs. Two consequences made it untenable rather than merely wasteful:

- It was **unsatisfiable in practice.** `azure_ai/gpt-5.4-mini` has a `coding` row, is not ladder-eligible, and cannot be measured from this deployment because the backend refuses to serve it (§2.7). Under the broad rule, `coding` could never go operational — blocked forever by a model that is not a rung and cannot become one.
- A validation gate that demands pointless work **gets satisfied with junk.** The cheapest way past an unsatisfiable check is to type a plausible number into the cell, which converts a hard failure into a wrong ladder. This codebase already holds that lesson for the model check: a check that cries wolf gets switched off.

Nothing is given up. Every guarantee the broad form made about rungs still holds, because a rung is by definition ladder-eligible: no rung can lack a latency (so the multiplier always computes), a context window, or a rate.

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

**As of 2026-09-15, `coding` is the first task type that clears every invariant — with one unresolved dependency, below.** Every other task type remains non-operational, so with the kill switch on, almost nothing routes. That is the designed bootstrap state (§1.1), not a fault. Audited against §2.6:

| task_type | rows | accuracy | n | cost | latency | max_context |
|---|---|---|---|---|---|---|
| coding | 4 | 3 | 3 | **4** | 3 | **4** |
| comprehension | 3 | 2 | 2 | **3** | 0 | **3** |
| long-context | 3 | 1 | 1 | **3** | 0 | **3** |
| multi-turn | 2 | 0 | 0 | **2** | 0 | **2** |
| planning | 2 | 0 | 0 | **2** | 0 | **2** |
| reasoning | 4 | 2 | 1 | **4** | 0 | **4** |
| reviewer-gate | 3 | 3 | 3 | **3** | 0 | **3** |
| security-gate | 3 | 3 | 3 | **3** | 0 | **3** |
| voice | 3 | 0 | 1 | **3** | 0 | **3** |

Cost and `max_context` are complete (23/23). Accuracy is 8/23 and latency 4/23 after today's coding run (§3.1).

**`max_context` was the cheapest column and is now filled** (§2.6) — it needed no benchmark run, only the gateway's `/model/info` and a documented model table. It is recorded here because the readiness table above was the thing that surfaced it: a column no measurement pass would ever produce had been sitting at 0/23 while every task type waited on it.

**`coding` is one row from complete, and that row cannot be filled from this deployment.** It now holds 4/4 cost, 4/4 `max_context`, and 3/4 on accuracy, `n` and latency — every gap is the same row, `azure_ai/gpt-5.4-mini`, which the active backend refuses to serve (§3.1). The §1.1 scoping decision resolved this on 2026-09-15: scoped to ladder-eligible rows, **`coding` is complete today.**

#### `coding` clears all six invariants — and then hits a dependency they do not express

Checked individually against §1.1, with the decisions of 2026-09-15 applied:

| invariant | `coding` | why |
|---|---|---|
| no blank fields | **pass** | its four ladder-eligible rows (vllm, luna, terra, sonnet) are complete in all five columns; mini's row is exempt because it is not ladder-eligible |
| model resolution | **pass** | all four resolve to real backend-and-model pairs |
| no empty ladder | **pass** | `vllm → luna → terra → sonnet` survives the cost ceiling |
| every rung backed by a row | **pass** | all four rungs have §2.6 rows |
| ceiling fits the budget | **pass, on a remaining lower bound** | 1,107.8s against the 1,500s ceiling, 392.2s of margin, with the baseline derived against the current reference and the gate rows re-measured at n=56 (§5.1, 2026-09-17). Against the gate rows still live in `delegation_capability` it is 1,281.4s and 218.6s; both pass. Still excludes §4.5's security re-run term, which cannot be priced until §12's gate-type item is decided |
| ladder fits the budget | **pass** | $0.657 against `BUDGET_USD` of $1.00, on the capped ladder below |

This table was re-derived from the live `delegation_capability` on 2026-09-17. It previously reported three rungs, a 1,398.2s worst case and a passing budget check; then four rungs and a budget check that could not be computed at all; and now three rungs again. `coding` fails **one** of the six invariants, and it is not a data invariant — it is §12's policy hold.

#### The attempt budget caps the ladder (2026-09-17)

When terra was measured, `coding`'s generated ladder became **four rungs** and the type became *unpriceable*: §2.7 publishes reach probabilities for rungs 0–2 only, and it refuses an unpriced rung rather than truncating it, because truncation would price the fourth rung at zero and make an unpriced rung indistinguishable from a free one.

**But a fourth rung can never run.** `MAX_ATTEMPTS` is 3, and §5.1's worst-case path already timed only the first three rungs. So the two halves of the same table disagreed about whether rung 3 existed — §5.1 ignored it, §2.7 refused to price it. **The ladder is now capped at `MAX_ATTEMPTS` where it is generated**, so both halves see the same rungs.

**Which rungs are dropped is the substantive part.** Taking the first `MAX_ATTEMPTS` would drop the *top* rung, and because §3's walk produces non-decreasing accuracies the top rung is always the accuracy ceiling — so plain truncation removes the most capable model the type has. On `reasoning` that is the difference between a ladder ending at 100% and one ending at 50%. The rule therefore keeps **both ends** and drops from the middle:

- **rung 0 stays** — it is §4.1's free start and the cost thesis. §5.1 already rejected dropping it once, under its option 2, for exactly that reason.
- **the last rung stays** — it is the accuracy ceiling.
- **middle rungs go redundant-first**: a rung that does not improve on its predecessor's accuracy buys an attempt and no capability. That is what a four-rung ladder is made of in practice — `coding` had luna and terra both at 100% for the same price, `reasoning` had luna and terra both at 50%.
- if no redundant middle rung remains, the **lowest-accuracy** middle rung goes.

Note the last two rules disagree on exactly the case that caused this, and the redundancy rule is the one that is right: with luna and terra tied above a 66% free rung, a lowest-accuracy rule drops whichever is listed first and keeps the other, producing a ladder that escalates from the free model to a *repeat of the rung it just failed*.

What it produces on today's table:

| task type | before | after | tree cost |
|---|---|---|---|
| `coding` | `vllm → luna → terra → sonnet` (unpriceable) | **`vllm → luna → sonnet`** | $0.657 — fits |
| `reasoning` | `luna → terra → sonnet → opus` (unpriceable) | **`luna → sonnet → opus`** | $3.366 — over |
| every other type | ≤ 3 rungs | unchanged | unchanged |

`coding` regenerates to **exactly what §3 publishes** and what §2.7 calls "correct and survives unchanged" — so the generator agrees with the spec it implements again, rather than contradicting it.

`reasoning` is now honestly over budget rather than unpriceable, and that is an improvement even though it still blocks: its ladder previously *appeared* to cost $0.724 only because opus, the one model measured at 100%, sat at an unreachable rung 3. The cost is real — every cheap model measures 0.5 on reasoning (luna, terra, sol and mini all 0.5; sonnet 0.834; opus 1.0), so there is no affordable rung that can reason.

**The rung-count refusal stays in §2.7**, no longer reachable by a ladder merely being long. It now guards the relationship that replaced that failure: `MAX_ATTEMPTS` attempts need `MAX_ATTEMPTS` published reach probabilities, and raising the attempt budget without publishing one must refuse rather than price the extra attempt at zero.

**But a coding leaf is not only its generation ladder.** Stages 3–5 run on the `reviewer-gate` task type (§3, §4.3), and `reviewer-gate` is **not** operational: both its rows lack measured accuracy, and §2.7 puts `claude-sonnet-5` at its rung 1 at **$1.868** against a `BUDGET_USD` of 1.00, so its ladder does not fit. Its rung 0 (luna, $0.068) does fit.

So flipping `coding` to operational today would route generation through a validated ladder into gates whose own task type has not passed validation. **§1.1's invariants are all per task type and none of them express this dependency** — nothing in the current check would catch it.

That is an open question, not a thing to decide in passing, and it is recorded in §12. The two shapes it could take: require that a task type's gate types be operational before it can be (strict, and it blocks `coding` until reviewer-gate accuracy is measured), or scope `reviewer-gate` affordability to the rungs actually reachable, since §4.3 only climbs to sonnet when the generator's top rung keeps being rejected — a tail case the flat reach-probability model in §2.7 prices as routine.

#### `leaves_per_tree` cannot be measured from history, and the reason matters

§2.7 names `leaves_per_tree` as its one pure assumption and the highest-leverage number missing. It is not obtainable from this deployment's history, because **the orchestrator has barely run**:

- **3 tasks have ever existed**, across 2 orchestrators, both on 2026-08-31. Two of the three failed.
- **Every one of them is a root.** `parent_task_id` is empty on all three, so no task ever decomposed — no tree has ever been formed, and the observed children-per-node is zero. There is no shape to measure.
- **`usage_events` contains no orchestrator rows at all.** Origins present are `terminal` (145,410), `web-routed` (21,891), `web` (309) and `voice` (33). `orchestrator` and `supervisor` appear zero times.

**The empty usage table is not a broken mechanism, and the distinction was worth checking before acting on it.** The obvious reading — that the orchestrator spends tokens without recording them, the failure this codebase has hit before — is wrong here. `orchestrator.py` records usage with `origin="orchestrator"`, attaches cost once per turn rather than per model, and recovers the attempts a content-quality retry discarded via `take_retried_usage`; `tests/test_qa_orchestrator_usage.py` and `tests/test_qa_orchestrator_cost.py` cover it, and all 34 pass.

The recording landed on **2026-09-06**. The last orchestrator task ran on **2026-08-31**, six days earlier. **The code is correct and has simply never executed.** Absence of rows was evidence about how much the orchestrator has been used, not about whether it records — two claims that look identical from the table alone.

So the ordering is not "fix recording first". It is: **run the orchestrator at all.** `leaves_per_tree` becomes measurable the first time a tree is built, and §10's entire measurement story starts producing rows in the same act, with no code change. Until then §2.7's ladder admissibility rests on `leaves_per_tree = MAX_NODES = 40`, which is an upper bound and therefore the conservative choice — the true figure is almost certainly smaller, and a smaller figure admits *more* models, so nothing currently excluded is excluded in error.

**That scoping question is now decided (2026-09-15): the completeness invariant is scoped to ladder-eligible rows** (§1.1). It was the shortest path to a first operational task type and it was blocking `coding` outright, because mini's unmeasurable `coding` row would have held that type non-operational forever. The counts above are therefore read against ladder-eligible rows only; a TBD in a row that can never be a rung no longer blocks anything.

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
| `fix.*bug\|implement\|refactor\|debug\|add.*function\|write.*function` | 3 | coding | True |

### 2.1 Multi-pattern conflicts

Patterns are ranked by **specificity**, not list order.

**Specificity is defined, not judged.** For a matched alternative (one branch of a pattern's `|`), specificity is the count of **literal characters** in it — every character that is not a regex metacharacter (`.` `*` `+` `?` `|` `(` `)` `[` `]` `\` `^` `$`). So `refactor.*large` scores 13 — the `.` and `*` are metacharacters and do not count — and `quick` scores 5, and the first wins on the text `quick refactor large module`. Ties resolve by longer matched span in the input; a remaining tie resolves by table order, so the result is always deterministic.

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
| `nvidia/Qwen3.6-35B-A3B-NVFP4` *(alias of the row above — same deployment)* | 933 | 93.51M | 0.00 | **0.000** | 3.6% |
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

#### Budget raised to $3.50, 2026-09-18 — and it re-opens three positions

`BUDGET_USD` was raised from $1.00 by operator decision, to let `comprehension` and `reasoning` go operational. Both price at **$3.366**, and the money is concentrated in one rung: `claude-sonnet-5` at rung 1 costs **$1.868** alone — more than the entire previous cap — because rung 1 is reached half the time. opus at rung 2 adds $1.430; the free rung is $0.068.

**This is not only a change for those two types.** §2.7's admissibility conclusions were derived against $1.00, and moving the line re-opens three of them:

| model | rung | cost | at $1.00 | at $3.50 |
|---|---|---|---|---|
| `azure_ai/gpt-5.4-mini` | 0 | $1.251 | refused | **admissible** |
| `claude-sonnet-5` | 1 | $1.868 | refused | **admissible** |
| `claude-opus-5` | 2 | $1.430 | refused | **admissible** |

The first matters most: **`mini`'s exclusion now rests on the operator decision alone, not on cost.** `EXCLUDED_MODELS` still keeps it out of every ladder, and that entry was always a decision rather than a derivation — but the cost argument that used to agree with it no longer does. There is no smaller version of this raise that avoids it: any budget above $1.251 admits mini at rung 0, and both task types need $3.366.

opus stays refused at rungs 0 and 1, where it costs more than the whole tree budget.

**Two reasons to treat $3.50 as provisional.** The cost is dominated by a single rung, so any competent model measured between luna (0.0285) and sonnet (1.5709) would drop both types under the *old* $1.00 with no raise at all — that is the 55× gap this section names. And `comprehension`'s ladder rests on a measurement taken while it was the classifier's **default** task type and absorbed most real coding traffic (fixed 2026-09-18), so re-measuring it may move the ladder and with it this number.

**Consequence for the ladder:** the ordering survives — free is free, luna is ~5× cheaper per request than mini — but the *magnitudes* do not. `BUDGET_USD = 1.00` per **tree** (§5 — one pool shared by every leaf, not one budget per goal) buys roughly **335 luna requests or 67 mini requests** across the whole tree, not the thousands the original figures implied. Re-check that cap before implementation.

#### 2.5.2 Attribution gaps to close first

- **€1.13 of MTD spend (10%) matches no model above** — €0.83 of `gpt-4o-mini` meters plus €0.30 "Others".
- ~~**`nvidia/Qwen3.6-35B` carries 93.51M tokens with no cost attribution.**~~ **Not a gap — closed 2026-09-15.** `nvidia/Qwen3.6-35B-A3B-NVFP4` and `vllm/Qwen3.6-35B-A3B-NVFP4` are **the same self-hosted deployment under two labels**; the `vllm/` name was adopted to make it visible that the model is served through this deployment's own vLLM engine. It is free under either label, so no cost is unattributed. What *was* wrong is the arithmetic: the two labels were counted as separate models, splitting one deployment's usage across two rows. Combined, the free deployment is **25,477 requests and 2,344.09M tokens — 91.2% of all traffic**, not the 87.6% the `vllm/` row alone reports.
- **`azure_ai/gpt-5-mini` has 127 requests and no billing line.**
- **3,778 requests (10.5%) are unattributed**, carrying 510 tokens between them — requests logging without token attribution. §10's entire measurement story runs on `usage_events`; this gap must be closed before the ≥70% target in §10.1 can be trusted.

### 2.6 Model benchmark table — each model against each task type

Holding each model against each task type, with columns: measured accuracy, sample size, cost per request, latency, and context window. A row per model per task type.

| model | task_type | accuracy | n | cost_per_1M_tokens | median_latency_s | max_context |
|---|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | coding | 66% | 44 | 0.0000 | 26.8 | 229,376 |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | long-context | 83.3% | 12 | 0.0000 | 13.9 | 229,376 |
| `azure_ai/gpt-5.6-luna` | coding | 100% | 24 | 0.0370‡ | 12.8 | 922,000 |
| `azure_ai/gpt-5.6-luna` | long-context | 100% | 12 | 0.0370‡ | 6.3 | 922,000 |
| `azure_ai/gpt-5.6-luna` | comprehension | 58.3% | 12 | 0.0370‡ | 9.2 | 922,000 |
| `azure_ai/gpt-5.6-luna` | reasoning | 50% | 6 | 0.0370‡ | 16.5 | 922,000 |
| `azure_ai/gpt-5.6-luna` | voice | 83.3% | 12§ | 0.0370‡ | 11.0◊ | 922,000 |
| `azure_ai/gpt-5.4-mini-copilot` | voice | TBD | 46* | 0.0781‡ | 2.002 | TBD |
| `azure_ai/gpt-5.6-sol` | coding | 100% | 18 | 3.4043‡ | 7.85 | 922,000 |
| `azure_ai/gpt-5.6-sol` | long-context | 100% | 12 | 3.4043‡ | 7.84 | 922,000 |
| `azure_ai/gpt-5.6-sol` | multi-turn | 83.3% | 12 | 3.4043‡ | 17.02 | 922,000 |
| `azure_ai/gpt-5.6-sol` | planning | 83.3% | 12 | 3.4043‡ | 39.41 | 922,000 |
| `azure_ai/gpt-5.6-sol` | comprehension | 58.3% | 12 | 3.4043‡ | 14.53 | 922,000 |
| `azure_ai/gpt-5.6-sol` | reasoning | 50% | 6 | 3.4043‡ | 11.78 | 922,000 |
| `azure_ai/gpt-5.4-mini` | coding | TBD | — | 0.0850‡ | TBD | 1,050,000 |
| `azure_ai/gpt-5.4-mini` | reasoning | 86% | TBD | 0.0850‡ | TBD | 1,050,000 |
| `azure_ai/gpt-5.6-luna` | multi-turn | 91.7% | 12 | 0.0370‡ | 13.5 | 922,000 |
| `azure_ai/gpt-5.6-luna` | planning | 75% | 12 | 0.0370‡ | 40.7 | 922,000 |
| `azure_ai/gpt-5.6-luna` | reviewer-gate | 96.43% | 28 | 0.0370‡ | 6.07 | 922,000 |
| `claude-opus-5` | reviewer-gate | 89.29% | 28 | 1.2310‡ | 5.025 | 1,000,000 |
| `azure_ai/gpt-5.6-luna` | security-gate | 85.71% | 28 | 0.0370‡ | 5.83 | 922,000 |
| `claude-sonnet-5` | security-gate | 92.86% | 28 | 0.4769‡ | 4.635 | 1,000,000 |
| `claude-opus-5` | security-gate | 96.43% | 28 | 1.2310‡ | 4.8 | 1,000,000 |
| `claude-sonnet-5` | coding | 100% | 24 | 0.4769‡ | 15.5 | 1,000,000 |
| `claude-sonnet-5` | long-context | 66.7% | 12 | 0.4769‡ | 4.2 | 1,000,000 |
| `claude-sonnet-5` | comprehension | 100% | 12 | 0.4769‡ | 6.0 | 1,000,000 |
| `claude-sonnet-5` | reasoning | 83.4% | 6 | 0.4769‡ | 24.6 | 1,000,000 |
| `claude-sonnet-5` | voice | 100% | 12§ | 0.4769‡ | 6.9◊ | 1,000,000 |
| `claude-sonnet-5` | multi-turn | 100% | 12 | 0.4769‡ | 7.8 | 1,000,000 |
| `claude-sonnet-5` | planning | 83.3% | 12 | 0.4769‡ | 17.5 | 1,000,000 |
| `claude-sonnet-5` | reviewer-gate | 78.57% | 28 | 0.4769‡ | 4.555 | 1,000,000 |
| `claude-opus-5` | comprehension | 100% | 12 | 1.2310‡ | 11.9 | 1,000,000 |
| `claude-opus-5` | reasoning | 100% | 6 | 1.2310‡ | 10.2 | 1,000,000 |
| `azure_ai/gpt-5.6-terra` | coding | 100% | 18 | 1.4760‡ | 7.2 | 922,000 |
| `azure_ai/gpt-5.6-terra` | long-context | 100% | 12 | 1.4760‡ | 6.3 | 922,000 |
| `azure_ai/gpt-5.6-terra` | multi-turn | 100% | 12 | 1.4760‡ | 12.4 | 922,000 |
| `azure_ai/gpt-5.6-terra` | planning | 66.7% | 12 | 1.4760‡ | 26.9 | 922,000 |
| `azure_ai/gpt-5.6-terra` | comprehension | 50% | 12 | 1.4760‡ | 9.8 | 922,000 |
| `azure_ai/gpt-5.6-terra` | reasoning | 50% | 6 | 1.4760‡ | 8.9 | 922,000 |

**The billing arrived on 2026-09-18 and no `†` survives in this column. Every cost above is now a rate somebody was charged.** The previous revision of this paragraph said terra carried luna's rate by operator assumption and that "real gateway billing figures are expected tomorrow". Tomorrow came; this records what it said.

**The first column below is deliberately not a model id.** `tests/test_qa_delegation_shipped_state.py` parses this section for capability rows by taking every line beginning with `` | ` `` — so a correction table whose rows begin with a backticked model name is read as seven more capability rows, and the suite goes red on `could not convert string to float: '**1.4760‡**'`. That is exactly what the first version of this table did.

| repriced | model | was | now | how |
|---|---|---|---|---|
| terra | `azure_ai/gpt-5.6-terra` | 0.0285† | **1.4760‡** | €2.43 / 1,780,071 tokens |
| sol | `azure_ai/gpt-5.6-sol` | 0.0285† | **3.4043‡** | €7.62 / 2,420,157 tokens |
| luna | `azure_ai/gpt-5.6-luna` | 0.0285 | **0.0370‡** | €9.30 / 271,978,522 tokens |
| mini | `azure_ai/gpt-5.4-mini` | 0.5261 | **0.0850‡** | €0.62 / 7,885,210 tokens |
| copilot | `azure_ai/gpt-5.4-mini-copilot` | 3.5167‡ | **0.0781‡** | billed with 5.4-mini |
| opus | `claude-opus-5` | 3.6082 | **1.2310‡** | re-derived, below |
| sonnet | `claude-sonnet-5` | 1.5709 | **0.4769‡** | re-derived, below |

Euro figures are converted at §2.5's dated **EUR→USD = 1.0812**.

**Terra's assumption was wrong by 52×, and it had been holding rung 0 of four ladders.** The correction moves rung 0 to luna in six ladders — `coding`, `long-context`, `comprehension`, `planning`, `reasoning` and `multi-turn`. It does not threaten the budget: the worst operational tree cost *fell*, from $3.3662 to $2.0046 against `BUDGET_USD` $3.50.

**Terra's usage was not missing; it was under a different key.** Two sessions independently read "no usage rows for terra" and stopped. The rows exist as `gpt-5.6-terra`, without the `azure_ai/` prefix the capability table uses — 159 events, 1,780,071 tokens, all inside a fifteen-minute window on 2026-09-17. Treat this as a class rather than an incident: **a model id that is a join key in one table and a display name in another will do this again.**

**The two Anthropic figures could not be reproduced and were rebuilt.** §2.6's provenance note says they are "blended from `usage_events` rows carrying `cost_basis='list'`". No denominator reproduces them — not input+output, not with cache reads or writes, not restricted to September; the closest are 1.2310 and 0.4769, and the published pair looks like it divided the cost of **77 and 94** priced rows across **all 25,168 and 24,249** requests. The replacements take numerator and denominator from the same rows. Only `claude-opus-5` and `claude-sonnet-5` have any `cost_basis='list'` rows at all, so this method cannot reach any other Anthropic model.

**Three models now carry a price and no accuracy, and are deliberately in no ladder:** `azure_ai/gpt-5-mini` (0.4375‡), `claude-haiku-4-5-20251001` (0.6896) and `claude-fable-5` (6.8902). The two Anthropic ones are priced from list rates against their own recorded token mix — a *fifth* provenance for this column, and not comparable to the `‡` figures beside them. `claude-opus-4-8` is left unpriced: no published rate for it exists in this repo, and the gateway refuses it outright (`403 … Model is blocked`).

**What is now stale is the accuracy column, not the cost column.** Every accuracy above was measured against the old ordering, in which terra held rung 0. The ladders those numbers describe no longer exist.

#### Sections below this point that still quote the superseded rates

These were not rewritten, because each is an *argument* built on the old numbers and re-deriving them silently would be worse than saying which ones moved. Read them knowing the inputs changed:

- **§2.6's `‡` paragraph on `gpt-5.4-mini-copilot`** said 3.5167 was suspicious because "a model named *mini* pricing within 3% of `claude-opus-5` is not what a mini model should cost". That suspicion was correct and is now resolved: the real rate is **0.0781**, and copilot is the second-cheapest priced model rather than the second-dearest.
- **§2.6's provenance note** describes four sources and calls terra's figure "the one figure in this column that is not a rate this deployment has actually observed". Terra is observed now; there are five sources, and the fifth is the list-rate basis used for haiku and fable.
- **§2.7's exclusion of `azure_ai/gpt-5.4-mini` is now built on an inverted premise.** It recorded mini as **2.99× cheaper than sonnet**; at the corrected rates mini (0.0850) is **5.6×** cheaper than sonnet (0.4769). The exclusion itself still stands — §2.7 line 3 already records it as an operator decision rather than an arithmetic result, precisely so it would not move when the numbers did. This is that provision doing its job.
- **The "55× gap" between luna and sonnet, cited in §2.7 and §5, is now about 12.9×** (0.0370 → 0.4769). The argument that excluding mini forces a large jump survives; its magnitude does not.
- **§5.1's per-rung admissibility table** prices every rung from the old column. Its conclusions about which rungs a model may occupy need recomputing against the new rates before anyone relies on them.

**Nothing in this subsection changes a ladder by itself.** The ladders in use are generated from the database, not from this document — §2.6 is a snapshot of that table, and the snapshot is what has just been brought up to date.

This is in direct tension with §2.7's **"a model nobody priced must not come out cheapest"** — assuming a cheap price is exactly how an unpriced model comes out cheapest, and this assumption does that. That tension is not resolved here: it is a **deliberate operator decision**, recorded rather than argued away, not an oversight.

**`§` marks an accuracy measured over the CLI transport, not the voice transport.** `voice` had **no benchmark tasks at all** until 2026-09-17, which is why every accuracy in this column read TBD and why §3's voice ladder was a guess: nothing could be run. Four single-turn tasks now exist (`voice-arithmetic`, `voice-conversion`, `voice-ordering`, `voice-declines-to-invent`), written to the register of a real recorded voice turn — 23–52 characters in, a sentence or two back — rather than to this file's existing 100-token written prompts.

Two limits on what those numbers mean, and both are structural:

- **The harness runs the Claude Code CLI (§0); voice does not.** `routes/voice.py` is the documented exception and speaks to an OpenAI-compatible endpoint directly. So these figures measure *the model* on voice-shaped tasks, over a different transport from the one production voice uses. That is a reasonable proxy for accuracy and **not** for latency: the CLI run measured 6.6–9.6s against the 2.0s the voice path actually records.

  **`◊` marks a latency borrowed from that CLI transport anyway, by operator decision on 2026-09-18, to let `voice` go operational without waiting for an on-transport measurement.** The value is each model's median `median_latency_s` across the eight task types where it IS measured — luna 11.0, sonnet 6.9 — chosen over its maximum because, counter-intuitively, larger borrowed latencies produce a *smaller* worst-case path (§5.1 normalises against the reference rung), so the median is the conservative choice as well as the more honest summary. Against the one on-transport data point available, 2.002s for `azure_ai/gpt-5.4-mini-copilot`, both are over-estimates by roughly 3–5x, which is the safe direction for a ceiling.

  This paragraph previously said `median_latency_s` "stays TBD here", and §3 said voice would stay non-operational until it did not. Both were deliberate and both have been overridden rather than quietly edited away: the reasoning that produced them is below, unchanged, because it is still the argument for replacing these two cells with a real measurement.
- **`azure_ai/gpt-5.4-mini-copilot` cannot be measured by this harness at all.** Twelve attempts all failed with the CLI's own refusal — the model is not served by the backend the CLI resolves, exactly the failure §0.1 describes. The one model that actually serves voice is therefore the one model whose voice accuracy cannot be benchmarked without a second transport.

**What the measurement did settle:** walking the generator over these accuracies reproduces `luna → sonnet` — precisely the ladder §3 published before any voice task existed. The shape was right; it just had nothing behind it until now. It is still not affordable: at $1.936 against a `BUDGET_USD` of 1.00 the voice ladder fails §1.1's cost invariant, the same way `planning` does and for the same reason — sonnet at rung 1.

**`‡` marks a rate blended from this deployment's own `usage_events` rather than a published price.** `azure_ai/gpt-5.4-mini-copilot` at **3.5167**/1M is 47 recorded events totalling 25,309 tokens for $0.089. Treat it with suspicion rather than confidence: a model named *mini* pricing within 3% of `claude-opus-5` (3.6082) is not what a mini model should cost, and the likeliest explanations are a gateway markup, a cost field the gateway populates differently, or too small a sample. It is recorded because the alternative — leaving the only model that actually serves voice unpriced — is what §2.7 warns against most directly ("a model nobody priced must not come out cheapest"). Re-derive it from a real price list before any ladder depends on it.

**`max_context` is the input window, and the output cap is the one that bites.** Filled 2026-09-15. The gateway models come from the gateway's own `/model/info`, which is authoritative and live; the two Anthropic figures come from the `claude-api` skill's model table (cached 2026-06-24) because this host authenticates Anthropic by OAuth with no stored key, so the Models API could not be queried directly. Re-check the Anthropic rows against `client.models.retrieve()` when a key is available. **`azure_ai/gpt-5.6-terra` added 2026-09-17, same source**: `/model/info` reports `max_input_tokens: 922000` for it, the same figure as luna. This column was measured while its cost cell was still assumed; since 2026-09-18 both are measured, so the contrast this sentence used to draw no longer exists.

| model | max input | max output |
|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 229,376 | **32,768** |
| `azure_ai/gpt-5.6-luna` | 922,000 | 128,000 |
| `azure_ai/gpt-5.4-mini` | 1,050,000 | 128,000 |
| `azure_ai/gpt-5.6-terra` | 922,000 | 128,000 |
| `claude-sonnet-5` | 1,000,000 | 128,000 |
| `claude-opus-5` | 1,000,000 | 128,000 |

**The free model's output cap is 32,768 — a quarter of every other model's.** Its input window is ample, so a naive read of `max_context` alone says it fits anything; the constraint is on what it can *write*. That is the same shape as the failure already recorded for this deployment, where a 32k-window gateway model raised `ContextWindowExceededError` on an obviously small prompt because the requested output budget consumed the whole window. A coding task whose patch exceeds ~32k tokens cannot complete at rung 0 no matter how accurate the model is, and it will fail in a way that reads like a context error rather than a capacity one. The column stores the input window because that is what §3's tooltip is for; the output cap is recorded here because it is the figure that will actually stop a leaf.

**An `n` marked with `*` is a latency sample, not an accuracy sample.** Five rows carry measured `median_latency_s` from `bench/pipeline_ab.py` while their `accuracy` is still TBD — four from 2026-09-15, plus `claude-sonnet-5` on `reviewer-gate` measured 2026-09-16 at **3.675s over n=20**, pooled from three separate passes (medians 3.705, 3.500, 4.190). That row was the one §5.1's ceiling derivation was blocked on. It is deliberately pooled across passes rather than taken from one: §5.1's own history has the same quantity reading 22.4, 33.2, 36.5 and 26.8 across four passes in a single day, so a single pass cannot establish one. A fourth pass of the same shape failed entirely — 14 calls, 14 errors — because the gate model was named `gpt-5.6-terra` rather than `azure_ai/gpt-5.6-terra`, and a bare id returns a 429 that reads like capacity (§9.3); its results are discarded, not averaged in. The `n` column means *accuracy* sample size everywhere else, and §2.6's blocking constraint on Opus depends on that reading, so the two must not be confused: **no row in this table yet carries a measured accuracy sample size for coding.** A row needs both before its task type can go operational.

**`multi-turn`'s jump to 91.7%/100% is a harness fix, not a capability change, and needs saying so nobody re-derives from an earlier run and gets confused.** Measured tonight (`bin/wc-bench.py --repeats 3`), both `azure_ai/gpt-5.6-luna` and `claude-sonnet-5` score far above what four models — including these two — scored earlier: the `multi-turn-recall` task was unwinnable until commit `7414482` fixed the harness, which had verified only the last turn of the exchange, leaving `join_fields` undefined so the round trip could never complete. All four models previously measured on it scored 0.0. The figures recorded above (`luna` 91.7%/n=12, `sonnet` 100%/n=12) are from the re-measured, fixed harness — the `multi-turn-recall` task itself now reads 1.0 for both models. Anyone diffing against a run predating `7414482` will see `multi-turn` jump and should read this paragraph before concluding either model improved.

**Four cells re-measured 2026-09-17 (`bin/wc-bench.py --repeats 3`), all at a larger `n` than what they replace.** Accuracy is the mean `pass_rate` across the task type's tasks; `median_latency_s` is the median of those tasks' own medians:

| model | task_type | old (small-sample) | new |
|---|---|---|---|
| `claude-opus-5` | comprehension | 50% at n=2 | **100% at n=12**, latency 11.9s |
| `claude-sonnet-5` | comprehension | 100% at n=2 | 100% at **n=12**, latency 6.0s |
| `claude-sonnet-5` | reasoning | 75% at n=2 | **83.4% at n=6**, latency 24.6s |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | long-context | 100% at n=10 | **83.3% at n=12**, latency 13.9s |

**The `claude-opus-5` / `comprehension` row is the one that matters downstream, and it reverses §3.** At n=2 Opus measured worse than Sonnet on comprehension (50% against 100%), which is why the 2026-09-16 amendment (commit `b782e4d`) rewrote §3's comprehension row to `claude-sonnet-5` alone — Opus was skipped under the generator's own "measured worse than the current rung" rule, correctly applied to the data that existed then. At n=12 Opus now ties Sonnet at 100%. Tied is not worse, so Opus is no longer skipped, and §3's comprehension row is corrected below to match. See §3 for the full account of why the row changed twice in two days.

**`reviewer-gate`'s accuracy cell stays `TBD` — pending §12's open gate-type decision, not for lack of a measurement.** Measured tonight with `bench/gate_accuracy.py`, the reviewer gate and the security gate disagree about which of the two candidate models is better, and both climb the same shared `reviewer-gate` ladder (§3):

| | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` |
|---|---|---|
| reviewer-gate accuracy | 96.4% (12 of 13 defects caught, 0 missed) | 78.6% (3 real defects waved through) |
| security-gate accuracy | 85.7% (2 of 3 vulnerabilities caught) | 92.9% (3 of 3 caught) |
| LRU `__repr__` fixture (labelled safe) | **rejected** — reproduces §4.5's documented miscalibration | **passed** correctly |

Luna is the better reviewer; Sonnet is the better security check. §3 mandates one shared `reviewer-gate` row set for all three gates, so a single `accuracy` figure has to stand for both, and whichever model is chosen leaves one gate running on its worse option. That is §12's open gate-type question to resolve, not a fact this table can average away — so both `reviewer-gate` rows (luna, sonnet) keep `accuracy = TBD` here **deliberately**: TBD because the choice is undecided, not because nobody measured it.

**Provenance of the `cost_per_1M_tokens` column, which comes from four different places now.** Anthropic figures (sonnet 1.5709, opus 3.6082) are blended from `usage_events` rows carrying `cost_basis='list'`. Azure figures (luna 0.0285, mini 0.5261) come from **gateway billing**, which is a separate source from this database and the reason they never reconciled with it (§2.5). `0.0000` for the self-hosted model is a property of the deployment, not a measurement. Terra's 0.0285 is **none of the above** — it is luna's rate, assumed onto an unbilled model by operator decision (marked `†` above), and it is the one figure in this column that is not a rate this deployment has actually observed. Every priced-and-measured one of them is a **rate**, independent of request size — which is the whole point of the unit change, since the previous per-request column silently encoded how large each model's historical jobs happened to be (§2.7).

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

**Model identity is resolved through an alias map before anything is recorded.** `nvidia/Qwen3.6-35B-A3B-NVFP4` and `vllm/Qwen3.6-35B-A3B-NVFP4` are one deployment under two labels (§2.5). The gateway answers a request for either with `model_served: nvidia/...`, so a harness that keys on the served string files results under a different model than the one the ladder chose. Two rows for one deployment splits its accuracy sample in half and its cost attribution in two. **Alias resolution happens before a row is written, never after**, so the table cannot hold the same deployment twice.

**`median_latency_s` is a median, and that has to be enforced rather than assumed.** The values recorded on 2026-09-15 were not medians — each was a single run's `total_s` lifted out of a four-run sample (vllm 33.2 where the median was 45.8; luna 14.7 where it was 15.2; sonnet 12.7 where it was 13.8). The column name asserted one thing and the cell held another, and because §5.1 derives every deadline and the whole latency ceiling from this column, a sample masquerading as a median propagates into two derived quantities without anything noticing. The figures above are now computed medians.

#### Measurement provenance, 2026-09-15

`vllm` on `coding` is measured at **66% (n=44), 95% interval 51%–78%**, across six tasks. Getting there took four passes and the sequence is the lesson:

| pass | tasks | n | result |
|---|---|---|---|
| first | 2 | 4 | 25% |
| more repeats | 2 | 20 | 55% |
| more **tasks** | 6 | 40 | 70% |
| all runs pooled | 6 | 44 | **66%** |

The first two are statistically consistent — a true 55% yields ≤1 success in 4 runs about 24% of the time — so 25% was ordinary small-sample noise. The move from 55% to 70% is different in kind: it came from **adding tasks, not repeats**. Repeats narrow the interval around whatever the existing tasks happen to measure; only new tasks change what is being measured. The suite had two `coding` tasks and one of them turned out to be an outlier. The last row is the same six tasks with the four earliest runs folded back in, which is the figure of record.

| task | result | median s |
|---|---|---|
| `coding-edit-chunks` | 5/5 | 15.9 |
| `coding-edit-top-scores` | 5/5 | 15.9 |
| `coding-edit-extend-cases` | 5/5 | 18.4 |
| `coding-algo` | 8/10 | 44.1 |
| `coding-edit-mutable-default` | 2/5 | 25.3 |
| `coding-bug-fix` | **3/10** | 29.6 |

**`azure_ai/gpt-5.6-luna` scored 24/24 on the same six tasks.** That control is what makes the numbers above readable: the tasks discriminate between models rather than being uniformly hard, so `vllm`'s failures are the model's and not the suite's.

Two corrections to the earlier reading, both of which cut against the alarming interpretation:

- **The "writes code well, repairs it badly" conclusion does not survive more tasks.** `vllm` scores 15/15 on three of the four repair tasks. `coding-bug-fix` (3/10) is an outlier, not a representative of its class — its failures are genuine logic failures on the specific order-and-slice bug, not a general inability to edit.
- **Two of the twelve failures were compliance, not capability.** On `coding-edit-mutable-default`, two runs fixed the actual bug correctly (`tags=None`) and failed only the assertion that the prompt's requested docstring be present. Counting them alongside a wrong answer conflates "cannot do it" with "did not do all of it" — worth knowing, because the pipeline's gates treat those identically while a human would not.

**Consequence for the ladder: the free rung on `coding` stands.** A rung-0 model at 66% behind an oracle that catches its failures (§4.2) is doing its job — it completes two thirds free and escalates the rest. The earliest figures suggested removing it; the better-measured one does not.

**But it does not meet §10.1's target, and the two must be reconciled.** That section sets **≥70% of leaves completing on the free rung**, and `coding`'s rung 0 measures 66% with an interval of 51%–78%. The target sits inside the interval, so this is not yet evidence the target is unreachable — but the point estimate is below it, and the honest reading is that the target was written before anything was measured and has never been checked against a number.

Three ways out, and the choice is not obvious enough to make in passing:

1. **The target is per-tree, not per-task-type.** `long-context` measures 100% on the free rung and is the other free-starting type; a weighted average across both could clear 70% while `coding` alone does not. This is the most likely resolution and needs `long-context`'s leaf share to settle.
2. **The target is aspirational** and should be restated as a floor that triggers review rather than a threshold the design claims to meet.
3. **`coding`'s rung 0 is genuinely wrong** and the ladder should start at Luna — but at 66% with an oracle catching the failures, the evidence does not support that today.

Recorded in §12 rather than decided here.

The `floor-add` control passed 10/10, so the invocation was sound throughout.

**On the latency column — the task mix is now equal, and equalising it changed the answer.** For part of 2026-09-15 accuracy and latency rested on different subsets, because only the two original tasks had been run on all three models; the multiplier therefore used `vllm`'s 36.5s from that harder pair rather than a figure polluted by four tasks the other models had never seen. Running `claude-sonnet-5` on the four edit tasks closed it, and all three models now have all six.

The correction was not cosmetic. **The reference model changed**: over the two hard tasks Sonnet was fastest (13.8s to Luna's 15.2s), over all six Luna is (12.8s to Sonnet's 15.5s). A multiplier is a ratio, so the model at the bottom of it sets every deadline in the system — and which model that is turned out to be a property of the task sample rather than of the models. See §5.1.

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
2. **The mini exclusion was reached by the wrong route, and is nonetheless kept — by decision, not by arithmetic.** Mini is cheaper per token than a model the design keeps, so the cost argument for excluding it does not stand. **`azure_ai/gpt-5.4-mini` is excluded from every ladder as an operator decision, recorded here on 2026-09-15.** The distinction matters for maintenance: a cost-derived exclusion would reverse itself the moment rates moved, so the ceiling would have to be re-tuned to keep mini out. A decision does not move when the numbers do. Nothing recomputes it, and re-admitting mini requires editing this line.
3. **The threshold itself must be restated in per-token terms.** `$0.015/request` is not a threshold that can be applied to the table above; it is a number that only sorts one historical workload mix.

**What the decision costs, stated plainly:** mini is the only model priced between luna (0.0285) and sonnet (1.5709) — a 55x gap that no remaining model occupies. Excluding it means every coding and long-context escalation from rung 1 lands directly on a model 55x dearer per token. If a future measurement shows luna failing often enough on a task type that the jump to sonnet dominates the tree budget, this line is the first thing to revisit.

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
| `P(reach rung 2)` | 0.167 (`1/6`) | measured, **n=6** — one leaf of six. The cost table below is computed from the exact `1/6`, not from a rounded 0.17; at 0.17 every cell of it disagrees |
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

`coding` and `long-context` — the two three-rung ladders, and the only two that start free — are the only ones that fit unchanged. **Comprehension had no affordable ladder at all** on these inputs, and under §1.1 that made it non-operational until one of the assumptions changed or a cheap rung was measured for it. (This sentence also named `split-decision`, removed 2026-09-18 — see below.) Luna already holds a comprehension row awaiting accuracy, which is the cheapest way to fix it.

#### `split-decision` was removed on 2026-09-18, and is `comprehension`

Operator decision, taken on the evidence below. Every row, ladder entry and
readiness line for it is gone from this document and from
`bin/wc-seed-delegation.py`; its one capability row was deleted from the
database. Work that would have been a split-decision is classified
`comprehension` and takes comprehension's ladder.

**The type's own founding line said it was comprehension.** From the first
design, 2026-09-12 (commit `627e85a`), where the tier table gave it
`claude-sonnet-5` at $5.68/1k tasks — the same model, the same cost and the
same justification as the `comprehension` row directly above it:

> | split decision | `claude-sonnet-5` | 5.68 | judging scope is comprehension work, where cheap models fail |

**Nothing since distinguished the two.** Both entered at sonnet; both priced a
rung-0 tree at $3.736; §5.1 named them in one sentence as the two types with no
affordable ladder. Where they diverged is only in the fix: comprehension had a
cheap rung measured and cleared, `split-decision` never did.

**And it was never reachable.** It is the only task type with no bench task and
no classifier pattern — `classify()` could not return it, so no work has ever
been routed to it or measured against it. That is not an oversight to correct
but the consequence of the founding line: if judging scope *is* comprehension
work, the classifier calling it `comprehension` is right.

**What this costs, stated plainly.** If a future measurement shows that judging
scope needs a different model from ordinary comprehension, this decision hides
that: the two are now one type with one ladder and one accuracy figure. The
signal to watch for is comprehension accuracy splitting by task shape. Bringing
it back means a definition first — what a split-decision task looks like that a
comprehension task does not — which is the thing that never existed.

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
| comprehension | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | `claude-opus-5` |
| voice | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | — |

**The voice ladder above names two models that have never served a voice turn.** Production voice runs `azure_ai/gpt-5.4-mini-copilot` — 46 measured turns, and every `voice_turn_timing` row in the database is that model. It had no row in §2.6 at all until 2026-09-17, so no §1.1 invariant could see it and the published ladder described a configuration that has never existed. The row is added with what is measured; it is **not** ladder-eligible (no accuracy), so the ladder above is still what the generator produces — the discrepancy is between the ladder and reality, not between the ladder and the table.
| reasoning | TBD — **Luna must be benchmarked on reasoning** before a rung is set (an editorial hold; see below) | — | — |
| **reviewer-gate** | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` (see §4.3) | — |

**All three review gates share the `reviewer-gate` ladder** — stages 3, 4 and 5 climb the same rungs, so there is no separate `security-gate` task type and the table keeps nine rows. What differs is what happens at the top: a security rejection surviving the gate's top rung goes to a human rather than failing the leaf (§4.5).

Reasoning has no rung until Luna is benchmarked on that type. The existing 86%/75% accuracy figures are from a small sample (n=—) and must be re-verified against production request shapes before any rung is set.

**One row of this snapshot is not what the generator would produce from today's table, and it was previously stated as though it were.** Recorded here rather than left for whoever next compares the two:

- **`reasoning`'s hold is editorial and the generator does not enforce it.** With today's data the only ladder-eligible reasoning model is `claude-sonnet-5` (mini is cost-excluded by §2.7, Luna and Opus are TBD), so the generator returns `[claude-sonnet-5]` — not the "TBD" this table shows. The hold is a judgement that an 83.4% n=6 figure should not set a rung, and it lives in prose. **Until it is either enforced in code or lifted by measurement, `reasoning` must stay non-operational (§1.1)** — that flag, not this table, is what actually prevents the rung being used.

The general point applies beyond this one: this table is a statement of intent, the generator is the mechanism, and where they disagree the generator wins at runtime. §1.1's "every rung is backed by a row" checks the snapshot against the table, not against intent.

**`comprehension`'s row above supersedes the 2026-09-16 amendment (commit `b782e4d`), and the reason is kept visible rather than overwritten — this row has now changed twice in two days, and the second change reverses the first.**

- **2026-09-16 (`b782e4d`):** with `azure_ai/gpt-5.6-luna`'s comprehension row still TBD, the only two ladder-eligible models were `claude-sonnet-5` (100%, n=2) and `claude-opus-5` (50%, n=2). Cheapest-first ordered Sonnet before Opus, and step 3 skipped Opus as *measured worse than the current rung* — a correct read of the generator's own rule against the data that existed that day. The amendment rewrote the row from `sonnet → opus` to `sonnet` alone on exactly that evidence, and said so explicitly rather than silently.
- **Since then, the data changed twice, not once.** Luna's comprehension row was filled in this morning (58.3%, n=12), which makes Luna — cheapest and now ladder-eligible — the real rung 0, ahead of Sonnet. And `claude-opus-5` on comprehension was re-measured today at **n=12: 100%, tying Sonnet** — not "measured worse" any longer, so step 3 no longer skips it.
- **Neither edit was a mistake; the rule was applied correctly to two different tables.** The 2026-09-16 amendment is exactly what §3 elsewhere warns against building a rung on: a figure "from a small sample (n=—) [that] must be re-verified against production request shapes before any rung is set". That is precisely what happened here — an n=2 figure was used to justify dropping Opus from the ladder, and re-measurement at n=12 reversed the drop. The row above, `luna → sonnet → opus`, is what the generator now produces from the current table.

**Voice is latency-bound, not accuracy-bound.** A spoken exchange is the most latency-sensitive path in the product, so the voice ladder starts at Luna and climbs only to Sonnet; Opus is not a voice rung at any accuracy. The argument for keeping voice non-operational was that a ladder ordered on cost alone is the wrong ordering for the one task type where latency is the binding constraint.

**Overridden 2026-09-18 by operator decision: voice is operational, on borrowed CLI latency (`◊` in §2.6).** The ladder is unchanged — `luna → sonnet` — because latency is not an input to §3's generation, which sorts on cost. What the override changes is only whether §1.1 lets the type flip: the two cells that were TBD now carry numbers, so the worst-case path computes (2,231.9s against a 2,900s ceiling) instead of being refused as missing data.

The objection above is not answered by this, only set aside. Voice remains the one task type whose ladder is ordered by the wrong quantity, and its rung 0 and rung 1 are still two models that have never served a voice turn on this deployment. The measurement that would settle it is a run of the four voice tasks through `routes/voice.py`'s own transport, which no harness does today.

Only `coding` and `long-context` start free (§4.1).

### 3.1 Coding, measured 2026-09-15

Exec-verified coding tasks. The table reflects every run as of 2026-09-15 — `bench/coding_accuracy_20260915.json` (4 models, 2 tasks), `bench/coding_vllm_n20_20260915.json` (`vllm`, 2 tasks, n=20 plus a floor control) and `bench/coding_edits_20260915.json` (`vllm` and `luna`, 4 edit tasks, n=20 each):

| model | accuracy | n | tasks | median latency | $/1M |
|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | **66%** | 44 | 6 | 26.8s | 0.0000 |
| `azure_ai/gpt-5.6-luna` | **100%** | 24 | 6 | 12.8s | 0.0285 |
| `claude-sonnet-5` | **100%** | 24 | 6 | 15.5s | 1.5709 |
| `azure_ai/gpt-5.4-mini` | **not measured** | — | — | — | 0.5261 |

All three models have run the same six tasks, so accuracy and latency rest on one mix. `vllm` carries more runs than the other two because it was measured twice more while the figure was still moving.

**The ladder is confirmed unchanged.** Walking the generator's rule over these rows — cheapest-first, skipping any model measured worse than the current rung — gives `vllm → luna → sonnet` exactly as the snapshot above shows. Equal accuracy is not "worse", so Sonnet survives as rung 2.

Three results the numbers force, none of which the design anticipated:

**The free rung measures 25% on coding.** It is rung 0 for the one task type that has an oracle, and it fails three attempts in four. Every failure costs a full escalation, so the free rung on coding is not mostly-free — it is mostly a first attempt that gets thrown away. §10.1's ≥70%-on-free-rung target is not reachable on coding at this accuracy, and that is a measurement about the model, not about the target.

**Luna matches Sonnet at 1/55th the cost per token.** Both pass 4/4; Luna's median score is 100.0 against Sonnet's 98.2 — at n=4 that difference is noise, but it is certainly not evidence for Sonnet. Rung 2 currently exists to catch what rung 1 misses, and on this evidence rung 1 misses nothing. Dropping Sonnet from the coding ladder is not justified on n=4 either, so the honest position is that **rung 2 is unevidenced rather than wrong**, and the cheapest way to settle it is more repeats, not more models.

**Mini could not be measured, and it is not 0%.** All four runs returned an error, not a wrong answer: the active backend does not serve that model and answers `429`, the routing failure §0.1 of `CLAUDE.md` describes — a capacity error's clothes on a routing problem. The harness reported it as an error rather than scoring it, which is the same discipline that keeps a truncated run from being recorded as wrong. Its row stays TBD, because recording 0% would assert a measurement nobody took.

**That last point is what settled §1.1's scoping question, on 2026-09-15.** Mini has a `coding` row, is not ladder-eligible (no measured accuracy), and *cannot be measured from this deployment* while the backend refuses to serve it. Under the original §1.1 — every row of a task type complete — `coding` could never go operational, blocked by a model that is not a rung and cannot become one. The invariant is now scoped to ladder-eligible rows, which resolves it; mini's unmeasurable row is exempt from completeness for exactly the same reason it is excluded from the ladder.

**Hover tooltip on chosen model:** each rung in the settings page displays the model name with a hover tooltip showing all five measured fields for the current task type — accuracy, sample size (`n`), cost per request, median latency, and **context window** (`max_context`). This lets a reviewer see why a model was chosen without leaving the page. Context window is in the tooltip because it is the field that decides whether a rung can take the task at all: a model that is cheaper and more accurate is still the wrong rung if the input does not fit.

**Excluded outright:** `vllm/Qwen3.5-0.8B` (23% correct), `claude-haiku-4-5` (dearer and slower than Sonnet), `claude-fable-5` ($23.15/1k). **Excluded by cost ceiling (§2.7):** `azure_ai/gpt-5.4-mini` (and by extension all variants).

---

## 4. The coding pipeline — five stages

A coding leaf above the trivial floor passes through five stages in order. Each is an independent pass/fail gate.

### 4.1 Stage 1 — Generation

Produced at whatever rung the ladder currently points to. Attempt 1 for `coding` and `long-context` is the free model, at the per-type baseline derived in §5.1.

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

**When both exhaustion conditions are reached in the same step** — the 2-cycle cap *and* the gate's own top rung — the rung rule wins: the leaf is escalated to a human, not failed with a flag. The two rules have different outcomes and can fire together, so the order is stated here rather than left to whoever implements it. The reason is the one above: a false reject at the gate's ceiling cannot be told from a true one without judgement, and that risk does not disappear because the cycle count happened to run out in the same step. Escalating preserves work that may be correct; failing discards it.

A gate that rejects at every rung is reported with the rejection text at each rung attached, so the miscalibration is legible from the record rather than requiring a re-run to reproduce.

### 4.6 Blast-radius check

**Ordinary coding work was reaching `comprehension`, not `coding` (added 2026-09-18).** Until the row above existed, `coding` matched three narrow shapes only — a test suite, a large refactor or a database migration, and trivia. Everything else fell through to the default task type, which is `comprehension`:

```
fix the bug in the auth middleware   → comprehension
implement a retry decorator          → comprehension
add a function to parse the config   → comprehension
refactor this module                 → comprehension
debug why the login fails            → comprehension
```

That made §4.1's cost thesis unreachable in practice. `coding` is the only task type with an oracle (§4.2) and one of only two that start free — and it was receiving almost none of the work it exists for, while `comprehension`, the most expensive ladder in §2.6 and a **default rather than a category**, absorbed it. It also means `comprehension`'s measured accuracy is a figure over a mixed bag, not over comprehension.

The new row is score 3, the mid point, so §2.1's highest-score rule leaves its neighbours intact: `refactor.*large` still wins at 4, `simple|quick|minor` still at 1. Specificity keeps the other types too — `debug.*complex` outranks bare `debug` for `reasoning`, and `implement.*multiple` outranks bare `implement` for `planning`, both by literal-character count.

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

**`side_effecting_read` takes the read row here, and keeps stage 3.** This reverses a ruling made earlier on 2026-09-16 (see that revision in this file's history), which had put it on the write row by analogy with §2.3 and §4.7 — both of which align `side_effecting_read` with `True` for their own purposes, transport eligibility and stage count respectively. That analogy does not transfer to the bypass, and this section already says why: **"the bypass exists because a typo fix is cheap to verify by oracle — that argument is about writes, and it does not transfer to a read whose oracle is vacuous."** The bypass's scope is not "align with whatever `True` gets"; it is "the reviewer gate may be dropped only where an oracle can still check the output." A trivial write's output is code, and stage 2 can execute it. A trivial `side_effecting_read`'s output may be prose — precisely the shape §4.2 and §4.7 already say stage 2 cannot check — so a score-1 `side_effecting_read` on the old ruling ran stage 1, then a stage 2 that does nothing on prose output, then stopped: money spent, nothing verified. That is the exact hole this section exists to close, reproduced for the third value instead of closed by it. So a score-1 `side_effecting_read` now runs stages 1, 2 and 3, the same as a score-1 `mutates=False` task — stage 3 is retained because it is the only gate an oracle-vacuous read ever had, and the bypass does not get to remove a leaf's only real gate merely because the leaf also happens to cost money.

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
| combined latency ceiling | 1,500 — **derived, see §5.1** | per leaf, all five stages |

Attempt counts are **per gate, not shared pipeline-wide**. A review gate rejecting repeatedly points at bad generation, so its own cap is 1 — the retry happens at the generator, not at the reviewer.

### 5.1 Timeouts

The deadline for one attempt is three factors multiplied together:

```
effective_deadline = per_type_baseline × size_factor × model_speed_multiplier
```

None of the three is a hardcoded table of every combination — each is derived from something already recorded, which is what keeps this from becoming a deadline × model × size matrix nobody maintains.

**Per-type baseline.** What the task type costs on a mid-sized instance of that task, **on the fastest model measured for it**. That last clause is a dependency on the §2.6 table, so the baseline is stored as the published figure *paired with the reference it was calibrated against*, and re-derived whenever the reference moves:

```
TIER0_BASELINE_CALIBRATION = {           # (published baseline s, reference median_latency_s)
    "long-context": (45.0, 4.2),         # claude-sonnet-5
    "coding":       (90.0, 12.8),        # azure_ai/gpt-5.6-luna
}

baseline(task_type) = (published ÷ reference_at_calibration) × current_reference
```

The dimensionless quotient — 7.03125 for `coding`, 10.714 for `long-context` — is the durable half: how long a mid-sized instance of the type takes relative to *one* benchmark task of that type on the same model. It is what does not move when the fleet does. See the 2026-09-17 subsection below for why a fixed seconds map broke both task types the day a faster model was measured.

A type absent from this map never uses the free rung. An unknown type must receive the **longest** deadline, never the shortest — taken as the maximum of the **published** baselines only, and deliberately not of the derived ones.

The derivation deliberately stops at the boundary of a calibrated type. A derived baseline is `ratio × reference` and nothing bounds `reference`, so letting derived figures into this maximum couples every unmeasured type to the absolute speed of whichever calibrated fleet happens to be slowest: a `coding` fleet measuring 600s would hand every unknown type a 4,218.75s baseline, an inflation caused by a task type the unknown type has nothing to do with. The published maximum is a fixed, documented floor that no measurement can move, which is what "we have not measured this must not become a timeout" actually requires — a floor, not a figure tracking someone else's fleet. This is also the semantics the fixed map had, so the unknown-type rule did not move when the calibrated types started deriving.

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

`600` satisfied none of the three. It has been replaced — see the derivation below.

#### Run for real, 2026-09-15 — and the ceiling is too low

Coding latencies are now measured (§2.6), so the worst case above is no longer hypothetical. **The free model is the slowest thing in the ladder**, which is the fact that decides this:

All three models have now run the **same six `coding` tasks**, so these are medians over an identical task mix rather than over whatever each model happened to be given:

| model | `median_latency_s` (coding, 6 tasks) | n | multiplier |
|---|---|---|---|
| `azure_ai/gpt-5.6-luna` | 12.8 | 24 | **1.00** (reference — fastest ladder-eligible on `coding`) |
| `claude-sonnet-5` | 15.5 | 24 | 1.21 |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | 26.8 | 44 | **2.09** |
| `azure_ai/gpt-5.6-luna` on `reviewer-gate` | 11.1 | 9 | 0.87 | *(superseded: re-measured at 6.055 over n=56 on 2026-09-17, below)* |

**The reference moved from Sonnet to Luna when the task mix was equalised**, and that is the whole argument for insisting on one: measured over the two hard tasks alone Sonnet was faster (13.8 against 15.2), measured over all six Luna is faster (12.8 against 15.5). Neither model changed. The earlier ordering was an artefact of Sonnet having run only the harder half, and a multiplier built on it would have been scaling every deadline against a reference that does not hold.

```
multiplier sum = generation (2.09 + 1.00 + 1.21) + 3 gates (3 x 0.87)
               = 4.30 + 2.60 = 6.90

score 3 (size 1.0):  90 x 1.0 x 6.90 =   621s   vs 600s ceiling — over by  21s
score 4 (size 1.5):  90 x 1.5 x 6.90 =   932s   vs 600s ceiling — over by 332s
score 5 (size 2.0):  90 x 2.0 x 6.90 = 1,242s   vs 600s ceiling — over by 642s
```

**All three fail.** A `coding` leaf at score 3 — the ordinary case, `write.*test.*suite` — cannot complete its worst-case path inside the 600s ceiling, and the binding score-5 case misses by more than double.

**Two corrections are folded into the figures above, and the second is the instructive one.**

First, an earlier set (sonnet 10.7, luna 12.8, vllm 22.4) predated the 2026-09-15 coding run and is simply superseded.

Second — and this is why the column now carries an `n` — **the values that replaced them were not medians.** Each was one run's `total_s` lifted from a four-run sample: vllm was recorded at 33.2 when the median of its four runs was 45.8, luna at 14.7 against 15.2, sonnet at 12.7 against 13.8. The column asserted "median" and held a sample. Because §5.1 derives every per-attempt deadline *and* the combined ceiling from this one column, a sample wearing a median's name propagates into two derived quantities with nothing in between to catch it. The figures above are computed medians, and `vllm` is now an `n=20` median rather than an `n=4` one.

Note what the correction did **not** do: it did not move the decision. The sum went 7.39 → 7.16 and the binding case 1,331s → 1,289s, both comfortably under the ceiling (both have since moved again — see the six-task figures below). The derivation was wrong and the conclusion survived it — which is the argument for deriving rather than choosing, not against it.

#### Decided 2026-09-15: option 1, and the ceiling is 1,500s

**Option 2 was rejected, and its own arithmetic is the reason.** The figures it quotes — score 3 at 478s, score 4 at 717s — both imply a multiplier sum of 5.31, which is `1.20 + 1.00 + 3.11`: luna, sonnet, and the three gates. It reaches those numbers by **dropping the free rung**, not the top one. So option 2 is not a smaller attempt budget; it is the removal of the free tier from `coding` — one of only two task types that start free (§4.1) — measured against a §10.1 target of ≥70% of leaves completing on the free rung. It fixes a latency number by abandoning the cost thesis, which is not a trade this design can make silently.

**Option 1 it is: the ceiling is derived from the worst-case path, not chosen.** The binding case is the highest score `coding` can reach, and that is **5, not 4** — §2.1 takes the highest score among all matched patterns, so a task matching a coding pattern *and* the score-5 `implement.*multiple|coordinate.*agent|orchestrate` pattern is classified `coding` at score 5. Budgeting to score 4 would leave the ceiling below the worst case for a task the classifier produces by ordinary means.

**Which latency a gate uses had to be pinned before this could be computed at all.** §5.1 says a gate takes "the gate model's multiplier and the leaf's own task type", and for a gate running on luna against a `coding` leaf those two point at different rows: luna's `coding` row (12.8s) and luna's `reviewer-gate` row (11.1s). The choice moves the worst case by well over a hundred seconds, so it is not a detail.

**A gate uses the `reviewer-gate` row when one exists for that model**, falling back to the leaf's task-type row when it does not. The gate row is the direct measurement of the call being timed — a gate prompt carries the code plus the task description and returns one line, which is a different shape from a generation call, and §2.6 holds a row for it precisely so that shape is measured rather than inferred.

```
reference = 12.8s   (luna, fastest ladder-eligible on coding over all 6 tasks)

generation   (26.8 + 12.8 + 15.5) / 12.8 = 4.3047
gates        3 x (11.1 / 12.8)            = 2.6016
                                     sum  = 6.9063

binding case: coding, score 5, size factor 2.0
90 x 2.0 x 6.9063 = 1,243s
```

The exact sum is `6.90625`, giving `1,243.125s`. An earlier revision of this line
multiplied the sum already rounded to 6.90 and so printed 1,242s. The startup check
of §1.1 recomputes this from the table at full precision, so the figure written here
is the one it must reproduce.

**Ceiling = 1,500s**, and the margin is the point. The binding case lands at **1,243s**, leaving 257s — about 17%. A ceiling set flush to the worst case would be a coincidence rather than a margin: the next re-measurement of any of these latencies breaks it, and the failure mode is a leaf killed after paying for all five stages.

The re-measurement history makes the case better than any argument could. `vllm`'s coding latency has read 22.4, then 33.2, then 36.5, and now 26.8 across four passes at the same task type on the same day; the multiplier sum has read 7.40, 7.77, 7.39, 7.16 and now 6.91; and the reference model itself changed identity once the task mix was equalised. Every one of those readings was taken honestly and every one would have been used. **The ceiling has held at 1,500s throughout, which is the only reason none of it mattered** — and that is an argument for the margin, not for the arithmetic.

**An earlier revision of this section stated a multiplier sum of 7.77 and a worst case of 1,399s, and those reproduce from no reading of §2.6** — against the values available at the time, the two defensible gate choices gave 7.3937 and 8.2441, and 7.77 was neither. It came from assuming a gate multiplier of 1.00 instead of deriving one. This matters more than a typo would, because §1.1 recomputes this arithmetic at startup and refuses to load when it disagrees with the stored ceiling: a number nobody can reproduce would have been compared against on every boot.

Every version of this derivation so far has landed under 1,500s, so **the ceiling has never been wrong — only successive attempts at the arithmetic behind it.** That is an uncomfortable record for a quantity the design calls derived, and it is the reason §11 now asserts the derivation itself rather than the stored value.

#### Decided 2026-09-16: the formula above is incomplete, and 1,243.125s is a lower bound

The derivation sums three gate calls, each run once, each at the gate's entry rung. **The pipeline this design specifies does not behave that way**, and 0.19.0 is the first release to implement both halves, which is how the disagreement surfaced. Two terms are missing:

- **The gate climbs (§4.3, §4.5).** "The reviewer itself only climbs (`luna → sonnet`) if it keeps rejecting output from the generator's top rung." Each of the three model gates can therefore make a second call, at `claude-sonnet-5` rather than at Luna.
- **Security re-runs (§4.5).** `SECURITY_RERUN_CAP` permits two generation → security review → fix → security review cycles, and a cycle contains generation work as well as a gate call.

Where §5.1 and §4.3/§4.5 disagree, **§4.3/§4.5 win and this formula is corrected**, because §5.1's own decision above is that the ceiling is *derived from the worst-case path, not chosen*. A formula that models less than the pipeline does is not a derivation of the ceiling; it is a derivation of something cheaper than the ceiling has to cover.

**Measured 2026-09-16, and the ceiling holds.** The gate-climb term needed `claude-sonnet-5`'s `median_latency_s` on `reviewer-gate`, which §2.6 now records at **3.675s over n=20**, pooled from three passes. With the climb included:

```
reference = 12.8s   (luna, fastest ladder-eligible on coding)

generation   (26.8 + 12.8 + 15.5) / 12.8 = 4.30469
gate entry   3 x (11.1  / 12.8)          = 2.60156
gate climb   3 x (3.675 / 12.8)          = 0.86133
                                    sum  = 7.76758

binding case: coding, score 5, size factor 2.0
90 x 2.0 x 7.76758 = 1,398.2s   against the 1,500s ceiling — 101.8s of margin
```

**The climb is affordable because sonnet's gate is fast, not because the term is small.** At 3.675s a gate call on sonnet is *less than a third* of one on Luna (11.1s), so climbing costs less than the entry rung it climbs from. Had sonnet's gate matched Luna's, the same path would be **1,711.4s** and the ceiling would already be broken. That is the sense in which this measurement was load-bearing rather than confirmatory.

**Sensitivity, because the margin is now thinner than it was.** Each additional second of sonnet's gate median adds `3 × 180 ÷ 12.8` = **42.2s** to the worst case. The break-even is a median of **6.09s**; the three passes read 3.705, 3.500 and 4.190, so even the slowest gives 1,419.9s with 80.1s to spare. A re-measurement above 6.09s breaks the ceiling and must re-derive it rather than be averaged away.

**One term is still not modelled: the security re-run.** §4.5's `SECURITY_RERUN_CAP` permits two generation → security review → fix → security review cycles, and a cycle contains generation work. Modelling it requires knowing which rung backs a re-run's fix-generation call, and that is precisely §12's open gate-type item — so pricing it here would decide that question in passing, which the same "derived, not chosen" rule forbids. **1,398.2s therefore remains a lower bound**, though a far tighter one than 1,243.125s was. `coding` cannot go operational until that term is either modelled or ruled out of the path.

`reviewer-gate` also remains non-operational on its own account: both its rows still lack a measured *accuracy*, and this measurement was latency only (§2.6's `*` convention).

#### Decided 2026-09-17: the baseline is derived from the reference, not fixed

**Adding a faster model broke both task types that previously validated, and no model got slower.** `azure_ai/gpt-5.6-terra` was measured on 2026-09-16 at **7.2s** on `coding` against luna's 12.8s. On the next read of the table:

| task type | worst case before terra | after terra | ceiling |
|---|---|---|---|
| `coding` | 1,398.2s | **2,278.1s** | 1,500s |
| `long-context` | 1,382.7s | **1,517.7s** | 1,500s |

**The two failures have different mechanisms, and only one of them is this section's defect.**

**`coding` is the defect.** Terra displaced luna as the fastest ladder-eligible model, so the 1.0 reference fell from 12.8s to 7.2s and every multiplier on the type inflated by `12.8 ÷ 7.2 = 1.78×` — while `TIER0_DEADLINE["coding"]` stayed at 90, a figure calibrated when luna *was* the reference. The multiplier was derived from the table and the baseline was not, so the two halves of the same formula were normalised against different references. Nothing about the work got slower; the arithmetic simply double-counted a change of reference.

**The fix is to derive both halves from the same measurement**, which is what the calibration form above does. The normalisation then cancels:

```
baseline × multiplier = (ratio × reference) × (latency ÷ reference)
                      =  ratio × latency
```

So a model's deadline tracks **its own measured latency** rather than the fleet's spread, which is what this section's prose always described. Re-derived against terra's 7.2s reference:

```
baseline      (90.0 ÷ 12.8) × 7.2      =    50.625s
multiplier sum, first MAX_ATTEMPTS rungs plus 3 gates with climb
              (26.8 + 12.8 + 7.2) ÷ 7.2 + 3 × ((11.1 + 3.675) ÷ 7.2)
                                       =    12.65625
binding case  50.625 × 2.0 × 12.65625  = 1,281.4s   against 1,500s — 218.6s of margin
```

Note what the change does **not** do: it does not re-found 45 and 90. Neither was ever derived from a measurement — both were published before the latencies they are now paired with existed. The ratios inherit them exactly (`coding` 7.03125, `long-context` 10.714), so today's figures are unchanged wherever today's reference equals the calibration reference. What changes is that they can no longer silently disagree with the table.

**`long-context` is not this defect, and this fix does not clear it.** Its reference never moved: `claude-sonnet-5` at 4.2s was and remains the fastest ladder-eligible model on the type, because terra measured 6.3s — identical to luna. What terra did was **add a third rung**. The ladder went `vllm → luna` to `vllm → luna → terra`, and the generation sum went `13.9 + 6.3 = 20.2s` to `26.5s`. That is the real latency of a real rung, not a normalisation artefact, and the type is over the ceiling by 17.7s on honest arithmetic.

`long-context` therefore stays non-operational on a **live** blocker, separate from every other open item, and it has exactly the three resolutions §5.1 already names: re-derive the ceiling from the new worst case (option 1, the standing default), drop a rung (option 2 — here terra, the rung that is neither the free one nor the accuracy ceiling, at the price of an escalation step), or accept truncation (option 3). **Option 1 is the one this section's own rule points at** — the ceiling is derived from the worst case of the most expensive operational type, and the worst case moved — but no type is operational, so nothing is harmed by leaving the ceiling at 1,500 until terra's real price lands and the ladders are recomputed. Deciding it before that would set the ceiling from a table that is about to change.

#### Re-measured 2026-09-17: the gate rows were the stalest input, and they clear `long-context`

The three model gates are **52% of `coding`'s multiplier sum**, so `reviewer-gate` is the single most load-bearing row in this section — and it was carried at `n=9` and `n=20` while every generation row had been re-measured at `n=12` or better. Re-derived from `bench/gate_accuracy.py` at **n=56 per model**, pooled across the reviewer and security gates (both are the same call shape: code plus task description in, one line out):

| model | stored | re-measured (n=56) | |
|---|---|---|---|
| `azure_ai/gpt-5.6-luna` | 11.1 (n=9) | **6.055** | the stored value was 1.83× too high |
| `claude-sonnet-5` | 3.675 (n=20) | **4.605** | the stored value was 25% too low |

```
coding        gates 6.15625 → 4.44167   worst 1,281.4s → 1,107.8s   margin 218.6s → 392.2s
long-context  sum  16.86310 → 13.92381  worst 1,517.7s → 1,253.1s   margin  −17.7s → 246.9s
```

**`long-context` clears the ceiling on this measurement alone.** Its breach was 17.7s against a gate row overstated by 5 seconds and sampled at n=9 — so the breach was an artefact of the stalest number in the computation, not of the third rung terra added. The rung is still real and still costs 6.3s of generation; it simply was never what put the type over.

Note what this does **not** settle. It does not vindicate `long-context`'s 4.2 anchor, which remains chosen rather than derived and still scales the whole type linearly — the type now passes with that anchor, which is a weaker claim than the anchor being right. And it does not touch `multi-turn` (1,515.7s), `reasoning` (1,658.0s) or `comprehension` (1,772.4s), which stay over because they are uncalibrated and take the fixed 90s baseline against derived multipliers — the defect this section fixed for calibrated types only.

**These figures take effect when the rows are seeded.** `bin/wc-seed-delegation.py` and §2.6 above carry the re-measured values; the live `delegation_capability` still holds the stored ones until a seeding run is made, so the Delegation page will keep reporting `long-context`'s breach until then.

**What the fix costs, stated plainly.** A faster fleet now yields *shorter* absolute deadlines. That is correct if a production task's duration scales with a benchmark task's on the same model, and wrong if production tasks have a fixed absolute size the benchmark does not capture. The ratio is where that assumption lives, and it is the least-evidenced quantity in this section: both values are inherited from figures that were estimates. A measured distribution of real leaf durations per task type (§10) is what would replace them, and until it exists the ratios should be treated as the calibration they are, not as measurements.

The derivation is the durable part, not the number. `1,500` is what today's §2.6 produces; it is recomputed whenever a ladder or a measured latency changes, and it is **not** an independent constant to be tuned on its own. §1.1 enforces that by refusing to start when the two disagree — so a future re-measurement that pushes the worst case past 1,500s stops the system at load rather than truncating leaves in production.

**What this ceiling costs, stated plainly:** 1,500s is 25 minutes for a single coding leaf's worst case. That is tolerable only because these are background orchestrator leaves with no one waiting on them, and because the worst case requires every rung and every gate to run to its full deadline. It is not a latency budget for anything interactive. If §10 shows leaves routinely approaching it rather than finishing early, the right response is to look at why the free rung fails 75% of the time on coding (§3.1), not to raise the ceiling again.

Note what drives it: the free rung is **2.09x slower than the model it exists to avoid**. Its multiplier alone spends 188s of a score-3 leaf's budget. The free tier buys cost, and it is charged for in latency — §10.1's free-rung target measures the cost side of that trade while the ceiling enforces the other, and the two have never been reconciled against one set of numbers until now.

**Startup validation (§1.1) checks this.** For every operational task type, the computed worst case is compared against the ceiling, and a ceiling below it fails the same way a blank field does — at load, naming the task type and both numbers. The check needs measured `median_latency_s` values, which an operational task type already guarantees.

**How the ceiling is enforced.** It is evaluated **before each stage starts**, never mid-stage. If the elapsed time plus the next stage's deadline would exceed the ceiling, the leaf stops there. Interrupting a stage in flight would pay for a model call and discard its verdict, which is the most expensive possible way to save time.

The comparison is strict. A projection landing **exactly on** the ceiling has not exceeded it, so that stage runs — a `>=` here would cut a leaf that fits, which is the same waste the "never interrupt a stage in flight" rule exists to avoid.

#### Added 2026-09-17: enforcement is a knob, and it is off by default

**The ceiling is computed and reported always; whether it *blocks* is a setting.** `delegation_enforce_latency_ceiling` (§9.2, stored in `settings`, default **off**) governs both places the ceiling can stop something: §1.1's refusal to start or to flip a type operational, and the per-stage runtime check above.

The default is off for a reason that is about evidence, not about convenience:

- **The ceiling is derived from the worst case of the most expensive *operational* task type. Nothing is operational.** So today it is derived from nothing — a placeholder that has held through seven successive re-derivations of the arithmetic behind it. Enforcing a constant that is currently unbound by any measurement is choosing it, which this section's own "derived, not chosen" rule forbids.
- **Every task type currently over the ceiling is over for a reason that is not its latency.** `multi-turn` (1,515.7s), `reasoning` (1,658.0s) and `comprehension` (1,772.4s) have no entry in `TIER0_BASELINE_CALIBRATION`, so each pairs a *fixed* 90s baseline with *derived* multipliers — precisely the defect the 2026-09-17 change fixed for calibrated types only. Recomputed with a calibrated baseline they land at 923.6s, 1,152.8s and 830.8s, all comfortably inside. Blocking those types today would be blocking them on a formula known to be wrong for them.

**What "off" does not mean.** Off means **not blocking**; it never means **not measured**. With the knob off, a breach still appears in `CapabilityTable.latency_ceiling_breaches`, still shows on the §9.2 page as a warning distinct from a blocker, still logs at boot, and the runtime check still returns `exceeded=True` alongside `proceed=True`. §10 asks for the rate of ceiling cuts to be monitored, and that rate is wanted *precisely* while nothing is being stopped by it — a knob that suppressed the measurement along with the block would make the decision to turn it on one taken with no evidence.

**What the knob does not gate.** An **incomputable** worst-case path. Missing data and a breach are different failures: "we cannot tell how long this takes" is not relaxed by deciding not to enforce a limit, and §1.1 refuses on it either way.

**Turning it on is validated before it is stored.** Enabling enforcement can invalidate a task type that is already operational, and writing the flag first would leave a deployment that refuses to start on its next restart. Turning it **off** is never validated — relaxing a blocking invariant cannot break another one, and an operator must never be trapped in the enforcing state with no way back.

**A leaf stopped by the runtime check emits `latency_ceiling_exhausted` (§6.1) and terminates.** A leaf that is over the ceiling while enforcement is off emits **no signal at all**: it is not being stopped, and emitting the terminating signal for a leaf that carries on would make the log say the opposite of what happened.

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

**Startup validation (§1.1):** every model named in any ladder is checked against the valid options in the model combo box at config load. Combined with the benchmark-table completeness check, this ensures no blank fields **in any ladder-eligible row**, every model resolves to a valid backend-and-model pair, every rung is backed by a §2.6 row, no task type is left with an empty ladder after cost-ceiling exclusion, no operational task type has a worst-case path exceeding the combined latency ceiling (§5.1), and no operational ladder has an expected tree cost above `BUDGET_USD` (§2.7). All six checks fail loudly if broken.

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

**Measured 2026-09-15, this target is unreachable on `coding` and the reason is not the deadline.** The free model scores **25%** on exec-verified coding tasks (§3.1), so roughly three leaves in four escalate on correctness alone. No deadline adjustment moves that number — it is the model's accuracy, not its speed, and the timeout rate will read clean while the target misses by a factor of nearly three.

That leaves three options, and this target cannot be assessed until one is chosen:

1. **Re-scope the target per task type.** 70% may be right for `long-context`, where the free model measures 100% (n=10), and wrong for `coding`. A single number across task types averages two very different models-on-tasks.
2. **Lower it for `coding` to what the measurement supports**, and treat the free rung there as a cheap filter that catches a quarter of the work rather than most of it.
3. **Stop starting `coding` free.** Luna measures 100% at $0.0285/1M — 55x cheaper than Sonnet per token and, on n=4, no less accurate. A ladder starting at Luna would complete most coding leaves on rung 0 at a cost that is still negligible against the tree budget (§2.7 admits Luna at any rung).

Option 3 is the one the measurements point at, and it is a larger change than it looks: it would make `coding` the second task type not to start free, so §4.1 and the "only coding and long-context start free" rule in §3 both move with it.

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
| `TIER0_BASELINE_CALIBRATION` contents | asserting the two present keys — also assert the other types are **absent**, and that each entry carries a reference beside its published seconds |
| the baseline is derived, not published | asserting the published 45/90 — change the table's fastest ladder-eligible latency and assert the baseline moves with it. This is the 2026-09-17 defect: a baseline read out of the map keeps 90 while the multipliers renormalise, and `coding` lands at 2,278.1s -- **778.1s over** the 1,500s ceiling -- with no model having got slower |
| the reference cancels | asserting one model's deadline in isolation — assert a model's deadline is unchanged when only its **neighbours'** latencies move, which is the property deriving both halves from one reference buys |
| unknown task type | letting `KeyError` escape, or defaulting to the shortest deadline — assert it gets the **longest**, and assert **both directions** of the coupling: a calibrated type deriving *below* its published baseline must not drag the unmeasured type down, and one deriving far *above* it must not drag the unmeasured type up. The upward direction was the untested one, and the maximum was unbounded above until 2026-09-17 |
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
| ceiling vs attempt budget | asserting the ceiling is enforced — assert startup **fails** for an operational task type whose computed worst case (`baseline x size x [sum(m_rung) + sum(m_gate)]`) exceeds the ceiling. Both sides: an operational `coding` type at score 5 (1,242s) **must load** against the 1,500s ceiling, and raising any ladder-eligible `median_latency_s` enough to push the sum past 1,500 **must stop it loading** |
| the derivation reproduces | asserting the stored ceiling is under some number — recompute the sum from §2.6's rows inside the test and assert it equals what §5.1 states. Three successive revisions of this arithmetic were wrong (7.40, 7.77, 7.39) while the conclusion happened to survive each time; only a test that reproduces the sum from the table would have caught any of them |
| `median_latency_s` is a median | asserting the cell has a value — assert it equals the median of that model's recorded runs. The 2026-09-15 values were single samples (vllm 33.2 where the median was 45.8), and the column feeds both the deadline and the ceiling, so a sample here corrupts two derived quantities silently |
| an alias is one model, not two | keying on the served model string — assert that a request for `vllm/Qwen3.6-35B-A3B-NVFP4` answered as `nvidia/Qwen3.6-35B-A3B-NVFP4` records **one** row under the canonical name. Two rows for one deployment halve its accuracy sample and split its cost attribution (§2.5) |
| `MAX_SUBAGENTS_PER_LEAF` | asserting the pipeline stops — assert hitting 12 is a **human-flagged hard failure**, a different class from an exhausted escalation (§7), not a silent give-up |
| tree caps are enforced individually | asserting the tree terminates — assert `MAX_DEPTH` (3), `MAX_CHILDREN` (4) and `MAX_NODES` (40) each stop expansion on their own, with a tree that would breach only one of them at a time |
| a stale placement sample is refused | asserting placement works when samples exist — assert a sample older than **120s** reads as unknown and is **refused**, not treated as headroom. "Not known yet" and "has headroom" are opposite claims |
| sub-agents are not resumable | asserting a sub-agent returns a verdict — assert it is closed after its gate and cannot be resumed (§7) |
| the sub-agent record outlives the sub-agent | asserting the sub-agent closed — assert its input, output and verdict persist, tied to the parent leaf ID and tagged with the gate. §10's per-gate attribution reads from this record, so losing it makes every gate metric unattributable |
| every gate gets all four context parts | asserting the gate received the code — assert stages 3, 4 and 5 each receive task description, acceptance criteria, code, **and every prior gate's rejection reason** (§4.9). A gate judging code without intent context reproduces the oracle's blind spot, and nothing downstream would reveal it |
| re-benchmark flags a ranking flip | asserting the job runs and writes rows — assert that a fresh run which reorders two rungs on a task type **raises the flag**. Running the job is the easy half; the flag is what makes a silent reordering visible |
| the tooltip carries all five fields | asserting a tooltip appears — assert accuracy, `n`, cost, latency **and `max_context`** are all present (§3, §9.2). Context window is the field that decides whether a rung can take the task at all |
| the ceiling is derived, not configured | asserting the stored `1,500` — change a `median_latency_s` in §2.6 and assert the required ceiling moves with it; a ceiling that survives a latency change unchanged is a constant wearing a derivation's clothes |
| the gate multiplier uses its own task type | computing gate multipliers against the `coding` reference — assert a `reviewer-gate` multiplier is derived from the `reviewer-gate` rows, and that moving a `coding` latency does **not** change it |
| binding score is 5, not 4 | computing the worst case from the highest *coding pattern* score — §2.1 takes the highest score among **all** matched patterns, so assert a task matching both a coding pattern and the score-5 planning pattern is classified `coding` at **score 5** and budgeted at 2.0x |
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
| completeness is scoped to ladder-eligible rows | asserting every row must be complete — assert an operational task type loads with a TBD-accuracy row present (mini's `coding` row is the real case), and **fails** only when a *ladder-eligible* row has a blank column |
| exemption and ineligibility are one fact | testing them separately — assert no row is ever exempt from completeness while still being offered as a rung |
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
- **§10.1's ≥70% free-rung target is not met by `coding`'s measured 66%** (§2.6). The target sits inside the 51%–78% interval, so it is not yet disproved, but it was written before any measurement and has never been checked against one. Resolve by deciding whether it is a per-tree average (`long-context` measures 100% on the free rung and would pull the weighted figure up), an aspirational floor that triggers review, or a genuine constraint that `coding`'s rung 0 fails. **Do not treat the target as met.**
- **~~A task type's gate types are not covered by its own validation~~ — RESOLVED 2026-09-17.** Both remedies §12 offered were taken, and a measurement dissolved the premise of the third.

  **The gates were measured** (`bench/gate_accuracy.py`, n=28 per model per gate), and pooled into one `reviewer-gate` row they had been averaging two different behaviours:

  | model | reviewer | security |
  |---|---|---|
  | `azure_ai/gpt-5.6-luna` | **96.4%** (0 false accepts) | 85.7% |
  | `claude-sonnet-5` | 78.6% (3 false accepts) | 92.9% |
  | `claude-opus-5` | 89.3% (0 false accepts) | **96.4%** |

  So `reviewer-gate` (§4.3's reviewer, §4.4's QA) and `security-gate` (§4.5) are now **separate task types**, each getting the model that measures best at the job it does.

  **§12's affordability argument does not survive the measurement.** It priced `claude-sonnet-5` at reviewer-gate rung 1 at $1.868 — but sonnet is the *worst* reviewer of the three, so the ladder rule skips it as measured worse and it is never a rung. The concern was an artefact of the rows being unmeasured.

  **§4.3's climb rung moves off sonnet, and for the reviewer gate it disappears.** Nothing measured beats luna at reviewing, so `reviewer-gate` is a one-rung ladder with no climb. The security gate climbs `luna → opus`.

  **Remedy 1 — gate types must be operational first.** A task type may not route through a gate type that has not itself cleared §1.1. Gate types are exempt from the rule: a gate does not run gates, and requiring them to depend on each other would make the pair unsatisfiable. Enforced in `CapabilityTable.validate`.

  **Remedy 2 — gate ladders are priced as gate calls, not as leaves.** §2.7 priced every ladder with `TOKENS_PER_LEAF` (59,460), a measured *generation* leaf. A gate call is the code plus the task description in and one line out: **13,883 tokens**, measured over 168 calls. Pricing a gate as a leaf overstated it by **4.28×**, which is most of why `security-gate` looked unaffordable. Gate ladders also take their own attempt budget — §4.3 grants one climb, so `GATE_MAX_ATTEMPTS` is 2 and a third gate rung is as unreachable as a fourth generation rung.

  ```
  reviewer-gate   [luna]          $0.0158    fits
  security-gate   [luna, opus]    $0.3498    fits
  ```

  **One input here is PROVISIONAL and marked `†`.** A gate's reach probability is not measured. `GATE_REACH_PROBABILITY` is `(1.0, 1/6)`, where 1/6 is **borrowed** from §2.7's P(the generator reaches its top rung) — the precondition for a gate climb, and therefore an upper bound on it. The direction of the error is what makes it usable as a stand-in: an upper bound **overprices**, so a gate type that fits under this figure fits under the true one. A gate type that does **not** fit under it is the case that must not be trusted.

  **`coding`'s hold is LIFTED, 2026-09-18, by operator decision.** §12 held it because the gate-type question was open; that closed on 2026-09-17, and the remaining objection was §5.1's own — that the worst case excluded §4.5's security re-run and was therefore a lower bound. That term was then priced and found not to be a separate term at all: a security cycle **is** a generation attempt, so `MAX_ATTEMPTS` and `SECURITY_RERUN_CAP` bound the same loop. Correcting the formula to charge each gate once per attempt raised `coding` to 1,726s, and the ceiling was raised to 2,900s to cover it.

  `coding` now clears all six of §1.1's invariants — **$0.657** against a $1.00 budget, **1,726s** against a 2,900s ceiling — and does so with the ceiling **enforced**, not merely with enforcement switched off. Flipping it makes something route for the first time: `orchestrator.assign_model` returns the ladder's rung 0 for coding-classified tasks instead of `config.ANTHROPIC_MODEL`, which is `vllm/Qwen3.6-35B-A3B-NVFP4` at 66% measured accuracy. That is §4.1's cost thesis working as designed, and a real change in which model answers.

  **`bench/pipeline_ab.py` already runs leaves through the gates, and the rate at which a gate rejects the top rung twice *is* this number.** Until it is measured, both gate costs above and anything derived from them are provisional. `GATE_REACH_PROBABILITY_IS_PROVISIONAL` carries the same flag in code so it cannot be quietly promoted.
