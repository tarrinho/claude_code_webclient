# Auto-answer knob — approving prompts without a person, per chat

Date: 2026-09-02 · Target: after the 0.10.0 file-structure reorganisation

A per-chat toggle. Off, prompts wait for the user, exactly as today. On, the
console answers approval and permission prompts itself and keeps the last ten
answers it gave, readable from an `i` affordance beside the toggle.

## The security decision, stated plainly

This feature approves, without a person, the prompts that exist to ask a
person. `prompts.read_prompt`'s own docstring gives the example:

> Permission rule `Bash(git push*)` requires confirmation for this command

Answers are delivered by driving keystrokes into a live tmux or screen pane, so
the effect is immediate and there is nothing to undo. Turning this on for a chat
means that chat approves what the permission rules were configured to stop.

It is a legitimate control on an operator's own machine — the CLI ships
`--dangerously-skip-permissions`, and this project's own proxy already passes it
(`claude_proxy.py:438`, `runner.py:561`). The point of writing it down is that
the blast radius is a deliberate choice, not a preference:

- **Default off**, per chat, never global.
- **Every answer recorded**, and the record is the only trace.
- **The UI says what it does.** Not "Auto-answer" as though it were a display
  option: it approves permission prompts.

## What it will and will not answer

Three different things can block a session, and only two of them have a
meaningful "yes".

| Kind | Where it comes from | Auto-answered |
|---|---|---|
| Permission prompt | terminal only; the CLI never writes it to the JSONL | **yes** |
| Approval (ExitPlanMode) | transcript, renders approved/rejected | **yes** |
| `AskUserQuestion` | transcript; N questions, each with labelled options | **no** |

`AskUserQuestion` is excluded because "yes" does not exist for it. Given options
`Delete the branch` / `Keep it`, an auto-yes would have to pick one blind, and
index 1 is not reliably the safe one.

**No new classifier is needed for this.** The codebase already draws exactly
this line: `approval: True` is set by the screen reader at `prompts.py:661` and
by the transcript path at `transcripts.py:277`, and propagated at
`transcripts.py:1615`. A structured question has no `approval` flag and carries
real `options`. So the gate is:

```python
if not pending.get("approval"):
    return  # a structured question; it waits for a person
```

That is worth preferring over any heuristic on the prompt text. An earlier piece
of work in this repo matched `_ASKS_FOR_INPUT` phrases against prose and
produced false positives on ordinary sentences ("worth fixing", "your call",
"should I"); reading a flag the producer already set has none of that risk.

## Choosing the option — the trap

Options are read off the live terminal by `prompts.visible_options`, which
parses `N. Label` lines. A permission prompt conventionally shows:

```
1. Yes
2. Yes, and don't ask again for Bash(curl*) in this project
3. No, and tell Claude what to do differently
```

Index 1 and index 2 are **both affirmative**, and index 2 grants a standing
permission for every future command matching that rule. Pressing 1 blindly is
usually right and occasionally grants something nobody chose — and because
`prompts.answer` navigates and confirms, a wrong choice is committed before
anyone sees it.

So selection is by **label**, not by position:

- Take `visible_options(snapshot)`.
- Accept only a label that is affirmative **and** carries no broadening clause —
  reject anything matching `don't ask again`, `always`, `for this project`,
  `and tell Claude`.
- If exactly one option survives, answer it.
- **If none survives, or more than one does, do not fire.** Record the skip and
  leave the prompt for the user.

Not firing is the decided behaviour: auto-mode never presses a key it cannot
justify. The cost is that some prompts still block; the alternative is a
standing permission grant arriving silently in a log nobody is reading.

## Trigger

One `asyncio` task, started with the app, polling only chats whose knob is on.

- Interval ~5s. Each tick costs a `prompts.locate()` and one multiplexer
  snapshot per enabled chat, so cost tracks how many are switched on, not how
  many chats exist.
- **Held in a module-level handle and cancelled on shutdown.** `rules.md` §4
  names a bare unstoppable timer as a failure case, and this project has already
  been bitten: `web/supervisor.js` carried a bare `setInterval` that nothing
  could stop and that doubled whenever its setup ran twice. A background task
  with no handle is the same defect in Python.
- Server-side rather than in the page, so an unattended agent is answered with
  no browser open. A client-side implementation would fire only while a tab was
  open, and twice with two tabs.

Reuses `app._pending_prompt(session_id)`, which asks the transcript first and
the terminal second — so the watcher inherits the ordering fix already made
there rather than re-deriving it.

## Sessions that cannot be answered

`prompts.answer` needs the session inside screen or tmux; the existing endpoint
returns 409 *"This session cannot be answered from here — it is not running
inside screen or tmux"* otherwise.

So the knob can be **on and structurally unable to fire**. The UI must show that
state rather than leaving a toggle that looks armed and does nothing — a control
that silently fails is worse than one that is visibly unavailable. The watcher
records the skip reason so the `i` panel can say why.

## Data model

Two additive columns, via the existing `_ensure_chat_columns` pattern at
`db.py:378` — a `PRAGMA table_info(chats)` read and a per-column `ALTER TABLE`,
alongside `pinned`, `deleted_at`, `model`, `ai_machine_id`:

```python
"auto_answer":     "ALTER TABLE chats ADD COLUMN auto_answer INTEGER NOT NULL DEFAULT 0",
"auto_answer_log": "ALTER TABLE chats ADD COLUMN auto_answer_log TEXT",
```

`auto_answer_log` holds a JSON array, newest first, **trimmed to 10 on write**
so it cannot grow. Each entry:

```json
{
  "at": "2026-09-02T14:03:11Z",
  "kind": "Permission",
  "prompt": "Permission rule Bash(curl*) requires confirmation…",
  "index": 1,
  "label": "Yes",
  "outcome": "answered"
}
```

`outcome` is `answered` or `skipped`, and a skip carries `reason` — no
affirmative option, ambiguous options, or not in a multiplexer. The skips are
the entries worth having: they are the prompts still waiting for the user.

A capped column rather than a table, matching how config blobs are already
stored. It is a rolling window, not an audit trail — if a durable record of
what an agent approved is ever wanted, that is a separate table and a separate
decision.

## API

| Route | Purpose |
|---|---|
| `PUT /api/chats/{id}/auto-answer` | `{"enabled": true\|false}` |
| `GET /api/chats/{id}/auto-answer` | current state plus the last ten entries |

Both owner-scoped through `db.chat_get(chat_id, session["user"])`, like every
other per-chat route. This matters more than usual here: the routes arm an
automatic approver, so a cross-tenant write would let one user turn on silent
approval in another's chat. Scoping is already the pattern — five `db.py`
functions once accepted `owner_id` and ignored it, which is the defect
`ec548d1` fixed, and this is exactly the shape of thing that regression would
have made dangerous.

## UI

- A toggle in the chat controls, default off, labelled so it reads as approving
  permission prompts rather than as a display preference.
- An `i` beside it opening the last ten entries, newest first, each showing
  time, the prompt's first line, and either the chosen label or the skip reason.
- When the session is not in a multiplexer, the toggle renders unavailable with
  the reason, not merely off.
- Inherits the mobile spec's rules: 44×44 touch targets on coarse pointers,
  Escape closes the panel and restores focus, and the panel is a live region so
  a new auto-answer is announced rather than appearing silently.

## Testing

The two tests that matter most are the ones whose failure is silent:

1. **A structured `AskUserQuestion` is never auto-answered.** Feed the watcher a
   pending question with `options` and no `approval` flag; assert no keystroke
   is delivered.
2. **A broadening option is never selected.** Options `1. Yes`,
   `2. Yes, and don't ask again for Bash(curl*)`; assert index 1 is chosen, and
   again with the order reversed to prove selection is by label and not by
   position.

Then: the log caps at 10 and keeps newest first; a skip records its reason; the
watcher task is cancellable and does not survive shutdown; a chat with the knob
off is never polled; a non-multiplexed session records a skip rather than
raising; and the routes reject a cross-owner chat id with 404.

**Mutation-check both suites before claiming them**, and confirm each mutation
actually changed the file before drawing a conclusion — a mutation that fails to
apply reports a false pass, which has happened in this repo. Mutations to make:
drop the `approval` gate; reverse the label preference so the broadening option
wins; remove the log trim; leave the watcher handle uncancelled.

## Sequence

Split by contention, because `app.py` is being reorganised into 11 files.

1. **`db.py`: the two columns and their helpers.** Uncontended — `db.py` is on
   the reorg's *leave alone* list. Land first.
2. **The watcher and the option-selection logic**, as a module of its own rather
   than more `app.py`. Testable without HTTP, and it does not add surface to a
   file being split.
3. **The two routes** — hold until the reorg's `app.py` route extraction has
   landed. Adding endpoints to a file mid-split is the delete-here/add-there
   merge the reorg design calls registry #51.
4. **UI**, last, once the state it reflects exists.

## Out of scope

- **Auto-answering `AskUserQuestion`.** Decided against above.
- **A global or per-user default.** Per chat only. A global switch would arm
  every future chat, including ones created by automation, which is not a thing
  to have by default.
- **A durable audit trail.** The rolling ten is the decided storage.
- **Answering sessions outside screen or tmux.** Would mean inventing a delivery
  channel into a process that only reads a terminal.
