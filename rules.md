# WebConsole Rules

## 0. Preflight capacity gate — run this first, and stop if it fails

**This is the first thing a run does, before cleanup, before any check, and
before anything is killed or started. If it reports ABORT, the run stops here.
Do not continue to §0a, do not "just run the static stages", do not run the
suite anyway.**

The reason is measured, not precautionary. This host has 3.73 GB of RAM and
routinely carries six or more concurrent `claude` sessions holding around 2 GB
between them, while the live server alone peaks at 1.0–1.2 GB per run
(systemd's own `memory peak` accounting). A full suite adds a uvicorn per
browser class and a Chromium on top of that. When it does not fit, the kernel
does not fail the thing that asked for the memory — it kills the largest
resident process, and on this box that is the live server. On 2026-09-07 two
full-suite runs died mid-flight with no summary, and at 23:35:11 both
`webconsole.service` and `webconsole-proxy.service` were killed in the same
second with `status=9/KILL`. Running the suite on a full box does not merely
fail the run; it can take the site down with it.

**The verdict travels in a file, not a shell variable, and that is the whole
point of this section.** The gate this replaces set `SKIP_TESTS=1` in one
block and tested `${SKIP_TESTS:-}` in another. Every stage of a run executes in
its own shell, so the variable never arrived and the gate was open in exactly
the condition it existed to close — the low-memory case it was written for was
the one case it could not stop. Verified, rather than reasoned about: set in
one shell, empty in the next.

```bash
# >>> preflight-block
# Thresholds. Overridable so the gate can be tested without waiting for a full
# box -- the same seam wc-health.sh uses for systemctl (registry #44).
MIN_MEM_KB="${WC_PREFLIGHT_MIN_MEM_KB:-2097152}"      # 2 GB available
MAX_SWAP_PCT="${WC_PREFLIGHT_MAX_SWAP_PCT:-80}"
STATE="${WC_PREFLIGHT_STATE:-${TMPDIR:-/tmp}/wc-rules-preflight-$(id -u)}"

# Readings. The overrides exist for the test; a real run reads /proc.
MEM_KB="${WC_PREFLIGHT_MEM_KB:-$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)}"
SWAP_TOTAL="${WC_PREFLIGHT_SWAP_TOTAL:-$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)}"
SWAP_FREE="${WC_PREFLIGHT_SWAP_FREE:-$(awk '/^SwapFree:/{print $2}' /proc/meminfo)}"

if [ "${SWAP_TOTAL:-0}" -gt 0 ] 2>/dev/null; then
  SWAP_PCT=$(( (SWAP_TOTAL - SWAP_FREE) * 100 / SWAP_TOTAL ))
else
  SWAP_PCT=0
fi

REASONS=""
[ "${MEM_KB:-0}" -lt "$MIN_MEM_KB" ] 2>/dev/null && REASONS="${REASONS}only $((MEM_KB/1024)) MB available (need $((MIN_MEM_KB/1024)) MB); "
[ "$SWAP_PCT" -gt "$MAX_SWAP_PCT" ] && REASONS="${REASONS}swap at ${SWAP_PCT}% (limit ${MAX_SWAP_PCT}%); "

echo "preflight: MemAvailable=$((MEM_KB/1024))MB swap=${SWAP_PCT}% load=$(cut -d' ' -f1 /proc/loadavg) cpus=$(nproc)"

if [ -n "$REASONS" ]; then
  printf 'ABORT\t%s\n' "$REASONS" > "$STATE"
  echo "PREFLIGHT ABORT: ${REASONS}"
  echo "STOP. Do not run any further stage of rules.md on this box right now."
  exit 1
fi

printf 'OK\t%s\n' "$(date -Is)" > "$STATE"
echo "PREFLIGHT OK: enough headroom to run"
# <<< preflight-block
```

`exit 1` is load-bearing: it is what makes the stop visible to whatever is
driving the run, rather than a line of output that can be read past. The state
file at `$STATE` is what later stages read — see §0b — so a stage cannot
silently proceed on a verdict it never saw.

Two things this gate deliberately does **not** decide:

* **§17 is a separate hazard and passing this gate does not clear it.** That
  stage kills the app, kills the proxy, and `SIGSTOP`s the live server on
  purpose. It interrupts the site and every session's in-flight turns whatever
  the memory reading says, so it stays an explicit choice rather than something
  a green preflight authorises.
* **Peer sessions.** Six sessions share this tree. A green light here means the
  box has room, not that nobody else is mid-run.

## 0a. Process cleanup before a run

Kill stray pytest/python/test processes from previous runs so they don't
interfere with the current one — they are the first thing that goes wrong
when a previous run was interrupted (Killed or Ctrl-C'ed) or two sessions
overlap on one tree. A stray process holds the database and locks out the
new run; it can also hold a port and prevent a fresh uvicorn from binding,
which looks like a launch problem rather than an orphan.

```bash
ps aux | grep -E 'pytest|python.*test' | grep -v grep | awk '{print $2}' | xargs -r kill -9 2>/dev/null
```

Also kill stale Chromium tabs from previous dev runs. A tab alive >60s with >50MB RSS is almost certainly an old companion/dev session, not an active browser. Best-effort (`|| true`) — not running a run is worse than leaving one tab behind.

```bash
for pid in $(pgrep -u $(id -u) chrome chromium 2>/dev/null); do
  rss=$(awk '/VmRSS/{print $2}' /proc/$pid/status 2>/dev/null) || continue
  elapsed=$(( $(date +%s) - $(stat -c %Y /proc/$pid 2>/dev/null) )) || continue
  if [ "$rss" -gt 51200 ] && [ "$elapsed" -gt 60 ]; then kill -TERM "$pid" 2>/dev/null; fi
done || true
```

## 0b. Re-check the preflight verdict before the suite

§0 decided whether this box can take a run. This reads that decision back
immediately before the expensive stages, because time passes between the two:
a peer session can start its own suite, or a turn can grow, in the minutes a
run takes to reach §14.

Read the verdict from the state file rather than from a variable. A missing
file means §0 never ran, which is treated as a stop rather than as permission —
an absent gate must never read as an open one.

```bash
# >>> preflight-recheck-block
STATE="${WC_PREFLIGHT_STATE:-${TMPDIR:-/tmp}/wc-rules-preflight-$(id -u)}"
if [ ! -f "$STATE" ]; then
  echo "STOP: no preflight verdict at $STATE — §0 never ran. Run it first."
  exit 1
fi
if ! grep -q '^OK' "$STATE"; then
  echo "STOP: preflight recorded $(cut -f1 "$STATE") — $(cut -f2 "$STATE")"
  exit 1
fi
# Conditions change during a run, so the reading is taken again, not trusted.
MEM_KB="${WC_PREFLIGHT_MEM_KB:-$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)}"
MIN_MEM_KB="${WC_PREFLIGHT_MIN_MEM_KB:-2097152}"
if [ "${MEM_KB:-0}" -lt "$MIN_MEM_KB" ] 2>/dev/null; then
  printf 'ABORT\tmemory fell to %s MB during the run\n' "$((MEM_KB/1024))" > "$STATE"
  echo "STOP: memory fell to $((MEM_KB/1024)) MB since §0 — do not start the suite"
  exit 1
fi
echo "preflight still OK: $((MEM_KB/1024)) MB available"
# <<< preflight-recheck-block
```

## 0c. Version consistency

`config.VERSION` must match the version string in `web/index.html`.

```bash
V=$(python3 -c 'import config;print(config.VERSION.split("_")[1])')
grep -rn "$V" --include='*.html' web/
grep -rn '0\.' --include='*.py' --include='*.html' . | grep -v test | grep -v __pycache__ | grep -v .venv | grep -v "$V" || echo "no stale versions"
```

## 1. Static/source invariant tests

- All Python files compile (`py_compile`) — no syntax errors, no unbound
  names at parse time.

```bash
python3 -m py_compile $(find . -name '*.py' -not -path './.venv/*' -not -path './__pycache__/*' | grep -v test)
echo "compile: PASS"
```

## 2. Input sanitization pass 1

- No `shell=True` in any `subprocess` call.
- No SQL f-strings with user data (table names are OK).
- No path traversal: user input must be checked against `projects_root`
  using `is_relative_to`.

```bash
grep -rn 'shell=True' --include='*.py' . | grep -v test | grep -v '.claude'
grep -rn 'f".*{' --include='*.py' . | grep -i 'execute\|query\|sql' | grep -v test | grep -v '.claude' | grep -v 'PRAGMA\|mode=ro\|uri' | grep -v __pycache__
```

## 3. Logging check

- All Python files under 400 lines.
- All JS files under 300 lines.

`./.claude/*` is excluded because it holds git worktrees — other checkouts of
this same project. Left in, every file is reported twice, the list is roughly
double its real length, and half the paths are another branch's problem. That
is not a hypothetical: a 2026-09-10 run reported 80 oversized Python files, of
which 53 were worktree copies.

`tail -5` also under-reports on purpose-defeating terms — it shows the five
largest whether or not they breach the cap, and hides the sixth when six
breach it. Print what is actually over.

```bash
find . -name '*.py' -not -path './.venv/*' -not -path './__pycache__/*' -not -path './.claude/*' -not -path './tests/*' | xargs wc -l | awk '$2 != "total" && $1 > 400' | sort -n
find . -name '*.js' -not -path './.venv/*' -not -path './__pycache__/*' -not -path './.claude/*' -not -path './tests/*' | xargs wc -l | awk '$2 != "total" && $1 > 300' | sort -n
```

Both caps are long and widely breached (measured 2026-09-10: 27 Python files
over 400 lines, 12 JS files over 300). Treat the list as a standing debt
register and a reason not to add to the worst offenders, not as a gate to be
cleared in one run.

## 4. XSS scan

Scan every client-side JS file for `innerHTML` and `escapeHtml` usage.
Files should use `esc()` from the shared escaper — not raw string
interpolation or `innerHTML=`.

```bash
find web -name '*.js' -exec grep -l 'innerHTML' {} \;
find web -name '*.js' -exec grep -l 'escapeHtml' {} \;
```

## 5. Requirements / imports

`requirements.txt` must list every package that the code imports.
Cross-reference the import list against the requirements file.

Parsed, not grepped. The previous form of this stage was a `grep 'import '`
into two `sed` substitutions, which matched the word "import" anywhere —
including inside docstrings and comments — and then mangled whatever followed
into a module name. A 2026-09-10 run produced entries like `# `, `argparse`,
`a click on a list row, or immediately after creating one, so opening` and
`"""A chat resumed before the existed`. Output that noisy is output nobody
reads, which makes the stage decorative: registry #19 and #20 are both missing
requirements that reached production, and #20 notes this scan missed it.

```bash
.venv/bin/python - <<'PYEOF'
import ast, pathlib, sys
stdlib = set(sys.stdlib_module_names)
root = pathlib.Path(".")
local = {p.stem for p in root.glob("*.py")}
local |= {p.name for p in root.iterdir() if p.is_dir()}
local |= {p.stem for p in root.glob("tests/*.py")}
found = set()
for p in root.rglob("*.py"):
    if {".venv", "__pycache__", ".claude"} & set(p.parts):
        continue
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        continue
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            found |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            found.add(n.module.split(".")[0])
# requirements.txt entries, normalised: strip version pins and extras
# ("uvicorn[standard]==0.52.4" -> "uvicorn"), since neither appears in an
# import statement and leaving them in reports a satisfied dependency missing.
reqs = set()
for line in open("requirements.txt"):
    line = line.strip()
    if line and not line.startswith("#"):
        for sep in ("==", ">=", "<=", "~=", ">", "<"):
            line = line.split(sep)[0]
        reqs.add(line.split("[")[0].strip().lower().replace("-", "_"))
# Distribution name != import name for these.
ALIAS = {"argon2": "argon2_cffi", "dotenv": "python_dotenv", "jwt": "pyjwt",
         "yaml": "pyyaml", "PIL": "pillow", "multipart": "python_multipart"}
# Test-only tooling: rules.md §14 installs these explicitly on the QA node
# rather than shipping them as runtime dependencies.
TEST_ONLY = {"pytest", "playwright", "selenium", "quickjs", "httpx", "requests",
             "pyflakes"}
missing = sorted(
    m for m in found
    if m not in stdlib and m not in local and m not in TEST_ONLY
    and m.lower().replace("-", "_") not in reqs
    and ALIAS.get(m, "").lower() not in reqs
)
print("MISSING from requirements.txt:", missing or "none")
PYEOF
```

A name-based scan of any kind still cannot catch a runtime-only dependency
that nothing imports by name — registry #20 (`python-multipart`, reached
through `await request.form()`) is the standing example, and the only thing
that finds that class is §13 exercising the endpoint.

## 6. SQLite integrity

Run `PRAGMA integrity_check` against the production (or test) database.

```bash
python3 -c "import sqlite3; conn = sqlite3.connect('data/webconsole.db'); print(conn.execute('PRAGMA integrity_check').fetchone()); conn.close()"
```

## 7. Auth smoke

Log in via the test client, verify the session cookie is set, then
access an authenticated endpoint.

## 8. Security audit (signature stage)

Sign the current audit pass with a one-line SHA of the repo state.

## 9. Code review (flake8)

Run flake8 to catch unused imports, undefined names, and style issues.

```bash
flake8 $(find . -name '*.py' -not -path './.venv/*' -not -path './__pycache__/*' -not -path './tests/*') 2>/dev/null | head -20
```

## 10. Static hardening

- No `setInterval` without a handle (`_timer = setInterval`).
- No `eval(` in client JS.
- No `setTimeout("...")` (string eval).

## 11. No bare `setInterval`

Every `setInterval` call must be assigned to a variable and cleared.

```bash
grep -n 'setInterval(' web/assets/*.js | grep -v '_.*Timer\|_.*timer' | head -10
```

## 12. Code review — review changed files

Manually review every changed file for logic errors, race conditions,
and security issues. Pay special attention to:
- New routes or handlers: are they authenticated?
- Database queries: do they use parameterized queries?
- User input: is it sanitized/escaped before output?

## 13. DAST — dynamic application security test

Set up a local proxy (Caddy or nginx) that terminates TLS and proxies to
uvicorn on port 8080. The Caddyfile should have:

```
{
    # ... any site-level config
}

kali-2.tail850c40.ts.net {
    reverse_proxy 127.0.0.1:8080
}
```

uvicorn is started as:

```bash
.venv/bin/python -m uvicorn app:app --host 100.107.88.3 --port 443 \
  --ssl-certfile /home/kali/.local/share/webconsole/certs/fullchain.pem \
  --ssl-keyfile /home/kali/.local/share/webconsole/certs/key.pem \
  --log-level info
```

So the app binds to port 443 directly with TLS. Caddy on 443 would
conflict — only one can listen on a port. In a local test environment,
start uvicorn on a different port:

```bash
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8080 --log-level info
```

Then configure Caddy to proxy to 8080.

Run the full suite:

```bash
.venv/bin/python -m pytest tests/test_dast.py -v -rs
```

If a server is already running on the port, point it explicitly:

```bash
WC_SERVER_URL=http://127.0.0.1:18081 .venv/bin/python -m pytest tests/test_dast.py -v -rs
```

The test file creates its own test DB and test admin user, starts a
temporary uvicorn instance, and tears everything down. It never touches the
production database or any shared state.

## 14. Dependency installation gate

All dependencies must be installable and importable in a clean environment:

```bash
# Use the venv so we never hit "externally-managed-environment" (PEP 668)
# on distros that enforce it (Kali, Fedora, openSUSE, Arch, etc.).
# The dry-run flag doesn't touch the filesystem, but pip's guard fires
# anyway — it's a check on how pip was invoked, not on what it would do.
.venv/bin/pip install -r requirements.txt --quiet --dry-run 2>/dev/null && echo "requirements.txt OK" || echo "FAIL: requirements.txt"
.venv/bin/python -c "import fastapi; import aiosqlite; import argon2; import uvicorn; print('All imports OK')" 2>/dev/null || echo "FAIL: imports"
```

Run §0b immediately before this and let it stop the run on a non-`OK` verdict.
The suite is then started unconditionally, because the decision has already
been made by something that can actually stop it.

The old form of this block guarded the command with `${SKIP_TESTS:-}`, a
variable set two stages earlier in a different shell. It was always empty here,
so the guard read as "not skipping" every single time — including on the full
box it was written for. A gate that cannot be reached from the thing it guards
is worse than no gate, because the run reports having been gated.

### The suite can run on a transport with headroom

The slices below exist because this box cannot hold the suite in one process.
A transport that can does not need them. Measured 2026-09-09: the same set that
takes 55 minutes here — when it finishes at all, having been OOM-killed three
times in a row that afternoon — took **4 minutes 6 seconds** on
`Pentester - Kali_MAc` in a single uncapped process. That is not a tuning win,
it is the difference between a result and no result.

**The suite is one stage run across two hosts, not one host's run moved.** Most
test files are pure logic and run anywhere. A minority are *about this host* —
they read its git checkout, drive its terminal, need the `claude` CLI on PATH,
or launch a browser — and sending those away does not make them portable, it
makes them wrong. So:

- **Remote pass:** every file not named in `LOCAL_ONLY` below. ~3,300 tests.
  `test_qa_rules_preflight.py` is in this pass, not the local one: it reads this
  file, and this file was gitignored until 2026-09-09, so it could not travel.
  Now that it is tracked the tests move with it, and they were always portable
  anyway -- they inject their readings through `WC_PREFLIGHT_MEM_KB` rather than
  reading `/proc`, so they test the block's logic and not the box.
- **Local pass:** `LOCAL_ONLY`, run here with the existing capped block.
- **§14 passes only if both pass.** Report both numbers and say which host ran
  which. One figure covering a split run is the same lie as a green total over
  unrun cases (registry #50, #51).

`test_qa_agent_spawn_not_blocked.py` joined the list on 2026-09-10: it runs
`bin/wc-claude.sh` for real, which reads the console's own database, and a QA
node has no reason to have one -- it failed there with `sqlite3.OperationalError:
no such table: ai_machines`, which is a fact about the node and not a defect.

Note what is *not* done to make the remote number look complete: no git
metadata is shipped, and nothing is installed to make a host-specific test
pass. Both were considered and both are the same mistake — moving a test away
from the host its assumptions describe, then adding machinery to compensate.
The browser layer was never what exhausts memory here; it runs one file at a
time and fits.

This section used to say "no Chromium is installed there", and that is no
longer true: measured 2026-09-10, both `Pentester - Kali_MAc` and `Kali3` carry
`/usr/bin/chromium` with Playwright's browsers already downloaded, and the
browser layer ran remotely without anyone installing anything. That is useful
when the §0 gate has made the local browser pass unavailable — but it does not
promote those files out of `LOCAL_ONLY`, because what makes them local is their
assumptions about *this* host, not the absence of a browser.

Two node facts to expect rather than investigate, both of them the node being
unlike this host rather than a defect:

- **`Kali3` has `/usr/bin/claude`**, which fails the two
  `test_qa_proxy_claude_path` fallback cases there, and a live session on that
  node fails two of the `test_qa_sessions_read` family the same way. Four of
  seven remote failures on that node were this shape.
- **A QA node has no console database and no API key**, so the Anthropic
  backend the Backends panel seeds on first load carries only
  `config.ANTHROPIC_MODEL`. Its model picker offers `['', 'claude-sonnet-5']`,
  and any test asserting a *probed* list — anything expecting `claude-opus-5`
  alongside it — cannot pass without a key and network.

And a caution on reading counts from a node at all: the browser tests are flaky
there. Identical code across three consecutive runs of
`BackendsPanelBrowserTests` plus `BackendsTurnCountBrowserTests` gave 5/7, 7/5
and 6/6. Compare failure *signatures* between runs, never totals, and do not
conclude anything about a change from a single run's count — that mistake was
made in this session and retracted.

```bash
# >>> local-only-block
# The one hand-maintained list in this scheme. Everything else is derived as
# "all files minus these", so a test file added to tests/ joins the remote pass
# automatically -- a new file can never fall out of both passes, which is the
# failure the slice-coverage check exists to catch.
LOCAL_ONLY="
tests/test_frontend_browser.py
tests/test_qa_voice_conversation_browser.py
tests/test_qa_orchestrator_ux_shortcuts.py
tests/test_qa_mobile_no_autofocus.py
tests/test_qa_machines_race.py
tests/test_qa_browser_console_errors.py
tests/test_qa_bench_harness.py
tests/test_qa_agent_spawn_not_blocked.py
"
# A name that no longer matches a file is the one way this list can lie: it
# stays in the remote pass (so it still runs -- the safe direction) while
# someone reads this list and believes it ran here. Checked, not trusted.
missing=""
for f in $LOCAL_ONLY; do [ -f "$f" ] || missing="$missing $f"; done
if [ -n "$missing" ]; then
  echo "FAIL: LOCAL_ONLY names files that do not exist:$missing"
  exit 1
fi
echo "LOCAL_ONLY OK ($(echo $LOCAL_ONLY | wc -w) files)"
# <<< local-only-block
```

Pick a node. No hardcoded host: if none qualifies, this prints nothing and the
run falls through to the slices below, which is exactly today's behaviour.

```bash
# >>> node-pick-block
# Floor of 1500 MB and Python 3.13+: below that the node is no better than this
# box, and a different minor risks a package resolving differently -- which
# would make a remote failure indistinguishable from a real one.
WC_QA_FLOOR_MB="${WC_QA_FLOOR_MB:-1500}"
.venv/bin/python - <<'PYEOF' > /tmp/wc-qa-nodes 2>/dev/null
import sqlite3, sys
sys.path.insert(0, ".")
import config
con = sqlite3.connect(config.DB_PATH)
for r in con.execute("SELECT ssh_user, ssh_host, ssh_key_path, name FROM ssh_transports"):
    print("\t".join(str(x or "") for x in r))
PYEOF
# The interpreter to match, read from the venv rather than assumed: a node on a
# different minor can resolve a package differently, which would make a remote
# failure indistinguishable from a real one. An exact match is preferred over
# more memory for that reason -- 2026-09-09 this chose a 4.3 GB node on 3.13
# over a 6.6 GB one on 3.14.
WANT_MINOR=$(.venv/bin/python -c 'import sys; print(sys.version_info[1])')
best=""; best_mb=0; best_exact=0
while IFS=$'\t' read -r user host key name; do
  [ -n "$host" ] || continue
  # -n is load-bearing: without it ssh consumes this loop's stdin and only the
  # first transport is ever probed. Measured -- the first version of this block
  # reported one node and picked it, which looks exactly like "only one
  # qualified".
  probe=$(timeout 20 ssh -n -i "${key:-~/.ssh/id_ed25519}" -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$user@$host" \
    "free -m | awk '/Mem:/{print \$7}'; python3 -c 'import sys;print(\"%d.%d\"%sys.version_info[:2])'" \
    2>/dev/null | tr '\n' ' ')
  mb=$(echo "$probe" | awk '{print $1+0}')
  ver=$(echo "$probe" | awk '{print $2}')
  minor=$(echo "$ver" | cut -d. -f2)
  [ -z "$minor" ] && { echo "  $name: unreachable"; continue; }
  exact=0; [ "$minor" = "$WANT_MINOR" ] && exact=1
  echo "  $name: ${mb}MB python $ver$([ $exact = 1 ] && echo ' (matches the venv)')"
  [ "$mb" -ge "$WC_QA_FLOOR_MB" ] || continue
  [ "$minor" -ge 13 ] || continue
  # An exact-minor node beats any inexact one; among equals, more memory wins.
  if [ "$exact" -gt "$best_exact" ] || { [ "$exact" = "$best_exact" ] && [ "$mb" -gt "$best_mb" ]; }; then
    best_exact=$exact; best_mb=$mb; best="$user@$host"
  fi
done < /tmp/wc-qa-nodes
if [ -n "$best" ]; then
  echo "QA_NODE=$best (${best_mb}MB, exact_minor=$best_exact)"
  [ "$best_exact" = 1 ] || echo "  NOTE: no node matches the venv's Python 3.$WANT_MINOR; a failure there needs checking here before it is believed."
else
  echo "QA_NODE= (none qualified; use the slices below)"
fi
# <<< node-pick-block
```

Sync and run the remote pass. What is sent is always decided by git **on this
host** — never a path list from anywhere else. That is `transport_sync.py`'s
stated security property and it holds here for the same reason: a request may
trigger *that* a sync happens, never *what* is sent.

It ships `git archive HEAD`, so what runs on the node is a **commit**, not the
working tree. The distinction is not pedantry: `git ls-files | tar`, which this
used before, took its manifest from git and then read every path off disk, so a
remote number described a blend of however many sessions had unsaved edits at
that moment and could not be compared with the next run. Uncommitted work is
deliberately not tested remotely — test it here, or commit it first.

```bash
# >>> remote-suite-block
QA_NODE="${QA_NODE:?set from node-pick-block}"
REMOTE_DIR="${WC_QA_REMOTE_DIR:-~/wc-qa-checkout}"
SSH="ssh -i ${WC_QA_KEY:-~/.ssh/id_ed25519} -o BatchMode=yes -o ConnectTimeout=8"

# The tracked tree is CLEARED before it is extracted, and that is not tidiness.
#
# `tar -xzf` over an existing directory only ever adds and overwrites -- it
# never removes. So a file deleted in this repo lived on the node for ever, and
# any test that globs a directory rather than naming files kept finding it.
# Measured 2026-09-10: `web/assets/voice-conversation.js` was deleted here in
# 07d5b41 and was still on kali-3 hours later carrying `app.js?v=53`, so
# test_qa_asset_module_versions reported app.js referenced at two versions and
# three tests failed on a defect that did not exist at HEAD. An earlier run in
# the same session reported `{'56', '54'}` from the same cause and it was
# misattributed to a peer's mid-edit working tree. A stale-file failure is
# expensive precisely because it looks like a real one, and it points at code
# that is already correct.
#
# .venv is preserved: it is minutes of pip installs and it is not part of the
# tracked tree, so nothing in it can go stale in this way. The guard on
# REMOTE_DIR is there because this is an `rm -rf` running over SSH -- an empty
# or absurd value must stop the run, not widen the delete.
case "$REMOTE_DIR" in
  ""|"/"|"~"|"~/"|"$HOME"|"/home"|"/root") echo "FAIL: refusing to clear REMOTE_DIR=$REMOTE_DIR"; exit 1;;
esac
# PIP_USER is set on at least one transport and breaks every venv install with
# "Can not perform a '--user' install"; pip reports it and carries on, so the
# suite then fails on missing imports rather than on anything real.
# `git archive HEAD`, not `git ls-files | tar`. Both take their file list from
# git -- that is the security property, and it is why the old form was chosen --
# but `ls-files` supplies only the *manifest*: tar then reads each path off
# disk, so every uncommitted edit in the tree travelled to the node. A remote
# result therefore described a tree that no commit corresponded to.
#
# Measured 2026-09-12: an uncommitted line in `routes/chats.py` was present in
# the tarball and absent from HEAD, and it produced 20 `UnboundLocalError`
# failures on the node for a defect that did not exist in any committed
# version. Six sessions share this tree, so at any moment the working tree is a
# blend of several half-finished changes; a number measured against it cannot be
# compared with the next run, which is what a baseline is for.
#
# Registry #103 reached this conclusion and stopped one step short of applying
# it: "A clean `git archive` export of HEAD is the right way to rule out
# working-tree contamination." That was written about *investigating* a stale
# node and never made it into the sync the same section describes.
#
# `git archive` is also the stricter of the two on the property the old form
# was picked for: the paths come from a commit's tree and cannot come from
# anywhere else at all. It drops one entry `ls-files` reports -- the
# `.claude/worktrees/db-modularize` gitlink -- which is a nested worktree that
# should never have been shipped (see tests/test_qa_transport_sync.py on mode
# 160000). 468 real files either way.
#
# The consequence to accept, rather than discover: uncommitted work is no
# longer tested remotely. That is the point. Test your own changes here, or
# commit them first.
git archive --format=tar.gz HEAD \
  | $SSH "$QA_NODE" "mkdir -p $REMOTE_DIR \
      && find $REMOTE_DIR -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} + \
      && tar -xzf - -C $REMOTE_DIR" || exit 1
$SSH "$QA_NODE" "cd $REMOTE_DIR && [ -x .venv/bin/python ] || python3 -m venv .venv; \
  PIP_USER=0 .venv/bin/pip install -q -r requirements.txt \
  && PIP_USER=0 .venv/bin/pip install -q pytest pytest-subtests quickjs httpx requests playwright pyflakes" || exit 1

# The remote pass: everything not in LOCAL_ONLY, as --ignore arguments.
IGNORES=""
for f in $LOCAL_ONLY; do IGNORES="$IGNORES --ignore=$f"; done
# setsid, because a dropped SSH channel must not take the run with it.
$SSH "$QA_NODE" "cd $REMOTE_DIR && rm -f /tmp/wc-qa.log && setsid nohup \
  .venv/bin/python -m pytest -q -rs -p no:cacheprovider $IGNORES \
  > /tmp/wc-qa.log 2>&1 < /dev/null & echo started" || exit 1
echo "started on $QA_NODE; poll /tmp/wc-qa.log there"
# <<< remote-suite-block
```

The remote pass's skips are not this suite's usual six. Expect `WC_LIVE_TESTS`
opt-ins plus `app harness unavailable` — that harness needs pieces this node
does not have. Read the reasons, not the count: the six-skip rule below applies
to the **local** pass, where it is a statement about this box.

The local pass is the existing block with the list named. `PYTEST_SLICE` must
be **one space-separated line**, which is why `$LOCAL_ONLY` is collapsed rather
than used directly: it is newline-separated, and passed through as-is those
newlines survive into `CMD`, so `bash -c` reads the first line as the pytest
invocation and each later line as a command of its own. Measured -- the local
pass silently became a whole-suite run and was at 29% before anyone looked. A
slice that quietly turns into a different run is the exact failure this stage
has been bitten by before.

```bash
PYTEST_SLICE="$(echo $LOCAL_ONLY)"   # unquoted on purpose: collapses to one line
WC_TEST_MEMORY_MAX=2G                # it is the browser layer, so size for browsers
```

Naming the files is also what makes the browser layer run here at all: an
`--ignore` for a path that is *also named* on the command line does not apply,
which is the trap documented below being used deliberately rather than tripped
over.

### The suite runs inside a memory cap, and in slices

Two separate problems, and they need different answers. Lowering priority
solves neither: `nice` and `ionice` ration CPU and disk, while the failure here
is memory, and the kernel's OOM killer ignores niceness entirely — it chooses by
resident size, which on this box means the live server at around 860 MB rather
than the test run that asked for the memory.

**The cap makes failure safe.** A `systemd-run` scope with `MemoryMax` set
confines the kill to its own cgroup: if the run outgrows the ceiling, the run
dies and the site does not. Measured on this host — the memory controller is
delegated to the user slice (`cpu memory pids`), and a scope capped at 128 MB
attempting a 400 MB allocation was killed with exit 137 while host
`MemAvailable` was unchanged either side. It does **not** make the suite fit;
it makes overshooting survivable.

**Slicing makes it pass.** On 2026-09-07, with roughly this much headroom, four
sliced runs completed — 520, 263, 757 and 788 passed — while a single whole-suite
invocation was killed twice, once at about 19% and once inside the browser
file. Excluding the browser layer alone was not enough: the run that died at
19% had already excluded it. Peak demand is what matters, and one slice at a
time is what lowers it.

```bash
# >>> capped-run-block
# WC_TEST_CMD is the seam the test uses; a real run leaves it unset.
CAP="${WC_TEST_MEMORY_MAX:-1G}"
# The browser exclusions are in the DEFAULT, not only in the slices. Left to
# the slices alone, invoking this with PYTEST_SLICE unset runs the whole suite
# including the browser layer -- which is the invocation that was killed twice.
# The safe thing has to be what you get for free; the browser files are run
# deliberately, by naming them in PYTEST_SLICE.
SKIP_BROWSER="--ignore=tests/test_frontend_browser.py --ignore=tests/test_qa_voice_conversation_browser.py"
CMD="${WC_TEST_CMD:-.venv/bin/python -m pytest -rs $SKIP_BROWSER ${PYTEST_SLICE:-}}"

status=0
systemd-run --user --scope -q -p MemoryMax="$CAP" -p MemorySwapMax=0 \
  -- bash -c "$CMD" || status=$?

# 137 is SIGKILL: the cgroup ceiling, not your code. Reporting it as a test
# failure would send someone hunting a bug that does not exist -- and reporting
# it as a pass would be worse.
if [ "$status" -eq 137 ]; then
  echo "RUN KILLED BY THE CAP (exit 137, MemoryMax=$CAP). This is NOT a test result."
  echo "The cap did its job: the live server was never a candidate. Re-run a"
  echo "smaller slice, or wait for headroom. Do not quote this as green or red."
  exit 1
fi
exit "$status"
# <<< capped-run-block
```

Slices. Each is one invocation of the block above with `PYTEST_SLICE` set; run
them in order and stop at the first genuine failure:

```bash
PYTEST_SLICE="tests/test_qa_[a-c]*.py"
PYTEST_SLICE="tests/test_qa_[d-l]*.py"
PYTEST_SLICE="tests/test_qa_[m-r]*.py"
PYTEST_SLICE="tests/test_qa_[s-z]*.py"
PYTEST_SLICE="$(ls tests/test_*.py | grep -v '/test_qa_' | grep -v 'test_frontend_browser' | tr '\n' ' ')"
```

The fifth slice is expressed as a listing rather than a glob because "every
test file that is not a `test_qa_` one" has no glob, and a slice set that
silently omits files would report green over tests nobody ran — the same class
of failure as registry #51, where 43 unrun cases sat inside a passing total.

`test_frontend_browser.py` is subtracted from that listing by name, and the
reason is the trap directly below: a listing **names** its files, and a named
file defeats the `--ignore` in the default command. Without that `grep -v` the
fifth slice quietly became the browser run — measured, not predicted: it was
still going after ten minutes while the four slices before it took two to five
minutes each. The default exclusion protects the case where no slice is given;
it cannot protect a slice that asks for the file outright.
`tests/test_qa_rules_preflight.py` asserts the five slices cover every file
exactly once, so adding a file outside them fails a test rather than
disappearing.

**The browser layer is run separately, and only with headroom.** Chromium plus
a uvicorn per browser class is the single largest consumer in the suite, and it
is what killed the whole-suite attempts. Run it as its own capped invocation
when the box is quiet, never folded into a slice above:

```bash
# Naming the files explicitly is what overrides the default exclusion: an
# --ignore for a path that is also named on the command line does not apply.
PYTEST_SLICE="tests/test_frontend_browser.py tests/test_qa_voice_conversation_browser.py"
WC_TEST_MEMORY_MAX=2G   # browsers need more than a slice does
```

`MemoryHigh=` may be substituted for `MemoryMax=` when you would rather a long
run went slow than died: it throttles and reclaims instead of killing. Use it
when the run matters more than the clock, not as a default — a run that swaps
for an hour and then reports is worse than one that stops in two minutes.

**Any other interpreter is wrong — not just `python3`.** This rule named only `python3` and cost a
session a full misdiagnosis for it: they ran `python`, which is a *different*
binary (`/usr/bin/python`), read the rule as not applying, and reported 34
failures and 91 skips as real. All 34 vanish under the venv. Measured:

    python            -> /usr/bin/python       quickjs absent
    python3           -> /usr/bin/python3      quickjs absent
    .venv/bin/python  -> the venv              quickjs present

So the trap is **anything that is not the venv**, and naming one example of it
invited the reading that the others were fine. `quickjs` is absent from every
system interpreter, and `playwright` behaves differently again: it is importable
under system Python (the Debian package) but its driver resolves to
`/usr/bin/node`. That path now exists — nodejs 24.19.0 was installed
2026-09-01 22:12 — so the browser layer *can* run outside the venv today, which
makes this trap quieter rather than gone: the failure mode has moved from "all
browser classes skip" to "some do, depending on what is installed this week".
The venv is the only interpreter whose behaviour is a property of the
repository rather than of the box.

`-rs` is not optional: a skip that nobody prints is invisible in an aggregate
total, and a total that hides unrun cases is worse than no total (registry #50).
On this suite a trustworthy run currently shows **exactly 6 skips**, all
`WC_LIVE_TESTS=1` opt-ins that spend real tokens. Any other skip count means
something stopped running — investigate the number, do not report it.

**Two cheap checks, one positive and one negative.** Neither needs a flag or
anyone remembering this paragraph:

```bash
# Positive: is this interpreter even capable of a meaningful run?
python -c "import quickjs"        # ImportError => wrong interpreter, stop
```

```bash
# Negative: a browser test file that finishes instantly has NOT run.
tests/test_frontend_browser.py    system python  -> 54 skipped in 0.47s
tests/test_frontend_browser.py    .venv          -> launches browsers, minutes
```

The wall-clock tell is the more durable of the two, and deliberately so: with
nodejs present since 2026-09-01 22:12, *which* browser classes skip is now a
property of the box and the count drifts. Elapsed time does not — a real
browser class cannot finish in half a second. Use the import check before a
run and the clock afterwards; the first catches the wrong interpreter, the
second catches a run that looked green. (Both from cweb4, who arrived at them
after reporting 34 failures that did not exist.)

```bash
.venv/bin/python -c "import importlib.util as u; print('quickjs:', bool(u.find_spec('quickjs')))"
.venv/bin/python -c "from playwright._impl._driver import compute_driver_executable as c; import pathlib; p=c(); p=p[0] if isinstance(p,(list,tuple)) else p; print('driver:', pathlib.Path(p).exists())"
```

Both must print `True` before any browser result is quoted as evidence.

## 15. Docs + version sweep

- `README.md` reflects current endpoints, env vars, architecture diagram, and security model.
- **Architecture diagram parity.** `README.md` includes an ASCII architecture diagram showing: Browser → FastAPI → (SQLite, Claude Code subprocess). Updated when routes, storage, or subprocess interaction changes.
- `.env.example` (if created) lists every var `config.py` reads with defaults and comments.
- No stale version strings anywhere:
```bash
grep -rn --include='*.py' --include='*.html' -E '[0-9]+\.[0-9]+\.[0-9]+' . | grep -v "$(python3 -c 'import config;print(config.VERSION.split("_")[1])')" || echo "no stale versions"
```
- All documentation in English — no non-English prose in committed files.

## 15a. Changelog update (mandatory — never `⏭️ SKIP` on a run that changed code)

`CHANGELOG.md` is the operator-facing record of what has changed and why. It is
the one artefact read by someone who was not in the room when the change was
made, so it is updated **every run that touches code**, not only at a release.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), newest first,
sections `Added` / `Changed` / `Fixed` / `Security` / `Removed` / `Deprecated`
(plus `Testing` and `Documentation` where they carry real news). English only,
per the global convention at the top of this file.

**Rules:**

1. **Every run that changed code writes an entry.** If work landed under
   `## [Unreleased]`, put it there. If `config.VERSION` was bumped this run,
   rename `[Unreleased]` to the new version with today's date and open a fresh
   empty `[Unreleased]` above it.
2. **A version in `config.VERSION` must exist in `CHANGELOG.md`.** The check
   below fails the stage when it does not.
3. **Skipped version numbers are recorded, not hidden.** 0.4.0 and 0.7.1 were
   never shipped; a gap in the sequence must read as deliberate rather than as a
   lost release.
4. **Write why, not just what.** Several defects here were invisible from the
   outside — a log file that grew while recording nothing, a CSP header gated on
   a nonce nothing set. An entry that says only "fixed logging" is worth less
   than the line it occupies. Where a §16 registry row exists, the changelog
   entry is its plain-English form; the registry keeps the forensic detail.
5. **Never rewrite a released section.** Corrections go in the current
   `[Unreleased]` block, naming what was wrong, in the same spirit as §16's
   append-only registry.

```bash
# The current version must have a section
V=$(python3 -c 'import config;print(config.VERSION.split("_")[1])')
grep -q "^## \[$V\]" CHANGELOG.md \
  && echo "PASS: CHANGELOG.md has a section for $V" \
  || echo "FAIL: no '## [$V]' section in CHANGELOG.md"

# An entry must exist for work landed since the last release commit
git diff --stat HEAD -- CHANGELOG.md
git log --oneline -1 -- CHANGELOG.md

# Every released version must be reachable from the file
python3 - <<'PY'
import re, subprocess, pathlib
log = subprocess.run(["git", "log", "--all", "--pretty=%H"],
                     capture_output=True, text=True).stdout.split()
seen, prev = [], None
for commit in reversed(log):
    src = subprocess.run(["git", "show", f"{commit}:config.py"],
                         capture_output=True, text=True).stdout
    m = re.search(r'^VERSION\s*=\s*"WebConsole_([^"]+)"', src, re.M)
    if m and m.group(1) != prev:
        seen.append(m.group(1)); prev = m.group(1)
changelog = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
missing = [v for v in seen if f"## [{v}]" not in changelog]
print("FAIL: versions shipped but not in CHANGELOG.md:", missing) if missing \
    else print(f"PASS: all {len(seen)} shipped versions documented")
PY
```

Pass: a section exists for `config.VERSION`; every version that ever appeared in
`config.py` is documented; and any run that changed code left a changelog entry.

## 16. Post-release bug watch

Append every field-found bug to the registry below (cumulative — append, never delete): id, symptom, root cause, fix, regression test.

| # | Symptom | Root cause | Fix | Regression test |
|---|---------|-----------|-----|-----------------|
| 1 | Streaming endpoint registered as GET but handler uses `request.json()` (POST body) | Route was `methods=["GET"]` while the handler read `request.json()` | Changed to `methods=["POST"]`, UI sends `POST` with `{content}` in body | N/A — structural; caught by compilation + smoke test |
| 2 | `_build_cmd` used `str(Path().resolve())` as `--session-id` placeholder | Copy-paste error from a previous draft | Replaced with `uuid.uuid4()` | N/A — structural; caught by linter review |
| 3 | `chat_update()` SQL parameter ordering was wrong | `UPDATE chats SET {sets}, updated_at = ? WHERE id = ? AND owner_id = ?` with `vals + [_now()]` sent `_now` in the wrong position | Fixed to `[_now(), chat_id, owner_id]` matching `? ? ?` positions | N/A — caught by DB smoke test (§6) |
| 4 | `slug_pattern` regex too strict (`[a-z0-9][a-z0-9-]{1,38}[a-z0-9]`) requiring 5+ chars minimum | Three-char slugs like `a-1` were rejected | Fixed to `[a-z0-9-]{3,40}` with leading/trailing dash rejection | N/A — caught by DB smoke test (§6) |
| 5 | Authentication rejection responses omitted the mandatory security headers | `AuthMiddleware` wrapped `SecurityMiddleware`, so early 401 responses bypassed header injection | Reordered middleware so `SecurityMiddleware` is outermost | `test_security_middleware_wraps_auth_rejections` plus deployed 401 header smoke |
| 6 | The CLI-session resume route accepted arbitrary session IDs and could create duplicate linked chats | The path parameter was trusted without checking discovered CLI sessions or existing links | Require an exact discovered session ID and return an existing linked chat when present | `test_resume_handler_rejects_unknown_cli_session`; `test_resume_handler_reuses_existing_linked_chat` |
| 7 | The blocking message endpoint persisted a user prompt before a failed model turn | User and assistant writes were separate and the user write preceded `run_turn()` | Persist the completed user/assistant pair atomically after successful execution | `test_submit_message_persists_user_and_assistant_messages` |
| 8 | Failed streams could emit a trailing `done` event after an error | The app forwarded proxy `done` even after recording an error frame | Stop the generator after an error and suppress completion/persistence | `test_failed_retry_does_not_duplicate_user_prompt` |
| 9 | Oversized prompts reached the runner before handler-level rejection | Prompt limits were enforced only inside runner functions | Reject oversized blocking and streaming requests before invoking the runner | `test_submit_message_rejects_oversized_prompt_before_runner`; `test_oversized_stream_prompt_is_rejected_before_runner` |
| 10 | The separately deployed proxy could run stale source and failed after an image-only recreation | `claude_proxy.py` was not copied into the application image | Package `claude_proxy.py` in the image and recreate both containers from the same image | Deployment hash comparison plus final live proxy stream |
| 11 | A client disconnect after handshake but before a turn produced an unhandled `IncompleteReadError` | The turn-frame read handled timeout but not EOF | Handle incomplete reads and connection errors as a normal early disconnect | `test_proxy_handles_disconnect_before_turn` |
| 12 | Model validation inconsistent between blocking/streaming handlers | Blocking handler validated before stripping, streaming stripped first | Normalised both to strip-first-then-validate | compile check |
| 13 | `_sanitize_session_id` blocked valid CLI session IDs that contained `..` | Path traversal check rejected non-traversal session IDs | Relaxed validation; work_dir always below PROJECTS_ROOT | `test_resume_handler_rejects_unknown_cli_session` |
| 14 | `AuthMiddleware` class deleted but registration line remained | Accidental deletion during SSRF protection refactor | Restored middleware class | `test_security_middleware_wraps_auth_rejections` |
| 15 | Editing any AI machine returned `400 No valid fields to update`; the UI showed only `(400 Bad Request)` | `_saveMachine` sent `port`/`base_url`/`description` after their inputs were removed from `index.html`, and `handle_machine_patch` rejects the whole body if any key falls outside `_MACHINE_ALLOWED_FIELDS` (`issubset`, all-or-nothing). The reason was invisible because the API returns `{"error": ...}` while the client read `data.detail`/`data.message` | Build the request body from the fields the form actually collects; read `data.error` first when parsing failures | `MachinePatchTests` (6 cases); `test_machine_form_script_matches_markup` guards markup/script drift |
| 16 | Every turn failed with "Connection lost during streaming"; proxy logged `bad handshake`, runner raised `IncompleteReadError` | `_load_settings_from_db()` runs before `config.validate()`, so the app's `PROXY_TOKEN` comes from the DB, while `launch.sh` generated a fresh random `WC_PROXY_TOKEN` on every launch and never persisted it. The two could only agree by accident | `launch.sh` resolves the token DB → file → generate and persists it with `umask 077`; warns when a proxy is already listening that may hold a different token | Live handshake + blocking turn + SSE turn against an isolated instance (§13) |
| 17 | Turns rejected with "There's an issue with the selected model … may not exist" | Default model was `claude-sonnet-4-20250514`, retired 2026-06-15. The id was hardcoded in `config.MODEL_NAME`, the `ai_machines` schema default, the `index.html` datalist, the `app.js` fallback and `.env.example` | Moved all defaults to `claude-sonnet-5` (fallback `claude-haiku-4-5-20251001`) | Live turn returns a reply in ~6s with `"model":"claude-sonnet-5"` |
| 18 | Admin database export stalled all other requests, including live SSE streams | `db_backup()` ran `sqlite3.backup()`, the file read and the gzip pass directly on the event loop | Extracted `_db_backup_sync()` and wrapped it in `asyncio.to_thread` | Backup → restore → chat-survives round-trip (§6/§11) |
| 19 | `starlette` imported directly but absent from `requirements.txt` | Relied on the transitive FastAPI pin, so a FastAPI bump could silently move the version behind a direct import | Pinned `starlette==1.6.0` explicitly | §5 import-vs-requirements cross-reference |
| 20 | `POST /api/admin/import` returned 500 unconditionally — database restore had never worked | `handle_db_restore` calls `await request.form()`, which Starlette refuses without `python-multipart`. The package was neither installed nor in `requirements.txt`, and §5's import scan missed it because nothing imports `multipart` by name | Added `python-multipart==0.0.32` to `requirements.txt` and installed it | Live export → import → chat-survives round-trip against an isolated instance (§13). Note: a name-based import scan cannot catch this class of runtime-only dependency |
| 21 | Every turn silently started a new Claude session, so conversations never carried history | `--resume <id>` / `--session-id <id>` were appended *after* the `--` sentinel in both spawn paths, so the CLI treated them as prompt data rather than options. `-p/--print` is a boolean and the prompt is positional, so the sentinel was protecting the wrong operand | Session flags moved before the sentinel; the prompt moved after it, which also protects a prompt beginning with a dash | `test_build_cmd_uses_argument_list_and_resume`, rewritten — the previous version asserted the broken order and so locked the bug in |
| 22 | No Content-Security-Policy header was served on any response | `SecurityMiddleware` emitted the header only when `request.state.csp_nonce` was set, and the code that set it had been removed; nothing else assigned it | Emit the policy unconditionally with `script-src 'self'` and delete the nonce plumbing — both templates load external scripts only | Live header check against the running instance; `test_qa_coverage` security-header assertions |
| 23 | Every authenticated account passed the admin gates | `auth.session_new` hardcoded `role: "admin"` and never read `users.role`, making the checks on `/api/admin/*` decorative | `session_new` takes the role and `handle_login` passes the DB value | `test_qa_transcripts` / auth suites; a non-admin session now receives 403 from `handle_settings_patch` |
| 24 | `PATCH /api/settings` was ungated while writing the session secret, proxy token, model API key and `projects_root` | The handler had no admin check, and `projects_root` is the sandbox boundary `runner.py` validates every `work_dir` against | Admin gate plus `_validate_projects_root`, which requires an existing directory under a configured base | `AdoptSessionCwdTests`; non-admin rejection asserted directly |
| 25 | Half of a long conversation was unreachable in the transcript viewer | When one window held more than `MAX_TURNS`, the page was capped but `start` still pointed at the head of the whole window, so the caller paged back from below the discarded turns and no call ever returned them. Walking a 2000-turn transcript recovered 1000 | The readers return `(byte offset, turn)` pairs and the cap moves `start` to the first turn kept | `test_paging_recovers_everything_when_a_window_exceeds_the_cap` — the earlier whole-file test used records too large for a window ever to exceed the cap, so it passed against the bug |
| 26 | A conversation started on the local gateway could not be moved to an Anthropic model: `400 messages: text content blocks must be non-empty` | The gateway streams one reply as several assistant records and the first can carry an empty text block. It replays those happily; the Anthropic API rejects the whole request. One conversation held 2074 such records, another 1778; conversations written by Claude models held none | `transcripts.repair_if_needed()` removes those records and relinks each child to its nearest surviving ancestor, backing the file up first. Runs before any turn on an Anthropic backend | `TranscriptRepairTests` (10 cases); the affected conversation was repaired and then resumed successfully on `claude-sonnet-5` |
| 27 | Conversation reordering appeared not to save | Three faults at once: `state.chats` was never refreshed, so any later render redrew the old order; `apiFetch` resolves for 4xx/5xx so a rejected save was silent; and a drag inside Favourites wrote positions across the whole sidebar | Refresh after a successful save, check `response.ok`, and scope a reorder to its own section | `test_qa_chat_order` (21 cases, four mutations caught); drag verified in a browser |
| 28 | Every `PUT` route was reachable cross-site | `CsrfMiddleware._MUTATING` listed POST, PATCH and DELETE but not PUT, so the new reorder endpoint — and any future PUT — was unguarded | Added PUT to the guarded set | A no-token `PUT /api/chats/order` returned 200 and reordered before the fix; it returns 403 after |
| 29 | Every `_log.info` in the project was silently discarded, so none of the §3 (A09) login, chat-creation or turn-timeout records existed | The server runs as `python3 -m uvicorn app:app`, which *imports* app.py, but the only `logging.basicConfig` sat inside `if __name__ == "__main__"` and never ran. Uvicorn configures only its own loggers, leaving root with no handler, and logging's last-resort fallback emits WARNING and above. `logging.conf` was in the repo but referenced by nothing. Nothing looked wrong because `logs/webconsole.log` kept growing — it was the shell's stdout redirect collecting uvicorn's access lines, and held no `wc.*` record at all | `_configure_logging()` runs at import, loading `logging.conf` when present (`disable_existing_loggers=False`, or uvicorn's own loggers would be silenced) and falling back to a stream handler | `test_qa_logging` (8 cases); removing the import-time call fails 3 of them. Live: `wc.app: login user=…` now appears in the file, where `grep -c "login user="` returned 0 before |
| 30 | A first attempt at #29's tests failed 794 of 1251 tests, none for a real reason | `logging.config.fileConfig` closes every existing handler, pytest's log-capture handlers included, so any test that called it left the rest of the run writing to closed streams | The two tests that reconfigure logging run in a subprocess | Full suite 1251 passed with the file included; the same suite was 794 failed with the in-process version |
| 31 | `runner.py`'s records — turn launches, proxy connect failures, handshake problems, timeouts — never reached the log file, so the whole turn-execution path §3 (A09) asks for was absent even after #29 was fixed | `runner.py` logs through **loguru**, whose sinks are a separate system that `logging.conf` cannot reach. Its output went to stderr and died with whatever shell started the server. Proved rather than inferred: a probe emitted through `runner._log` was absent from the file while one through `wc.app` was present | A loguru→stdlib sink in `app.py` forwards records onto the `wc.runner` logger the config already declared, and drops loguru's default stderr sink so lines are not printed twice. Ten `{}`-style call sites left untouched. `logging.conf` also gained the `wc.auth` and `wc.db` loggers, which had been reaching the file only by propagating to root | `test_qa_logging_setup` (9 cases). One derives the logger list from the source, so a new `wc.*` logger that is not declared fails the suite |
| 32 | Proxy mode — the default — logged nothing on a successful turn, so a turn that worked left no trace and one that hung left nothing to say where it stopped | The direct path logged every launch; `_proxy_turn` logged only failures | `_log.info("proxy turn chat=… work_dir=… model=…")` at the head of the proxy path, matching the direct one | `TurnTraceabilityTests`; live, a turn now traces `wc.app: transcript_repaired` → `wc.runner: proxy turn` → `wc.app: usage_recorded` |
| 33 | The server vanished mid-session several times, and three separate features silently did nothing | Neither long-lived process was supervised. A backgrounded `uvicorn … &` dies with its parent shell; and nothing reloads `claude_proxy.py`, so it served an hour-old copy while the file moved on — which is how the proxy token handshake, `ANTHROPIC_BASE_URL`, and usage accounting each broke with no error anywhere | `systemd --user` units for both with `Restart=always`, linger enabled so they survive logout, `WC_EXEC=1` making `launch.sh` exec uvicorn rather than background it, an `ExecStartPre` that reclaims port 9000 only from a process whose cmdline is genuinely `claude_proxy.py`, and a 30s health timer for the wedged case `Restart=always` cannot see | §17. `kill -9` on each process and `SIGSTOP` on the app all recovered to HTTP 200 with new PIDs. Both guards fired for real during install: an orphaned proxy held the port, and `StartLimitBurst` stopped the resulting restart loop |
| 34 | The health check restarted a **healthy** server, and would have done so every 30 seconds for ever | It probed `https://127.0.0.1:443`, but the server binds the tailnet address, so every probe was refused and read as unhealthy. Strictly worse than having no health check | Resolve the address actually listening on 443, falling back to the tailnet address then loopback. Also stopped the shell fallback appending a second status code after curl had already written one, which logged `HTTP 000000` | The healthy case is now an explicit step in §17, not only the broken one — a restarter never tested against a working server is a liability |
| 35 | A run of the full suite reported **865 failed / 515 passed** while every file passed on its own. The failures named `IsolatedAsyncioTestCase`s that had nothing to do with the change | The browser fixture acquired playwright in `setUp` and released it in `tearDown`. unittest skips `tearDown` when `setUp` raises, and `_login` raises on any slow boot -- so one login timeout leaked playwright. Its sync API drives an asyncio loop through greenlets, so a playwright that is never stopped leaves that loop flagged as the **running** loop for the thread, and every async test after it died on `Runner.run() cannot be called from a running event loop`. The report blamed ~850 innocent tests and hid the one that broke | `addCleanup` registered immediately after each resource is acquired (LIFO, so the browser closes before playwright stops), and `addClassCleanup` for the server and temp dir, which `setUpClass` leaked the same way when its boot loop raised | `test_qa_browser_fixture` (6 cases), driven with a stubbed playwright so it needs no browser. End to end: the same command that produced 865 failures produces 0 |
| 36 | The device-alert tests failed on a machine running other Claude sessions, and passed on a quiet one | `/api/supervisor` does not read only its own database -- it merges the live CLI sessions under `~/.claude`. The test server inherited the real `HOME`, so its waiting count moved with whatever the other five agents on this machine were doing. Alerts fire on a **rise** in that count, so an unrelated session answering a question in the same 15s poll window netted the test's own seed out to zero | The fixture gives its server a `HOME` of its own inside the temp directory, with empty `.claude/sessions` and `.claude/projects`. Also stops the suite reading the developer's real transcripts | The class went from 6 failed / 1 passed to 7 passed, and from 167s to 93s -- it had been spending the difference on 90-second timeouts |
| 37 | Later tests in a browser class failed with `ERR_CONNECTION_REFUSED` against a server that had started fine | The fixture passed `stdout=subprocess.PIPE` and nothing ever read it. An unread pipe holds 64K; past that uvicorn blocks for ever writing its own access log and stops answering. The symptom names a port and nothing else | Server output goes to a file in the temp directory, which cannot fill, and `setUp` now fails with the exit code and the log tail when the server has died | Whole browser file 26 passed, having been 21 passed / 5 failed |
| 38 | `test_routine_output_raises_no_alert_at_all` compared the tab title before and after, and failed whenever an earlier test had left a question waiting | The baseline was read straight after `domcontentloaded`, before the first supervisor poll -- so it was the static title from the HTML, and the poll landing, not the seed, moved the count. Its sibling documents this exact trap and waits it out | Wait for the app shell and the first poll before reading the baseline. `test_a_notification_fires_for_a_new_waiting_agent` had the same race with no way to observe it, since an empty queue reads `WebConsole` both before and after the poll: it now seeds one question first, so the count in the title is proof the poll ran | Both tests pass, and the class is order-independent |
| 39 | The Server statistics page showed a "live" host reading that never changed, so the number on screen was whatever it had been when the tab was opened | `loadServer()` was wired to the tab switch and to its two `<select>`s, and to nothing else — there was no refresh at all. The sampler was working correctly the whole time, which made the page look right and be wrong. Nearly misdiagnosed as a dead sampler: the stored samples appeared to stop 34 minutes before the last restart, until reading the timestamps as UTC against a BST clock showed the newest was 11 seconds old | A guarded 30s interval, started when the Server tab opens and cleared when it closes or the dialog does. Skips the fetch when the page is hidden or the panel is not the visible one. Refreshes are quiet — no skeletons, and a failed poll keeps the last good reading | `test_qa_server_refresh` (17 cases) plus `ServerPanelBrowserTests` (5), which counts the requests the page actually makes. Verified negatively: with the polling call neutered in a served copy of `app.js`, the request count stays at 2 across 38s and the test fails |
| 40 | Every chart on both statistics pages read an hour early — 21:00 in Lisbon was labelled 20:00 | Timestamps are stored in UTC and were bucketed in UTC. The wrong label was the visible half; the day and month buckets also split at UTC midnight, so work done after 23:00 local was filed under the next day, which no label would have revealed | Bucket on `replace(datetime(created_at,'localtime'),' ','T')` inside `_bucket_expr`, the one expression all three series share. `localtime` resolves the zone per timestamp, so it follows DST — Portugal is +1 in summer and +0 in winter, and a fixed offset is wrong for half the year. Range filters deliberately stay UTC: a span has no timezone. The key keeps its shape and width, so no client change | `test_qa_usage_localtime` (8 cases) sets TZ explicitly and restores it, so it cannot pass by accident on a UTC box; `test_qa_usage_halfhour` derives expected keys instead of hardcoding them. Verified against the old expression in-process: `20:23:54Z` gave `T20` and one day bucket, now gives `T21` and two. Live after deploy, the newest slot reads 21:00 against a 21:14 WEST clock |
| 41 | For 37 minutes the live server accepted requests, served HTTP 200, and silently wrote nothing — no messages, no usage, no host samples. Noticed only because the Server statistics page stopped gaining history | Its single long-lived aiosqlite connection was left holding a read transaction opened before another process wrote, so in WAL mode it could never upgrade to a writer again: every write returned `database is locked`, permanently, on that connection alone. A fresh connection took the write lock instantly, which is what made it look fine from outside. Triggered by me: a verification script called `db.init()` against the **production** database while the server was running, and init writes (migrations). A peer's hung diagnostic had the same file open. Both are the same mistake — a second process on the live database | Restart clears it (writes resumed within one sampling interval). The lasting lesson is procedural: verification runs against a throwaway `WC_DB_PATH`, never the live file. `bin/wc-health.sh` did not and could not catch this — it probes `/login` for a 200, which stayed 200 throughout | Confirmed by the shape of the outage: `usage_events`, `messages` and `system_samples` all stopped within 100 seconds of each other and resumed together after a restart. **Open gap:** the health check still cannot see a write-only failure; a liveness probe that only reads will pass through the next one exactly the same way |
| 42 | The production log carried interleaved and future-stamped lines from processes nobody was watching, making it misleading about ordering during an incident | `logging.conf` names the log file with an absolute path, so every server the test suite spawns inherited it and appended to the real log. Not cosmetic: it is the first artefact anyone opens when something breaks, and it cost time during #41 before the foreign entries were recognised | The filename comes from `config.LOG_FILE` (`WC_LOG_FILE`, defaulting to the existing path) and reaches the handler through `fileConfig`'s `defaults`, with the config naming it `%(logfile)s`. Chosen over rewriting the handler afterwards because that needs a second `fileConfig` call, and `fileConfig` closes every existing handler — registry #30. The browser fixture and the logging tests' own subprocesses now redirect too | `test_qa_log_path` (13 cases). Verified negatively rather than assumed: the same probe grows the production log by 53 bytes without the env var and by 0 with it. Also asserts the unset case still resolves to the production path — a test writing somewhere odd is a nuisance, a deployment logging where nobody looks is the real failure |
| 43 | **Open.** A corrupt or missing `logging.conf` silently drops the file handler: `_configure_logging` falls back to `basicConfig`, everything goes to stdout, and the file log simply stops existing | The fallback was written to keep the app booting rather than to keep it observable, which is the right priority and the wrong outcome — it trades a hard failure for a degraded state that looks identical to a working one, and is discovered only when someone needs the artefact it stopped producing | Not fixed. Raised while changing the log path and deliberately scoped out: it deserves its own fix and its own test rather than a paragraph inside an unrelated change | Same shape as #41 — a silent degradation behind a healthy-looking surface. The fix probably wants the fallback to still attach a file handler, and a test that a broken config does not cost you the file |
| 44 | Proving §17 case 1 turned one crash into two stop/start cycles: `kill -9` at 22:48:49, systemd's `Restart=always` brought it back at 22:48:52, and `wc-health.sh` stopped it again at 22:48:55 | Two restarters with no knowledge of each other. Boot takes over ten seconds (db.init, WAL recovery, TLS) while `ATTEMPTS × GAP` gives up in about nine, so the health check reliably interrupted a restart systemd had just begun. A first attempt at a boot grace made it worse by failing *unsafe*: when `MainPID` named a process that no longer existed the age was unknowable, and it treated that as "no grace" — which is exactly the state during systemd's own restart | Three guards, all deferring rather than acting: skip while `ActiveState=activating` or `SubState=auto-restart`; skip while the main process is younger than `WC_HEALTH_BOOT_GRACE` (45s), **failing safe** when its age cannot be determined; and, immediately before restarting, skip when `/proc/<MainPID>` has gone, because a process that has already exited is `Restart=always`'s job and not the wedged case this script exists for | `tests/test_qa_health_check.py` (11 cases, through a `WC_SYSTEMCTL` seam so the decision is observable without restarting production). Live: §17 cases 1–4 all pass, case 1 now producing exactly one `Scheduled restart job` and no competing `Stopping` |
| 45 | A health check that only reads cannot see a dead write path — registry #41 served HTTP 200 for 37 minutes while writing nothing | `/login` returns 200 whether or not the server can write. `messages` and `usage_events` are useless as liveness signals because silence in either is normal | `python3 -m sysstats --pid <pid>` classifies `system_samples` freshness — the only table written unconditionally on a timer — into ok/warming/stale/unknown, and `wc-health.sh` restarts on `stale` alone. Four states rather than a boolean because a just-restarted server inherits old rows, and a boolean would restart it and then restart it again (#34). The probe opens the database `mode=ro` so it physically cannot repeat the mistake that caused #41, and declines to act when its `config.DB_PATH` disagrees with the file the server actually holds open | `tests/test_qa_sysstats.py` (WriteHealthTests, ProbeTests, CliTests); live, a stale fixture produces a restart request and a fresh one does not |
| 46 | Restarting the server killed every other session's test servers, and the failures were reported against the tests rather than against the thing that killed them | `launch.sh` cleared a previous instance with `pkill -f "uvicorn app:app"`. That substring is in every test server's command line too, and `launch.sh` runs on every `systemctl restart` — so proving the §17 recovery cases (ten restarts) repeatedly swept the box. Diagnosis was slow because every suspect script was innocent: the health check only calls `systemctl`, and the proxy reclaim checks a cmdline. The pattern kill was three layers down, in the thing the restarts invoked | Reclaim by port and cmdline, the shape `bin/wc-free-proxy-port.sh` already used for port 9000 and documents a warning about. Resolve the pid listening on 443, confirm `uvicorn app:app` in its cmdline, SIGTERM it, escalate to SIGKILL only if ignored, and report-and-leave anything that is not ours | Proved external before blaming anything: a server spawned exactly as the fixture does and touched by nothing died at 225s with code -15. `test_qa_launch_reclaim` (9 cases) scans executable lines only — the fix's own comment names the old idiom, so a substring search would find the bug in its own postmortem. Verified the scanner detects the original line and ignores a comment mentioning it |
| 47 | `sqlite3.OperationalError: database is locked` recurring in normal use — 492 `sync_all_failed` warnings, 66 `session_forget_failed`, 39 sampler failures in one day's production log, 486 of them in steady-state running rather than around a restart | The project wrote to one SQLite file from **three** connections: the shared aiosqlite one, a fresh sqlite3 connection opened **per index write inside a worker thread**, and a fresh one per auth session write. WAL permits a single writer, so the 30-second sync sweep — which writes messages to nine conversations and fires an index write for each — had the two main writers racing continuously, and the loser waited out its busy timeout and failed. Worse than the logged error: the index write swallowed its exception, so a message whose index write lost the race was committed and never indexed, unfindable by search for ever with nothing logged. Four theories were eliminated by measurement first — slow transactions (a full FTS rebuild measured 0.47s and a 2000-row batch 0.09s against a 5s timeout), an unrolled-back BEGIN (all five have handlers), restart windows (6 of 492), and a connection leak (2 fds). A fifth, 'two servers are running', was an artefact of the diagnostic itself: the server restarted mid-watch and the watcher was comparing against a stale pid | Index maintenance moved onto the shared connection, removing that writer entirely, and `busy_timeout` set explicitly to 15s on the shared connection — the only one of the three that had never set one, inheriting sqlite3's undocumented 5s default. Sharing the connection introduces its own hazard, so every failure path rolls back: a statement failing part-way leaves a write transaction open, which holds the writer lock against auth.py's connection and stops WAL checkpointing, converting a missing index entry into exactly the site-wide lock this removes | `tests/test_qa_fts_index.py`, 33 cases. Two are new and pin the fix rather than its effects: one counts `sqlite3.connect` calls during index maintenance and requires zero, because the index ends up correct either way and only the connection count can tell the arrangements apart; the other asserts `db_conn.in_transaction` is false after a failed index write. **The second was rewritten after failing its own mutation test.** Its first version dropped `messages_fts` and checked that writes still landed — which passed with the rollback removed, because a statement against a missing table fails at prepare time and opens no transaction, so it exercised nothing. Failing the INSERT after the DELETE has succeeded is what reaches the branch |
| 48 | `sqlite3.OperationalError: cannot start a transaction within a transaction` — 114 `sync_all_failed` warnings between 2026-09-03 14:22 and 2026-09-04 00:30, then silent (idle traffic, not a fix) | `messages_batch` and `chats_reorder` (`routes/db_chats.py`) each issued a literal `await db.db_conn.execute("BEGIN")` before their write loop, guarded only by `messages_batch`'s own private `_messages_batch_lock`. Every other writer on the same shared connection — `chat_set_transcript_offset`, `chat_set_question_ids`, `chat_delete`, usage recording, the sysstats sampler — does a plain `execute()` then `commit()` with no lock at all, which opens an *implicit* transaction that stays open across the `await` until its own `commit()` runs. Because the event loop interleaves coroutines at every `await`, one task's implicit transaction could still be open when another task's literal `BEGIN` landed on the same connection, and SQLite raises immediately rather than waiting out `busy_timeout` — a different failure shape from #47's lock contention, not fixed by it | Removed the explicit `BEGIN` from both sites. SQLite already opens an implicit transaction on the first `INSERT`/`UPDATE` of a sequence when the connection is not already inside one, which was already sufficient for both functions' own atomicity (all their statements still land in one transaction, still committed or rolled back together) — the literal `BEGIN` was adding a way to crash without adding any guarantee. **Open residual gap:** the private lock does not stop an unrelated writer's statements from landing inside `messages_batch`'s/`chats_reorder`'s implicit transaction and being committed or rolled back together with it, if their `await`s interleave; only the specific crash is closed, not every cross-writer interleaving on the shared connection | `tests/test_qa_fts_index.py` and `tests/test_qa_chat_order.py` (54 cases) still pass with no explicit `BEGIN`; `tests/test_qa_chats.py`, `test_qa_sync_all.py`, `test_qa_transcript_sync.py` (106 cases) unaffected. No test reproduces the interleaving itself — it is timing-dependent on concurrent coroutines sharing one connection, the same class of gap #43 leaves open for the logging fallback |
| 49 | My own port-reclaim fix took the site down: 43 restart attempts, exit 1 each, banner printed and nothing after it, no traceback anywhere | `holder="$(ss ... | grep -oP 'pid=\K[0-9]+' | head -1)"` under `set -euo pipefail`. With nothing listening — the normal case on any clean start — grep finds no match and exits 1, pipefail propagates, and set -e kills the script silently. The reclaim could therefore only succeed when the problem it exists to fix was already present. Nothing crashed, so nothing was logged; cweb1 located it by noticing uvicorn never printed even "Started server process", which puts the failure between the banner and the exec | `|| true` on the assignment, which is load-bearing rather than defensive | The real lesson is the test, not the fix. My nine cases all read the source and none executed it, so a block that could never succeed passed every one. `tests/test_qa_launch_reclaim.py` now lifts the block between explicit markers and runs it under the same `set -euo pipefail`, against real listeners on spare ports: no holder, a foreign holder, our own stale instance, and a matching server on a different port. Verified negatively — remove the `|| true` and the cold-start case exits 1 |
| 50 | The release-wait sitting beside that block slept its full ten seconds on every start while checking nothing, so each restart cost ten seconds it did not need. Authorship untraced and deliberately not guessed: `git log -S"_retry" -- launch.sh` returns nothing and it appears in no commit, so it existed only in the shared working tree. Proximity to the reclaim block is not evidence of who wrote it — one session's commit had already swept another's uncommitted edits to this file | `if ! ss -tln "sport = :$PORT" >/dev/null 2>&1` tests ss's **exit status**, and ss returns 0 for any successful query whether or not anything matched. The condition was therefore false on a free port and a busy one alike: the loop never broke early and never observed the thing it was waiting for | Test for output instead: `! ss -tlnH "sport = :$PORT" \| grep -q .` | Invisible to every assertion that existed — exit status and stderr were identical either way, so only the clock could tell. A timing case now asserts the block returns in under 3s when the port is free; the inert version takes 10.1s against 0.0s. Production restart went from 10+s back to 2s |
| 51 | Every suite total quoted in this session's reports was missing 43 tests, and nothing in the output said so. `python3 -m pytest` reported green while the 3 JS-syntax cases and all 40 browser cases never ran — a skip line reads as "not applicable on this machine", which is indistinguishable from "ran and passed" in an aggregate, so neither the author nor a reviewer would question the number | Two different causes behind one symptom, and the first one I gave was too broad. `quickjs` is genuinely venv-only. The browser skips were not: `playwright` **is** importable under system `python3` — it is the Debian package in `/usr/lib/python3/dist-packages` — but its `compute_driver_executable()` resolves to `/usr/bin/node`, which does not exist on this box, so `DRIVER_OK` is false and all eight browser classes skip. The pip wheel in `.venv` bundles its own node under `playwright/driver/node`, which exists. So the fleet's habit of typing `python3` selected an interpreter where the browser layer was structurally unrunnable | Run the suite as `.venv/bin/python -m pytest`, and always with `-rs` so a skip has to be seen rather than inferred from a total. The interpreter is now named in §14 rather than left to whichever `python3` is first on PATH. Not fixed here, and deliberately: making the Debian playwright work would mean installing a system node, which changes the box to suit a habit instead of fixing the habit | Verified in both directions rather than asserted: `find_spec` shows `quickjs` absent under system `python3` and present under the venv, and `compute_driver_executable()` returns `/usr/bin/node` (exists: False) under system `python3` against a real bundled path (exists: True) under the venv. Full venv run: **1848 passed, 3 skipped, 0 failed** in 13:02, roughly 90 cases more than any figure reported earlier. Same family as #41, #43, #48 and #49 — a healthy-looking surface over something that was not running — except this one degraded the instrument used to judge every other fix, which is why it is the worst of them |
| 52 | HEAD called a function no committed file defined. `d80cdaf` -- a supervisor SSE fix belonging to another session -- swept my uncommitted `app.py` route and handler into itself and left `prompts.py` behind, so the committed tree called `prompts.dismiss` against a module without it. Every `DELETE /api/chats/{id}/question` on a fresh clone would have raised AttributeError | Whole-file staging is the only granularity this shared tree offers, so any session committing `app.py` commits whatever else is in `app.py` -- and a feature split across two modules is swept in halves. Registry #49 recorded the same sweep costing an edit; this one shows the worse case, where the half that lands is the caller and the half left behind is the callee. Nothing local fails: my working tree had both files, so every test I ran passed while HEAD was broken | Repaired by committing the other half (`110d43a`), naming paths explicitly rather than letting a bare `git commit` take the index. No process fix claimed: the hazard is structural to eight sessions sharing one index, and the check below is what makes it survivable rather than preventable | `tests/test_qa_head_consistency.py::CrossModuleReferencesResolveTests` found it unprompted -- it resolves cross-module calls against **HEAD** rather than the working tree, which is the only place the break existed. Worth stating plainly because it inverts the usual reading: a test failing on a name my own tree defines was correct, and the tree I was measuring in was the misleading one. It passes again (5/5) with `prompts.dismiss` committed |
| 53 | `/api/supervisors/{id}/members` returned 500 for any supervisor holding a member that had never been spoken in -- so adding an agent and opening the panel before it said anything broke the panel entirely | `"last_seen": last.get("created_at")` where `last` is `None`. `chat_last_activity` only carries conversations that have activity, and the fallback dict containing that line is reached **only** when `chat` or `last` is missing -- so the one path that evaluates it is the path where `last` is None. The handler's own docstring, eight lines above, says "A member with nothing to say is reported idle rather than dropped": the behaviour was designed and then not written | `last.get("created_at") if last else None`, and the `chat and last` guard split so a resolvable chat with no activity still classifies | Two tests named this and only one of them was real. `test_a_member_whose_chat_was_deleted_is_skipped_not_fatal` passes with the deleted-chat branch **deleted** -- `supervisor_members_list` INNER JOINs `chats ... AND deleted_at IS NULL`, so a deleted member never reaches the loop and that test has never exercised the branch it names. Found by mutation, not by reading. The branch turned out to guard something else entirely: the members query filters `deleted_at` but **not owner**, and selects `c.title` from the join, so a membership row naming another account's conversation renders that account's title -- exactly what `supervisor_member_add`'s docstring warns the layer permits. `test_another_accounts_conversation_is_not_rendered` now pins it; disabling the skip yields `["bob's private title", "mine"] != ["mine"]`. **Untested defence in depth is how the deleted-chat branch came to be believed without running.** The reporting error is its own variety, distinct from the fourteen before it: not a check that cannot fail, but a check that fails for someone else's reason and is then counted as evidence it never earned. Two red tests were read as two covered paths, which is reading green and inferring coverage with the sign flipped |
| 54 | Every API-level TestClient test in the suite runs anonymously and passes anyway. `test_qa_chats.py`'s `APIChatListTests` logs in, gets 200, and is unauthenticated for every request after it | The session cookie is set `Secure` unless `COOKIE_ALLOW_INSECURE`, and httpx will not send a Secure cookie to an `http://` URL. `TestClient` defaults to `http://testserver`, so login succeeds, the cookie lands in the jar, and nothing sends it. The tests accommodate that instead of failing on it: `assertIn(response.status_code, [200, 401])` followed by `if response.status_code == 200:`, so the assertions that matter run only when the request happened to work -- and it never does | `TestClient(web_app, base_url="https://testserver")`. With it the same probe returns 401 anonymous, 200 authenticated with real rows, 403 without a CSRF header and 400 from inside the handler -- four distinct codes where there had been one. Not retrofitted onto `test_qa_chats.py` here: those tests are committed and someone else's, and rewriting a class to fail is a conversation, not a drive-by | Found while adding access tests for the question routes, which had none: GET, POST and DELETE all reach into a live terminal, and nothing asserted that one account could not close another's prompt. `tests/test_qa_question_access.py` (12 cases) pins it from outside the stack. Mutation-verified in both directions -- replacing `session["user"]` with a fixed owner in the three handlers fails 6 cases: the three ownership tests go `200 != 404` and `409 != 404`, and the three that prove the handler is reachable at all go the other way. The status-code tolerance is the tell to look for elsewhere: an `assertIn(status, [...])` over codes that mean opposite things is a test declaring it does not know what should happen |
| 55 | The two tests that pinned the `/dev` auth bypass now report **"not in a git repo"** in a git repository, and neither can fail | `DevAuthSkipTests.test_dev_endpoint_exists` and `..._is_get` wrap their own assertion in `try: ... except Exception: self.skipTest("not in a git repo")`. `AssertionError` is an `Exception`, so the failing assertion is caught by the handler meant for a missing git, and every failure becomes a skip carrying a diagnosis that is false. The route was removed from HEAD in `dc85305`, which is exactly the condition those tests existed to catch, and the suite reported two skips | Not fixed by me: the file is committed and another session's, and rewriting somebody's assertions to fail is a conversation rather than a drive-by. Raised with the owner and with Pedro. The shape to avoid is narrow -- put the `assertIn` **after** the `try`, or catch `OSError`/`subprocess.SubprocessError` rather than `Exception` | Distinct from #50: there the skip was honest and merely invisible in a total; here the failure manufactures the skip that hides it, and the message sends the reader to look at git. Same root as the bare `except Exception` that swallowed a NameError for every request -- a handler broad enough to catch the thing it was protecting against. Note the third assertion in that class, `test_auth_middleware_skips_dev_routes`, has no try block, so it still genuinely pins `"/dev/"` in app.py: removing the exemption fails it |
| 56 | The `/dev/` auth exemption in `AuthMiddleware` was defended by a committed test as "a live product decision". It was **debug scaffolding of mine that nobody meant to ship** | I added the exemption and a throwaway `/dev/supervisor-trigger` endpoint, uncommitted, on 08-31 at 18:11 to reproduce a supervisor failure. At 18:39 `1f7c914` -- a commit about pause/resume, recency sort and member heartbeat -- swept both into itself. `dc85305` later removed the endpoint and left the exemption. A third session found the orphaned line in HEAD, read it as deliberate, and wrote `test_auth_middleware_skips_dev_routes` to pin it. Registry #51 recorded a sweep breaking a cross-module call; #49 recorded one costing an edit. This is the same hazard one turn further on: the sweep **manufactured a product decision out of debris, which then acquired a test defending it** — so the artefact that normally proves intent (a committed test with a reasoned docstring) certified something no one had decided | Exemption removed from `AuthMiddleware`. Nothing was exploitable — no route was registered under the prefix, so requests 404'd — but the prefix is unauthenticated *by default* in an application that spawns Claude with `--dangerously-skip-permissions`, so the next route added there would have been RCE. The pinning test is another session's committed file and is flagged, not edited: its own docstring says the assertion "should then be deleted rather than weakened" | Found by running the §8/§4 audit against a working tree I had dirtied myself, then checking `git log -S` rather than trusting either the code or the test. **The lesson is about evidence, not about `/dev/`:** a test's docstring is an account of what its author believed, not of what was decided, and in a shared tree those two diverge silently. `git log -S <line>` on anything surprising is the only cheap way to tell provenance from consensus. My own first reading in this session was also wrong in the other direction — I called the line "debris I left" before checking, and had it been a real product decision, removing it would have been the drive-by that #53 and #54 both refuse to commit |
| 57 | `web/supervisor.js:1500` holds a bare `setInterval` — no handle, no `clearInterval` anywhere in the file — which §4 names explicitly as *the* failure case | `3a68c5c` ("hold every repeating timer") swept `web/assets/*.js` and left `web/supervisor.js` untouched, because the supervisor page is the one client file that lives in `web/` rather than `web/assets/`. §4's own verification command has the same blind spot: it greps `web/assets/*.js`, so the rule could not have caught this even when run exactly as written. The timer polls `loadSupervisors()` and `loadTasks()` every 30s and cannot be stopped; the page is loaded in an iframe whose `src` is reset each time the supervisor pane opens, so it survives as long as that document does Fixed on the operator's instruction, surgically — an exact-string edit rather than a whole-file write, so the peer's concurrent +238 lines in the same file were left untouched (verified afterwards by their own feature markers). `_refreshTimer` holds the handle, `if (!_refreshTimer)` guards re-entry, and a `pagehide` listener clears it: the guard is load-bearing beyond the handle because the console loads this page in an iframe it *resets* rather than navigates, so a second `init()` is reachable and would otherwise leak the previous timer. **The scanner was widened first and observed to fail** (`supervisor.js:1544`) before the code was touched, so the test is known to see the defect rather than assumed to `tests/test_qa_timer_handles.py`, widened to the union of `web/*.js` and `web/assets/*.js`, plus two new cases: one pinning that **scope** (the glob was the defect, so a narrowed glob must fail — without it the scope can be reverted and every other case still passes) and one pinning the guard and teardown. Four mutations, each verified to have actually modified the file before its result was believed: narrow the glob → 1 fail; drop the guard → 1 fail; restore the bare timer → 2 fails; drop the `clearInterval` → 1 fail. §4's grep in this file was corrected too. The second-order lesson is the durable one: a checklist whose glob does not cover every file it claims to audit reports clean and *looks* clean — the #50 family again, a healthy instrument over something unmeasured — and here the rule, the sweep that implemented it, and the test enforcing it had all inherited the same blind spot, so three independent-looking confirmations were one mistake counted thrice |
| 58 | Two suite failures that had nothing to do with the code under test, both surfacing as confident accusations of bugs that do not exist | **(a)** `test_a_later_ask_brings_it_back` hardcoded its "later" ask as `2026-09-01T09:00:00Z`, but the dismissal it has to post-date is stamped with the real `_now()` and the feed suppresses `stamp <= dismissed_at` — so the fixture was only "later" while the wall clock was behind 09:00Z on 2026-09-01. It passed all morning and then failed for ever, blaming a "permanent mute" in the dismiss control. **(b)** `test_nothing_reaches_the_production_log` compared the production log's byte size before and after its probe, which measures every writer on the box — the live server, the health timer, five other sessions — so it failed for unrelated traffic and passed only when nothing else was running | (a) derive the timestamp from the dismissal just made; (b) assert a unique marker is absent from the production log **and present in the redirect target** — the second half matters, or a probe that logged nothing anywhere satisfies the first. Both verified negatively: removing `WC_LOG_FILE` from the probe env makes the marker appear in production and fails 3 cases | Both are the inverse of the failure this registry usually records. #50, #48 and #41 are *green* surfaces over broken things; these are *red* surfaces over working things, and the cost is the same instrument-level damage — a suite that cries wolf gets discounted wholesale, and the 4 failures in this run had to be individually triaged before any of them could be believed. An expiring literal is the worse of the two: it is not flaky, it is green until a date and broken after it, so nothing about the failure points at the fixture. `git archive --format=tar HEAD | tar -x -C "$T"` and running there is what separated "my change broke it" from "HEAD is broken", without touching a working tree that four sessions were editing |
| 59 | A full-suite run reported **26 failed**, every one of them `binascii.Error: Incorrect padding`, from a file that passes 28/28 in isolation | `tests/test_qa_supervisor_ux_shortcuts.py` (another session's, created mid-run) wraps its headless-browser helper `_run` in `@functools.lru_cache(maxsize=None)` and then does `json.loads(base64.b64decode(found.group(1)))` on whatever a regex scrapes out of the page. Under load — two full suites plus five live sessions on one box — a browser launch returns nothing, the regex captures a non-base64 fragment, and the decode raises. Because the helper is **memoised**, that single transient result is replayed to every test in the file: one flake becomes 26 identical failures, perfectly consecutive, which reads exactly like a systemic defect | **Not fixed — another session's in-flight file, and it is green standalone.** Handed off with the diagnosis. The shape to fix is the helper, not the tests: validate the scrape before decoding and raise something that names the browser failure, and do not cache a failed result — `lru_cache` on anything that can fail transiently converts a flake into a permanent one for the life of the process | The instrument-damage lesson again, from a new direction. #50 hid 43 unrun tests inside a green total; this inflates **one** environmental flake into 26 red results, and the triage cost is the same: every failure had to be traced before any could be believed, and the honest verdict ("one flake, one file, passes alone") is three steps away from the number the report prints. Isolation is the cheap discriminator — `pytest <file>` alone separates "this file is broken" from "this file is a victim" in one command, and should be the first move on any consecutive block of identical failures |
| 60 | Restarting the proxy to fix a *cosmetic* §17 `source_mtime` mismatch **broke every turn**. The restart was correct and safe by every check made beforehand — idle socket, no `claude` children, no in-flight turns — and it still took turn execution down | `bin/wc-proxy-run.sh` needs `~/.local/bin` on PATH or an absolute `WC_CLAUDE_PATH`, because `systemd --user` supplies neither and `claude_proxy.py` refuses to spawn when `shutil.which("claude")` returns None. That fix had been made the previous day as an `export PATH=...` line which **lived only in the shared working tree and was never committed**, so another session's checkout reverted `wc-proxy-run.sh` to `9e6fa1b` and silently removed it. Nothing failed at the time: the running proxy kept the good environment it had *started* with, so the regression was invisible for ~18 hours and armed itself for whoever restarted next. `git log -S 'home/kali/.local/bin' -- bin/wc-proxy-run.sh` returns nothing, which is the proof it was never committed — the same forensic as #49 | `WC_CLAUDE_PATH` resolved to an absolute path from a candidate list (`~/.local/bin`, `/usr/local/bin`, `/usr/bin`, then `command -v`), chosen over `export PATH=` so the value cannot be reordered out of relevance, and because `claude_proxy.py` already reads that variable. A missing binary now **warns loudly and still starts**, rather than aborting (which would take the site down over an optional dependency) or proceeding silently (which is what cost two outages). Verified live rather than by reasoning: a raw-TCP turn against the restarted proxy returned `PROXY_OK`, and the proxy log shows `resolved=/home/kali/.local/bin/claude` while its PATH still lacks that directory — so the fix demonstrably does not depend on PATH | `tests/test_qa_proxy_claude_path.py` (8 cases). It **executes** the block between `>>> claude-path-block` markers under a synthetic HOME and systemd's real PATH, rather than reading it — #48 is the precedent, where nine source-reading tests all passed against a block that could never succeed. Mutations: delete the block → 6 fail; drop `~/.local/bin` from the candidates → 2 fail; make a missing binary `exit 1` → 1 fail. **The generalisable lesson is about what "safe restart" means.** Every pre-flight check answered "is anything in flight?", and none answered "does the process about to start differ from the one running?" — for a long-lived process in a shared tree those are different questions, and only the second catches a fix that exists nowhere but in RAM. An uncommitted fix to a supervised service is a time bomb whose fuse is the next restart, and §17's `source_mtime` check exists to detect drift in exactly one file while the environment drifts unwatched |
| 61 | Writing the request-picker tests produced **two** defects of the kind this registry exists for, in the same sitting: an assertion that could not fail, and a mutation that could not detect | **(a)** `test_a_prompt_that_looks_like_markup_stays_text` seeded its `<img src=x onerror=...>` payload as the *oldest* request. The picker offers only the newest ten, so the payload was never rendered, and "no `<img>` in the menu" was true of a menu that had never been given one — it passed with `label.textContent` swapped for `label.innerHTML`. The docstring *described* the payload being outside the ten and treated that as fine, so the reasoning was written down and still wrong. **(b)** The mutation meant to prove the cap keeps the newest ten was `requestHistory = found.slice(-10)`. `found` is already capped at ten by a `break` inside the collecting loop, so slicing the last ten of a ten-item list is a no-op: the file differed, `cmp` confirmed it differed, and the behaviour was identical | (a) `MARKUP_AT = 3` seeds the payload inside the ten, and the case now asserts the payload **is** rendered before asserting no element was built from it — "present as text" and "absent as markup" are both required, or a row that dropped the content entirely would pass. Extended to pin it into the strip too, which caught a second `innerHTML` path. (b) The effective mutation removes the `break` first, then keeps the wrong end; both halves are needed | Confirms and sharpens the discipline from #47 and #52. Verifying a mutation **changed the file** is necessary and *not sufficient* — it must change the *behaviour*, and the cheap check is to state what the mutated code should now do and confirm it does. Here `grep`-ing for the mutated expression and printing "keeps oldest ten" was enough. The pairing is the useful part: an un-failable assertion and an inert mutation are the same error viewed from either end — something in the loop between code and verdict is disconnected — and each one *hides* the other, because a mutation that does nothing cannot expose an assertion that checks nothing. Four earlier mutations in this session reported "passed" while being void (#56's note); this is the first time one of them concealed a live XSS hole in the code under test |
| 62 | Every agent that *finished* its work was reported by the supervisor as one **blocked on a question**, with its closing sentence displayed as the ask. At the moment of diagnosis half the waiting feed was rows that needed nobody | `status` in `~/.claude/sessions/<pid>.json` was tested as `!= "busy"`. That was correct while `busy` was the only value Claude Code wrote, and `db.read_claude_sessions`'s comment said so in as many words — "Observed value is `busy`". CLI 2.1.252 writes **three**: `busy`, `idle` (concluded, needs nothing) and `waiting` (blocked, needs a person). Nothing re-read the comment, so two call sites inherited it, one of them stating the belief explicitly: "Any non-busy value means it has stopped and is waiting on a human." The fabricated `reason: "asks"` is the sharp end — it came from the status field, not from reading a transcript, so the supervisor asserted a question it had no evidence for | An allowlist, `_CLI_STATUS_NOT_BLOCKED = {"busy", "idle"}`, rather than a comparison against `waiting`: an unrecognised future status then reads as blocked and reaches a human, because over-reporting costs a dismissal and under-reporting leaves an agent stuck with nobody told. `idle` falls through to the routine-output path where a read mark retires it; `waiting` still needs an explicit dismissal, since reading a question does not answer it. Also added `transcripts.turn_concluded()` as an independent corroborating signal — `stop_reason == "end_turn"` **and** no prompt after it — and corrected both stale comments | `tests/test_qa_session_status_split.py` (10) and `tests/test_qa_turn_concluded.py` (15). Five mutations, each verified applied: revert to `!= "busy"` → 4 fail; hide `waiting` in the allowlist → 4 fail; treat unknown as finished → 1 fail; drop `prompt_after` → 3 fail; count tool results as prompts → 3 fail. **Two lessons.** First, the conclusion detector had to be tested on a *constructed* positive case: every live session was busy or waiting, so a detector returning `False` unconditionally agreed with all six — the same always-passing shape as #50 and #60. Second, and new here: **a stale comment is a defect that propagates.** This one was accurate when written, was believed by two later call sites, and outlived the fact it described by an unknown number of CLI releases. Nothing in the codebase dates an observation about an external tool's behaviour, so there is no mechanism by which anyone would have re-checked it — the same class as #17's retired model id. Where a comment records something observed about a dependency rather than about this code, naming the version observed (`2.1.252`) is the cheapest available guard |
| 63 | `QuestionPendingNoteTests` guarded the one branch in `classify_chat` that detects a *structured, definitely-unanswered* question — and could not fail. Deleting the note handling from production left both cases green | Three faults compounding, all in two short test bodies. **(a)** Both **copied** the production branch instead of calling it, so neither touched `classify_chat`; the copy drifted from the original the moment the original changed. **(b)** Both fixtures were `"Which backend should I use?"`, which `_attention` already flags via the phrase `should i` — so `if not reason:` was False and the note logic never executed *even inside the test's own copy*. **(c)** `test_note_not_present` asserted `reason == "asks"` beneath a docstring saying the message "should not be flagged". The class docstring stated the premise the real bug depended on — "the preview carries the note" — which holds only for messages under ~400 characters, because the note is appended last | Rewritten to drive `classify_chat` with filler containing no `?` and nothing from `_ASKS_FOR_INPUT` or `_REPORTS_A_BLOCKER`, so only the note can raise the flag, plus a long-and-finished control so a classifier that answered "asks" to everything long would fail. Four mutations, each verified applied: reason from the preview alone → 1 fail; note checked against the preview alone → 1 fail; note handling deleted → 2 fail; `"asks"` for everything → 1 fail (the control) | Found by cweb4 while reviewing a fix I had just verified as correct — the fix *was* correct, and the tests over it were empty, which is why "landed and verified" did not cover it. **The transferable part is the failure mode: a test that copies the branch it means to guard.** It passes on the day it is written and then measures a snapshot of code that has since moved, so it reports on the production line it names while being causally disconnected from it — the strongest possible form of the always-green trap, because the copied logic is *visibly right there* in the test body and reads as coverage. Grepping a suspect test class for the function it claims to cover (`grep -c 'classify_chat\|handle_supervisor'`) is a one-command check, and here it returned a single hit that was a comment. Same family as #52's untested defence in depth and #54's manufactured skip, and the fourth always-green defect in two days: #50, #54, #60, and this |
| 64 | Two sessions misattributed each other's work in consecutive messages, in opposite directions, and one of the misattributions was used as corroboration for deleting a committed test | Eight sessions share one working tree, write first-person comments into shared files, and commit whole files -- so `# I added this` and a CHANGELOG entry reading "debug scaffolding of mine" attach to nobody in particular, and a sweeping commit moves them under someone else's name. cweb2 was named as the author of a `/dev/` exemption that was somebody's swept-in scaffolding and of a test-file change they had never opened; earlier they had read proximity in `launch.sh` as authorship of the inert release-wait. In my case I also quoted `tests/test_qa_timer_handles.py:80` as unwired from a **stale snapshot delivered in my own context**, without opening the file -- in the same message that recommended checking before naming anyone | No code fix; the rule is `git log -S"<distinctive string>" -- <file>` or reading the file, before attributing anything to anyone. It costs about fifteen seconds. Provenance claims in a shared tree are measurements, not impressions | The consequence that matters is not the discourtesy: `DevAuthSkipTests` was deleted partly on the strength of "cweb2's entry settles the provenance", where the entry and the thing it explained were the same source. The deletion survives on its own merits -- three assertions defending an unauthenticated admin-session mint, two of which could not fail -- but the reasoning offered for it was invented. A stale in-context snapshot is the sharper trap of the two, because it *looks* like having read the file Now four times in one session, all mine: cweb2 twice, cweb5 once, and cweb3's `--user-data-dir` fix credited to cweb5 on the strength of `git status` showing the file modified. cweb5's formulation is the rule: **`git diff` shows you *a* change, not *whose*.** In a shared tree the status line carries no authorship whatsoever, and reading the diff would have shown a comment about a 126 MB profile leak -- not a signature, but evidence, and I did not look. Reliable attribution is asking, or `git log` once it is committed; nothing else. |
| 65 | **#50 recurred, in full, against a session that had read it.** A peer polled a "green and settled" tree eleven times with `python3 -m pytest` while the entire browser layer never ran; the same test that fails in 6.68s under `.venv/bin/python` reported `1 skipped in 0.14s` under system python. The picker defect now recorded as a 0.9.4 KNOWN was invisible for all eleven polls. In the same session I reported "0 skipped" as a *positive* finding without noting that the figure was only obtainable because I happened to type `.venv/bin/python` | #50 diagnosed this exactly and its remedy was "run the suite as `.venv/bin/python -m pytest`" — a habit, recorded in prose, in a §14 paragraph. Habits are not enforced by being written down, and `python3` remains the shorter thing to type, is what shell history offers, and produces a green result. The recurrence is therefore not carelessness but the predictable failure of a documentation-only fix: nothing in the repository *makes* the wrong interpreter behave differently from the right one, and the wrong one fails in the reassuring direction | Not fixed, and the gap is now explicit rather than implied. The candidates each have a real cost: a `conftest.py` that hard-errors when `sys.prefix` is not the venv would stop the wrong interpreter silently succeeding, but also breaks any legitimate system-python invocation; making the Debian playwright work needs a system node, which #50 rejected as changing the box to suit a habit. What is *not* acceptable is a third prose reminder — two have now failed | Verified in both directions by the peer and by me: same node id, `1 skipped in 0.14s` under `python3` against `1 failed in 6.23s` under `.venv/bin/python`. The regression test this wants is a meta-test asserting the browser layer actually ran, since every existing test is by construction unable to report its own non-execution — which is the whole of #50 and the reason it recurred |
| 66 | A 13-minute suite started before an edit reports on a tree that no longer exists, and the number still reads as authoritative. Twice in one session I quoted a total measured against a state I had since changed *(originally recorded as #62; renumbered because cweb3's #62 was cited externally first -- in an append-only registry a published number has to keep pointing at what it pointed at.)* | The run holds no reference to the tree it read. Nothing in `2069 passed` says which bytes were on disk when the collection happened, so the figure survives every edit made while it was running and arrives looking like a measurement of now. Compounded by two habits of mine: editing during a run, and running §13/§17 -- which start servers and invoke the health check -- concurrently with §20's suite, which is how five `DeviceAlertBrowserTests` cases died with `test server exited with code -15` and got reported as failures before a clean re-run cleared them | Start the suite last, edit nothing until it finishes, and if the tree changes, kill the run rather than reading it. Where a number is going to be quoted, verify the artefact instead of the tree: `git archive $(git write-tree)` for the index, `git archive HEAD` for the commit -- both are immutable while the run reads them, which is the property the working tree does not have | Same family as #41 and #50: a healthy-looking surface over something that was not what it claimed to measure. cweb6's chunked runner hit the identical shape inside the instrument built to avoid #50 -- its aggregator scanned for a `=== ... ===` banner that `-q` does not emit, matched nothing, and printed `passed=0` beside sixteen chunks that had passed. It failed loudly, which is the only reason it was caught in a minute. Verified the fixed aggregator against synthetic chunk logs: an OOM-killed chunk with rc=137 and no counts is reported as `NO SUMMARY -- killed or crashed` rather than counted as zero |
| 67 | The members panel announced a conversation's work as **finished while its terminal session was still running**, and the sidebar — same rule, same moment — correctly kept it quiet. Two surfaces sharing one classifier, disagreeing | `handle_supervisor_members_get` called `classify_chat(chat, last, live_ids, queued, marks, {}, {}, {})` — three empty CLI maps. So the members panel could not see a linked session's status at all. That was a mild under-report for as long as the CLI cross-reference only *added* a waiting reason; the `done` promotion turned it into a false claim, because the guard that suppresses "finished" for a busy linked session (`cli_status == "busy"`) cannot fire on data the caller never supplied. `classify_chat`'s own docstring explains it was extracted so "the sidebar and the supervisor members panel share one definition of what stuck means" — and it was doing exactly that, faithfully, on two different inputs | `_cli_maps(marks)` extracted and called from both surfaces, replacing the sidebar's twenty inline lines and the members panel's `{}, {}, {}`. Returns empty maps when the registry is unreadable, since unknown status is the safe default everywhere it is consulted | Found by `StatusReuseTests::test_routine_output_agrees_with_the_sidebar` — a test whose *fixture guard* broke first ("fixture must be routine output"), which reads like fixture rot and was reported to me as such. Repairing the fixture is what exposed the real defect underneath. **The lesson is about what "share one function" buys you.** Extracting the rule prevents two rules; it does nothing about two sets of inputs, and the second failure is harder to see precisely because the shared function is right there in both call sites looking authoritative. When a decision is centralised, its *arguments* are the new duplication — and a call site passing `{}` for something the other passes real data is the shape to grep for. Also: cweb6 suggested repairing the fixture by choosing a different message string. There is no such string — no text produces `updated` for a bare chat now — so the plausible-looking repair would have quietly rewritten the test into a second copy of the case above it |
| 68 | **Shipped in 0.9.4.** A finished report was announced as a question, and a report that something was *un*blocked was announced as a blocker — the opposite claim | `_ASKS_FOR_INPUT` and `_REPORTS_A_BLOCKER` were tested with `phrase in text`, which matches inside words. Five cases were live: `"should i"` matched `"should include"` and `"should interfere"`, `"confirm"` matched `"confirmed"`, `"blocked"` matched `"unblocked"`. Reported to me as a regression in the change that made the classifier read a message's tail as well as its opening — and it was not one. `_attention(preview)` had the identical fault for any message whose opening happened to contain "should include"; the wider window only changed how often it fired | `\b` on each side of every phrase, compiled once per list, both windows kept. 21-case table went from 5 wrong to 0. Anchored as a whole phrase rather than per word, because `"can't proceed"` and `"i was denied"` contain spaces and an apostrophe | **The lesson is about where a fix gets aimed.** Two sessions independently proposed fixing the *windows* — one of them a decomposition giving each phrase list its own window — and neither would have fixed a single one of the five, because they all fire in the preview too. What made the misdirection so easy is that the bug genuinely did become visible when the window widened, so "the window change introduced it" explained the observation perfectly while being false. That is the same trap as #63's picker diagnosis, and cweb2 named it best: a wrong cause that predicts every observed symptom is nearly impossible to falsify by reading, because reading is the method that produced it. The check that broke it here was cheap and mechanical — run the phrase lists against the failing text and ask *which* phrase matched, then ask whether it matched a word or part of one. Coverage: `IncidentalPhraseTests`, including a property case gluing every phrase in both lists to a letter on each side, so a sixth incidental match cannot appear without a test naming it. Four mutations; restoring the shipped behaviour fails 28 |
| 69 | Browser tests failed differently on every run for a whole day. Three sessions produced three causes — contention, order-sensitivity, and a memoised `lru_cache` helper — and all three were wrong | Six chromium harnesses launch `subprocess.run([CHROMIUM, "--headless", ...])` with **no `--user-data-dir`**, so chromium creates a ~126 MB profile under `/tmp/org.chromium.Chromium.scoped_dir.*` and leaves it. The `TemporaryDirectory()` in those helpers holds only the probe HTML. `/tmp` was found at **100%, 72 orphaned profiles, 1.7 GB, zero live chromium processes**. Once tmpfs fills, every launch times out and leaks another — so the failure count grows with each run, which is exactly why no two runs agreed and why every proposed cause could be supported by some subset of the evidence | `f"--user-data-dir={page.parent}/chrome-profile"` at all 7 launch sites, putting the profile inside the temp dir that is already cleaned. Verified by cweb6 through the **kill path** rather than the happy path: the same file that previously took tmpfs from 227 M to 1.6 G and leaked 24 profiles was killed after a 25-minute hang and leaked **0**, disk moved 1 MB | **Proved causally, which is the part worth copying.** After reclaiming the space and changing no code, `test_qa_supervisor_page_restore.py` went from *5 failed, 4 passed in 0.9 s* to *9 passed in 3.4 s*. Same commit, same tree, only free disk — and I had been one step from filing those five against a peer's layout commit on the strength of the timing signature. The check that stopped me was `df -h /tmp`. **And a correction on my own claim:** I reported the leak as the cause of the instability outright; cweb6 was right that it explains the cascades and not the signal underneath. `test_qa_supervisor_members_picker.py` still reaches test 7 and hangs indefinitely with 1.5 G free, which is its own defect. An environmental cause that explains most of a mess is the easiest thing in the world to over-extend to all of it |
| 70 | Every "full suite" figure quoted today by two independent sessions was short by 14 tests, and neither noticed | `test_functional.py` lives at the repository root. cweb6's chunked runner globbed `ls tests/test_*.py`; I ran `pytest tests/`. Two different instruments, the same blind spot: 2184 collected instead of 2198. So the numbers 1988 / 2028 / 2168 / 2187 quoted across the day all excluded the same file — one of whose tests was genuinely failing at the released commit | cweb6 changed the runner to take its file list from `pytest --collect-only`, plus a refusal when the chunk plan count differs from the collected count. A later commit moved `test_functional.py` into `tests/` as well | This is cweb2's "agreement is one observation with two signatures" in its purest form: two sessions independently confirmed a total, and the agreement was worthless because both instruments shared an assumption about where tests live. The remedy that works is the one that cannot disagree with pytest **by construction** — derive the list from collection rather than from a glob or a path argument. Related to #50, which was also a suite total concealing unrun cases, except that one was visible as skips and this one was invisible entirely |
| 71 | Moving a conversation between the gateway and Anthropic left it accumulating blocks the API refuses. The turns still worked, so nothing looked wrong — this is the state that killed five terminal sessions on 2026-08-31 with `400 ... each thinking block must contain non-whitespace thinking`, permanently and with nothing in the interface to explain it | `_prepare_transcript_for_backend` knew about one poison and not the other. `transcripts._needs_repair_sync` scanned for `"text": ""` and nothing in the module mentioned `signature`. A thinking block carries a provider-specific signature, so a block one provider wrote cannot be replayed by another — and the repair ran, removed the empty text, logged success, and left them. A guard that covers one of two routes reads as covered | `_is_foreign_thinking` discriminates on the **signature, never the type**: a signed block is the provider's own and replays correctly, so removing those would discard real reasoning for nothing. Repair now trims blocks *inside* a record rather than only dropping whole records, because a record is usually `[thinking, text]` and dropping it whole discards the answer | `tests/test_live_backend_switch.py` found it, and only by running a real switch. The round-trip case asserts the poison **exists** before the strict replay and is gone after, so it cannot pass vacuously — zero problems is also what "the gateway stopped emitting them" looks like. `tests/test_qa_transcripts.py` gained the contract change: it asserted `repaired is False` for `[empty text, tool_use]`, which encoded the old limitation rather than the rule — the API refuses *any* empty text block, not only a record made of one |
| 72 | Switching a machine off the gateway back to Anthropic kept sending turns to the gateway. The documented workaround was `unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN` before starting the proxy — a fix that must be remembered every time and is invisible when forgotten | `claude_proxy._backend_env` builds the child environment from `dict(os.environ)` and cleaned `ANTHROPIC_BASE_URL` only on the **non-anthropic** path. A machine with no `base_url` means "the official API", and that branch left the variable alone, so the value inherited from the proxy's shell survived. `ANTHROPIC_AUTH_TOKEN` was never cleaned on either path, and the CLI prefers it over `ANTHROPIC_API_KEY` — so an inherited token silently outranked the key the backend had just been given | Drop `ANTHROPIC_AUTH_TOKEN` unconditionally before the branch, and drop `ANTHROPIC_BASE_URL` when the machine supplies none. `runner._build_env` never had this bug: it builds from an allowlist, so nothing is inherited — the asymmetry is why it went unnoticed, since the path with the bug is the one that is on by default | `tests/test_qa_backend_env_isolation.py` (8 cases), mutation-verified: against the old function three fail, and they are exactly the two reported symptoms. One case pins the runner's allowlist, so turning it into a copy of `os.environ` would inherit the same bug and fail here. **Reported by Pedro from operational experience, not found by any check in this document** — every static stage passed while the workaround was load-bearing |
| 73 | Registry #42 was fixed and reported done, and the production log kept filling with test traffic — `ip=testclient`, `user=alice`, a stored-XSS probe — as recently as the day it was closed | #42 redirected the servers the suite *spawns*, via `WC_LOG_FILE` in the browser fixture's subprocess. Four modules drive the app with an in-process `TestClient`, which imports `app` inside the pytest process itself with the variable unset, and never saw the fix. The test guarding #42 asserted the property only for the subprocess path, so it passed throughout | `tests/conftest.py` sets `WC_LOG_FILE` before anything imports `config` — it cannot be a fixture, because `config` binds `LOG_FILE` at import. An explicit setting still wins, so CI can point it elsewhere | `tests/test_qa_log_path.py` gained `InProcessRedirectionTests`, asserted **in-process** on purpose: every other case there shells out, which is precisely why they all passed while this route leaked. Verified end to end — the four offending modules produce 0 new lines in the production log and 279 in the test log. The general form is #71's: a guard covering one of two routes reads as covered, and nobody looks again |
| 74 | Pedro found three unanswered questions by reading the `cweb4` terminal; the console showed none of them anywhere | Both question signals miss a question asked in prose. `transcripts.pending_question()` reads the CLI transcript for an `AskUserQuestion` tool_use with no `tool_result`, so a question merely *written* produces no block for it to find; and `_attention()` does match the text, but `classify_chat` judges a conversation by its **newest** message, so an agent that asks and then keeps working buries its own question and the highlight goes out. cweb4 asked at 13:48, 16:00 and 19:00, then produced ninety more minutes of routine output | `handle_chat_get` marks each served message with `_asks_a_question(content)` for assistant rows, and the conversation view badges them. The mark is per-message precisely so later output cannot bury it; the judgement stays server-side because the same helper backs the supervisor panel's "?" and a JS copy would drift | `tests/test_qa_chat_question_marker.py` (13 cases). `test_a_buried_question_is_still_marked` is the regression proper: ask, then four routine messages, and the mark must survive |
| 75 | A message that *discusses* the pending-question note is marked as asking a question | `_asks_a_question` tests the note with `note in body` rather than an endswith. Its own docstring argues the narrower reading — "a structured question is rendered to text **ending** in it" — so the substring form is wider than the stated intent. Found by measuring against live data: of 36 marks on the `cweb4` conversation, 35 are genuine (16 end in "?", 19 end in the note) and one is a message whose last line is `is literally *"The work is done."* with no question in it.` | **Not fixed. ⚠️ KNOWN.** The helper backs the existing supervisor panel "?", so tightening it changes that feature's precision too; it belongs to whoever owns that mark. Recorded here rather than silently inherited | None yet. A fix should assert both directions: a message ending in the note is marked, a message merely quoting it is not |
| 76 | Changing the active machine and the default model in the UI did nothing for a conversation that had already run once. Pedro configured the AI machine and the qwen model and kept getting a different model, repeatedly, across several attempts to explain it | Both turn paths wrote the model that had just *served* a turn onto `chats.model`, and `runner.get_default_model` reads that field before the backend default and the global setting. So a conversation was silently pinned to whatever answered it, by the machine, on the user's behalf. `chats.model` held two meanings -- "the user chose this" and "this is what happened" -- in one column, and routing could not tell them apart | Both write-backs removed; `chats.model` now means only the user's choice. Nothing is lost: the served model is in `usage_events` per turn, returned in the turn response, and the UI has a label for it that is not the picker. Existing pins were cleared after saving them to `data/model_pins_backup.json`, because a genuine choice and an automatic record were indistinguishable by then | `tests/test_qa_model_choice_sticks.py` (7 cases). The write-back check parses the AST rather than grepping: the removal left comments naming `chat_set_model` exactly where the calls were, so a substring search finds the explanation and reports the bug still present. Mutation-verified -- restoring either call fails it and names the line. The evidence that started it was production data, not a test: three conversations pinned to `azure_ai/gpt-5.6-luna`, which no configured backend serves |
| 77 | `bin/wc-claude.sh` repairs a transcript before resuming a session onto a different provider, and the repair silently did nothing. Observed end to end: an Anthropic conversation moved to the gateway and back gave `400 messages: text content blocks must be non-empty`, "repaired", and failed again identically | `claude-transcript-doctor.py --fix <session-id>` matched nothing. `discover()` keyed its results by session *name* only, while the usage line had always said "by name or id". The wrapper passes whatever followed `--resume`, and a session started by `claude -p` is auto-named from its first prompt -- so it is resumed by id, and the id reached nothing. It printed "no transcripts found", which reads like a healthy empty machine rather than a failed request | `discover()` keys by name or id and carries the session id alongside each path; `select()` matches either. A request that matches nothing now says so distinctly from "there is nothing here" | `tests/test_qa_transcript_doctor.py` (24 cases): signature-based poison detection, selection by either identifier, discovery of auto-named sessions, block-level trimming versus whole-record dropping, parent relinking, and that `.orig` survives a second repair. The bug was found by running a real round trip, not by reading -- the same lesson as #48, one layer up: the tool was correct, and the thing calling it could not reach it |
| 78 | A wrapper written to protect terminal sessions was shipped with a line that could not run. Its dry-run report died half way through, under `set -e`, with `bad substitution` | `${#!v}` -- length combined with indirect expansion -- is not valid bash. I wrote it, read it back twice, described the wrapper as "verified in pieces", and never executed it. The sandbox had blocked me running it five times and I treated that as an obstacle to report rather than a gap to close | Indirect expansion into a variable first, then take its length | `tests/test_qa_wc_claude_wrapper.py` (14 cases) **executes** `bash bin/wc-claude.sh` in dry-run against a throwaway database with a deliberately polluted environment, and found this on its first run. Mutation-verified: removing the `ANTHROPIC_AUTH_TOKEN` unset fails two cases, making an explicit `--model` stop winning fails one. This is #48 exactly -- nine tests once passed on a `launch.sh` block that could never succeed, because every one read the source instead of running it. Knowing that story did not stop me repeating it; writing the executing test did |
| 79 | Four checklist stages reported clean against files their subject had moved out of. Section 4's XSS scan scored every `web/*.html` at `innerHTML=0 escapeHtml=0` -- a perfect result for three files containing no JavaScript at all -- and its timer glob could not see five of the six supervisor modules | Two separate causes with the same shape. The 0.10.0 route split moved the handlers to `routes/`, and the supervisor.js split moved the client code to `web/assets/supervisor/`, which matches neither `web/*.js` nor `web/assets/*.js` -- registry #56 exactly, one directory deeper. Independently, the escaper in this codebase is `esc()`, so section 4's grep for `escapeHtml` was searching for a name that has never existed here and finding nothing, which reads identically to "no escaping happens" | Section 2's source list names every module; section 4 scans `find web -name '*.js'` recursively and greps the real helper name; section 8 enumerates `routes/` decorators as well as `add_route` | The tests were already right and that is the finding: `test_qa_timer_handles.py` uses `rglob` and pins its scope explicitly, so the suite and the hand-run checklist disagreed, with only the checklist wrong. A prose checklist has no equivalent of a find-nothing guard -- section 4's own scan returning all zeros looked like a clean bill rather than an empty search. Verified by re-running the corrected scans: 5 JS files hold timers, 8 hold innerHTML |
| 80 | A fresh database crashed on the first supervisor, task or message it ever tried to create; the running production instance never showed it | The `worktree-db-modularize` merge's `db.py` extraction had drifted from what `routes/db_supervisors.py` actually reads and writes, in four different ways across four tables -- `supervisors` missing `config`/`plan`/`progress_pct`/`completed_at` entirely, `supervisor_tasks` missing `description`/`model`/`result`/`parent_task_id`/`depends_on` (and carrying three dead columns -- `started_at`/`finished_at`/`task_list` -- that nothing reads), `supervisor_members` shaped with a surrogate autoincrement key where `supervisor_member_add`'s `ON CONFLICT(supervisor_id, chat_id)` needs the composite primary key the real schema has always used, and `supervisor_messages` missing `metadata`. This database was created under an earlier, correct schema and never recreated, so `CREATE TABLE IF NOT EXISTS` was a no-op against it every time and the drift stayed invisible. The changelog entry for the same extraction states "no schema changes" -- true of intent, contradicted by this | `_ensure_supervisor_columns` (already added for the narrower #-- see the `priority` crash this same file fixed earlier in the session) extended to ALTER each missing column into an existing database, plus the `supervisor_members` table definition corrected to the real composite-key shape. Found by comparing `db.py`'s schema text against a throwaway `sqlite3.Connection.backup()` copy of the real production database, never queried live | Confirmed against a brand-new database for every table (create/get/update supervisor, create/list tasks) before and after; `tests/test_qa_supervisor_{materialise,run,security}.py` and most of `test_qa_supervisor_members.py`, which had been failing outright, now pass |
| 81 | `ai_machine_active()`'s disconnected-database guard could raise `NameError` instead of the `sqlite3.Error` it was written to raise | `raise sqlite3.Error("database not connected")` in `routes/db_machines.py`, which never imports `sqlite3` -- an extraction artefact, the same shape as #80 one file over. Invisible to every test, because the guard only executes on the one path that is already failing, and nothing exercises a disconnected database on purpose | `import sqlite3` added. Found by flake8's F821 during a full rules.md §9 pass, not by reading -- the line reads correctly at a glance, since `sqlite3.Error` looks exactly as valid as it would with the import present | Verified by forcing `db.db_conn = None` and confirming the intended exception now raises instead of `NameError: name 'sqlite3' is not defined`. Same file also had a `PRAGMA table_info(supervisor_tasks)` dupe of the fix in #80's spirit -- worth grepping sibling `routes/db_*.py` modules for the same missing-import shape, since this session only checked the two flake8 happened to flag |
| 82 | Three CSS rules for a feature built and tested earlier the same session -- a click-cycle icon's third-state colour, its info badge's corner position, and a tooltip's styling -- were silently absent, and every source-inspection test for that feature still passed | The tree-reset incident recorded informally earlier this session (a peer's legitimate merge landed while several fixes sat uncommitted, and the reset took them with it) was recovered by reapplying each piece by hand against the post-merge files. The JS side was reapplied completely; the CSS side was not, and nothing caught the gap because every test written for the feature reads `app.js`/`machines.js` text, never `styles.css` -- the same blind spot #79 named for a different pair of files, recurring one file over from where it was last found | The three rules restored verbatim from the earlier, working version | Found by `AutoAnswerCycleBrowserTests`, which actually renders the page rather than reading its source -- the class this registry keeps citing (#37, #38, #39...) as the only kind of test that catches this shape of bug. All 6 cases in the class pass again. The generalisable point is #79's, restated: a feature spanning HTML/CSS/JS needs its regression coverage to span the same files, or a reapplication under pressure can drop exactly the file nothing is watching |
| 83 | A supervisor's id and status string reached the DOM unescaped in the one card-list template that renders every supervisor, one directory and one file over from where the same class of gap was fixed for task status (#82's neighbourhood, different bug) | `web/assets/supervisor/list.js`'s `renderSupervisorList()` interpolated `s.id` into a `data-id` attribute and `statusClass` into a badge's class and text without `esc()`, while the sibling `renderChatMessages()` in the same file escapes every value it writes. Both values are server-controlled enums/UUIDs today, so nothing exploits it now -- the same currently-inert shape as the `task.status` fix, found by the same full-file review rather than by any grep, since a per-file `innerHTML`/`esc` count cannot see which of several sites in one file is missing the call | Wrapped both in `esc()` | Not yet covered by a dedicated regression test; `SupervisorListBrowserTests` (if one exists) should assert on a title/id containing `<script>` the way the auto-answer/task-status fixes did |
| 84 | `WrapperTests::test_it_opens_the_database_read_only` failed against current `HEAD` -- not a regression from any change this session, a pre-existing drift never caught before this pass | The test asserted `"mode=ro" in bin/wc-claude.sh`'s source, but the actual `mode=ro` connection string had moved to `bin/wc-backend-env.py` when the wrapper's DB read was refactored into that helper. Same shape as #56/#79: a test's literal target drifted out from under it and nothing re-pointed the assertion | Fixed: assertion now reads `bin/wc-backend-env.py`, the file that actually holds the guarantee | `test_it_opens_the_database_read_only` passes again |
| 85 | 13 of `test_qa_wc_claude_hotswap.py`'s cases failed against current `HEAD`, with two distinct symptoms mistaken at first for one: some printed a resolved path where a bare `claude` was expected, most printed nothing at all -- the fake claude on `$PATH` was never invoked | (a) `test_explicit_model_flag_always_wins` hardcoded `would exec: claude`, but the resolver always prints the fully resolved binary path -- a property of the box, not the repo. (b) The other 12 fixtures inject a fake `claude` by prepending it to `$PATH`, but `resolve_claude_bin` (bin/wc-claude.sh) checks `$WC_CLAUDE_PATH`, then `~/.local/bin/claude-real`, then the newest `~/.local/share/claude/versions/*` -- all *before* falling back to `$(command -v claude)` -- so on any host with a real CLI installed (every dev box), the real binary always won and the fake one on PATH was never reached. One test's symptom (a resolved path shown) generalised wrong at first to all 13; running the others individually showed the real, different failure (b) | (a) assert on the argument shape (`--resume test1 --model claude-haiku-4-5`), not the resolved executable. (b) pin the fake claude through `$WC_CLAUDE_PATH`, the override seam the script already provides for exactly this, in all three fixtures that inject one (`DryRunResolutionTests`, `HotswapLoopTests`, and the screen-based acceptance class) | All 13 pass |
| 86 | A hot-swap mid-session -- deactivating the active machine, or switching to a non-anthropic one -- left the *previous* machine's `ANTHROPIC_API_KEY` exported into the child `handoff_unmanaged` starts, instead of clearing it. Fixing #85's PATH-injection bug made this reachable by a test for the first time; it was invisible before because the fake claude was never actually invoked | `backend_env.deltas()` -- the single shared definition used by `claude_proxy`, `runner`, and the shell wrapper's `apply_env` -- only unset `ANTHROPIC_API_KEY` inside the anthropic-with-empty-key branch. Its early-return branch (`not isinstance(backend, dict) or backend.get("provider") != "anthropic"`, covering both "no active machine at all" and "a non-anthropic backend") unset `ANTHROPIC_BASE_URL` but never `ANTHROPIC_API_KEY`. Harmless for the proxy/runner, which build each turn's child environment fresh from a copy of the long-lived service's own environment -- there is nothing stale to leak. Live and exploitable only for the shell wrapper, the one caller that is itself a single long-lived process incrementally re-exporting into its *own* environment across a hot-swap, so a key `apply_env` set for an earlier machine survived a later call that should have cleared it | `unset.append("ANTHROPIC_API_KEY")` added to the early-return branch, so any non-anthropic-with-key backend clears it unconditionally. Deliberately *not* extended to `CLAUDE_CODE_SIMPLE` in the same branch -- no test demands it and the proxy/runner never exhibited a related symptom, so touching it would be a speculative behaviour change riding on a security fix, not the fix itself | `test_a_mid_session_deactivation_hands_off_unmanaged`. `tests/test_qa_backend_env.py`'s frozen `GOLDEN` equivalence table (capturing `claude_proxy._backend_env`'s shipped output, 2026-09-03) had to be updated for the four affected rows -- it existed to catch the proxy silently diverging from `deltas()`, not to pin the old value as correct, and both sides moved together under the fix, confirming the proxy still delegates rather than keeping its own copy |
| 87 | `PollerTests` (`test_qa_wc_claude_hotswap.py`) failed with the poller silently never detecting a real backend change, no matter how long the test waited | Its `_script()` fixture builds the poller under test by slicing the function definitions out of `bin/wc-claude.sh`'s source text and running them via `bash -c "<spliced text>"`, specifically so the code under test is the real implementation. But one of those spliced lines is `HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`, and `${BASH_SOURCE[0]}` under `bash -c` names the `-c` string itself, not `wc-claude.sh` -- so `HERE` resolved to `/` (or emptier), `query_backend`'s `python3 "$HERE/bin/wc-backend-env.py"` pointed at a file that does not exist, and the poller's own `\|\| continue` guard against a *transient* query failure (this same fixture's other test exists to prove that guard works) swallowed it silently on every tick, forever | Re-pin `HERE` to the real repo root immediately after the spliced text, overriding the broken self-detection | All 5 `PollerTests` cases pass; confirmed via `set -x` trace showing `python3: can't open file '//bin/wc-backend-env.py'` before the fix and a clean poll after |
| 88 | Minutes after `ada8e13` fixed the app.js `?v=31`/`?v=32` split (registry-shaped: two module instances, two `state` objects) and was committed, the exact same three lines were back on disk, uncommitted, as if the fix had never landed: `index.html` back to `app.js?v=31` and `styles.css?v=31`, `app.js` back to importing `conversation.js?v=2`, and `styles.css` missing the `.conversation-resource` rules a different in-flight feature (a PDF comparison report link) depends on. A full rules.md pass caught it because `test_qa_asset_module_versions.py` failed again immediately, and the failure cascaded into 28 more: every real-browser test that drives the page timed out waiting on elements `app.js` never got to wire up, since the page was silently running two `state` objects again | Something -- most likely an editor or session holding a pre-`ada8e13` buffer of these three files open — saved over the committed fix. Confirmed each file's *entire* uncommitted diff was nothing but the reversion (`git diff --stat`: 2, 1, and 5 lines respectively) before touching anything, specifically to rule out clobbering someone's real in-progress work under the same three filenames | `git checkout -- web/index.html web/assets/app.js web/assets/styles.css`, restoring exactly what `ada8e13` had already shipped. Not a code fix -- the fix already existed in `HEAD`; this was pure recovery of a live regression | `test_qa_asset_module_versions.py` (2 cases) plus the 28 cascaded failures (`test_frontend_browser.py`'s Supervisor/DeviceAlert/AutoAnswerCycle classes, `test_qa_last_command_picker.py` in full, `test_qa_composer_chat_name.py`) all pass again. **Open**: whatever re-saved the old buffer can do it again; nothing in this pass identifies which session or process it was |
| 89 | Auto-answer, and answering a terminal prompt from the browser, both stopped catching anything -- with zero trace in the logs. Not a race lost to a fast human, though that was the first theory and it fit the symptom just as well: found only by checking a *currently still live* permission prompt directly against `prompts._is_claude`, mid-incident, which returned `False` for a process independently confirmed running and correct via `ps` | `_is_claude`/`_is_claude_process` trust `/proc/<pid>/comm` alone. The CLI installs each version as `~/.local/share/claude/versions/<version>` and execs that path directly, so `comm` is the bare version string (`2.1.260`) -- containing no "claude" -- and every armed session on the host failed the check at once. Silent because a failed identity check here is indistinguishable from "nothing is pending": `session_pid` returns `None`, `locate`/`find_target` return `None`, and `consider()` returns `None` before ever reaching `_record`, so not even a "skipped" row was written. Confirmed against the exact live-waiting process (`ps` showed `Rl+`, 4m47s elapsed) before touching any code | Fall back to `cmdline`'s first argument -- still the full launch path, still contains "claude" -- when `comm` does not match | Reproduced with a real spawned process (copy of `/bin/sleep`, renamed to a bare version string, launched from a path containing `claude/versions/`) rather than the mocked `_is_claude` every existing test used -- which is exactly how this shipped unnoticed. Mutation-verified: reverting the fallback fails the new test immediately. Live: the fix resolved `locate()` against the still-waiting cweb3 session correctly before deploy |
| 90 | Writing a browser-based XSS regression test for #83 (supervisor `id`/`status` unescaped) produced two tests that passed identically whether the fix was present or reverted -- twice, for two unrelated reasons, on the first attempt each time | (a) A `<script>` payload inside `data-id="..."` proves nothing: text inside an attribute's quotes is never parsed as markup regardless of escaping, so the payload needed a bare `"` to break out, which `esc()` did not touch -- it only ever escaped `&`/`<`/`>`, never quotes, so the "fix" applied to `s.id` in registry #83 was a real no-op for the position it was applied to (harmless only because a UUID cannot contain a quote today). (b) Once corrected to a real quote-breakout (`onmouseover=`) and an `onerror`-`<img>` payload for the text-node `status` case, asserting on *execution* (a global the payload sets) still passed under both fixed and broken code: `Content-Security-Policy: script-src 'self'` blocks every inline `on*` handler regardless of what escaped it, so the CSP header -- not the fix -- was the thing the test was actually measuring | `esc()` extended to also escape `"`/`'` (it round-trips through `div.textContent`/`div.innerHTML` already; a harmless no-op everywhere it is used in text-node position, real protection where it is used in an attribute). Tests rewritten to assert on DOM *structure* -- `getAttribute("onmouseover")` is `null`, no `<img>` element exists -- rather than on a handler firing, so CSP blocking execution cannot masquerade as the escaping working | Mutation-verified against three separate reverts: quote-escaping removed (id test fails), `esc()` removed from `status` entirely (status test fails), each restored (both pass). See §16a below -- this entry is the reason it exists |
| 91 | `POST /api/chats/search` 500'd unconditionally -- `ModuleNotFoundError: No module named 'db_chats'` -- on every request, found by a full `run full rules.md` test pass rather than by anyone hitting search live | `handle_chat_search` (`routes/chats.py`) did `from db_chats import _fts_validate_query`, a bare top-level module name left over from writing the FTS5 query-validation check inside `routes/db_chats.py` itself and not updating the importer to match -- the module is `routes.db_chats`, not `db_chats` | `from routes.db_chats import _fts_validate_query` | 11 already-existing tests (`SearchAPITests`, `ChatSearchApiTests`, `CrossForkSearchTests`) caught this the moment the full suite ran; no new test needed, since coverage already existed and simply hadn't been exercised in the same run as the breaking change |
| 92 | `POST /api/tokens` 500'd on every creation (and, downstream, the backup and settings-patch endpoints too) | `handle_api_token_create` (`routes/misc.py`) calls `db.admin_action_record(...)` for the audit trail, but `admin_action_record` (defined in `routes/db_users.py`) was never added to `db.py`'s `__getattr__` dispatch table -- the same extraction-completeness gap as registry #80/#81, one function further out. `db.<name>` for an undeclared name raises `AttributeError`, past the point the token was already created and logged, so the token existed with no record of it having been issued | Added `"admin_action_record": "routes.db_users"` to the dispatch table | `TokenManagementApiTests` (4 cases), `SettingsApiTests` (2), `BackupAPITests`/`BackupApiTests` (5) all caught this the same run; all seven now pass with no new test needed |
| 93 | Voice handoff summarization silently produced nothing for any conversation without a pinned model -- the exact case its fallback exists to handle. No error, no log line, no failed request | `routes/voice.py` called `runner.get_default_model()` and never imported `runner`. The surrounding `except Exception: return None` caught the resulting `NameError`, so the fallback path failed identically to "no summary available" and could not be told apart from it. Three sibling route modules already `import runner` and there is no cycle -- `runner` imports only stdlib, `backend_env` and `config` | `import runner` in `routes/voice.py` | `tests/test_qa_no_undefined_names.py` -- a pyflakes gate over every module, written for the class rather than the site, because a unit test here would need a chat with no pinned model *and* an assertion that the fallback produced something. Mutation-verified: deleting the import fails the gate |
| 94 | Every `bench/judge_delegation.py` grading fallback reported `parse failed`, so a proxy error was indistinguishable from unparseable model output and the real cause of a zeroed score was never recorded | The diagnostic read `{e if 'e' in dir() else 'parse failed'}` after an `except Exception:` that never bound an `e`. `'e' in dir()` was therefore always false, the `e` branch was unreachable, and the string was a constant wearing a conditional | `except Exception as exc` with the last error captured as `f"{type(exc).__name__}: {exc}"` and printed | Same gate as #93 -- the old form was itself an undefined name. Mutation-verified: restoring the `'e' in dir()` expression fails it |
| 95 | All 35 tests in `test_qa_supervisor_map_geometry.py` failed with `TypeError: not a function`, and read as 35 behavioural regressions from the horizontal-layout restructure. The harness had in fact stopped exercising the module at all | The restructure chained `.nodeSize()` onto `d3.tree()`, and `tests/js/d3_dom_stub.js` implemented only `.size()` and `.separation()`. Every test died inside `renderSupervisorMap` before reaching anything it asserted on. Nothing compared the d3 surface the module calls against the surface the stub provides, so a one-method gap presented as a broad failure | `.nodeSize()` added to the stub's tree layout with d3-hierarchy's real semantics: one flag, both setters write dx/dy, last call wins, and `tree.size()` reads back null once nodeSize is in effect. Faithful deliberately -- a stub honouring both would make a module calling both look correct while behaving differently in a browser | `tests/test_qa_d3_stub_fidelity.py` (7 cases, 3 subtests). Mutation-verified against three reverts: removing `.nodeSize()`, making the stub honour both setters, and collapsing the two modes onto one formula. The third initially passed -- the fixture was two levels deep, where `depth * dy` and `(depth / maxDepth) * dy` are the same number -- and the fixture is three levels now |
| 96 | `web/assets/supervisor-map.js` never applies its canvas dimensions to the tree layout, so the map's measured box does not affect node placement. **Fixed in `064189f`** | Lines 221-224 call `.size([CANVAS_H - verticalPadding, CANVAS_W - horizontalGap])` and then `.nodeSize([20, horizontalGap])` on the same layout. In d3-hierarchy those are mutually exclusive: both write dx/dy and the flag set by the later call decides how they are read, so the `.size()` values are overwritten and gone. Whichever of the two lines was meant, the other is dead | Open. Whoever owns the restructure should delete one of the two calls | `MapGeometryTests::test_the_layout_spacing_is_fixed_and_not_a_fit`, and it asserts on the *setter calls* rather than the resulting values -- mutation showed no value-based assertion can work here, because re-adding `.size()` before `.nodeSize()` leaves `tree.size()` reading back null in d3 and in the stub alike, so the first version of this test passed against the exact defect it was written for. The stub now records the setter sequence in `STUB.treeCalls`. Both mutations caught: adding `.size()` back alongside, and swapping `nodeSize` for `size`. All 49 tests in the file pass **Correction, and the reason #96 should not have been marked simply "fixed": removing the dead call was right, and it did not fix what the dead call was pointing at.** The map still does not fit its canvas, and a browser shows it -- `test_frontend_browser.py::SupervisorMapBrowserTests::test_the_tree_is_rendered_at_a_visible_size` fails with a node circle at x=1258 against an SVG right edge of 1249, i.e. 9px outside the drawable box, on a 900x600 panel. That test is in `LOCAL_ONLY` and the local browser pass has been blocked by the §0 memory gate, so it only ran once the set was sent to a QA node. Established that my change is not the cause rather than asserting it: rendering the pre-`064189f` module and the current one through the quickjs harness gives byte-identical node transforms, with `tree.size()` reading back null in both -- the fixed spacing was already in force. So the overflow predates the removal and belongs to the horizontal restructure choosing fixed `nodeSize` spacing with no fit-to-canvas step, which `zoomToFit()` frames but does not bound. **Still open.** **Closed in `ead81ff`, and the remaining half was a defect rather than the design question I had called it.** `zoomToFit` computed bounds from raw `d.x`/`d.y` while the render drew at `MARGIN_LEFT + d.x + LEVEL_GAP, MARGIN_TOP + d.y`, so it centred a rectangle offset by (90, 40) layout units from the one on screen and the tree sat that far right-and-down of centre, scaled -- a few pixels at real scales, which is why it read as "a node is 9px outside the box". Measured on a 6x5 tree at two panel sizes: before, 4 nodes outside with the rightmost at x=1327 against a 1249 edge; after, 0 outside with the rightmost at x=1170. The geometry constants are module-level now and `_nodeXY` is the one definition of where a node sits, called by the render and the fit both -- two copies of that expression is what let them describe different pictures. The design question is settled rather than deferred: fixed `nodeSize` spacing stays, because the map is zoomable and `zoomToFit` frames it, and fit-to-canvas was a radial requirement that does not survive the horizontal layout. |
| 97 | Four tests passed when their file ran alone and failed in a full run, which is why they survived the commits that introduced them | Three were process-global state and one was a host-dependent literal. (a) `routes/misc._settings_cache` is module-level with a 30s TTL; the production path is correct -- `handle_settings_patch` invalidates -- but tests build state *underneath* the endpoint via `setting_set`, fresh fixture databases and a different backend per subtest, none of which is a PATCH, so the endpoint correctly served the previous test's payload. (b) `test_qa_voice_model_per_backend` still stubbed `db.setting_get` after the handler switched to a single `setting_get_all` batch read, so every value read empty, `voice_backend_id` fell through to `config.VOICE_BACKEND_ID_DEFAULT`, and the handler's own validity check nulled it. (c) `test_qa_usage` hardcoded `vllm/Qwen3.6-35B-A3B-NVFP4` as its "second" model while `WC_TESTING_MODEL` resolves to that same id on this host, collapsing two dict keys into one. (d) The agent OOM opt-out test asserted a literal `"0"`, but opting out means *write nothing*, so the process keeps what it inherited -- it broke when the running CLIs on this host were backfilled to 200 and pytest became a child of one | (a) autouse reset in `conftest.py`, matching the rate-limiter fixture beside it. (b) stub `setting_get_all` too, and invalidate in the test's `_settings` helper since switching backend there mutates a dict rather than issuing the PATCH that would invalidate. (c) derive the second id and assert the two differ. (d) compare against the parent's own score. `bin/wc-claude.sh` was unchanged and correct | The four test files themselves; each failed before its fix and passes after. (d) additionally mutation-verified: removing the opt-out guard from the wrapper fails both opt-out tests |
| 98 | `config.VERSION` moved to 0.16.0 and none of the five display surfaces followed, and `CHANGELOG.md` had no section for the release -- the **second consecutive** release to half-land in exactly this way (registry-adjacent: the 0.15.4 CHANGELOG entry records the same thing against 0.15.3) | A version bump is six edits in five files plus a changelog section, and nothing in the commit path requires them to travel together. `tests/test_qa_version_consistency.py` is the only thing that has caught it either time, and it caught it both times only because a full run happened afterwards | `ARCHITECTURE.md`, `web/index.html`, `web/orchestrator.html` (title and topbar) and `web/assets/orchestrator/main.js` set to 0.16.0, plus a `[0.16.0]` CHANGELOG section covering the six commits in the release | `test_qa_version_consistency.py` (6 failures before, all passing after). Worth noting it is a detector, not a preventer: it cannot fail until someone runs the suite, which is why this has now shipped twice |
| 99 | Three defects in `routes/db_sessions.py`. **Now fixed**, by the owning sessions rather than by me | The in-flight session-cache feature carries (a) a second `read_claude_sessions` definition shadowing the older one, leaving 80 lines of unreachable code (`F811`), (b) a `_dedupe_sessions(merged)` result computed and discarded, with the dedupe that matters happening a few lines later, and (c) `_read_claude_sessions_sync()` called unwrapped from two `async` functions -- it globs a directory, reads every JSON file, and runs a SQLite lookup plus a PID check per session, all on the event loop, while the remote read on the very next line correctly uses `asyncio.to_thread` | Closed. Remote discovery is gated behind a config flag (`60b4bde`, `0cf3e7d`), which was the design decision the 17 failures needed and not mine to make; my three fixes landed in the same area via the owner's commit. Re-measured after: `tests/test_qa_sessions_read.py` is **31 passed in 1.21s**, back to its pre-feature figure, and `flake8 --select=F811,F841,F821` is clean on HEAD. The history below is kept because the diagnosis took three sessions and one of my conclusions was wrong. Original note: The three fixes above were made, verified behaviour-neutral, and then left uncommitted -- because committing that file ships the feature they sit inside, and the feature is broken: `tests/test_qa_sessions_read.py` is **31 passed in 1.21s** against HEAD and **17 failed, 14 passed in 305s** with the working-tree version, identically before and after my fixes. The cause is a fourth defect, larger than the three: `read_claude_sessions()` now queries remote transports unconditionally, so every existing test of it reaches four real hosts over SSH. A test patching `db._CLAUDE_SESSIONS_DIR` to a temp dir and expecting one session gets 14 real ones back. Clearing the cache did not help when measured (17 failed, 344s), which led me to conclude the remote reads were being hit on every cache miss -- **that conclusion is now doubtful and the number should not be reused.** A peer (cweb2) independently hit the same defect from production and found a second cause I was measuring without knowing: the refresh loop spawned `update_sessions_cache()` with `create_task` every 3s and never awaited it, so passes stacked several deep at four transports of blocking SSH each. Fixed in `fa5c5ee` (one pass at a time, on `config.REMOTE_SESSION_CACHE_S`, default 60s). My 344s may have been measuring the stacking rather than the miss path; it has not been re-measured since. The same session also fixed a genuine outage in `4a81f37`: `app.py` awaited `tunnel_manager.start()` inside lifespan startup, making SSH connectivity a precondition for binding the port -- `/login` was answering in 75 seconds, and is 8-26ms now. Remote discovery needs gating, or the tests need it patched; either is a design decision for whoever owns the feature. (c) is the same class as registry #18 | Not written. `tests/test_qa_no_undefined_names.py` deliberately scopes itself to undefined names and does *not* assert `F811`, because a gate that fails on another session's uncommitted edit trains people to skip the suite. Add the `F811` assertion once the tree is quiet |

| 100 | `webconsole.service` was `active` but `disabled`, so the site would not have come back after a reboot. Nothing was wrong with the running server, which is exactly why this was invisible: every health check, every request and `systemctl is-active` all reported fine | The unit had been started but never enabled -- no `default.target.wants` symlink -- while `webconsole-proxy.service` and `webconsole-health.timer` both were. `Linger=yes` was set, so the units survive logout; that is a different property from surviving a reboot, and having one is easy to mistake for having both | `bash bin/wc-install-supervision.sh`, which is idempotent and created the missing symlink (it also restarted the app, picking up peers' committed Python changes in the same step) | §17's own first check, `systemctl --user is-enabled` on all three units, which is why that line lists three units rather than asserting on the one being deployed. All four recovery cases then passed: crash restart, independent proxy restart, the wedged `SIGSTOP` case that only the health check can see, and a healthy server left untouched |
| 101 | Every one of the 19 tests reaching `_open_backends()` in `test_frontend_browser.py` timed out on `wait_for_selector(".machine-card")`, and the browser pass had been reading as a broad frontend regression | Two correct features with no card between them. `loadBackends()` calls `_collapseAllTransportGroups()` before rendering (Pedro's request, pinned by `test_qa_backend_groups_collapse_on_open.py`), and a collapsed group renders **no** cards at all -- `_renderMachineList` appends the group header, then builds cards only for groups absent from `_collapsedGroups`. So after opening the panel there were zero `.machine-card` elements in the DOM and the wait could not succeed. Nothing was wrong with the product | The helper waits for `.transport-collapse-toggle` -- which exists collapsed or not, so it is the honest "panel has rendered" signal -- expands one collapsed group, then waits for a card. One, not all: collecting every toggle upfront and clicking each detaches the handles, because a click re-renders the list (`ElementHandle.click: Element is not attached to the DOM`) | The 19 tests themselves. Signature, not count: before, all died on a 10s `.machine-card` timeout and the two classes took ~359s; after, that timeout does not occur and they take ~60s. **The count is not a usable measure here** -- identical code gave 5/7, 7/5 and 6/6 across three runs on the node, and an earlier three-way "variant comparison" of mine (3/9, 9/3, 6/6) was reading that noise as signal, which is retracted in `b2a14ae`. Residual failures are that flakiness plus a node fact: the seeded Anthropic backend carries only `config.ANTHROPIC_MODEL`, so the picker offers `['', 'claude-sonnet-5']` and tests expecting a probed list need an API key and network |
| 102 | `test_frontend_browser.py`'s `_close_settings_and_reopen` had two bugs stacked, and could never have run to completion in any form | `page.wait_for_selector("#settingsDialog", is_hidden=True)` -- Playwright takes `state` from {attached, detached, hidden, visible} and has no `is_hidden` parameter, so every call raised `TypeError`. Fixing that exposed the next line, `self._load()`, which is defined on `SupervisorBrowserTests` and one other subclass but not on `_BrowserFixture` where the helper lives, while its sole caller is `BackendsPanelBrowserTests`: `AttributeError` every time | `state="hidden"`, and the fresh page load the docstring describes, via `self.base` as `_login` already does | Verified against the installed Playwright signature rather than from memory, and the rest of the file checked: `state=` and `timeout=` are the only kwargs used anywhere else. Also scanned every class for the same shape of mistake -- a `self._helper()` call resolving to nothing through the MRO -- and this was the only instance. Both in `75db75d` / `7d761a6` |
| 103 | Three tests failed on a QA node reporting `app.js` referenced at two versions (`?v=53` alongside `?v=56`, and in an earlier run `?v=54`), which reads as the ES-module identity split this repo has shipped more than once -- the browser loading `app.js` twice with two `state` objects. HEAD was correct the whole time | §14's sync is `git ls-files -z | tar ... | ssh "tar -xzf - -C $REMOTE_DIR"`, and `tar -xzf` over an existing directory only adds and overwrites: it never removes. So a file deleted in this repo stayed on the node indefinitely, and any test globbing a directory rather than naming files kept finding it. `web/assets/voice-conversation.js` was deleted here in `07d5b41` and was still on `kali-3` hours later carrying `app.js?v=53`. The failure is expensive precisely because it looks real and points at code that is already correct: a peer investigated it against a clean `git archive` export of HEAD before we worked out it was the node's own leftovers, and I had earlier misattributed the `{'56','54'}` form of it to a peer's mid-edit working tree | The tracked tree is cleared before extraction: `find $REMOTE_DIR -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +`. `.venv` is preserved because it is minutes of pip installs and, not being tracked, cannot go stale this way. `REMOTE_DIR` is guarded against empty/`/`/`$HOME` first, since this is an `rm -rf` over SSH | Verified on the node rather than reasoned about: after the corrected sync the stale file is gone, `app.js` resolves to one version (13 references, all `?v=56`), the venv survived, and `test_qa_asset_module_versions` plus `test_qa_version_consistency` are 10 passed / 5 subtests there. Before it, the same files failed Two method notes from working it out, both from the peer who investigated it. A clean `git archive` export of HEAD is the right way to rule out working-tree contamination, and it **only rules it out for the files you actually re-run in the export** -- theirs re-ran the browser classes but not `test_qa_asset_module_versions`, so the export was clean and silent about the very test that was failing. And the reasoning that misled both of us was the same shortcut in two directions: "the file is not in my `git status`, therefore it is committed, therefore it is at HEAD" skips the third possibility, which is that it exists on the node and nowhere else. Checking HEAD directly (`git ls-tree HEAD`, `git show HEAD:<path>`) is one command and settles it. |
| 104 | Settings → Backends rendered nothing, no console error, on at least two separate occasions in the same session -- reported by Pedro as "I don't see any of the transports nor the machines" | The recurring module-split bug: `bin/wc-asset-versions.py` content-hashes each asset and rewrites its `?v=` query string, but a same-turn edit to a dependency (`transports.js`, `device-alerts.js`) without re-running the tool leaves the importer (`app.js`, `supervisor-map.js`) pinned to the old hash. ES module identity is keyed by the full URL including the query string, so the browser loads two separate module instances -- one with the real state, one empty -- and the empty one is what renders, silently | `python3 bin/wc-asset-versions.py` (no-arg mode rewrites and self-verifies), then `--check` to confirm zero drift. Re-ran after each recurrence in this session | `test_qa_asset_module_versions.py` (pre-existing, from #103's fix) asserts every `?v=` reference in the tree resolves to the same hash as the file it names; it is a detector, not a preventer -- nothing stops a same-turn edit from reintroducing the drift before the next check runs |
| 105 | Every chat creation carrying a user-given title and no `"prompt"` field crashed live, in `routes/chats.py`'s `handle_chat_create`, with `UnboundLocalError` at the `elif session_id and not data.get("title"):` line | `session_id = data.get("session_id")` was assigned only inside the `if title == "Untitled":` branch, so a request with a real title never defined it, and the `elif` a few lines later read a name that was never bound on that path | Moved `session_id = data.get("session_id")` unconditionally, immediately before `title = user_title if user_title else "Untitled"`, so both branches see it | `tests/test_anchored_naming.py`'s new cases (`test_a_user_typed_title_takes_the_task_slot_once`, `test_a_user_typed_title_never_updates_again`) exercise exactly this path -- titled creation with no prompt -- and both failed with the same `UnboundLocalError` before the fix |
| 106 | `webconsole.service` was down for 2h 36m (20:08:41-22:44:30), crash-looping five times in seconds and hitting systemd's `StartLimitBurst`, then sitting `failed (start-limit-hit)` rather than retrying -- silent, because nothing was polling it | `dbfd252`'s Design Specs Gallery feature added `import nh3` to `specs_gallery.py`. `requirements.txt` was updated and `.venv` had it, so every test run and `.venv`-based check was green. The live service, per `launch.sh`, runs under bare `python3` (system, `/usr/lib/python3/dist-packages`) -- not `.venv` -- and that interpreter never got the new dependency, so every restart died at import with `ModuleNotFoundError: No module named 'nh3'` before uvicorn could bind a socket. The same system/`.venv` split #50, #65 and #51 documented for the *test* suite turns out to apply to the *deploy* target too, in the opposite direction: there, the wrong interpreter silently skips real coverage; here, the right interpreter (system) is the one that never saw the new package | `python3 -m pip install --user --break-system-packages nh3==0.3.7` (matches the pin; system `python3` has user-site enabled, so this needed no `sudo` and did not touch `dist-packages`), then `systemctl --user reset-failed webconsole.service && systemctl --user restart webconsole.service`. Confirmed serving (`startup_step` lines through `Application startup complete`, then a real HTTP response) rather than just `active (running)`, since #100 already established that a green unit state is not the same as a working one | None written yet. The gap is real: nothing in §14's dependency gate (`.venv/bin/pip install -r requirements.txt --dry-run`) or anywhere else checks that a new `requirements.txt` entry is also importable under the interpreter `launch.sh` actually execs. A candidate is a preflight step that diffs `python3 -c "import X"` against every top-level import newly added to `requirements.txt`, but that needs a "new since when" baseline this registry does not currently have a place to keep |
| 107 | Settings > Specs showed no files at all -- reported by Pedro as "I don't see any file / spec" -- while the backend held 22 real spec documents | `specs_gallery.discover_specs()`/`enrich()` were not the defect: called directly they find and render all 22 correctly. `specs.js`'s `loadSpecs()` was: `if (!response.ok) return;` and a bare `catch { return; }` treated any failed request -- an expired session (`401 "Session expired"`, reproduced by hitting `/api/specs` directly with no cookie), a 500, a network error -- identically to a genuinely empty gallery. Nothing distinguished "the request failed" from "there are truly no specs" | Surfaces the failure through `notifyResult`, the same convention `_deleteSpec` already followed a few lines below it in the same file | `tests/test_qa_specs_load_failure_surfaced.py`, asserted from source (no JS runner covers this file, same convention as `test_qa_backend_status_on_first_paint.py`). Mutation-verified: reverting the fix (`git stash` on `specs.js`/`app.js`) fails all three new cases against the original code |
| 108 | Settings > Specs took 15.2s to load 23 specs -- reported by Pedro as "loading the specs files takes a long time" | Two independent causes. (a) `enrich()`'s `find_references()` re-ran a fresh `grep -rl` over the *whole repo tree* once per spec -- 23 subprocess spawns at ~0.55s each, 12.55s of the 15.2s. (b) `discover_specs()`'s `Path.rglob("*.md")` walks into `.venv`/`.git`/`node_modules`/`__pycache__`/`.claude` before an after-the-fact `parts` check discards what it found there -- 49 of this repo's 157 `.md` files are under `.venv` alone, 2.7s of the 15.2s | (a) `find_all_references()`: one `grep -rlF` across every spec's filename finds every referencing file in one tree walk, then one `grep -HoF` across all of those files attributes each hit back to which spec(s) it mentions -- two subprocess calls total, not one per spec. `enrich()` takes an optional precomputed list so its single-spec contract is unchanged; only the list endpoint batches. (b) `os.walk` with `_EXCLUDED_DIRS` pruned from `dirnames` in place, so the walk never descends into an excluded directory rather than filtering it out afterward | New tests in `tests/test_qa_specs_gallery_enrich.py` (`FindAllReferencesTests`, including one asserting the batched result equals calling `find_references()` once per name) and `tests/test_qa_specs_gallery_scan.py` (a timing-bound test against 3000 decoy files under `.venv`, 0.1s threshold). Both mutation-verified: the timing test measured 0.209s against the pre-fix `rglob` version and passes at well under 0.03s with the fix. A read_text()-call-counting version of the same test was tried first and passed against *both* old and new code -- old and new alike skip reading an excluded file's content via the same post-glob `continue`, so counting reads does not distinguish them; only the traversal cost differs, which timing measures and a call count does not |
## 16a-audit (this run): §16a ran against entries #93-99. #93 and #94 share one new gate (`test_qa_no_undefined_names.py`), mutation-verified by restoring each bug separately; both are undefined names, which is why the gate checks the class rather than the two sites. #95 has `test_qa_d3_stub_fidelity.py`, mutation-verified against three reverts -- and the third revert passed on the first attempt, so the fixture was deepened until it failed, which is exactly the check this stage exists to force. #97's four fixes are each covered by the test that was failing, verified before and after, with the OOM one additionally mutation-verified against the wrapper. #98 is covered by the pre-existing `test_qa_version_consistency.py`, which is a detector rather than a preventer and is recorded as such. #96 was subsequently fixed in `064189f` and its test is described in its own row -- worth reading, because the first version asserted on the layout's read-back values and passed against the defect, since d3 reports `tree.size()` as null whether nodeSize is the only sizing call or merely the last one; the assertion moved to the setter call sequence. #100 was found by §17's first check and is covered by that check. #103 is covered by the two version tests it was breaking, checked green on the node after the fix and red before. #101 and #102 are covered by the 19 tests that were failing, all of which now reach their own assertions; #101's entry states why its pass/fail *count* is not evidence and what is. Both were found only by sending the LOCAL_ONLY set to a QA node, which is the argument for doing that while the §0 gate keeps the local browser pass unavailable -- and the node turning out to have Chromium and Playwright browsers already installed means §14's "no Chromium is installed there" note is stale. #99 stays open and is the one entry with no test: the fixes are written and verified behaviour-neutral, but the file cannot be committed without shipping the broken feature they sit inside, and the measurements in that row say why. Nothing skipped.

## 16a-audit (previous run): §16a ran against entries #91-92. Both were caught by pre-existing tests the moment the full suite ran -- no gap to fill, so no new test was written for either. #85/#86 (the run before) already have their tests confirmed still passing. Nothing skipped.

## 16a. Regression test coverage (mandatory -- never `⏭️ SKIP` on a run that changed code)

**Every fix this run makes needs a test that fails without it, checked by actually
reverting the fix and watching the test fail.** A test that passes identically
before and after a fix proves nothing and is worse than no test: it reads as
coverage in every report from here on, and the next person to touch that code
trusts it. Registry #90 is the reason this stage exists, found while writing a
test for #83 -- two different, unrelated ways for a "regression test" to pass
regardless of the bug, on the same feature, back to back.

**Do this after §16, using the same list of fixes:**

1. **For every entry added to §16 this run, and every `### Fixed`/`### Security`
   line added to `CHANGELOG.md` this run**, check whether an existing test
   already exercises it. Read the test, do not take its name's word for it.
2. **Write one for anything not covered.** Match what the bug actually is, not
   the nearest existing pattern:
   - A behaviour a browser renders, times, or executes (DOM structure, timers,
     CSS, focus, scroll position) needs a real `Playwright` render. Registry
     #79/#82/#84 are the standing reason: a source-text assertion passes
     against a version that never reaches the code path it claims to check.
   - A source *invariant* (every `setInterval` is held, every module is
     imported at one version) is honestly tested by reading the text, the way
     §1's compile gate and `test_qa_timer_handles.py` do -- the property is
     about the text.
   - Pure Python logic (a parser, a query builder, a `deltas()`-shaped
     function) gets a direct unit test with no server, no browser, no mock of
     the function under test itself. Registry #85's own fix would have shipped
     mocked-and-passing forever, the same way it shipped broken forever: every
     prior test for `_is_claude` patched `_is_claude` itself.
3. **A security-shaped test must survive an XSS/CSP sanity check before it is
   trusted**: does the payload actually reach the sink this fix touches (an
   attribute value needs a quote to break out of; a text node needs a `<`/`>`
   to become an element; a `<script>` tag inserted via `innerHTML` never
   executes, in any browser, and proves nothing either way)? Does anything
   *else* already in front of the sink -- a CSP header, a framework's own
   escaping -- block the payload regardless of the fix being tested? If yes,
   assert on DOM structure (`getAttribute`, element existence, `textContent`)
   instead of on the payload firing. Registry #90, both halves.
4. **Mutation-verify anything nontrivial before trusting it**: revert the fix
   (comment it out, restore the buggy line), run the new test, confirm it
   fails with the reason you expect -- not a fixture error, not a timeout for
   an unrelated cause. Restore the fix, confirm it passes again. This is the
   single check that would have caught both halves of #90 immediately instead
   of after a live-browser round trip each; do it *before* moving on, not
   after a review flags a suspiciously-quiet test.
5. **Run the full set of tests written this stage, and report the aggregate**
   in §20 alongside the rest -- a test written and never run is a claim, not
   a check.

Pass: every fix from this run's §16/CHANGELOG entries has a test, every test
newly written this run was mutation-verified against the bug it names, and the
aggregate result is reported. `⏭️ SKIP` here is only legitimate when this run
changed no code (documentation, rules.md itself, a pure investigation).

## 17. Deploy, supervise + smoke

**Supervision is part of the deploy, not an optional extra.** Both long-lived
processes — the web application and `claude_proxy.py` — are bare background
processes unless something owns them, and that has failed in two distinct ways
that both present as "the feature does nothing", with no error anywhere:

* **The process dies with whatever shell started it.** A backgrounded
  `uvicorn ... &` is killed when its parent exits, so the site simply
  disappears mid-session.
* **The process keeps serving the code it started with.** Nothing reloads
  `claude_proxy.py`, so it happily runs an hour-old copy while the file on
  disk has moved on. This silently broke the proxy token handshake, the
  `ANTHROPIC_BASE_URL` a turn needs, and usage accounting — three separate
  features, none of which logged a cause. **Before debugging any proxy
  behaviour, compare the process start time against the file mtime**; the
  proxy's startup line carries `source_mtime=` for exactly this.

Install with `bash bin/wc-install-supervision.sh` (idempotent). It requires
linger, or the units stop the moment the last SSH session logs out.

```bash
# Units enabled, active, and surviving logout
systemctl --user is-enabled webconsole.service webconsole-proxy.service webconsole-health.timer
systemctl --user is-active  webconsole.service webconsole-proxy.service webconsole-health.timer
loginctl show-user "$USER" -p Linger      # must be Linger=yes

# The proxy is running current code. Its unit writes stdout to the log file,
# not the journal, so read it there — `journalctl -u webconsole-proxy` shows
# only systemd's own start/stop lines and will look empty.
grep -o 'source_mtime=.*' logs/claude-proxy.log | tail -1
stat -c%y claude_proxy.py        # the two must agree
```

Expect `bad handshake` lines in `logs/claude-proxy.log` while the test suite is
running: several tests open a real connection to the proxy port with a test
token. They are only a symptom when they accompany a *failing turn*.

**Recovery must be demonstrated, not assumed.** Four cases: the third is the
one `Restart=always` cannot see, and the fourth is the one that is easiest to
skip and most dangerous to get wrong.

```bash
# 1. Crash: the supervisor restarts it
kill -9 "$(systemctl --user show webconsole.service -p MainPID --value)"
sleep 8; curl -sk -o /dev/null -w '%{http_code}\n' https://<tailscale-ip>/login   # 200

# 2. Proxy crash: same, and independently
kill -9 "$(systemctl --user show webconsole-proxy.service -p MainPID --value)"
sleep 8; ss -tln | grep 127.0.0.1:9000                                            # listening

# 3. Wedged: alive, holding the port, answering nothing. systemd reports
#    `active` throughout, so only the health check catches it.
PID="$(systemctl --user show webconsole.service -p MainPID --value)"
kill -STOP "$PID"; bash bin/wc-health.sh; sleep 7
curl -sk -o /dev/null -w '%{http_code}\n' https://<tailscale-ip>/login             # 200
kill -9 "$PID" 2>/dev/null

# 4. And the case that matters most: a HEALTHY server is left alone.
BEFORE="$(systemctl --user show webconsole.service -p MainPID --value)"
bash bin/wc-health.sh
[ "$BEFORE" = "$(systemctl --user show webconsole.service -p MainPID --value)" ] \
  && echo "healthy server untouched" || echo "FAIL: restarted a healthy server"
```

Case 4 is not padding. The first version of the health check probed
`127.0.0.1` while the server binds the tailnet address, so every probe was
refused and it would have restarted a healthy server every 30 seconds for
ever — strictly worse than having no health check. A restarter that is never
tested against the healthy case is a liability.

Pass: all four cases behave as above, `Linger=yes`, and the proxy's
`source_mtime` matches the file on disk.
