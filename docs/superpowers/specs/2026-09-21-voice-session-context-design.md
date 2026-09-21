# Voice session context — design

**Status:** design, not implemented
**Date:** 2026-09-21

A voice session should open knowing what the conversation it came from is
about, be able to look up any detail it is unsure of, and be interruptible by
voice while it talks.

---

## 0. What is already there

This is not a greenfield feature, and three of the twelve requirements are
partly built. Stating what exists first, because the cost of missing it is
building a second mechanism beside a working one.

**Parent context already exists, as a keyword heuristic.** `routes/voice.py`
builds a structured block from the parent chat's last 12 messages by substring
matching: a user line containing `goal`, `trying`, `need to` becomes `GOAL:`, a
line containing `decided`, `go with` becomes `DECISIONS:`, quoted strings
become `NAMES / CONSTRAINTS:`. It is injected as a user-role preamble before
the prompt and captured in `sent_messages` for the replay record.

So requirement 1 — "a voice session currently starts with no knowledge of the
chat it was opened from" — is not accurate. It starts with a crude and often
wrong impression of it. The work is *replacing* that extractor, and retiring it
is part of the change rather than a follow-up.

**Barge-in already exists, for one word.** `web/assets/voice-engine.js` runs a
second `SpeechRecognition` instance (`bargeInRecognition`, continuous, interim
results) while `voiceStatus === 'speaking'`, matching `/\bstop\b/i` and calling
`performVoiceStop(false)`. Requirement 10's "whole words only" and "no
confidence threshold" are therefore already satisfied for `stop`.

**A startup message already exists.** `voice-tooltip.js` writes "Using
pre-existing conversation context in voice chat…" while it creates the temp
chat. Requirement 8's status line supersedes it.

**What does not exist at all:** tool calling on the voice path
(`chat.completions.create` is called with no `tools`), any interrupt handling
in `routes/voice.py` (zero occurrences of interrupt or barge), an audio
thinking cue, and any model-generated summary.

## 1. Measured constraints

These numbers decided three parts of the design and are recorded so a future
reader can tell when they have gone stale.

**Chat size is extremely skewed.** Across 66 chats with messages: median **22**
messages, 75th percentile **2,048**, 90th **5,571**, maximum **15,175**. The
largest chat is 3,936,511 characters ≈ **984,000 tokens**, which exceeds the
922,000-token window of `azure_ai/gpt-5.6-luna`, the ladder's first rung. The
longest single message is **256,560 characters**. Summarising "the chat" is
therefore not a bounded operation, and a message-count window alone does not
bound it either.

**The comprehension ladder is measured, not pending.** Requirement 3 says model
choice "depends on the pending comprehension benchmarks"; they are done, n=12
for every model:

| rung | accuracy | median latency (CLI transport) | cost/1M |
|---|---|---|---|
| `azure_ai/gpt-5.6-luna` | 0.583 | 9.2s | 0.037 |
| `claude-sonnet-5` | 1.0 | 6.0s | 0.4769 |
| `claude-opus-5` | 1.0 | 11.9s | 1.231 |

Two consequences. The first rung is wrong more often than right, so escalation
is the **normal** path rather than an exception. And luna + sonnet is 15.2s,
which already breaches requirement 5's 15-second budget before opus is
reached — so the ladder as written cannot complete inside the budget it is
given.

Those latencies were measured through the CLI transport (`bin/wc-bench.py`
reports transport `cli`), so they already include subprocess spawn. This is
what makes §3's transport choice free.

### 1.1 The budget and the ladder do not fit, and the gap is 0.2 seconds

Worked through at the medians above, the common path is:

```
luna attempt ends at 9.2s, and fails (it is right 58.3% of the time)
remaining budget: 15.0 - 9.2 = 5.8s
sonnet needs 6.0s  ->  cancelled 0.2s short
```

So the single most likely outcome of the requirements exactly as written —
15-second budget, ladder starting at luna, degraded open on exhaustion — is
**no summary at all, on most sessions**. Not as an edge case: as the default.
The feature would ship and mostly not do the thing it exists for, and because
§2's degraded open is a graceful failure, nothing would appear broken.

The margin is 0.2s at the median, so this is not a safe bet either way: a
faster-than-median luna failure leaves enough room, a slower one does not. The
outcome would vary run to run for no reason the user can see.

**Two fixes, either of which resolves it. This needs an operator decision
before implementation.**

- **Start the walk at `claude-sonnet-5`.** It is 100% accurate at 6.0s, leaving
  9.0s inside the budget for a retry or for opus. Costs 0.4769/1M against
  luna's 0.037 — about 13× — on one call per voice session. Skipping a rung
  measured at 58.3% for a task whose failure is silent is defensible on its own
  terms, not only on arithmetic.
- **Raise the budget to ~25s.** Keeps the cost-first ladder intact and lets
  luna+sonnet complete with margin. Costs up to 25 seconds before a voice
  session opens, which for a feature whose point is talking is a long time to
  stare at a status line.

Recorded here rather than resolved, because it trades cost against startup
latency and that is the operator's call. §11 assumes the first option; if the
second is chosen, §3's budget changes and nothing else does.

## 2. Amendments to the stated requirements

Two requirements are changed by operator decision on 2026-09-21, recorded here
rather than silently implemented differently.

**Requirements 4 and 5: the session opens degraded instead of refusing.** As
written, "the session never opens without a summary" combined with the measured
numbers above means voice sessions would frequently refuse to open — for a
feature whose failure mode is being unable to talk. Instead: if the ladder is
exhausted or the 15s budget is spent, the session **opens with no summary** and
the status line says so. The fetch tool (§4) is what makes this survivable: the
model can still retrieve anything it needs, it simply starts without an
overview.

**Requirement 2: the summary reads a bounded recent window, not the whole
chat.** Forced by §1's sizes. Anything older is reachable through the fetch
tool, which is consistent with requirement 7's instruction to fetch rather than
guess.

## 3. Summarisation

**Transport: the Claude Code CLI, via `run_turn`.** CLAUDE.md §0 requires every
model call to go through the CLI and names `routes/voice.py` as the single
deliberate exception, argued on latency grounds for *spoken turns*.
Summarisation is not a spoken turn — it happens once, at startup, before anyone
is listening — so it has no claim on that exception, and §1 shows the CLI costs
nothing extra because the ladder's own latencies were measured through it.

This also keeps the call inside normal usage accounting. CLAUDE.md §5 requires
each caller to record its own usage; a direct `AsyncOpenAI` call from
`voice.py` would spend tokens that appear nowhere. Usage is recorded with
`origin="voice-summary"` so it can be told apart from spoken turns.

**Input window.** The parent chat's most recent messages, subject to both
bounds, oldest dropped first:

- at most **100 messages**, and
- at most **40,000 characters** total (≈10,000 tokens).

The message cap covers the median chat (22) entirely. The character cap is not
redundant with it: one message can be 256,560 characters, so a 100-message
window is unbounded without it. When truncation occurs the summary prompt says
so, so the model knows it is seeing a tail rather than a whole.

**Ladder walk.** Walk the comprehension ladder from `GET /api/delegation`'s
`ladders["comprehension"]`, in order. A rung fails when the call errors, times
out, or returns an empty summary — where **empty** means fewer than 20
non-whitespace characters, so that a model answering "N/A" or "" counts as a
failure and escalates rather than being stored as the session's context. On
failure, escalate to the next rung. The
walk stops at the first success, at ladder exhaustion, or when the total budget
is spent — whichever comes first.

**Budget.** 15 seconds total across all attempts, measured from the first
attempt's start. Before starting a rung, if elapsed time already exceeds the
budget, the rung is not attempted. A rung in flight when the budget expires is
cancelled. Per §1 this means a realistic walk reaches two rungs, not three;
that is a property of the numbers, not a defect, and §2's degraded open is what
absorbs it.

**Failure reporting.** Each attempt emits a status event (§5) naming the state
and the rung: `summarising (luna)`, `failed (luna)`, `escalating (sonnet)`.

## 4. The fetch tool

**Placement: a new `voice_context.py`.** Not inside `routes/voice.py`, which is
543 lines and is the one file CLAUDE.md singles out for care. The module owns
summarisation and the fetch tool and hands `voice.py` two finished things: a
context string and a bound tool. `voice.py`'s own responsibilities do not
change.

**Binding.** The tool is constructed with the parent `chat_id` captured in the
closure. Its schema takes **only** `from_id` and `to_id` — there is no chat
parameter to supply, so it cannot address another conversation. This is a
structural guarantee rather than a validated one: the model cannot express the
request that would read someone else's chat.

**Signature and behaviour.** `fetch_messages(from_id, to_id)` returns the
messages of the bound chat whose `messages.id` falls in that inclusive range,
verbatim, in id order, each with its id, role and content. `messages.id` is a
stable ordered integer key, which is what makes a range expressible. A range
that matches nothing returns an empty list rather than an error, because "there
is nothing there" is a true and useful answer.

**Result bounding.** A range can name 15,175 messages. The tool returns at most
**200 messages or 40,000 characters**, whichever comes first, and says
explicitly that it truncated and at which id — so the model can ask for the
next span rather than silently receiving a partial answer it believes is whole.

**Lifecycle.** Constructed when the voice session is created, discarded when it
ends. Nothing persists it; there is no registry to leak from.

**Wiring.** `chat.completions.create` is currently called with no `tools`
argument. Tool calling is new on this path: the tool schema must be passed, and
the streaming loop must handle a `tool_calls` delta, execute the call, append
the result, and continue the turn. This is the largest single piece of new
code in the feature.

**Instruction (requirement 7).** The system prompt tells the model to call
`fetch_messages` rather than guess whenever it is unsure of a specific detail —
a number, a name, a decision — and that the summary it was given is a tail of
the conversation, not all of it.

## 5. Startup status line

States, in order: `initialising` → `summarising current context` → `ready`,
with the failure states from §3 appearing between the second and third.

The line is rendered in the voice panel, stays in place, and is pushed up by
the conversation as it begins. It has no dismiss control. It replaces
`voice-tooltip.js`'s current "Using pre-existing conversation context in voice
chat…" message.

**Transport: SSE.** Summarisation takes up to 15 seconds and must report each
attempt, so it cannot be a field on the `POST /api/chats` response without
blocking chat creation for that long. A new endpoint,
`POST /api/voice/{chat_id}/context`, streams status events and finishes by
storing the summary on the temp chat. The frontend renders the line from those
events. The app already uses SSE for turns, so this is an existing pattern.

**Ordering.** `voice-tooltip.js` calls it immediately after `POST /api/chats`
returns the temp chat id, and does not await completion before showing the
voice panel — the panel appears at `initialising`, and the line advances as
events arrive. The first spoken turn is gated on the stream finishing, in
either outcome: with a summary, or degraded. The user can therefore see the
session opening while it summarises, which is the point of streaming it, but
cannot get a reply built on context that has not arrived yet.

`setVoiceStatus` in `voice-engine.js` currently models `idle`, `listening`,
`speaking`, `thinking`. The startup states are additions to it, not a second
status mechanism.

## 6. Thinking tone

While the model is thinking, a repeating tone at approximately **one pulse per
second**, via WebAudio (an oscillator gated by a periodic envelope — not an
audio asset, which would be a file to ship and fail to load). It stops when
speech begins.

This is **additional** to the existing visual `renderThinkingIndicator`, not a
replacement: requirement 9 makes the tone's *absence* the signal that something
is wrong, which only works if the tone is the thing being listened for.

Muting is the user's own system volume. There is no in-app control, because a
control to silence the fault signal defeats the reason for having it.

## 7. Interrupts

**Words.** `stop`, `wait`, `pause`. Whole words only — the existing
`/\bstop\b/i` shape is correct and extends to `/\b(stop|wait|pause)\b/i`.
Substrings must not match, so "stopping" and "waitress" do nothing. No
confidence threshold: occasional false triggers from transcription error are
accepted, per requirement 10.

**When.** Today the recogniser restarts only while `voiceStatus === 'speaking'`.
It must also run while **thinking**, and in that state a trigger cancels the
pending reply rather than halting speech that has not started. Both states
return to listening afterwards.

**Self-interrupt suppression.** The model saying "wait a moment" would
otherwise interrupt itself, and on laptop speakers browser echo cancellation
does not prevent it. This is systematic rather than occasional, so it is the
one false trigger that is filtered: the engine keeps the text it has spoken in
the last **3 seconds**, and a trigger whose word appears in that text is
discarded.

This narrows requirement 10 in exactly one way, deliberately: thresholdless
matching is preserved for the user's speech, and only the model's own echo is
suppressed. The cost is a real user saying "stop" within 3 seconds of the model
saying "stop" being missed; the user says it again, which is a far better
failure than the model cutting itself off mid-sentence for no visible reason.

## 8. Error handling

| failure | behaviour |
|---|---|
| a rung errors or times out | escalate, report in the status line |
| ladder exhausted | open degraded, status line says no summary |
| 15s budget spent | abort the walk, open degraded |
| `fetch_messages` names an unknown range | return an empty list |
| `fetch_messages` result too large | truncate, say so, name the last id returned |
| parent chat has no messages | skip summarisation, open with no summary |
| SSE connection drops mid-walk | session opens degraded; the walk is not retried |
| speech recognition unavailable | no barge-in; voice still works, which is today's behaviour |

## 9. Testing

**Pure functions, unit tested directly:** window selection (both bounds,
truncation flag), the trigger matcher (whole word, the three words, substring
rejection), echo suppression (inside and outside the 3s window), and fetch
range bounding (empty range, over-long range, truncation id).

**Integration:** the ladder walk against a stubbed runner — first rung fails and
the second succeeds; every rung fails; the budget expires mid-walk. Each asserts
the emitted status events, because the status line is the only thing the user
sees of this.

**Browser:** the status line's three states in order, the tone starting and
stopping with thinking, and barge-in on each of the three words.

Node is not installed on this host, so JS pure functions are exercised through
a browser test rather than a unit runner. Browser tests run under rules.md §14's
memory cap.

## 10. Out of scope

- Summarising the voice session back into the parent — `voice_handoff` already
  does that and is unchanged.
- Any change to how spoken turns reach the model. §3 moves *summarisation* onto
  the CLI; the spoken path keeps its documented exception.
- Re-summarising mid-session. The summary is built once, at open; the fetch tool
  covers what changes after that.
- A confidence threshold on triggers. Explicitly rejected by requirement 10.

## 11. Suggested decomposition

This is larger than one comfortable implementation plan, and the two halves
have no dependency on each other. They should be planned and landed
separately, in this order:

1. **Context** — §3 summarisation, §4 the fetch tool, §5 the status line.
   Delivers requirements 1–8 and is independently useful: a voice session that
   knows what it was opened from and can look things up.
2. **Audio** — §6 the thinking tone, §7 interrupts. Delivers requirements
   9–11. Touches only `voice-engine.js` and shares no code with part 1.

Requirement 12 is not a deliverable; it is the constraint that both parts land
on the `routes/voice.py` path rather than the CLI turn path.

Part 1 is the larger of the two, and within it the tool-calling loop (§4's
"Wiring") is the single riskiest piece, being the only genuinely new mechanism
on this path.

## 12. Open risk

The first ladder rung is measured at 58.3% comprehension accuracy. Under §2's
degraded open, the expected steady state is that a meaningful share of voice
sessions begin with **no summary** and rely entirely on the fetch tool. That may
be acceptable — the tool is precise where a summary is lossy — but it should be
measured after the feature ships rather than assumed. If it proves common, the
cheapest fix is starting the walk at `claude-sonnet-5`, which is 100% accurate
at 6.0s and leaves 9s of budget for a retry.
