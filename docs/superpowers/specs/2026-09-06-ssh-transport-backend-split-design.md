# SSH Transport / Backend Split — Design

**Status:** Draft, pending user review.
**Origin:** `ssh_proxy` machines were added as a third `ai_machines` provider type
alongside `anthropic`/`claude_code` and `proxy`/`direct`, conflating two different
things in one row: *which backend to talk to* and *where the `claude` process that
talks to it actually runs*. This spec splits them.

## Motivation

Confirmed during design discussion: an `ssh_proxy` machine's purpose is to let a
**different server** (a remote box on the tailnet — Kali3, Pentester-Kali_Mac) run
the actual `claude` CLI subprocess, using backends reached "thru claude code" the
same way this host's own conversations do. `runner.get_proxy_target` already does
this today — an `ssh_proxy` machine forwards its local port to a `claude_proxy`
listening on the *remote* host, so the agent that answers a turn genuinely executes
there, not here.

But today, one `ai_machines` row has to *be* both the remote connection (`ssh_host`,
`ssh_user`, `ssh_key_path`, `ssh_host_key_fingerprint`) and the backend it serves
(`model`, `base_url`, `api_key`, `active_models`) — and the backend half is
degenerate for every `ssh_proxy` row that exists today: `runner.get_backend()`
returns `{}` for any provider other than the wire-protocol-compatible one, so
`base_url`/`api_key` on an `ssh_proxy` row are stored but never read. `model` *is*
read (`get_default_model` has no provider exception), and on both existing rows it
happens to equal CF AI Machine's own default model — a strong signal the real
intent was always "run CF AI Machine's backend, but from over there," not "Kali3
is its own backend."

Confirmed requirement: one transport must be able to back **multiple** backends at
once (e.g. reach Kali3 once, offer two different gateways listening on two
different remote ports through that one connection).

## Non-goals

- **Not** a network-pivot/SOCKS-style transport. The remote host runs its own
  `claude_proxy`/`claude` subprocess; WebConsole never reaches *through* the
  tunnel to dial some other, unrelated backend directly. (This was considered —
  see "Alternative considered" below — and ruled out.)
- **Not** a change to how a *conversation* picks a backend. Pinning stays exactly
  as it is (`chats.ai_machine_id`, `chats.model`, "follow active"); a backend that
  happens to have a `transport_id` is selected the same way any other backend is.
- **Not** a retry/failover mechanism across transports. If Kali3's tunnel is down,
  a backend routed through it fails the same way any unreachable backend does
  today — no automatic fallback to a different transport or to running locally.

## Alternative considered, and rejected

A "pure pivot" model was floated early: `ssh_proxy` as a raw network tunnel that
lets *this* host's `runner` reach a backend it can't dial directly (an
internal-only gateway visible only from the remote host's network position),
with the agent still executing locally. Rejected once confirmed: the actual goal
is the CLI subprocess running *on* the remote host, which is also what the
existing code already does (`claude_proxy` on the far end, not a bare port
forward to an arbitrary backend). Recorded here so a future reader doesn't
reintroduce it as if it were the untried option.

## Data model

**New table `ssh_transports`:**

```sql
CREATE TABLE ssh_transports (
    id                        TEXT PRIMARY KEY,
    name                      TEXT NOT NULL,
    owner_id                  TEXT NOT NULL,
    ssh_host                  TEXT NOT NULL,
    ssh_user                  TEXT NOT NULL DEFAULT 'kali',
    ssh_key_path              TEXT NOT NULL DEFAULT '',
    ssh_host_key_fingerprint  TEXT NOT NULL DEFAULT '',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
```

Exactly the four SSH-connection columns that exist on `ai_machines` today, moved
into their own table, owner-scoped the same way `ai_machines` already is.

**`ai_machines` changes:**

- Add: `transport_id TEXT NULL REFERENCES ssh_transports(id)`. `NULL` (the
  default, and every existing backend's value after migration) means "runs on
  this server" — today's only behavior, completely unchanged. Set means this
  backend's `claude` process runs on that transport's remote host instead.
- Remove: `ssh_host`, `ssh_user`, `ssh_key_path`, `ssh_host_key_fingerprint` (moved
  to `ssh_transports`).
- Remove: `provider = 'ssh_proxy'` as a value. A backend's `provider` again only
  describes the wire protocol it speaks (`anthropic`/`claude_code`,
  `direct`/etc.) — never "how to reach it," which `transport_id` now owns
  independently.
- Unchanged: `model`, `base_url`, `api_key`, `active_models`, `host`, `port`,
  `active`, `owner_id`. When `transport_id` is set, `host`/`port` describe where
  *on the remote host* to reach the backend (forwarded through the tunnel, not
  directly dialable from this machine) — same meaning `host`/`port` already have
  for a plain `proxy`/`direct` backend, just resolved through a different local
  port.
- The same backend can exist as two rows if it should be reachable both locally
  and via a transport (e.g. "CF AI Machine" with `transport_id = NULL`, "CF AI
  Machine (via Kali3)" with `transport_id` set) — matching how this schema
  already tolerates duplicate names rather than inventing a variant mechanism.

**`ssh_tunnels` changes:**

Today: one row per `ai_machines.id`, one SSH connection, one forwarded port.
New: one row per **(transport_id, machine_id)** pair. `tunnel_manager.start()`
groups every backend with a non-null `transport_id` by that id, opens **one SSH
connection per transport**, and adds one `-L <local_port>:<host>:<backend.port>`
forward per backend in the group — so two backends sharing a transport get two
local ports over one connection, and a transport with zero backends referencing
it opens no connection at all. `runner.get_proxy_target` resolves a backend's
local port by the `(transport_id, machine_id)` pair instead of by machine id
alone.

## UI: the Backends map

Today's map is `From (composer) → Runs on (backend list)`. This adds a middle
stop, but **only for backends that have one** — a backend with `transport_id =
NULL` draws no node beyond itself, exactly as today:

```
From                    Executes on            Runs on

┌─────────────┐         ┌──────────────┐      ┌────────────────────────┐
│  New chat   │────────▶│ This server  │─────▶│ Anthropic API  [ACTIVE]│
│  composer   │         └──────────────┘      ├────────────────────────┤
│             │                              │ CF AI Machine           │
│             │                              └────────────────────────┘
│             │
│             │         ┌──────────────┐      ┌────────────────────────┐
│             │────────▶│    Kali3     │─────▶│ CF AI Machine          │
│             │         │ kali-3.tail… │      │  (via Kali3)           │
└─────────────┘         └──────────────┘      ├────────────────────────┤
                                              │ (another backend, same │
                                              │  tunnel, different port)│
                                              └────────────────────────┘
```

"This server" is an implicit node (always present, not a database row) that
every `transport_id = NULL` backend collects under. Each real transport is its
own node, fed by however many backends reference it — adding a second backend
to an existing transport grows that node's backend list; it never adds a second
node for the same transport.

## UI: forms

**"+ Add transport"** — new button next to "+ Add backend", same pattern as
today's single "+ Add machine" button. Small form: `Name`, `SSH Host`, `SSH
User`, `SSH Key Path` — the same three fields already in today's machine form,
moved into their own thing. Saves to `ssh_transports`; appears in the map
immediately as a node with zero backends under it.

**"+ Add backend"** — today's "Add machine" form (`Name`, `Provider`, `Host`/
`Base URL`, `Model`, `API Key`), plus one new field:

```
Executes on
┌──────────────────────────┐
│ This server            ▾ │   ← default; every backend that exists today
├──────────────────────────┤
│ This server               │
│ Kali3                     │
│ Pentester-Kali_Mac         │
└──────────────────────────┘
```

A plain dropdown: "This server" plus every transport the owner has created. No
transports yet → dropdown offers only "This server," with a hint pointing at
"+ Add transport" first. Transport creation deliberately lives in its own
top-level button rather than inline in this dropdown — transports are created
rarely (once per remote box, ever), backends more often (once per routing
combination wanted), so the common path (adding a backend) stays a simple form
rather than gaining a nested creation flow for the rare case.

Same field, same dropdown, on the **edit** form for an existing backend —
changing which transport (or "This server") a backend executes on is an
ordinary field edit, not a special action.

## Migration

Kali3 and Pentester-Kali_Mac's `ssh_host`/`ssh_user`/`ssh_key_path` become two
new `ssh_transports` rows. Since both rows' old `model` field already matches CF
AI Machine's own default (`vllm/Qwen3.6-35B-A3B-NVFP4`) — the strongest available
signal of original intent — auto-create one new backend row per transport: a
copy of CF AI Machine's `provider`/`base_url`/`api_key`/`model`/`active_models`,
with `transport_id` set to the new transport's id. The two old `ai_machines`
rows are deleted once their replacements exist. No conversation currently pins
`ai_machine_id` to either old row (confirmed: zero matches when checked), so
nothing needs re-pinning.

## Out of scope for this spec

- The actual `tunnel_manager.py`/`tunnel_manager_ssh.py` code changes to
  implement multi-backend-per-connection forwarding — this spec describes the
  target shape; the implementation plan (next step) breaks it into tasks.
- Any change to how `routes/machines_tunnel.py`'s existing per-tunnel health/stats
  probing works, beyond it now iterating transports instead of machines.
