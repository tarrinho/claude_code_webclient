# Ladder pinning — design

**Status:** design approved 2026-09-18, not yet implemented.
**Scope:** let an operator edit a task type's ladder directly — replace a rung,
add one, remove one — and have that edit survive re-benchmarking. Editing only.
No change to what routes, because nothing routes yet (§7).

---

## 1. Why this exists, and what it costs

The request was "I want to be able to change each of the ladder options, add one
step, or remove one existing one."

Today a ladder is **derived and never stored**. `routes/delegation.py` says so
on the line that sends it to the page: *"Derived, never stored: the ladder is
the output of walking 2.6, so sending a stored copy would let the page show a
ladder the generator does not produce."* `CapabilityTable.ladder()` recomputes
it on every call from spec 3's three steps — take the ladder-eligible rows,
sort cheapest-first on effective cost per task, walk that order skipping any
model measured strictly worse than the current rung — then applies spec 5's
attempt budget through `_truncate_to_attempt_budget`.

Storing an operator's ladder introduces a second answer to a question that has
had exactly one. That is the whole difficulty of this design, and every section
below is a consequence of it. This is why the work is architectural rather than
a new endpoint: nine production call sites read `ladder()`, and four spec
sections (§1.1, §2.6, §3, §9.2) describe behaviour that assumes the answer was
computed.

### 1.1 The four operator decisions this design encodes

Taken 2026-09-18, in order, each one narrowing the next:

1. **A pin is a pinned override that survives.** Not a constraint fed back into
   the generator, and not a one-off discarded at the next write. A re-benchmark
   that makes a cheaper model better does not change a pinned ladder. The
   operator clears the pin or it stands.
2. **A pin that breaches an invariant is allowed, and warns loudly.** It is not
   refused the way a capability-row edit is refused.
3. **A pinned rung may name a model with no measured row for that task type.**
   Cost and latency for that type then become *incomputable*, not merely over
   budget.
4. **The editor caps at the attempt budget.** `MAX_ATTEMPTS` is 3, and
   `GATE_MAX_ATTEMPTS` is 2 for `reviewer-gate` and `security-gate`. A rung past
   that limit can never run, so it cannot be added.

Decisions 2 and 3 collide, and §4 is the resolution.

### 1.2 The cost, stated once rather than discovered later

Spec 3's walk produces a ladder whose accuracies are **non-decreasing**: rung 1
is never measured worse than rung 0. That property is what makes climbing a
rung meaningful — a retry at the next rung is a retry somewhere better.

Decision 3 gives that up. A pinned rung with no measured accuracy has no place
in that ordering, so for a pinned type "climb to the next rung" stops carrying a
guarantee and becomes whatever the operator arranged. That is a loss of meaning
rather than a missing number, and it is the honest price of the freedom asked
for. It is recorded here so nobody later reads an unordered pinned ladder as a
bug in the generator.

---

## 2. Storage

```sql
CREATE TABLE IF NOT EXISTS delegation_ladder_pin (
    task_type  TEXT PRIMARY KEY,
    rungs      TEXT NOT NULL,   -- JSON array of model ids, in rung order
    updated_at TEXT NOT NULL
);
```

**A full ordered list, not a diff.** "Replace a rung, add one, remove one" is
precisely a list editor, and the list is at most three entries long. A diff
representation ("always include X", "never use Y") would need its own rules for
what happens when the generator no longer produces the rung a diff refers to —
a whole conflict-resolution problem bought for nothing at this size.

`task_type` is the primary key: one pin per type, and clearing a pin is a
delete. No `id` surrogate, because unlike `delegation_routing_decision` there is
no history to keep here — a superseded pin is not data, it is a previous
setting.

`rungs` is JSON rather than a delimited string. Model ids contain `/` and `.`
and `-`; picking a delimiter they cannot contain is exactly the kind of
assumption that breaks the first time a gateway renames something.

An empty array is a legal pin and means **"this type has no ladder"** —
distinct from having no pin, which means "use the generated one". The settings
page must not collapse the two.

Accessors live in `routes/db_delegation.py` beside the other delegation tables
and are registered in `db.py`'s dispatch map:

- `async def delegation_pin_all() -> dict[str, list[str]]`
- `async def delegation_pin_set(task_type: str, rungs: list[str] | None) -> bool`
  — `None` clears.

---

## 3. The seam

`CapabilityTable.__init__` gains a `pins: dict[str, list[str]] | None = None`
parameter, defaulting to None so every existing construction — including every
test that builds a table by hand — keeps its current behaviour exactly.

- `ladder(task_type)` returns the pin when one exists for that type, else
  generates as it does today.
- `generated_ladder(task_type)` is the current implementation, renamed and kept
  public. It is what the page compares against to show "this differs from the
  generated ladder", and what §4's boot fallback falls back to.

**One change covers every consumer.** The nine production readers of `ladder()`
are: `_worst_case` (5.1's path), `_tree_cost` (2.7's cost), the gate-multiplier
term, two branches of `validate()`, the routes payload, `orchestrator.assign_model`,
and `delegation_recorder.resolve_shadow`. None of them needs editing, and none
of them should — a consumer that reached past `ladder()` to the generator would
be a consumer that ignores pins.

`expected_tokens` stays a parameter of `generated_ladder` only. It exists to let
a caller price a ladder at a different call size; a pinned ladder does not
depend on cost at all, so passing it through would imply an influence that is
not there.

---

## 4. Pins never block the boot

This section resolves the collision between decisions 2 and 3.

`validate()` gates a *breach* behind its enforcement knob, but reports missing
data unconditionally — `problems.extend(why_not)` runs whatever the knobs say,
for both the ceiling and the budget, and the comment beside it explains why:
*"an incomputable worst-case path is MISSING DATA, not a breach, and the
enforcement knob does not gate it."*

So under decision 3, pinning an unmeasured model on an operational type would
make `validate_or_die` raise and the service refuse to start — at the next
restart, possibly hours later, looking nothing like the pin that caused it.
That is the failure mode this system has now been bitten by twice in other
forms, and decision 2 says a pin should warn rather than refuse.

**Resolution.** `CapabilityTable.without_unusable_pins()` returns `(table, dropped)`.

For each pinned task type it collects that type's problems twice — once from a
table carrying the pin, once from the same table with the pin removed — and
drops the pin if and only if the first set is not a subset of the second. The
comparison is on the problem STRINGS, scoped to problems naming that task type,
because those strings are the same ones the page and the log render and
comparing anything else would let the two disagree about what "the same
problem" means.

Both collections are made with enforcement forced ON, not with the live knob
settings. The question this is asking is "could this pin stop the service
starting", and the answer must not change when an operator toggles a knob — a
pin that is dropped today and kept tomorrow because the budget knob moved would
make the boot log unreproducible.

`validate_or_die` calls it before validating, and logs each dropped pin at
`warning` with the task type, the pinned rungs, and the specific problem.

Consequences, all deliberate:

- A bad pin is **stored, shown, and not in effect**. The two places that say so
  are the Delegation page and the boot log. This is the real cost of decision 2
  and it is not hidden.
- A problem the generated ladder *also* has is not the pin's fault, so the pin
  is kept and the problem still refuses to start — the existing rule for bad
  data is untouched.
- Dropping is per task type, not global. One unusable pin does not discard
  another type's good one.
- Nothing is written during a drop. It is a boot-time decision about which
  ladder to use, not an edit; the pin is still there to be fixed or cleared.

---

## 5. Endpoint

`PUT /api/delegation/ladder`, admin only, body `{task_type, rungs}` where
`rungs` is a list of model ids or `null` to clear.

Refused with 400:

- an unknown `task_type` — one with no rows in `delegation_capability`;
- more than `MAX_ATTEMPTS` rungs, or more than `GATE_MAX_ATTEMPTS` for a gate
  type (`is_gate_task_type`);
- a duplicate rung within one ladder — the same model twice is not a ladder,
  it is a retry, and retries are `MAX_ATTEMPTS`;
- a model id that fails `ModelRouter.validate_model`'s shape check.

**Not refused:** a pin that breaches the budget or the ceiling, and a pin naming
a model with no row for that task type. Those are decisions 2 and 3.

The response carries `{"ok": true, "problems": [...]}` — the output of
`delegation_startup.problems_with` against the prospective table, so the page
can show the consequence immediately in the same words every other path uses.
Validation is not *skipped* here; it is run and reported without blocking.

`problems_with` is already the single place that pairs a table with the live
settings of both enforcement knobs, shared by the startup check, both settings
write endpoints and the seed script. This endpoint is its fifth caller and must
not grow a sixth copy of that logic.

---

## 6. The page

The ladder on each card becomes editable in place:

- each rung is a control that can be replaced from the live model list, or
  removed;
- rungs can be reordered;
- **"add a step" disables at the cap** — 3 rungs, or 2 for `reviewer-gate` and
  `security-gate` — with the reason in its `title`: generation stops after that
  many attempts, so a further rung can never run;
- a pinned type carries a badge and a "revert to generated" control, which shows
  what the generator would produce before it is clicked.

Breaches and incomputable costs render through the **existing `warnings`
channel** — the one already used for "over ceiling, reported not enforced" —
never through `blockers`. A blocker means "you cannot do this"; a pin's problem
means "you did this and here is what it costs", and merging them would make the
page say the flip is impossible when it is not.

All writes go through `apiFetch`, never bare `fetch`. `PUT` is a mutating
request and `/api/delegation/ladder` will not be in `CsrfMiddleware._EXEMPT_PATHS`,
so a bare `fetch` is answered 403 before the handler runs. Both enforcement
knobs shipped with that bug and it went unnoticed for a day because their tests
called the handler directly.

The `?v=` reference for `delegation.js` must be regenerated with
`bin/wc-asset-versions.py`, never by hand. A hand-written token was wrong twice
in two days; the checker reads it as a number and a hash prefix silently
truncates.

---

## 7. What this deliberately does not do

- **It does not make anything route.** `ModelRouter.assign_model` still has no
  production caller and `app.state.capability_table` is still written at
  `app.py:629` and read nowhere. A pin changes what the page shows, what the
  shadow recorder records, and what the invariants are computed against — not
  which model serves a turn.
- **It does not feed pins back into the generator.** Decision 1 rejected the
  constraint model; a pin overrides, it does not steer.
- **It does not keep pin history.** Superseded pins are not data.
- **It does not pin gate ladders differently.** A gate type is pinnable on the
  same terms, with its own lower cap.

---

## 8. Testing

In `tests/test_qa_delegation_ladder_pin.py`, following the repo's
`tests/test_qa_*.py` conventions.

**Schema and accessors** — the table exists after `db.init()`; a pin round-trips
including an empty list; `None` clears; `delegation_pin_all` returns every pin
keyed by task type.

**The seam** — `ladder()` returns the pin; `generated_ladder()` still returns
the generated ladder for the same table; a table built with no `pins` argument
behaves exactly as before. Then, one test per production consumer, driving each
of the nine call sites against a pinned table and asserting it saw the pin. That
list is written out rather than sampled: a consumer that reaches past `ladder()`
is the failure this design is most exposed to.

**Boot safety (§4)** — a pin naming an unmeasured model makes `_tree_cost`
incomputable AND `validate_or_die` does not raise; the dropped pin is logged
with its task type and the problem; a pin that breaches but is computable is
kept in the page payload; a problem the generated ladder *also* has still
refuses to start; one unusable pin does not discard another type's usable one;
and the drop writes nothing — the pin is still in the database afterwards.

**Endpoint** — each 400 case above, asserted on the reason and on nothing having
been stored; a breaching pin stored successfully with its problems reported in
the response.

**Page** — a browser test that clicks "add a step", clicks a rung's remove
control, and then reads the pin back from `GET /api/delegation`, not from
`aria-*` state. Plus: the add button is disabled at 3 rungs and at 2 for a gate
type, asserted with `is_disabled()` and never `get_attribute("disabled")` —
a present boolean attribute reads back as `""`, and `bool("")` is False, so the
attribute form passes either way. That exact mistake has shipped in this file
before.

Every assertion is verified by mutating the code it covers and confirming a
**named** test goes red, and every mutation asserts its anchor matched before
rewriting — a `.replace` that silently no-ops has produced false negatives in
this subsystem before.

A fixture warning specific to this area, from five instances so far: a fixture
that puts a free row in front means a dear row lands at rung 1, where
`REACH_PROBABILITY` halves its contribution, so a fixture meant to be over
budget quietly is not and the test stops being able to fail. Price fixtures for
the rung they will actually occupy, and derive rates from `BUDGET_USD` rather
than writing literals — a literal stops describing an over-budget ladder the
moment the budget moves, which has now happened three times.

---

## 9. Open questions

- **Does a pin survive a task type losing all its rows?** Currently it would:
  the pin is keyed on task type, not on the rows. The page would show a ladder
  for a type with an empty capability table. Harmless today, worth deciding
  before routing.
- **Should clearing a pin be undoable?** §2 says a superseded pin is not data.
  If that turns out wrong in use, the fix is the `delegation_routing_decision`
  shape — a surrogate key and an append-only table — not a second column here.
