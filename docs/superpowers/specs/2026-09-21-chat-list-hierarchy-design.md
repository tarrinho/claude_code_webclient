# Chat list hierarchy — design

**Status:** approved for implementation planning (2026-09-21)
**Decided with:** Pedro, 2026-09-21
**Mockups:** Settings › Images, "Chat list hierarchy — 3 candidates" (ids 16–18),
files in `/home/kali/projects/chat-hierarchy-designs-2026-09-21/`

## 1. The problem

The sidebar is flat. When a conversation spawns work — a subagent, an
orchestrator's members, a voice brainstorm — nothing on screen says the two are
related. The operator cannot tell which conversation produced which, and for
subagents there is nothing to tell: they leave no record anywhere.

Measured on this deployment on 2026-09-21, before any change:

| | count |
|---|---|
| live chats | 104 |
| chats with a `parent_chat_id` | 2 |
| …of which are hidden voice temporaries | 2 |
| orchestrators / members | 3 / 6 |
| orchestrator tasks / with a parent task | 4 / 0 |

So a tree drawn over today's data would render almost nothing. **The work is
mostly capture, not rendering**, which is why this is a design doc rather than
a one-file change.

## 2. Decisions taken

Four decisions were made before designing, and the design is only valid while
they hold.

1. **All four relationship kinds are in scope**: subagents spawned in a chat,
   orchestrator and its members, voice handoff children, and chats opened from
   another.
2. **An in-chat Task-tool subagent is a display-only child node** — a
   lightweight record with a name, status and timing. It is *not* an openable
   conversation, so a conversation that fans out to several Task subagents adds
   small rows rather than full chats, and "chat" keeps meaning what it means
   today.

   **This governs only the subagents of decision 1's first kind**: those
   spawned by the `Task` tool inside a single conversation, which today leave
   no record anywhere (§4.1). It says nothing about orchestrator tasks, which
   are a separate relationship kind in decision 1 and whose nature is decided
   by the orchestrator's own design, not here — see §4.2.

   An earlier draft of this clause said "a supervisor fan-out", which names
   orchestrator machinery rather than in-chat subagents and read as a ruling
   on orchestrator tasks. It was never one. Corrected 2026-09-21 after a peer
   session reading this spec against
   `2026-09-20-orchestrator-chats-all-the-way-down-design.md` asked which
   of the two kinds it governed — the right question, and the wording was
   the whole of the problem.
3. **Parentage is explicit only.** It is recorded by the code that creates the
   child. A conversation started with "New conversation" is a root even if
   another chat was open at the time. Inferring parentage from whatever was
   active would nest unrelated conversations, and a wrong tree is worse than no
   tree.
4. **Layout is the family card** (candidate B), not an indented tree. See §5.

## 3. Data model

One new table, for the only relationship that is not already recorded:

```sql
CREATE TABLE IF NOT EXISTS chat_subagents (
    id           INTEGER PRIMARY KEY,
    chat_id      TEXT NOT NULL,        -- the parent chat
    tool_use_id  TEXT NOT NULL,        -- the transcript's tool_use id
    agent_type   TEXT,                 -- "code-review", "general-purpose", …
    description  TEXT,                 -- the Task call's own short description
    status       TEXT NOT NULL,        -- 'running' | 'done'
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    UNIQUE(chat_id, tool_use_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_subagents_chat ON chat_subagents(chat_id);
```

`UNIQUE(chat_id, tool_use_id)` with `INSERT OR IGNORE` is the idempotency rule,
copied from `generated_images`: re-reading a transcript is a no-op rather than a
duplicate. The transcript is the source of truth and may be re-scanned for
reasons that have nothing to do with this feature.

**Nothing else changes shape.** `chats.parent_chat_id` keeps meaning voice
handoff. `orchestrator_members` and `orchestrator_tasks` keep meaning
orchestration. They are not migrated into a generic relations table: an
orchestrator *coordinates* its members, which is not parentage, and a generic
table would flatten that distinction for presentation gain. Both already work
and already have tests.

## 4. Capture

Three sources. Only one needs new code.

### 4.1 Subagents — new

Task-tool subagents run **inside the CLI process**, so the console cannot hook
their spawn. `CLAUDE.md` §0 is the reason: the console spawns the `claude` CLI
and varies its parameters; what the CLI does internally is not the console's to
intercept. They are observable in exactly one place — the transcript, as
`tool_use` blocks whose `name` is `Task`.

Capture is therefore a **post-turn scan of the newly-read transcript bytes**,
recorded in `routes/chats.py` immediately beside the existing
`generated_image_record` call, under the rule that comment already states:

> Deliberately after `messages_batch`: a failure here must never risk losing
> the turn's actual transcript write, which is the primary artifact — this is a
> secondary index.

That ordering is binding for this feature too. A subagent row is a convenience;
the transcript is the artifact.

**Status** comes from pairing, not from a second source: a `Task` `tool_use`
with a matching `tool_result` is `done`; one without is `running`.
`transcripts.py` already performs exactly this pairing for `AskUserQuestion`
(`pending_question`, and the `question_ids` registry in `read_turns`), so this
reuses a proven shape rather than inventing one.

**A subagent that never completes** stays `running` for ever if left alone —
the CLI can die without writing a `tool_result`. §7 covers that.

### 4.2 Orchestrator — no capture, and no opinion

`orchestrator_members(orchestrator_id, chat_id)` and `orchestrator_tasks`
already hold the relation. This design reads them and renders them; it writes
nothing to them and changes nothing about them.

**Whether an orchestrator task is itself a real chat is not decided here.** It
is decided by `2026-09-20-orchestrator-chats-all-the-way-down-design.md`, where
a task being a real chat is the mechanism rather than the presentation: usage,
transcripts, resume, standby and the generated-images gallery are inherited
precisely because a task goes through `routes/chats.py`'s `_start_turn`, which
is the only place images are ever recorded. Nothing in this design depends on
the answer either way — §9's `children` array already carries both a `chat`
kind and a `subagent` kind, so a task that is a real chat renders as the
former and needs nothing added here.

### 4.3 Voice handoff — no capture

`chats.parent_chat_id` already holds it, and `handle_chats_list`
(`routes/chats.py:388`) already sends it to the client.

Voice children are currently hidden from the sidebar as ephemeral
(`chat-list.js:910`, `chats.filter(c => !c.is_temporary)`). Nesting them
means reversing that decision: they become visible **inside their parent's
card** and nowhere else, so they never again appear as orphan rows in the flat
list. That is the only behaviour change to an existing surface.

## 5. Rendering — the family card

A chat with no children renders exactly as it does today. A chat **with**
children becomes a bordered card: the parent row at the top, an accent rail down
the left edge, children full-width inside it.

```
┌─ Delegation page redesign              3 spawned │
│  ────────────────────────────────────────────────│
│  ▫ subagent · code-review                   done │
│  ▫ subagent · test-writer                running │
│  • Voice brainstorm                        voice │
└──────────────────────────────────────────────────┘
```

Why this and not an indented tree (candidate A):

- **Children keep full width.** Indentation costs horizontal space in a 380px
  sidebar, and subagent names are long. At a wide fan-out an indented tree
  truncates exactly the text that identifies which subagent is which.
- **The card is one draggable unit** — see §6, which is the decisive reason.
- **A family has a visible start and end**, which a run of indented rows does
  not.

The cost, accepted: **only one level of nesting is expressible.** A subagent
that spawns its own subagent has nowhere to go. Today none can — Task-tool
subagents are leaves as far as the transcript shows — so this is a real
limitation with no current instances. If nested subagents appear, the card grows
a depth badge rather than a second indent level.

A display-only subagent node is visually distinct from an openable chat: a
square marker rather than a status dot, muted text, no click target, no context
menu.

## 6. Ordering — the constraint that chose the layout

`commitOrder` in `web/assets/chat-list.js` builds the order it persists by
**walking the DOM** and sending the resulting id list to `PUT /api/chats/order`.
This is not incidental; it is how manual placement has always been stored, and
it is why the layout decision and the ordering decision are the same decision.

Under the family card:

- **Only roots participate in manual ordering.** A card is one row as far as
  drag is concerned; dragging it moves the whole family.
- **Children are never independently draggable** and are excluded from the id
  list `commitOrder` sends, so stored `position` stays a flat list of roots.
- Children sort inside their card by `started_at`, ascending. Spawn order is
  the only order that means anything for them.

The exclusion mechanism already exists in the file: rendered rows carry a
`data-floated` marker and `commitOrder` skips them, added 2026-09-20 for the
active-conversation float. Child rows reuse that mechanism rather than
introducing a second one.

An indented tree would have put children into the DOM run that `commitOrder`
walks, which is precisely the shape that wrote temporary positions into
permanent `position` values the last time this code was touched.

## 7. Volume, collapse and retention

A supervisor fan-out can add many subagent nodes in one turn. Unmanaged, the
sidebar degrades worst in exactly the sessions where the tree is most useful.

- **Cards collapse by default above a threshold.** A card showing more than
  **five** children renders collapsed, with `N children ▸`. Five is chosen to
  show a typical two-or-three-subagent turn in full while refusing to let a
  ten-way fan-out push every other conversation off screen.
- **Collapse state is per-browser**, like `unreadIds` and `endedIds` already
  are in `chat-list.js`. It is a viewing preference, not a property of the
  conversation, and does not belong in the database.
- **Retention:** subagent rows are deleted with their parent chat, in the same
  transaction as the chat's other owned rows. There is no independent expiry —
  a subagent row is small, and a chat's own lifetime is the only lifetime that
  means anything for it.
- **Stuck `running` rows:** a subagent whose `tool_result` never arrives is
  shown as `running` until its parent chat is deleted. It is displayed with its
  `started_at` age so a stale one is visible as stale rather than as active
  work. No timeout sweeper: inventing a "probably dead" threshold would report a
  guess as a fact, and the age already carries the truth.

## 8. Composition

One function builds the tree, and it is the unit worth testing hardest:

```
build_chat_tree(chats, subagents, orchestrator_members, ...) -> list[TreeNode]
```

It is a **pure function over already-fetched rows** — no database access, no
DOM, no network. That is deliberate: the three sources have different shapes and
different failure modes, and a composer that fetched its own inputs could not be
tested without standing up all three.

Rules it enforces:

- A chat appears exactly once in the output. A chat that is both an orchestrator
  member and a voice child of a different parent resolves to **one** parent, by
  precedence: orchestrator member → voice parent → root.
- A child whose parent is absent from the list (archived, deleted, filtered by
  search) renders as a **root**, never dropped. Losing a conversation because
  its parent was filtered would be a worse bug than showing it unnested.
- Cycles are impossible by construction for subagents (a subagent is not a
  chat), but the composer still refuses to recurse more than one level, so a
  malformed `parent_chat_id` cannot hang the sidebar.

## 9. API

`GET /api/chats` gains one field per chat:

```json
"children": [
  {"kind": "subagent", "agent_type": "code-review", "description": "...",
   "status": "done", "started_at": "..."},
  {"kind": "chat", "id": "...", "title": "Voice brainstorm", "relation": "voice"}
]
```

`children` is always present and may be empty. The client never infers a
relation; it renders what the server composed. Search and filtering continue to
operate on the flat chat list, with §8's orphan rule covering a parent filtered
out by a query.

## 10. Testing

- **Composer** (`build_chat_tree`): pure-function tests, no DOM, no database.
  One chat appearing once; each precedence rule in §8; the orphan rule; the
  depth cap. Fixtures use the real relationship shapes, not invented ones.
- **Capture**: a transcript fixture containing a `Task` `tool_use` with and
  without a matching `tool_result`, asserting `running` / `done`; a re-scan of
  the same transcript asserting no duplicate row.
- **Ordering**: that `commitOrder`'s id list contains only roots. This is the
  regression that would silently corrupt stored `position`, so it is asserted
  on the id list itself, not on the rendered markup.
- **Front end**: source-level assertions in the style of
  `tests/test_frontend.py`, plus behavioural tests executed under `node` where
  the logic is a pure function — `node` is installed (v24.19.0), and
  `chat-list.js` has no top-level imports, so it can be imported directly.

## 10a. Open — needs Pedro's decision, not adopted here

**Proposed: orchestrator task chats are hidden from the flat root list and
render only inside their run's card.**

Relayed to this session on 2026-09-21 by the orchestrator session as a ruling
from Pedro, which reached them through a third session rather than from him
directly. They said so themselves and advised confirming first-hand. **It is
therefore recorded here as proposed and is not applied**: it hides
conversations from the sidebar, which is the operator's call and not something
to enact on a second-hand relay.

The argument for it, recorded because it is a good one. §4.3 already does
exactly this for voice children — they become visible inside their parent's
card and nowhere else — so extending the same rule to orchestrator members
invents nothing, and the composer's precedence rule (§8) is already the thing
that would implement it: the hiding predicate becomes "not a root" rather than
"not present". It also solves a problem the orchestrator design cannot solve
alone: a task chat that asks a question lands in `GET /api/orchestrator`'s
`waiting` bucket and would otherwise mark a sidebar row that no longer exists,
so the summons disappears silently. A card gives it somewhere to be drawn.

If adopted, the edit is to §4.3 and §8, and nothing in §3–§7 changes.

**Inherited hazard, verified 2026-09-21 rather than taken on trust.** Any
surface consuming the `waiting` bucket — including this card — must re-apply
the filter itself, because it is **client-side**, in `chat-list.js`'s
`setSupervisor`, not in the endpoint:

```js
entry.kind === 'chat' && entry.id && entry.reason !== 'done'
```

The bucket carries three reasons: `asks`, `blocked` and `done`. Only the first
two want a person. It also carries CLI/terminal sessions, whose ids are session
ids. That function's own comment records the measurement: against the live
database on 2026-09-21, **61 of 64 conversations were in the bucket and 54 of
them were `done`** — so consuming it whole marks nearly every row and
reproduces, in a new colour, the noise that filter exists to remove.

Also open, and unverified by anyone so far: whether `chat-list.js`'s existing
`is_temporary` filter (line 910) is the right hook for "not a root", or whether
that needs its own predicate. Whoever reaches it first should read
`setSupervisor` before changing it.

## 11. Out of scope

- Subagents as openable conversations (decision 2).
- Inferred parentage (decision 3).
- Nesting deeper than one level (§5).
- Re-parenting or detaching by hand. Parentage is recorded by the creator; an
  editing surface is a separate feature and is not needed until parentage is
  ever wrong, which explicit-only capture is designed to prevent.
- Migrating orchestrator or voice relations into a shared table (§3).
