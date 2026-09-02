# Backend & Model Comparison — 2026-09-02

**Delegation Score**: How confident am I that this model will get the job done right without me reworking it? 0-100%.

**Benchmark**: 6 tasks × 6 models = 36 queries against the WebConsole gateway (`llm.ai-machine.cfappsecurity.com`).

## Delegation Scores (overall)

| Rank | Model | Delegation Score | Speed | Quality Tier |
|---|---|---|---|---|
| 1 | **azure_ai/gpt-5.6-luna** | **73.7%** | 50s/task | Best overall balance |
| 2 | **azure_ai/gpt-5.4-mini-copilot** | **72.1%** | 6s/task | Fastest capable |
| 3 | **azure_ai/gpt-5.6-sol** | **69.5%** | 11s/task | Solid code quality |
| 4 | **azure_ai/gpt-5-mini** | **67.3%** | 14s/task | Good for planning |
| 5 | **azure_ai/gpt-5.4-mini** | **65.8%** | 3s/task | Fastest, weakest code |
| 6 | **vllm/Qwen3.6-35B-A3B-NVFP4** | **55.4%** | 149s/task | Broken code responses |

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

### 6. vllm/Qwen3.6-35B-A3B-NVFP4 — Delegation Score: 55.4%

| Dimension | Score | Avg Rating | Notes |
|---|---|---|---|
| Correctness | 50 | 2.5 | Math correct, but coding responses empty |
| Code Quality | 0 | 0.0 | No code produced (responses empty, only thinking) |
| Completeness | 50 | 2.5 | Math and comprehension covered, coding empty |
| Reasoning | 50 | 2.5 | Math correct, but comprehension/edge cases imprecise |
| Constraint Handling | 50 | 2.5 | Some constraints handled, some not |
| Clarity | 25 | 1.25 | Mostly empty responses for coding, verbose for others |
| **Avg Delegation Score** | **55.4%** | | **Unreliable, avoid for coding** |

**Task detail:**
- `coding-bug-fix`: ❌ Empty — only `[thinking]` block returned, no code
- `reasoning-puzzle`: ❌ Empty — only `[thinking]` block, no steps
- `coding-algo`: ❌ Empty — only `[thinking]` block, no code
- `reasoning-math`: ✅ Correct formula and answer (0.5073)
- `comprehension-read`: ✅ All 4 questions answered correctly, well-structured
- `planning-task`: ✅ Good plan with SQL, API endpoints, frontend components

**Critical issue:** Qwen generates extensive thinking blocks but **produces zero output text** for 3 of 6 tasks (all coding tasks). For the other 3, the output appears after the thinking. This makes it unreliable — you never know if it's going to produce output or not. When it works, the thinking is thorough but the actual answer often doesn't arrive.

**Verdict:** Unusable for production. The thinking-heavy output pattern means ~50% of tasks produce no actionable output. When it does work, reasoning is adequate but code quality is absent. Not recommended for any task.

---

## Speed Comparison (avg time per task)

| Model | Avg Time | Range | Interactive? |
|---|---|---|---|
| gpt-5.4-mini | 3s | 1-5s | ✅ Excellent |
| gpt-5.4-mini-copilot | 6s | 2-5s | ✅ Good |
| gpt-5.6-sol | 11s | 3-24s | ✅ Acceptable |
| gpt-5-mini | 14s | 9-20s | ⚠️ Borderline |
| gpt-5.6-luna | 50s | 4-29s | ❌ Batch only |
| Qwen3.6-35B | 149s | 123-177s | ❌ Very slow |

---

## Task-Level Scoring (Delegation Score per dimension)

### Coding Tasks

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Bug fix | 100 | 100 | 75 | 75 | 75 | 0 (empty) |
| LRU cache | 100 | 50 | 25 | 50 | 25 | 0 (empty) |
| **Avg** | **100** | **75** | **50** | **62.5** | **50** | **0** |

### Reasoning Tasks

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Water jug | 100 | 100 | 100 | 100 | 100 | 0 (empty) |
| Birthday paradox | 100 | 100 | 100 | 100 | 100 | 75 |
| **Avg** | **100** | **100** | **100** | **100** | **100** | **37.5** |

### Comprehension

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Function explain | 100 | 100 | 100 | 75 | 75 | 75 |
| **Avg** | **100** | **100** | **100** | **75** | **75** | **75** |

### Planning

| Task | gpt-5.6-luna | gpt-5.4-mini-copilot | gpt-5.6-sol | gpt-5-mini | gpt-5.4-mini | Qwen3.6 |
|---|---|---|---|---|---|---|
| Dark mode toggle | 75 | 75 | 75 | 75 | 50 | 75 |
| **Avg** | **75** | **75** | **75** | **75** | **50** | **75** |

---

## Recommended Task Assignments

| Task Type | Recommended Model | Backup | Notes |
|---|---|---|---|
| **Coding** (bug fix, algo) | gpt-5.6-luna | gpt-5.4-mini-copilot | luna for quality, copilot for speed |
| **Reasoning** (puzzle, math) | Any Azure model | — | All Azure models score 100% |
| **Comprehension** | gpt-5.6-luna | gpt-5.4-mini-copilot | luna most thorough |
| **Planning** | gpt-5-mini | gpt-5.6-luna | mini most detailed plan, luna solid |
| **Fast batch** | gpt-5.4-mini-copilot | gpt-5.4-mini | copilot better, mini fastest |

**Avoid for production:** Qwen3.6-35B (50% of tasks produce no output). Use only for thinking/exploratory queries where you'll parse the thinking block.

---

## Technical Notes

- **Gateway:** `https://llm.ai-machine.cfappsecurity.com` (vLLM OpenAI-compatible endpoint)
- **Authentication:** ANTHROPIC_API_KEY header (`<redacted>`)
- **Models served:** 6 models total (1 vLLM, 5 Azure AI)
- **Anthropic models (opus-5, sonnet-5, etc.)** are NOT accessible through this gateway — only the 6 listed above
- **benchmark script:** `bin/model_benchmark.py`
- **raw results:** `model_benchmark_results.json`

## Scoring Methodology

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