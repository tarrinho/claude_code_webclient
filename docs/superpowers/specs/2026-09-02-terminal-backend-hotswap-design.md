# Terminal backend hot-swap — switch the active AI machine under a running session

Date: 2026-09-02 · Target: `bin/wc-claude.sh`

## The problem

Pedro hit Anthropic's rate limit mid-conversation, in a terminal session
started via `bin/wc-claude.sh --resume cweb2`. There was no way to move that
session to another backend without losing it: `wc-claude.sh` resolves the
active machine once, at line 226 does `exec claude ...`, and from that instant
the process's environment (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`) is fixed
for the process's entire life. Flipping the active machine in WebConsole's
Settings only changes what the *next* web turn gets — a live terminal session
never re-reads it.

The web side does not have this problem, because it does not have long-lived
processes: `runner.stream_turn` resolves the backend fresh on every turn
(`get_backend` → `_build_env`), so switching the active machine takes effect on
the next message. The terminal case is the one place a `claude` process
persists across many turns with its backend nailed down at birth. That
asymmetry, not a bug in either path, is what this fixes.

## Constraint carried over from CLAUDE.md §0

The console never talks to a model API directly; it spawns `claude` and varies
its parameters and environment. This design does not add a second transport,
a hot-reload feature inside the CLI, or a new IPC surface between WebConsole
and a terminal. It stays inside the existing vocabulary: kill the process,
`--resume <uuid>` a new one with different environment. That is the same
mechanism the CLI already offers for "continue this conversation," used here
to also mean "on a different backend."

Checked directly against the installed CLI (2.1.258): there is no runtime
config-reload command. `claude auth` only manages login/logout for whichever
provider the process is already wired to. `--fallback-model` retries a model
list on the *same* provider and only works under `--print`. `ANTHROPIC_BASE_URL`
/ `ANTHROPIC_API_KEY` are read once from the process environment at startup and
never polled. Nothing short of restarting the process changes them.

## The finding that shapes the design: screen does not care

`wc-claude.sh` is invoked from an interactive shell inside `screen`
(`alias claude=.../wc-claude.sh`), and its last line is `exec claude ...`. Exec
replaces the shell's process image, so the pty's foreground process becomes
`claude` itself — nothing survives underneath it. Kill that pid and the window
has nothing left running.

`screen` tracks the **pty**, not the pid inside it. It has no notion of
"claude" as a specific process to reattach to; `screen -ls` / `screen -r
<id>` identify the window, and that identity never has to change. So the fix
does not need to discover which screen window maps to which terminal, does not
need any registry of that mapping, and does not change how Pedro attaches to a
session. It only needs `wc-claude.sh` to stop exec'ing away the one process
that has to stay resident in that pty across a switch.

## Approaches considered

| | Mechanism | Verdict |
|---|---|---|
| **A — self-supervising wrapper (chosen)** | `wc-claude.sh` becomes a loop: run `claude` as a child, not via `exec`; a background poller inside the same script watches the DB and signals the loop to restart the child with new env, `--resume`ing the same session. | Smallest change, reuses the `--resume` contract CLAUDE.md already documents, no new service, no new IPC surface. |
| B — external signal (push instead of poll) | Same restart mechanic, but WebConsole pushes a signal/socket message to the terminal's process instead of the terminal polling the DB. | Removes poll latency, but adds an IPC surface (signal delivery across users/sessions, or a socket) for a problem a 5s poll already solves acceptably. Not worth it unless "switch now" needs to be instant. |
| C — surface-agnostic resume parity | Make "which backend serves this session" a first-class, push-driven property in the DB/runner layer that both web and terminal subscribe to symmetrically. | Correct long-term shape, but rewrites `runner`'s resume contract and touches both paths CLAUDE.md §1 says must change together. Far more than this problem needs. |

A is the recommendation: it fixes exactly the reported failure, touches one
file, and does not open runner/web parity work that was not asked for.

## Design

### Loop shape

Everything `wc-claude.sh` already does — DB resolution, env export, model-arg
building, the transcript-doctor mismatch check, dry-run reporting — stays as
is, wrapped in a function (`resolve_and_launch`, or equivalent) that can be
called more than once. The final line changes from

```bash
exec claude "${MODEL_ARGS[@]}" "$@"
```

to a supervised child:

```bash
claude "${MODEL_ARGS[@]}" "$@" &
CHILD=$!
wait "$CHILD"
STATUS=$?
```

After `wait` returns: if the restart flag (below) is set, re-run
`resolve_and_launch` with `--resume <session>` substituted for the original
resume argument, and loop. If it is not set, `exit "$STATUS"` — identical to
today's behaviour for a normal `/exit` or a crash.

### Detection: a read-only poller, no new pattern

A background subshell, started once at the top of the script and killed via
`trap ... EXIT`:

```bash
( while sleep "${WC_CLAUDE_POLL_S:-5}"; do
      <same read-only sqlite query already in the script>
      # compare (provider, base_url, model) against what was last resolved;
      # signal only on an actual change
  done ) &
POLLER=$!
```

This is the exact read-only query pattern the script already uses (registry
#41: read-only, always — a second read-write connection to this database is
what broke the production write path for 37 minutes). No new query shape, no
new risk to the database.

### Delivery: a trapped signal interrupts `wait`

```bash
trap 'RESTART=1' USR1
```

Bash returns early from `wait` when a trapped signal arrives — this is
standard behaviour, not a busy-poll. The poller sends `kill -USR1 $$` (its
parent's pid) when it detects a change; the main loop's `wait "$CHILD"` returns,
the trap has already set `RESTART=1`, and the loop proceeds to restart.

### Kill discipline

SIGTERM first, so `claude` can flush its own session state; a short grace
period (~2s); SIGKILL only if it is still alive after that. Never SIGKILL
first — it is what would lose the transcript write the whole point of
`--resume` depends on.

### Resume identity — the one hard requirement

To restart under the *same* conversation, the wrapper needs a real session
identity to pass to `--resume`. Every terminal in the current fleet already
launches with `--resume <name>` (`cweb1` … `cweb8`), and the script already
resolves a name to a session id for the transcript-doctor check. This feature
**only engages when the original invocation carried `--resume <name>`.** A
freshly started, unresumed `claude` session (no `--resume` flag) runs exactly
as it does today: no poller, no restart-on-switch. Sniffing the session id the
CLI assigns itself mid-flight, for a session nobody named, is fragile and
matches no real usage on this host — every long-lived terminal here is already
named.

### What Pedro sees

Nothing about attaching changes. The existing banner —

```
wc-claude: ${NAME} · ${MODEL} · ${BASE_URL}
```

— reprints when the child restarts, in the same screen window, so the switch
is visible as a reconnect with a new banner line rather than as a silent
change or a dropped session.

## Non-goals / stated limitations

- **Mid-tool-call interruption is not solved.** If the switch lands while
  `claude` is running a long tool call, SIGTERM interrupts it — the same class
  of loss as the user hitting Ctrl-C today. This design does not add a
  "wait for a safe point" mechanism.
- **Cross-provider transcript poisoning is not newly introduced or newly
  fixed.** The existing transcript-doctor check already runs whenever
  `--resume` is used with a model mismatch (lines 129–198 of the current
  script); a hot-swap restart goes through the same `resolve_and_launch` path
  and gets the same check for free, but this design adds nothing beyond it.
- **Unresumed sessions are out of scope**, as stated above.
- **This does not touch `runner.py`, `claude_proxy.py`, or the web path.** The
  asymmetry between web (resolved per turn) and terminal (resolved per
  process) is closed only on the terminal side, because that is the side with
  the actual defect.

## Testing strategy

- The state machine (poll → detect change → signal → wait interrupted →
  restart with `--resume`) is testable without spending a real turn: replace
  `claude` with a fake binary (a `sleep` loop that traps SIGTERM and exits 0)
  and drive the DB row directly. Assert: the child restarts, with the new
  `MODEL_ARGS`/env, `--resume <same name>` preserved, and no restart happens
  when the DB row does not change.
- `WC_CLAUDE_DRY_RUN=1` already exists for reporting resolved env without
  starting a session; it should keep working unchanged and continue to report
  what a single resolution would produce, since the dry-run path never enters
  the loop.
- A manual smoke test against a real `screen` window is the acceptance check
  for the actual reported problem: start `wc-claude.sh --resume <test>` under
  screen, flip the active machine in WebConsole Settings, confirm the same
  window reprints the banner with the new backend and the conversation
  continues.

## Open questions for the implementation plan

- Default poll interval (`WC_CLAUDE_POLL_S`, proposed default 5s) — tunable,
  not load-bearing to the design.
- Whether this ships as the default behaviour of `wc-claude.sh` immediately, or
  behind an opt-in flag for the first release before it is trusted across all
  eight terminal sessions.
