# Terminal Backend Hot-Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a terminal session started via `bin/wc-claude.sh --resume <name>` follow WebConsole's active-machine setting while it runs, instead of the backend being fixed forever at launch.

**Architecture:** `wc-claude.sh` stops `exec`-ing `claude` directly. It runs `claude` as a supervised child, polls the same read-only DB row it already resolves at startup, and on a real change kills the child (SIGTERM, then SIGKILL after a grace period) and relaunches it with the same `--resume <name>` and fresh environment — same `screen`/`tmux` pty throughout, since `screen` tracks the pty, not the pid. Opt-in only: engages when `WC_CLAUDE_HOTSWAP=1` **and** the invocation already carries `--resume <name>`; every other invocation runs exactly as it does today.

**Tech Stack:** Bash (the script itself), Python 3 one-liners (the existing read-only sqlite queries), `.venv/bin/python -m pytest` for tests (subprocess-driven against the real script, matching the existing `test_qa_proxy_claude_path.py` pattern).

**Spec:** `docs/superpowers/specs/2026-09-02-terminal-backend-hotswap-design.md`

## Global Constraints

- **Read-only, always.** Every DB read (initial resolve and poller) uses the existing `sqlite3.connect(f"file:{DB}?mode=ro", uri=True)` pattern. Never open the database read-write from this script or its tests against the *real* `data/webconsole.db` — a second read-write connection is what broke the production write path for 37 minutes (registry #41). Tests use their own throwaway sqlite file.
- **SIGTERM before SIGKILL, always**, with a grace period. Never signal a live `claude` child with SIGKILL first — it is what would lose the transcript flush `--resume` depends on.
- **Opt-in gate:** the supervised loop only activates when both `WC_CLAUDE_HOTSWAP=1` is set **and** the original invocation contained `--resume <name>`. Every other invocation (no flag, or no `--resume`) must behave byte-for-byte as the current script does today — this is a regression constraint, not a preference.
- **No changes to `runner.py`, `claude_proxy.py`, `config.py`, or anything under `web/`.** This plan touches exactly one file plus its test.
- **Tests run as `.venv/bin/python -m pytest`, invoked bare** (not `pytest tests/` — the latter misses files on this checkout). Any other interpreter silently skips parts of this repo's suite.
- **Flags/env pass through untouched**, per the script's existing contract: `--model` is only injected when the caller did not pass one; an explicit `--model` always wins.

---

## Current script, for reference

The file being modified is `bin/wc-claude.sh` (226 lines today). Its existing structure, which Task 1 preserves behaviourally and restructures into functions:

1. Resolve `HERE`/`DB`. If no DB file, `exec claude "$@"` unchanged (line 30-33).
2. Read-only sqlite query joining `ai_machines` (`WHERE active = 1 LIMIT 1`) and `settings` (`key = 'default_model'`), printed as one tab-separated line: `provider  base_url  api_key  model  name`. Missing fields print as `-` (except `name`, which is space-preserving and defaults to `-` only when empty).
3. If `provider = "-"` (no active machine), `exec claude "$@"` unchanged (line 84-87).
4. Export/unset `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_SIMPLE` to mirror `claude_proxy._backend_env` (line 89-109).
5. Build `MODEL_ARGS=(--model "$MODEL")` unless the caller already passed `--model` (line 111-122).
6. Scan `"$@"` for `--resume <target>`; if found, read that session's last-used model out of `~/.claude/sessions`/`~/.claude/projects`, and if it differs from the resolved `$MODEL`, run `bin/claude-transcript-doctor.py --fix <target>` before continuing (line 124-198).
7. Print the `wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}` banner (line 200).
8. If `WC_CLAUDE_DRY_RUN=1`, report the resolved env and exit 0 without starting anything (line 202-224).
9. `exec claude "${MODEL_ARGS[@]}" "$@"` (line 226).

---

## Task 1: Extract the script into reusable functions, zero behaviour change

This is a pure refactor. After this task, `bin/wc-claude.sh` still always ends by `exec`-ing `claude` — nothing about its observable behaviour changes. The refactor exists so Tasks 2–3 can call the same resolution logic more than once instead of duplicating it.

**Files:**
- Modify: `bin/wc-claude.sh` (whole file rewritten into functions; behaviour identical)
- Test: `tests/test_qa_wc_claude_hotswap.py` (new file)

**Interfaces:**
- Produces (bash functions, all operating on script-global variables, defined in `bin/wc-claude.sh`):
  - `detect_resume_name "$@"` — sets global `RESUME_NAME` to the value following a `--resume` flag in its arguments, or `""` if none is present.
  - `query_backend` — runs the read-only sqlite query against `$DB`, prints one tab-separated line `provider\tbase_url\tapi_key\tmodel\tname` to stdout. Pure: no globals set, no side effects.
  - `resolve_backend` — calls `query_backend`, splits its output into globals `PROVIDER BASE_URL API_KEY MODEL NAME`.
  - `apply_env` — using the current `PROVIDER`/`BASE_URL`/`API_KEY` globals, exports/unsets `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_SIMPLE`.
  - `build_model_args "$@"` — using the current `MODEL` global and its own arguments, sets global array `MODEL_ARGS`.
  - `check_transcript_doctor` — using the current `RESUME_NAME` and `MODEL` globals, runs the existing mismatch-check-and-repair block.

**Step 1: Write the failing regression test**

Create `tests/test_qa_wc_claude_hotswap.py`:

```python
"""QA: wc-claude.sh resolves the active backend correctly, with and without
the hot-swap feature engaged.

Task 1 establishes that refactoring the script into reusable functions changes
nothing observable: the same four DB fixtures must resolve to the same
WC_CLAUDE_DRY_RUN report, byte for byte, before and after the refactor. Tasks 2
and 3 build the poller and the supervised loop on top of these functions.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-claude.sh"


def _make_db(path: str, machines: list[dict], default_model: str = "") -> None:
    """A throwaway DB holding only what wc-claude.sh reads: ai_machines and
    settings. Deliberately not the full app schema -- the script only ever
    touches these two tables and these columns."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ai_machines (name TEXT, provider TEXT, base_url TEXT, "
        "api_key TEXT, model TEXT, active INTEGER)"
    )
    con.execute("CREATE TABLE settings (key TEXT, value TEXT)")
    for m in machines:
        con.execute(
            "INSERT INTO ai_machines (name, provider, base_url, api_key, "
            "model, active) VALUES (?, ?, ?, ?, ?, ?)",
            (m["name"], m["provider"], m.get("base_url"), m.get("api_key"),
             m.get("model"), 1 if m.get("active") else 0),
        )
    if default_model:
        con.execute(
            "INSERT INTO settings (key, value) VALUES ('default_model', ?)",
            (default_model,),
        )
    con.commit()
    con.close()


class DryRunResolutionTests(unittest.TestCase):
    """Four fixtures covering every branch query_backend/resolve_backend take.

    Run via WC_CLAUDE_DRY_RUN=1 against a fake `claude` on PATH, so nothing
    ever actually starts a session or spends a token.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake_claude = Path(self.tmp.name) / "bin"
        fake_claude.mkdir()
        (fake_claude / "claude").write_text("#!/bin/sh\necho FAKE-CLAUDE-RAN\n")
        (fake_claude / "claude").chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{fake_claude}:{os.environ.get('PATH', '')}",
            "WC_CLAUDE_DRY_RUN": "1",
        }

    def _run(self, db_path: str | None, *args: str) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if db_path is not None:
            env["WC_DB_PATH"] = db_path
        else:
            env.pop("WC_DB_PATH", None)
        return subprocess.run(
            [str(SCRIPT), *args],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_no_database_file_starts_claude_unchanged(self):
        missing = str(Path(self.tmp.name) / "does-not-exist.db")
        result = self._run(missing, "--resume", "test1")
        # WC_CLAUDE_DRY_RUN never triggers here: the no-DB branch execs before
        # the dry-run check exists in the script, exactly as it does today.
        self.assertIn("FAKE-CLAUDE-RAN", result.stdout)
        self.assertIn("no WebConsole database", result.stderr)

    def test_no_active_machine_starts_claude_unchanged(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{"name": "idle", "provider": "anthropic",
                                "active": False}])
        result = self._run(db, "--resume", "test1")
        self.assertIn("FAKE-CLAUDE-RAN", result.stdout)
        self.assertIn("no active machine", result.stderr)

    def test_anthropic_machine_with_key_resolves(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Anthropic API", "provider": "anthropic",
            "base_url": "", "api_key": "sk-test-key-value",
            "model": "claude-opus-5", "active": True,
        }])
        result = self._run(db, "--resume", "test1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ANTHROPIC_API_KEY = <set, 17 chars>", result.stdout)
        self.assertNotIn("FAKE-CLAUDE-RAN", result.stdout,
                         "dry run must not start claude")
        self.assertIn("--model claude-opus-5", result.stdout)

    def test_gateway_machine_unsets_anthropic_vars(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Gateway", "provider": "openai",
            "base_url": "https://gateway.example/v1", "api_key": "gw-key",
            "model": "gpt-5.6-luna", "active": True,
        }])
        result = self._run(db, "--resume", "test1")
        self.assertIn("ANTHROPIC_BASE_URL = (unset)", result.stdout)
        self.assertIn("ANTHROPIC_API_KEY = (unset)", result.stdout)

    def test_explicit_model_flag_always_wins(self):
        db = str(Path(self.tmp.name) / "db.sqlite")
        _make_db(db, machines=[{
            "name": "Anthropic API", "provider": "anthropic",
            "api_key": "sk-test", "model": "claude-opus-5", "active": True,
        }])
        result = self._run(db, "--resume", "test1", "--model", "claude-haiku-4-5")
        self.assertIn("would exec: claude  --resume test1 --model claude-haiku-4-5",
                     result.stdout.replace("\n", " "))
        self.assertNotIn("--model claude-opus-5", result.stdout)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run the test to verify it fails (or passes for the wrong reason)**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py -v`

Expected: these tests exercise the **current, unmodified** script, so they
should already pass — this step is the baseline. If any fail against the
current script, stop and fix the test fixture (not the script) before
proceeding: Task 1 must not change behaviour, so a failing test here means the
test is wrong, not the script.

**Step 3: Rewrite `bin/wc-claude.sh` into functions**

Replace the full file with:

```bash
#!/usr/bin/env bash
# wc-claude — start Claude Code on the backend WebConsole is configured to use.
#
# WebConsole's "active machine" and default model govern the turns *it* spawns:
# claude_proxy reads the machine record and puts the endpoint and key into the
# child's environment. A session started by hand from a shell never touches that
# database, so it silently ignores the configuration and falls through to the
# CLI's own default — api.anthropic.com on the host login.
#
# The consequence is not visible anywhere. You set "use the AI machine" in the
# one place the product offers, the web chats obey it, and six terminal sessions
# keep spending Anthropic credit with nothing to say so.
#
# This resolves the same machine the proxy would, exports the same variables the
# proxy would, and execs claude. Use it instead of `claude`:
#
#     bin/wc-claude.sh --dangerously-skip-permissions --resume cweb2
#
# or make it the default for interactive shells:
#
#     alias claude='/home/kali/projects/claude-code-webconsole/bin/wc-claude.sh'
#
# Flags are passed through untouched. --model is added only when you did not
# give one, so an explicit --model always wins.
#
# WC_CLAUDE_HOTSWAP=1, combined with --resume <name>, keeps the session on the
# active machine as it changes: see wc_claude_supervised_loop below.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${WC_DB_PATH:-$HERE/data/webconsole.db}"

# Sets global RESUME_NAME to the value following --resume in "$@", or "".
# Used both for the transcript-doctor mismatch check and to gate hot-swap.
detect_resume_name() {
    RESUME_NAME=""
    local i j
    for i in $(seq 1 $#); do
        if [ "${!i}" = "--resume" ]; then
            j=$((i + 1))
            RESUME_NAME="${!j:-}"
            return
        fi
    done
}

# Read-only, always. Opening this database read-write from a second process is
# what took production's write path down for 37 minutes (registry #41): the
# server's connection could never upgrade its transaction.
#
# Pure: prints one tab-separated line, sets nothing. Called by resolve_backend
# on startup and, from Task 2 onward, by the poller on a timer -- one query
# definition, not two copies to keep in sync.
query_backend() {
    python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
try:
    row = con.execute(
        "SELECT name, provider, base_url, api_key, model FROM ai_machines "
        "WHERE active = 1 LIMIT 1"
    ).fetchone()
    setting = con.execute(
        "SELECT value FROM settings WHERE key = 'default_model'"
    ).fetchone()
finally:
    con.close()

def clean(value):
    # Any whitespace would break the field split, and a newline in a value
    # would let it forge a field. Empty becomes "-" so the positions hold.
    text = "" if value is None else str(value).strip()
    return "".join(text.split()) or "-"


def last_field(value):
    # The name is read last, and `read` puts everything remaining into the final
    # variable -- so spaces are safe there and only newlines are not. Collapsing
    # them like the others printed "CurrentAIMachine" in the banner, which is a
    # small thing to get wrong in the one line that tells you which backend you
    # are about to talk to.
    text = "" if value is None else str(value).strip()
    return " ".join(text.split()) or "-"

if row is None:
    print("- - - - -")
else:
    # The machine's own model first, then the global default -- the same order
    # runner.get_default_model uses once a chat pins nothing.
    model = (row["model"] or "").strip() or (
        (setting["value"] if setting else "") or "").strip()
    fields = [clean(x) for x in (
        row["provider"], row["base_url"], row["api_key"], model)]
    print("\t".join([*fields, last_field(row["name"])]))
PY
}

# Sets globals PROVIDER BASE_URL API_KEY MODEL NAME from query_backend's output.
resolve_backend() {
    read -r PROVIDER BASE_URL API_KEY MODEL NAME <<<"$(query_backend)"
}

# Mirror claude_proxy._backend_env exactly, including what it *removes*. A
# non-anthropic backend must not inherit Anthropic variables from this shell,
# and a machine with no base_url means "the official API" — leaving an inherited
# one there is registry #68, the bug whose workaround was to unset these by hand.
apply_env() {
    unset ANTHROPIC_AUTH_TOKEN
    if [ "$PROVIDER" != "anthropic" ]; then
        unset ANTHROPIC_BASE_URL
    elif [ "$BASE_URL" != "-" ]; then
        export ANTHROPIC_BASE_URL="$BASE_URL"
    else
        unset ANTHROPIC_BASE_URL
    fi

    if [ "$PROVIDER" = "anthropic" ] && [ "$API_KEY" != "-" ]; then
        export ANTHROPIC_API_KEY="$API_KEY"
    else
        # No key: the CLI must fall back to the host's own login, which it will
        # not do while CLAUDE_CODE_SIMPLE is set.
        unset ANTHROPIC_API_KEY
        unset CLAUDE_CODE_SIMPLE
    fi
}

# --model only if the caller did not pass one. Sets global array MODEL_ARGS.
build_model_args() {
    local want_model=1 arg
    for arg in "$@"; do
        case "$arg" in
            --model|--model=*) want_model=0 ;;
        esac
    done
    MODEL_ARGS=()
    if [ "$want_model" = 1 ] && [ "$MODEL" != "-" ]; then
        MODEL_ARGS=(--model "$MODEL")
    fi
}

# Switching a session between providers is what poisons a transcript: a thinking
# block carries a provider-specific signature, and the other provider rejects the
# whole conversation from then on. WebConsole repairs that before every turn; a
# terminal session has nothing doing it. So warn before resuming a session whose
# last turn ran on a different model, and say what to do about it.
#
# Uses the RESUME_NAME global (set by detect_resume_name) rather than
# re-scanning "$@" -- from Task 3 onward this runs again on every hot-swap
# restart, and the resume target never changes across those restarts.
check_transcript_doctor() {
    [ -z "$RESUME_NAME" ] && return
    local last
    last="$(python3 - "$RESUME_NAME" <<'PY' 2>/dev/null || true
import json, pathlib, sys
name = sys.argv[1]
sess = pathlib.Path.home() / ".claude" / "sessions"
proj = pathlib.Path.home() / ".claude" / "projects"
sid = name
for meta in sess.glob("*.json"):
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        continue
    if data.get("name") == name and data.get("sessionId"):
        sid = data["sessionId"]
        break
for path in proj.glob(f"*/{sid}.jsonl"):
    seen = ""
    for line in path.read_text(errors="replace").splitlines():
        if '"model"' not in line:
            continue
        try:
            message = (json.loads(line).get("message") or {})
        except ValueError:
            continue
        if message.get("role") == "assistant" and message.get("model"):
            model = message["model"]
            if model != "<synthetic>":
                seen = model
    print(seen)
    break
PY
)"
    if [ -n "$last" ] && [ "$MODEL" != "-" ] && [ "$last" != "$MODEL" ]; then
        # Repair rather than warn. A warning puts the work on someone who
        # has to remember it every time, and the whole reason this failure
        # cost five sessions is that nothing was watching.
        #
        # This is the one moment the repair is safe to run unattended: the
        # session is closed, because we are the thing about to open it. The
        # doctor's own banner says to close it first, and here that is
        # guaranteed rather than requested.
        #
        # Safe in both directions. Going to a gateway there is nothing to
        # remove -- every block Anthropic wrote is signed -- so it is a
        # no-op. Going back to Anthropic it removes exactly the blocks that
        # would otherwise fail the conversation permanently. The doctor
        # keeps <file>.orig (never overwritten) and a .bak per run, and
        # refuses to install anything that does not parse or that breaks the
        # uuid chain.
        if [ "${WC_CLAUDE_NO_REPAIR:-}" = "1" ]; then
            echo "wc-claude: '$last' -> '$MODEL'; repair skipped " \
                 "(WC_CLAUDE_NO_REPAIR=1)" >&2
        else
            echo "wc-claude: '$last' -> '$MODEL' — checking the transcript" >&2
            if ! "$HERE/bin/claude-transcript-doctor.py" --fix "$RESUME_NAME" >&2; then
                cat >&2 <<'WARN'
wc-claude: the transcript repair did not succeed. Starting anyway, but if this
           conversation was written by a different provider the API may refuse
           it. Run bin/claude-transcript-doctor.py --fix <name> by hand.
WARN
            fi
        fi
    fi
}

detect_resume_name "$@"

if [ ! -f "$DB" ]; then
    echo "wc-claude: no WebConsole database at $DB — starting claude unchanged" >&2
    exec claude "$@"
fi

resolve_backend

if [ "$PROVIDER" = "-" ]; then
    echo "wc-claude: no active machine in WebConsole — starting claude unchanged" >&2
    exec claude "$@"
fi

apply_env
build_model_args "$@"
check_transcript_doctor

echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2

# WC_CLAUDE_DRY_RUN=1 reports what would happen and exits, so the resolution can
# be checked without starting a session or burning a turn. Secrets are reported
# as set/unset and by length, never printed.
if [ "${WC_CLAUDE_DRY_RUN:-}" = "1" ]; then
    echo "would exec: claude ${MODEL_ARGS[*]} $*"
    for v in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN \
             CLAUDE_CODE_SIMPLE; do
        val="${!v-}"
        if [ -n "$val" ]; then
            case "$v" in
                *KEY*|*TOKEN*) echo "  $v = <set, ${#val} chars>" ;;
                *) echo "  $v = $val" ;;
            esac
        else
            echo "  $v = (unset)"
        fi
    done
    exit 0
fi

exec claude "${MODEL_ARGS[@]}" "$@"
```

**Step 4: Run the test to verify it still passes**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py -v`
Expected: PASS, all 5 tests — identical results to Step 2's baseline, proving
the refactor changed nothing observable.

**Step 5: Commit**

```bash
git add bin/wc-claude.sh tests/test_qa_wc_claude_hotswap.py
git commit -m "refactor: split wc-claude.sh into reusable functions, no behaviour change"
```

---

## Task 2: The poller — detect an active-machine change and signal, in isolation

The poller is tested standalone here, against a dummy target process, with no
`claude` and no supervised loop involved. This proves the detection and
signalling mechanism works before Task 3 wires it into a real restart.

**Files:**
- Modify: `bin/wc-claude.sh` (append poller functions; not yet called from the
  main body)
- Test: `tests/test_qa_wc_claude_hotswap.py` (add a test class)

**Interfaces:**
- Consumes: `query_backend` (from Task 1).
- Produces:
  - `write_backend_state FILE` — writes the current `PROVIDER BASE_URL API_KEY MODEL` globals (four fields, tab-separated; `NAME` is cosmetic and deliberately excluded from the comparison) to `FILE`, replacing it atomically (`printf ... > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"`).
  - `start_poller FILE TARGET_PID` — forks a background subshell that, every `${WC_CLAUDE_POLL_S:-5}` seconds, re-runs `query_backend`, compares its first four fields against the contents of `FILE`, and sends `SIGUSR1` to `TARGET_PID` if they differ **and** the freshly-read provider is not `-`. Sets and returns the poller's pid via global `POLLER_PID`.
  - `stop_poller` — kills `$POLLER_PID` if set, ignoring errors (the poller may already be gone).

**Why a state file and not a bash variable:** `start_poller`'s loop runs in a
forked subshell. A subshell gets its own copy of the parent's variables at
fork time; if the main loop later updates its own in-memory "last known"
variables after a restart, the poller's subshell never sees the update — it
would keep comparing against the value from the moment it was forked, forever.
A file has one writer (the main script, via `write_backend_state`) and one
reader (the poller, on each tick), so there is no synchronisation problem
smaller than an actual file.

**Step 1: Write the failing test**

Add to `tests/test_qa_wc_claude_hotswap.py`:

```python
class PollerTests(unittest.TestCase):
    """The poller in isolation: no claude, no loop, just detect-and-signal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "db.sqlite")
        self.state = str(Path(self.tmp.name) / "state")

    def _script(self, body: str) -> str:
        # Sources the functions from the real script (everything up to, but
        # not including, its main body) so the poller under test is the real
        # implementation, not a re-typed copy.
        functions = SCRIPT.read_text(encoding="utf-8").split(
            'detect_resume_name "$@"', 1)[0]
        return (
            f'export WC_DB_PATH="{self.db}"\n'
            f'DB="{self.db}"\n'
            f'{functions}\n{body}\n'
        )

    def _run_bash(self, body: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", self._script(body)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def test_a_real_change_signals_the_target(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        # write_backend_state needs PROVIDER/BASE_URL/API_KEY/MODEL set, as
        # resolve_backend would set them at startup.
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 2
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("{self.db}")
con.execute("UPDATE ai_machines SET api_key = 'key-two' WHERE name = 'one'")
con.commit()
con.close()
PY
sleep 3
stop_poller
'''
        self._run_bash(body)
        self.assertTrue(Path(f"{self.state}.hit").exists(),
                        "poller did not signal after a real backend change")

    def test_no_change_does_not_signal(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 4
stop_poller
'''
        self._run_bash(body)
        self.assertFalse(Path(f"{self.state}.hit").exists(),
                         "poller signalled with no actual backend change")

    def test_no_active_machine_does_not_signal(self):
        """A machine being deactivated with nothing else active must not
        trigger a restart into a broken state."""
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
trap 'echo SIGNALLED > "{self.state}.hit"' USR1
start_poller "{self.state}" "$$"
sleep 2
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("{self.db}")
con.execute("UPDATE ai_machines SET active = 0")
con.commit()
con.close()
PY
sleep 3
stop_poller
'''
        self._run_bash(body)
        self.assertFalse(Path(f"{self.state}.hit").exists(),
                         "poller signalled a transition to no active machine")

    def test_stop_poller_leaves_no_process_behind(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        body = f'''
export WC_CLAUDE_POLL_S=1
resolve_backend
write_backend_state "{self.state}"
start_poller "{self.state}" "$$"
echo "POLLER_PID=$POLLER_PID"
stop_poller
sleep 1
if kill -0 "$POLLER_PID" 2>/dev/null; then
    echo STILL_ALIVE
else
    echo GONE
fi
'''
        result = self._run_bash(body)
        self.assertIn("GONE", result.stdout, result.stdout)
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py::PollerTests -v`
Expected: FAIL — `write_backend_state`/`start_poller`/`stop_poller` do not
exist yet (bash reports `command not found`).

**Step 3: Append the poller functions to `bin/wc-claude.sh`**

Insert these functions after `check_transcript_doctor` and before
`detect_resume_name "$@"` (the start of the main body):

```bash
# Writes PROVIDER BASE_URL API_KEY MODEL (four fields; NAME is cosmetic and
# excluded) to FILE, atomically. This is the single source the poller compares
# against, written by the main script whenever it accepts a new resolution.
write_backend_state() {
    local file="$1"
    printf '%s\t%s\t%s\t%s\n' "$PROVIDER" "$BASE_URL" "$API_KEY" "$MODEL" \
        > "${file}.tmp"
    mv "${file}.tmp" "$file"
}

# Forks a background poller comparing query_backend's output against FILE
# every WC_CLAUDE_POLL_S seconds (default 5), signalling TARGET_PID with
# SIGUSR1 on a real, non-empty change. Sets global POLLER_PID.
start_poller() {
    local file="$1" target="$2"
    (
        while sleep "${WC_CLAUDE_POLL_S:-5}"; do
            current="$(query_backend | cut -f1-4)"
            last="$(cat "$file" 2>/dev/null || true)"
            provider="$(printf '%s' "$current" | cut -f1)"
            if [ "$provider" != "-" ] && [ "$current" != "$last" ]; then
                kill -USR1 "$target" 2>/dev/null || true
            fi
        done
    ) &
    POLLER_PID=$!
}

stop_poller() {
    [ -n "${POLLER_PID:-}" ] && kill "$POLLER_PID" 2>/dev/null || true
}
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py::PollerTests -v`
Expected: PASS, all 4 tests.

**Step 5: Run the full file to confirm no regression**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py -v`
Expected: PASS, all 9 tests (5 from Task 1 + 4 from Task 2).

**Step 6: Commit**

```bash
git add bin/wc-claude.sh tests/test_qa_wc_claude_hotswap.py
git commit -m "feat: add a standalone DB-change poller to wc-claude.sh"
```

---

## Task 3: Wire the hot-swap gate and the supervised loop

This is the feature. `WC_CLAUDE_HOTSWAP=1` plus `--resume <name>` now keeps a
running terminal session on the active machine as it changes, without losing
the conversation or disturbing the `screen`/`tmux` window it runs in.

**Files:**
- Modify: `bin/wc-claude.sh` (replace the final `exec claude` with the gate + loop)
- Test: `tests/test_qa_wc_claude_hotswap.py` (add a test class)

**Interfaces:**
- Consumes: `RESUME_NAME`, `PROVIDER`/`BASE_URL`/`API_KEY`/`MODEL`/`NAME`,
  `resolve_backend`, `apply_env`, `build_model_args`, `check_transcript_doctor`,
  `write_backend_state`, `start_poller`, `stop_poller` (all from Tasks 1–2).
- Produces: no new functions — this task replaces the script's tail end
  (previously just `exec claude ...`) with the gated loop, which is the
  script's final behaviour for a hot-swap-eligible invocation.

**Step 1: Write the failing test**

Add to `tests/test_qa_wc_claude_hotswap.py`:

```python
FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Fake claude for hot-swap integration tests.

Logs one line per invocation (its env + argv) to LOG, then sleeps until
SIGTERM, at which point it exits 0 -- mimicking a real claude session
shutting down cleanly on the signal the loop sends it.
"""
import json, os, signal, sys, time

LOG = os.environ["FAKE_CLAUDE_LOG"]

def _term(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, _term)

with open(LOG, "a") as f:
    f.write(json.dumps({
        "argv": sys.argv[1:],
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    }) + "\\n")

time.sleep(60)
'''


class HotswapLoopTests(unittest.TestCase):
    """End to end: real script, fake claude, real throwaway DB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        claude = bindir / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.db = str(Path(self.tmp.name) / "db.sqlite")
        self.log = str(Path(self.tmp.name) / "claude.log")
        Path(self.log).touch()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "WC_DB_PATH": self.db,
            "FAKE_CLAUDE_LOG": self.log,
            "WC_CLAUDE_HOTSWAP": "1",
            "WC_CLAUDE_POLL_S": "1",
            "WC_CLAUDE_NO_REPAIR": "1",
        }

    def _invocations(self) -> list[dict]:
        import json
        lines = [ln for ln in Path(self.log).read_text().splitlines() if ln]
        return [json.loads(ln) for ln in lines]

    def _wait_for(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_a_backend_change_restarts_with_the_same_resume_name(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1),
                        "first claude invocation never happened")

        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, active) "
            "VALUES ('two', 'anthropic', 'key-two', 'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()

        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 2),
                        "no second invocation after the active machine changed")
        first, second = self._invocations()[:2]
        self.assertEqual(first["argv"], ["--resume", "test-session"])
        self.assertEqual(second["argv"], ["--resume", "test-session"],
                         "resume target must be identical across a hot-swap")
        self.assertEqual(first["ANTHROPIC_API_KEY"], "key-one")
        self.assertEqual(second["ANTHROPIC_API_KEY"], "key-two")

    def test_no_change_means_no_restart(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        time.sleep(4)  # several poll ticks at WC_CLAUDE_POLL_S=1
        self.assertEqual(len(self._invocations()), 1,
                         "an unrelated poll tick caused a restart")

    def test_without_the_flag_behaves_like_a_single_exec(self):
        env = dict(self.env)
        env.pop("WC_CLAUDE_HOTSWAP")
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, active) "
            "VALUES ('two', 'anthropic', 'key-two', 'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()
        time.sleep(3)
        self.assertEqual(len(self._invocations()), 1,
                         "a change restarted the session with WC_CLAUDE_HOTSWAP unset")

    def test_without_resume_behaves_like_a_single_exec(self):
        env = dict(self.env)
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT)], env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO ai_machines (name, provider, api_key, model, active) "
            "VALUES ('two', 'anthropic', 'key-two', 'claude-sonnet-5', 1)")
        con.execute("UPDATE ai_machines SET active = 0 WHERE name = 'one'")
        con.commit()
        con.close()
        time.sleep(3)
        self.assertEqual(len(self._invocations()), 1,
                         "a change restarted a session with no --resume name")

    def test_the_child_exiting_on_its_own_ends_the_wrapper(self):
        """The everyday case: the user runs /exit or Ctrl-D. The wrapper must
        not treat that as something to restart from."""
        exiting_claude = '''#!/usr/bin/env python3
import sys
sys.exit(0)
'''
        bindir = Path(self.tmp.name) / "bin"
        (bindir / "claude").write_text(exiting_claude)
        (bindir / "claude").chmod(0o755)
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        result = subprocess.run(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0)

    def test_the_poller_and_child_are_cleaned_up_on_exit(self):
        _make_db(self.db, machines=[{
            "name": "one", "provider": "anthropic", "api_key": "key-one",
            "model": "claude-opus-5", "active": True,
        }])
        proc = subprocess.Popen(
            [str(SCRIPT), "--resume", "test-session"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.assertTrue(self._wait_for(lambda: len(self._invocations()) >= 1))
        # Find the fake-claude child before killing the wrapper.
        children = subprocess.run(
            ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True,
        ).stdout.split()
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(1)
        for pid in children:
            with self.assertRaises(subprocess.CalledProcessError,
                                   msg=f"child {pid} survived the wrapper"):
                subprocess.run(["kill", "-0", pid], check=True,
                              capture_output=True)
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py::HotswapLoopTests -v`
Expected: FAIL — today's script always `exec`s, so there is only ever one
invocation and no restart, and the wrapper's own pid *is* the child's pid (an
`exec` replaces the process, it does not fork), so the cleanup test also
fails.

**Step 3: Replace the tail of `bin/wc-claude.sh`**

Replace the final two lines (the `WC_CLAUDE_DRY_RUN` block's `exit 0` stays
exactly where it is; only what comes **after** it changes) —

```bash
exec claude "${MODEL_ARGS[@]}" "$@"
```

— with:

```bash
HOTSWAP=0
if [ "${WC_CLAUDE_HOTSWAP:-}" = "1" ] && [ -n "$RESUME_NAME" ]; then
    HOTSWAP=1
fi

if [ "$HOTSWAP" != "1" ]; then
    exec claude "${MODEL_ARGS[@]}" "$@"
fi

# ---- Supervised loop: claude runs as a child, this process stays resident ----
#
# Never exec here. screen/tmux track the pty, not the pid inside it; whatever
# is exec'd becomes the pty's only foreground process, and killing it to swap
# backends would leave nothing in the window. Running claude as a child keeps
# this script itself resident in the pty for the whole session.
STATE_FILE="$(mktemp)"
cleanup() {
    stop_poller
    rm -f "$STATE_FILE"
}
trap cleanup EXIT

write_backend_state "$STATE_FILE"

RESTART=0
trap 'RESTART=1' USR1
start_poller "$STATE_FILE" "$$"

while true; do
    claude "${MODEL_ARGS[@]}" "$@" &
    CHILD=$!

    while true; do
        RESTART=0
        wait "$CHILD"
        STATUS=$?
        if kill -0 "$CHILD" 2>/dev/null; then
            # The child is still alive: wait was interrupted by our own
            # trapped SIGUSR1, not by the child exiting.
            if [ "$RESTART" = "1" ]; then
                resolve_backend
                NEW_STATE="$(query_backend | cut -f1-4)"
                OLD_STATE="$(cat "$STATE_FILE")"
                if [ "$NEW_STATE" = "$OLD_STATE" ]; then
                    # False alarm (e.g. two rapid ticks collapsed into one
                    # signal) -- the child is fine, keep waiting on it.
                    continue
                fi
                break
            fi
            continue
        else
            # The child exited on its own (user ran /exit, Ctrl-D, or it
            # crashed). That is not ours to override.
            exit "$STATUS"
        fi
    done

    # A real change was confirmed above: bring the child down cleanly and
    # relaunch it with the same "$@" -- --resume <name> is already in there
    # and does not change across a hot-swap, only the environment does.
    kill -TERM "$CHILD" 2>/dev/null || true
    for _ in 1 2 3 4; do
        kill -0 "$CHILD" 2>/dev/null || break
        sleep 0.5
    done
    kill -0 "$CHILD" 2>/dev/null && kill -KILL "$CHILD" 2>/dev/null || true
    wait "$CHILD" 2>/dev/null || true

    apply_env
    build_model_args "$@"
    check_transcript_doctor
    echo "wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}" >&2
    write_backend_state "$STATE_FILE"
done
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py::HotswapLoopTests -v`
Expected: PASS, all 6 tests.

**Step 5: Run the full test file**

Run: `.venv/bin/python -m pytest tests/test_qa_wc_claude_hotswap.py -v`
Expected: PASS, all 19 tests (5 + 4 + 6, plus the pre-existing 4 not
re-enumerated above — count against your own file as written).

**Step 6: Run the wider suite to confirm nothing else broke**

Run: `.venv/bin/python -m pytest`
Expected: same pass/skip counts as before this plan, plus the new file's
tests. This script is not imported by any other module and touches no shared
state, so no other test should be affected — a change here is the signal to
stop and investigate, not to proceed.

**Step 7: Manual acceptance check (not automated — this is the reported bug)**

Inside a real `screen` session:

```bash
WC_CLAUDE_HOTSWAP=1 bin/wc-claude.sh --dangerously-skip-permissions --resume <existing-test-session>
```

While it runs, flip the active machine in WebConsole's Settings page. Confirm,
in the same window: the `wc-claude:` banner reprints with the new
name/model/base_url within `WC_CLAUDE_POLL_S` seconds, and the conversation
continues under `--resume` rather than starting fresh.

**Step 8: Commit**

```bash
git add bin/wc-claude.sh tests/test_qa_wc_claude_hotswap.py
git commit -m "feat: wc-claude.sh follows the active machine under WC_CLAUDE_HOTSWAP=1"
```

---

## Self-Review

**Spec coverage** — checked against `docs/superpowers/specs/2026-09-02-terminal-backend-hotswap-design.md`:

| Spec section | Covered by |
|---|---|
| Loop shape (child, not exec; restart on flag) | Task 3, Step 3 |
| Detection: read-only poller, no new query pattern | Task 2 (`query_backend` reused, not duplicated) |
| Delivery: trapped signal interrupts `wait` | Task 3, Step 3 (`trap 'RESTART=1' USR1`) |
| Kill discipline: SIGTERM, grace period, then SIGKILL | Task 3, Step 3 |
| Resume identity: only engages with `--resume <name>` | Task 3's `HOTSWAP` gate; `test_without_resume_behaves_like_a_single_exec` |
| Banner reprints visibly on restart | Task 3, Step 3 (`echo "wc-claude: ..."` inside the loop) |
| Non-goal: unresumed sessions unaffected | `test_without_resume_behaves_like_a_single_exec` |
| Non-goal: mid-tool-call interruption not solved | Not testable as a unit; stated as a known limitation in the spec, no code claims otherwise |
| Rollout: opt-in via `WC_CLAUDE_HOTSWAP=1` | Task 3's `HOTSWAP` gate; `test_without_the_flag_behaves_like_a_single_exec` |
| Poll interval `WC_CLAUDE_POLL_S`, default 5s | Task 2's `start_poller` |
| Screen/tmux window preserved (no exec at the top level) | Task 3, Step 3 comment + design; the child, not the wrapper, is what restarts |

**Placeholder scan** — no "TBD", no "add error handling" hand-waves, no
"similar to Task N" references; every step shows complete code. The one
narrative-only item (Step 7 of Task 3) is explicitly marked manual/not
automated, which is honest rather than a placeholder for a test that could not
actually be written.

**Type/name consistency** — function names introduced in Task 1
(`detect_resume_name`, `query_backend`, `resolve_backend`, `apply_env`,
`build_model_args`, `check_transcript_doctor`) are the exact names called in
Tasks 2 and 3. `write_backend_state`, `start_poller`, `stop_poller`
(Task 2) are the exact names called in Task 3's loop. `POLLER_PID` (Task 2)
matches its use in `cleanup()` via `stop_poller` (Task 3) — no direct
reference to `POLLER_PID` outside `stop_poller` itself, so no drift is
possible there.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-03-terminal-backend-hotswap.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
