# Tiered agent delegation — implementation spec v3

**Date:** 2026-09-14
**Status:** design complete, not implemented.

This document is standalone. The base design's measurement tables from the original design are still the evidence; everything an implementer needs to *build* is below.

---

## 1. Scope

Today `ModelRouter.assign_model` returns `config.ANTHROPIC_MODEL` on both branches — complexity is computed and discarded. This design fills that seam.

What must be **built**: the task classifier (§2), the coding oracle (§4.2), the three review gates (§4.3–4.5), the blast-radius check (§4.6), the settings page (§9.2). Everything else is configuration over existing mechanisms.

### 1.1 Startup validation

At config load the system checks three invariants and fails loudly if any are broken:

| invariant | condition |
|---|---|
| no blank fields | every row in the benchmark table from §2.6 has a value in every column; TBD is not permitted at load time |
| model resolution | every model name named in any ladder (§3) resolves to a valid backend-and-model pair available in the model combo box (§9.3) |
| no empty ladder | after applying the cost-ceiling filter (§2.7), every task type must have at least one rung; no task type is left with an empty ladder |

If any check fails the system refuses to start. The error lists every broken invariant so the operator can fix the data before deployment.

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

Patterns are ranked by **specificity**, not list order. Resolution:

- `task_type` — the most specific matching pattern wins.
- `mutates` — if any two matched patterns disagree, the answer is **`True`**.
- Every multi-match logs a warning with the matched pattern set, so pattern refinement is driven by production data rather than guesswork.

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

**Consequence for the ladder:** the ordering survives — free is free, luna is ~5× cheaper per request than mini — but the *magnitudes* do not. `BUDGET_USD = 1.00` per goal buys roughly **335 luna requests or 67 mini requests**, not the thousands the original figures implied. Re-check that cap before implementation.

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
| `azure_ai/gpt-5.4-mini` | coding | TBD | — | ~0.015 | TBD | TBD |
| `azure_ai/gpt-5.4-mini` | reasoning | 86% | TBD | ~0.015 | TBD | TBD |
| `claude-sonnet-5` | comprehension | 100% | 2 | TBD | TBD | TBD |
| `claude-sonnet-5` | reasoning | 75% | 2 | TBD | TBD | TBD |
| `claude-opus-5` | comprehension | 50% | 2 | TBD | TBD | TBD |
| `claude-opus-5` | reasoning | TBD | TBD | TBD | TBD | TBD |

**Blocking constraint:** do not accept Opus as the reasoning entry rung until the number of runs used for every other model on that task type is recorded in the capability table. Record accuracy and sample size in the capability table. Do not accept Opus as the reasoning entry rung until that number exists.

Every row is a first-class datum. Ladders are generated at runtime by walking this table — sort cheapest-first per task_type, skip any model measured worse than the current rung on that task_type. The table is the source of truth; §3 is just the output. Cost per request is precomputed from blended MTD spend ÷ MTD requests so the ladder algorithm is a two-field sort-and-filter.

### 2.7 Per-model cost ceiling

A configurable threshold automatically excludes any model whose blended cost per request crosses it. Applied now: mini at ~$0.015/request exceeds the ceiling implied by the $1.00 tree budget and expected leaves per tree (§5), so **mini is excluded from all ladders.** This makes the coding/long-context ladders two-rung (`vllm → luna → sonnet`) and removes mini from multi-turn, planning, voice, and reasoning.

---

## 3. Escalation ladders

Ladders are generated at runtime by walking the benchmark table from §2.6, sorting each task type from cheapest-first and skipping any model measured worse than the current rung on that task type. The cost ceiling from §2.7 is applied first, removing models whose blended cost exceeds the threshold.

The table below shows the expected output of this computation against current production data. All accuracy values marked TBD must be filled before the ladders become operational.

| task_type | attempt 1 | attempt 2 | attempt 3 |
|---|---|---|---|
| coding | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` |
| long-context | `vllm/Qwen3.6-35B-A3B-NVFP4` | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` |
| multi-turn | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | — |
| planning | `azure_ai/gpt-5.6-luna` | `claude-sonnet-5` | — |
| comprehension | `claude-sonnet-5` | `claude-opus-5` | — |
| reasoning | TBD — **Luna must be benchmarked on reasoning** before a rung is set | — | — |
| split decision | `claude-sonnet-5` | — | — |
| **reviewer gate** | `azure_ai/gpt-5.6-luna` | (see §4.3) | — |

Reasoning has no rung until Luna is benchmarked on that type. The existing 86%/75% accuracy figures are from a small sample (n=—) and must be re-verified against production request shapes before any rung is set. The comprehension ladder is correct as-is: sonnet → opus → —, because no measured model beats sonnet on comprehension.

Only `coding` and `long-context` start free (§4.1).

**Hover tooltip on chosen model:** each rung in the settings page displays the model name with a hover tooltip showing its accuracy for the current task type, sample size, cost per request, and latency. This lets a reviewer see why a model was chosen without leaving the page.

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

`mutates=False` means the task touches no local files — it reads code, spends money, or queries an API without writing back. The question is whether stages 3–5 still apply to tasks where `mutates` is `False`.

The classifier routes a read-only task to the same ladders as any other task for model selection, but the review gates are the decision point. Stages 3–5 check **intent match, functional regression, and security**. A read-only task that calls an external API or computes something expensive still carries risk: wrong intent wastes money, and a security lapse in a network call or file read can leak data. But these tasks have lower blast radius than writes — they cannot break imports or silently corrupt the codebase.

Whether the cost of three additional gates outweighs the risk is unresolved. A read-only task that spends significant money on an API call might benefit from review even if it never touches a file. The blast-radius check in §4.6 handles writes; a separate threshold for read-only spend (e.g. API cost above $X triggers stage 3) is one option, but not yet specified.

### 4.8 Trivial-task bypass

A task matching the score-1 `simple|small|quick|minor|fix.*typo` pattern runs **stages 1 and 2 only**. Stages 3–5 are skipped. The full five-stage pipeline applies to anything scoring above that floor.

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

Per-task-type deadlines are the baseline (they encode task size). A **model-speed multiplier** is applied on top, so the same task type gets more time on the slow self-hosted model than on Sonnet. This avoids a full deadline × model table.

```
TIER0_DEADLINE = {
    "long-context": 45,   # seconds
    "coding":       90,
}
```

A type absent from this map never uses the free rung. An unknown type must receive the **longest** deadline, never the shortest.

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
| deadline expiry | `TIER0_DEADLINE` × multiplier | slowness only |
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
- deadlines and model-speed multipliers (§5.1)
- attempt and spawn caps (§5)
- the global kill switch (§9.1)
- circuit-breaker thresholds (§10)
- the cost ceiling (§2.7)
- the benchmark table (§2.6)

### 9.1 One global kill switch

The entire design — classifier, all five stages, voice ladder, placement rules — ships behind **a single global kill switch**. Old routing or new routing, nothing in between. No per-gate switches, no phased ramp, no percentage-of-traffic rollout.

> This reverses the addendum's per-gate kill switches. Per-gate toggles created partial states that are individually untested combinations.

**Rollback must be clean.** Flipping back to off is one action leaving no side effects: no leaf stuck mid-pipeline, no orphaned sub-agent, no dangling config. **The off path is tested and confirmed clean before the switch is ever turned on in production.**

### 9.2 Multi-agent settings page

A dedicated settings page in the web console surfaces the kill switch, the tunables from §5 and §10, the cost ceiling (§2.7), and the full benchmark table from §2.6 as an editable matrix. Each cell (`accuracy`, `n`, `cost_per_request`, `median_latency_s`, `max_context`) is inline-editable so the operator can update measurements without code changes. The ladders in §3 are regenerated at runtime from whatever data is in the table — editing it is live. Changes take effect without a deploy.

Hover tooltips on rung values in the settings page show the model's accuracy for the current task type, sample size, cost per request, and latency.

### 9.3 Compatibility with existing backend/model selection

The console exposes two combo boxes: **backend** (Claude Code CLI, or direct API to the LLM gateway) and **model**.

The router does not replace or bypass this. At every stage and every escalation it programmatically sets the same backend-and-model pair a person would set manually, **through the identical invocation path**. There is no second way to invoke a model.

A model is never selected as a bare string. Every routing decision returns **`(model, machine)`** together — a model chosen without its machine reaches a gateway that does not serve it and returns `429 "No deployments available"`, a routing failure wearing a capacity error's clothes.

**Startup validation (§1.1):** every model named in any ladder is checked against the valid options in the model combo box at config load. Combined with the benchmark-table completeness check, this ensures no blank fields, every model resolves to a valid backend-and-model pair, and no task type is left with an empty ladder after cost-ceiling exclusion. All three checks fail loudly if broken.

---

## 10. Observability

- Every attempt writes a usage row with `origin="orchestrator"`, **including failures**. A failed rung-0 attempt that writes no row makes the free tier look better the more it fails.
- Each gate writes **its own row with its own signal tag**.
- Every escalation records **which gate** rejected it, so repeated attempts are traceable to generation quality versus review calibration.
- **Elapsed time versus deadline** is logged on every attempt, success included, to calibrate the model-speed multiplier.
- **Cost per leaf end to end**, across every sub-agent call in every stage. The base design's per-1000-task table is generation-only and now understates a five-stage leaf.
- **Circuit breaker per gate:** a gate whose rejection rate crosses its threshold raises an alert rather than burning retries. A gate rejecting nearly everything is miscalibrated, not surrounded by bad code.
- **Human spot-check sampling:** a small percentage of leaves that passed every gate are sampled for manual review. All gates agreeing is itself an unverified assumption.

### 10.1 Free-tier target and its review trigger

**Target: ≥70% of orchestrator leaves complete on the free rung**, measured from `usage_events`, not asserted in a unit test. Below 70%, read the timeout rate first: a deadline set too tight and a gateway genuinely too slow need opposite fixes.

**Trigger for widening the free rung:** once `usage_events` shows **≥50 escalations** on `comprehension`, `multi-turn`, `planning` or `reasoning` where the paid rung's answer materially differed from the free model's, that threshold greenlights benchmarking a Sonnet-tier judge against the exec-verified labels. It replaces "someday" with a query.

### 10.2 Scheduled re-benchmark

The ladders are re-benchmarked **quarterly, as a scheduled job**, not as a prose reminder. The run compares fresh numbers against the values encoded in §3 and **flags any task type where the ranking between two rungs has flipped** — that is the signal an escalation order needs rewriting, not merely a number updating.

---

## 11. Verification

The router is a **pure function** of `(task_type, score, resource snapshot)` returning `(model, machine, deadline)`. No model calls, no I/O. The whole policy is testable by table without spawning anything.

| what | the trap |
|---|---|
| tier selection | asserting the model string alone — assert `(model, machine)` together |
| escalation ladder | asserting the final outcome — assert the *sequence* and the cap |
| all ladders in §3 | testing only the types named in prose — assert all nine rows, including the three that break positional order |
| rung 0 is scoped | asserting the free model appears somewhere — assert it is attempt 1 for `coding` and `long-context` and **attempt 1 for nothing else** |
| `TIER0_DEADLINE` contents | asserting the two present keys — also assert the other types are **absent** |
| unknown task type | letting `KeyError` escape, or defaulting to the shortest deadline — assert it gets the **longest** |
| deadline is configuration | hardcoding the number in router and test so both agree and neither tracks the map — change a value in the test and assert the router follows |
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
| `side_effecting_read` | folding it into `False` — assert it is refused a transport and tagged separately |
| `mutates` default | testing only the nine explicit patterns — assert unmatched text returns `True` |
| multi-pattern conflict | testing single matches only — assert a conflicting pair returns `mutates=True` and logs |
| child re-classification | asserting the parent's type — assert a coding parent's comprehension child gets comprehension's ladder |
| kill switch off | asserting new behaviour only — assert routing is **byte-identical to today** with the switch off |
| rollback leaves no state | asserting the switch flips — assert no leaf, sub-agent or config survives the flip |
| startup validation | asserting valid config loads — assert an invalid model name **fails at load**, not at first use |
| free tier has no capacity gate | asserting behaviour when samples exist — assert routing is unchanged when `system_latest_by_host()` returns nothing |
| usage recording | asserting successes — a failed and a timed-out attempt must each produce a row |
| budget ceiling | asserting per node — spend accumulates across the tree; assert the tree total |
| termination guard | asserting intent — assert node count when children score ≥ parent |
| admission control | mocking `resource_guard` to always return ok — feed a fake `/proc/meminfo`; `read_meminfo()` already takes a path |

Every test is mutation-checked: break the ladder order, the guard, the termination rule, and confirm a specific test fails for each.

---

## 12. Open items

- `claude_proxy.py` drift on the pentester transport (`bb117e85…` vs HEAD `691fe393…`) — a deployment decision to settle before any wholesale transport sync. **Do not sync transports until it is decided.**
- Circuit-breaker rejection-rate threshold: **60%** of the last 20 leaves passing through a gate triggers the breaker. Spot-check sampling rate: **2%** of leaves that cleared all gates. Values are provisional; re-benchmark quarterly.
- Model-speed multiplier values: direction agreed (slow models get longer deadlines); values TBD from a benchmark run against production request shapes.
