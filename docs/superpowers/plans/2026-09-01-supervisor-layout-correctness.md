# Supervisor Layout Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the supervisor page's Task Detail panel reachable and give the Event Log the full-width bottom bar its CSS already assumes.

**Architecture:** `web/supervisor.html` carries a stray `</div>` that closes `#layout` before `#panel-right` opens, ejecting the Task Detail panel to become a sibling of `<body>` where it renders off-screen and cannot be scrolled to. The fix removes that closer, wraps the layout row and the bottom bar in a new `#shell` flex column, and relocates `#panel-bottom` out of the horizontal row. Two small JS changes follow: clear the event log when switching supervisors, and move drag listeners from the 5px handle to `document`.

**Tech Stack:** Vanilla JS (no framework, no build step), inline `<style>` in `supervisor.html`, FastAPI static serving, pytest + headless chromium via `subprocess` for UI assertions.

**Spec:** `docs/superpowers/specs/2026-09-01-supervisor-layout-correctness-design.md`

## Global Constraints

- **Use targeted string-replacement edits only on `web/supervisor.html` and `web/supervisor.js`. Never a whole-file write.** The 0.9.5 version strings at `supervisor.html:6`, `supervisor.html:947` and `supervisor.js:1452` are committed content at HEAD (`ede43a1`), not anyone's in-flight work — but a whole-file write from a stale read would still revert them and break `tests/test_qa_version_consistency.py`, and this tree acquires an in-flight bump every few hours. Hygiene applies regardless of who is mid-release.
- **Do not edit the version strings.** They are not in scope. If a version string changes, the edit was wrong — revert it. Check with `git diff -- <file> | grep -c '^[+-].*0\.9\.'`, anchored to `+`/`-`: an unanchored `grep -c '0\.9\.'` also counts *context* lines, so it reports a match whenever an edit lands within three lines of `supervisor.html:947` — a false alarm that invites "fixing" something that never changed. The authoritative check is `diff <(git show HEAD:web/supervisor.html | grep '0\.9\.') <(grep '0\.9\.' web/supervisor.html)`, which compares content and ignores line movement.
- **Coordination:** cleared. cweb5 confirmed they hold nothing uncommitted in either file, and `git status --porcelain web/supervisor.html web/supervisor.js` was empty at plan time, with `git diff HEAD --stat` on both files empty too. Re-check both before the first edit anyway — three readings of this tree went stale mid-session, in both directions.
- **Known pre-existing failure, not caused by this plan:** `tests/test_qa_supervisor_ux_shortcuts.py` fails 27 of 28 with 180s timeouts. It is cweb5's harness, which drives the real page through raw `chromium --headless --dump-dom` and depends on Chromium exiting by itself; Chromium now hangs when `supervisor.js` is loaded. cweb5 verified the page is byte-identical to when those tests passed and is migrating the harness to the repo's playwright fixture. **Do not attribute those 27 failures to this change, and do not try to fix them here.** Record the count before the first edit so the comparison is available.
- **Commit with an explicit pathspec** limited to this plan's own files, so cweb5's uncommitted work cannot be swept in. Never `git commit -a`. Never `git stash` in this tree.
- **Before every commit** run `git diff --cached --stat` and confirm it lists only the files that task names.
- `#shell` must not create a containing block: no `position`, `transform`, `filter`, or `contain`. All four `body.max-*` rules use `position:absolute; top:44px` resolved against the viewport.
- Scope is geometry only. No colour, token, or responsive work — those are sub-projects C and D.
- Interpreter: `python -m pytest` (pytest 9.0.3, chromium reachable). The new tests shell out to `chromium` and do not need selenium.

---

### Task 1: A failing test that proves the panel is unreachable

The test comes first and must fail for the right reason. It asserts two things a naive DOM check would miss: that `#panel-right` is a *child of `#layout`*, and that it is *inside the viewport*. "Present in the DOM at y=813 in an 813px viewport" satisfies `is_visible()`-style checks while being invisible to a person, so geometry is asserted, not presence.

**Files:**
- Create: `tests/test_qa_supervisor_layout.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `probe_geometry(width:int=1440, height:int=900, body_class:str="") -> dict[str,str]` — loads the real `supervisor.html` with its `<script>` tag stripped, returns a dict parsed from `document.title`, keyed by selector, values `"x,y WxH"`, plus `"panel-right.parent"` and `"viewport"`. Tasks 3 and 7 reuse it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qa_supervisor_layout.py`:

```python
"""The supervisor page's four panels must all be reachable.

`web/supervisor.html` shipped a stray `</div>` that closed `#layout` before
`#panel-right` opened, so the Task Detail panel became a sibling of <body>.
At 1440x900 it rendered at y=813 in an 813px viewport, and `body` sets
`overflow:hidden`, so it could never be scrolled to. `renderTaskDetail()`
wrote correct HTML into an element no user could see, and clicking a task in
the tree read as a no-op.

Geometry is asserted rather than DOM presence: "present but at y=813 in an
813px viewport" would satisfy a naive visibility check while being invisible
to a person. The div-balance assertion is the regression guard -- it fails on
the original fault directly, independent of any rendering.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR_HTML = REPO / "web" / "supervisor.html"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")

PANELS = ("#panel-left", "#panel-center", "#panel-right", "#panel-bottom")


def body_markup() -> str:
    """The <body> of supervisor.html with HTML comments stripped.

    Comments are removed because they contain tags -- a commented-out </div>
    would otherwise be counted by the balance check below.
    """
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", html[html.index("<body"):], flags=re.S)


def div_depth_trace() -> tuple[int, dict[str, int]]:
    """Return (final depth, {panel id: depth at which it opens}).

    Final depth 0 means balanced. A panel opening at a different depth from
    its siblings is the ejection this module exists to catch.
    """
    depth = 0
    opens: dict[str, int] = {}
    for match in re.finditer(r"<(/?)div\b([^>]*)>", body_markup()):
        if match.group(1):
            depth -= 1
            continue
        depth += 1
        found = re.search(r'id="([^"]+)"', match.group(2))
        if found:
            opens[found.group(1)] = depth
    return depth, opens


def probe_geometry(width: int = 1440, height: int = 900,
                   body_class: str = "") -> dict[str, str]:
    """Render the real page headlessly and report each panel's box.

    The app script is stripped, and that is load-bearing rather than tidiness.
    Chromium does not exit when supervisor.js is loaded under --dump-dom -- it
    hangs until the timeout, which is what has `test_qa_supervisor_ux_shortcuts`
    failing 27 of 28 at the time of writing. Measured by cweb5:

        supervisor.html alone, no supervisor.js    rc=0,   1s
        supervisor.html + supervisor.js, no stubs  rc=124, hangs

    Stripping it also removes API fetches that would fail with no server, and
    every assertion here is decided by markup and CSS alone. If this probe ever
    starts timing out, check whether the script tag is being stripped before
    looking anywhere else.

    Results come back through document.title because --dump-dom is the only
    channel headless chromium offers without a debugging port.
    """
    html = SUPERVISOR_HTML.read_text(encoding="utf-8")
    html = re.sub(r'<script src="supervisor\.js[^"]*"></script>', "", html)
    if body_class:
        html = html.replace("<body>", f'<body class="{body_class}">', 1)
    probe = """
<script>
window.addEventListener("load", function () {
  var out = {};
  ["#layout", "#shell", "#panel-left", "#panel-center",
   "#panel-right", "#panel-bottom"].forEach(function (sel) {
    var el = document.querySelector(sel);
    if (!el) { out[sel] = "MISSING"; return; }
    var r = el.getBoundingClientRect();
    out[sel] = [Math.round(r.x), Math.round(r.y),
                Math.round(r.width), Math.round(r.height)].join(",");
  });
  var pr = document.querySelector("#panel-right");
  out["panel-right.parent"] = pr
    ? (pr.parentElement.id || pr.parentElement.tagName) : "MISSING";
  out["viewport"] = window.innerWidth + "," + window.innerHeight;
  document.title = JSON.stringify(out);
});
</script></body>"""
    html = html.replace("</body>", probe, 1)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             f"--window-size={width},{height}",
             "--virtual-time-budget=4000", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>(.*?)</title>", result.stdout, re.S)
    if not match:
        raise AssertionError("probe produced no title; chromium output: "
                             + result.stdout[:400])
    return json.loads(match.group(1))


def box(geometry: dict[str, str], selector: str) -> tuple[int, int, int, int]:
    raw = geometry[selector]
    if raw == "MISSING":
        raise AssertionError(f"{selector} is not in the document")
    x, y, w, h = (int(v) for v in raw.split(","))
    return x, y, w, h


class MarkupNestingTests(unittest.TestCase):
    """Decided by the markup alone, so it runs without a browser."""

    def test_the_body_divs_balance(self):
        """The original fault was a stray closer; -1 is its signature."""
        depth, _ = div_depth_trace()
        self.assertEqual(depth, 0,
                         "unbalanced <div> nesting in supervisor.html body")

    def test_panel_right_is_a_child_of_layout(self):
        """The ejection itself. #panel-right must sit with its siblings."""
        _, opens = div_depth_trace()
        self.assertIn("panel-right", opens)
        self.assertEqual(
            opens["panel-right"], opens["panel-left"],
            "#panel-right opens at a different depth from #panel-left, so it "
            "is not a sibling of the other panels",
        )


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class PanelGeometryTests(unittest.TestCase):
    """Every panel must be on screen, not merely in the document."""

    @classmethod
    def setUpClass(cls):
        cls.geo = probe_geometry()

    def test_panel_right_reports_layout_as_its_parent(self):
        self.assertEqual(self.geo["panel-right.parent"], "layout")

    def test_every_panel_is_inside_the_viewport(self):
        """y=813 in an 813px viewport is the bug, and has non-zero size."""
        self._assert_all_on_screen(self.geo)

    def test_every_panel_is_inside_a_smaller_viewport(self):
        """1024x768. The panels have fixed 320px side widths and 300px centre
        minimum, so a narrower viewport is where a wrong flex basis shows up
        as an overflow that 1440px hides. Not a responsive test -- that is
        sub-project C; this only asserts nothing escapes the viewport.
        """
        self._assert_all_on_screen(probe_geometry(width=1024, height=768))

    def _assert_all_on_screen(self, geo: dict[str, str]):
        vw, vh = (int(v) for v in geo["viewport"].split(","))
        for selector in PANELS:
            with self.subTest(panel=selector, viewport=f"{vw}x{vh}"):
                x, y, w, h = box(geo, selector)
                self.assertGreater(w, 0, f"{selector} has zero width")
                self.assertGreater(h, 0, f"{selector} has zero height")
                self.assertLess(y, vh,
                                f"{selector} starts below the viewport")
                self.assertLessEqual(y + h, vh + 1,
                                     f"{selector} extends past the viewport")
                self.assertLessEqual(x + w, vw + 1,
                                     f"{selector} extends past the right edge")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails, and for the right reason**

Run: `python -m pytest tests/test_qa_supervisor_layout.py -v`

Expected: 4 failures, specifically —
- `test_the_body_divs_balance`: `-1 != 0`
- `test_panel_right_is_a_child_of_layout`: `1 != 2`
- `test_panel_right_reports_layout_as_its_parent`: `'BODY' != 'layout'`
- `test_every_panel_is_inside_the_viewport`: subTest `#panel-right` — starts below the viewport

If `test_every_panel_is_inside_the_viewport` fails on `#panel-bottom` instead, the probe is wrong, not the page — fix the probe before continuing.

- [ ] **Step 3: Confirm no test silently skipped**

Run: `python -m pytest tests/test_qa_supervisor_layout.py -q 2>&1 | tail -3`

Expected: the summary reports failures and **`skipped: 0`**. A skip here means chromium was not found and the geometry class did not run at all, which would let the rest of this plan pass vacuously.

- [ ] **Step 4: Commit the failing test**

```bash
git add tests/test_qa_supervisor_layout.py
git diff --cached --stat   # must list ONLY tests/test_qa_supervisor_layout.py
git commit -- tests/test_qa_supervisor_layout.py -m "test: assert supervisor panels are reachable (currently failing)"
```

---

### Task 2: Remove the stray closer so `#panel-right` rejoins `#layout`

The smallest change that fixes the reported defect, kept as its own task because it is independently verifiable and a reviewer could accept it while rejecting the `#shell` restructure that follows.

**Files:**
- Modify: `web/supervisor.html:1029` (delete one line)
- Test: `tests/test_qa_supervisor_layout.py`

**Interfaces:**
- Consumes: `probe_geometry`, `div_depth_trace` from Task 1.
- Produces: markup in which `#panel-right` is the fourth child of `#layout`. Task 3 relocates `#panel-bottom` out of it.

Current markup, `web/supervisor.html:1025-1032`:

```
1025|            <div id="event-log" tabindex="-1"></div>
1026|                <!-- Floating scroll-down button: appears when user scrolls up -->
1027|                <button id="log-scroll-btn" ...>&#8595;</button>
1028|            </div>          <- closes #panel-bottom
1029|        </div>              <- closes #layout, prematurely. THIS ONE.
1030|
1031|        <!-- Right: Detail Panel -->
1032|        <div id="panel-right">
```

Line 1028 closes `#panel-bottom`; 1029 then closes `#layout`, so `#panel-right` opens outside it and the `</div>` at 1042 has nothing left to close — which is the `-1`.

- [ ] **Step 1: Delete line 1029**

Targeted replacement. Match on the surrounding lines so the edit is unambiguous, and do **not** rewrite the file:

Find:
```
            </div>
        </div>

        <!-- Right: Detail Panel -->
        <div id="panel-right">
```

Replace with:
```
            </div>

        <!-- Right: Detail Panel -->
        <div id="panel-right">
```

- [ ] **Step 2: Confirm cweb5's version strings are untouched**

Run: `git diff -- web/supervisor.html | grep -c '^[+-].*0\.9\.'`

Expected: `0`. Any other number means the edit touched a version line — revert and redo with a narrower match.

- [ ] **Step 3: Run the nesting tests**

Run: `python -m pytest tests/test_qa_supervisor_layout.py::MarkupNestingTests -v`

Expected: both PASS. Depth is now 0 and `#panel-right` opens at the same depth as `#panel-left`.

- [ ] **Step 4: Run the geometry tests**

Run: `python -m pytest tests/test_qa_supervisor_layout.py::PanelGeometryTests -v`

Expected: `test_panel_right_reports_layout_as_its_parent` PASSES. `test_every_panel_is_inside_the_viewport` may still FAIL on `#panel-bottom`, which is Task 3's job — `#panel-bottom` is still a child of the horizontal flex row and renders as a narrow column at the right edge.

- [ ] **Step 5: Confirm the version-consistency baseline still holds**

Run: `python -m pytest tests/test_qa_version_consistency.py -q`

Expected: `6 passed, 5 subtests passed`. This is the guard against half-reverting cweb5's bump.

- [ ] **Step 6: Commit**

```bash
git add web/supervisor.html
git diff --cached --stat   # must list ONLY web/supervisor.html
git commit -- web/supervisor.html -m "fix: stray </div> ejected the Task Detail panel from #layout

#panel-right was a sibling of <body>, rendering at y=813 in an 813px
viewport under body{overflow:hidden}, so it could never be scrolled to.
renderTaskDetail() has been writing correct HTML into an element no user
could see."
```

---

### Task 3: `#shell` wrapper, and the Event Log becomes a full-width bottom bar

`#panel-bottom` is styled as a full-width bar — `border-top`, and `body.max-bottom #panel-bottom` uses `position:absolute; left:0; right:0` — but it is a child of the row, so at 1440px it renders `168x200` at `x=1272`. A column wrapper puts it where its own CSS assumes it is.

**Files:**
- Modify: `web/supervisor.html` — markup 950 (`#layout` open), 1019-1028 (`#panel-bottom` block moves), 1042 (`#layout` close); CSS 46-51
- Test: `tests/test_qa_supervisor_layout.py`

**Interfaces:**
- Consumes: Task 2's corrected nesting.
- Produces: `#shell` as the flex column parent of `#layout` and `#panel-bottom`. Task 4 adds a handle inside `#panel-bottom`.

Target structure:

```
#shell                       (new, flex column)
 ├─ #layout                  (flex row)
 │   ├─ #panel-left
 │   ├─ #panel-center
 │   └─ #panel-right
 └─ #panel-bottom            (full width)
```

- [ ] **Step 1: Open `#shell` before `#layout`**

Find:
```
    <div id="layout">
```
Replace with:
```
    <div id="shell">
    <div id="layout">
```

- [ ] **Step 2: Cut the `#panel-bottom` block**

Delete these lines (currently 1019-1028, immediately before the `<!-- Right: Detail Panel -->` comment):

```
        <div id="panel-bottom">
            <div class="panel-header">
                <button class="panel-btn" data-panel="bottom" title="Minimize">&#9654;</button>
                <h2>Event Log</h2>
                <button class="panel-btn" data-max="bottom" title="Maximize">&#9633;</button>
            </div>
            <div id="event-log" tabindex="-1"></div>
                <!-- Floating scroll-down button: appears when user scrolls up -->
                <button id="log-scroll-btn" class="scroll-down-btn" title="Scroll to bottom" aria-label="Scroll to bottom" type="button">&#8595;</button>
            </div>
```

Note the block as written has its own internal oddity: the `<button>` sits outside `#event-log` but is indented as though inside it, and `</div>` at the end closes `#panel-bottom`. Paste it back verbatim in Step 3 rather than reflowing it — reindenting invites a second nesting bug.

- [ ] **Step 3: Paste it after `#layout` closes, inside `#shell`**

Find (the two closers that now end `#panel-right` and `#layout`):
```
        </div>
    </div>

    <script src="supervisor.js?v=4"></script>
```
Replace with:
```
        </div>
    </div>

        <div id="panel-bottom">
            <div class="panel-header">
                <button class="panel-btn" data-panel="bottom" title="Minimize">&#9654;</button>
                <h2>Event Log</h2>
                <button class="panel-btn" data-max="bottom" title="Maximize">&#9633;</button>
            </div>
            <div id="event-log" tabindex="-1"></div>
                <!-- Floating scroll-down button: appears when user scrolls up -->
                <button id="log-scroll-btn" class="scroll-down-btn" title="Scroll to bottom" aria-label="Scroll to bottom" type="button">&#8595;</button>
            </div>
    </div>

    <script src="supervisor.js?v=4"></script>
```

The final `</div>` closes `#shell`.

- [ ] **Step 4: Add the `#shell` rule and make `#layout` a flex child**

Find (CSS, lines 46-51):
```
        #layout {
            display: flex;
            height: calc(100vh - 44px);
            overflow: hidden;
        }
```
Replace with:
```
        /* #shell owns the page's vertical split: the panel row, then the
           Event Log beneath it. It must NOT be given position, transform,
           filter or contain -- every body.max-* rule below positions with
           `position:absolute; top:44px` resolved against the viewport, and
           making #shell a containing block breaks all four maximize buttons
           while leaving the page looking correct at rest. */
        #shell {
            display: flex;
            flex-direction: column;
            height: calc(100vh - 44px);
            overflow: hidden;
        }
        #layout {
            display: flex;
            /* flex:1 with min-height:0, not a height calc: #panel-bottom is a
               sibling now, so the row takes what is left. min-height:0 is
               load-bearing -- a flex child with overflow:hidden will not
               shrink below its content without it, and the Event Log would be
               pushed off-screen again by a different route. */
            flex: 1;
            min-height: 0;
            overflow: hidden;
        }
```

- [ ] **Step 5: Verify no version string moved**

Run: `git diff -- web/supervisor.html | grep -c '^[+-].*0\.9\.'`

Expected: `0`.

- [ ] **Step 6: Run the full layout suite**

Run: `python -m pytest tests/test_qa_supervisor_layout.py -v`

Expected: all PASS, including every subTest of `test_every_panel_is_inside_the_viewport`.

- [ ] **Step 7: Confirm the bar actually spans the width**

Run:
```bash
python -c "
import sys; sys.path.insert(0, 'tests')
from test_qa_supervisor_layout import probe_geometry, box
g = probe_geometry()
sx, sy, sw, sh = box(g, '#shell')
bx, by, bw, bh = box(g, '#panel-bottom')
vw, vh = (int(v) for v in g['viewport'].split(','))
print('shell        ', sx, sy, sw, sh)
print('panel-bottom ', bx, by, bw, bh)
assert bw == sw, 'bottom bar is not as wide as the shell'
assert bx == 0, 'bottom bar does not start at the left edge'
assert abs((by + bh) - vh) <= 1, 'bottom bar is not at the viewport bottom'
print('OK: full-width bottom bar')
"
```

Expected: `OK: full-width bottom bar`. Before this task the same probe reported `panel-bottom 1272 44 168 200`.

- [ ] **Step 8: Regression guard — assert the fault cannot return**

Add to `tests/test_qa_supervisor_layout.py`, inside `MarkupNestingTests`:

```python
    def test_panel_bottom_is_not_inside_the_layout_row(self):
        """It is styled as a full-width bar (border-top, and max-bottom uses
        left:0/right:0). As a child of the horizontal row it rendered 168x200
        at x=1272. This fails if it is ever moved back.
        """
        _, opens = div_depth_trace()
        self.assertIn("panel-bottom", opens)
        self.assertEqual(
            opens["panel-bottom"], opens["layout"],
            "#panel-bottom must be a sibling of #layout, not a child of it",
        )
```

- [ ] **Step 9: Run it**

Run: `python -m pytest tests/test_qa_supervisor_layout.py::MarkupNestingTests -v`

Expected: 3 PASS.

- [ ] **Step 10: Commit**

```bash
git add web/supervisor.html tests/test_qa_supervisor_layout.py
git diff --cached --stat   # ONLY those two files
git commit -- web/supervisor.html tests/test_qa_supervisor_layout.py -m "fix: Event Log becomes the full-width bottom bar its CSS assumed

Wraps #layout and #panel-bottom in a #shell flex column. #panel-bottom was
a child of the horizontal row, so height:200px yielded 168x200 at x=1272
despite border-top and a max-bottom rule using left:0/right:0."
```

---

### Task 4: Wire the bottom resize handle

`onResizeMove` already has a complete `resizing === "bottom"` branch, and `reinitResizeHandles` already sets a `row-resize` cursor for `type === "bottom"`. No element carries `data-resize="bottom"`, so the whole path is dead code. This task is markup and CSS only.

**Files:**
- Modify: `web/supervisor.html` — add one handle inside `#panel-bottom`; add `.resize-handle.horizontal` after the CSS at 101-114
- Test: manual, plus the existing suite

**Interfaces:**
- Consumes: Task 3's `#panel-bottom` in `#shell`.
- Produces: an element matching `.resize-handle[data-resize="bottom"]`, which Task 5's listener change then applies to.

- [ ] **Step 1: Add the handle as the last child of `#panel-bottom`**

Find:
```
                <button id="log-scroll-btn" class="scroll-down-btn" title="Scroll to bottom" aria-label="Scroll to bottom" type="button">&#8595;</button>
            </div>
    </div>

    <script src="supervisor.js?v=4"></script>
```
Replace with:
```
                <button id="log-scroll-btn" class="scroll-down-btn" title="Scroll to bottom" aria-label="Scroll to bottom" type="button">&#8595;</button>
            <div class="resize-handle horizontal" data-resize="bottom"></div>
            </div>
    </div>

    <script src="supervisor.js?v=4"></script>
```

- [ ] **Step 2: Add the horizontal variant to the CSS**

Find:
```
        .resize-handle:hover, .resize-handle.active {
            background: rgba(59, 130, 246, 0.3);
        }
```
Replace with:
```
        /* The bottom bar's handle runs along its top edge, so width/height and
           the offset axis swap. onResizeMove already implements the "bottom"
           branch and reinitResizeHandles already picks a row-resize cursor for
           it; only this rule and the element were missing. */
        .resize-handle.horizontal {
            width: auto;
            height: 5px;
            left: 0;
            right: 0;
            top: -3px;
            bottom: auto;
            cursor: row-resize;
        }
        .resize-handle:hover, .resize-handle.active {
            background: rgba(59, 130, 246, 0.3);
        }
```

`#panel-bottom` needs no `position` for this: `.resize-handle` is `position:absolute` and `#panel-bottom` is a flex item in `#shell`; the handle resolves against the nearest positioned ancestor, which is fine for a 5px strip at the top edge. If the handle appears in the wrong place, add `position: relative` to `#panel-bottom` — it is a flex item, so that does not disturb the layout, and `#panel-bottom` is not referenced by any `body.max-*` absolute positioning of a *different* element.

- [ ] **Step 3: Verify the handle exists and the version strings did not move**

Run:
```bash
grep -c 'data-resize="bottom"' web/supervisor.html   # expect 1
git diff -- web/supervisor.html | grep -c '^[+-].*0\.9\.'   # expect 0
```

- [ ] **Step 4: Run the layout suite — the handle must not disturb geometry**

Run: `python -m pytest tests/test_qa_supervisor_layout.py -v`

Expected: all PASS. A 5px absolutely-positioned strip must not change any panel's box.

- [ ] **Step 5: Commit**

```bash
git add web/supervisor.html
git diff --cached --stat   # ONLY web/supervisor.html
git commit -- web/supervisor.html -m "feat: wire the Event Log's resize handle

onResizeMove's `bottom` branch and reinitResizeHandles' row-resize cursor
were already implemented; no element carried data-resize=\"bottom\", so the
path was unreachable."
```

---

### Task 5: Move drag listeners to `document`

`reinitResizeHandles` attaches `mousemove`/`mouseup` to the 5px handle itself, so any drag quicker than the pointer can stay inside the strip stops silently. Pre-existing for the left and centre handles; included because Task 4 makes the bottom bar draggable for the first time and a control that fails on first use is not a delivered control.

**Files:**
- Modify: `web/supervisor.js` — `reinitResizeHandles()` (~1142) and `onResizeEnd()` (~1134)

**Interfaces:**
- Consumes: Task 4's handle.
- Produces: `onResizeEnd` detaches the document-level listeners it did not previously own. No signature changes.

- [ ] **Step 1: Move the listeners onto `document`**

Find:
```javascript
        const type = newHandle.dataset.resize;
        resizing = type;
        newHandle.addEventListener("mousemove", onResizeMove);
        newHandle.addEventListener("mouseup", onResizeEnd);
        newHandle.classList.add("active");
```
Replace with:
```javascript
        const type = newHandle.dataset.resize;
        resizing = type;
        // On document, not on the handle. The handle is 5px wide, so a drag
        // faster than the pointer can stay inside it left the strip and the
        // move events stopped arriving -- the drag died mid-gesture with no
        // sign of why. Released in onResizeEnd.
        document.addEventListener("mousemove", onResizeMove);
        document.addEventListener("mouseup", onResizeEnd);
        newHandle.classList.add("active");
```

- [ ] **Step 2: Release them when the drag ends**

Find:
```javascript
  function onResizeEnd() {
    resizing = null;
    $$(".resize-handle").forEach((h) => h.classList.remove("active"));
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }
```
Replace with:
```javascript
  function onResizeEnd() {
    resizing = null;
    // Symmetric with the mousedown handler: these are on document now, so
    // they outlive the gesture unless removed here. Leaving them attached
    // would stack a new pair on every drag.
    document.removeEventListener("mousemove", onResizeMove);
    document.removeEventListener("mouseup", onResizeEnd);
    $$(".resize-handle").forEach((h) => h.classList.remove("active"));
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }
```

- [ ] **Step 3: Confirm the version line is untouched**

Run: `git diff -- web/supervisor.js | grep -c '^[+-].*0\.9\.'`

Expected: `0`. `supervisor.js:1452` is cweb5's `"0.9.5"`.

- [ ] **Step 4: Check the file still parses**

Run: `node --check web/supervisor.js && echo "parses"`

Expected: `parses`. If `node` is unavailable, run `python -m pytest tests/test_qa_supervisor_page_restore.py -q` instead — it lifts source out of this file and executes it in chromium, so a syntax error surfaces there.

- [ ] **Step 5: Run the suites that read this file**

Run: `python -m pytest tests/test_qa_supervisor_page_restore.py tests/test_qa_supervisor_layout.py -q`

Expected: all PASS, `skipped: 0`.

- [ ] **Step 6: Commit**

```bash
git add web/supervisor.js
git diff --cached --stat   # ONLY web/supervisor.js
git commit -- web/supervisor.js -m "fix: resize drags no longer die when the pointer leaves the handle

mousemove/mouseup were bound to the 5px handle, so a fast drag stopped
mid-gesture. Moved to document and released in onResizeEnd."
```

---

### Task 6: Clear the event log when switching supervisors

`addLogEntry()` only appends to the DOM, and `selectSupervisor()` never clears it, so switching supervisors interleaves two runs' events with no divider. `chatMessages` is already replaced per supervisor; this brings the log into line.

**Files:**
- Modify: `web/supervisor.js` — `selectSupervisor()` (~394)
- Test: `tests/test_qa_supervisor_layout.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `selectSupervisor(id)` empties `#event-log` and truncates the `eventLog` array. Signature unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_qa_supervisor_layout.py`:

```python
class EventLogResetTests(unittest.TestCase):
    """One supervisor's events must not appear under another's name.

    Source-level, because the behaviour is a single ordering property:
    selectSupervisor must clear the log before showActiveSupervisor repopulates
    it. Driving it in a browser would need the whole API surface stubbed to
    assert one statement's presence and position.
    """

    def setUp(self):
        self.source = (REPO / "web" / "supervisor.js").read_text(
            encoding="utf-8")

    def _select_supervisor_body(self) -> str:
        start = self.source.index("function selectSupervisor(")
        brace = self.source.index("{", start)
        depth, pos = 1, self.source.index("{", start) + 1
        while depth:
            if self.source[pos] == "{":
                depth += 1
            elif self.source[pos] == "}":
                depth -= 1
            pos += 1
        return self.source[brace + 1:pos - 1]

    def test_the_log_is_cleared_on_switch(self):
        body = self._select_supervisor_body()
        self.assertIn("eventLog.length = 0", body,
                      "the eventLog array must be truncated on switch")
        self.assertRegex(
            body, r'el\.eventLog\.innerHTML\s*=\s*""',
            "the #event-log element must be emptied on switch")

    def test_it_is_cleared_before_the_panel_repopulates(self):
        """Clearing after showActiveSupervisor would erase the new log."""
        body = self._select_supervisor_body()
        self.assertLess(
            body.index("eventLog.length = 0"),
            body.index("showActiveSupervisor()"),
            "the log must be cleared before the new supervisor is shown",
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_qa_supervisor_layout.py::EventLogResetTests -v`

Expected: both FAIL — `'eventLog.length = 0' not found`.

- [ ] **Step 3: Clear the log in `selectSupervisor`**

Find:
```javascript
  function selectSupervisor(id) {
    activeSupervisorId = id;
    // The count belongs to the conversation you were reading, not to the one
    // you just opened.
    clearBadge();
    rememberOpen(id);
    renderSupervisorList();
    showActiveSupervisor();
  }
```
Replace with:
```javascript
  function selectSupervisor(id) {
    activeSupervisorId = id;
    // The count belongs to the conversation you were reading, not to the one
    // you just opened.
    clearBadge();
    // Same for the event log. addLogEntry only ever appends to the DOM, so
    // without this the previous supervisor's events stayed on screen and
    // interleaved with the new one's, undivided -- two runs presented as one.
    // Before showActiveSupervisor, which reconnects SSE and starts filling it.
    if (el.eventLog) el.eventLog.innerHTML = "";
    eventLog.length = 0;
    rememberOpen(id);
    renderSupervisorList();
    showActiveSupervisor();
  }
```

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_qa_supervisor_layout.py::EventLogResetTests -v`

Expected: both PASS.

- [ ] **Step 5: Mutation-check the test is real**

Temporarily move `eventLog.length = 0;` and the `innerHTML` line to *after* `showActiveSupervisor();`, then run:

Run: `python -m pytest tests/test_qa_supervisor_layout.py::EventLogResetTests -v`

Expected: `test_it_is_cleared_before_the_panel_repopulates` FAILS. Confirm the file actually changed (`git diff -- web/supervisor.js | head`) before drawing a conclusion — a mutation that did not land reports a false pass. Then restore the correct order and re-run to green.

- [ ] **Step 6: Commit**

```bash
git add web/supervisor.js tests/test_qa_supervisor_layout.py
git diff --cached --stat   # ONLY those two
git commit -- web/supervisor.js tests/test_qa_supervisor_layout.py -m "fix: clear the event log when switching supervisors

addLogEntry only appends, and selectSupervisor never cleared, so two runs'
events interleaved with no divider."
```

---

### Task 7: Cache-bust, prove maximize still works, and verify the whole change

The `body.max-*` rules are the trap in this change: `#shell` acquiring a containing block would break all four while the page still looks correct at rest. This task asserts that explicitly, then bumps the script version so a cached `supervisor.js` cannot mask Tasks 5 and 6.

**Files:**
- Modify: `web/supervisor.html:1044` (script version)
- Test: `tests/test_qa_supervisor_layout.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing further.

- [ ] **Step 1: Write the maximize test**

Append to `tests/test_qa_supervisor_layout.py`:

```python
@unittest.skipUnless(CHROMIUM, "chromium not installed")
class MaximizeTests(unittest.TestCase):
    """Each body.max-* class must fill the area below the topbar.

    These rules use `position:absolute; top:44px`, resolved against the
    viewport. Giving #shell a position, transform, filter or contain would
    re-root them silently: the page looks right at rest and every maximize
    button lands in the wrong place. This is the assertion that catches it.
    """

    TOPBAR_H = 44

    def _assert_fills_below_topbar(self, panel: str, body_class: str):
        geo = probe_geometry(body_class=body_class)
        vw, vh = (int(v) for v in geo["viewport"].split(","))
        x, y, w, h = box(geo, panel)
        self.assertEqual(y, self.TOPBAR_H,
                         f"{panel} under {body_class} does not start at the "
                         f"topbar's lower edge")
        self.assertEqual(x, 0, f"{panel} under {body_class} is inset")
        self.assertEqual(w, vw, f"{panel} under {body_class} is not full width")
        self.assertEqual(h, vh - self.TOPBAR_H,
                         f"{panel} under {body_class} does not fill the height")

    def test_max_left(self):
        self._assert_fills_below_topbar("#panel-left", "max-left")

    def test_max_center(self):
        self._assert_fills_below_topbar("#panel-center", "max-center")

    def test_max_right(self):
        self._assert_fills_below_topbar("#panel-right", "max-right")

    def test_max_bottom(self):
        geo = probe_geometry(body_class="max-bottom")
        vw, vh = (int(v) for v in geo["viewport"].split(","))
        x, y, w, h = box(geo, "#panel-bottom")
        self.assertEqual(x, 0)
        self.assertEqual(w, vw)
        self.assertEqual(h, vh - self.TOPBAR_H,
                         "max-bottom sets height:calc(100vh - 44px)")
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_qa_supervisor_layout.py::MaximizeTests -v`

Expected: 4 PASS. If any fails on `y`, check whether `#shell` was given `position`, `transform`, `filter` or `contain` — that is the documented cause. `max-bottom` is checked without asserting `y`, because its rule sets `bottom:0` with a height calc rather than a top offset.

- [ ] **Step 3: Bump the script version**

Find:
```
    <script src="supervisor.js?v=4"></script>
```
Replace with:
```
    <script src="supervisor.js?v=5"></script>
```

This is the page's own asset query, not the application version — leave `0.9.5` alone. `test_qa_supervisor_page_restore.py::test_the_script_tag_was_cache_busted` asserts `>= 4`, so `5` keeps it passing.

- [ ] **Step 4: Confirm exactly one version-ish change, and it is the right one**

Run:
```bash
git diff -- web/supervisor.html | grep '^[+-].*supervisor\.js?v='   # the bump
git diff -- web/supervisor.html | grep -c '^[+-].*0\.9\.'                  # expect 0
python -m pytest tests/test_qa_version_consistency.py -q            # 6 passed
```

- [ ] **Step 5: Mutation-check the layout suite before claiming it**

Four mutations, one at a time. For each: apply it, **confirm with `git diff` that the file actually changed** (a mutation that did not land reports a false pass, which has happened in this repo), run the command, restore.

| # | Mutation | Command | Must fail |
|---|---|---|---|
| 1 | Re-add the stray `</div>` before `<!-- Right: Detail Panel -->` | `python -m pytest tests/test_qa_supervisor_layout.py -q` | `test_the_body_divs_balance`, `test_panel_right_is_a_child_of_layout`, `test_panel_right_reports_layout_as_its_parent` |
| 2 | Delete `min-height: 0;` from `#layout` | `python -m pytest tests/test_qa_supervisor_layout.py::PanelGeometryTests -q` | a `test_every_panel_is_inside_the_viewport*` subTest |
| 3 | Add `position: relative;` to `#shell` | `python -m pytest tests/test_qa_supervisor_layout.py::MaximizeTests -q` | at least one of `test_max_left/center/right` |
| 4 | Move `#panel-bottom` back inside `#layout` | `python -m pytest tests/test_qa_supervisor_layout.py -q` | `test_panel_bottom_is_not_inside_the_layout_row` |

If mutation 2 or 3 does **not** fail, the corresponding assertion is not doing its job — strengthen it before proceeding. Mutation 3 is the important one: it is the failure mode that leaves the page looking correct at rest.

After all four, confirm the tree is restored: `git diff -- web/supervisor.html` should show only this task's script-version bump.

- [ ] **Step 6: Lint**

Run: `ruff check .`

Expected: clean. Only `tests/test_qa_supervisor_layout.py` is new Python in this change; the other edits are HTML and JS. If `ruff` reports pre-existing failures in files this plan did not touch, note them and do not fix them here.

- [ ] **Step 7: Run the full supervisor suite**

Run: `python -m pytest tests/ -k "superv" -q 2>&1 | tail -5`

Expected: all pass, no new failures against the 404-passing baseline recorded in the spec. This takes roughly five minutes; run it in the background rather than with a short timeout.

- [ ] **Step 8: Audit the skips**

Run: `python -m pytest tests/test_qa_supervisor_layout.py -q 2>&1 | tail -3`

Expected: `skipped: 0`. A skipped geometry or maximize class means chromium was not found and the browser-level assertions never executed — the suite would be green and prove nothing.

- [ ] **Step 9: Live check in a real browser**

A green suite does not show a person a panel. Load the supervisor page and confirm:
1. The Task Detail panel is visible on the right.
2. Clicking a task in the tree fills it — id, title, status, model, dependencies, timeline, and the full result text.
3. The Event Log spans the full width along the bottom.
4. Dragging the log's top edge resizes it, and a deliberately fast drag that leaves the 5px strip keeps working.
5. Switching supervisors empties the log.
6. Each of the four maximize buttons fills the area below the topbar, and restore returns to normal.

Note on (2): `renderTaskDetail()` has never been exercised by a human, because its panel has never been visible. Any rendering defect in it is latent and will surface now. If the panel renders wrongly, that is a **new** finding — record it separately rather than widening this change, which is scoped to reachability.

- [ ] **Step 10: Final commit**

```bash
git add web/supervisor.html tests/test_qa_supervisor_layout.py
git diff --cached --stat   # ONLY those two
git commit -- web/supervisor.html tests/test_qa_supervisor_layout.py -m "test: assert maximize survives the #shell wrapper; cache-bust supervisor.js

The body.max-* rules position against the viewport, so #shell acquiring a
containing block would break all four maximize buttons while the page still
looked correct at rest."
```

---

## Notes for the executor

- **Coordination is cleared, but re-verify.** cweb5 confirmed nothing uncommitted in either file and both were clean at HEAD at plan time. Three separate readings of this tree went stale mid-session — mine ("clean"), cweb6's ("dirty"), and mine again ("clean") — each correct when taken, because a 0.9.5 bump was committed in between. Assume the same will happen to you.
- **`git status --porcelain web/supervisor.html web/supervisor.js` before each task.** This tree has moved under a measurement roughly every ten minutes; a clean reading goes stale within minutes. If either file has changed under you, re-read before editing.
- **Every "Find/Replace" step is a targeted string replacement.** Never write either file whole.
- **If a test passes when you expected failure, suspect the test.** Two tests in `tests/test_qa_supervisor_changes.py` copy production logic into the test body and pass regardless of the code they claim to cover; that pattern is present in this repo and worth not repeating.
