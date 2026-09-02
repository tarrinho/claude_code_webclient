# Backend & Model Comparison — 2026-09-02

**Delegation Score**: How confident am I that this model will get the job done right without me reworking it? 0-100%.

**Benchmark**: 6 tasks × 6 models = 36 queries against the WebConsole gateway (`llm.ai-machine.cfappsecurity.com`).

> ## Correction, 2026-09-02 17:05 — read this before the tables
>
> **Qwen3.6's original scores measured the benchmark harness, not the model.**
> It was ranked last at 55.4% with "Code Quality 0" and "produces zero output
> text", and that verdict is withdrawn. Three limits in the harness, each of
> which disqualified the model before it could answer:
>
> 1. **`max_tokens = 4096` covered thinking and answer together.** Qwen3.6 is
>    the only reasoning model in the set. Re-measured, it needs 6097, 6411 and
>    6360 output tokens on the three tasks that "failed" — so the answer was
>    being cut off mid-thought. No other model came near the cap: the highest
>    was luna at 2725, and the two cheap ones use under 800. The cap was
>    calibrated on non-reasoning models and only ever bound on this one.
> 2. **The extractor recorded truncation as silence.** It substituted the
>    literal string `[thinking]` for a thinking block, so a truncated run and an
>    empty run became indistinguishable in the data. Visible in the original
>    JSON: the three Qwen tasks that *did* finish have responses beginning
>    `'[thinking]\n\n\n$'` — placeholder, then the real answer.
> 3. **`TIMEOUT = 180s` was never a considered value.** The original run
>    measured Qwen at 123–177s per task, so every one of its successes landed
>    within 3s of the limit. Anything slower than the five Azure models failed
>    on arrival.
>
> Re-run at `max_tokens = 16384` and a 600s timeout, **5 of 6 tasks complete
> with `stop_reason: end_turn`, `had_text_block: true`, and no `[thinking]`
> prefix anywhere.** Clean answers are this model's normal output; the prefix
> was the harness flattening two channels into one string.
>
> Evidence: `bench_qwen_rerun_20260902.json`, committed alongside this file.
>
> **The speed column below is not safe to read as a model property.** See
> "Transport, not model" — the same task the HTTP path cannot finish in 600s is
> answered correctly through the CLI path in 23.0s.

## Delegation Scores (overall)

Ranked on quality only, which is what these six dimensions measure. Cost is not
one of them — see "Cost" below, which changes the ordering for most real work.

| Rank | Model | Delegation Score | Speed | Marginal cost | Quality Tier |
|---|---|---|---|---|---|
| 1 | **azure_ai/gpt-5.6-luna** | **73.7%** | 50s/task | paid | Best overall balance |
| 2 | **azure_ai/gpt-5.4-mini-copilot** | **72.1%** | 6s/task | paid | Fastest capable |
| 3 | **azure_ai/gpt-5.6-sol** | **69.5%** | 11s/task | paid | Solid code quality |
| 4 | **azure_ai/gpt-5-mini** | **67.3%** | 14s/task | paid | Good for planning |
| 5 | **azure_ai/gpt-5.4-mini** | **65.8%** | 3s/task | paid | Fastest, weakest code |
| — | **vllm/Qwen3.6-35B-A3B-NVFP4** | **withdrawn** | see caveat | **free** | Correct on 5/6; self-hosted |

Qwen3.6 is deliberately left unranked rather than given a new number. Five of
its six tasks are re-measured and clean; the sixth cannot be measured on this
path at all. A single figure would hide that, which is how the 55.4% happened.

---

## Per-Model Breakdown

### 1. azure_ai/gpt-5.6-luna — Delegation Score: 73.7%

| Dimension | Score | Avg Rating | Notes |
|---|---|---|---|
| Correctness | 100 | 5.0 | All answers correct, math right, puzzle right |
| Code Quality | 75 | 3.75 | Type hints, generic types, clean structure |
| Completeness | 75 | 3.75 | Covers all requirements, minor detail gaps |
| Reasoning | 75 | 3.75 | Sound multi-step, clear constraints noted |
| Constraint Handling | 75 | 3.75 | Respects all stated constraints |
| Clarity | 50 | 2.5 | Good structure but verbose in places |
| **Avg Delegation Score** | **73.7%** | | **Best balance** |

**Task detail:**
- `coding-bug-fix`: ✅ Correct fix with TypeVar, early break on n reached
- `reasoning-puzzle`: ✅ 7 pours, correct steps
- `coding-algo`: ✅ OrderedDict, O(1), clean __repr__
- `reasoning-math`: ✅ Correct formula and answer (0.5073), boxed
- `comprehension-read`: ✅ All 4 questions correct, precise edge case analysis
- `planning-task`: ✅ System/light/dark design with CHECK constraint, `data-theme` attribute, early init script

**Verdict:** Best overall. Correct, well-typed code. Slowest at ~10s/task but worth it for quality. Delegate coding and reasoning freely.

---

### 2. azure_ai/gpt-5.4-mini-copilot — Delegation Score: 72.1%

| Dimension | Score | Avg Rating | Notes |
|---|---|---|---|
| Correctness | 100 | 5.0 | All answers correct |
| Code Quality | 50 | 2.5 | Works but minimal typing, basic structure |
| Completeness | 75 | 3.75 | Covers requirements, less detailed |
| Reasoning | 75 | 3.75 | Correct reasoning, brief |
| Constraint Handling | 75 | 3.75 | Respects constraints |
| Clarity | 50 | 2.5 | Structured but terse explanations |
| **Avg Delegation Score** | **72.1%** | | **Fastest capable** |

**Task detail:**
- `coding-bug-fix`: ✅ Correct fix, basic type hints (List[T])
- `reasoning-puzzle`: ✅ 7 pours, correct
- `coding-algo`: ✅ OrderedDict, O(1), no type annotations on class methods
- `reasoning-math`: ✅ Correct, full expansion
- `comprehension-read`: ✅ All correct, brief
- `planning-task`: ✅ Solid plan, good structure

**Verdict:** Fastest capable model (~6s/task). Gets everything right but produces leaner code. Best for batch/async tasks where speed matters and you'll review.

---

### 3. azure_ai/gpt-5.6-sol — Delegation Score: 69.5%

| Dimension | Score | Avg Rating | Notes |
|---|---|---|---|
| Correctness | 75 | 3.75 | Math correct, but coding issues |
| Code Quality | 25 | 1.25 | LRU cache missing docstring, minimal type hints |
| Completeness | 75 | 3.75 | Covers most, thin on some detail |
| Reasoning | 75 | 3.75 | Correct reasoning steps |
| Constraint Handling | 75 | 3.75 | Respects stated limits |
| Clarity | 50 | 2.5 | Structured but terse |
| **Avg Delegation Score** | **69.5%** | | **Good, but code needs review** |

**Task detail:**
- `coding-bug-fix`: ✅ Correct fix with TypeVar
- `reasoning-puzzle`: ✅ 7 pours, correct steps
- `coding-algo`: ⚠️ Full linked-list implementation but missing `__init__` type hint, no docstring, capacity=0 edge case silently does nothing (not clearly documented)
- `reasoning-math`: ✅ Correct (0.5073)
- `comprehension-read`: ✅ All correct
- `planning-task`: ✅ Good, with trigger for updated_at

**Verdict:** Good code structure, but produces minimal annotations. The linked-list LRU is more complex than needed (OrderedDict would be simpler). Delegate coding with review, best for reasoning tasks.

---

### 4. azure_ai/gpt-5-mini — Delegation Score: 67.3%

| Dimension | Score | Avg Rating | Notes |
|---|---|---|---|
| Correctness | 75 | 3.75 | Mostly correct, some imprecision |
| Code Quality | 50 | 2.5 | Works but inconsistent typing |
| Completeness | 75 | 3.75 | Covers requirements |
| Reasoning | 50 | 2.5 | Some gaps in edge case analysis |
| Constraint Handling | 75 | 3.75 | Respects constraints |
| Clarity | 50 | 2.5 | Structured, some verbosity |
| **Avg Delegation Score** | **67.3%** | | **Use for planning, not coding** |

**Task detail:**
- `coding-bug-fix`: ✅ Correct fix with set optimization
- `reasoning-puzzle`: ✅ 7 pours, correct
- `coding-algo`: ⚠️ Full linked-list LRU but `capacity=0` silently ignores puts (should raise or document), `__repr__` shows MRU first (not clearly specified), missing `__init__` docstring
- `reasoning-math`: ✅ Correct (0.5073), shows full precision intermediate
- `comprehension-read`: ✅ All correct, brief
- `planning-task`: ✅ Excellent — most detailed plan, covers FOUC prevention, server-side injection

**Verdict:** Best planner but weakest at edge-case awareness in coding. The LRU cache silently fails on capacity=0 and has inconsistent docs. Best for planning tasks, use code with review.

---

### 5. azure_ai/gpt-5.4-mini — Delegation Score: 65.8%

| Dimension | Score | Avg Rating | Notes |
|---|---|---|---|
| Correctness | 75 | 3.75 | Answers right but thin |
| Code Quality | 25 | 1.25 | No docstrings, no type hints, minimal structure |
| Completeness | 50 | 2.5 | Covers basics, thin on detail |
| Reasoning | 50 | 2.5 | Correct but brief, edge cases missed |
| Constraint Handling | 75 | 3.75 | Respects stated constraints |
| Clarity | 50 | 2.5 | Structured but minimal |
| **Avg Delegation Score** | **65.8%** | | **Fastest, lowest quality** |

**Task detail:**
- `coding-bug-fix`: ✅ Correct fix but no docstring, bare `List[Any]` type hints
- `reasoning-puzzle`: ✅ 7 pours, correct
- `coding-algo`: ⚠️ OrderedDict LRU works but no docstrings, no type hints on methods, bare `__repr__` return type but nothing else typed
- `reasoning-math`: ✅ Correct (0.5073)
- `comprehension-read`: ⚠️ Misses nuance — says "cannot happen" but doesn't discuss the pre-initialization edge case as clearly as luna
- `planning-task`: ✅ Good plan, similar to gpt-5.4-mini-copilot but less detailed on FOUC prevention

**Verdict:** Fastest model (~3s/task) but produces production-unready code. No docstrings anywhere, bare types. OK for quick tasks where speed trumps quality, not recommended for anything you're deploying.

---

### 6. vllm/Qwen3.6-35B-A3B-NVFP4 — re-measured 2026-09-02 17:05

The scores in this section previously read `Correctness 50 / Code Quality 0 /
Clarity 25`, totalling 55.4%, with the verdict "unusable for production, not
recommended for any task". **Every one of those numbers came from a truncated
capture.** They are replaced, not adjusted — there is no partial credit to
salvage from a measurement of the harness.

Re-run: `max_tokens = 16384`, 600s timeout, current extractor.

| Task | Result | Time | Output tokens | `stop_reason` |
|---|---|---|---|---|
| `coding-bug-fix` | ✅ correct, typed, full docstring | 290.8s | 6097 | `end_turn` |
| `coding-algo` | ✅ correct O(1) LRU, hashmap + doubly-linked list | 287.6s | 6411 | `end_turn` |
| `reasoning-math` | ✅ correct formula and answer (0.5073) | 283.5s | 6360 | `end_turn` |
| `comprehension-read` | ✅ all 4 answers correct | 147.2s | 3121 | `end_turn` |
| `planning-task` | ✅ SQL, endpoints, frontend components | 143.1s | 3437 | `end_turn` |
| `reasoning-puzzle` | ⚠️ unmeasurable on this path — see below | >600s | — | timed out |

All five completed tasks report `had_text_block: true` and **no `[thinking]`
prefix**. Clean output is this model's ordinary behaviour.

**Correctness verified rather than assumed**, on the two where correctness is
checkable by reading:

* `last_n_unique` returns `seen[::-1][-n:]`. Traced on `[1,2,3,2,1], n=2` → the
  unique elements ordered by last occurrence, last two → `[2,1]`. Correct, and
  it fixes the dropped-last-element bug the prompt describes.
* The LRU is a genuine O(1) hashmap-plus-doubly-linked-list with sentinel head
  and tail, type hints on the public methods, and a `__repr__` walking MRU to
  LRU.

**The one real code-quality gap:** `capacity=0` breaks. `put` compares
`len(self.cache) == self.capacity`, so on an empty zero-capacity cache it calls
`_pop_tail()` on an empty list, gets the sentinel head back, and raises
`KeyError` deleting `cache[0]`. Worth noting that `gpt-5.6-sol` and
`gpt-5-mini` have the same defect, scored there as a ⚠️ rather than a zero.
Missing docstrings on the class and its methods.

**Verdict:** correct on every task it can complete, with clean, well-structured
output. Its costs are wall-clock time and one unmeasurable task on this
transport — not correctness, and not code that has to be rewritten. Delegate
freely for anything not blocking a person; see "Cost" for why that is often the
whole job.

#### Transport, not model

`reasoning-puzzle` is the finding worth following up, and it is not about
Qwen3.6:

| Path | Result |
|---|---|
| `wc-claude.sh` (CLI) | **23.0s**, correct 7-pour solution |
| HTTP gateway, `max_tokens=16384` | **>600s, timed out** |

Same model, same task, same prompt, ≥26x apart. A model cannot be 26x slower
because the caller used a different socket, so the difference is in the path —
most likely that the HTTP route is unstreamed and the gateway buffers the whole
reasoning output before returning a byte, or that the two paths send different
thinking configuration. Unresolved, and larger than this comparison.

**Consequence for every number in this document:** all per-task times were
measured on the HTTP path, so the Speed column mixes model latency with
transport latency and cannot separate them. It should not be read as a property
of a model until that gap is closed.

---

## Speed Comparison (avg time per task)

**Measured on the HTTP path only, which for Qwen3.6 is a transport figure as
much as a model figure** — the same task it cannot finish in 600s here is
answered in 23.0s through the CLI. Treat the Azure rows as model latency and
the Qwen row as an upper bound.

| Model | Avg Time | Range | Interactive? |
|---|---|---|---|
| gpt-5.4-mini | 3s | 1-5s | ✅ Excellent |
| gpt-5.4-mini-copilot | 6s | 2-5s | ✅ Good |
| gpt-5.6-sol | 11s | 3-24s | ✅ Acceptable |
| gpt-5-mini | 14s | 9-20s | ⚠️ Borderline |
| gpt-5.6-luna | 50s | 4-29s | ❌ Batch only |
| Qwen3.6-35B (re-run, 5 tasks) | 230s | 143-291s | ❌ Batch only, and free |

The re-run figures are higher than the original 149s because the original run
was truncating at 4096 tokens — it was timing incomplete answers. 230s is the
cost of letting it finish.

---

## Task-Level Scoring (Delegation Score per dimension)

Qwen3.6's column is re-scored from the 2026-09-02 17:05 re-run. Its previous
entries — `0 (empty)` on Bug fix, LRU cache and Water jug — were the truncated
captures described at the top of this document, and they are replaced.

### Coding Tasks

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Bug fix | 100 | 100 | 75 | 75 | 75 | **100** |
| LRU cache | 100 | 50 | 25 | 50 | 25 | **50** |
| **Avg** | **100** | **75** | **50** | **62.5** | **50** | **75** |

Qwen3.6's bug fix is correct with type hints and a full Args/Returns docstring,
which is the same standard luna was given 100 for. Its LRU is correct and
genuinely O(1) but raises `KeyError` at `capacity=0` and carries no docstrings —
50 is the score `gpt-5.4-mini-copilot` received for an equivalent gap, and the
`capacity=0` defect is the one `gpt-5.6-sol` and `gpt-5-mini` also have.

### Reasoning Tasks

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Water jug | 100 | 100 | 100 | 100 | 100 | **n/m** |
| Birthday paradox | 100 | 100 | 100 | 100 | 100 | **100** |
| **Avg** | **100** | **100** | **100** | **100** | **100** | **100** (of 1 task) |

`n/m` = not measurable on this path. Water jug exceeds 600s over HTTP and is
answered correctly in 23.0s through the CLI transport, so a 0 here would record
a transport limit as a model failure — which is exactly what this document did
the first time. It is left unscored rather than scored generously: the CLI
answer was correct, but it was not produced by the same harness as every other
cell in these tables, and mixing the two would make the column unreadable.

Birthday paradox is raised from 75 to 100. The original 75 was for a response
beginning with a `[thinking]` placeholder; the re-run returns the correct
formula and 0.5073 with no placeholder, which is what the other five models
were scored 100 for.

### Comprehension

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Function explain | 100 | 100 | 100 | 75 | 75 | **100** |
| **Avg** | **100** | **100** | **100** | **75** | **75** | **100** |

Raised from 75. `gpt-5.4-mini` was given 75 for saying an edge case "cannot
happen" without discussing the pre-initialisation case as clearly as luna. The
re-run answers that directly — *"it would only trigger if the code were
modified to pre-initialize categories, remove items from lists, or process data
differently"* — so it meets the standard the 100s were given for. All four
answers correct, no placeholder.

### Planning

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Dark mode toggle | 75 | 75 | 75 | 75 | 50 | 75 |
| **Avg** | **75** | **75** | **75** | **75** | **50** | **75** |

---

## Cost

**The Delegation Score has no cost dimension.** Its eight weights are
Correctness 25, Completeness 15, Reasoning 15, Code Quality 10, Constraint
Handling 10, Consistency 9, Clarity 8, Speed 8. Nothing for what a query costs
to run — which means the league table above prices every backend at zero, and
ranks the only free one last.

Three tiers, cheapest first:

| Backend | Marginal cost per query | What you pay instead |
|---|---|---|
| `vllm/Qwen3.6-35B-A3B-NVFP4` | **none** — self-hosted | wall-clock, and GPU occupancy on our own hardware |
| `azure_ai/*` | per-token, moderate | little; 3–14s/task |
| `anthropic/*` (opus-5, sonnet-5, fable-5, haiku-4-5) | per-token, **highest** | little; 4–7s/task measured 2026-09-02 |

Per-token rates for this deployment are not recorded here; the ordering is, and
the ordering is what changes the decision.

**No single ranking survives this.** Which backend is correct depends on which
resource is scarce:

| Constraint | Choose | Why |
|---|---|---|
| **Cost** — batch, background, scheduled, anything not blocking a person | `vllm/Qwen3.6` | free, and correct on all five tasks it completes; slowness is nearly irrelevant when nobody is waiting |
| **Latency** — anything a person is watching | `gpt-5.4-mini-copilot` or `gpt-5.6-luna` | 6s and 50s/task against Qwen's 143–291s. Two orders of magnitude, and unsoftened |
| **Quality** — code going to production, hard reasoning | `anthropic/*`, then `gpt-5.6-luna` | best output, and explicitly the most expensive per token |

A weighted cost dimension is deliberately **not** added to the existing formula.
Folding cost into one number is what produced a 55.4% that nobody could take
apart; the trade-off belongs on the surface where it can be argued with.

## Recommended Task Assignments

Quality-ranked, cost ignored. Read with the table above.

| Task Type | Recommended Model | Backup | Notes |
|---|---|---|---|
| **Coding** (bug fix, algo) | gpt-5.6-luna | Qwen3.6 (free) or gpt-5.4-mini-copilot | luna for quality; Qwen correct on both coding tasks at zero cost if latency allows |
| **Reasoning** (puzzle, math) | Any Azure model | Qwen3.6 for math | All Azure models score 100%. Qwen's math is correct; its puzzle is unmeasurable on the HTTP path but correct via CLI |
| **Comprehension** | gpt-5.6-luna | Qwen3.6 (free) | luna most thorough; Qwen all 4 correct |
| **Planning** | gpt-5-mini | Qwen3.6 (free) | mini most detailed; Qwen produced SQL, endpoints and components |
| **Fast batch** | gpt-5.4-mini-copilot | gpt-5.4-mini | copilot better, mini fastest |
| **Cheap bulk** | **Qwen3.6** | — | the only zero-cost option, and correct where measured |

**Previously here:** "Avoid for production: Qwen3.6-35B (50% of tasks produce no
output)." Withdrawn — that was a 4096-token cap and a 180s timeout, not the
model.

---

## Technical Notes

- **Gateway:** `https://llm.ai-machine.cfappsecurity.com` (vLLM OpenAI-compatible endpoint)
- **Authentication:** ANTHROPIC_API_KEY header (`<redacted>`)
- **Models served:** 6 models total (1 vLLM, 5 Azure AI)
- **Anthropic models (opus-5, sonnet-5, etc.)** are NOT accessible through this gateway — only the 6 listed above
- **benchmark script:** `bin/model_benchmark.py`
- **raw results:** `model_benchmark_results.json` (untracked — the original
  six-model run, whose Qwen rows have since been partly overwritten by a CLI
  re-run. The script writes this file into the *current directory* and
  overwrites it whole, so run it from a scratch directory or lose the baseline.)
- **Qwen re-run evidence:** `bench_qwen_rerun_20260902.json` (tracked, so the
  corrected numbers above have something behind them)
- **harness limits, now configurable:** `MAX_TOKENS = 16384`,
  `TIMEOUT = int(os.environ.get("WC_BENCH_TIMEOUT_S", "1000"))`. Both defaults
  were raised because both disqualified a working model; the timeout moved to
  the environment because it is a property of the harness, not of any backend.

## Scoring Methodology

**Known limitations of this methodology**, added after it produced a wrong
verdict:

1. **No cost dimension.** See "Cost". The formula prices every backend at zero.
2. **Speed conflates model and transport.** All timings come from one HTTP path.
3. **One run per task.** Consistency is weighted at 9% but never measured across
   repeats, so it is an impression rather than a number.
4. **A harness limit is indistinguishable from a model failure in the output.**
   This is the one that caused real damage. Guarding it now: `stop_reason` and
   `had_text_block` are recorded per task, and a run that hits `max_tokens`
   keeps its thinking content instead of being replaced by a placeholder. Any
   future score of 0 should be checked against those two fields first.

8 weighted dimensions → Delegation Score (0-100%):
- Correctness (25%): Right answer, no hallucinations
- Completeness (15%): Does the full task
- Reasoning (15%): Multi-step chain holds together
- Code Quality (10%): Readable, idiomatic, maintainable code
- Constraint Handling (10%): Respects stated and implicit limits
- Clarity (8%): Well-organized, scannable output
- Speed (8%): Time to first token + total
- Consistency (9%): Quality holds across tasks and runs

Each dimension scored 1-5 per task, converted to 0-100 with weighted average.