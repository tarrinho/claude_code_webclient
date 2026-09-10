# Remote QA execution — design

## Context

`bin/run-suite-chunked.sh` exists because the whole suite in one process gets
OOM-killed on this box (3.8 GB, shared by up to six interactive `cweb*`
sessions plus the live server). Chunking bounds what one run costs, but it
does not create memory that is not there — today's session was refused
outright more than once:

```
run-suite-chunked: refusing to start — not enough memory on this host.
6 interactive agents holding 1669 MB; the console holds 871 MB
```

The goal here is relief from that specific pressure: move a suite run's
memory cost off this host, onto a transport with room, using machinery this
project already has rather than building a second one.

**What already exists and this design reuses as-is, unmodified:**

- `tunnel_manager_ssh.exec_command(machine_id, cmd, timeout)` — run a command
  over a transport's live SSH connection, `(stdin, stdout, stderr)` back.
- `tunnel_manager_ssh.open_sftp(machine_id)` — an SFTP session over the same
  connection.
- `transport_sync.sync_transport(machine_id, remote_path, last_synced_sha)` —
  push this host's `git ls-files` manifest (or an incremental diff against
  `last_synced_sha`) to `remote_path` on the transport. **Already parameterised
  on both `remote_path` and the SHA it diffs against** — it does not read
  `ssh_transports.remote_path` or `.last_synced_sha` itself; those are the
  production-sync caller's choice, not the engine's. This is why §2 below
  needs no changes to `transport_sync.py` at all.
- `bin/run-suite-chunked.sh`'s file-discovery and chunking rule: the file list
  comes from `pytest --collect-only`, never a glob (a glob silently missed
  `test_functional.py` once and shipped a release verified against a report
  that had never actually run it); browser files run one at a time, everything
  else in groups; a per-chunk wall-clock cap turns a hang into a failed chunk
  instead of a starved, silently-partial run.

**Non-goal:** general-purpose remote code execution, or a second way to
deploy `claude_proxy.py`. This is one job — run this project's own test suite
somewhere with spare memory and report back — and every piece below is scoped
to that job.

**Explicit limitation, carried forward from `run-suite-chunked.sh`'s own
header rather than dropped:** *"a chunked run and a single-process run are not
equivalent... a green result here does not license the claim that the whole
suite is green."* A remote run inherits that caveat and adds to it — different
filesystem, different load, network latency touching anything timing-sensitive.
This is for getting a trustworthy reading when the local box cannot give one,
not a replacement for a full local run when the machine is quiet. Nothing
about to ship should be verified by a remote run alone.

## Design

### 1. Provisioning — once per transport, separate from every later sync

New script, `bin/wc-provision-qa.sh <transport>`, in `wc-deploy-proxy.sh`'s
own shape: resolve the transport's `ssh_host`/`ssh_user`/`ssh_key_path` from
`ssh_transports` (never re-derive connection details a second way), then over
SSH:

```
python3 -m venv ~/wc-qa-checkout/.venv
~/wc-qa-checkout/.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
~/wc-qa-checkout/.venv/bin/playwright install chromium
```

Idempotent: checks whether `~/wc-qa-checkout/.venv/bin/python` and a chromium
binary already exist before doing the expensive parts again, mirroring
`wc-deploy-proxy.sh`'s own "safe to press again" property. This step is
deliberately not part of every QA run — a `pip install` plus a Chromium
download is real time and bandwidth, worth paying once, not on every
invocation.

Run once, by hand, before a transport is usable for QA. Not automatic, not
triggered by a sync or a test run — a provisioning failure (disk full,
network flaky) should be diagnosable on its own, not buried inside a larger
operation's output.

### 2. Sync — the existing engine, a second path, a second pointer

`~/wc-qa-checkout` is deliberately **not** `~/wc-proxy` (the transport's
`remote_path`, where `claude_proxy.py` actually runs from). Two independent
reasons, both load-bearing:

- **Blast radius.** `~/wc-proxy`'s sync (`docs/superpowers/specs/2026-09-09-transport-project-sync-design.md`)
  is human-approval-gated because it writes files that become the running
  application. A QA checkout never runs as the application — it only ever
  runs `pytest` against itself — so it does not need, and should not carry,
  that approval gate. Making it share a path with `~/wc-proxy` would either
  put test scaffolding on the production checkout, or force every QA run to
  wait on a human click before it could even sync.
- **Independent cadence.** A QA run wants "whatever is on `HEAD` right now,"
  which can be more frequent than anyone wants to manually approve pushes to
  the live proxy's own source.

`ssh_transports` gains one column, migrated the same way `last_synced_sha`
was (`db.py`, alongside the existing `if columns and "last_synced_sha" not in
columns:` block):

```sql
ALTER TABLE ssh_transports ADD COLUMN last_qa_synced_sha TEXT NOT NULL DEFAULT ''
```

Plus a setter mirroring `ssh_transport_set_last_synced_sha` exactly
(`routes/db_transports.py`):

```python
async def ssh_transport_set_last_qa_synced_sha(transport_id: str, sha: str) -> None
```

A QA run's sync step is one call, no new sync engine code:

```python
result = await transport_sync.sync_transport(
    machine_id, "~/wc-qa-checkout", transport["last_qa_synced_sha"])
if result["ok"]:
    await db.ssh_transport_set_last_qa_synced_sha(transport_id, result["head_sha"])
```

Unattended — no `transport_sync_requests` row, no approval step. This is the
one place this design deliberately diverges from the production-sync
precedent, and the reason is stated above (§2, blast radius): the security
property that mechanism protects (nothing reaches the running application
without a human) does not apply to a checkout that never runs as the
application.

### 3. Capacity check — measured live, on the transport, not trusted from storage

`tunnel_manager_health.collect_stats` already runs `free | awk
'/Mem:/{printf "%.1f", $3/$2*100}'` over `exec_command` for connected
transports, but its `store_fn` currently fails on every call
(`wc.tunnel.health: store remote stats failed: system_sample_insert() takes 1
positional argument but 2 were given` — an existing, separate bug, not part
of this design to fix) — so nothing about remote memory is reliably
persisted. A capacity check for this feature cannot read stored data; it has
to ask the transport directly, every time, immediately before using it:

```python
_, stdout, _ = await exec_command(machine_id, "free -m | awk '/Mem:/{print $7}'", timeout=5)
available_mb = int(stdout.read().decode().strip())
```

Checked before syncing (no point pushing a checkout to a host about to refuse
the run) and again before each chunk (load can shift mid-run on a shared
transport the way it does on this host). Below a floor (default 700 MB,
matching `WC_SUITE_COST_MB`'s existing default for a chunk's declared cost),
the check fails.

The pre-sync check and the per-chunk check fail differently, and the
difference is deliberate: the pre-sync check picks (or refuses) the transport
for the *whole run* (§5) — nothing has been pushed or run yet, so refusing
there costs nothing and trying a different transport is free. A per-chunk
check failing mid-run does **not** switch transports — the run already
committed to one, that transport's checkout is the one actually synced, and
hopping to another mid-run would need a sync and (if unprovisioned) a full
provisioning step nothing here waits for. A chunk that fails its own
pre-check is recorded failed for that reason, in the same per-chunk results
file a timed-out chunk already lands in (§6) — surfacing that the chosen
transport ran out of room partway through, rather than silently completing a
run that starved on its last few chunks.

**Deliberately a hard refusal, not a warning** — the opposite choice from
today's local agent-spawn guard, and the difference is the situation, not a
change of mind: an interactive agent spawn refusing locks the operator out of
the one tool that could fix an overloaded host, which is the worst possible
failure direction. A QA run choosing a different transport is a batch job
with other nodes to try; blocking one and reporting why is strictly better
than running anyway and reporting a false memory-pressure failure, or
"succeeding" on a host so starved the numbers cannot be trusted.

### 4. Where this runs: inside the app process, not a standalone script

Self-review caught a real gap here, worth stating plainly rather than fixing
silently: `exec_command`, `open_sftp`, and therefore `sync_transport` are all
bound to `tunnel_manager._STATE` — the **running `webconsole.service`
process's own in-memory live SSH connections**. A bash or standalone Python
script invoked from a terminal has no access to that state; it is not
persisted anywhere a second process could read it. This is the same wall hit
earlier today verifying live behaviour, worked around then by minting a
throwaway API token and calling the running server's own HTTP API rather than
its internals directly — the same fix applies here.

**Consequence for the design:** the orchestrator is not a standalone script
that calls `exec_command`/`sync_transport` directly. It is route logic running
*inside* the app process (a new module, e.g. `qa_remote.py`, called from a new
route — mirroring exactly how `/api/transports/{id}/sync` already works),
where `tunnel_manager._STATE` is real. `bin/wc-run-suite-remote.sh` becomes a
thin CLI wrapper: mint a short-lived API token the same way, `POST` to the new
route, stream the response back to the terminal. This is not a downgrade —
it's what keeps the "zero changes to `transport_sync.py`" claim true, since
that engine was built assuming a live connection and this does not ask it to
be anything else.

**Real precondition this surfaces, not previously stated:**
`transport_sync.py`'s own design already says it plainly — *"Only transports
with `tunnel_up=1` are ever synced."* A QA run therefore needs its target
transport already Active before it can do anything at all. Unlike Check
(`transport_readiness.check_transport`) and Init, which deliberately open
their *own* one-shot SSH connection specifically so they work on a transport
that has never connected — *"a cold transport is exactly when these
questions matter most"* — a QA run cannot follow that pattern without
either requiring the target be live first, or duplicating `transport_sync`'s
diff/push logic outside `tunnel_manager`. Requiring Active is the cheaper,
more honest choice: refuse clearly (`"<name> has no live tunnel — Check or
Init it first"`) rather than silently reimplementing connection handling a
second way.

**Second precondition, same shape:** liveness proves the transport can be
reached; it does not prove `bin/wc-provision-qa.sh` has ever been run there.
Before syncing, check for `~/wc-qa-checkout/.venv/bin/python` over
`exec_command` (a `test -x` one-liner, no different in kind from the
capacity check in §3) and refuse with `"<name> has not been provisioned for
QA — run bin/wc-provision-qa.sh <name> first"` if it is missing. Without
this, the first sign of an unprovisioned transport is a
`.venv/bin/python: No such file or directory` from inside a pytest command
string — technically correct, useless to whoever is staring at it.

### 5. Node selection

```
POST /api/qa/run   {"transport": "<name-or-omitted>"}
```

exposed as a route, invoked by `bin/wc-run-suite-remote.sh [transport-name]`
via the mechanism in §4. Named explicitly: use that one, or refuse with the
capacity reason (§3) or the liveness precondition (§4) if it cannot take the
run. Unnamed: consider only transports already Active, check each one's live
memory (§3), and pick the roomiest; refuse entirely, in this host's own name,
if none qualifies — never silently fall back to running locally, which would
masquerade as relief while actually adding another process to the box this
feature exists to relieve.

**One run per transport, held for the run's whole lifetime.** Nothing above
stops two callers — two `cweb` sessions, or a retry racing a still-running
attempt — from targeting the same transport at once; doing so doubles the
remote memory cost this design exists to avoid, on the exact host it was
supposed to relieve. `qa_remote.py` holds an in-process lock per
`machine_id` (a dict of asyncio locks, the same shape `tunnel_manager`
already uses for one-connection-attempt-at-a-time) for the run's full
duration — sync through last chunk. A second run targeting a locked
transport is refused immediately: `"<name> already has a QA run in progress
— wait for it or pick another transport"`, not queued silently behind it.
Unnamed node selection treats a locked transport the same as one that fails
the capacity check: skipped when picking the roomiest, refused if it was the
only candidate.

### 6. Execution and result streaming

Reuses `run-suite-chunked.sh`'s file-discovery and grouping rule exactly —
`pytest --collect-only` for the file list (never a glob), browser files one
at a time, everything else in groups — so the two runners can never define
"a chunk" two different ways. The chunk command itself runs remotely:

```python
cmd = f"cd ~/wc-qa-checkout && .venv/bin/python -m pytest {chunk_files} -q --tb=short"
_, stdout, stderr = await exec_command(machine_id, cmd, timeout=CHUNK_TIMEOUT)
```

Output is read and written to a local per-chunk results file as it comes
back, the same "a chunk that dies is visible as itself" property
`run-suite-chunked.sh` already has — never buffered into one aggregate that
could go silently missing a slice. `CHUNK_TIMEOUT` mirrors the local script's
own per-chunk wall-clock cap (default 600s): a chunk that does not finish in
time is reported as failed, never as a missing, uncounted chunk.

**Streamed, not held open as one blocking response.** A full run is many
chunks at up to `CHUNK_TIMEOUT` each — minutes, not seconds — so
`POST /api/qa/run` responds the way `stream_turn` already does for a turn:
`StreamingResponse`, one JSON line per event (`chunk-start`, `chunk-result`,
`run-done`), consumed incrementally by `bin/wc-run-suite-remote.sh` and
printed as it arrives. A single blocking response would mean nothing is
visible until the entire run finishes, and would tie the whole result to one
HTTP request surviving for the run's full length — including surviving a
`systemctl --user restart webconsole.service`, which CLAUDE.md rule 9
already documents as routine here. Made explicit rather than left to be
discovered live: a restart during a QA run kills the request the same way it
kills a turn — the connection drops, `bin/wc-run-suite-remote.sh` reports
the run as interrupted, and the remote `pytest` chunk process (a plain child
of the SSH session, not detached) dies with it. No resume-in-place;
re-running is `bin/wc-run-suite-remote.sh <transport>` again, which re-syncs
(cheap, incremental) and starts a fresh set of chunks. Full resumability
(picking up only the chunks that had not finished) is not attempted here —
YAGNI unless restarts during QA runs turn out to be common enough to matter.

**Each chunk result records which of two things failed, not just that
something did.** An `exec_command` raising (SSH drop, timeout, transport
gone away) and a chunk's own pytest run failing are different problems with
different next actions — one says "try again, maybe on this transport,
maybe another"; the other says "a test actually broke." The per-chunk result
file gets a `status` field distinguishing them: `"passed"`, `"test_failure"`
(pytest ran, something in it failed), `"transport_error"` (`exec_command`
itself raised or timed out — the chunk never got a real pytest verdict),
`"capacity_refused"` (§3's per-chunk check). A run summary that only says "N
chunks failed" hides which of these it was; the report
`bin/wc-run-suite-remote.sh` prints distinguishes them the same way.

### 7. Security

No new credential and no new listener — same live SSH connection every other
transport feature already holds, same trust tier. The manifest pushed to
`~/wc-qa-checkout` is `git ls-files` on this host, identical security property
to the production sync (§5 of the 2026-09-09 sync design): nothing about what
gets pushed is influenced by anything remote. Skipping the approval gate (§2
above) is safe specifically because the destination never executes as the
application — approving that gate protects against exactly the class of risk
a QA-only, non-running checkout does not carry.

**Token scope is inherited, not new, and worth stating rather than
assuming:** `bin/wc-token.py` (existing) mints a token carrying whatever role
its owner has — there is no per-route scoping mechanism in this codebase
today, so a token minted for `wc-run-suite-remote.sh` can call anything that
owner's session could, not only `/api/qa/run`. This design does not add
scoping (out of scope — it would be a change to `auth.py`/`middleware.py`
serving every token consumer, not just this one) but it does fix the expiry:
`wc-run-suite-remote.sh` mints with a short, explicit expiry
(`wc-token.py create ... --days 1`) rather than the no-expiry default, so a
token left in a script's environment or a stray log line is a bounded
exposure, not a standing one. Calling it "short-lived" without pinning a
number was a gap in the draft; one day is the number, chosen to comfortably
cover a single run plus a same-day retry without leaving a token live for a
week nobody remembers.

## Files touched

- `bin/wc-provision-qa.sh` (new) — one-time per-transport environment setup.
  Standalone, its own one-shot `ssh`/`scp`, no live tunnel required — same
  shape as `wc-deploy-proxy.sh`, for the same reason (§4): provisioning has
  to work on a transport nothing has connected to yet.
- `db.py` — `ssh_transports.last_qa_synced_sha` column + migration.
- `routes/db_transports.py` — `ssh_transport_set_last_qa_synced_sha`.
- `qa_remote.py` (new) — the orchestrator, run *inside the app process* (§4):
  liveness precondition → provisioning check (§4) → per-transport lock (§5) →
  node selection (§5) → capacity check (§3) → sync (§2, calling
  `transport_sync.sync_transport` unmodified) → chunked remote pytest,
  streamed (§6) → per-chunk result files tagged `passed`/`test_failure`/
  `transport_error`/`capacity_refused` (§6), same layout
  `run-suite-chunked.sh` already produces.
- `routes/qa.py` (new) — `POST /api/qa/run`, `StreamingResponse` calling into
  `qa_remote.py` (§6). Owner-scoped like every other transport route.
- `bin/wc-run-suite-remote.sh` (new) — thin CLI wrapper: mint a short-lived
  (`--days 1`, §7) API token, `POST /api/qa/run`, consume and print the
  streamed response incrementally, reporting an interrupted run plainly if
  the connection drops mid-run (§6). Not a standalone SSH client itself —
  see §4 for why it cannot be one.
- No changes to `transport_sync.py`, `tunnel_manager_ssh.py`, or
  `run-suite-chunked.sh` itself — all three are consumed as they already are.
- Tests: capacity-gate refusal (mocked `exec_command` returning low
  available memory), node-selection picks the roomiest *Active* transport and
  refuses cleanly when none qualifies (including when every transport exists
  but none is live), the QA sync path/column stays independent of the
  production one (a QA sync must never read or write `last_synced_sha`, and
  vice versa), a chunk-file-list parity check mirroring
  `run-suite-chunked.sh`'s own collected-vs-planned assertion, an
  owner-scoping test on `POST /api/qa/run` matching the existing transport
  route tests' rigor, a provisioning-check refusal (mocked `exec_command`
  reporting no venv) with the exact "run wc-provision-qa.sh first" message,
  a per-transport lock test (second concurrent run against a locked
  transport is refused, not queued, and a locked transport is excluded from
  unnamed node selection), a streaming-response test asserting events arrive
  incrementally rather than only after the whole run completes, a
  `transport_error`-vs-`test_failure` tagging test (an `exec_command` raising
  produces `transport_error`, a nonzero pytest exit with output produces
  `test_failure`), and a token-expiry test confirming
  `wc-run-suite-remote.sh` mints with the short expiry rather than the
  no-expiry default.
