# Statistics: billing route, a comparable token basis, and per-agent spend

**Date:** 2026-09-10
**Status:** approved design, not yet implemented
**Surface:** Settings → Statistics (`#panelStats`), fed by `GET /api/usage/series`

## The problem, measured

Three complaints, one of which turned out to be a data problem rather than a
display one. Every figure below comes from a read-only query against the live
usage table on 2026-09-10.

### The first two charts group by a column that holds five vocabularies

`usage_events.provider` is written by four different code paths, and they do not
agree on what the column means:

| provider | origin | rows | tokens | written by |
|---|---|---|---|---|
| `cli` | terminal | 115,672 | 6.48B | `usage_import` (transcript importer), hardcoded literal |
| `cli` | web-routed | 15,642 | 786M | same |
| `anthropic` | web | 71 | 4.27M | a raw machine-provider value |
| `through_claude_code` | web | 69 | 3.60M | `shared.backend_kind` display kind |
| `anthropic-compatible` | web | 62 | 2.81M | a raw machine-provider value |
| `ssh-proxy` | web | 9 | 95K | `shared.backend_kind` display kind |
| `through_claude_code` | voice | 32 | 6.49K | display kind |

`stats.js`'s `SOURCES` table names four display kinds and falls back to the raw
string for anything else, so 99.9% of the data renders in a series labelled
`cli` and the chart's "source" axis answers no question anyone asks.

### The third chart cannot be reconciled with LiteLLM, for three reasons

1. **86.6% of the charted total is re-counted context.** 6.30B of 7.27B tokens
   sit on rows flagged `context_unsplit` — models that report no cache
   breakdown, so every turn counts the whole conversation again. The Usage tab
   subtracts these (`_buildOriginBreakdown`'s "comparable"); the charts do not.
2. **Cache tokens are charted nowhere.** 25.78B cache-read and 438M
   cache-creation tokens are in the table and in no series. A gateway meters
   them.
3. **One model is charted as two.** `nvidia/Qwen3.6-35B-A3B-NVFP4` (7.15B) and
   `vllm/Qwen3.6-35B-A3B-NVFP4` (10.6M across four provider values) are the
   same weights under two ids; likewise `Qwen/Qwen3.5-0.8B` and
   `vllm/Qwen3.5-0.8`. Only the top 6 of 19 distinct ids are drawn; the rest
   merge into "Other".

### There is no per-agent chart

131,314 of 131,557 rows carry a `session_id`; only 15,885 carry a `chat_id`. So
the data to answer "which agent spent this" has always been there, keyed on the
session rather than the conversation.

## What cannot be recovered, and what that forces

The billing route is **not recorded anywhere** for existing rows. Checked
directly: 120 transcripts, every assistant record, across seven models. A
gateway turn and a subscription turn are shape-identical — `service_tier` is
`standard` on all of them, `quotaLimits` is absent on all of them, and
`cache_read_input_tokens` is present on all of them. There is no field that
separates them.

What *is* available:

* **Going forward:** the write sites know. `runner` and `claude_proxy` resolve a
  backend through `backend_env.deltas` before spawning, so the base URL — and
  therefore the route — is known at the moment the row is written.
* **For history:** the model id. On this deployment, gateway models carry a
  prefix (`vllm/`, `nvidia/`, `azure_ai/`, `Qwen/`) or an OpenAI-family name
  (`gpt-5.6-luna`, `gpt-5.4-mini`), and subscription models are bare
  `claude-*`.

Applying those rules to the whole table classifies **every row**:

| route | rows | tokens |
|---|---|---|
| subscription | 53,178 | 58,341,025 |
| gateway | 79,310 | 7,298,985,027 |
| unclassified | 0 | 0 |

(All history; the table starts 2026-08-26.) The design still carries an
`unclassified` bucket, and that is deliberate — it is currently empty, and the
moment a new model id arrives that matches no rule, it must appear as its own
series rather than being folded into whichever side looks plausible. An empty
safety net is the desired state, not evidence it can be removed.

## Design

### 1. `billing_route`: recorded where it is known, inferred where it is not

Add one column:

```sql
ALTER TABLE usage_events ADD COLUMN billing_route TEXT NOT NULL DEFAULT ''
```

Empty string means "not recorded", which is exactly what every existing row is.
No backfill: a stored value must mean "the write site knew this", and writing
inferences into the same column would destroy the distinction between a fact and
a guess within one migration.

**Write sites** (all three already resolve a backend, so none needs new
plumbing): `routes/chats.py::_record_turn_usage`, `orchestrator.py::_record_usage`,
and `usage_import` for transcript rows. The first two pass the value they
already compute via `shared.backend_kind`; `usage_import` has no backend and
keeps writing `''`.

**The classifier** lives in one function in `routes/db_usage.py`:

```python
def billing_route_of(stored: str, model: str) -> tuple[str, bool]:
    """Return (route, inferred). route is subscription | gateway | unclassified."""
```

It prefers the stored value and falls back to model-id rules. The rule table is
a module-level constant with the prefixes above, and every rule carries a
comment naming the deployment fact behind it — these are true of *this*
gateway, not of gateways in general.

**Why not classify entirely at read time:** because then nothing ever becomes
ground truth, every new backend needs a rule edit before it charts correctly,
and a chart that is 100% inference cannot say so honestly. Recording it forward
means the inferred share shrinks on its own.

### 2. A token basis that can be compared with a gateway

Three measures, computed the same way everywhere they appear:

| Measure | Definition |
|---|---|
| Billable input | `SUM(input_tokens + cache_creation_tokens)` **excluding rows where `context_unsplit = 1`** |
| Cache read | `SUM(cache_read_tokens)` |
| Output | `SUM(output_tokens)` |

Cache creation belongs with billable input because that is how it is billed —
writing the cache costs full rate, reading it does not. Re-counted context is
excluded from all three and reported as its own footnote figure under the
chart: not hidden, not summed into anything.

On current data this changes the headline from 7.27B to 0.90B billable input,
25.78B cache read and 75M output, with 6.30B named as re-counted context.

### 3. The four charts

**Chart 1 — Tokens by billing route.** One line per route, plotting billable
input + output. The three-measure split goes in the table beneath it, one row
per route per measure. Six table rows is readable; six chart lines is not.

**Chart 2 — Turns by billing route.** The same series, counting turns. Same kind
of chart as today, correctly grouped for the first time.

**Chart 3 — Tokens by model.** Same basis. Gateway prefixes normalised so one
model is one line, with every raw id it merged listed in the tooltip. Top-N
raised from 6 to 12 (19 distinct ids exist in 30 days).

**Chart 4 — Tokens per agent, top 8.** New. Grouped by `session_id`, named from
the session file or the linked chat title, falling back to the first 8
characters of the id. The remainder merges into "Other". This is the chart that
explains a surprising total: the top five sessions are 0.71B–1.22B tokens each.

Series colours keep the existing rule — the validated categorical palette via
`slotColor`, legend plus direct labels plus a table under every chart, because
the light-surface aqua sits below 3:1 and obligates the relief rule.

### 4. Where each piece lives

| File | Change |
|---|---|
| `db.py` | The `billing_route` migration. |
| `routes/db_usage.py` | `billing_route_of` and its rule table; `usage_series` grouped by route with the three measures; `usage_model_series` with alias normalisation and top-12; new `usage_agent_series`. |
| `routes/misc.py` | `/api/usage/series` carries `agents` and the route-classified series; the unsplit footnote figure. |
| `routes/chats.py`, `orchestrator.py` | Pass the resolved route to `usage_record`. |
| `web/assets/stats.js` | Four charts, the new labels, the tooltip carrying merged ids, the footnote. |

### 5. Testing

Unit, against the vocabulary that actually exists:

* Every provider value in the live table (`cli`, `anthropic`,
  `anthropic-compatible`, `through_claude_code`, `ssh-proxy`) classifies without
  raising, and a stored route always beats an inferred one.
* A model id matching no rule returns `unclassified`, and the pair
  `(route, inferred)` reports `inferred=True` for it — the flag is what the
  legend renders, so it has to be asserted, not assumed.
* Billable input excludes `context_unsplit` rows and nothing else; a fixture
  with one unsplit and one normal row proves the exclusion is per row rather
  than per model.
* Alias normalisation merges `vllm/X` and `nvidia/X` into one series and keeps
  both raw ids reachable for the tooltip.
* `usage_agent_series` names a session from its session file, falls back to the
  chat title, then to a short id, and merges beyond the top 8.

Browser, in `test_frontend_browser.py`:

* Four figures render with the expected captions.
* The route chart's legend shows both routes and marks the inferred one.
* The per-agent chart draws a line per session and no line for a session with
  no rows.

Each new assertion is mutation-checked before it is trusted — the recurring
failure in this repo is a test that passes for a reason unrelated to what it
claims.

## Deliberately not in scope

**Reconciling against LiteLLM's own spend API.** It would answer "why don't
these match" definitively, but it needs gateway credentials inside a page load,
covers only gateway traffic, and the three causes of the mismatch are already
measured above. Revisit only if the figures still disagree after this lands.

**Backfilling `billing_route`.** See §1: the column means "recorded", and
filling it with inferences would erase the only thing it is for.

## Open questions

None. The one that was open — whether `unclassified` should be its own series
even when large — is settled by measurement: it is currently empty, and it
stays in the design as a safety net for model ids that do not exist yet.
