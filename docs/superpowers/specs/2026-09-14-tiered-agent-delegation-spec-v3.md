# Tiered agent delegation — implementation spec v3

**Date:** 2026-09-14
**Status:** design complete, not implemented.

This document is standalone. The base design's measurement tables from the original design are still the evidence; everything an implementer needs to *build* is below.

---

## 1. Scope

Today `ModelRouter.assign_model` returns `config.ANTHROPIC_MODEL` on both branches — complexity is computed and discarded. This design fills that seam.

What must be **built**: the duplicate-branch fix in `ModelRouter.assign_model` so the computed complexity reaches the routing decision instead of being discarded (§3), the task classifier (§2), the coding oracle (§4.2), the three review gates (§4.3–4.5), the blast-radius check (§4.6), the settings page (§9.2). Everything else is configuration over existing mechanisms.

### 1.1 Startup validation

At config load the system checks four invariants and fails loudly if any are broken:

| invariant | condition |
|---|---|
| no blank fields | every row in the benchmark table from §2.6 has a value in every column; TBD is not permitted for an **operational** task type |
| model resolution | every model name named in any ladder (§3) resolves to a valid backend-and-model pair available in the model combo box (§9.3) |
| no empty ladder | after applying the cost-ceiling filter (§2.7), every **operational** task type must have at least one rung; no operational task type is left with an empty ladder |
| every rung is backed by a row | every `(model, task_type)` pair appearing in the §3 ladder snapshot has a row in the §2.6 table. A rung named in the snapshot with no backing row means the snapshot and the generator disagree, and the generator silently wins |

**Bootstrap exemption.** §2.6 ships mostly unmeasured, so a check that refused every TBD would mean the system could never start for the first time. Each task type therefore carries an `operational` flag, default **false**. The three checks above apply only to task types flagged operational; a non-operational task type may hold TBD in any column.

A non-operational task type is **not routed**. Work classified into it falls back to today's routing (`config.ANTHROPIC_MODEL`), exactly as if the kill switch (§9.1) were off for that type alone, and each such fallback is logged so the gap is visible rather than silent.

Flipping a task type to operational is the act that submits it to validation: at that moment every one of its rows must be complete and every rung must resolve, or the system refuses to start. This is the only way a task type becomes routable, so no type can go live on unmeasured data.

If any check fails the system refuses to start. The error lists every broken invariant, naming the task type and the column, so the operator can fix the data before deployment.

**Validation runs on every write, not only at load.** The benchmark table is live-editable (§9.2) and the ladders regenerate at runtime from it, so a check that only ran at startup would let an operator clear a `median_latency_s` on an operational row at 15:00, see routing carry on unchanged, and discover at the next restart — possibly weeks later, possibly mid-incident — that the system will not boot. A validation gap whose blast radius is delayed by an arbitrary interval is worse than one that fails immediately.

So the same four checks run at three moments, with the same code path and the same error text:

| moment | on failure |
|---|---|
| config load | refuse to start |
| any write to the benchmark table or to a versioned config value (§9) | reject the write, name the broken invariant and the column, leave the stored value unchanged |
| flipping a task type to `operational` (§9.2) | reject the flip |

A write is rejected only against the invariants of task types that are *already* operational, plus the type being written. Editing a non-operational type's rows stays free — that is the bootstrap path, and gating it would make the table impossible to fill in.

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

Anthropic models carry **no production rows**, so they cannot be blended. Their list rates stand: opus 5.00/25.00, sonnet 2.00/10.00, haiku 1.00/5.00, fable 10.00/50.00 USD per 1M in/out. **Comparing a blended Azure rate to an Anthropic list output rate is not like-for-like** and must not be done without saying so.

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

| model | task_type | accuracy | n | cost_per_request | median_latency_s | max_context |
|---|---|---|---|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | coding | TBD | — | ~0.00 | TBD | TBD |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | long-context | 100% | 10 | ~0.00 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | coding | TBD | — | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | long-context | TBD | — | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | comprehension | TBD | — | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | reasoning | TBD | TBD | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | voice | TBD | — | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.4-mini` | coding | TBD | — | ~0.015 | TBD | TBD |
| `azure_ai/gpt-5.4-mini` | reasoning | 86% | TBD | ~0.015 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | multi-turn | TBD | — | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | planning | TBD | — | ~0.003 | TBD | TBD |
| `azure_ai/gpt-5.6-luna` | reviewer-gate | TBD | — | ~0.003 | TBD | TBD |
| `claude-sonnet-5` | coding | TBD | — | TBD | TBD | TBD |
| `claude-sonnet-5` | long-context | TBD | — | TBD | TBD | TBD |
| `claude-sonnet-5` | comprehension | 100% | 2 | TBD | TBD | TBD |
| `claude-sonnet-5` | reasoning | 75% | 2 | TBD | TBD | TBD |
| `claude-sonnet-5` | voice | TBD | — | TBD | TBD | TBD |
| `claude-sonnet-5` | multi-turn | TBD | — | TBD | TBD | TBD |
| `claude-sonnet-5` | planning | TBD | — | TBD | TBD | TBD |
| `claude-sonnet-5` | split-decision | TBD | — | TBD | TBD | TBD |
| `claude-sonnet-5` | reviewer-gate | TBD | — | TBD | TBD | TBD |
| `claude-opus-5` | comprehension | 50% | 2 | TBD | TBD | TBD |
| `claude-opus-5` | reasoning | TBD | TBD | TBD | TBD | TBD |

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

Every row is a first-class datum. Ladders are generated at runtime by walking this table — sort cheapest-first per task_type, skip any model measured worse than the current rung on that task_type. The table is the source of truth; §3 is just the output. Cost per request is precomputed from blended MTD spend ÷ MTD requests so the ladder algorithm is a two-field sort-and-filter.

### 2.7 Per-model cost ceiling

A configurable threshold automatically excludes any model whose cost per request crosses it. Applied now: mini at ~$0.015/request exceeds the ceiling implied by the $1.00 tree budget and expected leaves per tree (§5), so **mini is excluded from all ladders.** Coding and long-context are left three-rung (`vllm → luna → sonnet`), and mini is removed from multi-turn, planning, voice, and reasoning.

#### A missing cost figure is not an exemption

§2.5 records that Anthropic models carry no production rows and cannot be blended, so `cost_per_request` is TBD for sonnet and opus in §2.6. A ceiling phrased purely over *blended* cost cannot evaluate a TBD and therefore excludes nothing — which would ban mini at $0.015/request while opus, the single most expensive model in the system, rode free on a missing number. That inverts the argument the ceiling exists to make.

So the ceiling is evaluated against `effective_cost_per_request`, which is the blended figure when one exists and a **documented estimate** when it does not:

```
effective_cost_per_request =
    blended MTD cost per request            if production rows exist
    list rate x tokens_per_request          otherwise
```

where `tokens_per_request` is that model's own measured figure (§2.5.1) when available, and otherwise the **highest** tokens-per-request observed across all models — the conservative direction, because an estimate that flatters an unmeasured model is how the exemption reappears. The list rates in §2.5 are the input for the estimate, and the resulting value is stored in the row **tagged as an estimate**, so a reviewer can tell a measured ceiling decision from an inferred one at a glance. An estimate is replaced by the blended figure the moment production rows exist.

**The consequence must not be papered over.** Against §2.5.1's production request shapes (~100k tokens) and §2.5's list rates, an estimated opus request lands far above the $0.015 threshold that excluded mini, and sonnet likely does too. Under this rule both become ineligible, and **every ladder whose only rungs are Anthropic models — comprehension and split-decision — goes empty, and therefore non-operational** (§1.1). That is the honest output of the design's own cost argument, not a bug in it.

Resolving it is a deliberate decision, not an automatic one, and it is the first thing to settle before implementation. Three options, and exactly one must be chosen and recorded here:

1. **Raise the ceiling** to a value that admits sonnet for gate and comprehension work, accepting the per-tree budget consequence and re-deriving `BUDGET_USD` (§5) from it.
2. **Measure a cheaper rung** for comprehension and split-decision — luna already holds a comprehension row awaiting accuracy — so those ladders have a rung under the ceiling.
3. **Exempt named gate models explicitly**, in the config and by name, with the cost consequence stated. An exemption that is written down and argued is defensible; one that arises from a missing number is not.

---

## 3. Escalation ladders

Ladders are generated at runtime by walking the benchmark table from §2.6. The generator:

1. takes the **ladder-eligible** models for the task type (§2.6) — a row exists, its accuracy is measured, and it survives the cost ceiling;
2. sorts them cheapest-first on `effective_cost_per_request` (§2.7);
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

Execution verification only: does the produced code parse, import, compile, or pass the test it was asked to satisfy. **QA and regression testing are not part of this stage** — they are stage 3.

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
| 2 — oracle check | yes | execution verification is as meaningful for a read as for a write |
| 3 — reviewer gate | **yes** | intent match is where a read-only task fails: a wrong answer, confidently delivered, is the whole risk |
| 4 — QA / regression | **no** | nothing was changed, so there is no regression surface to test |
| 5 — security review | **no** | the defects this gate looks for — command injection, unsafe file writes, secrets written out — all require a write |

The reasoning is that stages 4 and 5 both check for consequences of *changing* something. A task that changes nothing cannot produce them, so running those gates spends two model calls per leaf to confirm an invariant that already holds structurally.

Stage 3 stays because the failure mode of a read-only task is entirely a stage-3 failure mode: it returns something plausible and wrong, and nothing downstream catches it. The oracle checks that an answer was produced, not that it answers the question asked.

This rule applies to `mutates=False` only. **`side_effecting_read` is not covered by it** — it takes the full five stages, the same as `True`. It spends money or consumes an external rate limit, so it has real consequences to review even though it writes no local file, and §2.3 already refuses it a transport for that reason.

### 4.8 Trivial-task bypass

A task matching the score-1 `simple|small|quick|minor|fix.*typo` pattern runs **stages 1 and 2 only**. Stages 3–5 are skipped. The full five-stage pipeline applies to anything scoring above that floor.

**Precedence between §4.6, §4.7 and §4.8.** Three rules can each subtract stages, so the order they resolve in is fixed:

1. **Blast radius (§4.6) first.** It is the only rule based on what the task *did* rather than what its text predicted, so it overrides the trivial bypass: a score-1 task that modified more than `MAX_FILES_TRIVIAL` files runs stages 3–5.
2. **Trivial bypass (§4.8) next**, if blast radius did not override it — stages 3–5 skipped.
3. **Read-only (§4.7) last**, applied to whatever survives — stages 4 and 5 removed for `mutates=False`.

The net effect: a non-trivial read-only task runs stages 1–3, and a trivial read-only task runs stages 1–2, because the bypass had already removed stage 3 before §4.7 was reached. No rule ever *adds* a stage an earlier rule removed.

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
| combined latency ceiling | 600 | per leaf, all five stages |

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

**The combined ceiling still binds.** `effective_deadline` governs a single attempt; the 600-second combined latency ceiling from the table above governs all five stages of a leaf together. A leaf whose stages would individually fit but collectively exceed 600s is stopped by the ceiling, not by any per-attempt deadline.

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

Model-speed multipliers are **not** in this list: they are derived from the benchmark table (§5.1), so versioning the table versions them. Recording a derived value alongside its input is how the two drift apart.

### 9.1 One global kill switch

The entire design — classifier, all five stages, voice ladder, placement rules — ships behind **a single global kill switch**. Old routing or new routing, nothing in between. No per-gate switches, no phased ramp, no percentage-of-traffic rollout.

Per-gate toggles are rejected deliberately: each one multiplies the number of reachable states, and every combination is a configuration nobody has tested. One switch has two states, both of which can be verified.

**Rollback must be clean.** Flipping back to off is one action leaving no side effects: no leaf stuck mid-pipeline, no orphaned sub-agent, no dangling config. **The off path is tested and confirmed clean before the switch is ever turned on in production.**

### 9.2 Multi-agent settings page

A dedicated settings page in the web console surfaces the kill switch, the tunables from §5 and §10, the cost ceiling (§2.7), the per-task-type `operational` flag (§1.1), and the full benchmark table from §2.6 as an editable matrix. Each cell (`accuracy`, `n`, `cost_per_request`, `median_latency_s`, `max_context`) is inline-editable so the operator can update measurements without code changes. The ladders in §3 are regenerated at runtime from whatever data is in the table — editing it is live. Changes take effect without a deploy.

Because the matrix writes to the same database table the ladder generator reads (§2.6), an edit changes routing for the next leaf with no deploy and no restart. That is exactly why **every write is validated before it is stored** (§1.1), not only the `operational` flip: a live-editable table checked once at startup can be broken at any time and will not say so until the next restart. A write that would break an invariant of an operational task type is rejected with the offending column named, and the stored value is left as it was.

Hover tooltips on rung values in the settings page show all five measured fields for the current task type: accuracy, sample size (`n`), cost per request, median latency, and **context window** (`max_context`) — the same five the §3 tooltip shows, from the same row.

### 9.3 Compatibility with existing backend/model selection

The console exposes two combo boxes: **backend** (Claude Code CLI, or direct API to the LLM gateway) and **model**.

The router does not replace or bypass this. At every stage and every escalation it programmatically sets the same backend-and-model pair a person would set manually, **through the identical invocation path**. There is no second way to invoke a model.

A model is never selected as a bare string. Every routing decision returns **`(model, machine)`** together — a model chosen without its machine reaches a gateway that does not serve it and returns `429 "No deployments available"`, a routing failure wearing a capacity error's clothes.

**Startup validation (§1.1):** every model named in any ladder is checked against the valid options in the model combo box at config load. Combined with the benchmark-table completeness check, this ensures no blank fields, every model resolves to a valid backend-and-model pair, every rung is backed by a §2.6 row, and no task type is left with an empty ladder after cost-ceiling exclusion. All four checks fail loudly if broken.

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
| trivial bypass | asserting a typo fix succeeds — assert stages 3–5 **did not run** |
| blast-radius check | asserting a small-prompt task stays trivial — assert a task matching `simple|typo` that produces >3 file changes **still runs stages 3–5** |
| security re-run cap | asserting infinite recursion is impossible — assert a leaf that cycles through generation→security exactly 2 times **fails with human-flag on the 3rd** |
| `mutates` gates placement | asserting a read-only and a writing coding task take the same path — assert the writing one is refused a transport **while a transport has headroom** |
| all comprehension rungs | asserting comprehension only uses sonnet — assert rung 0 is sonnet, rung 1 is opus, rung 2 is absent; **rung 2 is empty by design** |
| read-only runs 1–3 | asserting a read-only task "skips the gates" — assert stage 3 **ran** and stages 4 and 5 **did not** |
| stage-subtraction precedence | testing §4.6, §4.7 and §4.8 in isolation — assert a **trivial read-only** task runs stages 1–2, not 1–3 |
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
| cost ceiling has no exemption | asserting mini is excluded — assert a model with **no blended cost** is ceiling-checked against its `effective_cost_per_request` estimate and excluded on the same threshold; assert the estimate is **tagged as an estimate** in the row |
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
