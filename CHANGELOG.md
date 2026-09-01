# Changelog

All notable changes to WebConsole, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
version string lives in `config.VERSION` as `WebConsole_<semver>`.

This file is reconstructed from the git history and is authoritative from 0.1.0
onward. Two version numbers — **0.4.0** and **0.7.1** — never reached a commit
on `main`, but both existed as real work and both shipped, folded into the next
release. They have sections below rather than being written off as gaps, because
"skipped" would have been wrong: the work is in the product, it just never wore
its own number.

Entries describe what changed for someone using or operating the console. Where
a fix is worth understanding rather than merely noting, the cause is stated —
several of these were invisible from the outside and would otherwise look like
churn.

---

## [Unreleased]

### Added

- **API tokens, so a script has a supported way in.** `Authorization: Bearer
  <token>` or `X-API-Token: <token>` authenticates any request, carrying the
  token owner's identity and role. `bin/wc-token.py` mints, lists and revokes
  them from the shell — needed because the HTTP routes require a login, which is
  exactly what an operator setting a machine up does not have.

  This is the other half of removing the `/dev/*` exemption below. The exemption
  survived as long as it did because there was no supported way for a caller
  without a browser to authenticate, so an unsupported one kept being invented;
  removing the hole without providing the door would have invited the next one.

  Details that are decisions rather than defaults:

  - Only a sha256 hash is stored. A database backup is therefore not a set of
    working keys, and a lost token is replaced rather than recovered. sha256 and
    not argon2 because the secret is 256 random bits — there is nothing to slow
    down — and this runs on every authenticated request.
  - **Token requests skip CSRF; cookie requests do not.** A browser never
    attaches an `Authorization` header on its own, so there is no ambient
    credential to forge. The exemption keys on what the auth middleware
    *accepted*, not on whether a token header is present — keying on the header
    would let any unauthenticated request switch CSRF off by sending an invented
    token, which is the same shape of convenience-becomes-bypass as the `/dev/`
    prefix. Cookie authentication also wins when both are presented, so a token
    leaked into page JavaScript cannot disable CSRF for that session.
  - A token cannot create another token, so one leaked credential cannot become
    an unrevocable supply. It can revoke itself, because needing a browser to
    retire a credential you think is loose is the wrong way round.
  - `last_used_at` is written at most once a minute per token rather than on
    every request: a row update on the hottest path in the server is the write
    pressure that produced the site-wide `database is locked` once already.
  - No expiry by default, capped at a year when one is requested. A cron job
    should not stop working at 3am because nobody renewed it.

- **A supported way to ask whether a session's task has finished.**
  `transcripts.turn_concluded(session_id)` reports the newest turn's
  `stop_reason` and whether a prompt arrived after it:

      concluded  ⟺  stop_reason == "end_turn"  AND  no prompt after it

  Both halves are needed. A session that finished a turn and was then given more
  work still carries `end_turn` as its newest `stop_reason` — observed live, on a
  session that flipped from idle to busy between two reads — so the stop reason
  alone reports a working agent as finished.

  A bounded 64 KB tail read, matching `last_error`'s budget: transcripts here run
  10–18 MB over 6,000–10,000 lines, and the newest `stop_reason` sat within the
  last 9 lines and 16 KB of every one measured. Reading only the last few
  *records* is not enough — one live session's last six were all metadata
  (`agent-name`, `mode`, `permission-mode`, `atis-latch`) with no assistant
  record among them.

  It corroborates the session registry's `status` field rather than replacing
  it. The two agreed on every live session tested. Deliberately **not** built on
  the `notify_idle` peer feature and the per-session socket at
  `/run/user/1000/cc-socks/<pid>.sock`: that is the push version of the same
  fact, but it is Claude Code's private protocol with no CLI surface, so it
  would break silently on a CLI update.

### Fixed

- **A finished agent is no longer reported as one blocked on a question.** The
  supervisor tested `status != "busy"`, and Claude Code 2.1.252 writes three
  values into `~/.claude/sessions/<pid>.json`, not one:

      busy     working
      idle     nothing further to do — the task concluded
      waiting  blocked, a person is needed

  So `idle` and `waiting` were treated identically: every agent that *finished*
  joined the waiting feed with `reason: "asks"`, its closing sentence presented
  as the question it was supposedly asking — an ask the code had no evidence
  for, since it came from the status field and not from reading anything. At the
  moment of the fix, two of the machine's live sessions were non-busy and one of
  them was merely idle, so half the badge was rows that needed nobody. The badge
  is worth having only while every row in it is real.

  `idle` now falls through to the routine-output path, where being read retires
  it; `waiting` still requires an explicit dismissal, because reading a question
  does not answer it. An **unrecognised** status is treated as blocked rather
  than finished: over-reporting costs a dismissal, under-reporting leaves an
  agent stuck with nobody told.

  Fixed in both places that made the comparison — the linked-conversation
  branch of `classify_chat` and the bare CLI-session path, whose comment stated
  the belief outright ("Any non-busy value means it has stopped and is waiting
  on a human").

- **Two comments that had quietly become false.** `db.read_claude_sessions`
  documented the status field as "Observed value is `busy`" — the value the
  callers above were written against — and a note in `classify_chat` claimed
  `statusUpdatedAt` was absent on "every non-busy session on this machine",
  which is no longer true of any of them. Both were accurate when written; the
  CLI moved and nothing re-read them. Corrected rather than deleted, since the
  fallback the second one justifies is still worth keeping for older builds.

### Security

- **Any account could read any supervisor's tasks and messages.**
  `GET /api/supervisors/{id}/tasks` and `.../messages` returned another
  account's task titles, descriptions and results — agent output — and its whole
  message history, to any authenticated caller who knew or guessed a supervisor
  id. Confirmed by exploit before it was fixed: one user logged in, requested
  another's supervisor by id, and got HTTP 200 with the contents.

  The cause is worth stating precisely, because the code read correctly.
  `db.supervisor_tasks_get(supervisor_id, owner_id)` and
  `db.supervisor_messages_get(...)` both **accepted `owner_id` and never used
  it**; the SQL filtered on `supervisor_id` alone. Every call site passed the
  argument, so every call site looked scoped, and a reviewer checking the
  handler would see it and stop. An AST sweep found five functions in that
  state — the two reachable ones plus a third read, a *write*, and one with no
  callers. All five now scope in SQL, at the data layer rather than in the
  handlers, because the handlers are where it was already missing.

  Two more things fell out of the same functions. `after_id` was accepted and
  ignored, with the `if after_id is not None` branch holding two
  byte-identical bodies — so the supervisor's SSE poller re-sent the same first
  hundred messages for ever, and the dead branch is what made it look
  deliberate. And the SSE handler had two further inline copies of the messages
  query, neither owner-filtered, safe only by virtue of the ownership check
  above them; both now call the scoped function.

  The regression test that matters is not the two functions but the invariant:
  no `db.py` function may accept `owner_id` and ignore it. See
  `tests/test_qa_supervisor_security.py` and `docs/threat-model.md` F-21.

- **A plan could choose the argv of the Claude Code child process.** A plan
  task's model is extracted with `[:(\S+)]` — any non-whitespace run — and
  becomes the argument to `--model`. A plan reading
  `[:--mcp-config=/tmp/evil.json]` therefore handed an attacker-chosen argv
  token to a subprocess that already runs with
  `--dangerously-skip-permissions`. The route in is prompt injection into
  whatever the planner was reading, which the threat model already treats as
  reachable.

  The ordinary path had it too: the model pattern was
  `^[A-Za-z0-9_.:/\[\]-]+$`, which accepts `-p`, `--model` and
  `-dangerously-skip-permissions`, all settable through
  `PATCH /api/chats/{id}`.

  This is argument injection, not command injection — there is no shell — and
  whether the CLI mis-parses a flag-shaped option value is the CLI's business.
  That is the point: `runner._build_cmd_direct` already protects the *prompt*
  from exactly this by putting it after a `--` sentinel "where a leading dash
  cannot be mistaken for a CLI flag", and the model had no equivalent.
  `config.valid_model_id()` now requires an alphanumeric first character and
  caps the length, from one definition shared by `app.py` and `supervisor.py`.
  A rejected plan model logs and falls back to the backend's choice, which is
  what a plan with no `[:model]` already gets. `claude-opus-5[1m]` still
  validates — the CLI documents that suffix, and a fix that broke it would have
  broken the feature it protects. `docs/threat-model.md` F-22.

- **`docs/threat-model.md` now covers the surfaces it said it did not.** The
  document analysed 0.7.2 and carried a note naming three uncovered surfaces:
  the supervisor orchestration API, the host statistics endpoints and the
  supervisor SSE stream. §5a covers those plus the two added since — API tokens
  and the question-dismiss route — as F-21 to F-24. F-23 (host facts readable
  by any authenticated account, no admin gate) and F-24 (tokens outside the
  login rate limiter; the dismiss route as a new sink for F-02's targeting
  oracle) are recorded open rather than fixed: the first is a product decision
  about who may see host statistics, and the second needs a rate-limiting
  design rather than a patch.

### Removed

- **`DevAuthSkipTests`.** All three assertions defended the `/dev/*` exemption,
  including two that required an endpoint minting credential-free admin sessions
  to be present at HEAD. Two of the three could not fail: each wrapped its own
  assertion in `except Exception: self.skipTest("not in a git repo")`, and
  `AssertionError` is an `Exception`, so the removal they existed to catch
  surfaced as two skips blaming git — inside a git repository.
  `tests/test_qa_api_tokens.py` asserts the opposite: nothing is exempt, and a
  cookieless caller uses a token.

## [0.9.3] — 2026-09-01

> The bump this section was waiting on. It sat as `[Unreleased]` for a day
> because the version is stated in five files besides `config.py` and two of
> them held another session's uncommitted work, so a partial bump would have
> landed the half-applied state the version-consistency tests exist to catch.
> All six were moved together this time and `tests/test_qa_version_consistency.py`
> was run against the result. `docs/threat-model.md` still reads 0.9.2 on
> purpose: it records which build was security-analysed, and rewriting it would
> claim an analysis nobody performed.

### Added

- **Supervisor pause / resume.** A supervisor that is planning or running can be
  put on hold and resumed later. The button sits in the chat panel header; it
  shows pause while running and play while paused. The engine respects a
  ``_wait_if_paused`` guard so it stops spinning but still persists progress
  every two seconds so the UI does not look stale.

- **Recency sort for the supervisor list.** A toggle button (``⇅``) cycles
  between newest-first and last-active, so recently-used supervisors stay at
  the top. The choice persists in ``localStorage`` under ``wc_supervisor_sort``.

- **Member heartbeat timestamps.** Each member row now shows when it last
  produced output, so a row that says "working" is distinguished from one that
  has been stuck for hours.

- **Task dependency hints.** When a plan task carries a ``depends_on`` list,
  the task tree shows ``depends on: <task>`` below the title.

- **Auto-grow composer.** The prompt textarea starts at 38 px and expands up to
  200 px as the user types, so long prompts fit without swallowing the chat.

- **Supervisor list shows last-activity time.** When a supervisor's
  ``updated_at`` differs from ``created_at`` the list item renders
  ``· 3 days ago`` alongside the status badge.

- ~~**The auth middleware now skips ``/dev/*`` routes.** They are intentionally
  public development endpoints.~~ **Withdrawn — this was never a feature.** The
  exemption was one session's uncommitted debug scaffolding, swept into a commit
  by a whole-file `git add`, and this entry recorded a later reader's reasonable
  guess that it had been deliberate. It was not, and the sentence above states
  the opposite of the truth about an authentication boundary. Struck rather than
  deleted so the misreading stays visible; see **Security** below for what
  actually happened and `rules.md` §16 registry #55. `/dev/*` is **not** public.

- **A question can be declined instead of answered.** Every control in the
  question bar answered the question, so a question that did not deserve an
  answer had two ways out and both were bad: pick something the user does not
  mean, which the session then acts on, or leave the prompt blocking that
  session until somebody walks to the terminal. A *Don't answer* control now
  delivers Escape — which is what the prompt itself offers — through
  ``DELETE /api/chats/{id}/question``.

  Three details are load-bearing rather than polish. An unreadable terminal is
  reported as unknown, not as closed: ``looks_like_a_prompt("")`` is false, so
  the obvious implementation claims success exactly when it has gone blind, and
  the session would sit blocked behind a UI that had stopped mentioning it. The
  response reports *delivered* separately from *ok*, because only one of the two
  failures may be retried — a key the terminal refused can be sent again, while
  one that was accepted and left the prompt open must not be, since the second
  Escape reaches whatever the session moved on to. And a question the user
  declined stays gone: cancelling a prompt need not write anything to the
  transcript, so the endpoint may keep reporting it as pending for ever, and
  without client state the bar returned four seconds after being dismissed.

  Where the terminal cannot be reached at all the control reads *Hide* and
  sends nothing, because calling it "Don't answer" there would promise a
  session had been let go while it is still sitting on the prompt.

- **Five supervisor UX affordances.** Keyboard shortcuts (`Ctrl`/`Alt`+`Enter`
  to send, `Ctrl`+`N` for a new supervisor, `Escape` to close a banner, `1`–`4`
  to focus a panel), expandable task rows that show a task's result inline
  instead of only in the right panel, an unread badge in the top bar, a pulse on
  a supervisor's status badge when its status changes, and a goal banner that
  shrinks to a slim strip once streamed output pushes it above the fold, with an
  arrow to expand it again.

  Four of the five needed a second pass, and all four first cuts were the kind
  that read correctly and do not work. The bare `1`–`4` keys had no
  typing guard, so every digit typed into the composer threw focus at a panel —
  the composer could not be used for a prompt containing a number, which is
  worse than having no shortcut. Those keys also called `.focus()` on plain
  `<div>`s, a silent no-op without `tabindex`, so three of the four did nothing
  even outside the composer. The restore arrow cleared the shrink flag but not
  the reason for it, so the next streamed message re-shrank the banner the user
  had just expanded — about a second on a live supervisor, so the control looked
  inert. The unread badge counted log events only, missing chat messages, which
  is the one thing anyone actually misses while scrolled up. And the expand
  toggle's CSS was scoped under `.task-detail-row` while the button lives in
  `.task-item`, a *sibling* of that row, so the rule matched nothing and the
  control rendered as default chrome.

  Recorded in this much detail because every one of those passes a
  source-substring test: the call is present, the listener is attached, the
  class is in the stylesheet. See the new browser coverage under *Testing*.

### Fixed

- **A repeating timer in the conversation controller could not be stopped.**
  `createConversationController` installed a bare 30-second `setInterval` with
  no handle, so a second call would have added a second timer repainting from
  the first controller's state. It is called once today, which is a property of
  where the call sits rather than of the code — the same shape app.js's chat
  poller had before it was fixed. The handle is now held at module scope and
  the previous timer cleared before a new one replaces it. Found by running
  rules.md §4 by hand, so `tests/test_qa_timer_handles.py` now checks the rule
  on every suite instead.

- **Two fetch failures were swallowed in the browser.** `loadSettings` caught
  everything and returned `{}`, so a failed or non-2xx `/api/settings` showed
  the panel default values that were indistinguishable from the server's
  answer; and `logout` swallowed its request error, which is the one failure
  worth recording there — the server session stays live while the UI has said
  "logged out". Both now log, and `logout` still navigates away, because the
  user asked to leave (rules.md §12).

- **Members sorted by last activity instead of just creation time.** The member
  table now uses the newer of ``added_at`` and the last message ``created_at``
  as the recency key, so a chat that was updated mid-session moves ahead of
  newly-added but silent ones.

- **The list-loading test no longer breaks on top-level IIFEs.** The restore
  test used a brittle ``split("renderSupervisorList()")`` approach that could
  hit a helper's closing brace. It now uses brace-depth parsing so the
  ``loadSupervisors`` function body is extracted accurately.

- **The proxy resolves the Claude binary without depending on PATH.**
  `bin/wc-proxy-run.sh` now exports an absolute `WC_CLAUDE_PATH`, resolved from
  `~/.local/bin`, `/usr/local/bin`, `/usr/bin` and finally `command -v`. A
  missing binary warns loudly and still starts, rather than aborting or — as
  before — proceeding silently.

  `systemd --user` supplies a PATH without `~/.local/bin`, and
  `claude_proxy.py` refuses to spawn when `shutil.which("claude")` returns
  None, so without this **every turn fails** with "claude binary not found" —
  which the UI shows only as a failed turn. This is the second time the fix has
  been made: the first was an `export PATH=` line that existed only in the
  shared working tree, was never committed, and was reverted by another
  session's checkout. Nothing broke at the time, because the running proxy kept
  the environment it had started with — so the regression sat dormant for
  eighteen hours and detonated on the next restart. Now pinned by
  `tests/test_qa_proxy_claude_path.py`, which executes the resolution block
  under systemd's real PATH instead of reading it.

- **`wc.transcripts` is declared in `logging.conf`.** `transcripts.py` logs the
  repair pass, paging and usage extraction through a logger the config never
  declared, so its records reached the file only by propagating to root — the
  arrangement registry #31 removed for `wc.auth` and `wc.db`. Caught by the
  guard from that same entry, which derives the logger list from source
  precisely so a new `wc.*` logger cannot be added without being declared.

- **Two tests fixed that could no longer pass, for reasons unrelated to the
  code they cover.** Both were reporting failure against working behaviour,
  which is worse than not existing: they train a reader to discount the suite.

  - `test_qa_supervisor_dismiss_endtoend.py::test_a_later_ask_brings_it_back`
    hardcoded its "later" message as `2026-09-01T09:00:00Z`. The dismissal it
    must post-date is stamped with the real `_now()`, and the feed suppresses
    anything with `stamp <= dismissed_at` — so the case passed all morning and
    then failed permanently once the clock passed 09:00Z, with an assertion
    message pointing at a "permanent mute" bug that does not exist. It now
    derives the timestamp from the dismissal it just made.
  - `test_qa_log_path.py::test_nothing_reaches_the_production_log` compared the
    production log's **byte size** before and after its probe. That measures
    every writer — the live server, the health timer, five other sessions — so
    on the machine this project runs on it failed for traffic unrelated to the
    probe, and passed only on an idle box (registry #36's shape). It now
    asserts a unique marker is absent from the production log and present in
    the redirect target, which is the property the test is named for and is
    immune to concurrent writers.

- **The supervisor page's 30-second poller can be stopped.** It was a bare
  `setInterval` — no handle, no teardown — which rules.md §4 names as *the*
  failure case. It now holds `_refreshTimer`, guards re-entry with
  `if (!_refreshTimer)`, and clears on `pagehide`. The guard matters beyond the
  handle: the console loads this page in an iframe it *resets* rather than
  navigates, so a second `init()` is reachable and would have leaked the
  previous timer, leaving two polls running with only one of them stoppable.

  Worth recording is why it survived. The sweep that fixed every other bare
  timer (`3a68c5c`, "hold every repeating timer"), §4's verification command,
  and `tests/test_qa_timer_handles.py` had all inherited the same glob —
  `web/assets/*.js` — and `web/supervisor.js` is the one client script that
  lives directly in `web/`. So three apparently independent confirmations that
  the rule held were a single blind spot counted three times. All three globs
  were widened; the test additionally pins its own **scope**, because the scope
  was the defect and a test that only checked the regex kept passing.

### Testing

- **The supervisor UX affordances are covered in a real browser.**
  `tests/test_qa_supervisor_ux_shortcuts.py` loads the actual page in headless
  Chromium and drives it the way a user does — real clicks, real keystrokes,
  real scrolls, and real stream frames pushed through the `window._supervisorSSE`
  the page already exports. `supervisor.js` is an IIFE, so nothing inside is
  reachable by name, which is the point: the tests can only use the handles a
  user has. One of them asks the browser for the toggle's computed `cursor`,
  because a CSS rule scoped to the wrong ancestor is invisible to any check that
  reads the stylesheet as text.

  Each of the five headline bugs was reintroduced into throwaway copies of the
  two web files to confirm the matching test fails against it. All five did.
  Tests that pass against the defect they name are the recurring failure in this
  area — two earlier browser suites seeded fixtures that the broken code handled
  fine — so having teeth is verified rather than assumed.

  Chromium costs ~25 s to start here and that is fixed overhead, so the file
  shares one launch across all 28 tests, with each feature area isolated so one
  failure does not erase the evidence from the others.

### Security

- **The `/dev/` prefix no longer bypasses authentication.** `AuthMiddleware`
  exempted every path under `/dev/` from the session check, so any route added
  there in future would have been unauthenticated by default — and this
  application spawns Claude Code with `--dangerously-skip-permissions`, so an
  unauthenticated route under that prefix is remote code execution.

  The exemption was **debug scaffolding of mine that nobody meant to ship.** I
  added it, uncommitted, on 2026-08-31 at 18:11 alongside a throwaway
  `/dev/supervisor-trigger` endpoint used to reproduce a supervisor failure.
  Twenty-eight minutes later `1f7c914` — a commit about pause/resume, recency
  sort and member heartbeat — swept both into itself; `dc85305` then removed the
  endpoint and left the exemption behind. A later session found the orphaned
  line in `HEAD`, read it as intentional, and wrote
  `test_auth_middleware_skips_dev_routes` to defend it as "a live product
  decision".

  Nothing was exploitable: with no route registered under the prefix, requests
  returned 404. The defect was latent, and the misattribution is the part worth
  recording — a whole-file commit did not merely move a line, it manufactured a
  product decision out of somebody's debris and then acquired a test guarding
  it. See rules.md §16 registry #55.

  That test's own docstring anticipated this: *"If that exemption is dropped,
  this assertion is the one that will say so, and it should then be deleted
  rather than weakened."* It is another session's committed file, so it is
  flagged rather than edited here.

---

## [0.9.2] — 2026-08-31

### Added

- **The health check now notices a server that has stopped writing.** Until now
  it asked `/login` for a 200, which a server with a dead write path answers
  perfectly — that is how one served for 37 minutes while recording nothing.
  It now also asks whether `system_samples` is still growing: the only table
  written unconditionally on a timer, so silence in it cannot be normal.

  The verdict has four states rather than two. Only `stale` restarts;
  `warming` and `unknown` do nothing, because a server that has just restarted
  inherits rows from before the restart and a yes/no check would restart it,
  and then restart it again. The probe opens the database read-only, so it
  physically cannot cause the fault it looks for, and declines to act at all
  when the file it is reading is not the one the server has open.

### Fixed

- **Supervisor turns crashed Claude Code with "session ID not UUID".**
  The supervisor passed synthetic identifiers (`supervisor_xxx`, `subtask_xxx`)
  to Claude Code's `--resume` flag, which only accepts real UUID session IDs.
  Added a UUID-format guard in `runner.py` and `claude_proxy.py`: `--resume`
  is used only when the session_id matches the UUID pattern; synthetic IDs fall
  through to `--session-id` with a freshly generated UUID so Claude Code can
  track the session internally.

- **Every restart slept ten seconds for no reason.** The release-wait beside
  the port reclaim looped on `! ss -tln "sport = :443" >/dev/null`, testing
  `ss`'s exit status — which is 0 for any successful query, matched or not. The
  condition was false on a free port and a busy one alike, so the loop never
  broke early and never observed the thing it was waiting for. Both exit status
  and stderr were identical either way, which is why nothing caught it; only
  the clock could tell. It tests for output now, and a restart is back to about
  two seconds.

- **The server could not start unless something was already holding its port.**
  The port-reclaim added earlier today resolves the process listening on 443
  through a `ss | grep | head` pipeline, and `launch.sh` runs under
  `set -euo pipefail`. With nothing listening — the normal case on a clean
  start — `grep` found no match, exited 1, and took the script down silently
  right after its banner. The reclaim worked only when the problem it exists to
  fix was present. 43 restart attempts, exit code 1 each time, no traceback in
  the journal or either log, and the application itself starting perfectly by
  hand.

- **`database is locked`, recurring in ordinary use.** The project wrote to one
  SQLite file from three separate connections: the shared one every request
  uses, a fresh connection opened *per search-index write inside a worker
  thread*, and a fresh one per session write. WAL allows a single writer, so
  the 30-second conversation sync — which writes messages to nine
  conversations and triggers an index write for each — had two of them racing
  continuously, and whichever lost waited out its timeout and failed. One
  day's log held 492 sync failures, 66 session failures and 39 from the
  statistics sampler.

  Index maintenance now runs on the shared connection, removing that writer
  altogether, and the shared connection sets its wait explicitly at 15
  seconds — it was the only one of the three that had never set one, quietly
  inheriting a five-second default nobody had chosen.

  The part that was worse than the error: the index write swallowed its own
  exception. A message whose index write lost the race was saved and never
  indexed, so it existed and search could not find it, permanently, with
  nothing logged anywhere.


- **Starting the server killed every other test server on the machine.**
  `launch.sh` cleared a previous instance with `pkill -f "uvicorn app:app"`,
  which matches any uvicorn running this app — including the throwaway servers
  the test suite spawns on random ports. Since `launch.sh` runs on every
  `systemctl restart`, one restart swept the whole box, and the tests reported
  it as their own servers exiting with code -15. Proving the recovery cases
  meant ten restarts, which turned a green suite into 128 failures that had
  nothing to do with the code under test — and the evidence pointed at the
  tests rather than at the supervision that had killed them. The port is now
  reclaimed the way `bin/wc-free-proxy-port.sh` already did it for the proxy:
  find the process actually listening, confirm from its command line that it is
  ours, stop that one, and escalate to SIGKILL only if it ignores SIGTERM. A
  holder that is not ours is reported and left alone.

- **The health check was restarting servers that were merely starting up.**
  Found while proving the recovery cases: a `kill -9` produced two stop/start
  cycles instead of one, because systemd's own `Restart=always` began a
  restart and the health check interrupted it three seconds later for the
  honest reason that nothing was answering yet. Boot takes longer than the
  check's patience, so any restart could be cut short by the next one.

  It now stands aside while systemd is mid-restart, while the process is
  younger than 45 seconds, and when the main process has already exited —
  which is `Restart=always`'s job, not this script's. The first attempt at
  that guard made things worse by failing *unsafe*: when the process was gone
  its age was unknowable, and unknowable was treated as "old enough to
  restart", which is precisely the state during a restart. It now fails safe,
  on the principle that a restarter acting on missing information is worse
  than one that waits thirty seconds for better information.

- **Test servers no longer write into the production log.** `logging.conf`
  named the log file with an absolute path, so every server the suite spawns
  inherited it and appended to `logs/webconsole.log`. The production log ended
  up carrying interleaved lines from processes nobody was watching, including
  future-stamped ones from tests that fake a clock — so the one artefact you
  open first during an incident was actively misleading about ordering. It cost
  real time during the write outage before the foreign entries were recognised
  for what they were.

  The path now comes from `config.LOG_FILE` (`WC_LOG_FILE`, defaulting to the
  current location so a deployment is unchanged) and reaches the handler through
  `fileConfig`'s `defaults`, with the config naming it as `%(logfile)s`. That
  route was chosen over rewriting the handler afterwards because it needs no
  second `fileConfig` call — and `fileConfig` closes every existing handler,
  which is what failed 794 unrelated tests once before. The formatter's own
  `%(asctime)s` tokens are untouched by the interpolation, since `fileConfig`
  reads format strings raw; that was verified by checking a written line
  actually carries a timestamp rather than by assuming.

---

## [0.9.1] — 2026-08-30

### Added

- **The last request stays in view**, on its own line directly under the
  workspace strip, so "what did I ask here?" is answerable without scrolling.
  That matters most on a phone, where the conversation shows two or three
  messages at a time. Set when a conversation is opened, when a turn settles,
  and the moment a request is sent — the last of those because a routed request
  and a queued one produce no turn at all, so waiting for one would leave the
  line permanently stale for exactly the two cases added this week.

### Fixed

- **A request made in the website was counted as a terminal's.** This shipped in
  `0.9.0` without a changelog entry; recorded here rather than left out, since
  the number it corrects is one an operator may already have looked at.

  A conversation linked to a live terminal has its web requests *typed into that
  terminal* rather than run by the server, so the tokens land in that
  terminal's transcript and were imported as its own work. There is now a third
  origin, `web-routed` — asked here, ran there — and neither plain label had
  been true.

  Attribution matches on **what was asked**, not on when. The first version used
  a byte offset and a time window, and credited a routed request with whatever
  the agent happened to be doing meanwhile: typing into a busy session queues
  the input, so work can begin long afterwards. Caught by reading the transcript
  of the live test that was supposed to prove it worked.

- **Terminal usage was one anonymous figure dominated by agent sessions.** A day
  spent working from a phone reported hundreds of millions of "terminal" tokens
  belonging to the agents the console had adopted, filed under the operator's own
  account. Usage is now broken down by origin and named per session, so a
  surprising total is explainable rather than mysterious.

- **Context was counted as spend.** A model reporting no cache breakdown puts
  the whole conversation into `input_tokens` on every turn — one session
  averaged 106,769 a turn with no cache line, so summing it reported 409 million
  tokens for work that mostly re-sent the same context. Those rows are flagged
  at import and reported separately with the reason. Detection is by whether the
  transcript carries the cache keys, not by a size threshold: a threshold would
  flag long Anthropic turns and quietly delete real spend.

- **`styles.css` shipped with no cache-busting query**, so a browser kept
  serving the previous stylesheet and new panels rendered unstyled. The test
  meant to allow this had permitted it for `app.js` and forbidden it for the
  stylesheet, which is why only one of the two ever had one.

- **The queue was unreachable.** Three endpoints had no caller, so a prompt held
  because the turn ahead of it failed could be counted and not acted on. There
  is now a panel above the composer listing each queued prompt with Send and
  Discard.

- **A conversation whose terminal is working showed nothing at all**, because
  `running` comes from the turn registry and a routed request creates no turn.
  Reported separately as `terminal_busy`.

- **Waiting for a concurrency slot looked identical to a slow model** — running,
  with nothing arriving.

- **`tests/test_qa_usage.py` read the operator's real `~/.claude`** through the
  endpoint and imported 18,572 transcript rows into its own temporary database,
  drowning every assertion about what the test had inserted. Two tests were
  failing on `main` because of it.

### Testing

- `tests/test_qa_usage_origin.py`, `tests/test_qa_usage_plumbing.py`,
  `tests/test_qa_last_command.py`, `tests/smoke_last_command.py` — 87 tests
  covering usage origin and attribution, the transcript plumbing that feeds it,
  and the new strip line in a real browser at phone width. All mutation-checked,
  with each mutation confirmed to have modified the file before its result was
  read: three mutations across this work reported "passed" while never having
  applied at all.

- Version numbers are no longer hardcoded in tests. Two assertions carried the
  release number and so broke on every bump; with several sessions working at
  once that turned the assertion into a place to disagree about the number
  rather than a check on the code.


### Added

- **Server statistics** — a new Settings → Server tab reporting the health of
  the machine the console runs on: CPU, memory, swap, disk, load average,
  uptime, and WebConsole's own resident memory, thread and descriptor counts.
  Live cards answer "is it struggling right now"; charts underneath answer
  "was it struggling at 04:00". This closes a request made on 29 August that
  had been half-answered: three sessions built *usage* statistics (tokens,
  requests, cost) and nobody built host statistics, so the Statistics tab was
  a second view of model spend rather than of the server. `psutil`,
  `loadavg`, `virtual_memory` and `disk_usage` appeared nowhere in the tree.
  - Collector (`sysstats.py`) is pure stdlib — `/proc` plus `os.statvfs` — so
    no dependency was added to a pinned requirements set. Linux-only, which
    the deployment already is.
  - A background sampler stores one row a minute (`WC_SYSTEM_SAMPLE_S`) into
    `system_samples`, kept 30 days (`WC_SYSTEM_RETENTION_DAYS`) and pruned at
    startup like usage rows. History has to accumulate while nobody is
    watching, or the charts only ever cover the moments the tab was open.
  - Each bucket stores **both the average and the peak** for CPU, memory,
    disk and process memory. Three idle minutes and one at 100% average to
    25%: a page showing only the mean reports that the machine was
    comfortable during the minute it was not.
  - `GET /api/system` (live snapshot) and `GET /api/system/series`
    (bucketed history), defaulting to 24 hours in half-hour slots rather than
    the usage page's 30 days — a machine in trouble is read by the hour.
  - `lineChart`/`seriesTable` in `stats.js` became exported and gained
    `formatValue`, `formatTip`, `axisMax` and `summarize`, so both statistics
    pages share one chart implementation. Percentage axes are pinned to
    0–100, without which a box idling at 3% CPU draws a line across the top
    of the chart — a truthful shape and a completely misleading picture.

- **Supervisor orchestration engine** (`supervisor.py` / `SupervisorEngine`): a
  multi-agent planning and execution layer. A free-text user prompt is parsed
  into a structured task DAG by `PlanParser` (extracts tasks from
  `<<PLAN>>`…`>>` markers, resolves self- and cross-refs, assigns per-task
  model preferences). `TaskGraph` tracks dependencies so only ready tasks run,
  `ModelRouter` picks the best model per task using a simple ruleset scored by
  a complexity heuristic, and `SupervisorEngine` drives the schedule loop: plan,
  execute ready tasks, repeat until done. Background `LiveTurn` tasks buffer
  each subtask's NDJSON SSE stream so the web UI receives real-time progress.
  A task-level SSE endpoint (`/api/supervisors/{id}/tasks/{taskId}/stream`)
  lets clients follow individual subtasks.

- **Supervisor CRUD, task and message tables** in the database. New `supervisor`
  column on the `chats` table. Tables `supervisor_tasks` (per-supervisor task
  records with status, dependencies, model, progress),
  `supervisor_messages` (task-level messages), and `agent_sessions` (agent
  registry / blocking questions) are all created at startup.

- **Supervisor management endpoints**: POST /api/supervisors (create + start),
  GET /api/supervisors (list), PATCH /api/supervisors/{id} (rename / update
  config), DELETE /api/supervisors/{id} (remove). POST
  /api/supervisors/{id}/send (submit a new prompt). SSE streams at
  /api/supervisors/{id}/stream (all events) and
  /api/supervisors/{id}/tasks/{taskId}/stream (single-task).

- **Supervisor mode selector** in the chat form, supervisor dashboard page
  (`supervisor.html`), and the sidebar supervisor list. A supervisor card shows
  live progress bars for each subtask.

### Fixed

- **Engine planning flow** now calls `runner.run_turn()` directly instead of a
  broken callback chain, meaning planning turns go through the same concurrency
  gate, proxy/subprocess routing, and NDJSON parsing as regular turns.

### Testing

- **Supervisor pipeline stages** in `tests/test_pipeline_audit.py` verify task
  graph construction, dependency resolution, model assignment rules, progress
  tracking, PlanParser robustness, and the supervisor engine's start+send+stream
  endpoints.

---

## [0.9.0] — 2026-08-30

### Fixed

- **Every statistics chart was labelled an hour early, and the day column
  started at the wrong time.** Timestamps are stored in UTC, which is right,
  and were then bucketed in UTC, which was not: work done at 21:00 in Lisbon
  charted at 20:00. The labels were the visible half. The other half was
  quieter and worse — grouping by day or month split at UTC midnight, so an
  evening's work after 23:00 local was filed under the following day. Bucketing
  now groups on the local rendering of the timestamp via SQLite's `localtime`,
  which resolves the zone per timestamp and so follows daylight saving: Portugal
  is UTC+1 in summer and UTC+0 in winter, and a fixed offset would have been
  wrong for half the year. The `created_at >= ?` range filters stay in UTC on
  purpose — "the last 24 hours" is a span measured back from now, and a span has
  no timezone. Fixes the usage statistics, the per-model series and the server
  statistics together, since all three share one bucketing expression. Nothing
  changed on the client: it prints the bucket key verbatim, and every other
  timestamp in the UI was already converted in the browser.

- **The Server statistics panel never updated.** `loadServer()` ran once when
  the tab was opened and never again, so CPU, memory, load and uptime were
  frozen at whatever they read the moment the panel appeared, and the history
  charts never picked up samples the server had stored since. A live reading
  that does not change is worse than none: it looks current. The panel now
  refreshes every 30 s — half the sampler's interval — while it is on screen,
  and stops when you leave the tab or close settings, so a closed panel is not
  polling `/proc` for the rest of the session. Background refreshes are quiet:
  they keep the current reading on screen instead of flashing skeletons, and a
  single failed poll leaves the last good reading rather than replacing it with
  an error. (The sampler itself was fine — it had been recording once a minute
  throughout.)

- **The test suite reported 865 failures that were not real, and hid the one
  that was.** Running everything at once failed 865 of 1380 tests while every
  file passed alone. None of the named tests were at fault: the browser fixture
  released playwright in `tearDown`, which unittest skips when `setUp` raises,
  so a single slow login leaked it. Playwright's sync API drives its asyncio
  loop through greenlets, and a loop that is never stopped stays flagged as the
  *running* loop for the thread — after which every `IsolatedAsyncioTestCase`
  in the process dies on "Runner.run() cannot be called from a running event
  loop". Resources are now released with `addCleanup` and `addClassCleanup`,
  registered as they are acquired, so a failure can no longer escape with them.
  The suite is 1405 passed, 0 failed, twice in a row.
- **The browser tests read the developer's own Claude sessions.** `/api/supervisor`
  merges the live CLI sessions under `~/.claude`, and the test server inherited
  the real `HOME` — so its "waiting agents" count reflected whatever other
  agents on the machine were doing. Since a device alert fires only on a *rise*
  in that count, an unrelated session answering a question in the same poll
  window cancelled the rise and the test waited 90 seconds for a notification
  that had already been netted out. The fixture now gives its server a `HOME`
  of its own.
- **A test server could block for ever on its own log.** Its output went to a
  `subprocess.PIPE` nothing read; past 64 KB uvicorn blocked writing and stopped
  answering, which surfaced only as `ERR_CONNECTION_REFUSED` naming a port.
  Output goes to a file now, and a dead server is reported with its exit code
  and log tail instead of a refused connection.

### Testing

- `tests/test_qa_browser_fixture.py` pins the cleanup contract with a stubbed
  playwright, so it runs without a browser and fails if anyone moves resource
  release back into `tearDown`.

- **Messages sent in the web UI were duplicated when the conversation was linked
  to a live Claude Code terminal.** The `/stream` handler stored the user prompt
  in the `messages` table, then the background sync poll (every 5 s) read the
  same turn from the CLI transcript and inserted it again via a plain `INSERT`.
  The same duplication happened on the non-streaming `/api/chats/{id}/messages`
  endpoint. Two changes close the race: the sync endpoint now compares the last
  stored row against the first row from the transcript and skips the import
  when they match (a tail-equality check on fixed-order data is sufficient to
  know the entire block is already present), and both terminal-routed paths
  now call `_skip_transcript_to_end` immediately after storing the prompt so
  the offset advances past it before any poll can fire. (`db.messages_last()`
  helper added for the tail lookup.)

### Added

- **Conversation `updated_at` is bumped when a sync imports new messages.**
  Previously a sync that only moved a read offset would not touch the
  timestamp, so a conversation whose terminal was busy was never reprieved of
  its "working" status in the sidebar.

- **Terminal "busy" dot in the sidebar.** A turn now outlives the request that
  started it, so `running` on the server side is insufficient — conversations
  linked to live terminals need their own indicator. The sidebar now shows a
  busy dot for sessions whose Claude Code process reports `"busy"` in
  `~/.claude/sessions/<pid>.json`, read from the status field the supervisor
  already trusts.

- **Explicit Stop endpoint** (`POST /api/chats/{id}/stop`). When a turn
  outlives its viewer, implicit stop-by-aborting-the-reader no longer works.
  A stopped turn keeps its partial answer, because the tokens are already billed.

- **Waiting for a concurrency slot is reported.** With `WC_MAX_CONCURRENT` turns
  already running, the next one used to look identical to a slow model: running,
  with nothing arriving. The server now yields a `waiting_for_slot` status event
  describing how many slots are occupied.

- **Usage series endpoint** (`GET /api/usage/series`) for the Usage tab charts.
  Bucketed over time (day, week, month) with a configurable window (1–3650 days).
  Kept separate from the flat `/api/usage` table so the common read does not pay
  for the rare one.

- **Supervisor module** (`supervisor.py`): read/write the agent session
  registry, stream live session updates, list and manage blocking questions,
  and message agents back. Agents that have asked a question and are waiting are
  surfaced so a stalled agent is visible instead of silently idle.

### Changed

- Version string bumped to `0.9.0`.

---

## [0.8.2] — 2026-08-30

`0.8.1` never reached a commit. It existed only as a version string in a working
tree and carried no release of its own, so — following the convention this file
already uses for 0.4.0 and 0.7.1 — its number is recorded as skipped rather than
silently reused. Everything previously under *Unreleased* ships here.

### Added

- **A request keeps running when you switch conversation.** Sending a prompt used
  to lock you into that conversation: the UI refused the switch with "Stop the
  current response before switching conversations". The refusal was protecting
  the turn, not the interface — the browser's stream reader *owned* the turn, so
  closing it terminated the Claude process, and because nothing was persisted
  until the turn finished, the answer was thrown away. The usage row had already
  been written, since tokens are recorded as they arrive. **Leaving mid-turn
  billed you and returned nothing.**

  A turn is now a background task on the server with a numbered event buffer
  (`turns.py`). Clients attach and detach through
  `GET /api/chats/{id}/live?since=<seq>`. Switching conversation, reloading,
  closing the tab and a phone locking are all just a viewer leaving; the turn
  runs to completion, stores its answer, and replays what you missed when you
  come back.

- **Prompts sent during a turn are queued** rather than refused, in a persisted
  `turn_queue` table so a queued prompt survives a reload. The queue drains one
  prompt per clean finish. On failure the rest are **held** rather than fired
  into a conversation that has just broken, and a panel above the composer lists
  them with *Send* and *Discard*.

- **The sidebar shows what is busy**, read from the server rather than from the
  page: several conversations can be mid-turn at once, so the single running
  indicator became a set. A conversation whose linked terminal is working gets
  its own outlined dot — that state previously showed nothing at all. A reply
  that lands while you are elsewhere leaves an unread mark.

- **Stop is now explicit** (`POST /api/chats/{id}/stop`). It had been implicit —
  abort the reader and the turn died with it — and once a turn outlives its
  viewer that is the only way to tell "I am leaving" from "stop working". A
  stopped turn keeps its partial answer, because the tokens are already billed.

- **Waiting for a concurrency slot is reported.** With `WC_MAX_CONCURRENT` turns
  already running, the next one used to look identical to a slow model: running,
  with nothing arriving.

- `tests/test_qa_background_turns.py` — 26 tests, mutation-checked, including the
  billing regression: a client that disconnects mid-turn must still leave the
  answer stored and exactly one usage row.
- `tests/smoke_background_turns.py` — 22 checks in a real browser, since the
  thing asked for is a UI behaviour and the unit tests cannot demonstrate it.

### Fixed

- **Switching conversation announced "Response stopped"** for a turn that was
  still running. A detach and a stop are the same abort from the client's point
  of view; only the intent differs. Found by the browser smoke test.
- A tool result is rendered rather than dropped since `1cfc978`, which left
  `test_results_for_other_tools_are_still_dropped` failing on `main`. Rewritten
  to the contract that still matters: an unrelated tool result must not pair
  with a pending question and resolve it.
- `wc.turns` was logging without a `logging.conf` block, so its records — the
  queue drain, timeouts, crashes in turns nobody is watching — fell through to
  root.


### Fixed

- **A fresh clone could not start.** `0.7.2` shipped the session-durability work
  with `auth.py` left out of the commit: the tests went in, the `app.py` call
  site went in, the implementation did not. `HEAD` therefore called
  `auth.load_sessions()` against an `auth.py` with no such function, so a clone
  raised `AttributeError` at startup and the 17 tests in
  `test_qa_session_durability.py` failed against a file that could not provide
  what they imported.

  It survived a full pipeline run and a push because `auth.py` was correct **on
  disk** the whole time — every test passed against a working tree that `HEAD`
  did not describe, and the repository had not been pushed since 0.3.0. The gap
  only ever existed for someone cloning it.

  Found while investigating why 0.4.0 was missing from this file.

- **Corrects this file's own first draft**, which stated that 0.4.0 and 0.7.1
  "were skipped and never shipped". Both existed as real work and both shipped
  inside the following release; they now have sections of their own.

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

## [0.7.1] — 2026-08-29 *(never committed; shipped inside 0.7.2)*

Same story as 0.4.0: stashed against the 0.7.0 tree on 29 August at 16:43, never
committed under its own number, and folded into 0.7.2. About 90% of its lines
are in the current tree.

### Added

- Supervisor coverage and browser tests for the conversation list, the question
  flow and usage logging — 969 lines across nine test files.
- The `auth.py` half of durable sessions. **This is the one piece that did not
  make it**, and it was not the stash's fault: see the correction under
  [Unreleased].

---

## [0.7.0] — 2026-08-29

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

## [0.4.0] — 2026-08-28 *(never committed; shipped inside 0.5.0)*

This version existed on disk for a day and never got a commit of its own. The
work was stashed on 28 August at 22:25 against the 0.3.0 tree, and when the next
release was cut the version went straight from 0.3.0 to 0.5.0. The stash still
exists as an unreferenced object; roughly 90% of its distinctive lines are in the
current tree, and the remainder is markup and tests that were rewritten later —
so this is a missing label, not missing work.

### Added

- **Skill discovery, and a Skills panel in Settings.** `_discover_user_skills`
  and `_discover_plugin_skills` walk the skill directories, `_read_skill` parses
  each one, and `_skill_description` / `_skill_summary` reduce it to a line worth
  showing. Surfaced as a fourth Settings tab beside Machines, Models and App,
  with a filter box and a live count.

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
[0.7.1]: https://github.com/tarrinho/claude_code_webclient/commit/ab2fb6d  (unreferenced stash object; `git show ab2fb6d`)
[0.7.0]: https://github.com/tarrinho/claude_code_webclient/compare/149aa79...e42a3f8
[0.6.1]: https://github.com/tarrinho/claude_code_webclient/compare/3137ca2...149aa79
[0.6.0]: https://github.com/tarrinho/claude_code_webclient/compare/42a4df7...3137ca2
[0.5.1]: https://github.com/tarrinho/claude_code_webclient/compare/71de7dc...42a4df7
[0.4.0]: https://github.com/tarrinho/claude_code_webclient/commit/e15f9ae  (unreferenced stash object; `git show e15f9ae`)
[0.5.0]: https://github.com/tarrinho/claude_code_webclient/compare/058cc2c...71de7dc
[0.3.0]: https://github.com/tarrinho/claude_code_webclient/compare/ed21559...058cc2c
[0.2.0]: https://github.com/tarrinho/claude_code_webclient/compare/aa40bc2...ed21559
[0.1.0]: https://github.com/tarrinho/claude_code_webclient/commit/aa40bc2
