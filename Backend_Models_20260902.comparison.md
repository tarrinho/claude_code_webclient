# Backend and Model Comparison — 2026-09-02

## Executive result

This comparison now uses the benchmark that WebConsole actually uses: the **CLI transport**. Raw HTTP is not an active WebConsole configuration and is included only in the diagnostic appendix because it exposed a separate gateway problem.

The benchmark ran 14 tasks, 2 repeats per task, with mechanical execution for code and claim checks for prose. Results below come from 264 measured CLI runs:

- 4 Anthropic models × 14 tasks × 2 repeats = 112 runs
- 5 Azure models × 14 tasks × 2 repeats = 140 runs
- Qwen × 6 floor/simple tasks × 2 repeats = 12 runs

Qwen was intentionally re-tested on simpler tasks. The clean or empty output seen in the old benchmark was a harness failure, not evidence that Qwen was unusable. Qwen is self-hosted and free; Anthropic models have the measured token costs below; Azure prices were not recorded.

## Recommendation by scarce resource

| Constraint | First choice | Why |
|---|---|---|
| **Lowest cost** | `vllm/Qwen3.6-35B-A3B-NVFP4` | 11/12 on the floor/simple CLI tier, 5.2 s median, $0 marginal token cost |
| **Best measured quality** | `claude-fable-5` | 28/28 CLI runs correct; most expensive Anthropic option |
| **Best Anthropic value** | `claude-haiku-4-5` | 27/28, $0.0433 per correct answer, fastest Anthropic TTFT |
| **Fast capable Azure option** | `azure_ai/gpt-5.4-mini-copilot` | 24/28, 5.8 s median; Azure price not recorded |
| **Fastest measured Azure option** | `azure_ai/gpt-5.4-mini` | 25/28, 6.3 s median; Azure price not recorded |
| **Higher-quality Azure fallback** | `azure_ai/gpt-5.6-sol` | 25/28, but 10.5 s median and Azure price not recorded |

No single quality-only ranking should hide cost. Qwen is the correct default when latency is not scarce. Anthropic is the expensive quality tier. Azure cannot be ranked on cost until its deployment rate card is recorded.

## CLI measurements

All rows below are CLI runs. `Correct` means all core checks for the run passed. Edge checks remain visible in task scores but do not decide whether the task is solved. `$ total` is the CLI-reported cost only when the result carries `costBasis: list`; unknown Azure pricing is reported as `n/r`, never as zero.

| Tier | Model | Runs | Correct | Median total | Median TTFT | Median output tokens | $ total | $/correct |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Anthropic | `claude-fable-5` | 28 | **28/28** | 9.1 s | 3.31 s | 142 | $7.3130 | **$0.2612** |
| Anthropic | `claude-opus-5` | 28 | **27/28** | 8.3 s | 2.55 s | 122 | $4.8428 | **$0.1794** |
| Anthropic | `claude-sonnet-5` | 28 | **27/28** | 6.0 s | 1.97 s | 132 | $2.4518 | **$0.0908** |
| Anthropic | `claude-haiku-4-5` | 28 | **27/28** | 10.6 s | **1.75 s** | 611 | $1.1682 | **$0.0433** |
| Azure | `azure_ai/gpt-5.6-sol` | 28 | **25/28** | 10.5 s | 4.80 s | 131 | n/r | n/r |
| Azure | `azure_ai/gpt-5.4-mini` | 28 | **25/28** | **6.3 s** | 1.53 s | 163 | n/r | n/r |
| Azure | `azure_ai/gpt-5-mini` | 28 | **25/28** | 35.1 s | 13.21 s | n/r | n/r | n/r |
| Azure | `azure_ai/gpt-5.4-mini-copilot` | 28 | **24/28** | **5.8 s** | 1.42 s | 161 | n/r | n/r |
| Azure | `azure_ai/gpt-5.6-luna` | 28 | **24/28** | 6.2 s | 3.52 s | 116 | n/r | n/r |
| Self-hosted | `vllm/Qwen3.6-35B-A3B-NVFP4` | 12 | **11/12** | **5.2 s** | **0.74 s** | 56 | **$0.0000** | **$0.0000** |

### What the table means

- Fable is the only clean sweep across the full 14-task Anthropic tier. It costs approximately 6× Haiku per correct answer.
- Opus and Sonnet each miss one of 28 runs. Opus costs about 2× Sonnet per correct answer.
- Haiku matches Opus and Sonnet at 27/28 while having the fastest Anthropic TTFT and the lowest Anthropic cost per correct answer. Its total time is higher because some reasoning tasks produce substantially more output.
- Azure models are all unpriced in the recorded data. Their cost must not be inferred from the old console totals, which applied Anthropic fallback rates to non-Anthropic backends.
- Qwen's measured CLI result is fast and free on the simpler tier. It is not directly comparable to the Anthropic/Azure 14-task count because only the floor/simple tier was re-run for Qwen.

## CLI task comparison

Task cells are median scores across the two CLI repeats. `100` means all core and edge checks passed; a lower score shows a visible verifier failure. For prose tasks, verification is claim-based and therefore weaker than code execution.

| Task | Difficulty | Verified by | Fable | Opus | Sonnet | Haiku | gpt-5.6-sol | gpt-5.4-mini | gpt-5-mini | copilot | luna | Qwen |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `floor-add` | floor | execution | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 |
| `simple-fizzbuzz` | simple | execution | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 |
| `simple-count-vowels` | simple | execution | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 |
| `simple-reverse-words` | simple | execution | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 |
| `simple-sum-evens` | simple | execution | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 |
| `simple-json-field` | simple | execution | 75 | 75 | 75 | 75 | 75 | 100 | 100 | 75 | 75 | 75/0 |
| `coding-bug-fix` | hard | execution | 100 | 100 | 94 | 100 | 100 | 100 | 100 | 100 | 100 | — |
| `coding-algo` | hard | execution | 100 | 100 | 96 | 98 | 100 | 100 | 100 | 100 | 100 | — |
| `reasoning-puzzle` | hard | claim | 100 | 100 | 75 | 100 | 50 | 100/50 | 50 | 50 | 50 | — |
| `reasoning-math` | hard | claim | 100 | 100 | 100 | 75/100 | 100 | 100 | 100 | 100 | 100 | — |
| `comprehension-read` | hard | claim | 100 | 100/75 | 100 | 100 | 75/100 | 75 | 75/100 | 75 | 75 | — |
| `planning-task` | hard | claim | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | — |
| `long-context-needle` | hard | claim | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | — |
| `multi-turn-resume` | hard | execution | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | 100 | — |

Qwen's two CLI repeats on `simple-json-field` scored `75` and `0`: one run passed the core checks but failed the optional edge check, while the other omitted the `json` import and failed execution. This is the one measured Qwen miss, not a clean/empty response.

### Task-level observations

- All models handled the five simplest executable tasks consistently.
- `simple-json-field` exposes a common idiom failure: direct indexing of an optional field instead of using `.get()`. This is an edge-quality signal, not a reason to reject a model that passes the core task.
- The malformed `reasoning-puzzle` target asks for 4 litres in a 3-litre jug. Models that identify the impossibility receive only the claim-check partial score. This task is useful for constraint awareness but should not be read as evidence that the models failed to reason.
- The hard coding tasks separate the Anthropic tier slightly from the Azure tier, but the sample is small and consists of only two repeats.

## Consistency

"Unstable" means the same prompt produced different scores in its two repeats.

| Model / transport | Unstable tasks |
|---|---:|
| `claude-fable-5` / CLI | 0 of 14 |
| `claude-opus-5` / CLI | 1 of 14 |
| `claude-sonnet-5` / CLI | 3 of 14 |
| `claude-haiku-4-5` / CLI | 2 of 14 |
| `azure_ai/gpt-5.6-sol` / CLI | 1 of 14 |
| `azure_ai/gpt-5.4-mini` / CLI | 1 of 14 |
| `azure_ai/gpt-5-mini` / CLI | 1 of 14 |
| `azure_ai/gpt-5.4-mini-copilot` / CLI | 1 of 14 |
| `azure_ai/gpt-5.6-luna` / CLI | 0 of 14 |
| `vllm/Qwen3.6-35B-A3B-NVFP4` / CLI | 1 of 6 |

Two repeats show that instability exists; they do not estimate long-run failure rates. Sonnet's three unstable tasks include a 50-versus-100 swing on `reasoning-puzzle`, so one-shot results should not be treated as deterministic.

## Cost

### Recorded rate card

| Model/backend | Input / 1M tokens | Output / 1M tokens | Cache read | Cache write | Basis |
|---|---:|---:|---:|---:|---|
| `claude-fable-5` | $10.00 | $50.00 | $1.00 | $20.00 | published |
| `claude-opus-5` | $5.00 | $25.00 | $0.50 | $10.00 | published and independently confirmed |
| `claude-sonnet-5` | $2.00 | $10.00 | $0.20 | $4.00 | published |
| `claude-haiku-4-5` | $1.00 | $5.00 | $0.10 | $2.00 | published |
| `vllm/Qwen3.6-35B-A3B-NVFP4` | $0 | $0 | — | — | self-hosted, marginal token cost treated as zero |
| `azure_ai/*` | not recorded | not recorded | not recorded | not recorded | no deployment rate supplied |

Anthropic totals use the CLI's `total_cost_usd` only when `costBasis` is `list`. The harness also captures uncached input, cache-read, cache-write, and output tokens. Unknown vendor pricing is not silently converted to zero.

### Why old non-Anthropic console costs are not usable

The old usage table charged non-Anthropic backends with an Anthropic fallback rate. That made self-hosted Qwen appear to cost money and produced fictional Azure costs. Those figures are excluded from this comparison. Qwen is free under the stated self-hosted deployment assumption. Azure remains `n/r` until its actual rate card is supplied.

## Qwen: corrected interpretation

The first benchmark ranked Qwen at 55.4% and described empty output as a model failure. That result is withdrawn.

The old harness used a 4,096-token cap for both reasoning and answer, a 180-second timeout, and an extractor that collapsed truncated thinking into the literal `[thinking]` marker. Qwen's reasoning output consumed the budget and time limit. A truncated answer and an empty answer became indistinguishable.

The corrected benchmark used 16,384 maximum output tokens, a 1,000-second timeout, private per-run directories, file-delivery recovery, and explicit core versus edge checks. The simpler CLI tier produced:

- 11/12 core tasks correct
- 5.2 s median total time
- 0.74 s median TTFT
- 56 median output tokens
- $0 marginal token cost
- no clean/empty runs in the corrected CLI result

The one miss was `simple-json-field`, where one answer omitted `import json`. That is a real answer defect. It is not the earlier harness artefact.

## Historical HTTP diagnostic: not active configuration

Raw HTTP was removed from the benchmark because WebConsole production uses the CLI path. The following measurements remain only to explain why old conclusions were wrong.

### Qwen head-to-head, same six floor/simple tasks

| Metric | HTTP | CLI |
|---|---:|---:|
| Correct | 11/12 | 11/12 |
| Median total time | 88.2 s | **5.2 s** |
| Median output tokens | 2,134 | **56** |
| Median thinking characters | 6,540 | **102** |
| Median TTFT | 76.71 s | **0.74 s** |

The model's correctness was the same. The HTTP path generated far more reasoning and was much slower. Those HTTP figures must not be used to choose the current WebConsole backend because that transport is no longer part of the application benchmark.

### Azure HTTP gateway failure

The historical Azure HTTP stream returned a LiteLLM gateway error (`list index out of range`) for most streaming attempts. The stream error carried no normal `type` field, so the old parser recorded silence. The corrected parser surfaces the error and can re-run unstreamed, but the raw HTTP transport was then removed from the production comparison.

This is a gateway/transport finding, not a model-quality finding. It should be fixed in the gateway separately if HTTP streaming is needed. It does not change the CLI measurements above.

## Benchmark method and limits

- Harness: `bin/wc-bench.py`
- Report generator: `bin/wc-bench-report.py`
- Verifiers: `bench/verify.py`
- Tasks: `bench/tasks.py`
- Cost logic: `bench/cost.py`
- Maximum output tokens: 16,384
- Timeout: 1,000 seconds
- Repeats: 2 for the completed comparison
- Active transport: CLI only
- Code correctness: extracted code executed in a subprocess with a restricted environment
- Prose correctness: mechanical claim checks, weaker than execution
- Core checks decide `correct`; edge checks remain in the score and report
- Result files: `bench_anthropic_20260902.json`, `bench_azure_20260902.json`, `bench_qwen_transport_20260902.json`

Limitations remain:

1. Azure and Anthropic are not identical vendor deployments, so raw scores do not establish general model rankings outside this environment.
2. Qwen was re-run on the floor/simple tier after the original harness failure; its 11/12 is not a 14-task score.
3. Two repeats are enough to expose some instability, not enough to establish production failure probabilities.
4. Claim checks can miss errors that execution would catch.
5. Azure rates are not recorded, so no Azure cost-per-correct result is claimed.
6. The benchmark measures model responses in isolated CLI runs, not complete end-to-end WebConsole user journeys.

## Files and provenance

| File | Contents |
|---|---|
| `bench_anthropic_20260902.json` | 112 Anthropic CLI runs |
| `bench_azure_20260902.json` | Completed Azure run: 280 historical CLI/HTTP rows, including diagnostic HTTP rows |
| `bench_qwen_transport_20260902.json` | 12 Qwen CLI rows and 12 historical HTTP rows |
| `bench_rates.json` | Explicit rate card used by the harness |
| `bin/wc-bench-report.py` | Reproducible report generator |

The final recommendation is therefore conditional, not a single league-table number:

- Choose **Qwen** for free background and batch work where the corrected floor/simple task bar is sufficient.
- Choose **Haiku** for the best measured Anthropic cost/quality balance.
- Choose **Fable** when the extra clean-sweep quality is worth its much higher cost.
- Choose **Azure** based on latency and task fit only until actual Azure pricing is recorded.
