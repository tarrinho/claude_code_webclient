# Tiered agent delegation: routing a goal across models by capability, cost and host load

**Date:** 2026-09-12
**Status:** design, approved in chat; not implemented

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
| `vllm/Qwen3.5-0.8B` | 26% / 13s | – | – | – | – | 0% / 226s |

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
4–8 responses. A 100% means "no observed failures", not proof of reliability,
and the difference between 88% and 93% aggregate is a handful of answers. The
25% comprehension cliff is the one gap wide enough to design around with
confidence.

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

The self-hosted model is at **109.1% CPU** — already oversubscribed, which is
consistent with its 161.4s p90. Its price is not zero; it is paid in GPU
occupancy shared with every other user of that gateway. luna is the proven
external workhorse at 1,996 requests; mini has served 166.

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

| task type | model | $/1k tasks | why this one |
|---|---|---|---|
| coding | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 4s — same accuracy as Opus at 75× less |
| long-context | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 3s |
| multi-turn | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 6s |
| planning | `azure_ai/gpt-5.6-luna` | 0.23 | 100% at 23s against Sonnet's 100% at 18s — 5s slower, 25× cheaper |
| comprehension | `claude-sonnet-5` | 5.68 | luna and mini both measure 25%; the one cliff |
| reasoning | `azure_ai/gpt-5.4-mini` → `claude-opus-5` | 3.54 expected | see below |
| split decision | `claude-sonnet-5` | 5.68 | judging scope is comprehension work, where cheap models fail |

**The tier column is an *entry rung*, not a fixed assignment.** A leaf starts at
the rung its task type names and escalates upward through the remaining rungs,
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

**Free tier.** `vllm/Qwen3.6-35B-A3B-NVFP4` is offered *ahead of* luna for
coding when, and only when, its gateway transport reports headroom. It is free
and 96% correct, but every one of those 23 responses was a coding task and its
p90 is 161s. A node placed there carries a 45s deadline — comfortably above its
8.5s median, far below its p90 — after which it is abandoned and retried on the
paid ladder.

### 2.1 "Headroom", defined

Used in three places above and below, so it gets one definition rather than a
judgement call at each site.

| gate | condition | source |
|---|---|---|
| free tier offered | `cpu_pct < 80` **and** `load1 / cores < 1.0` on the gateway's host | `system_latest_by_host()` |
| transport eligible for placement | `mem_pct <= 75` | `system_latest_by_host()` |
| local spawn permitted | `resource_guard.check().ok` | `/proc/meminfo`, live |
| any sample older than 120s | treated as **unknown**, and unknown is not eligible | `created_at` |

Samples arrive every 30s, so 120s is four missed intervals. Unknown is refused
rather than assumed healthy, for the reason `db_supervisor_map.py:356` already
gives about the hub glow: "nothing measured and nothing happening must not look
alike".

Against the numbers in §1.4 and §1.5 these gates currently resolve to: free tier
**closed** (109.1% CPU), all three transports **eligible** (18.4%, 31.5%, 51.5%),
local **refused** (`ok=False`). So a tree started today routes coding to luna and
places it on a transport.

**Escalation ladder:** `vllm → luna → mini → sonnet → opus`, one retry per rung,
**≤3 attempts per leaf**, then the node reports failed. Typical leaves cost one
attempt, since the entry tier is 100% on four of the six task types.

**Excluded outright:** `vllm/Qwen3.5-0.8B` (23% correct, 0% on reasoning),
`claude-haiku-4-5` (dearer and slower than Sonnet), `claude-fable-5` (100% but
$23.15/1k, 4.1× Sonnet).

---

## 3. Node lifecycle

Five gates. Only the third ever spends a model call on *deciding* anything.

1. **Score** — `orchestrator._score_complexity(text)`, free, returns 1–5.
2. **Split** — 1–2 execute now; 4–5 decompose now; exactly 3 costs one Sonnet
   call. A worker never judges its own scope: the models best at executing
   (luna, 100% on coding at 4s) are the worst at judging (25% on
   comprehension).
3. **Placement** — `resource_guard.check()` before every spawn. When local is
   full, select a `proxy`-provider machine whose `transport_id` host has
   headroom per `system_latest_by_host()`. `runner.py:370` states the mechanism:
   "A backend with transport_id set runs its claude process on that" transport.
   With no host available, the node queues rather than failing.
4. **Execute** — model and machine from the tier table, with the tier-0
   deadline where it applies.
5. **Escalate** — timeout or failure moves one rung. Usage is recorded per
   attempt with `origin="orchestrator"`, per CLAUDE.md §5, **including
   failures**: a turn that ran 45s and then timed out has been paid for, and
   recording only successes makes the cheapest-looking tier the one that fails
   most.

### 3.1 The missing classifier

The tier table is keyed by `task_type`, and **nothing in the codebase computes
one.** `_score_complexity` yields 1–5, not a type; the benchmark's types were
labelled by hand in the harness.

Extend `orchestrator.COMPLEXITY_PATTERNS` into a map emitting `(type, score)`
together. Its keys already read like types:

| existing pattern | score | type it implies |
|---|---|---|
| `architect\|design.*system\|create.*framework` | 4 | planning |
| `implement.*multiple\|coordinate.*agent\|orchestrate` | 5 | planning |
| `debug.*complex\|trace.*error.*chain\|performance.*bottleneck` | 4 | reasoning |
| `write.*test.*suite\|integration.*test\|e2e.*test` | 3 | coding |
| `analyze.*code.*review\|refactor.*large\|migrate.*database` | 4 | coding |
| `write.*doc.*umentation\|create.*tutorial\|explain.*concept` | 2 | comprehension |
| `research.*api.*document\|find.*replacement\|evaluate.*option` | 3 | comprehension |
| `read.*file\|list.*directory\|grep.*pattern\|summarize.*log` | 1 | long-context |

**Unmatched text defaults to `comprehension`**, deliberately. That is the type
where cheap models collapse to 25%, so an unclassifiable task routes to Sonnet.
Guessing wrong toward the capable model costs $5.68 per thousand; guessing wrong
toward the cheap one costs a wrong answer.

---

## 4. Data model and caps

`TaskNode` gains `depth`, `attempt`, `tier`, `machine_id`, `deadline_s`, and
begins actually using `parent_id` — already plumbed through `TaskGraph` and the
API payload at `orchestrator.py:417`, but `None` at the only construction site
(`orchestrator.py:754`), which is why the hierarchy is flat today.

```
MAX_DEPTH      = 3      # goal -> sub -> sub
MAX_CHILDREN   = 4      # per node
MAX_NODES      = 40     # whole tree
BUDGET_USD     = 1.00   # per goal, checked before each spawn
TIER0_DEADLINE = 45     # seconds, free tier only
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

Every test is mutation-checked before it is claimed to work: break the ladder
order, break the guard, break the termination rule, and confirm a specific test
fails for each.

---

## 6. Consequences accepted

**This design does not create capacity.** With local `capacity()` at
`existing 6, total 6`, a recursive tree runs today only by placing work on
transports. If none is available it degrades to a single serialised agent slot:
correct, bounded, and no faster than doing the work in one conversation. What it
buys is cost — the entry tier is 25× cheaper than Sonnet and free where the
self-hosted model has headroom.

**The free tier is contended.** At 109.1% CPU, the self-hosted gateway will
often fail its headroom check, and the policy will route to luna instead. That
is the intended behaviour, not a degradation, but it means "free" should not be
assumed in cost projections.

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
| free vs fast | free first, abandon at a 45s deadline and escalate | free only for background work; cheapest-that-works ignoring latency |
| Azure pricing | use the supplied billing lines, `Opt` read as output | leave Azure unpriced, as `bench_rates.json` had it |
