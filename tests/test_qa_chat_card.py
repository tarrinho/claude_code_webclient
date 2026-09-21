"""QA: the family card, and the ordering rule it exists to protect.

The behavioural half runs the real module under node -- node is v24.19.0 here
and chat-list.js has no top-level imports, so it can be imported directly. A
source-text assertion cannot tell a correct collapse rule from a broken one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_LIST = ROOT / "web" / "assets" / "chat-list.js"
NODE = shutil.which("node")


def _child_rows(children, expanded):
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const m = await import({json.dumps(module.as_uri())});
        const out = m.childRowsFor(
            {{id: 'a', children: {json.dumps(children)}}},
            {json.dumps(expanded)});
        process.stdout.write(JSON.stringify(out));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout)


def _sub(name):
    return {"kind": "subagent", "tool_use_id": name, "agent_type": name,
            "description": "d", "status": "running",
            "started_at": "2026-09-21T10:00:00Z"}


def _age(stamp, now):
    """childAge(stamp) with Date.now() pinned, so the assertion is stable.

    Without pinning, an age test is a clock test: it passes today and drifts
    tomorrow.
    """
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const fixed = new Date({json.dumps(now)}).getTime();
        Date.now = () => fixed;
        const m = await import({json.dumps(module.as_uri())});
        process.stdout.write(JSON.stringify(m.childAge({json.dumps(stamp)})));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:500]}")
        return json.loads(result.stdout)


@unittest.skipIf(NODE is None, "node is required to execute the module")
class ChildRowsTests(unittest.TestCase):

    def test_no_children_renders_nothing(self):
        self.assertEqual(_child_rows([], False), [])

    def test_a_small_family_renders_in_full_while_collapsed(self):
        """Five or fewer is the threshold: a typical two-or-three-subagent
        turn should be readable without a click."""
        kids = [_sub(f"s{n}") for n in range(5)]
        self.assertEqual(len(_child_rows(kids, False)), 5)

    def test_a_large_family_collapses_by_default(self):
        kids = [_sub(f"s{n}") for n in range(6)]
        self.assertEqual(_child_rows(kids, False), [])

    def test_a_large_family_expands_on_request(self):
        kids = [_sub(f"s{n}") for n in range(6)]
        self.assertEqual(len(_child_rows(kids, True)), 6)

    def test_a_running_subagent_reports_its_age(self):
        """§7: a stuck `running` row must read as stale rather than as active
        work. There is no timeout sweeper, so the age is the only truth on
        offer -- if it is missing, a dead subagent looks busy for ever."""
        out = _age("2026-09-21T10:00:00Z", "2026-09-21T10:45:00Z")
        self.assertEqual(out, "45m")

    def test_an_absent_timestamp_yields_no_age_rather_than_NaN(self):
        self.assertEqual(_age(None, "2026-09-21T10:45:00Z"), "")

    def test_a_malformed_timestamp_yields_no_age(self):
        self.assertEqual(_age("not-a-date", "2026-09-21T10:45:00Z"), "")

    def test_exactly_the_threshold_still_renders(self):
        """Boundary: 5 renders, 6 collapses. Asserted because an off-by-one
        here is invisible until someone runs exactly five subagents."""
        self.assertEqual(len(_child_rows([_sub(f"s{n}") for n in range(5)],
                                         False)), 5)
        self.assertEqual(_child_rows([_sub(f"s{n}") for n in range(6)],
                                     False), [])


# --- Behavioural harness for FamilyReorderTests -----------------------------
#
# Round-1 review proved CommitOrderExclusionTests vacuous: they were
# `assertIn` greps over commitOrder's source text, which the brief explicitly
# ruled out ("asserted on the id list, never on the markup") -- moving the
# filter to after `.map()`, where it is a permanent no-op, left every
# assertion green. This repo has no jsdom (no package.json, no npm on this
# machine), so the replacement is a hand-written DOM just large enough for
# chat-list.js's render/drag/nudge code paths to execute for real: only the
# element, selector and event-listener surface those functions actually call.
# Not a general-purpose fake -- scoped to this file's own use.
_FAKE_DOM_JS = r"""
function toDataAttr(prop) {
  return 'data-' + prop.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
}

function parseSelector(sel) {
  const m = /^(\*|[a-zA-Z][\w-]*)?((?:\.[\w-]+)*)(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(sel.trim());
  if (!m) throw new Error(`fake DOM cannot parse selector: ${sel}`);
  const [, tag, classesStr, attrName, attrValue] = m;
  const classes = classesStr ? classesStr.slice(1).split('.') : [];
  return {tag, classes, attrName, attrValue};
}

function matchesParsed(el, parsed) {
  if (!el || el.nodeType !== 1) return false;
  if (parsed.tag && parsed.tag !== '*' && el.tagName !== parsed.tag.toUpperCase()) return false;
  for (const c of parsed.classes) if (!el._classes.has(c)) return false;
  if (parsed.attrName) {
    const value = el._attrs[parsed.attrName];
    if (value === undefined) return false;
    if (parsed.attrValue !== undefined && value !== parsed.attrValue) return false;
  }
  return true;
}

function collect(root, parsed, out) {
  for (const child of root.children) {
    if (child.nodeType !== 1) continue;
    if (matchesParsed(child, parsed)) out.push(child);
    collect(child, parsed, out);
  }
}

class FakeTextNode {
  constructor(text) { this.nodeType = 3; this.textContent = text; this.parentElement = null; }
}

class FakeElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this._attrs = {};
    this._classes = new Set();
    this._listeners = {};
    this.style = {};
    const self = this;
    this.dataset = new Proxy({}, {
      get(_, prop) { return self._attrs[toDataAttr(prop)]; },
      set(_, prop, value) { self._attrs[toDataAttr(prop)] = String(value); return true; },
    });
  }
  get classList() {
    const self = this;
    return {
      add: (...cls) => cls.forEach(c => self._classes.add(c)),
      remove: (...cls) => cls.forEach(c => self._classes.delete(c)),
      contains: c => self._classes.has(c),
    };
  }
  // chat-list.js sets most rows' classes via `.className = 'a b'` (only
  // `.chat-family` and family-card rows use classList.add) -- both have to
  // land in the same `_classes` set or the selector engine below only ever
  // sees whichever one a given row happened to use.
  get className() { return [...this._classes].join(' '); }
  set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
  setAttribute(name, value) { this._attrs[name] = String(value); }
  getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; }
  removeAttribute(name) { delete this._attrs[name]; }
  appendChild(node) {
    if (node.parentElement) node.parentElement.removeChild(node);
    this.children.push(node);
    node.parentElement = this;
    return node;
  }
  append(...nodes) { nodes.forEach(n => this.appendChild(typeof n === 'string' ? new FakeTextNode(n) : n)); }
  prepend(...nodes) {
    nodes.forEach(n => { if (n.parentElement) n.parentElement.removeChild(n); });
    this.children.unshift(...nodes);
    nodes.forEach(n => { n.parentElement = this; });
  }
  insertBefore(newNode, refNode) {
    if (newNode.parentElement) newNode.parentElement.removeChild(newNode);
    if (refNode == null) {
      this.children.push(newNode);
    } else {
      const idx = this.children.indexOf(refNode);
      if (idx === -1) throw new Error('NotFoundError: refNode is not a child of this node');
      this.children.splice(idx, 0, newNode);
    }
    newNode.parentElement = this;
    return newNode;
  }
  removeChild(node) {
    const idx = this.children.indexOf(node);
    if (idx !== -1) this.children.splice(idx, 1);
    node.parentElement = null;
    return node;
  }
  replaceChildren() {
    this.children.forEach(c => { c.parentElement = null; });
    this.children = [];
  }
  get nextSibling() {
    if (!this.parentElement) return null;
    const idx = this.parentElement.children.indexOf(this);
    return this.parentElement.children[idx + 1] || null;
  }
  get nextElementSibling() { return this.nextSibling; }
  get previousElementSibling() {
    if (!this.parentElement) return null;
    const idx = this.parentElement.children.indexOf(this);
    return this.parentElement.children[idx - 1] || null;
  }
  addEventListener(type, cb) { (this._listeners[type] ||= []).push(cb); }
  removeEventListener(type, cb) {
    const list = this._listeners[type];
    if (!list) return;
    const idx = list.indexOf(cb);
    if (idx !== -1) list.splice(idx, 1);
  }
  querySelectorAll(sel) {
    const parsed = parseSelector(sel);
    const out = [];
    collect(this, parsed, out);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    const parsed = parseSelector(sel);
    let el = this;
    while (el && el.nodeType === 1) {
      if (matchesParsed(el, parsed)) return el;
      el = el.parentElement;
    }
    return null;
  }
  getBoundingClientRect() { return {top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0}; }
  focus() {}
}

const body = new FakeElement('body');
globalThis.document = {
  body,
  createElement: tag => new FakeElement(tag),
  createTextNode: text => new FakeTextNode(text),
  querySelector: sel => body.querySelector(sel),
  querySelectorAll: sel => body.querySelectorAll(sel),
  addEventListener: () => {},
  removeEventListener: () => {},
};
globalThis.CSS = {escape: s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`)};
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
"""

# Builds one rendered list: three root chats, the middle one ('b') carrying a
# single child CHAT ('child-b') -- the case that actually exercises the
# `data-child` exclusion, since a subagent child never gets a chat id at all.
# `reorderCalls` records every id list handed to onReorder, in call order.
_HARNESS_SETUP = _FAKE_DOM_JS + r"""
const m = await import('./chat_list.mjs');

const list = document.createElement('div');
list.id = 'chatList';
document.body.appendChild(list);

const reorderCalls = [];
const controller = m.createChatListController({
  lists: [list],
  searchInputs: [],
  formatTime: () => '',
  formatAbsoluteTime: () => '',
  onSelect: () => {},
  onAction: () => {},
  onResumeCli: () => {},
  onReorder: ids => { reorderCalls.push(ids); },
});

const chats = [
  {id: 'a', title: 'A', children: []},
  {id: 'b', title: 'B', children: [
    {kind: 'chat', id: 'child-b', title: 'Child of B', relation: 'task'},
  ]},
  {id: 'c', title: 'C', children: []},
];
controller.render(chats, null);

const fakeDataTransfer = () => ({setData() {}, effectAllowed: null});
"""


def _run_family_scenario(scenario_js):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # _HARNESS_SETUP embeds _FAKE_DOM_JS inline, so chat_list.mjs is the
        # only other file the driver needs on disk.
        (tmp_path / "chat_list.mjs").write_text(CHAT_LIST.read_text(), encoding="utf-8")
        driver = tmp_path / "driver.mjs"
        driver.write_text(_HARNESS_SETUP + scenario_js, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver)],
            capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr.strip()[:2000]}")
        return json.loads(result.stdout)


@unittest.skipIf(NODE is None, "node is required to execute the module")
class FamilyReorderTests(unittest.TestCase):
    """Behavioural coverage for the family-card reorder fixes (review round 1).

    Wrapping a family head in `.chat-family` moved it out of being a direct
    child of its section container, and three places still assumed
    `row.parentElement === target`: dragover (threw `NotFoundError` whenever
    the pointer hovered a family head), dragstart (a family head could never
    actually be picked up, since the guard compared the wrong parent), and
    `nudge` (mobile move-up/move-down, silently dead for any chat with
    children because its card holds exactly one `.chat-item`). All three are
    only provable by running the real reorder code against a real tree.
    """

    def test_child_rows_never_reach_the_persisted_order(self):
        """The regression commitOrder exists to prevent: a child row's
        position written as if it were a root. Asserted on the id list
        onReorder actually receives, never on source text."""
        out = _run_family_scenario(r"""
        const rowA = list.querySelector('.chat-item[data-chat-id="a"]');
        rowA._listeners.dragend[0]();
        const result = {
          ids: reorderCalls[reorderCalls.length - 1],
          childDraggable: list.querySelector('.chat-child[data-chat-id="child-b"]').draggable,
        };
        process.stdout.write(JSON.stringify(result));
        """)
        self.assertEqual(out["ids"], ["a", "b", "c"])
        self.assertNotIn("child-b", out["ids"])
        # The child row is built with draggable = false; confirmed on the
        # actual element rather than by grepping for the literal assignment.
        self.assertEqual(out["childDraggable"], False)

    def test_dragover_does_not_throw_when_hovering_a_family_head(self):
        """§1: target.insertBefore(dragging, item) raised NotFoundError on
        every dragover tick while the pointer sat over a family card, because
        `item` (the family head row) is not a child of `target` -- its
        `.chat-family` wrapper is. A new regression, and user-visible as
        reordering dying mid-drag."""
        out = _run_family_scenario(r"""
        const rowA = list.querySelector('.chat-item[data-chat-id="a"]');
        const rowBHead = list.querySelector('.chat-item[data-chat-id="b"]');
        rowA._listeners.dragstart[0]({dataTransfer: fakeDataTransfer()});
        let threw = false;
        try {
          rowBHead._listeners.dragover[0](
            {dataTransfer: fakeDataTransfer(), preventDefault() {}, clientY: 1});
        } catch (e) { threw = true; }
        rowA._listeners.dragend[0]();
        const result = {threw, ids: reorderCalls[reorderCalls.length - 1]};
        process.stdout.write(JSON.stringify(result));
        """)
        self.assertFalse(out["threw"])

    def test_a_family_head_can_be_dragged_and_dropped(self):
        """§2: `dragging.parentElement !== target` was always true for a
        family head, since its parent is the `.chat-family` wrapper, so the
        guard returned early and a chat with children could be picked up and
        never dropped."""
        out = _run_family_scenario(r"""
        const rowBHead = list.querySelector('.chat-item[data-chat-id="b"]');
        const rowC = list.querySelector('.chat-item[data-chat-id="c"]');
        rowBHead._listeners.dragstart[0]({dataTransfer: fakeDataTransfer()});
        let threw = false;
        try {
          rowC._listeners.dragover[0](
            {dataTransfer: fakeDataTransfer(), preventDefault() {}, clientY: 1});
        } catch (e) { threw = true; }
        rowBHead._listeners.dragend[0]();
        const result = {threw, ids: reorderCalls[reorderCalls.length - 1]};
        process.stdout.write(JSON.stringify(result));
        """)
        self.assertFalse(out["threw"])
        # b actually moved past c -- the drop was not silently swallowed.
        self.assertEqual(out["ids"], ["a", "c", "b"])

    def test_nudge_reorders_a_family_head_not_just_within_its_own_card(self):
        """§3: nudge walked `row.parentElement.querySelectorAll(...)`. For a
        family head that parent is the card, which holds exactly one
        `.chat-item` (children are `.chat-child`), so `siblings.length === 1`
        and move-up/move-down returned early without ever calling
        commitOrder. Mobile has no other way to reorder -- drag is unusable
        there, which is nudge's own reason for existing."""
        out = _run_family_scenario(r"""
        const moveDown = list.querySelectorAll('[data-action="move-down"]')
          .find(b => b.dataset.chatId === 'b');
        list._listeners.click[0]({target: moveDown});
        const result = {ids: reorderCalls[reorderCalls.length - 1]};
        process.stdout.write(JSON.stringify(result));
        """)
        # b moved past c: onReorder was actually called (proving nudge did not
        # return early), and with the family's new position reflected.
        self.assertEqual(out["ids"], ["a", "c", "b"])


if __name__ == "__main__":
    unittest.main()
