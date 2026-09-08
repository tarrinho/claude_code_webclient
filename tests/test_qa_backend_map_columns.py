"""QA: the Backends panel is three columns, and the two add buttons match.

Two changes, one panel.

**Three columns.** The map used to be From | wires | Runs on. A turn reaches a
backend either straight off this host or through a named SSH transport, and the
two-column map had nowhere to say which -- the transport appeared only as a
group header inside the backend list, so the path a turn takes was not drawn.
There is now a read-only transport column between the two, with a wire gutter
either side.

The load-bearing property is that both columns render from *one* ordered list
(`_machineGroups`). If the transport column ever sorted itself, the wires
between the columns would cross, which is a worse picture than the two-column
map it replaces -- and a second copy of the ordering is a copy that drifts.
This repo has been bitten by that exact shape before: `backendKindLabel` lives
in `app.js` and is imported here because a duplicated kind-to-label table
missed the `ssh_proxy` entry and mislabelled a backend.

**The buttons.** `＋ Add transport` and `＋ Add machine` were separated by the
entire transport form, so they could never share a line, and only one of them
had a CSS rule: `.btn-add-machine` set `width:100%`, `min-height:36px`,
`font-size:13px` while `.btn-add-transport` set nothing and fell through to
bare `.btn-secondary` at 38px and auto width. Three visible differences out of
one absent selector, which is why the fix is a single shared rule rather than
two matching ones.

Asserted from source: this repo has no JS test runner, and the browser test
that exercises the click path needs chromium.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLES = ROOT / "web" / "assets" / "styles.css"
MACHINES_JS = ROOT / "web" / "assets" / "machines.js"


class ThreeColumnMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        match = re.search(
            r'<div class="backend-map[^"]*" id="backendMap">(.*?)\n        </div>',
            self.html, re.DOTALL)
        self.assertIsNotNone(match, "backendMap block not found in index.html")
        self.map_block = match.group(1)

    def test_there_are_three_column_headers_in_order(self):
        headers = re.findall(r'<div class="map-col">([^<]+)</div>', self.map_block)
        self.assertEqual(headers, ["From", "Transports", "Run On"])

    def test_the_transport_column_has_a_container_for_the_spine(self):
        self.assertIn('id="transportSpine"', self.map_block)

    def test_there_are_two_wire_gutters(self):
        """One per hop. A single gutter cannot show where the middle hop is,
        or that a direct backend has no middle hop at all."""
        self.assertEqual(len(re.findall(r'class="map-wires"', self.map_block)), 2)
        self.assertIn('id="mapWires"', self.map_block)
        self.assertIn('id="mapWires2"', self.map_block)

    def test_the_gutter_ids_are_unique(self):
        """Two elements sharing an id makes byId() return whichever came first,
        so one gutter would silently never be drawn."""
        ids = re.findall(r'id="(mapWires2?)"', self.map_block)
        self.assertEqual(sorted(ids), ["mapWires", "mapWires2"])


class AddButtonTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        self.css = STYLES.read_text(encoding="utf-8")

    def test_both_buttons_are_in_one_row(self):
        match = re.search(
            r'<div class="backend-add-actions">(.*?)</div>', self.html, re.DOTALL)
        self.assertIsNotNone(match, "the buttons are not in a shared row")
        row = match.group(1)
        self.assertIn('id="addTransportBtn"', row)
        self.assertIn('id="addMachineBtn"', row)

    def test_neither_button_is_declared_twice(self):
        """Moving one into the row without removing the original would leave two
        elements with the same id -- and app.js binds its click listener by id,
        so the second copy would render but do nothing when pressed."""
        for element_id in ("addTransportBtn", "addMachineBtn"):
            self.assertEqual(
                self.html.count(f'id="{element_id}"'), 1,
                f"{element_id} is declared more than once")

    def test_both_buttons_still_carry_btn_secondary(self):
        row = re.search(
            r'<div class="backend-add-actions">(.*?)</div>', self.html, re.DOTALL).group(1)
        self.assertEqual(row.count("btn-secondary"), 2)

    def test_one_css_rule_covers_both(self):
        """The regression that started this. A rule per button is how they
        drifted to different heights, widths and font sizes in the first
        place, so the fix has to be a shared selector -- two matching rules
        would pass a naive check and drift again on the next edit."""
        self.assertRegex(
            self.css,
            r"\.btn-add-transport\s*,\s*\.btn-add-machine\s*\{[^}]*min-height[^}]*\}")

    def test_neither_button_has_a_rule_of_its_own_setting_size(self):
        for selector in (r"\.btn-add-machine", r"\.btn-add-transport"):
            for rule in re.findall(rf"(?<![-\w,]){selector}\s*\{{([^}}]*)\}}", self.css):
                for prop in ("width", "min-height", "font-size"):
                    self.assertNotIn(
                        prop, rule,
                        f"{selector} sets {prop} on its own; it must come from "
                        "the shared rule or the two can differ again")

    def test_the_row_stacks_on_a_narrow_screen(self):
        """The old width:100% existed for mobile. flex-wrap is what preserves
        that, and dropping it would make both buttons cramped rather than
        stacked."""
        match = re.search(r"\.backend-add-actions\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match)
        self.assertIn("flex-wrap", match.group(1))


class OneOrderingForBothColumnsTests(unittest.TestCase):
    """The load-bearing property: the columns cannot sort independently."""

    def setUp(self):
        self.js = MACHINES_JS.read_text(encoding="utf-8")

    def test_the_grouping_is_computed_in_one_function(self):
        self.assertIn("export function _machineGroups()", self.js)

    def test_both_renderers_consume_it(self):
        """_renderMachineList computes the groups and hands the same array to
        the spine. If the spine called _machineGroups() itself that would still
        be one ordering, but a future edit could pass it a filtered list and the
        wires would silently start crossing."""
        body = re.search(
            r"export function _renderMachineList\(\)\s*\{(.*?)\n\}", self.js,
            re.DOTALL).group(1)
        self.assertIn("_machineGroups()", body)
        self.assertIn("_renderTransportSpine(groups)", body)

    def test_the_spine_does_not_sort(self):
        """Any .sort() inside the spine renderer is the defect this class
        exists to catch."""
        body = re.search(
            r"function _renderTransportSpine\(groups\)\s*\{(.*?)\n\}", self.js,
            re.DOTALL).group(1)
        self.assertNotIn(".sort(", body)

    def test_direct_is_labelled_direct_in_the_column(self):
        """Pedro's requirement: where no transport exists, the path says
        `direct` rather than leaving the middle column blank."""
        groups = re.search(
            r"export function _machineGroups\(\)\s*\{(.*?)\n\}", self.js,
            re.DOTALL).group(1)
        self.assertIn("spineLabel: 'direct'", groups)

    def test_every_group_kind_reaches_the_column(self):
        """Three kinds of group exist -- direct, a known transport, and a
        machine pointed at a transport that no longer exists. The orphan case
        is the one that gets forgotten, and a group with no spine entry has a
        wire with no origin."""
        groups = re.search(
            r"export function _machineGroups\(\)\s*\{(.*?)\n\}", self.js,
            re.DOTALL).group(1)
        self.assertEqual(groups.count("groups.push("), 3)
        self.assertIn("key: 'direct'", groups)
        self.assertIn("key: transport.id", groups)
        self.assertIn("key: `unknown:${transportId}`", groups)


class SpineToGroupWiringTests(unittest.TestCase):
    def setUp(self):
        self.js = MACHINES_JS.read_text(encoding="utf-8")

    def test_the_header_and_the_entry_agree_on_the_key(self):
        """`data-group` is how a wire and a click find a spine entry's partner.
        Both sides must set it or the second gutter draws nothing."""
        self.assertIn("header.dataset.group = group.key", self.js)
        self.assertIn("entry.dataset.group = group.key", self.js)

    def test_the_spine_carries_no_actions(self):
        """One home per action. Edit/Delete/Check/Init stay in the Run On group
        header; duplicating them here is two places to keep in sync."""
        body = re.search(
            r"function _renderTransportSpine\(groups\)\s*\{(.*?)\n\}", self.js,
            re.DOTALL).group(1)
        for action in ("_showEditTransport", "_deleteTransport",
                       "_checkTransport", "_initTransport"):
            self.assertNotIn(action, body)

    def test_clicking_expands_a_collapsed_group_before_looking_for_it(self):
        """The header only exists in the DOM when the group is expanded, so
        scrolling to it without expanding first finds nothing and the click
        appears to do nothing at all."""
        body = re.search(
            r"function _revealGroup\(key\)\s*\{(.*?)\n\}", self.js, re.DOTALL).group(1)
        expand = body.index("_collapsedGroups.delete(key)")
        lookup = body.index("querySelector")
        self.assertLess(expand, lookup)
        self.assertIn("_renderMachineList()", body[:lookup])
        self.assertIn("scrollIntoView", body)

    def test_a_group_key_is_escaped_before_going_into_a_selector(self):
        """A transport id reaches a CSS attribute selector. It is a UUID today,
        but `unknown:<id>` already contains a colon, and an unescaped one is a
        syntax error that throws rather than returning null."""
        self.assertNotIn('[data-group="${key}"]', self.js)
        self.assertIn("CSS.escape(key)", self.js)


class NarrowViewportTests(unittest.TestCase):
    def test_the_transport_column_is_hidden_with_the_wires(self):
        """Below 620px the CSS already hides From and the gutters. The transport
        column has to go with them: wires are what made it mean anything, and
        without them it is a list of names above the group headers that already
        name them. _drawMapWires must then fall back to the two-column path
        rather than drawing nothing."""
        css = STYLES.read_text(encoding="utf-8")
        media = re.search(r"@media\(max-width:620px\)\s*\{(.*?)\n\}", css, re.DOTALL)
        self.assertIsNotNone(media)
        self.assertIn(".map-via", media.group(1))

        js = MACHINES_JS.read_text(encoding="utf-8")
        body = re.search(
            r"export function _drawMapWires\(\)\s*\{(.*?)\n\}", js, re.DOTALL).group(1)
        self.assertIn("!g2 || !spine.length", body)


if __name__ == "__main__":
    unittest.main()
