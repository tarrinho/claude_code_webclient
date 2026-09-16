# Spark framework — evaluation for WebConsole

**Status:** `[Decided]` — do not adopt yet; adopt after the worktree migration.
**Date:** 2026-09-16.
**Subject:** `spark-agentic-engineering-framework` (Celfocus-SPARK), Beta.
**Versions seen:** `.spark/manifest.json` declares `framework_version: 0.4.0`;
`README.md`'s install links pin `v0.3.0`.

Every claim below was read from the source in the distributed archive, not from
its README. Where the two disagree, that is noted, because on the one occasion
they did the README was the optimistic one.

This document is deliberately **not** in `docs/superpowers/specs/`. Everything
in that directory is listed in the Settings > Specs gallery as a design spec for
this product, and this is an assessment of somebody else's software. It is in
`docs/` alongside `threat-model.md` and `project-overview.md`.

---

## 1. The question that was asked, and why it had no answer

The brief was: *should we add all our WebConsole code to that framework, or
learn from it and apply it to new specs one at a time?*

The first option does not exist. **Spark is a control plane, not a container.**
Nothing is moved into it. `spark init` writes six paths into a repository that
stays entirely its own (`core/internal/initx/locations.go`):

```
.spark/manifest.json     .spark/loops/default.json     .spark/.env.example
.gitignore (appended)    .claude/settings.json         .vscode/settings.json
```

It does not touch `CLAUDE.md` or `AGENTS.md` — verified by grepping `core/` for
a writer of those names, rather than by believing the "zero footprint" claim.

So the real choice is *install it around our repo, or don't*, and the answer
turns on one question the README does not address: what happens when eight
agents share one working tree.

## 2. What it is

A meta-harness. A Go binary (`spark`) on PATH plus a host plugin that registers
four Claude Code hooks (`plugins/spark-claude/hooks/hooks.json`):
`PreToolUse`, `SessionStart`, `SubagentStart`, `UserPromptSubmit`.

A task becomes a gated SDLC: `spec → implement → verify`, each stage delegated
to a role with a fixed permission tier, each transition requiring a human
sign-off. Enforcement is at `PreToolUse` and fail-closed — if the binary is
missing, everything is denied. That ordering is why the binary must be installed
before the plugin.

The loop is data, not code. `.spark/loops/*.json` against a published schema,
selected per run with `spark start --loop`. This is not theoretical: the
framework's own repository runs a custom loop, `"default_loop": "jira-loop"` in
its manifest. A team can model its existing workflow here.

## 3. The engineering is good, and that is worth saying plainly

- `core/loop/verdict.go` is 765 lines, and the gate decision is a **pure
  function** over `(Loop, DecisionInput)` — no I/O, no clock, no globals — so
  every branch is testable in isolation.
- **74 test files against 155 Go files**, with dedicated schema-conformance and
  gate-decision suites. Zero `TODO`/`FIXME` in non-test code.
- **No network.** Zero files in `core/` import `net/http`. Telemetry is
  OpenTelemetry *resource attributes* only — it labels spans a host already
  emits; Spark ships no exporter. Local trace is an append-only
  `.spark/trace.jsonl`, gitignored, best-effort, never failing the gate.
- Every branch cites the failure it prevents, at the site. The fail-closed
  branch for an unreadable tool name records that it exists because *"that is
  the failure that let a whole session implement through a no-write stage
  unrefused."* That is the same discipline as this project's own §16 registry,
  enforced in code comments.

One detail worth stealing regardless of adoption: shell paths are extracted
**once** and shared by the forbidden-path check and the write floor, explicitly
because two independent parses were *"a latent inconsistency: nothing guaranteed
the two checks were looking at the same extraction."*

## 4. The decisive finding: it does not solve our problem

Our primary failure mode is five to eight sessions editing one working tree.
Spark has **no locking** — zero files in `core/` contain `flock`, `sync.Mutex`
or `O_EXCL` — and its guidance for parallel work is advisory. `start.go:72`:

```go
p.print("  starting a new run switches this tree's enforcement to it; to work both at once, use a separate worktree.\n\n")
```

That is a `print` statement, not a mechanism.

The nuance matters, though, and an earlier draft of this assessment overstated
it. Spark's **state layout already separates correctly per worktree**:
`.spark/active.json` and `.spark/trace.jsonl` are gitignored, so each worktree
carries its own run state, while `.spark/runs/*/` is tracked and merges through
git like any other file. Two worktrees running two Spark runs would not corrupt
each other. What is absent is **enforcement and visibility** — nothing detects a
sibling worktree with an active run, and nothing surfaces one — not structural
support.

**So Spark's own answer to our worst problem is the worktree migration we had
independently specified.** That makes the sequencing unambiguous rather than
making Spark unattractive.

## 5. The honest limits

- **Beta, and version-inconsistent.** Manifest says 0.4.0, README install links
  pin v0.3.0, and the binary reports `dev` unless ldflags-stamped. Nothing
  checks that the installed binary and plugin agree (§7.3).
- **Shell interception is porous, and says so.** `core/loop/shellpaths.go` is
  571 lines handling redirects, `tee`, `sed -i`/`perl -i`, and a destructive set
  (`rm`, `mv`, `truncate`). Its own comment: *"`python -c "shutil.rmtree(...)"`,
  `find -delete` and `./script.sh` all sail past."* `cp` is excluded
  deliberately, with a stated reason. This is a named boundary, not an
  oversight, and chasing a shell grammar is a tarpit — but a governed repo is
  not sealed.

## 6. Recommendation

**Adopt, but not first.** In order:

1. **Land the worktree migration**
   (`docs/superpowers/specs/2026-09-15-worktree-per-session-isolation-design.md`).
   It is the prerequisite: with one shared tree, Spark's single-active-run model
   would either be routinely bypassed or would serialise the whole fleet.
2. **Pilot Spark on one spec**, in one session's worktree, not repo-wide. Risk
   is genuinely low — six files, an append-only gitignore block, no network, no
   service dependency, fully reversible.
3. **Keep the tiered-delegation work.** It is orthogonal, not redundant: Spark
   governs *who may write, when, with whose sign-off*; the delegation spec
   governs *which model, at what cost, with what escalation*. They compose — the
   ladder runs inside Spark's `implement` stage.

The strongest argument for engaging at all is one this project reached on its
own. Registry #65: *"Habits are not enforced by being written down… what is not
acceptable is a third prose reminder — two have now failed."* Spark's founding
thesis is the same sentence from the other end: *"nothing is enforced by prompt
— the gate is a fail-closed binary beside the harness."* We derived the need
independently; they built the mechanism.

## 7. Improvements to feed back

Celfocus owns this framework, so a pilot here should return findings. Four,
ordered by value against cost. Each is expressed in Spark's own idiom.

### 7.1 Interpreter invocations should set `UnresolvedWrite`, not pass silently

When `sed -i` is detected with no resolvable file, `shellpaths.go` sets
`sp.UnresolvedWrite = true` — an explicit *"a write happened and I cannot see
where."* When `python -c` arrives, extraction returns **zero targets**, which is
indistinguishable from a command that genuinely wrote nothing.

`python`, `python3`, `node -e`, `ruby -e` are not shell grammar. They are a
small closed set of heads where `-c`/`-e` means "opaque program follows", which
is cheaply detectable. Setting the existing flag there converts a silent pass
into a declared unknown, using machinery that already exists.

### 7.2 Measure the soft centre

`.spark/trace.jsonl` records gate outcomes but does not distinguish *"shell
command allowed, zero write targets extracted"* from *"targets extracted and all
permitted."* Those are different facts: the first is the porous path being
exercised, the second is the gate working.

An extraction-confidence field turns "we accept a soft centre" from a design
position into a number. After one real engagement you could say how often the
unparseable path was hit. Nobody can answer that today, including its authors.

### 7.3 Nothing checks that the binary and the plugin agree

A genuine hazard for a fail-closed system installed as two manual pieces in a
required order. Loop *schema* versioning is rigorous — `core/loop/validate.go`
refuses a loop with no `schema_version` and never defaults it — but no code
compares the binary's version to the plugin's, and the shipped artefacts already
disagree (0.4.0 manifest, v0.3.0 README links, `dev` binary). A mismatch would
surface at an arbitrary tool call rather than once, clearly, at `SessionStart`.

### 7.4 Make the worktree advice a mechanism

The gap in §4. `spark start` could detect a sibling worktree of the same
repository holding an active run and refuse or warn, and `spark status` could
surface sibling runs. The state layout already supports concurrent worktrees;
only the operator is left to remember it. This is the one where we would have
standing to report from a real deployment rather than from a reading — and it
should wait until we have run it that way.

## 8. Method note

Everything here was read from source in the archive. The concurrency conclusion
in particular was checked in both directions: the absence of locking by grepping
`core/` for `flock`/`sync.Mutex`/`O_EXCL` (zero files), and the presence of
per-worktree state by reading which `.spark` paths `spark init` adds to
`.gitignore`.

An earlier draft asserted Spark "has nothing for concurrent sessions". That was
too strong and is corrected in §4. The distinction between *no mechanism* and
*no support* changes what we would report upstream, so it was worth getting
right rather than leaving as the more dramatic claim.
