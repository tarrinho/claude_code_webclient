# Changelog

All notable changes to WebConsole, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
version string lives in `config.VERSION` as `WebConsole_<semver>`.

This file is reconstructed from the git history and is authoritative from 0.1.0
onward. Two version numbers were skipped and never shipped: **0.4.0** and
**0.7.1**. They are recorded here so a gap in the sequence does not read as a
lost release.

Entries describe what changed for someone using or operating the console. Where
a fix is worth understanding rather than merely noting, the cause is stated —
several of these were invisible from the outside and would otherwise look like
churn.

---

## [Unreleased]

### Fixed

- **The application log recorded nothing.** Every `_log.info` in the project had
  been discarded since the first commit. The server runs as
  `python3 -m uvicorn app:app`, which *imports* `app.py`, but the only
  `logging.basicConfig` call sat inside `if __name__ == "__main__"` and never
  ran. Uvicorn configures only its own loggers, leaving root with no handler,
  and logging's last-resort fallback emits WARNING and above. `logging.conf`
  was in the repo and referenced by nothing.

  It stayed hidden because `logs/webconsole.log` kept growing — that file was
  the shell's stdout redirect collecting uvicorn's access lines. It held not one
  `wc.*` record, so none of the login, chat-creation or turn-timeout logging the
  build pipeline requires actually existed. `grep -c "login user="` returned 0
  against 209 KB of what looked like a healthy log.

  `_configure_logging()` now runs at import, loading `logging.conf` when present
  (with `disable_existing_loggers=False`, or uvicorn's own access log would go
  silent) and falling back to a stream handler so a broken config cannot stop
  the server from booting.

### Added

- Tool calls in the transcript viewer show what was actually run and what came
  back, rather than only the tool's name.
- `tests/test_qa_logging.py` and `tests/test_qa_logging_setup.py` — regression
  coverage for the above. Removing the import-time call fails three of them.

---

## [0.8.0] — 2026-08-29

### Changed

- **A web request now reaches the terminal that is running the conversation.**
  A conversation linked to a live CLI session had two possible homes for a turn
  and picked the wrong one: a request typed in the browser spawned
  `claude --resume` as a second, headless process. The work happened correctly
  and invisibly, in a window nobody was watching, while two processes appended
  to one transcript — the user watched their terminal and saw nothing.

  The request is typed into the terminal actually running that conversation, so
  the request and every step of the answer appear where the user is looking, and
  the reply still reaches the page through the existing transcript sync. With no
  live window, the headless turn runs exactly as before.

### Security

- **Stopped trusting the session file that names the target pid.**
  `~/.claude/sessions/<id>.json` is neither authoritative nor protected, and
  `locate()` trusted the pid recorded in it.

  *F-01:* the server records its own pid for every session created through the
  web, so walking up from that pid reached the shell and multiplexer window the
  server was launched from — a web-created session resolved to the server's own
  window. The existing answer path survived on a prompt-shaped gate; the newer
  free-text delivery path had no gate, and a line typed at a shell runs.

  *F-02:* the file is writable by any process running as this user, which
  includes every agent spawned with `--dangerously-skip-permissions`.

  `session_pid` now treats the file as a *claim* and checks it: the pid must
  identify as `claude` in `/proc`, must not be this process or any ancestor of
  it, and two live pids claiming one session are refused rather than resolved.
  F-02 is narrowed, not closed — while agents share this account, any file-based
  session-to-pid mapping stays agent-writable. The real fix is a privilege
  boundary, and `docs/threat-model.md` says so plainly.

### Documentation

- `docs/threat-model.md`: 20 findings, 5 confirmed against the running
  deployment. Records F-01 fixed, F-02 narrowed, and one recommendation from the
  first draft that was **wrong** — refusing any target sharing the server's
  window would have refused a legitimate target, because an interactive agent
  runs in the server's own window on this deployment. It had already been acted
  on, so it is corrected in place rather than quietly dropped.

---

## [0.7.2] — 2026-08-29

### Added

- **A supervisor that says which agents are waiting on you.** Surfaces sessions
  that have asked a question and are blocked, so a stalled agent is visible
  instead of silently idle.
- **Terminal usage folded into the Usage tab.** Turns run in a terminal never
  pass through the app, so the tab reported only what was typed into the
  website — 6 events against roughly nine thousand real ones. Every assistant
  record in a Claude Code transcript carries its model and token counts, so the
  history is recoverable after the fact; a byte cursor per transcript keeps a
  re-run from counting the same turns twice, and `created_at` comes from the
  record rather than the clock.
- **Durable sessions.** Sessions are mirrored to SQLite keyed by a hash of the
  id, so a restart no longer logs everyone out — on a phone-first console that
  meant retyping a password a dozen times a day. The raw id never reaches disk,
  so neither the table nor a database export can be replayed as a login.
- Answer a terminal session's question from the browser.

### Fixed

- **Transcripts a gateway left unreplayable on Anthropic.** Switching an
  existing conversation from the local gateway to an Anthropic model failed with
  `400 messages: text content blocks must be non-empty`, before the new prompt
  was even considered. The gateway streams one reply as several assistant
  records and the first can carry an empty text block; it replays those happily,
  the Anthropic API rejects the whole request. One conversation held 2074 such
  records, another 1778 — conversations written by Claude models held none.
  `transcripts.repair_if_needed()` removes them and relinks each child to its
  nearest surviving ancestor, backing the file up first.
- Questions and their options now render in the web page.
- A terminal question reaches the conversation, not just the viewer.
- Prompts aim at the asking window by identity rather than by screen contents.

---

## [0.7.0] — 2026-08-29

Version 0.7.1 was skipped.

### Added

- **Live CLI history.** A chat linked to a CLI session follows its transcript,
  polling every five seconds with a refresh button beside the run state, so
  turns typed in the terminal appear without reopening anything.

  A turn sent from WebConsole is written to `messages` *and* appended to the same
  transcript, because the runner resumes with `--resume` — measured, not
  assumed: one `/messages` call grew the transcript by 9 KB and the table by two
  rows. A naive sync would replay every web turn, so each chat carries a
  `transcript_offset` advanced past its own turns, and the sync reports only what
  arrived from elsewhere. The offset also keeps the poll cheap: the transcript
  this was built against is 22 MB.
- **Per-conversation backend and model.** One conversation can sit on the
  Anthropic API while another runs against a self-hosted machine, each on a
  different model. Before this the backend was global, so switching it in
  Settings moved every conversation at once. `chats.ai_machine_id` had been in
  the schema all along, selected and copied by `chat_fork`, but nothing wrote it
  and nothing read it for routing; this wakes that column up rather than adding
  another. A pin to a deleted machine, or to another owner's, falls back rather
  than failing the turn.

  Switching backend deliberately **keeps** the Claude session: `--resume` reads a
  local transcript so it stays valid, and starting fresh would silently discard
  continuity nobody asked to lose.
- **Favourite from the row, and manual ordering.** Favouriting existed as Pin,
  buried as the first item in the `⋯` menu — the most-used action behind the
  most clicks. It is now a `★` on the row itself. Manual ordering adds a
  nullable `chats.position`, with the rule worth stating plainly: *a conversation
  you have placed keeps its slot even when it gets new activity.* Sending an
  empty order clears every placement and returns the list to pure recency.
  Reordering is a single `PUT /api/chats/order` carrying the whole ordered
  section, applied in one transaction — a cascade of per-conversation updates
  could half-apply and leave an order nobody chose.

### Changed

- The conversation name moved out of the topbar into the workspace strip, ahead
  of its directory, so the two read as name-then-location on one line rather
  than being split across two rows that were never read together.

### Fixed

- A chat imported before `transcript_offset` existed carried the column default
  of 0 while already holding its history, so reading from 0 re-imported all of
  it — found live, having duplicated 63 messages. Such a chat is now treated as
  caught up and its position recorded.
- `handle_chat_get` and `handle_chats_list` build payloads from explicit field
  lists, so `ai_machine_id` was invisible to the client and a reopened
  conversation could not show its own backend.

---

## [0.6.1] — 2026-08-29

### Added

- **Suppressed costs now explain themselves using the CLI's own verdict.**
  Claude Code reports a per-model `costBasis`; the live gateway returns
  `"unknown"`. Stored in a new `usage_events.cost_basis` column and surfaced on
  the totals, so the dash in the cost column can say why it is a dash.

  Deliberately **not** used as the gate — `base_url` still decides whether cost
  is shown, because `costBasis` has exactly one observed value, from a gateway.
  What the official API reports is unobserved, so branching on it could suppress
  cost everywhere, including where it is real.

### Changed

- One source of truth for backend classification: `_usage_provider` became
  `backend_kind` and is served on both machine payloads, so a client no longer
  reimplements the provider + `base_url` rule. Two copies of that rule agreeing
  is a coincidence, not a guarantee.

---

## [0.6.0] — 2026-08-29

### Added

- **A dedicated view of the traffic between concurrent sessions.** Four Claude
  sessions had been coordinating over this repository all day — claiming files,
  catching each other's bugs, handing work back and forth — and none of it was
  visible anywhere.

  The messages are already on disk: each session's transcript records both what
  it sent and what it received, so this reads them rather than tapping the unix
  sockets the sessions talk over — a log, not an interception layer. It needed a
  separate parser: a message is written into four different record shapes, three
  of which the conversation parser deliberately drops as machinery, so reusing
  it would have shown 8 of 15 messages and looked complete.

  Each message is recorded at both ends and is normalised to
  `sender → recipient` and deduplicated: 146 raw records became 76 actual
  messages. A peer named inconsistently (by name in one record, by socket path
  in another) is resolved through the session registry, so the two ends can be
  matched rather than collapsed on a guess.
- **Usage tab** showing requests and tokens per model.

---

## [0.5.1] — 2026-08-29

### Added

- **Terminal transcript viewer** — read past conversations, page through them,
  and reopen any of them.
- Sidebar rework and Settings fixes.
- `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` for spawned turns, on both the proxy
  and the direct path, so the two agree for every backend shape.

### Fixed

- **`db_restore` accepted any gzip blob as the new database.** It decompressed,
  wrote the bytes over the live file and reconnected, with no check that the
  payload was even SQLite. Now the candidate must carry the SQLite magic header,
  pass `PRAGMA integrity_check`, and hold the `chats`/`messages`/`users` tables
  before the live file is touched. Around the swap: stale `-wal`/`-shm` sidecars
  are deleted (SQLite can otherwise replay the previous database's write-ahead
  log over the restored file), the reconnect goes through `init()` so additive
  migrations run, and `db_conn` is never left `None` on the failure path — which
  used to make every subsequent request 500 until restart.
- **The proxy truncated large turns.** `create_subprocess_exec` was left on
  asyncio's default 64 KiB `StreamReader` limit while `relay_stdout` iterates
  line by line, so a single large stream-json frame — one big tool result is
  enough — raised `LimitOverrunError`. Now 16 MiB.
- **Proxy concurrency was uncapped.** The only limit lived in the runner's
  semaphore, which bounds one client; a restarted app, a second instance, or any
  other holder of the token could spawn Claude processes without limit. Now
  enforced at the proxy via `WC_PROXY_MAX_CONCURRENT` (default 4).
- **Runner log lines rendered as literal `%s`.** `_log` binds loguru, which
  formats with braces, so every `%`-style call emitted the format string verbatim
  and dropped the values. Eight call sites converted.

### Testing

- Four settings-validation tests put their assertion inside `except Exception`,
  so a handler that stopped rejecting bad input would raise nothing, skip the
  assertion and pass — vacuous exactly where a validation test matters. Rewritten
  with `assertRaises`.
- FTS index maintenance covered, including the incremental property, mutation-
  tested rather than assumed: three defects that all leave search results correct
  are now each caught.
- Layered coverage for model selection and backend routing, pinning a failure
  that production hit while every existing test passed — a fully and correctly
  configured machine still routed every turn to the official API because
  `provider` was left at its default of `proxy`, and the resulting error pointed
  at the model rather than at the routing.

---

## [0.5.0] — 2026-08-29

Version 0.4.0 was skipped.

### Fixed

- **Every AI machine edit returned 400.** The settings form still sent `port`,
  `base_url` and `description` after those inputs were removed from the markup,
  and `handle_machine_patch` rejects the whole body when any key falls outside
  its allowlist. The reason was invisible because the API reports failures as
  `{"error": ...}` while the client read `data.detail`, so the UI showed a bare
  `(400 Bad Request)`.
- **Every turn failed with "Connection lost during streaming".**
  `_load_settings_from_db()` runs before `config.validate()`, so the app resolved
  `PROXY_TOKEN` from the database while `launch.sh` generated a fresh random one
  on each launch and never persisted it — the two could only agree by accident.
  `launch.sh` now resolves the token DB → file → generate and persists it with
  `umask 077`.
- **The configured model had been retired.** `claude-sonnet-4-20250514` was
  withdrawn on 2026-06-15 and was hardcoded in five places, including the
  `ai_machines` schema default that would have broken every fresh database. All
  defaults moved to `claude-sonnet-5`.
- **Database restore had never worked.** `POST /api/admin/import` returned 500
  unconditionally: `request.form()` requires `python-multipart`, which was
  neither installed nor listed. A name-based import scan cannot catch this class
  of runtime-only dependency.
- **Search never matched.** FTS5 selected a column the virtual table does not
  have. Indexed content is now prefixed with the chat title.
- **Admin export stalled every other request, including live SSE streams.**
  `db_backup()` ran `sqlite3.backup()`, the file read and the gzip pass directly
  on the event loop. Now off-loop via `asyncio.to_thread`.

### Security

- **No Content-Security-Policy had ever been served.** The header was gated on a
  per-request nonce that nothing set, so no policy shipped at all. Now emitted
  unconditionally with `script-src 'self'`.
- **Every authenticated account passed the admin gates.** `session_new`
  hardcoded `role: "admin"` and never read `users.role`, making the checks on
  `/api/admin/*` decorative. It now takes the role from the user record.
- **`PATCH /api/settings` was ungated** while writing the session secret, proxy
  token, model API key and `projects_root` — the sandbox boundary every
  `work_dir` is validated against. Admin gate plus `_validate_projects_root`.
- SSRF blocking for machine hosts; `COOKIE_ALLOW_INSECURE` defaults to `False`.
- `starlette` pinned explicitly rather than inherited through FastAPI, since
  `app.py` imports it directly.

---

## [0.3.0] — 2026-08-28

### Security

- CSRF validation middleware on all mutating endpoints.
- SSRF protection: private and reserved IPs blocked in the machine test and in
  settings.
- Command injection: a `--` separator added before user data in the subprocess
  argument list.
- Path traversal: `session_id` validated before filesystem use.
- CSP header with a per-request nonce on the login page; HSTS added.
- Boot secrets loaded from the database at startup.
- Password hash capped at 72 bytes (Argon2 entropy limit).
- Tightened model regex; fixed `base_url` scheme validation.

### Added

- **HTTPS on the Tailscale IP** using a Tailscale-managed CA certificate,
  auto-provisioned and stored under `~/.local/share/webconsole/certs/`.
  `launch.sh` detects the tailnet IP and domain and starts HTTPS.
- `ARCHITECTURE.md` — system context, request pipeline, concurrency model, data
  storage, and six sequence diagrams covering chat creation, blocking and
  streaming messages, CLI resume, settings with SSRF, and the login lifecycle.

---

## [0.2.0] — 2026-08-27

### Added

- Per-chat model persistence, with transcript-backed model discovery: the
  Claude JSONL transcripts are scanned for the last non-synthetic model of a CLI
  session, cached in process by session id to avoid rescanning.
- Persistent `settings` table (`ai_machine_host`, `session_ttl`, `turn_timeout`,
  `prompt_max`) with authenticated `GET`/`PATCH /api/settings`. The host
  validator accepts any valid hostname or IP, including IPv6, and rejects URLs,
  paths and embedded ports.
- Full AI machine management: CRUD, active-machine selection, model presets.
- Model badge in the workspace strip and the chat sidebar; Settings dialog with
  Machines / Models / App panels.
- 40 tests covering model extraction, settings CRUD, host validation and frame
  normalisation.

---

## [0.1.0] — 2026-08-27

### Added

- First public release: a self-hosted, mobile-first web front-end for the local
  Claude Code CLI. FastAPI over a vanilla-JS SPA, conversations in SQLite,
  Claude Code invoked with `--output-format stream-json`.
- Proxy authentication and a login rate-limit fix.
- Conversation management and a modular frontend; desktop sidebar and touch
  targets corrected.
- Supply-chain and secrets controls: pip-audit, Bandit, Ruff, Gitleaks and Trivy
  scans in CI, Dependabot for Python/Actions/Docker, a local Gitleaks pre-push
  hook, and a documented vulnerability-response process.

[Unreleased]: https://github.com/tarrinho/claude_code_webclient/compare/e137272...HEAD
[0.8.0]: https://github.com/tarrinho/claude_code_webclient/compare/3bd80aa...e137272
[0.7.2]: https://github.com/tarrinho/claude_code_webclient/compare/e42a3f8...3bd80aa
[0.7.0]: https://github.com/tarrinho/claude_code_webclient/compare/149aa79...e42a3f8
[0.6.1]: https://github.com/tarrinho/claude_code_webclient/compare/3137ca2...149aa79
[0.6.0]: https://github.com/tarrinho/claude_code_webclient/compare/42a4df7...3137ca2
[0.5.1]: https://github.com/tarrinho/claude_code_webclient/compare/71de7dc...42a4df7
[0.5.0]: https://github.com/tarrinho/claude_code_webclient/compare/058cc2c...71de7dc
[0.3.0]: https://github.com/tarrinho/claude_code_webclient/compare/ed21559...058cc2c
[0.2.0]: https://github.com/tarrinho/claude_code_webclient/compare/aa40bc2...ed21559
[0.1.0]: https://github.com/tarrinho/claude_code_webclient/commit/aa40bc2
