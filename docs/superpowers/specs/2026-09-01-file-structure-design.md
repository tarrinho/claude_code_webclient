# File structure reorganisation — design

Date: 2026-09-01 · Target release: 0.10.0 · Author: cweb1 (with Pedro Tarrinho)

## Why

Three goals, in priority order, decided with the operator:

1. **Sessions colliding in one file.** Eight sessions share one working tree and
   one index. `app.py` carried nine foreign hunks at once today, and landing a
   security fix required cutting a patch by hand to avoid sweeping other
   people's in-flight work into a security commit.
2. **Bugs hiding in long functions.** `handle_supervisor` (237 lines),
   `classify_chat` (200), `SupervisorEngine._run_planner_turn` (195) and
   `_execute_task` (122) are where this week's defects lived.
3. **Navigability**, which falls out of doing the first two properly.

## The observation that shapes the design

**None of this project's real defects were caused by file size, and none would
have been prevented by splitting files:**

| Defect | Cause |
|---|---|
| Cross-tenant read of supervisor tasks and messages | five `db.py` functions accepted `owner_id` and never used it |
| SSE poller re-sent the same hundred messages for ever | `after_id` accepted and ignored, two byte-identical branches |
| A timer nothing could stop | bare `setInterval` with no handle |
| Removal of an auth bypass surfaced as two skips | `except Exception` wrapped an assertion |
| `-p` accepted as a model id | validation pattern permitted a leading dash |

Every one is an **unenforced invariant**. What caught them was AST sweeps and
structural tests, not smaller files. Two of them lived in `db.py`, which is the
second-calmest file per line in the repository.

So a reorganisation that only moves code is cosmetic. This one installs the
invariant each boundary implies, and the test is written **before** the move it
justifies — that is what makes each move mechanically safe rather than hopeful.

## Measurements the design rests on

Taken from the code, not estimated.

**Helper partition in `app.py`** — 123 private helpers, traced transitively from
each route wrapper:

| Serves | Count |
|---|---|
| exactly 1 route prefix | **96 (78%)** |
| 2 prefixes | 10 |
| 3 prefixes | 5 |
| unreached from any route (import/lifespan only) | 12 |

The 78% is what makes route extraction mostly mechanical: those helpers move
with their route module and need no new home.

The five genuinely cross-cutting helpers form exactly two clusters, and they are
named from measurement rather than imagination:

* `_is_private_ip`, `_resolve_host` → SSRF/network validation
* `_question_to_text`, `_answer_to_text`, `_turn_to_message` → transcript rendering

**Contention, commits per file over two days:**

| commits | file | lines |
|---|---|---|
| 12 | `web/supervisor.js` | 1710 |
| 11 | `app.py` | 5792 |
| 8 | `supervisor.py` | 1041 |
| 6 | `config.py` | 185 |
| 5 | `db.py` | 3291 |
| 3 | `transcripts.py` | 1531 |

Contention, not size, sets the order. `db.py` is the second-largest file and one
of the calmest; `web/supervisor.js` is the hottest file in the repository.

**No dead code.** Zero unreferenced private helpers, so there is nothing to
delete before moving.

### How often a change would cross two of the new files

This is the number the split lives or dies by, because staging in this tree is
whole-file: a patch that spans two files cannot be committed without carrying
whoever else's edits are sitting in them. It is also the number this design got
wrong twice, so the method is recorded alongside each figure rather than the
figure alone.

Window: the 22 commits touching `app.py` since 2026-08-30. Two mappings from
function to target file, and two treatments of module-level lines:

| mapping | module-level lines | commits crossing 2+ files |
|---|---|---|
| each function by its own name and decorator | counted | 27% |
| each function by its own name and decorator | ignored | 36% |
| §81's rule — routes carry their single-prefix helpers | counted | 22% |
| §81's rule — routes carry their single-prefix helpers | ignored | **50%** |

The mapping matters more than the module-level treatment. §81 puts a helper
reached only from chat routes into `routes/chats.py`, so attribution has to
propagate along the call graph from the decorated routes; classifying each
function by its own name leaves those helpers in shared modules and inflates the
count on nearly every commit. The transitive figures were measured by cweb2
against the design's own rule; the earlier 56% quoted in conversation was mine,
from a narrower window and a scan that matched only `@@ ... @@ def name` hunk
headers, and it should not be relied on.

Two facts verified here independently, by AST rather than by hunk header: 14 of
the 22 commits change module-level lines, and 286 module-level lines change in
total. (cweb2 counted 306; the gap is whether a top-level `def` header and its
decorators count as module level. It does not affect the commit count.)

**So the honest answer is a range, 22% to 50%, and which end applies is a
judgement rather than a measurement** — it turns on whether editing a constant
or a route registration counts as touching a second file. In this tree it does,
because those lines land in `main.py` or `validation.py` under §81 and staging
is whole-file, which argues for the upper end. That is an argument, not a
finding, and it is stated here as one.

**A shared helper layer survives the split.** Even under transitive
attribution, a `shared` bucket appears in 8 of the 22 commits. The split does
not eliminate cross-file helpers; it gives them names (`net_validation.py`,
`transcript_render.py`, `validation.py`). "Every change lands in one route file"
is not what the data supports at either end of the range, and the design should
not be sold on it.

## Target structure

### `app.py` 5792 → 11 files

| file | ~lines | contents |
|---|---|---|
| `main.py` | 80 | app construction, router registration, lifespan, exception handler, the 12 import-time helpers |
| `middleware.py` | 280 | the three middleware classes, `_session_from_api_token`, `_authenticated_by_token` |
| `validation.py` | 220 | module-level constants and validators |
| `classification.py` | 260 | `classify_chat`, decomposed |
| `sse.py` | 300 | `stream_handler` and the shared SSE lifecycle |
| `net_validation.py` | 90 | measured cluster |
| `transcript_render.py` | 180 | measured cluster |
| `routes/chats.py` | 1100 | 22 routes and their single-prefix helpers |
| `routes/supervisors.py` | 800 | 15 routes |
| `routes/machines.py` | 450 | 8 routes |
| `routes/misc.py` | 400 | transcripts, tokens, sessions, settings, system, usage, admin |

`routes/misc.py` rather than six files: those prefixes have three or fewer
routes each, and six files of ~200 lines would sit under the 150-line floor this
plan is built on. They split when they grow.

### `supervisor.py` 1041 → 6 files

`SupervisorEngine` is 558 lines — over half the file — and its two largest
methods are 317 lines between them. A four-way split leaves `engine.py` at ~600
lines still holding both, so the decomposition is the work and the split is
bookkeeping.

| file | ~lines | contents |
|---|---|---|
| `plan_parser.py` | 200 | `ParsedTask`, `PlanParser`, the six plan regexes |
| `model_router.py` | 90 | `ModelRouter`, `DEFAULT_RULES`, `COMPLEXITY_PATTERNS` |
| `task_graph.py` | 120 | `TaskNode`, `TaskGraph` |
| `progress.py` | 60 | `ProgressEvent`, `ProgressTracker` |
| `engine.py` | 450 | `SupervisorEngine`, after decomposition |
| `prompts_text.py` | 80 | `SUPERVISOR_SYSTEM_PROMPT`, `clean_result` |

### `web/supervisor.js` 1710 → 8 files, under `web/assets/supervisor/`

The file already carries 21 comment-banner sections; the split follows those
rather than inventing boundaries: `main.js`, `api.js`, `list.js`, `banners.js`,
`tasks.js`, `stream.js`, `members.js`, `layout.js`.

**This is not only a move.** `supervisor.js` is a plain IIFE loaded as
`<script src="supervisor.js?v=5">`, while `index.html` uses
`<script type="module">`. Splitting requires converting to ES modules, which
changes the page tag, moves the file under `/assets/` so `StaticFiles` serves
it, and lets `_serve_supervisor_js` and `_serve_supervisor_page` be deleted from
`app.py`. `tests/test_qa_version_consistency.py`'s `STATED` table needs its
`supervisor.js` and `supervisor.html` patterns updated with the move.

### Left alone, deliberately

`db.py`, `transcripts.py`, `runner.py`, `prompts.py`, `sysstats.py`, `auth.py`,
`turns.py`, `config.py`, `claude_proxy.py`.

Large but calm and internally ordered. Splitting them is a wide diff buying
navigability only, on files nobody is colliding in. `claude_proxy.py`
specifically stays whole: it is a separate deployable with zero first-party
imports, and splitting it multiplies the version-skew surface that has already
caused one silent outage.

Net: **11 modules → 30 files.**

## Structural tests — the C half

Each boundary gets an invariant, written **before** the move it justifies, so
the move is verified rather than hoped for. All are source-level invariants in
the same sense as the compile gate: the property is about the text.

| test | invariant | why it is not cosmetic |
|---|---|---|
| `test_qa_test_layout` | every test file lives under `tests/` | `test_functional.py` at the root was invisible to the chunked runner; 0.9.4 was released against a total that could not see its real failure |
| `test_qa_module_boundaries` | no `routes/*` module imports another `routes/*` module | the partition is only real if it holds; a route reaching into another prefix's helpers is the coupling the split exists to remove |
| `test_qa_module_boundaries` | the import graph stays acyclic, and `config` imports nothing first-party | the current graph is clean; a 30-file split is exactly when that stops being true by accident |
| `test_qa_owner_scoping` (exists) | no `db.py` function accepts `owner_id` and ignores it | already caught five |
| `test_qa_timer_handles` (exists) | every `setInterval` keeps its handle, across `web/**.js` | already caught two, in two different globs |
| `test_qa_frontend_layout` | every client script lives under `web/assets/` | the one file outside it is why a §4 check scanned the wrong glob and reported clean |

Each test is committed with, or before, the move it guards.

## Sequence

Ordered by contention and by safety, not by size. Every step ends with the
suite; the tree has 33,000 lines of tests and this is what they are for.

0. **Spec and announcement.** This document, plus a message to the other
   sessions naming the file-by-file window protocol.
1. **Test hygiene.** `test_functional.py` → `tests/`, fix its stale fixture (it
   mocks four collaborators but not `bump_chat_updated_at`, added later), add
   `test_qa_test_layout`. Uncontended, closes a release-integrity hole.
2. **Structural tests** for the boundaries that do not exist yet. They fail;
   that is the point. They pass as each move lands.
3. **Decompose in place**, no file moves: `classify_chat`, `handle_supervisor`,
   `_run_planner_turn`, `_execute_task`. Safe with other sessions active,
   because editing within a file is an ordinary merge.
4. **Extract modules, one file at a time, announced.** Order:
   `web/supervisor.js` (hottest), `app.py` routes (one prefix per window),
   `supervisor.py`. Suite between each.
5. **Frontend consistency.** ES-module conversion, `/assets/` move, delete the
   two serving routes, drop `supervisor.html`'s inlined stylesheet block.
6. **0.10.0.** Bump `config.VERSION` and the five other files that state the
   version, in one commit, with `test_qa_version_consistency` green.
   `docs/threat-model.md` keeps its own analysed-version line: it records which
   build was analysed, and rewriting it would claim an analysis nobody did.
7. **Full rules.md pipeline**, stages 0 through 20, on a quiet box.

## Coordination protocol

Moving a function between files is the worst change git has to merge: it sees a
deletion in one file and an addition in another, so a concurrent edit to that
function either conflicts or survives silently as a duplicate definition. That
exact failure is registry #51 — a commit swept half a feature and left `HEAD`
calling a function no file defined.

Therefore:

* **Decomposition in place** (step 3) proceeds concurrently. No announcement.
* **Every file move** takes an announced exclusive window on that one file:
  message the sessions, take minutes not hours, commit, release.
* **Staging is hunk-by-hunk, never `git add -A`**, and the index is verified in
  isolation with `git archive $(git write-tree)` before the commit — a working
  tree with eight authors is not what the commit should be measured against.

## Where to stop

The failure mode is sixty files of eighty lines where every change is a
five-file diff. Guardrails, adopted from the original suggestion and kept:

* **Target 150–500 lines.** Under ~100 it belongs with its neighbour; over ~800
  it is hiding a second responsibility.
* **Split only at a boundary nameable in a noun phrase.** If the filename wants
  an "and", the split is wrong.
* **Never split a file you cannot test afterwards.** If extracting a module
  needs three others imported to exercise it, that is a coupling problem
  wearing a file-size costume — fix the coupling first.
* **One module per split, then the suite.** Thirty small verifications, not one
  large one.

## Explicitly not doing

* **A `db/` package.** The original suggestion proposed eleven files; two of
  them (`db/machines.py`, `db/ai.py`) are the same group counted twice — there
  is no `machine_*` prefix, the AI machine functions are `ai_*`. Two more
  (`db/users.py` at two functions, `db/sessions.py` at three) fall under the
  plan's own floor. And 25 functions — `queue_*`, `messages_*`, `fts_*`,
  `system_*`, `read_*`, `routed_*` — have no home in the eleven. The file is
  calm and ordered; it can be revisited once the hot files are quiet, as a
  measurement rather than a guess.
* **Splitting `transcripts.py`, `runner.py`, `prompts.py`, `sysstats.py`,
  `auth.py`.** Same reasoning. `prompts.py`'s screen/tmux seam is real and
  currently invisible, but extracting it is a restructure rather than a move —
  every function branches on `kind` internally — and it is a calm file.
* **Renaming anything that is only awkward.** Churn against eight sessions'
  working trees needs to buy something.
