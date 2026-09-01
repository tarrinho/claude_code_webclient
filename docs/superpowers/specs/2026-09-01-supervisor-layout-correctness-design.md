# Supervisor layout correctness — making the panels reachable

The supervisor page has four panels. One of them has never been on screen, and
a second renders as a 168px sliver at the wrong edge. Both are markup faults,
not styling opinions: the page ships a stray `</div>` that ejects the Task
Detail panel from the layout container entirely.

This is sub-project **A** of a five-part usability programme (see *Roadmap*).
It is deliberately the smallest of the five, and it blocks the other four.

## Problem

### The Task Detail panel is unreachable

`#panel-right` is a **sibling of `<body>`**, not a child of `#layout`. Measured
headlessly at 1440×900 (chromium, real markup and CSS, app script removed):

```
#layout       = 0,44    1440x769
#panel-left   = 0,44     320x769
#panel-center = 320,44   952x769
#panel-bottom = 1272,44  168x200
#panel-right  = 0,813    320x134     <- parent = BODY
viewport      = 1440x813
```

`#panel-right` starts at `y=813` in an 813px viewport, and `body` sets
`overflow:hidden`, so it can never be scrolled into view. A div-balance pass
over the body markup (comments stripped) ends at depth **-1**, with
`#panel-right` opening at depth 1 where the other panels open at depth 2.

The cost is a whole feature. `renderTaskDetail()` (`web/supervisor.js:664`)
builds the richest view in the application — id, title, status, progress, model,
description, dependencies, timeline, and the **full untruncated `task.result`**.
Clicking a node in the task tree calls `selectTask` → `renderTaskDetail`, which
writes correct HTML into an element no user can see. The interaction reads as a
no-op.

This also contradicts an assumption in
`2026-09-01-supervisor-ux-design.md`, which lists "results hard to find" as
*partly solved* because "the result is in the chat (3000 chars) and in full in
`supervisor_tasks.result`, which the detail panel renders". The panel does
render it. Nobody can read it. The chat's 3000-character cap is therefore a hard
ceiling today, not a summary with a fallback.

### The Event Log is a sliver at the wrong edge

`#panel-bottom` is a child of `#layout`, which is `display:flex` in the row
direction, so its `height:200px` yields a 168×200 column pinned at `x=1272`.
Three independent signals say it was meant to span the full width:

- `border-top` rather than `border-left`
- `body.max-bottom #panel-bottom { position:absolute; bottom:0; left:0; right:0 }`
- the drag handler's `resizing === "bottom"` branch computes
  `window.innerHeight - e.clientY`, which is only meaningful for a bar anchored
  to the viewport bottom

### The log's resize handle does not exist

`onResizeMove` has a complete `resizing === "bottom"` branch
(`web/supervisor.js:1121`) and `reinitResizeHandles` already special-cases
`type === "bottom"` to set a `row-resize` cursor. No element carries
`data-resize="bottom"`, so the whole path is dead code.

### The event log bleeds between supervisors

`addLogEntry()` only appends to the DOM, and `selectSupervisor()` never clears
it. Switching supervisors leaves the previous run's events on screen, with no
divider, interleaved with the new one's.

### Drags die when the pointer leaves the handle

`reinitResizeHandles` attaches `mousemove`/`mouseup` to the 5px handle itself
rather than to `document`, so any drag faster than the pointer can stay inside
the strip silently stops. Pre-existing for the left and centre handles; it is
included here because A makes the bottom bar draggable for the first time, and
a control that fails on first use is not a delivered control.

## Decisions

| Question | Decision |
|---|---|
| Where does the Event Log live? | Full-width bottom bar, under all three columns |
| How is the layout restructured? | New `#shell` flex column wrapping `#layout` + `#panel-bottom` |
| How is the log bleed fixed? | Clear the log on supervisor switch |
| Is the bottom resize handle added? | Yes — the JS already exists |
| Is the drag-listener fix included? | Yes |

## Design

### 1. Markup — `web/supervisor.html`

```
#shell                                    (new; flex column)
 ├─ #layout                               (flex row)
 │   ├─ #panel-left
 │   ├─ #panel-center
 │   └─ #panel-right                      (moved in)
 └─ #panel-bottom                         (full width)
      └─ .resize-handle.horizontal[data-resize="bottom"]   (new)
```

The stray `</div>` after `#panel-bottom` is removed. `#panel-right` moves to be
the third child of `#layout`, which is where every `#panel-right` CSS rule and
the `resizing === "right"` branch already assume it is.

### 2. CSS — the in-file `<style>` block

```css
#shell  { display:flex; flex-direction:column;
          height:calc(100vh - 44px); overflow:hidden; }
#layout { display:flex; flex:1; min-height:0; overflow:hidden; }
          /* was: height:calc(100vh - 44px) */

.resize-handle.horizontal {
  height:5px; width:auto; left:0; right:0; top:-3px; bottom:auto;
  cursor:row-resize;
}
```

Two constraints are load-bearing and easy to violate later:

- **`#shell` must not become a containing block.** No `position`, `transform`,
  `filter`, or `contain`. All four `body.max-*` rules position with
  `position:absolute; top:44px` resolved against the viewport; giving `#shell` a
  position silently breaks every maximize button while leaving the page looking
  correct at rest.
- **`min-height:0` on `#layout` is required.** A flex child with
  `overflow:hidden` will not shrink below its content without it, and the log
  would be pushed off-screen again — the same defect by a different route.

`#panel-bottom` keeps `height:200px`, `min-height:80px`, `flex-shrink:0` and
`border-top` unchanged; in a column flex parent these now mean what they say.

### 3. JS — `web/supervisor.js`

- `selectSupervisor()` empties `#event-log` and sets `eventLog.length = 0`
  before `showActiveSupervisor()`, so the log always describes the supervisor on
  screen. This mirrors how `chatMessages` is already replaced per supervisor.
- `reinitResizeHandles()` attaches `mousemove`/`mouseup` to `document` instead
  of to the handle, and removes them on `onResizeEnd`.
- Nothing else. The bottom handle needs no new JS.

## What is deliberately out of scope

- **All colour and token work.** `supervisor.html` links no stylesheet and
  carries 930 lines of inline CSS with 181 hard-coded hex colours and zero
  `var(--…)` uses, while `/assets/styles.css` defines a dark-default token set
  with an `html[data-theme="light"]` override that `index.html` links.
  **The light palette here is deliberate**, not an oversight:
  `2026-08-30-supervisor-orchestration-design.md` §*UI Design Notes* specifies a
  "clean blue light palette" matching `web/assets/supervisor-mockup.html`. So
  sub-project **D** is not "make this page dark like the others" — it is
  tokenising the inline CSS, keeping the specified light palette as the light
  theme, and adding a dark counterpart plus a toggle so
  `2026-09-01-supervisor-ux-design.md`'s "both themes" verification can pass.
  None of that is touched here; every CSS change above is geometry-only.
- **Responsive behaviour.** The page has zero `@media` rules. Breakpoints and
  the collapsible detail drawer are sub-project **C**, which depends on this one.
- **Live task output and Retry.** Owned by
  `2026-09-01-supervisor-ux-design.md`. That design's §3 feeds a live section
  into the detail panel; A is its prerequisite, and the two must not be
  implemented as one change.
- **The `center-delta` handle resizing both side panels at once.** Observed,
  odd, and left alone.

## Roadmap

A is first because the other four are unverifiable or meaningless without it.

| | Sub-project | Depends on |
|---|---|---|
| **A** | Layout correctness *(this document)* | — |
| B | Tier 1 affordances: left-column headers, shortcut legend, expandable results, composer hint | A |
| C | Responsive breakpoints + collapsible detail drawer | A |
| D | Tokenise the inline CSS; keep the specified clean-blue light palette as the light theme, add a dark counterpart and a toggle | C |
| E | Render plans from `metadata.kind` instead of the `<<PLAN` sentinel; task ↔ chat linkage | — |

## Relationship to prior designs

The as-built page diverges from
`2026-08-30-supervisor-orchestration-design.md` §*Frontend Architecture*, which
is the foundational design. Recorded here so a later reader can tell intent from
drift:

| Orchestration spec | As built | Status |
|---|---|---|
| Bottom panel = **Supervisor Chat** | Bottom = **Event Log** | Divergence, accepted; the Event Log is not in that spec at all |
| Centre panel = the selected **subtask's conversation** (its chat + SSE) | Centre = **Supervisor Chat** | Divergence; the per-subtask conversation view **does not exist** — a real gap, unclaimed by any spec |
| **Status bar** between centre and bottom (`3 running · 1 pending · 2 done · 35%`) | Absent; only a thin `#overall-progress` bar | Gap — folded into sub-project **B** |
| Status dots: green done, **blue running**, amber pending | `running` = amber, `ready` = blue, `pending` = grey | Minor colour divergence — sub-project **D** |
| `+`/`−` size buttons in every panel header | Not implemented (minimize/maximize only) | Gap, low value; not claimed |
| Separate `supervisor.css` + `supervisor-task-tree.js` under `/assets/` | One 930-line inline `<style>`; `supervisor.js` served from `/supervisor.js` | Divergence; the extraction is a precondition for **D** |
| Detail panel "toggleable" | Present but **unreachable** | Fixed by this document |

**A is safe against the unresolved divergence.** The `#shell` column wrapping
`#layout` plus a full-width bottom bar is the structure both layouts require —
the orchestration spec puts Supervisor Chat in that slot, this design puts the
Event Log there. Whichever eventually occupies it, the containing structure is
the same, so A does not need the divergence settled first.

Two items above are genuine feature gaps rather than layout faults — the
per-subtask conversation view and the status bar. Neither is in scope here. The
conversation view is the larger of the two and is not claimed by any current
spec; it needs its own design cycle before it can be planned.

## Files

| File | Change |
|---|---|
| `web/supervisor.html` | `#shell` wrapper; `#panel-right` moved into `#layout`; stray `</div>` removed; bottom resize handle; two CSS rules |
| `web/supervisor.js` | log cleared in `selectSupervisor()`; drag listeners moved to `document` |
| `tests/test_qa_supervisor_layout.py` | new — geometry and nesting assertions |

The script tag needs its cache-buster bumped (`supervisor.js?v=4` → `v=5`), and
`test_qa_supervisor_page_restore.py::test_the_script_tag_was_cache_busted`
asserts `>= 4`, so it keeps passing.

## Verification

The repo drives UI assertions through headless chromium already — `run_page` in
`tests/test_qa_supervisor_page_restore.py` loads a probe page and reads the
result out of `document.title`. The new test follows that pattern rather than
introducing a second harness.

1. **Nesting.** Div-balance over the body markup ends at depth 0, and
   `#panel-right.parentElement.id === "layout"`.
2. **Geometry**, at 1440×900 and at 1024×768: all four panels have non-zero
   size and lie fully inside the viewport; `#panel-bottom` width equals
   `#shell` width and its bottom edge equals the viewport bottom;
   `#panel-right` sits to the right of `#panel-center` on the same row.
3. **Maximize.** For each of `max-left`, `max-center`, `max-right`,
   `max-bottom` on `body`, the maximized panel covers the area below the topbar.
   This is the assertion that would catch `#shell` being given a position.
4. **Regression guard.** A test that fails if `#panel-bottom` is a child of
   `#layout`, so the original fault cannot come back unnoticed.
5. **Log clearing.** Selecting a different supervisor leaves `#event-log`
   empty and `eventLog.length === 0`.
6. **Existing suite green:** `python3 -m pytest tests/ -k superv -q` (404
   passing before this change) and `ruff check .` clean.
7. **Mutation-check the new suite before claiming it,** and confirm each
   mutation actually altered the file before drawing a conclusion. Mutations to
   make: leave `#panel-right` outside `#layout`; drop `min-height:0` from
   `#layout`; give `#shell` `position:relative`; skip the log clear.
8. **Live, in a browser.** Click a task and read its full result in the detail
   panel; drag the log's top edge and confirm it resizes and that a fast drag
   off the handle keeps working. A green suite does not demonstrate a panel
   being visible to a person.

## Coordination — before touching anything

`web/supervisor.html` and `web/supervisor.js` are both dirty with peer work, and
both files are edited here. Sessions cweb1–cweb4 share this working tree and two
whole-file commit accidents have already happened between careful sessions.

So: `ListAgents`, then message each live peer naming the exact regions — the
`#layout` / `#panel-bottom` / `#panel-right` block in the markup, the
`<style>` rules for `#layout`, `selectSupervisor()`, and
`reinitResizeHandles()` — and wait for acknowledgement before editing.

Never `git stash` in this tree. Prefer `git commit -- <paths>`; a pathspec
protects against a contaminated index and a bare commit against a contaminated
working tree, and **neither is safe when both are contaminated**. Check
`git diff --cached --stat` against the size of the change intended before every
commit.
