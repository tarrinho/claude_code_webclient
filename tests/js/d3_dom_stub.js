/* Minimal d3 + DOM stand-ins, so web/assets/supervisor-map.js can be executed
 * for real inside QuickJS instead of being read and paraphrased in Python.
 *
 * Scope note, because it decides which assertions are trustworthy: this stub
 * records what the module under test *does* -- which selection it joins nodes
 * onto, which attributes it computes, which zoom API it calls, what geometry
 * it passes to the layout. It deliberately does NOT reimplement d3's radial
 * tree layout; `tree()` here assigns angles and radii by a trivial rule. A
 * test asserting the shape d3 produces would be testing this file, not d3, so
 * no test here does that. Node positions are asserted only through the
 * transform strings supervisor-map.js itself builds from x/y.
 *
 * Everything observable is collected on the global STUB object.
 */

var STUB = {
  svgBox: {width: 900, height: 600, left: 0, top: 0},
  selectAllCalls: [],
  zoomHandlers: [],
  transforms: [],
  scaleToCalls: [],
  scaleByCalls: [],
  treeSize: null,
  treeNodeSize: null,
  // Which layout setters were called, in order. Needed because d3's own
  // read-back cannot distinguish "nodeSize only" from "size then nodeSize" --
  // tree.size() returns null in both cases -- so a redundant .size() call is
  // invisible to any assertion on the resulting values. It is still a defect:
  // one of the two calls does nothing while the code reads as though both
  // apply.
  treeCalls: [],
  // The tree-layout methods this stub really implements. Declared rather than
  // inferred, because the proxy below answers *every* method name with a
  // throwing function, so `typeof layout.foo === "function"` can no longer
  // tell present from absent -- this list is what the fidelity tests check
  // against.
  treeImplemented: ["size", "nodeSize", "separation"],
  zoomAttached: 0,
  currentTransform: null,
  elHandlers: {},
  focusCalls: [],
};

/* ── DOM ─────────────────────────────────────────────────────────────── */

function FakeEl(id) {
  var el = {
    id: id,
    tagName: "div",
    hidden: false,
    textContent: "",
    innerHTML: "",
    dataset: {},
    style: {},
    classList: {add: function () {}, remove: function () {}, toggle: function () {}},
    getBoundingClientRect: function () {
      return id === "supervisorMapSvg"
        ? STUB.svgBox
        : {width: 100, height: 100, left: 0, top: 0};
    },
    scrollIntoView: function () {},
    getAttribute: function () { return null; },
    // Focus tracking, for the drawer's focus handling. A test seeds
    // __focusables on the element it is about to query.
    __focusables: [],
    tabIndex: 0,
    disabled: false,
    focus: function () { document.activeElement = el; STUB.focusCalls.push(id); },
    querySelectorAll: function () { return el.__focusables; },
    contains: function (other) { return el.__focusables.indexOf(other) !== -1; },
    addEventListener: function (type, fn) {
      STUB.elHandlers[id] = STUB.elHandlers[id] || {};
      STUB.elHandlers[id][type] = fn;
    },
  };
  return el;
}

var _els = {};

var document = {
  body: null,   // replaced below, once FakeEl exists
  activeElement: null,
  getElementById: function (id) {
    if (!_els[id]) _els[id] = FakeEl(id);
    return _els[id];
  },
  querySelectorAll: function (selector) {
    return [];
  },
  addEventListener: function (type, fn) {
    STUB.elHandlers["document"] = STUB.elHandlers["document"] || {};
    STUB.elHandlers["document"][type] = fn;
  },
};

/* The module dispatches one of these to ask app.js to open a conversation --
 * app.js imports it, so importing back would be a cycle. */
function CustomEvent(type, init) {
  this.type = type;
  this.detail = (init && init.detail) || null;
}

// document.body has to be a real fake element: the module asks it for
// computed styles and uses body.contains() to decide whether the element it
// wants to hand focus back to is still in the page.
document.body = FakeEl("body");

function getComputedStyle() {
  return {getPropertyValue: function () { return ""; }};
}

/* ── d3 selections ───────────────────────────────────────────────────── */

function Sel(tag, opts) {
  opts = opts || {};
  var sel = {
    __tag: tag,
    __attrs: {},
    __styles: {},
    __children: [],
    __handlers: {},
    __data: opts.data || [],
    __computed: {},
    __text: null,
    __node: opts.node || null,
    __origin: null,
    __selector: null,
    tagName: tag,
  };

  sel.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(sel.__attrs, k) ? sel.__attrs[k] : null;
  };
  sel.attr = function (k, v) {
    if (arguments.length === 1) return sel.__attrs[k];
    if (typeof v === "function") {
      sel.__computed[k] = sel.__data.map(function (d, i) { return v.call(sel, d, i); });
      sel.__attrs[k] = sel.__computed[k].length ? sel.__computed[k][0] : undefined;
    } else {
      sel.__attrs[k] = v;
    }
    return sel;
  };
  sel.style = function (k, v) { sel.__styles[k] = v; return sel; };
  sel.text = function (v) {
    sel.__text = typeof v === "function"
      ? sel.__data.map(function (d, i) { return v.call(sel, d, i); })
      : v;
    return sel;
  };
  sel.append = function (t) {
    var child = Sel(t, {data: sel.__data});
    sel.__children.push(child);
    return child;
  };
  sel.selectAll = function (selector) {
    var s = Sel("selection", {});
    s.__selector = selector;
    s.__origin = sel;
    STUB.selectAllCalls.push({
      selector: selector,
      onTag: sel.__tag,
      onClass: sel.__attrs["class"] || null,
    });
    s.remove = function () { sel.__children.length = 0; return s; };
    return s;
  };
  sel.data = function (arr) { sel.__data = arr.slice(); return sel; };
  sel.join = function (t) {
    var parent = sel.__origin || sel;
    var joined = Sel(t, {data: sel.__data});
    joined.__joinedOntoClass = parent.__attrs["class"] || null;
    joined.__joinedOntoTag = parent.__tag;
    parent.__children.push(joined);
    return joined;
  };
  sel.filter = function (fn) {
    return Sel(sel.__tag, {data: sel.__data.filter(fn)});
  };
  sel.each = function (fn) {
    sel.__data.forEach(function (d, i) {
      var per = Sel(sel.__tag, {data: [d]});
      sel.__children.push(per);
      fn.call(per, d, i);
    });
    return sel;
  };
  sel.on = function (name, fn) { sel.__handlers[name] = fn; return sel; };
  sel.node = function () { return sel.__node || sel; };
  sel.remove = function () { sel.__children.length = 0; return sel; };
  sel.call = function (fn) {
    var rest = Array.prototype.slice.call(arguments, 1);
    fn.apply(null, [sel].concat(rest));
    return sel;
  };
  sel.transition = function () { return sel; };
  sel.duration = function () { return sel; };
  return sel;
}

/* ── d3 zoom transforms ──────────────────────────────────────────────── */

function ZTransform(k, x, y) { this.k = k; this.x = x; this.y = y; }
ZTransform.prototype.translate = function (x, y) {
  return new ZTransform(this.k, this.x + this.k * x, this.y + this.k * y);
};
ZTransform.prototype.scale = function (s) {
  return new ZTransform(this.k * s, this.x, this.y);
};
ZTransform.prototype.toString = function () {
  return "translate(" + this.x + "," + this.y + ") scale(" + this.k + ")";
};

/* ── d3 hierarchy + tree ─────────────────────────────────────────────── */

function _hierarchy(data, childrenAccessor) {
  var acc = childrenAccessor || function (d) { return d.children; };
  function build(d, depth, parent) {
    var n = {data: d, depth: depth, parent: parent, children: null};
    var kids = acc(d) || [];
    if (kids.length) {
      n.children = kids.map(function (k) { return build(k, depth + 1, n); });
    }
    // Real hierarchy nodes carry each(); the module under test walks the tree
    // with it to re-apply collapsed branches before layout.
    n.each = function (fn) {
      (function walk(node) {
        fn(node);
        (node.children || []).forEach(walk);
      })(n);
      return n;
    };
    return n;
  }
  var root = build(data, 0, null);
  root.descendants = function () {
    var out = [];
    (function walk(n) {
      out.push(n);
      (n.children || []).forEach(walk);
    })(root);
    return out;
  };
  return root;
}

function _tree() {
  var layout = function (root) {
    var nodes = root.descendants();
    var maxDepth = 0;
    nodes.forEach(function (n) { if (n.depth > maxDepth) maxDepth = n.depth; });
    if (!maxDepth) maxDepth = 1;
    // nodeSize means fixed per-node spacing; size means fit-to-box. Real d3
    // treats them as mutually exclusive (see below), so the effective mode
    // decides which one drives positions here.
    if (layout.__nodeSize) {
      var dx = layout.__nodeSizeVal ? layout.__nodeSizeVal[0] : 1;
      var dy = layout.__nodeSizeVal ? layout.__nodeSizeVal[1] : 1;
      nodes.forEach(function (n, i) {
        n.x = i * dx;
        n.y = n.depth * dy;
      });
    } else {
      var span = layout.__size ? layout.__size[0] : 1;
      var radius = layout.__size ? layout.__size[1] : 1;
      nodes.forEach(function (n, i) {
        n.x = nodes.length > 1 ? (i / nodes.length) * span : 0;
        n.y = (n.depth / maxDepth) * radius;
      });
    }
    // Mirrors d3: tree.size() reads back null once nodeSize is in effect.
    STUB.treeSize = layout.__nodeSize ? null : layout.__size;
    STUB.treeNodeSize = layout.__nodeSize ? layout.__nodeSizeVal : null;
    return root;
  };
  // Declared before the setters so each can return it. Returning the raw
  // `layout` instead lets a chain escape the proxy: the module calls
  // `.separation(...).nodeSize(...)`, so `separation` handing back the target
  // put `.nodeSize` back on an unwrapped object and the descriptive error
  // below never fired -- the failure was still a bare TypeError. Found by
  // replaying the original incident against this stub.
  var proxy;
  // d3-hierarchy's tree layout carries ONE flag for these two setters:
  // `size` clears it, `nodeSize` sets it, and the accessor that is called
  // last wins while the other's value is ignored. Reproduced faithfully on
  // purpose -- a stub that accepted both and honoured both would make a
  // module calling both look correct here and behave differently in a
  // browser, which is the one thing this harness must not do.
  layout.size = function (s) {
    STUB.treeCalls.push("size");
    layout.__size = s;
    layout.__nodeSize = false;
    return proxy;
  };
  layout.nodeSize = function (s) {
    STUB.treeCalls.push("nodeSize");
    layout.__nodeSizeVal = s;
    layout.__nodeSize = true;
    return proxy;
  };
  layout.separation = function (f) {
    STUB.treeCalls.push("separation");
    layout.__separation = f;
    return proxy;
  };
  // Say what is missing, instead of "TypeError: not a function".
  //
  // This stub deliberately implements only the d3 surface the module actually
  // uses, and failing loudly when the module reaches past it is the intended
  // behaviour. What was not intended: on 2026-09-10 the map gained a
  // `.nodeSize()` call, and all 35 tests in
  // test_qa_supervisor_map_geometry.py died inside renderSupervisorMap with a
  // bare TypeError carrying no hint of which method or which file was at
  // fault. They read as 35 behavioural regressions from the layout
  // restructure and stayed that way for a week, because nothing in the
  // failure pointed at the harness.
  //
  // The proxy keeps the loud failure and makes it self-describing. Anything
  // not implemented above is still an error at the moment it is called -- it
  // is not silently accepted, which would be far worse -- but the message
  // names the method, this file, and what to do about it.
  proxy = new Proxy(layout, {
    get: function (target, key) {
      if (key in target) return target[key];
      // Symbols and the internal __-prefixed fields must read as absent, or
      // `layout.__size ? ... : ...` starts seeing a function and every
      // truthiness test in here inverts.
      if (typeof key !== "string" || key.slice(0, 2) === "__") {
        return target[key];
      }
      return function () {
        throw new Error(
          "d3_dom_stub: d3.tree()." + key + "() is not implemented by this "
          + "stub. The module under test calls it. Add it to _tree() in "
          + "tests/js/d3_dom_stub.js with d3-hierarchy's real semantics, and "
          + "pin those semantics in tests/test_qa_d3_stub_fidelity.py -- a "
          + "stub more permissive than d3 makes the tests agree with code "
          + "that behaves differently in a browser. Implemented here: "
          + STUB.treeImplemented.join(", ") + "."
        );
      };
    },
  });
  return proxy;
}

/* ── d3 namespace ────────────────────────────────────────────────────── */

var d3 = {
  select: function (target) {
    if (target && target.__tag) return target;
    if (typeof target === "string" && target.charAt(0) === "#") {
      var el = document.getElementById(target.slice(1));
      if (!STUB.selections) STUB.selections = {};
      if (!STUB.selections[target]) {
        STUB.selections[target] = Sel("svg", {node: el});
      }
      return STUB.selections[target];
    }
    return Sel("unknown", {});
  },
  hierarchy: _hierarchy,
  tree: _tree,
  zoomIdentity: new ZTransform(1, 0, 0),
  zoomTransform: function () { return STUB.currentTransform || d3.zoomIdentity; },
  zoom: function () {
    var z = function () { STUB.zoomAttached += 1; };
    z.__handlers = {};
    z.scaleExtent = function (e) { z.__scaleExtent = e; return z; };
    z.on = function (name, fn) {
      z.__handlers[name] = fn;
      STUB.zoomHandlers.push(name);
      return z;
    };
    function fire(selection, t) {
      STUB.transforms.push({k: t.k, x: t.x, y: t.y, str: String(t)});
      STUB.currentTransform = t;
      if (z.__handlers.zoom) z.__handlers.zoom.call(selection, {transform: t});
    }
    z.transform = function (selection, t) { fire(selection, t); };
    z.scaleTo = function (selection, k) {
      STUB.scaleToCalls.push(k);
      var cur = STUB.currentTransform || d3.zoomIdentity;
      fire(selection, new ZTransform(k, cur.x, cur.y));
    };
    z.scaleBy = function (selection, k) {
      STUB.scaleByCalls.push(k);
      var cur = STUB.currentTransform || d3.zoomIdentity;
      fire(selection, new ZTransform(cur.k * k, cur.x, cur.y));
    };
    return z;
  },
};

/* ── helpers for probes ──────────────────────────────────────────────── */

function stubSvg() { return d3.select("#supervisorMapSvg"); }

function stubFindByClass(sel, cls) {
  if (!sel) return null;
  if (sel.__attrs && sel.__attrs["class"] === cls) return sel;
  var kids = sel.__children || [];
  for (var i = 0; i < kids.length; i++) {
    var hit = stubFindByClass(kids[i], cls);
    if (hit) return hit;
  }
  return null;
}

function stubFindNodeSelection() {
  var vp = stubFindByClass(stubSvg(), "map-viewport");
  if (!vp) return null;
  var kids = vp.__children || [];
  for (var i = 0; i < kids.length; i++) {
    if (kids[i].__attrs && kids[i].__attrs["class"] === "node") return kids[i];
  }
  return null;
}
