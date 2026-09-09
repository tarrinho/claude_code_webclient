# Transport-aware agent-reply — design

## Context

`transcripts.agent_reply_to()` (added earlier this session) lets the
webconsole relay a `<cross-session-message>` into another Claude Code
session's transcript on the caller's behalf, then fires a wake-up turn
through `claude_proxy.py` so the CLI's MCP picks it up promptly.

It only works when the target session lives on this host. Resolution goes
through `~/.claude/sessions/*.json` (local filesystem), the write is a
local file append, and the wake-up turn dials `config.PROXY_HOST`/`PORT`
(local `claude_proxy.py`, port 9000).

Some peer sessions run on remote hosts — Kali3, Kali_Mac, appsectools —
reached today only for **model-backend routing**: `ai_machines.transport_id`
points at an `ssh_transports` row, and `tunnel_manager.py` keeps a live,
shared, authenticated SSH connection (paramiko) per transport, refcounted
across every machine that shares it. That connection currently does exactly
one thing: port-forward `127.0.0.1:<local_port>` to the remote host's own
`claude_proxy.py`, so a turn pinned to that machine spawns its `claude`
process on the remote host. `tunnel_manager_ssh.exec_command(machine_id, cmd)`
already exists on top of that same connection, used today for health
probes — i.e. raw remote command execution over an already-trusted channel
is not new.

A session whose owning machine has a `transport_id` therefore has its
transcript file, its `~/.claude/sessions` registration, and its `cc-socks`
messaging socket **on that remote host's filesystem** — invisible to this
host's `transcripts.py`, which only globs local `~/.claude/projects`.

**Goal:** make `agent_reply_to` reach those sessions too, by reusing the
already-live SSH connection rather than building a new transport.

**Non-goal:** the reverse direction (a session on Kali3 replying to a
session on this host) isn't built here — that host runs its own webconsole
checkout and would do the mirror-image trick outbound from itself.

## Design

### 1. Resolution flow

```
agent_reply_to(target_name, text, *, chat_id, owner):
  1. Try local resolution (today's path, unchanged):
     _session_names_sync() over ~/.claude/sessions/*.json
     → found: append locally, return {"ok": True, "via": "local", "path": ...}

  2. Not found locally → candidates = ssh_transports rows for this owner
     WHERE the matching ai_machines row has tunnel_up=1 (live only —
     never opens a new SSH connection just to probe a name; see Security)

  3. For each candidate transport, in order (sequential, not parallel —
     typically 1-3 transports, and this keeps the "live only" cap in step 2
     meaningful), over its existing exec_command:
       payload = base64(json({"to": target_name, "text": text}))
       remote_cmd = (
         f"cd {remote_path} && {remote_venv}/bin/python -c "
         f"'import base64,json,transcripts; "
         f"d=json.loads(base64.b64decode(\"{payload}\")); "
         f"print(json.dumps(transcripts.agent_reply_to(d[\"to\"], d[\"text\"])))'"
       )
     First transport whose remote agent_reply_to() reports {"ok": True}
     wins. Stop there — no fan-out to the rest once one succeeds.

  4. All candidates exhausted with no match →
     {"ok": False, "reason": "not found locally or on any live transport"}
```

The remote one-liner calls the **same** `transcripts.agent_reply_to()`
already running locally — no separate remote helper script to write or
keep in sync. This only works because the remote host runs the identical
codebase (same checkout, tracked per-transport by the new `remote_path`
column below), so 100% of the resolve+append logic — including its
existing path-safety guarantees (session names resolve to session ids via
the registry, and `transcript_path()` validates the id against
`_SESSION_ID_SAFE_RE` before touching the filesystem) — is inherited for
free rather than reimplemented remotely.

### 2. Wake-up-turn addressing fix

`_fire_wake_up` currently calls `runner._proxy_turn(..., chat_id, ...,
owner)`, which resolves *where to connect* via
`get_proxy_target(chat_id, owner)` — the **replying** chat's own pinned
backend, not the **target's** host. For local-only traffic this
accidentally worked, since the fallback is always `config.PROXY_HOST`/
`PORT` regardless of chat. For a remote target it would silently connect
to the wrong proxy (or fall back to local) and either wake nothing or
misfire `--resume` against a session UUID that doesn't exist on that
connection.

Fix: once resolution (§1) determines *which* transport (if any) served the
target, the wake-up call addresses that transport directly —
`tunnel_manager.tunnel_status(machine_id)` for its `local_port`, bypassing
`get_proxy_target`'s chat/owner inference entirely. The local-resolution
case is unchanged: still connects to `config.PROXY_HOST`/`PORT` as today.

### 3. Security & trust boundary

**No new attack surface.** Reuses the exact SSH connection and credentials
`tunnel_manager` already holds for backend routing — same key, same
authenticated `ssh_client`, same `exec_command` primitive already used for
health probes. No new port, no new listener, no new credential stored
anywhere.

**Command injection avoided by encoding, not escaping.** `exec_command`
runs a string through the remote shell. `to`/`text` are never interpolated
directly (not even via `shlex.quote`) — they're JSON-encoded, then
base64'd, and only the base64 blob is embedded in the command string. Its
alphabet (`A-Za-z0-9+/=`) contains no shell metacharacters, so there is
nothing to escape and nothing to break out of.

**No broadcast probing.** Resolution only queries transports whose tunnel
is already `tunnel_up=1` — never opens a fresh SSH connection just to
check whether a name exists over there. A garbage `to` value fails
locally, then among already-live transports, and stops; it never triggers
a new outbound connection attempt. This keeps the endpoint from becoming a
probe vector against hosts not currently in active use.

**Remote output is data, never instruction.** The remote side returns JSON
only, parsed with `json.loads`; never `eval`/`exec`. Any parse failure or
unexpected shape becomes an error result, not a crash or a follow-on
action — remote/retrieved content cannot redirect what this process does
next.

**Bounded and validated inputs.** `to` is checked against the existing
session-name shape before packaging; `text` gets a length cap (matching
the pattern of `config.PROMPT_MAX_CHARS`). `exec_command` keeps its
existing timeout, wrapped in `asyncio.wait_for` so a wedged remote can't
hang this process indefinitely.

**No privilege expansion.** This doesn't grant the local webconsole
anything it doesn't already have — it's the identical remote-code-execution
capability already trusted today to spawn `claude` processes on that host.

**Local-caller authorization & abuse controls.** The endpoint is reachable
by any logged-in webconsole session, and every cweb session shares this
host's login — so without controls here, one agent (compromised,
prompt-injected, or just over-eager) could inject fabricated
`<cross-session-message>` records into an arbitrary peer's transcript,
local or (with this change) remote, impersonating a relay the human never
asked for. Four controls, all reusing existing patterns:

- **Standard auth stays the gate.** `request.state.session` is already
  required, matching every other `/api/chats/*` route; `chat_id` stays
  owner-scoped via `db.chat_get(chat_id, session["user"])`.
- **Marked, never mistaken for the human.** Already true today — the
  injected record carries `from-name="auto-reply"` /
  `from-mode="auto-reply"`, so a receiving agent can tell this arrived via
  automated relay, not a human's own typed instruction.
- **Per-target cooldown**, same shape as `auto_answer.py`'s
  `_auto_answer_cooldown` (`_COOLDOWN_S = 300.0`), but keyed by
  `(chat_id, target)` and backed by the DB (see below) rather than an
  in-memory dict, since this needs to survive a restart for audit purposes.
- **Audit trail** — every relay attempt (successful or not) is logged: who,
  from which chat, to which target, when, and whether it resolved locally
  or over which transport.

### 4. Schema changes

One column on `ssh_transports`, self-migrating like the rest of `db.py`'s
`init()`:

```sql
ALTER TABLE ssh_transports ADD COLUMN remote_path TEXT NOT NULL
  DEFAULT '~/wc-proxy';
```

Editable through the existing transport edit form (same shape as
`ssh_host`/`ssh_user`/`ssh_key_path`) — no new endpoint.

**Correction (2026-09-09):** the default originally shipped as
`~/projects/claude-code-webconsole`, assumed to match this host's own
layout without checking. It was wrong on every live transport — verified
via each host's `claude_proxy.py` process's own `/proc/<pid>/cwd`, the
real path is `~/wc-proxy` on all four configured transports (Kali3,
Kali_Mac, AppSec Tools, Node1-Appsec). Default and existing rows corrected
to `~/wc-proxy`.

One new table for the cooldown/audit trail from the security section. Not
folded into `chats.auto_answer_log`, because an agent-reply isn't scoped to
one chat's own auto-answer history — it's keyed by `(caller_chat_id,
target_name)`:

```sql
CREATE TABLE IF NOT EXISTS agent_reply_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL, owner_id TEXT NOT NULL,
  target TEXT NOT NULL, via TEXT NOT NULL,        -- 'local' | transport_id
  ok INTEGER NOT NULL, reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_reply_log_cooldown
  ON agent_reply_log(chat_id, target, created_at);
```

### 5. Files touched

- `db.py` — the two schema additions above, plus `agent_reply_log_add` /
  `agent_reply_cooldown_check` functions (owner-scoped, parameterized,
  matching every other function's style).
- `transcripts.py` — `agent_reply_to` gains the transport-broadcast branch
  (§1). Stays a pure function with no DB access itself, matching its
  current shape — the DB-backed cooldown/logging lives in the route.
- `routes/chats.py` — the `/agent-reply` endpoint gains: a cooldown check
  before doing anything, the corrected wake-up-turn addressing (§2), and a
  log write after.
- `routes/machines.py` — add `remote_path` to the transport edit form's
  accepted fields (same list `transport_id`/`ssh_host`/etc. already go
  through).
- `tunnel_manager_ssh.py` — no change; `exec_command` used as-is.

### 6. Testing & verification

- Unit tests for `agent_reply_to`'s new branch: mock `exec_command` to
  return canned JSON; assert the base64/JSON payload is built correctly and
  that `text` is never shell-interpolated raw (a regression test that
  would fail if this were later "simplified" back to string formatting).
- A test asserting the wake-up call uses
  `tunnel_manager.tunnel_status(machine_id)` for a transport-resolved
  target, not `get_proxy_target(chat_id, owner)` — pins the §2 fix the way
  `test_qa_stream_owner.py` already pins the `owner`-threading rule.
- Cooldown test: two rapid calls to the same `(chat_id, target)` — the
  second is rejected before any exec_command/local-write happens.
- No live SSH test against a real transport (no throwaway remote host to
  test against) — mocked at the `tunnel_manager.exec_command` boundary,
  consistent with how the rest of this codebase tests transport-routed
  code paths (existing `test_qa_transports_*` files).
