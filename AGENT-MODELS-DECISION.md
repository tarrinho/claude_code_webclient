# Agent & Model Decision Design

> **Superseded 2026-09-13** by
> `docs/superpowers/specs/2026-09-12-tiered-agent-delegation-design.md`, a
> more complete, measurement-grounded design for the same problem (real
> benchmark data, real cost/host figures, already reviewed and approved by
> Pedro). This document's one genuinely additive idea not already covered
> there — routing eligibility must depend on whether a task mutates state, not
> just on capability/cost/host load — has been folded into that spec's §3.2.
> Kept here for history; do not implement against this file.

Design for the orchestrator's automatic choice of (a) which model runs a
subtask and (b) whether that subtask runs locally or on a remote transport.
Produced via `/brainstorming`, 2026-09-12. Architectural path: this document
is the spec; implementation follows via the `writing-plans` skill, not this
file directly.

## 1. Current state (why this is needed)

The scaffolding for this already exists in `orchestrator.py` but is dead or
disconnected:

- `COMPLEXITY_PATTERNS` + `PlanParser._score_complexity()` already score every
  subtask 1-5 by regex on its title/description text. This part works and is
  reused as-is.
- `ModelRouter` exists and is instantiated (`self.router = ModelRouter()`),
  but `.assign_model()` is **never called** anywhere in the real dispatch
  path — dead code.
- `ModelRouter.assign_model`'s own complexity-based fallback is a no-op bug:

  ```python
  if complexity >= 4:
      return config.ANTHROPIC_MODEL
  return config.ANTHROPIC_MODEL
  ```

  Both branches return the same value; complexity currently influences
  nothing.
- Machine/transport selection happens completely separately
  (`_routing.get("machine") or await db.ai_machine_backend(owner_id)`) —
  no relationship to complexity, model choice, or any "can this run
  elsewhere" policy at all. Every subtask goes to the owner's single
  default/active machine regardless of shape.

This design wires real decision logic through that scaffolding, rather than
building a parallel system.

## 2. Rules driving the design

- **External-eligible = read-only/research subtasks.** Anything that writes
  or mutates state stays local. This is the safety-load-bearing rule: a
  transport is less trusted than the local host, so only reads travel there.
- **Model tier follows complexity (1-5, existing score).** Simpler tasks get
  a less potent (cheaper/faster) model; more complex tasks get a more
  capable one.
- Five additional signals, all approved for this design (no phased
  deferral — both correctness gates and adaptive refinements ship together):

  1. **Gate on the target machine's declared `active_models`.** A model is
     never assigned independent of a backend that can actually serve it —
     `ai_machines.active_models` is already a tested, declared subset of
     what a backend advertises (see project `CLAUDE.md` §0.1). Never assign
     a model absent from the chosen machine's declared list.
  2. **Feed recent failure/cost history back into the decision.**
     Usage/billing tables already record cost, tokens, and success per turn,
     tagged by `origin` (`CLAUDE.md` §5). If a model has been failing or
     getting retried a lot recently on a similar task shape, demote/bump the
     pick accordingly instead of trusting a static one-shot score.
  3. **Gate "external-eligible" on live transport health, not just task
     content.** Content-eligibility and reachability are different
     questions; both must pass. Reuses `tunnel_manager.tunnel_status()`
     (the same mechanism behind this session's transport Broken-badge fix).
  4. **Add an estimated input/output size axis alongside complexity.**
     Complexity captures reasoning difficulty, not payload size. A
     large-input task can veto a model with too small a context window even
     at low complexity (see the documented `vllm/Qwen3-0.6B` /
     `max_tokens=32000` context-window incident in `CLAUDE.md`).
  5. **A one-step escalation ladder on failure.** If a subtask fails, times
     out, or returns a garbled result (the "0.6B echoing a markdown
     template" failure mode `CLAUDE.md` documents) on its assigned tier,
     retry once at the next tier up before surfacing failure for real.

## 3. Architecture & components

All new/changed pieces live in `orchestrator.py`, plus one new gate:

- **`ModelRouter.assign_model`** (exists, currently dead/buggy) — fixed and
  wired into the real dispatch path (`orchestrator.py`, the `runner.run_turn`
  call sites currently near lines 839 and 1048). Inputs: complexity (1-5),
  size estimate, failure history.
- **`FailureHistory`** (new) — a thin read layer over the existing
  usage/billing tables, keyed by (model, task-shape). Answers: "has this
  model been failing/retried on tasks like this recently?" No new table.
- **`SizeEstimate`** (new) — a cheap heuristic (prompt length + any
  file/data size hint already present in the task description), computed
  alongside complexity, not instead of it.
- **Read/write classifier** (new, small) — extends the existing
  `COMPLEXITY_PATTERNS` regex table (patterns like
  `read.*file|list.*directory|grep.*pattern` already imply read-only) into
  an explicit boolean flag on the task, rather than leaving it implicit in
  the complexity score.
- **`TransportEligibilityGate`** (new) — decides local-vs-transport. Three
  checks, all must pass:
  1. task is read-only/research (per the classifier above)
  2. the candidate transport's `active_models` declares the model
     `ModelRouter` picked
  3. `tunnel_manager.tunnel_status()` reports that transport healthy right
     now
- **`EscalationLadder`** (new, thin) — wraps a subtask's dispatch: on
  failure/timeout/garbled result, retries once at the next tier up (re-
  running the eligibility gate for the new model), then surfaces failure for
  real if that also fails.

## 4. Data flow

1. `PlanParser` splits the prompt into subtasks (existing, unchanged).
2. Per subtask: existing complexity score (1-5), **plus** the new read/write
   classification, **plus** the new size estimate.
3. `ModelRouter.assign_model(complexity, size_estimate, failure_history)`
   picks a model tier. Complexity picks the reasoning tier; size can veto a
   too-small context window; failure history can bump the pick up one tier
   if that model has been struggling on this task shape lately.
4. `TransportEligibilityGate` runs its three checks against candidate
   transports. All three pass → external. Any fails → local (see §5 for the
   local-fallback model re-pick).
5. Dispatch via the existing `runner.run_turn`/`stream_turn`, with model and
   machine now resolved by the above instead of the current bare
   owner-default lookup.
6. `EscalationLadder` wraps the call: failure/timeout/garbled result →
   retry once at the next tier up, re-running step 4's gate for the new
   model → surface failure for real if that also fails.
7. Result is recorded to the usage/billing tables as it is today (`CLAUDE.md`
   §5) — that recording is what `FailureHistory` reads for the *next*
   subtask's decision.

## 5. Error handling

- **No transport passes all three eligibility checks** (even for a
  read-only task) → falls back to local. **The model is re-picked for the
  local backend at the same complexity tier — the transport-picked model id
  is never carried over.** This is load-bearing: per `CLAUDE.md` §0.1, "a
  model id is never a bare string — it travels with its backend." Carrying
  a gateway-only model id (e.g. `vllm/Qwen3.6-35B`) into a local-Anthropic
  fallback would reproduce the "429 no deployments available" failure this
  codebase has already been burned by once.
- **No machine anywhere declares the picked model** → fall back to the safe
  default (`config.ANTHROPIC_MODEL`) rather than dispatching an undeclared
  guess.
- **Read/write classification is ambiguous** (no regex match either way) →
  defaults to **write** (stays local). Misrouting a write task externally is
  the dangerous direction; misrouting a read task local is only a missed
  optimization.
- **`FailureHistory` has no data yet** for a (model, task-shape) pair (cold
  start) → no bump; trust the complexity score alone.
- **`SizeEstimate` can't be computed** (no length hint available) → falls
  back to complexity-only, does not block the decision.
- **Escalation retry also fails** → subtask marked `failed` (existing
  `TaskGraph` terminal state), propagates `blocked` to dependents — existing
  behavior, unchanged.
- **Already at the top model tier and it fails** → no further escalation;
  one attempt, then fail. No retry loop.
- **Transport flips healthy→broken between the gate check and the actual
  dispatch** (race) — not a new failure mode; it is exactly what
  `runner`/`get_backend` already surfaces today as a connection error. The
  escalation retry's re-run of the gate naturally routes the retry to local.

## 6. Testing plan

Conventions: same as the rest of this suite — `.venv/bin/python -m pytest`
invoked bare (never `pytest tests/`), no writes to the production database,
throwaway `WC_DB_PATH` for anything DB-backed.

**Unit tests, per component:**

- `ModelRouter.assign_model`:
  - complexity tiers actually map to different models (regression test for
    today's no-op bug — both branches currently return the same value)
  - a large size estimate vetoes a too-small-context model
  - failure history bumps a struggling model up one tier
  - cold start (no failure history) leaves the pick unchanged
- Read/write classifier:
  - representative read-only phrasings classify correctly
  - representative write phrasings classify correctly
  - an unmatched/ambiguous phrasing defaults to **write** (safe-side
    regression test)
- `SizeEstimate`:
  - a normal-sized prompt scores low
  - a large size hint vetoes a small-context model
  - missing size data falls back to complexity-only without blocking
- `FailureHistory`:
  - reads real usage-table rows correctly
  - a model with recent failures for a task-shape is flagged
  - no rows yet (cold start) returns "no opinion," not a crash or a default
    bump
- `TransportEligibilityGate`:
  - each of the three checks fails independently and correctly blocks
    eligibility (content type, `active_models` membership, live health)
  - the AND across all three
- `EscalationLadder`:
  - one retry at the next tier on failure/timeout/garbled result
  - already-at-top-tier fails immediately, no retry attempted
  - a successful first attempt never escalates

**Integration tests, full pipeline (subtask in → (model, machine) out):**

- read-only + transport healthy + model declared there → dispatches
  external, correct model
- write task → always local, regardless of complexity/size/health
- read-only + transport unhealthy → falls back local **and re-picks the
  model for the local backend** (dedicated regression test for the §5 fix)
- read-only + transport healthy but declares no suitable model → same
  re-pick fallback path
- a subtask that fails on its assigned tier → escalates once, re-running
  the eligibility gate for the new model, then succeeds or fails cleanly

## 7. Open items for the implementation plan

- Exact model-tier table (which model ids count as "less potent" vs "more
  advanced") is not fixed here — it should be read from each machine's
  declared `active_models`, not hardcoded, per §5's coupling rule.
- `FailureHistory`'s exact "recent" window and failure threshold are left to
  the implementation plan to size against real usage-table volume.
