# One default backend, many enabled ones — design

**Status:** approved in chat 2026-09-08 (option 1, refuse-while-depended-on,
with the dependents named). Not yet implemented.

**Goal:** separate "which backend is used when nothing else says otherwise"
from "which backends may be used at all", and make the difference visible in
Settings → Backends.

---

## Today

`ai_machines.active` carries one meaning: exactly one row per owner has
`active = 1`, and that is the backend every turn uses unless the conversation
pins another. `ai_machine_activate()` sets every row to 0 and then one to 1.

Seven places independently resolve "the machine to use":

| Resolver | Used by |
|---|---|
| `db_machines.ai_machine_active(owner)` | voice settings, chat creation, models route |
| `db_machines.ai_machine_backend(owner)` | the runner (carries the API key) |
| `runner.get_backend(chat_id, owner)` | every turn |
| `runner.get_proxy_target(chat_id, owner)` | proxy/transport routing |
| `bin/wc-backend-env.py machine_for()` | every terminal session |
| `routes/misc.handle_settings_get` | the voice backend picker |
| `db_machines.ai_machine_seed_anthropic` | the Backends panel |

There is no way to shelve a backend without deleting it. This deployment has
six, two behind transports, and **fifteen conversations pinned across four of
them**: CF AI Machine 8, CF AI Machine API 5, Anthropic Oauth via appsectools
1, CF AI Machine (via Pentester) 1.

## Scope

**In:** a per-backend enabled flag; refusal to disable a backend anything still
depends on, naming the dependents; the Settings visuals for default versus
enabled versus disabled.

**Out, decided explicitly:**

- **No health colour.** Whether a backend *would work right now* is a different
  question, already answered by the transport Check button
  (`POST /api/transports/{id}/check`). Folding liveness into the same flag
  would give "inactive" two meanings, which is how `active` became ambiguous
  in the first place.
- **No fallback-on-disable.** Considered and rejected; see below.
- **No renaming of the `active` column.** See "Naming".

## The rule

A backend is **disabled** when `enabled = 0`. A disabled backend:

- is not offered in the model picker, the voice backend picker, or
  `--wc-profile` resolution;
- is never chosen as the default;
- keeps its configuration, its `active_models`, and its API key.

Disabling is **refused** while anything depends on the backend:

1. it is the current default (`active = 1`), or
2. one or more conversations pin it (`chats.ai_machine_id`).

The refusal names the dependents. Not a count — a count sends the reader
hunting, and every other diagnostic added to this codebase this week
(transport readiness, the resource guard's memory breakdown) earns its keep by
naming the specific thing instead:

```
Cannot disable CF AI Machine — 8 conversations are pinned to it:
  cweb2 · cweb5 · kali3 tunnel test · voice test · … (4 more)
Repoint them, or make another backend the default first.
```

At most eight titles are listed, with a "(N more)" tail, so the message stays
readable when a popular backend has forty.

### Why not fall back to the default

Allowing the disable and letting pinned conversations follow the default is one
click instead of two, and it produces the worst-diagnosing failure this
codebase has. `CF AI Machine` serves `vllm/Qwen3.6-35B-A3B-NVFP4`;
`Anthropic Oauth` serves `claude-*`. CLAUDE.md §0.1 states the rule and the
symptom: a model id is only meaningful against the backend serving it, and sent
elsewhere the gateway answers **429 "No deployments available for selected
model"** — a routing failure wearing a capacity error's clothes.

Eight conversations would start failing that way minutes after an
innocuous-looking click, with nothing in the interface connecting the two. The
whole reason `active_models` enforcement and `bin/wc-backend-env.py
--check-model` exist is to catch that class before it reaches a user. A feature
that manufactures it would be working against them.

## Naming

The user-facing vocabulary is **Default** and **Active / Inactive**. The
database keeps `active` meaning *the default*, and gains `enabled` meaning
*may be used*.

Those two disagree, and the disagreement is deliberate. The alternative —
adding `is_default`, migrating `active`'s value into it, then redefining
`active` as "enabled" — reads better but flips the meaning of a column that
seven resolvers already read. Any resolver missed in that migration would see
`active = 1` for *every* backend and silently pick an arbitrary one: a
multi-backend routing bug with no error anywhere, which is precisely the
failure mode CLAUDE.md exists to prevent.

So the rename happens in the interface, where it costs a label, and not in the
schema, where it would cost a silent routing bug. The column comment and this
section are the record of why, since `active` meaning "default" is otherwise
surprising to a reader.

## Schema

One additive migration alongside the existing `ai_machines` ALTERs, which live
in `db._ensure_chat_columns` (db.py:1047-1086) — a surprising home for them,
but the one they already have; putting this ALTER anywhere else would split the
`ai_machines` migrations across two functions for no gain:

```sql
ALTER TABLE ai_machines ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1
```

`DEFAULT 1` is what makes this safe: every existing backend stays usable, and
a database that predates the column behaves exactly as it does today. No
backfill, no data migration, nothing to get wrong on a host that is mid-work.

## Behaviour

| Action | Result |
|---|---|
| Disable an enabled, non-default, unpinned backend | allowed |
| Disable the default | refused: "this is the default backend; make another the default first" |
| Disable a backend with pinned conversations | refused, naming up to 8 of them |
| Enable a disabled backend | always allowed |
| Make a disabled backend the default | refused: enable it first |
| `--wc-profile <disabled>` | refused, listing the enabled profiles |
| A conversation already pinned to a disabled backend | keeps working — the pin is honoured, because refusal above means this can only arise from a direct database edit |

The last row matters: the refusal rules mean a pinned conversation can never be
orphaned by using the interface. Honouring the pin anyway is the conservative
choice for the case where someone edits the database by hand, which happens on
this deployment.

## API

- `PATCH /api/machines/{id}` accepts `enabled` (bool), alongside the fields it
  already takes. Refusal returns **409** with
  `{"error": ..., "pinned_chats": [{"id", "title"}], "is_default": bool}` so
  the frontend can render the list rather than re-deriving it.
- `GET /api/machines` and `GET /api/machines/{id}` include `enabled`.
- Every picker that lists backends filters `enabled = 1`:
  `handle_settings_get`'s `voice_backend_options`, the model picker's source,
  and `bin/wc-backend-env.py`'s `machine_for()`.

`ai_machine_activate()` refuses a disabled machine rather than silently
enabling it, so "make default" cannot smuggle a backend back into service.

## Visuals

Three states, three treatments, in `web/assets/machines.js`'s machine card:

- **Default** — the existing `machine-active` card style plus a `Default`
  badge. One card can hold it. Today's `machine-state-live` label becomes
  `Default` rather than the current implicit "active means default".
- **Active** (enabled, not default) — normal card, a `Make default` button.
  This is the state most cards will be in, so it stays visually quiet.
- **Inactive** (`enabled = 0`) — card dimmed (reduced opacity, muted border),
  an `Inactive` badge, and its model section collapsed. `Make default` is
  absent; `Enable` replaces `Disable`.

The dim treatment rather than hiding: a disabled backend is still configuration
you own and will want to find again, and hiding it is how people end up
recreating a backend that already exists — this deployment accumulated seven
duplicate "Anthropic API" rows for a related reason.

The `Disable` button carries the obstacle before it is pressed: when the
backend is the default or has pins, the button is rendered `disabled` with a
`title` naming why. The 409 path still exists for the race where a pin is
created between render and click.

## Testing

Behaviour, not arrangement:

| Case | Expected |
|---|---|
| migration on a database without the column | every existing backend `enabled = 1` |
| migration run twice | idempotent, no error |
| disable a plain enabled backend | allowed |
| disable the default | 409, `is_default: true` |
| disable a backend with 3 pinned chats | 409, all 3 titles in `pinned_chats` |
| disable a backend with 40 pinned chats | 8 titles listed, count reports 40 |
| `ai_machine_activate` on a disabled backend | refused |
| voice backend options | disabled backends absent |
| `machine_for()` with a disabled active machine | falls through rather than returning it |
| `--wc-profile <disabled slug>` | non-zero exit, lists enabled profiles |
| frontend | a disabled card renders the Inactive badge and no `Make default` |

Two of those earn their place beyond coverage. **Migration on a column-less
database** is the one that decides whether a mid-work host survives the
deploy. **`machine_for()` falling through** is the terminal path: if it returns
a disabled machine, `bin/wc-claude.sh` launches a session against a backend
the operator shelved, which is silent and is the exact failure the wrapper was
built to prevent.

## Files

- `db.py` — the additive migration
- `routes/db_machines.py` — `enabled` in the SELECT lists;
  `ai_machine_set_enabled`; `ai_machine_activate` refuses a disabled machine
- `routes/db_chats.py` — a helper returning pinned chats for a machine
- `routes/machines.py` — `PATCH` handling for `enabled`, the 409 payload
- `routes/misc.py` — filter `voice_backend_options`
- `bin/wc-backend-env.py` — filter in `machine_for()`, and the profile refusal
- `web/assets/machines.js` — the three card states and the button logic
- `web/index.html` — the dim/badge styling
- tests as above

## Known limits

- Disabling is refused rather than cascading, so shelving a busy backend is a
  two-step job: repoint or re-default first. That is the intended trade.
- `enabled` is a manual flag and says nothing about whether the backend is
  reachable. Transport Check answers that, and the two are deliberately
  separate.
- A conversation pinned to a disabled backend still runs. Reachable only by a
  direct database edit, and honoured on purpose.
