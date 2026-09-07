# Resource Guard — design

**Status:** approved in chat 2026-09-08, not yet implemented.

**Goal:** stop this host from being driven into swap and the OOM killer by more
concurrent agents than it has memory for, by refusing to *start* new work when
there is no room for it, and by making the existing load visible before anyone
is refused.

---

## The problem, measured

Taken on 2026-09-08 while the box was in its ordinary working state:

```
MemTotal      3816 MB
MemAvailable   635 MB
MemFree        213 MB
SwapTotal     3151 MB
SwapFree      1681 MB     → 1470 MB already swapped out
```

Where it goes:

| Holder | Count | RSS |
|---|---|---|
| interactive `claude` sessions | 7 | 1884 MB |
| webconsole server | 1 | 633 MB |
| console-spawned turns (`-p --output-format stream-json`) | 1 | 3 MB |

Mean interactive agent: **310 MB**.

Two conclusions drive the whole design.

**The box is already degraded, not at risk of degrading.** 1.5 GB in swap is the
"everything stops" symptom, present right now.

**The existing limits do not apply to the thing consuming the memory.**
`config.MAX_CONCURRENT` (3) and `claude_proxy._MAX_CONCURRENT` (4) gate turns the
console spawns. Those are 3 MB of the 2520 MB in use. The load is long-lived
interactive sessions, which nothing limits.

This has already cost real outages. On 2026-09-07 the webconsole was
SIGKILLed at a 720 MB peak (`status=9/KILL`); a full-suite `pytest` is
OOM-killed reliably enough that `bin/run-suite-chunked.sh` exists solely to
work around it, and says so in its header.

## Scope

**In:** refuse new work when memory is short; report what is holding memory.

**Out, decided explicitly:**

- **No killing.** The guard never terminates anything. On a shared box one
  session's heuristic must not be able to destroy another session's work, and
  the failure mode of a wrong kill is worse than a slow box.
- **No CPU rule.** Load average is noisy on four cores running compilers and
  browsers, and CPU contention makes things slow rather than dead. Memory is
  what kills the service. Revisit only if CPU alone is observed taking the box
  down.
- **No daemon, no polling loop, no new table, no config UI.** The check runs
  when work starts, reads two files, exits.
- **No coverage of processes that do not ask.** A bare `pytest` typed by hand
  is not covered. Closing that needs a systemd slice with a memory cap, which
  is a separate design.

## The rule

A projection, not a level — *is there room after the thing I am about to
start?*

```
MemAvailable - projected_cost < floor   →  refuse
```

`MemAvailable`, not `MemFree` and not "used". `MemFree` reads 213 MB here and
would refuse constantly; "used" counts reclaimable page cache and would refuse
never. `MemAvailable` is the kernel's own estimate of what a new process can
have without swapping, and `sysstats._read_meminfo()` already parses it.

| Knob | Value | Where it comes from |
|---|---|---|
| `projected_cost` | 320 MB default | measured mean agent RSS 310 MB, rounded up |
| `floor` | 400 MB | headroom for the OS, the console and the proxy |

Against the numbers above: `635 - 320 = 315 < 400` → an eighth agent is
refused. Correct for today.

**Second, independent signal: swap.** `SwapFree / SwapTotal` below `0.40` means
the box is thrashing and adding load makes it worse, even when `MemAvailable`
looks adequate. Today that ratio is `1681 / 3151 = 0.53`, so this does not fire
yet; it is the slower-moving guard.

Both thresholds are starting values calibrated on one machine on one day, and
are expected to need tuning within a week of real use, so each is an
environment variable and tuning needs no code change:
`WC_RESOURCE_COST_MB` (default 320), `WC_RESOURCE_FLOOR_MB` (default 400),
`WC_RESOURCE_SWAP_MIN_RATIO` (default 0.40).

## Module

`resource_guard.py` at the repository root. Pure and importable without the
app — no database, no `config.validate()`, no event loop — because two of its
three callers are shell scripts that cannot afford to boot FastAPI to ask one
question.

```python
def check(cost_mb: int = 320, *, floor_mb: int = 400) -> Verdict
def report() -> Load
```

`Verdict` carries `ok: bool`, `reason: str`, and the numbers the decision was
made on, so every caller prints the same sentence rather than inventing its own
phrasing.

`Load` carries the breakdown: count and total RSS of `claude` processes, split
interactive versus console-spawned by whether the command line contains
`--output-format stream-json`. That split is what showed console turns were 3 MB
of the problem and everything else was interactive sessions; it is the single
most useful line in a refusal.

A CLI entry point, `python3 -m resource_guard`, so shell callers invoke one
thing and read one exit code instead of each embedding its own
`awk /proc/meminfo`. Exit `0` to allow, `1` to refuse; the human-readable
reason goes to stderr.

**It does not know what an agent is.** It answers one question — is there room
for N more megabytes — which is why three unrelated callers can share it
without agreeing on anything else.

## Callers

In order of how much they matter:

1. **`bin/wc-claude.sh`** — the dominant consumer, since interactive agents are
   1884 MB of the 2520 MB in use. Checks before `exec`, exits non-zero with the
   reason. This is the caller that stops an eighth agent.
2. **`bin/run-suite-chunked.sh`** — declares a larger `cost_mb` (a chunk running
   chromium is heavier than an agent) and refuses to start the run. This is the
   caller that would have stopped the 2026-09-07 incident, which was a test run
   rather than an agent.
3. **The console** — inside `runner`'s existing semaphore path, so a queued turn
   reports "waiting for memory" the way it already reports "waiting for a slot"
   (`routes/chats.py:996` has the vocabulary already).

## Refusal is also the report

```
wc-claude: refused — 315 MB would remain after a 320 MB agent, floor is 400 MB.
           7 interactive agents are holding 1884 MB; the console holds 633 MB.
           Close an agent, or override with WC_RESOURCE_GUARD=off.
```

A bare "low memory" sends the reader hunting. The breakdown is a decision they
can act on immediately.

The same breakdown is added to the console's Server panel, which already charts
memory from `system_samples` but cannot say *who*. Making the pressure visible
before anyone is refused is the actual goal; a refusal is a failure that has
merely been handled well.

## The override

`WC_RESOURCE_GUARD=off` allows the work, and logs at WARNING every time it is
used.

Refusal is hard in the sense that matters: no prompt, no `y/N`, no soft warning
that can be dismissed by pressing enter. The default is refusal and going around
it takes a deliberate, visible act.

It is not unbypassable, and that is deliberate:

- **The lockout scenario bites at the worst moment.** The box is wedged, you
  need an agent to diagnose it, and the guard refuses to start one *because* the
  box is wedged. No-bypass converts "slow box" into "no tools".
- **The thresholds are one day old.** The first time they are wrong they will be
  wrong in production, and the cost of that should be an annoyed override rather
  than a rebuild.
- **This repository already made this call.** `WC_SKIP_MODEL_CHECK=1` exists for
  the model-list check, and CLAUDE.md gives the reason in a line: *"a check that
  cries wolf gets switched off."* The real choice is between a switch that was
  designed and logged, and someone commenting out the call at 2am. The first is
  greppable.

## Failure behaviour: fail open

If `/proc/meminfo` is unreadable, parsing raises, or the process scan fails,
`check()` returns `ok=True` with a reason naming the failure, and logs it.

A guard that blocks work because it could not measure converts a monitoring bug
into an outage, and does so at every caller simultaneously. `tunnel_manager`'s
boot-grace helper already makes the same choice for the same reason, recorded in
its own comment: *a restarter that acts on missing information is worse than one
that waits.*

## Testing

`check()` takes its inputs, so the tests are a table rather than a mock of the
operating system:

| Case | Expected |
|---|---|
| today's real numbers (635 available, 320 cost, 400 floor) | refuse |
| healthy box (2000 available) | allow |
| exactly at the boundary | pinned explicitly, so a later `<` vs `<=` change fails a test instead of silently shifting behaviour |
| swap ratio below 0.40, memory nominally fine | refuse |
| `/proc` unreadable | **allow**, reason recorded |
| `WC_RESOURCE_GUARD=off` | allow, and asserts a WARNING is logged |

The fail-open case earns its test because it is the one a future reader will
"fix" into fail-closed. The override case earns its test because an override
that stops logging becomes invisible.

Plus one integration test per shell caller asserting the **exit code**: a guard
that returns the right verdict and the wrong exit status blocks nothing.

## Known limits

- Voluntary. Callers that do not ask are not covered.
- `report()` reads `/proc/<pid>/cmdline` for every `claude` process on each
  refusal. Cheap at this scale (21 processes) and not on any hot path, but it is
  a linear scan and should not be moved somewhere frequent.
- The interactive-versus-console split keys on `--output-format stream-json`. If
  the console's argv ever stops carrying that flag the split silently
  mis-attributes. CLAUDE.md §0 pins that argv, so the coupling is documented on
  both ends.
