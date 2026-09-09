# Transport project sync — design

## Context

The spike behind this (2026-09-09, chat) confirmed SFTP works over
`tunnel_manager`'s existing live SSH connection — the same one used for
backend routing and, as of the previous design, agent-reply relay. That spike
also found a real bug (a wrong `remote_path` default, now fixed) and
confirmed every remote checkout (`~/wc-proxy` on Kali3, Kali_Mac, AppSec
Tools, Node1-Appsec) is a **plain directory, not a git repository** — files
were placed there by hand (`deploy_kali3.sh`'s manual `scp`), not cloned.

**Goal:** a standing mechanism to keep a remote transport's checkout in sync
with this host's, triggerable either by a human (a button next to each
transport) or by an agent on the remote end asking for it — with a human
always approving before anything is actually pushed, because this writes
files that become the application's own running source.

**Non-goal:** turning the remote checkout into a real git repository, or
building general-purpose file transfer for arbitrary paths. Scope is fixed
to `git ls-files`' output on this host.

## Design

### 1. What gets copied, and how

The manifest is `git ls-files` on **this host**, never remote input — this
is the load-bearing security property (see §5). It is exactly what
`.gitignore` already trusts enough to publish to GitHub, so no secret
newly enters scope by adding this feature.

Transfer is SFTP (`paramiko`'s `open_sftp()`) over the transport's existing
live connection. `tunnel_manager_ssh.py` gains one new primitive, mirroring
`exec_command`'s shape:

```python
async def open_sftp(machine_id: str):
    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        raise RuntimeError("tunnel not connected")
    return state["ssh_client"].open_sftp()
```

Every SFTP call (`put`, `remove`, `mkdir`) is blocking (paramiko), so each
one runs through `asyncio.to_thread` — the same rule this codebase applies
to every other blocking call over a tunnel connection.

Only transports with `tunnel_up=1` are ever synced — the same "live only"
rule as agent-reply resolution: never open a fresh connection just to sync.

### 2. Incremental sync: diff against the last-synced commit

`ssh_transports.last_synced_sha` (new column, default `''`) tracks the git
SHA this host last successfully pushed to that transport.

- **Empty** (first sync ever for this transport): full manifest, every
  tracked file pushed.
- **Set**: `git diff --name-status <last_synced_sha> HEAD` gives
  Added/Modified/Deleted/Renamed paths. A/M are pushed; D are removed
  remotely; R (rename) is handled as remove-old + push-new — simplest
  correct behaviour, and renames are rare enough in this repo that a
  smarter single-request rename isn't worth the complexity.
- `last_synced_sha` only advances **after every file in the batch succeeds**.
  A partial failure must not silently advance the pointer — that would
  permanently lose the failed file's diff on the next sync, since it would
  no longer appear between the (wrongly-advanced) last-synced SHA and HEAD.

Remote directories are created as needed before a `put` — SFTP has no
`mkdir -p`, so the push walks each path component and creates any that are
missing (`mkdir`, ignoring "already exists").

### 3. Two entry points, one lifecycle

Every sync — human or agent-triggered — is a row in one table:

```sql
CREATE TABLE IF NOT EXISTS transport_sync_requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  transport_id  TEXT NOT NULL,
  owner_id      TEXT NOT NULL,
  requested_by  TEXT NOT NULL,   -- session name that asked, or 'ui'
  status        TEXT NOT NULL DEFAULT 'pending',
                                  -- pending | approved | rejected | done | failed
  files_changed INTEGER,
  reason        TEXT,
  created_at    TEXT NOT NULL,
  resolved_at   TEXT
);
```

Decided in chat: unify rather than split, matching the precedent
`agent_reply_log` already set (log every attempt unconditionally, not just
the notable ones) —

- **UI-triggered** (`POST /api/transports/{id}/sync`): inserts a row at
  `status='approved'` (the click *is* the approval) and runs synchronously,
  updating to `done`/`failed` with `files_changed` before the response
  returns.
- **Agent-triggered**: a remote peer's message creates a row at
  `status='pending'`; nothing is pushed until a human approves it through
  the UI. Two new endpoints: `POST .../sync-requests/{req_id}/approve`
  (runs the sync synchronously, same as the UI path, then resolves to
  `done`/`failed`) and `POST .../sync-requests/{req_id}/reject` (resolves
  to `rejected`, pushes nothing, matching the discard pattern the
  question/prompt UI already uses elsewhere in this codebase).

### 4. The message-driven trigger: a literal marker, not a heuristic

`auto_answer.py`'s own docstring already states this codebase's rule:
*"Deliberately not a heuristic on the prompt text... matching phrases
against model-authored prose is what made [it] fire on unrelated text
elsewhere in this tree."* The same rule applies here, more forcefully:
this trigger ends in a file-writing operation, so a false positive is a
worse failure than an unanswered prompt.

Convention: a remote agent's SendMessage/agent-reply text must **start**
with the literal token `SYNC_REQUEST` (case-sensitive). A new small poller,
`sync_request_watcher.py`, mirrors `auto_answer.py`'s `_loop`/`_pass` shape:
every `interval_s`, it scans `transcripts.agent_traffic()` for incoming
messages matching exactly that prefix, resolves the sender to a transport
(via the sender's `ssh_transports` row, matched the same way agent-reply
resolves a target), and inserts a `pending` row — deduplicated so a
message already turned into a pending (or resolved) request doesn't spawn
a second one.

### 5. Security

**The manifest is never attacker-influenced.** A remote peer's message can
request *that* a sync happen; it cannot say *what* gets synced — the file
list comes from `git ls-files` on this host, entirely independent of
message content. A compromised or malicious peer session can at most
trigger a pending request that a human must still approve; it cannot smuggle
arbitrary content into what gets pushed.

**Human approval is the gate for anything agent-triggered.** No file
reaches a remote host from a message-driven request without a person
clicking Approve — this is the direct answer to "this writes code that will
run," decided in chat rather than defaulted to automatic.

**Paths stay inside `remote_path`.** Every remote path is
`remote_path`-joined against a **relative** path that itself came from
`git ls-files` on this host — never from message content, never an
absolute path, never containing `..` (git doesn't produce such paths for
tracked files, but the join logic rejects any that would escape the root
as defense in depth).

**No new credentials, no new listener.** Same live SSH connection
`tunnel_manager` already holds, same trust tier as everything built on it
so far.

**No silent pointer advancement.** Covered in §2 — a partial failure must
not advance `last_synced_sha`, or the unsynced diff is lost.

### 6. Files touched

- `db.py` — `ssh_transports.last_synced_sha` column,
  `transport_sync_requests` table + migration.
- `routes/db_transport_sync.py` (new) — CRUD/query for
  `transport_sync_requests`, matching `routes/db_agent_reply.py`'s shape.
- `tunnel_manager_ssh.py` — `open_sftp(machine_id)`.
- `transport_sync.py` (new) — the sync engine: diff computation (`git
  diff`/`git ls-files` via subprocess), push/delete orchestration, SHA
  advancement.
- `routes/transports.py` — `POST /api/transports/{id}/sync`,
  `POST /api/transports/{id}/sync-requests/{req_id}/approve`,
  `POST /api/transports/{id}/sync-requests/{req_id}/reject`, a list
  endpoint for pending requests.
- `sync_request_watcher.py` (new) — the polling loop described in §4.
- `web/assets/transports.js` — a Sync button per transport row (alongside
  the existing Edit/Check/Init/Delete), and a pending-request
  badge/approve control.
- Tests mirroring `tests/test_qa_transport_agent_reply.py`'s rigor: pure
  diff-parsing tests, SFTP push/delete mocked at the
  `tunnel_manager_ssh` boundary, a path-escape defense-in-depth test, an
  approval-gate test (message-driven request must not push pre-approval),
  and a partial-failure test asserting `last_synced_sha` does not advance.

## Open question for spec review

`sync_request_watcher.py` as a new standing poller vs. folding this scan
into the existing `auto_answer.py` loop: kept separate here because they
answer different questions (auto-answer presses keys on this host's own
prompts; this scans incoming agent traffic for a specific text marker) and
mixing them would make `auto_answer.py`'s already-careful "never a
heuristic on prose" docstring cover two different kinds of pattern-matching
with two different risk profiles. Flagging this rather than deciding it
silently, since it's a structural choice, not just an implementation detail.
