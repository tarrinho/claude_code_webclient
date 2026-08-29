# WebConsole — Threat Model

Date: 2026-08-29 · Version analysed: 0.7.2 (`c6a9bd7`, plus uncommitted work in
`prompts.py` and `app.py`) · Author: Pedro Tarrinho

This is an analysis, not a policy. `SECURITY.md` states the rules for deploying
the thing; this document says what an attacker can do to it and how much of that
is already true on the machine it runs on today.

Findings marked **[verified]** were confirmed against the running deployment,
not inferred from source. Findings marked **[code]** are read off the source and
depend on a precondition that was not exercised.

---

## 1. Scope and method

Read in full: `app.py`, `auth.py`, `config.py`, `runner.py`, `claude_proxy.py`,
`prompts.py`, `launch.sh`, `start.sh`, `Caddyfile`, `requirements.txt`, and the
security-relevant halves of `db.py` and `transcripts.py`. Live state inspected:
the running uvicorn and proxy processes' environments, `~/.claude/sessions/`,
file modes on secrets at rest, `net.ipv4.ip_unprivileged_port_start`, and the
`/tmp` snapshot files.

Out of scope: the Claude Code CLI's own internals, the Anthropic API, Tailscale,
Docker packaging under `docker/`, and the front-end JavaScript beyond how it
reaches the API.

## 2. What the system is, in security terms

WebConsole is a web front end that spawns `claude -p
--dangerously-skip-permissions` on behalf of an authenticated user. That flag
removes tool gating entirely. **The application does not contain a sandbox and
does not attempt one.** Every control in it exists to answer one question —
"is this the operator?" — because once the answer is yes, arbitrary code runs as
`kali`.

That is stated plainly in `SECURITY.md` and it is the right framing. The
consequence for this document is that "remote code execution" is not a finding;
it is the product. The findings below are about the three things that *are*
still meaningful:

1. **Aim** — the console now writes into terminals. Sending the right keystroke
   to the wrong window is a new class of bug, and it is where the sharpest
   findings are.
2. **Confinement of the blast radius** — cwd roots, egress destinations,
   which secrets a compromised agent can read.
3. **The single-operator assumption**, which is load-bearing in at least five
   places and labelled in one.

### Trust boundaries

```
                    ┌──────────────────────── tailnet (Tailscale ACL) ─────┐
   browser ─TLS───▶ │  uvicorn :443  (user kali, in screen 2126909 wnd 0)  │
                    │    AuthMiddleware → CsrfMiddleware → handler         │
   ══════════════════════════ B1: authentication ═════════════════════════ │
                    │  app.py  ── SQLite (secrets in plaintext) ──────────  │
   ══════════════════════════ B2: process/privilege ══════════════════════ │
                    │  claude_proxy.py :9000 (token-auth, loopback)         │
                    │      └─▶ claude --dangerously-skip-permissions        │
   ══════════════════════════ B3: agent autonomy ═════════════════════════ │
                    │  the model's own decisions, driven by content it      │
                    │  reads: repos, web pages, transcripts, peer msgs      │
   ══════════════════════════ B4: terminal injection ═════════════════════ │
                    │  screen `stuff` / tmux `send-keys` into live windows  │
                    └──────────────────────────────────────────────────────┘
```

B4 is new as of this week and is the only boundary that runs *backwards* —
from the web tier into interactive sessions the web tier did not create and does
not own. It deserves the most scrutiny, and gets it in §5.

### Assets, in priority order

| Asset | Where it lives | Why it matters |
|---|---|---|
| Code execution as `kali` | every spawned CLI | the whole host, the tailnet, the user's SSH keys and git credentials |
| `WC_PROXY_TOKEN` | `.env` (0664), `data/proxy_token.txt` (0600), `settings` table, app + proxy env | one token = unlimited `--dangerously-skip-permissions` spawns |
| Per-machine `api_key` | `ai_machines.api_key`, plaintext | billable third-party credentials |
| Session cookies | memory + `sessions` table (sha256-keyed) | full app authority |
| Conversation history | `messages`, `~/.claude/projects/**/*.jsonl` | everything the operator has ever discussed with an agent |
| Terminal contents | `/tmp/wc-prompt-*.hardcopy` (0664) | whatever an agent last printed |

### Threat actors

- **A1 — Network attacker on the tailnet.** Cannot reach the app without a
  Tailscale ACL failure. Realistic if an ACL is widened or a tailnet node is
  compromised.
- **A2 — Authenticated non-admin user.** Does not exist today (single operator),
  but the code has a `role` column, admin gates, and owner scoping, so it is a
  designed-for actor. Several findings only bite here.
- **A3 — Another local user on the host.** Kali box, so plausibly none — but
  `/tmp` and `.env` modes make this actor cheap to serve.
- **A4 — Prompt injection reaching the agent.** *The most likely real attacker.*
  The agent reads repositories, web pages, transcripts of other sessions and
  cross-session peer messages, and it holds `--dangerously-skip-permissions`.
  Anything in those channels is an instruction from an untrusted party.
- **A5 — A malicious or compromised AI machine / gateway.** Configurable by any
  authenticated user, receives prompts and possibly API keys, and its responses
  are parsed by both normalisers and rendered in the browser.

---

## 3. Attack surface

**Unauthenticated:** `POST /login`, `GET /login`, `/assets/*` (also served
directly by Caddy, bypassing the app's middleware, when Caddy is in front).

**Authenticated, any role:** 34 endpoints. The ones that cross a boundary rather
than moving JSON around:

| Endpoint | Boundary it crosses |
|---|---|
| `POST /api/chats/{id}/messages`, `/stream` | spawns the CLI → B2, B3 |
| `POST /api/chats/{id}/question` | **writes keystrokes into a terminal → B4** |
| `GET /api/chats/{id}/file` | reads workspace files, serves inline |
| `POST /api/machines`, `PATCH /api/machines/{id}` | sets egress destination + credential |
| `POST /api/machines/{id}/test`, `GET /api/models` | server-side HTTP to a user-chosen URL |
| `POST /api/sessions/{id}/resume` | imports **any** session's transcript on the host |
| `GET /api/transcripts*`, `/api/supervisor` | reads `~/.claude/**` unscoped |
| `PATCH /api/settings` (admin) | rewrites `projects_root` — the sandbox root itself |
| `GET /api/admin/export` (admin) | downloads the DB, secrets included |
| `POST /api/admin/import` (admin) | replaces the authentication store |

**Non-HTTP:** `claude_proxy.py` on `127.0.0.1:9000`, token handshake, no
transport encryption — turn frames carrying an API key travel as plaintext JSON
over loopback.

---

## 4. STRIDE summary

| | Notable exposure |
|---|---|
| **S**poofing | Session ids are 32-byte random, sha256-keyed at rest, `compare_digest` on CSRF — solid. Weakness is *revocation*, not authentication (F-07). Proxy handshake uses `hmac.compare_digest` — correct. |
| **T**ampering | `~/.claude/sessions/*.json` is a trusted control file writable by any local process, including the agent (F-02). `/tmp` snapshots are pre-placeable (F-04). `db_restore` rewrites `users` (F-18). |
| **R**epudiation | Logging is genuinely good — every mutating handler logs actor and target. Gap: `send_text` deliveries and the identity of the *window* written to are not logged with the target's provenance. |
| **I**nformation disclosure | World-readable `.env` (F-05) and terminal dumps (F-04); secrets in an exportable DB (F-06); unscoped transcript reads (F-19); CLI stderr relayed to the browser (F-20). |
| **D**enial of service | No rate limiting outside `/login` (F-11); unbounded `_login_attempts` (F-12); the unscoped FTS pre-filter can build an oversized parameter list (F-10). Stream limits and semaphores are otherwise well placed. |
| **E**levation of privilege | The intended state is "authenticated ⇒ code execution". The unintended additions: keystrokes into a shell (F-01), agent-steered targeting (F-02), unconfined proxy cwd (F-03), egress redirection with credentials (F-08). |

---

## 5. Findings

### F-01 — Keystrokes can be aimed at the WebConsole's own terminal window **[verified preconditions]**

**Severity: Critical** (contingent — the vulnerable function is uncommitted)

`prompts.deliver_request()` / `send_text()` (working tree, 70 uncommitted lines)
type arbitrary text followed by Enter into a multiplexer window resolved by
`locate()`. `locate()` trusts `session_pid()`, which reads
`~/.claude/sessions/*.json` and believes the `pid` recorded there.

`db.write_claude_session_file()` writes `"pid": os.getpid()` — **the
WebConsole server's pid** — for every web-created session. Live state:

```
app pid 4046289    STY=2126909.pts-5.kali-2  WINDOW=0
cweb2 claude 3196923  STY=2126909.pts-5.kali-2   (same session, window 0)
```

So for a web-created session, `locate()` walks up from the *server's* process
and lands on the window the server was launched from. The keystroke path's core
guarantee — "the window comes from the process environment, so it is stated
rather than inferred" — holds only if the pid belongs to the session. Here it
belongs to the app.

`answer()` survives this because `find_target()` gates on
`looks_like_a_prompt()`. **`deliver_request()` does not call it at all.** If the
target window is sitting at a shell — the interactive agent exited, the window
was reused — the text is typed at `bash` and Enter runs it. An authenticated web
request becomes a shell command.

**Status: fixed in `80ba161`.** `session_pid()` now requires the pid to identify
as `claude` in `/proc` and to be neither this process nor any ancestor of it.
Refusing the whole ancestry — not just the exact pid — is what closes it, since
walking up from the server's pid is how the wrong window was reached. Verified
live: a file naming the calling process is refused even with `_is_claude` forced
true; a genuine peer session still resolves. Mutation-checked.

**One recommendation in the first draft of this document was wrong** and is
recorded here because it was acted on. I proposed refusing any target whose
`STY`/`WINDOW` matches the server's own, and called it "one comparison that
closes the case outright". It does not: an interactive agent runs *in* the
server's window on this deployment, so that check refuses a legitimate target
rather than an attack — it would have made the one session that motivated the
routing feature unreachable. The ancestry check already covers the mechanism.
`locate()` therefore reports `shares_server_window` and lets the caller decide.
The flag is relative to the calling process, so it reads `False` from a test
harness in another window and `True` from the server itself.

Still open: `deliver_request` does not gate on the window being at a prompt.
After discussion this is **correct** — a request routed to a live session is
precisely the case where the terminal is at its composer or busy, so gating on
`looks_like_a_prompt()` would refuse every legitimate delivery. Positive
identification of the process is the property that path needs, and it now has
it.

### F-02 — `~/.claude/sessions/` is an untrusted targeting oracle **[verified]**

**Severity: High**

Targeting is only as trustworthy as the file that maps session id → pid. That
file is writable by any process running as `kali`, which includes every agent
the console spawns with `--dangerously-skip-permissions`. A prompt-injected
agent (A4) can plant `{"sessionId": "<a chat's id>", "pid": <any pid>}` and
thereby choose which terminal window a later `answer()` or `send_text()` writes
into.

Not hypothetical that the directory is writable in practice: my own test suite
left `dead1.json`, `dead2.json` and `live1.json` in the real
`~/.claude/sessions/` — verified present. Tests writing into a directory the
security of a keystroke path depends on is its own problem; fix that too.

**Status: narrowed in `80ba161`, not closed — and it cannot be closed here.**
`session_pid()` now refuses a pid that is not `claude`, refuses our own
ancestry, and refuses a *contradiction*: two live pids claiming one session id
returns `None` rather than picking one, because choosing would let a planted
file win a race against the real entry by being read first.

A file naming a genuinely live, unrelated `claude` process still passes every
one of those checks. Corroborating against the kernel was attempted and does not
work: `claude` appends to its transcript and closes it, so no live process holds
an fd on the file that would identify it (checked on all five running sessions —
zero open transcript descriptors). `/proc` can say *what* a pid is; nothing on
disk can say which session it owns except the file being validated.

The honest conclusion is that F-02 is not solvable at this layer. While the
agents run under the same uid as the console, any file-based mapping is
agent-writable, and so is any database the console might keep instead. The real
fix is a privilege boundary — run spawned agents as a different uid — which is a
deployment change, not a code change. Until then this is narrowed and documented,
which is the accurate description of its state.

*Also still to do:* point the test suite at a temp directory. It currently
writes `dead1.json`, `dead2.json` and `live1.json` into the real
`~/.claude/sessions/`, which is the very directory whose trustworthiness this
finding is about.

### F-03 — Proxy cwd confinement is inert as deployed **[verified]**

**Severity: High**

`claude_proxy._safe_cwd()` confines the subprocess cwd only when a root is
configured:

```python
root = os.environ.get("WC_PROXY_ALLOWED_ROOT") or os.environ.get("WC_PROJECTS_ROOT", "")
...
if root:                      # ← no root ⇒ no confinement
```

The running proxy (pid 3913940) has exactly one `WC_*` variable in its
environment: `WC_PROXY_TOKEN`. Neither root is set. So the check is skipped and
any existing directory named by the client becomes the CLI's cwd — the docstring's
"confined to the allowed root" is not in force on this machine.

`runner.py` does validate `work_dir` against `PROJECTS_ROOT` before sending, so
the app is not the way in; the exposure is to anything else holding the token.

*Fix:* fail closed — refuse to start without a root, or default to the fallback
dir when none is configured. Export `WC_PROXY_ALLOWED_ROOT` from `launch.sh`,
which currently exports only the token.

### F-04 — Terminal snapshots are world-readable at predictable paths **[verified]**

**Severity: High**

`prompts.screen_snapshot()` dumps a window to
`/tmp/wc-prompt-<STY>-<window>.hardcopy`. Live:

```
-rw-rw-r-- kali kali 4638 /tmp/wc-prompt-2105607.pts-6.kali-2-0.hardcopy
-rw-rw-r-- kali kali 3363 /tmp/wc-prompt-2105607.pts-6.kali-2-2.hardcopy
-rw-rw-r-- kali kali 5308 /tmp/wc-prompt-2126909.pts-5.kali-2-0.hardcopy
```

Mode 0664 (umask 0002), name fully predictable, contents = whatever an agent had
on screen. Two distinct problems:

1. **Disclosure to A3.** Any local user reads them. Screen contents routinely
   include file excerpts, command output and anything the agent printed.
2. **Pre-placement.** The code `unlink`s the path and lets `screen` create it.
   A local user who wins that race with a symlink can (a) have `kali`'s `screen`
   overwrite a `kali`-owned file, and (b) *supply the screen contents the app
   then parses* — `visible_options()` and `selected_index()` read that file, so
   the attacker chooses which option the operator appears to be selecting.

*Fix:* `tempfile.mkdtemp(mode=0o700)` per call, or a fixed
`~/.local/state/webconsole/` directory created 0700; unlink after reading; never
a predictable name in a sticky world-writable directory.

### F-05 — `.env` is world-readable **[verified]**

**Severity: High**

`stat` reports `-rw-rw-r-- .env`. It holds the deployment's secrets. Contents
were deliberately not read or reproduced.

*Fix:* `chmod 600 .env`, and have `launch.sh` refuse to start if the mode is
group- or world-readable. Because this is a credential file that has been
world-readable for an unknown period on a host that may not be single-user,
treat the exposure window as unknown and rotate `WC_PROXY_TOKEN`, the admin
password and any machine API keys. If this host is Celfocus-managed or the
credentials are shared, follow the incident process —
information.security@celfocus.com.

### F-06 — Secrets sit in plaintext inside a downloadable database **[code]**

**Severity: Medium** (High if A2 ever exists)

`settings` holds `session_secret`, `proxy_token` and `model_api_key`;
`ai_machines.api_key` holds per-machine credentials. All plaintext.
`GET /api/admin/export` gzips the whole file to the client.

`auth.py` hashes session ids with the explicit justification that "the database
is downloadable through `/api/admin/export`". That reasoning is correct and
applies just as strongly to the credentials stored beside them — one class of
secret got the treatment and the others did not.

*Fix:* encrypt secret-bearing columns with a key held outside the DB (this is
the job `WC_SESSION_SECRET` currently does not do — see F-13), or exclude those
tables from export and document that a backup is not a full restore.

### F-07 — Sessions are never revalidated against the database **[code]**

**Severity: Medium**

`auth.py`'s module docstring: *"Sessions: server-side in-memory, each
revalidated against DB on every request."* `session_get()` reads the in-memory
dict, checks expiry and idle, and returns. It never touches the database.

So deleting a user, demoting an admin, or changing a password does not end their
live session. It survives up to `SESSION_TTL_S` (2 h) or 30 minutes idle. There
is no revocation path at all short of restarting the process — and since 0.7.x
persists sessions to SQLite and reloads them at boot (`load_sessions`), *even a
restart no longer revokes them.*

The docstring is the dangerous part: it documents a control that does not exist,
so a reviewer checking "can I revoke access?" gets a yes from the comments.

*Fix:* re-read the user's row (or a cheap `users.updated_at` epoch) inside
`session_get`, or add an explicit revocation table. Failing that, correct the
docstring — a known gap beats a false assurance.

### F-08 — `base_url` is an unvalidated egress destination that carries the API key **[code]**

**Severity: Medium**

`_validate_base_url()` checks length and that the URL parses as http(s). It
never resolves the host and never applies `_BLOCKED_NETS`. Only the separate
`host` column goes through `_validate_host()`.

`PATCH /api/machines/{id}` accepts `{"base_url": "http://169.254.169.254/"}`
alone. `runner.get_backend()` then hands it to the CLI as `ANTHROPIC_BASE_URL`
together with `ANTHROPIC_API_KEY`, so subsequent turns post the prompt **and the
credential** to that URL. Not admin-gated: any authenticated user, on machines
they own.

*Fix:* run `base_url` through the same resolve-and-blocklist path as `host`, at
both create and patch, and re-check at turn time rather than only at persist
time.

### F-09 — SSRF checks are resolve-then-connect, and blind to redirects **[code]**

**Severity: Medium**

Two independent bypasses of the same control:

1. **TOCTOU / DNS rebinding.** `_resolve_host()` returns a validated IP, and its
   docstring says the IP is returned "so that `asyncio.open_connection` can
   connect directly (avoiding a second DNS lookup)". `_test_anthropic_endpoint`
   discards it and calls `_probe_anthropic(url, ...)` with the original
   *hostname*, so DNS resolves a second time. A record that alternates between a
   public IP and `169.254.169.254` passes the check and connects elsewhere.
2. **Redirects.** `urllib.request.urlopen` follows 30x by default and the
   redirect target is never validated. A permitted host redirects to anything.

Worth being clear about the actual stakes: `_parse_allow_nets` deliberately
allows loopback, RFC1918 and CGNAT, because an AI machine is *meant* to be on
the operator's own network. That is a sound decision. It means the blocklist's
only remaining teeth are link-local (cloud metadata) and reserved ranges — and
these two bypasses are precisely what defeats those.

*Fix:* connect to the validated IP with an explicit `Host` header, or use a
custom opener with redirects disabled and re-validate every hop.

### F-10 — The full-text search pre-filter is not owner-scoped **[code]**

**Severity: Medium**

```python
"SELECT rowid FROM messages_fts WHERE content MATCH ?"   # every owner's messages
...
f"JOIN messages m ON ... m.id IN ({_placeholders}) WHERE c.owner_id = ?"
```

Results are correctly scoped by the second query, so there is no direct data
leak. But the id list is built from *everyone's* matches:

- A common term produces a parameter list that can exceed
  `SQLITE_MAX_VARIABLE_NUMBER`, and the failure is swallowed by
  `except Exception` — search silently returns nothing for the operator.
- It is a blind oracle: latency and failure behaviour vary with whether *any*
  user's messages match a term, which is a cross-tenant inference channel the
  moment A2 exists.

User input is also passed straight through as an FTS5 *query expression* (not
SQL — parameterisation is correct). Malformed expressions raise and are
swallowed, so this is a robustness issue rather than injection.

*Fix:* join `messages` → `chats` and filter `owner_id` inside the FTS query, or
`LIMIT` the rowid set.

### F-11 — No rate limiting outside `/login` **[verified]**

**Severity: Medium**

`login_attempt_flood` has exactly one call site. Nothing else is throttled —
including `POST /api/chats/{id}/stream`, which spawns a CLI process and spends
money. `MAX_CONCURRENT=3` and the proxy's `_MAX_CONCURRENT=4` bound
*concurrency*, not rate or spend: a loop of sequential turns is unbounded.

The Usage tab now makes the cost visible after the fact. Nothing caps it.

*Fix:* a per-user token bucket on turn-spawning endpoints, and a configurable
daily spend or turn ceiling.

### F-12 — Unbounded in-memory dictionaries **[code]**

**Severity: Low**

`_login_attempts` gains a list per source IP and is pruned only on that IP's
next attempt or on success — an attacker rotating source addresses grows it
without limit. `_persisted_last` is pruned only via `_forget`. `_sessions` is
correctly capped by `SESSION_MAX`.

### F-13 — Dead controls that read as live ones **[verified]**

**Severity: Low, but high-consequence-if-trusted**

- **`WC_SESSION_SECRET` is never used for anything.** It is required at boot,
  length-validated, stored in the DB, and settable through the admin API — and
  the only references are the validator and the settings plumbing. No signing,
  no encryption, no derivation. A reader reasonably assumes sessions are signed
  with it. They are not; they are random tokens in a dict.
- **`csrf_generate` / `csrf_consume` / `_csrf_store` are unreferenced** outside
  tests. Real CSRF validation happens in `_csrf_valid` against the session's
  own token, which is correct — but the vestigial store invites someone to wire
  it up and reintroduce the "any live token is accepted" bug the git history
  shows was already fixed once.

The honest fix is either to give `WC_SESSION_SECRET` a job (F-06 has one waiting)
or to delete it and stop requiring it. Both beat a validated secret that does
nothing.

### F-14 — Proxy-header trust and the shipped `Caddyfile` disagree **[code]**

**Severity: Low**

`_client_ip()` is right: forwarded headers are honoured only from
`WC_TRUSTED_PROXIES`, which defaults to empty, and the docstring explains
exactly why. Meanwhile `Caddyfile` sets `header_up X-Real-IP` and `launch.sh`
binds uvicorn directly to `:443` — so the Caddy config is drift, not
deployment.

If Caddy is ever restored without setting `WC_TRUSTED_PROXIES`, every request
attributes to `127.0.0.1`: the login limiter becomes one global bucket and ten
failures lock out every user. If it is restored *with* the variable set, note
that Caddy also serves `/assets/*` itself, bypassing `SecurityMiddleware` — so
those responses carry no CSP.

### F-15 — Unprivileged low-port binding is enabled host-wide **[verified]**

**Severity: Low** (host hygiene, wider than this app)

`net.ipv4.ip_unprivileged_port_start = 0`, which is how uvicorn binds `:443` as
`kali`. It also lets *any* unprivileged local process bind *any* low port — for
instance squatting `:443` after a restart, or standing up a fake `:53`.

*Fix:* prefer `CAP_NET_BIND_SERVICE` on the interpreter, or a reverse proxy
holding the port, over the global sysctl.

### F-16 — Inline SVG from the app origin **[code]**

**Severity: Low** (defence in depth)

`handle_chat_file` serves `.svg` as `image/svg+xml` with
`Content-Disposition: inline`. SVG is an XML document that can carry script.
The global CSP (`script-src 'self'`, no `unsafe-inline`) blocks it today, so this
is not live XSS.

It is worth fixing anyway because of *who writes the file*: an agent running
with `--dangerously-skip-permissions`, i.e. content reachable by A4. A single
CSP directive is the only thing between prompt injection and same-origin script
execution.

*Fix:* serve SVG as `attachment`, or add a per-response
`Content-Security-Policy: sandbox` / `default-src 'none'`, or drop `.svg` from
`_IMAGE_TYPES`.

### F-17 — Proxy children inherit the full server environment **[verified, limited]**

**Severity: Low**

`claude_proxy._backend_env()` starts from `dict(os.environ)`, so the CLI —
tool-unrestricted — can read the proxy's environment from its own
`/proc/self/environ`. `runner._build_env()` allowlists six variables on the
direct path; the proxy path does not.

Blast radius is currently small because the running proxy's environment holds
only `WC_PROXY_TOKEN` (verified). But that token is exactly what gates unlimited
spawning, and combined with F-03 it is unconfined spawning.

*Fix:* allowlist on the proxy path too, and drop `WC_PROXY_TOKEN` from the child.

### F-18 — `db_restore` replaces the authentication store **[code]**

**Severity: Low today** (admin→admin), **noted for A2**

Validation is `gzip` + SQLite magic + `PRAGMA integrity_check` + "has `chats`,
`messages`, `users`". A structurally valid database passes. Restoring it
replaces `users` — every password hash and role — and `settings`, including
`proxy_token`. Default upload cap is 500 MB.

The swap is atomic and reconnects through `init()` so migrations apply; that part
is well built. The gap is that nothing pins the restored database to this
deployment's identity.

### F-19 — No tenancy on the CLI-session surface **[code]**

**Severity: Low today**, **High the moment a second account exists**

`/api/sessions`, `POST /api/sessions/{id}/resume`, `/api/transcripts`,
`/api/transcripts/{id}`, `/api/supervisor` and `_import_cli_usage` all read
`~/.claude/**` globally. Any authenticated account can enumerate every Claude
Code session on the host and import any transcript in full into its own chat.

`CLI_USAGE_OWNER = "admin"` carries an admirable comment — *"Correct only while
this is a single-operator console — the day a second account exists, this line
is the bug."* The same caveat governs all six of these surfaces and is written
down at exactly one of them.

*Fix:* when a second account becomes real, gate this surface on `role == admin`
at minimum. Until then, replicate that comment at each site so the assumption is
discoverable rather than folkloric.

### F-20 — CLI stderr is relayed to the browser **[code]**

**Severity: Low**

`_do_direct_stream` yields `stderr[-2000:]`, and `claude_proxy` sends the same.
The SSE handler sanitises its *own* exceptions to fixed strings (`_SSE_INTERNAL`
et al. — good) but passes these through. stderr from a tool-unrestricted agent
can contain absolute paths, environment fragments and command output.

---

## 6. Realistic attack chains

**C1 — Prompt injection to shell command, via the keystroke path.**
A4 lands instructions in a repository or web page an agent reads → agent writes
`~/.claude/sessions/x.json` mapping a web chat's session id to a pid whose
window it chooses (F-02); alternatively it needs to do nothing at all, because
`write_claude_session_file` already points web sessions at the server's own pid
in a window shared with an interactive agent (F-01) → the operator sends any
request from the web UI → `deliver_request` types it into that window with no
prompt check → if the window is at a shell, it executes.
**Blocked by:** verifying pid identity and gating `deliver_request` on
`looks_like_a_prompt`.

**C2 — Local user reads the operator's screens, then steers an answer.**
A3 reads `/tmp/wc-prompt-*.hardcopy` for terminal contents (F-04), then
pre-places a symlink at the predictable next path so the app parses
attacker-authored "screen contents" — choosing which option the operator's
click appears to select.
**Blocked by:** 0700 private directory, unpredictable name, unlink after read.

**C3 — Credential exfiltration without admin.**
A2 (or A4 through the UI) patches a machine's `base_url` to a host it controls
(F-08) → turns post the prompt and `ANTHROPIC_API_KEY` there. No admin gate, no
SSRF check on that field, and cost accounting will label the rows
`anthropic-compatible` rather than flag them.
**Blocked by:** validating `base_url` on the same path as `host`.

**C4 — Secret harvest via a legitimate backup.**
Any admin session (or CSRF/XSS on an admin) calls `GET /api/admin/export` →
`proxy_token`, `session_secret` and every machine `api_key` in plaintext (F-06).
The proxy token alone yields unlimited `--dangerously-skip-permissions` spawns,
with cwd unconfined as deployed (F-03).
**Blocked by:** encrypting those columns or excluding them from export.

**C5 — Quiet financial drain.**
A2 loops `POST /api/chats/{id}/stream` (F-11). Concurrency is capped at 3; rate
and spend are not. Visible in the Usage tab afterwards; not preventable by
anything in the code.

---

## 7. Risk accepted by design — and correctly so

Listed to keep them out of the findings, not because they are unimportant:

- **`--dangerously-skip-permissions`.** This is the product. It reduces the
  security model to authenticating the operator and keeping the tailnet shut.
- **Prompt injection as the dominant threat.** No sanitisation of agent-read
  content is proposed, because none is possible at this layer. What *is*
  actionable is not letting injected content steer privileged mechanisms —
  which is what F-01, F-02 and F-16 are about.
- **A tailnet-only, single-operator deployment.** Sound. The exposure is that
  five surfaces depend on it and one says so.

Controls worth naming as genuinely well built: Argon2id with a 72-byte cap and
a documented rationale; sha256-keyed session storage; `compare_digest` on both
CSRF and the proxy handshake; the session-bound CSRF fix; `PUT` added to
`_MUTATING`; unconditional CSP and HSTS; `is_relative_to` after `resolve()` for
`work_dir`, workspace files and `projects_root`; `--` argv sentinels on every
CLI spawn; API keys passed by environment and never argv, with
`/proc/<pid>/cmdline` cited as the reason; `_STREAM_LIMIT`; both semaphores;
pinned dependencies with CI advisory scanning. The commentary throughout records
*why* each control exists and what broke without it, which is why this review
could be specific rather than generic.

---

## 8. Recommendations, in order

**Done:**
1. ~~F-01~~ — fixed in `80ba161` (pid identity + ancestry refusal). The
   `STY`/`WINDOW` veto in the original recommendation was withdrawn as wrong;
   see the finding.
2. F-02 — narrowed in `80ba161` as far as this layer allows. Residual accepted
   and documented; the only real fix is a uid boundary.
3. ~~F-05~~ — `.env` set to 0600. **Rotate the credentials it held**; the
   exposure window is unknown. Still to do: refuse to boot on a loose mode.

**Do this week:**
4. F-04 — private 0700 snapshot directory, unpredictable name, unlink after read.
5. F-03 — fail closed without an allowed root; export it from `launch.sh`.
6. F-08 / F-09 — validate `base_url` like `host`; connect to the validated IP;
   disable redirect following.
7. F-02 follow-up — point the test suite at a temp `~/.claude/sessions`.

**Do next:**
8. F-07 — revalidate against the DB, or correct the docstring.
9. F-06 — encrypt secret columns (gives `WC_SESSION_SECRET` a real job) or
   exclude them from export.
10. F-11 — per-user turn rate limit and a spend ceiling.
11. F-10 — scope the FTS pre-filter by owner.
12. F-13 — either use `WC_SESSION_SECRET` or stop requiring it; delete
    `_csrf_store`.
13. F-16, F-17, F-20 — attachment/sandbox for SVG; allowlist the proxy child's
    env; stop relaying stderr.
14. F-19 — replicate the single-operator caveat at all six sites now; gate on
    admin before a second account exists.

## 9. Not assessed

Front-end DOM-XSS paths through `transcript.js` / `conversation.js` rendering of
agent-authored content (worth a dedicated pass, since that content is A4-reachable
and F-16 shows CSP is the only backstop); the `docker/` packaging; Tailscale ACL
configuration; the CLI's own handling of a hostile gateway response;
`test_qa_*.py` coverage of the security controls themselves.
