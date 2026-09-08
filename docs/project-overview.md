# WebConsole — orientation for a new agent session

**Who this is for:** a Claude Code session (or a person) newly pointed at this
repository, who needs to know what the project is and what will go wrong,
without reading 47 modules first.

**Why it exists as a file rather than an answer:** several short-lived agent
sessions asked a live session for this summary and every one of them exited
before the reply could be delivered. Cross-session messages queue for the
receiver's *next tool round*, so a session that sends a question and then exits
has no round left to receive on — the send succeeds and the reply silently has
nowhere to land, which is indistinguishable from a broken transport. A file
does not have that problem. Read this instead of asking, and only ask a live
session for what this does not cover.

This is orientation, not reference. It deliberately does not restate the
documents listed below.

---

## Where the real documentation is

| Document | What it covers |
|---|---|
| `README.md` | What the product is, how to run it, and the deployment warning |
| `ARCHITECTURE.md` | The reference: components, data flow, database schema, API, security model, configuration, file inventory, testing strategy |
| `SECURITY.md` | Security posture and reporting |
| `docs/threat-model.md` | Threat model |
| `CHANGELOG.md` | Release history |

Two more documents govern this checkout and **are not in git**, by deliberate
choice — `CLAUDE.md` and `rules.md`, both gitignored:

- `CLAUDE.md` describes how a turn reaches a model backend *on this specific
  deployment*: the proxy hop, which environment variables carry credentials,
  and the routing table. That is operational detail about a running host, which
  is why it does not travel with the source. If you are working on the
  turn-execution path — `runner.py`, `claude_proxy.py`, `backend_env.py`,
  `bin/wc-claude.sh` — read it first. Every rule in it is there because it was
  broken once and cost real debugging time.
- `rules.md` is the project's own engineering rule registry.

If you have a clone rather than this checkout, you do not have either file, and
you should not assume the turn path works the way it looks like it works.

---

## The one rule that explains the design

**The console never talks to a model API. It spawns the `claude` CLI and varies
its parameters and environment.**

Every backend, every model, every resume, every provider is that one mechanism
configured differently. There is no second transport, no SDK call, and no HTTP
client for a model provider anywhere in this codebase. Adding one is not how a
problem gets solved here.

Three consequences that are easy to miss:

- **Reach for a CLI flag before writing code.** A capability the CLI already
  has behind a parameter must not be reimplemented in Python.
- **A provider is a URL and a key, not a code path.** An OpenAI-compatible
  gateway is served by pointing the same CLI at a different base URL.
- **A model id is only meaningful against the backend serving it.** The unit of
  configuration is therefore a *profile*: one backend, its credential, its
  default model. Send a gateway-only model to the official API and you get an
  error that reads like capacity and is really routing — the hardest kind to
  diagnose.

The environment a backend implies is defined in exactly one place,
`backend_env.deltas()`. It returns things to **set** and things to **unset**,
because the removals are the half that matters: an inherited credential
silently outranks the one you set, and the turn then succeeds against the wrong
account. Three callers apply those deltas — the TCP proxy, the direct runner,
and the shell wrapper. Never reimplement it; call it.

---

## Stack

FastAPI and uvicorn; SQLite through aiosqlite (WAL, `busy_timeout`, additive
`ALTER TABLE ... ADD COLUMN` migrations); vanilla ES modules on the frontend
with `?v=N` cache-busters, no build step and no framework. It runs as
`systemd --user` units — the app, a proxy, and a health timer — and is served
over HTTPS on a private Tailscale address with a Tailscale CA certificate.

Roughly 47 Python modules and 191 test files, 10 of which drive a real browser.

---

## What will bite you

### 1. Several sessions share this one working tree

This is not a worktree per session. Multiple Claude sessions edit and commit
here concurrently, and two sessions editing the same file will clobber each
other silently.

- Use **targeted pathspecs**. Never a whole-tree `git add`.
- **Never run `git stash`.** It reverts every peer's in-flight work across the
  whole tree, not just yours.
- Committing needs care, because the two obvious commands fail in opposite
  ways. `git commit` with no pathspec commits *the index*, so it sweeps in
  anything a peer has staged. `git commit -- <paths>` commits *the working
  tree* for those paths and ignores the index, so it sweeps in any peer edit
  sitting unstaged in a file you name. When both the index and the working tree
  are contaminated — the normal state here — neither is safe.
- **When one file holds two sessions' edits, hunk selection may not save you
  either.** Two sessions can add to the same physical line. What works is to
  compute the content you want to commit and stage that directly, leaving the
  working tree untouched:

  ```bash
  git show HEAD:<path>            # start from the committed content
  # apply only your own edit to that text, writing it to a temp file, then:
  sha=$(git hash-object -w --path <path> /tmp/staged)
  git update-index --cacheinfo 100644,$sha,<path>
  ```

  Then grep `git diff --cached` for the peer's identifying strings and assert
  none are present before committing. The assertion is what makes this safer
  than eyeballing a diff.
- Never amend or revert to unpick a peer's hunks out of a commit. The work is
  intact where it landed; surgery is how it actually gets lost. Land a fresh
  commit on top instead.

### 2. Tests have two invocation rules, and both are load-bearing

```bash
.venv/bin/python -m pytest        # correct
```

- **Use `.venv/bin/python`.** Any other interpreter — including a bare `python`
  — silently skips the entire browser layer. A skip reads as "not applicable
  here", which is indistinguishable from "ran and passed" in a total. A
  trustworthy full run reports exactly **6 skips**; a run reporting more has
  quietly not tested something. A collection-time guard now refuses the wrong
  interpreter rather than warning, because warning was tried and did not work.
- **Invoke pytest bare, not `pytest tests/`.** The latter misses files.

The host is small and shared, so a single-process full run gets refused by the
in-repo memory guard or killed by the OOM killer. A killed run just stops,
which reads like a hang or a broken test rather than a memory problem. Run the
suite in chunks and record a per-chunk result, so a chunk that dies is visible
as itself rather than as a silently missing slice of a total.

One more caution learned the hard way: **a chunk result taken while other
sessions are rewriting files underneath it is not trustworthy.** One run hung
for twenty minutes at a point where every file in that chunk passed
individually in seconds; the same chunk then passed cleanly on a quiet tree.

### 3. Two things that are destructive by surprise

- **Never run `db.init()` against the production database.** It migrates. Copy
  to a throwaway `WC_DB_PATH` using the SQLite backup API and run against that.
- **Restarting the service cancels in-flight turns**, and this host is shared
  with other sessions and with the user. Check for streaming activity first,
  and restart through systemd rather than reconstructing the command by hand —
  the unit carries environment the process needs.

### 4. Cache-busters must agree across every importer

The frontend has no bundler. A module is imported as `./app.js?v=52`, and a
differing query string is a **separate cache key**: the browser then evaluates
that module twice, as two instances with two copies of its top-level state, and
reports no error anywhere. Panels read the wrong state and nothing looks
broken.

So if you change a module's version marker, change it in `web/index.html` and
in *every* file that imports it, in lockstep. `tests/test_qa_asset_module_versions.py`
enforces this. Assets are served with ETag revalidation and no long `max-age`,
so these markers only need to *agree* — they do not need bumping on every edit.

---

## Current state

Everything in this section goes stale. It was true at the commit noted below;
re-derive it rather than trusting it.

```bash
grep -oP '(?<=^VERSION = ")[^"]+' config.py   # current version
git log --oneline -8                          # what landed recently
git status --porcelain                        # what other sessions hold right now
```

As of `37f7fa1` on 2026-09-08: version `WebConsole_0.15.4`, deployed and
healthy, about 3060 tests passing across four chunks with roughly ten failing.
None of those ten indicated a broken product. Six were a 0.15.3-to-0.15.4
release in flight at that moment: the version constant had been bumped while
`ARCHITECTURE.md` and several HTML files had not caught up, so the
version-consistency checks were failing on the transition itself. The rest were
another session's work in progress — a file over its own line limit while open
for editing, and a test still patching a function the code had stopped calling.

That distribution is the normal state here, and it is worth internalising
before you read a failure list: on a shared tree, *most* red tests at any given
moment belong to somebody else's half-finished change. Attribute a failure
before fixing it. `git log -S '<symbol>' --oneline -- <path>` and
`git status --porcelain` will usually tell you whose it is in under a minute,
and editing a file another session is actively holding is how their work gets
destroyed.

**Before you edit anything, run `git status --porcelain` and treat every
modified path as owned by another session** until you have established
otherwise. That list changes by the minute.

---

## Reaching a live session

Sessions on this machine can message each other. Discovery is by name, and a
name is the address. Two practical points:

- A message is delivered on the receiver's **next tool round**, so a busy
  session does not answer immediately. That is queuing, not failure.
- **If you send a question, stay alive to receive the answer.** A session that
  exits after sending cannot be replied to, and its socket is gone. This is the
  single most common way a cross-session exchange fails, and it looks exactly
  like a transport bug from the sending side.

Permission decisions are per-session and do not transfer. A peer cannot approve
an action your own settings refuse, and asking a peer to perform something you
were denied routes around the user's decision — take it back to the user
instead.
