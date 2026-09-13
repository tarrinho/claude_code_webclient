/* QuickJS has no console built-in. Provide no-op console so the module can
 * log without crashing, plus a recorder so probes can inspect calls. */
var _nativeConsole = typeof console !== 'undefined' ? console : null;
var console = {
  log: function() { STUB.logCalls = STUB.logCalls || []; var a=[]; for(var i=0;i<arguments.length;i++) a[i]=String(arguments[i]); STUB.logCalls.push(a.join(" ")); if(_nativeConsole) _nativeConsole.log.apply(console, arguments); },
  error: function() { STUB.errors = STUB.errors || []; var a=[]; for(var i=0;i<arguments.length;i++) a[i]=String(arguments[i]); STUB.errors.push(a.join(" ")); if(_nativeConsole) _nativeConsole.error.apply(console, arguments); },
  warn: function() { if(_nativeConsole) _nativeConsole.warn.apply(console, arguments); },
};

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
  // The tree-layout methods this stub really implements. Needed because the
  // proxy below answers *every* method name with a throwing function, so
  // `typeof layout.foo === "function"` can no longer tell present from absent
  // and the fidelity tests need some other source of truth.
  //
  // Derived from the layout itself, not hand-written. A declared list drifts
  // in one direction silently: understate it and two tests fail, but add a
  // method to _tree() without listing it and the coverage check goes quiet on
  // exactly the method you just added. The proxy already treats
  // `key in target` as the definition of "implemented", so this uses the same
  // rule -- own function-valued keys, minus the internal __-prefixed state --
  // and the two cannot disagree. It also keeps the "Implemented here:" line in
  // the error message accurate for free. Populated by _tree(); empty until the
  // first d3.tree() call, so read it after one.
  treeImplemented: [],
  zoomAttached: 0,
  // Timers the module sets, recorded rather than run. The map starts a 1s
  // freshness ticker inside renderSupervisorMap, and without these every
  // geometry test died on "ReferenceError: 'setInterval' is not defined" --
  // the stub has to model the surface the module actually uses, which is the
  // same rule test_qa_d3_stub_fidelity.py pins for d3.
  //
  // Not executed: a stub that ran the callback would have the ticker firing
  // during a render and mutating the DOM the test is about to assert on.
  intervals: [],
  clearedIntervals: [],
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
    setAttribute: function (k, v) { el._attrs = el._attrs || {}; el._attrs[k] = v; },
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
    appendChild: function (child) {
      if (!el._children) el._children = [];
      el._children.push(child);
      // Sync to connected Sel's __children so d3 selections see it.
      if (el._sel && el._sel.__children === el._children) {
        // already sharing the same array
      } else {
        // Push directly to the Sel's __children if connected.
        if (el._sel && el._sel.__children !== el._children) {
          el._sel.__children.push(child);
        }
      }
    },
    prepend: function (child) { el._children = el._children || []; el._children.unshift(child); },
    removeChild: function (child) {
      if (el._children) el._children = el._children.filter(c => c !== child);
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

document.createElement = function (tag) {
  var el = FakeEl("_createElement_" + tag);
  el.tagName = tag.toUpperCase();
  return el;
};

// document.body has to be a real fake element: the module asks it for
// computed styles and uses body.contains() to decide whether the element it
// wants to hand focus back to is still in the page.
document.body = FakeEl("body");

function getComputedStyle() {
  return {getPropertyValue: function () { return ""; }};
}

/* ── d3 ──────────────────────────────────────────────────────────────────── */
var _currentSel = null;  // set by .each() so select(this) resolves correctly
var d3 = {
  select: function (el) {
    // When .each() calls the callback, this is the per-node wrapper Sel.
    // Check if el is already a Sel (has __attrs + __children).
    if (el && typeof el.__attrs === "object" && typeof el.__children === "object") {
      _currentSel = el;
      return el;
    }
    // For SVG selection, return a new Sel rooted at the current context.
    var s = Sel(el.slice(1), {data: _currentSel && _currentSel.__data || []});
    if (_currentSel) {
      s.__origin = _currentSel.__origin || _currentSel;
    }
    return s;
  }
};

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
  // Insert places a child before the reference element.  The comms overlay
  // draws label pills *under* the text, and the stub's array of children
  // is paint-order, so the index must be tracked so tests can assert the
  // pill sits before the text node.
  sel.insert = function (t, ref) {
    var child = Sel(t, {data: sel.__data});
    // The ref selector picks the position; we find the first child whose tag
    // matches ref and insert before it.  If no match, push like append.
    var refIdx = ref
      ? sel.__children.findIndex(function (c) { return c.__tag === ref; })
      : -1;
    if (refIdx >= 0) {
      sel.__children.splice(refIdx, 0, child);
    } else {
      sel.__children.push(child);
    }
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
    if (typeof selector === "function") {
      // Real D3: iterate over children DOM nodes, passing bound datum as d.
      // Each placeholder carries its own datum via __data.
      var children = this.__children || [];
      for (var i = 0; i < children.length; i++) {
        var child = children[i];
        var datum = child.__data;
        if (datum !== undefined && datum !== null && datum !== true && datum !== false) {
          if (selector(datum, i, child)) s.__children.push(child);
        }
      }
      s.__data = this.__data || [];
    } else if (typeof selector === "string") {
      var selTag = null, selClass = null;
      if (selector.indexOf(".") !== -1) {
        var parts = selector.split(".");
        selTag = parts[0] || null;
        selClass = parts[1];
      } else {
        selTag = selector;
      }
      var parentData = (this.__data && Array.isArray(this.__data)) ? this.__data : null;
      var children = this.__children || [];
      for (var i = 0; i < children.length; i++) {
        var child = children[i];
        if (!child.__attrs) continue;
        var tagMatch = !selTag || (child.__attrs[selTag] === true);
        var classMatch = !selClass || (child.__attrs["class"] === selClass);
        if (tagMatch && classMatch) {
          if (parentData && parentData[i] !== undefined) {
            child.__data = parentData[i];
          }
          s.__children.push(child);
        }
      }
      // Inherit data from the parent selection — when no matching nodes exist
      // yet (first render), the parent holds the full data array via __origin.
      s.__data = parentData || (sel.__origin && sel.__origin.__data) || [];
    }
    return s;
  };
  sel.data = function (arr) {
    sel.__data = arr.slice();
    // Propagate to all existing node children so .each() and .filter() work.
    var kids = sel.__children || [];
    for (var i = 0; i < kids.length; i++) {
      if (arr[i] !== undefined) kids[i].__data = arr[i];
    }
    return sel;
  };
  sel.join = function (t, enterFn, updateFn, exitFn) {
    // D3 v6+ three-argument form: join(tag, enter, update, exit)
    if (typeof t === "function" || typeof enterFn === "function") {
      // The first argument is an enter-callback, not a tag.
      // Create real placeholder nodes in the parent's __children so
      // selectALL(tag.class) finds them. Each placeholder holds its own
      // data item and its enter-child (e.g. <g class="node">).
      var parent = sel.__origin || sel;
      var data = sel.__data || [];
      var placeholders = [];
      for (var i = 0; i < data.length; i++) {
        var ph = Sel("__placeholder__", {data: data[i]});
        ph.__attrs = {"class": "node"};
        ph.__tag = t;
        ph.__children = [];
        placeholders.push(ph);
      }
      // Helper: return a chainable fake selection over *children* created by append/insert.
      function _enterChain(created, data) {
        var r = Sel("__enter__", {data: data});
        r.__children = created;
        r.append = function (tag) { return _enterChain(created.map(function (c) { var child = Sel(tag, {data: c.__data}); return child; }), data); };
        r.insert = function (tag, ref) { return _enterChain(created.map(function (c) { var child = Sel(tag, {data: c.__data}); return child; }), data); };
        r.attr = function (k, v) {
          for (var j = 0; j < created.length; j++) { created[j].__attrs[k] = v; }
          return r;
        };
        r.text = function (t) {
          for (var j = 0; j < created.length; j++) { created[j].__text = String(t); }
          return r;
        };
        r.each = function (fn) {
          for (var j = 0; j < created.length; j++) { fn.call(created[j], created[j].__data, j); }
          return r;
        };
        r.selectAll = function (s) { return Sel("selection", {}); };
        r.on = function () { return r; };
        r.remove = function () { return r; };
        return r;
      }
      // Attach placeholders to the parent so selectALL finds them.
      for (var i = 0; i < placeholders.length; i++) {
        parent.__children.push(placeholders[i]);
      }
      // The merged selection returned by join() contains the same
      // placeholder nodes. updateFn iterates them; enterFn delegates to them.
      var merged = Sel("__joined__", {data: data});
      merged.__attrs = parent.__attrs;
      merged.__children = placeholders;
      // Propagate .attr() recursively through ALL descendants so chained
      // .attr("x").attr("width") after .join() reaches nested rect/text nodes.
      merged.attr = function (k, v) {
        function walk(n) {
          if (n.__attrs !== undefined) { n.__attrs[k] = v; }
          if (n.__children) {
            for (var j = 0; j < n.__children.length; j++) { walk(n.__children[j]); }
          }
        }
        if (placeholders.length > 0) {
          for (var j = 0; j < placeholders.length; j++) { walk(placeholders[j]); }
        } else {
          // No placeholders (empty data, enter-only path). Walk the parent
          // tree where enter created nodes — the real D3 DOM where these
          // newly-inserted elements live.
          walk(parent);
        }
        return merged;
      };

      // enterFake receives the full data array and a single call with .each()
      // semantics. .append("g") must create one child per placeholder, each
      // carrying its own datum. D3 does this: enterFn(d, i) is called once per
      // item, and append() returns a selection carrying that item's data.
      var enterFake = Sel("__merged__", {data: data});
      enterFake.__dataIsDescendants = true;
      enterFake.each = function (fn) {
        for (var i = 0; i < placeholders.length; i++) {
          fn.call(placeholders[i], data[i], i);
        }
        return enterFake;
      };
      enterFake.append = function (tag) {
        var created = [];
        for (var j = 0; j < placeholders.length; j++) {
          placeholders[j].__data = data[j];
          var child = Sel(tag, {data: data[j]});
          placeholders[j].__children.push(child);
          created.push(child);
        }
        return _enterChain(created, data);
      };
      enterFake.insert = function (tag, ref) {
        var created = [];
        for (var j = 0; j < placeholders.length; j++) {
          var child = Sel(tag, {data: data[j]});
          if (ref && placeholders[j].__children.length) {
            var idx = placeholders[j].__children.findIndex(function (c) { return c.__tag === ref; });
            if (idx >= 0) { placeholders[j].__children.splice(idx, 0, child); } else { placeholders[j].__children.push(child); }
          } else {
            placeholders[j].__children.push(child);
          }
          created.push(child);
        }
        return _enterChain(created, data);
      };
      enterFake.selectAll = function (s) { return Sel("selection", {}); };
      enterFake.attr = function (k, v) {
        for (var j = 0; j < placeholders.length; j++) {
          placeholders[j].__attrs[k] = v;
        }
        return enterFake;
      };
      enterFake.text = function (t) {
        for (var j = 0; j < placeholders.length; j++) {
          placeholders[j].__text = String(t);
        }
        return enterFake;
      };
      enterFake.on = function () { return enterFake; };
      enterFake.remove = function () { return enterFake; };
      if (typeof t === "function") t(enterFake);
      if (updateFn) updateFn(merged);
      if (exitFn) exitFn(Sel("exit", {data: []}));
      return merged;
    }
    var parent = sel.__origin || sel;
    var joined = Sel(t, {data: sel.__data});
    joined.__joinedOntoClass = parent.__attrs["class"] || null;
    joined.__joinedOntoTag = parent.__tag;
    parent.__children.push(joined);
    return joined;
  };
  sel.filter = function (fn) {
    var out = Sel(sel.__tag, {data: sel.__data.filter(fn)});
    out.__origin = sel.__origin || sel;
    return out;
  };
  sel.each = function (fn) {
    var data = sel.__data;
    if (Array.isArray(data) && data.length > 0) {
      data.forEach(function (d, i) {
        var per = Sel(sel.__tag, {data: [d]});
        // Attach to the origin (the real tree) rather than sel, because
        // sel might be a disconnected filter() result that was never
        // appended to the viewport.
        var parent = (sel.__origin || sel);
        parent.__children.push(per);
        fn.call(per, d, i);
      });
      return sel;
    }
    // Fallback: iterate children that have bound data (test stub pattern).
    // In real D3, .data().enter().append() creates nodes carrying their datum.
    // When the parent group has no __data array (just a layer <g>), delegate.
    var kids = sel.__children || [];
    var found = false;
    for (var i = 0; i < kids.length; i++) {
      if (kids[i].__data !== undefined && kids[i].__data !== null && kids[i].__data !== true && kids[i].__data !== false) {
        fn.call(kids[i], kids[i].__data, i);
        found = true;
      }
    }
    if (found) return sel;
    // Last resort: if __data is a single item (not array), call once.
    if (typeof fn === "function" && data !== null && data !== undefined) {
      fn.call(sel, data, 0);
    }
    return sel;
  };
  // ── enter / exit ──────────────────────────────────────────────────
  // Minimal support so the enter-update-exit flow works in tests.
  // .enter() returns a synthetic "enter" Sel carrying the same data
  // as the parent — it delegates appends to create one node per datum.
  // The returned selection is chainable: .attr(), .text(), .each() all
  // delegate to the real children so the enter chain is fully executable.
  sel.enter = function () {
    var e = Sel("enter", {data: sel.__data || []});
    e.__isEnter = true;
    e.__origin = sel.__origin || sel;
    var createdChildren = [];  // holds nodes created by append/insert
    // Helper: return a chainable fake selection over *children*
    function makeResult(children) {
      var r = Sel("__enter__", {data: e.__data || []});
      r.__children = children;
      r.append = function (tag) {
        var out = [];
        for (var i = 0; i < children.length; i++) {
          var c = children[i];
          var child = Sel(tag, {data: c.__data});
          var parent = (e.__origin || e);
          parent.__children.push(child);
          out.push(child);
        }
        return makeResult(out);
      };
      r.insert = function (tag, ref) {
        var out = [];
        for (var i = 0; i < children.length; i++) {
          var c = children[i];
          var child = Sel(tag, {data: c.__data});
          var parent = (e.__origin || e);
          if (ref) {
            var idx = parent.__children.findIndex(function (x) { return x.__tag === ref; });
            if (idx >= 0) { parent.__children.splice(idx, 0, child); continue; }
          }
          parent.__children.push(child);
          out.push(child);
        }
        return makeResult(out);
      };
      r.selectAll = function (s) { return Sel("selection", {}); };
      r.attr = function (k, v) {
        for (var i = 0; i < children.length; i++) {
          children[i].__attrs = children[i].__attrs || {};
          if (typeof k === "string") {
            children[i].__attrs[k] = v;
          } else if (typeof k === "object") {
            for (var key in k) children[i].__attrs[key] = k[key];
          }
        }
        return r;
      };
      r.text = function (t) {
        if (typeof t === "function") {
          for (var i = 0; i < children.length; i++) {
            children[i].__text = String(t(children[i].__data, i, children[i]));
          }
        } else {
          for (var i = 0; i < children.length; i++) {
            children[i].__text = String(t);
          }
        }
        return r;
      };
      r.each = function (fn) {
        for (var i = 0; i < children.length; i++) {
          fn.call(children[i], children[i].__data, i);
        }
        return r;
      };
      r.on = function () { return r; };
      r.remove = function () {
        for (var i = 0; i < children.length; i++) {
          var parent = (e.__origin || e);
          var idx = parent.__children.indexOf(children[i]);
          if (idx >= 0) parent.__children.splice(idx, 1);
        }
        return r;
      };
      return r;
    }
    // Override append to create one child per data item
    e.append = function (tag) {
      var data = e.__data || [];
      for (var i = 0; i < data.length; i++) {
        var child = Sel(tag, {data: data[i]});
        var parent = (e.__origin || e);
        parent.__children.push(child);
        createdChildren.push(child);
      }
      return makeResult(createdChildren);
    };
    e.insert = function (tag, ref) {
      var data = e.__data || [];
      for (var i = 0; i < data.length; i++) {
        var child = Sel(tag, {data: data[i]});
        var parent = (e.__origin || e);
        if (ref) {
          var idx = parent.__children.findIndex(function (c) { return c.__tag === ref; });
          if (idx >= 0) { parent.__children.splice(idx, 0, child); continue; }
        }
        parent.__children.push(child);
        createdChildren.push(child);
      }
      return makeResult(createdChildren);
    };
    e.selectAll = function (s) { return Sel("selection", {}); };
    e.attr = function () { return e; };
    e.text = function () { return e; };
    e.each = function (fn) { return e; };
    e.on = function () { return e; };
    e.remove = function () { return e; };
    return e;
  };
  sel.exit = function () {
    var e = Sel("exit", {data: []});
    e.__isExit = true;
    return e;
  };
  sel.on = function (name, fn) {
    sel.__handlers[name] = fn;
    // Propagate to the group-level so stubFindNodeSelection() returns
    // a selection that carries handlers for test probes that access
    // nodeSel.__handlers.click directly.
    var g = (sel.__origin || sel);
    if (g !== sel) g.__handlers[name] = fn;
    return sel;
  };
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
  // d3-hierarchy's real links(): one entry per node except the root, each
  // naming the node and its parent. Absent from this stub until the spokes
  // needed it -- and a missing method here reads as a bare "not a function"
  // rather than as a stub gap, which is what STUB's proxy exists to prevent.
  root.links = function () {
    return root.descendants()
      .filter(function (d) { return d.parent; })
      .map(function (d) { return {source: d.parent, target: d}; });
  };
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
  // Derived with the proxy's own rule, so the two cannot disagree: an own key
  // whose value is a function, excluding the __-prefixed internal state.
  STUB.treeImplemented = Object.keys(layout)
    .filter(function (k) {
      return k.slice(0, 2) !== "__" && typeof layout[k] === "function";
    })
    .sort();
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

/* ── Timers ──────────────────────────────────────────────────────────── */
var _nextTimerId = 1;
function setInterval(fn, ms) {
  var id = _nextTimerId++;
  STUB.intervals.push({id: id, ms: ms});
  return id;
}
function clearInterval(id) {
  STUB.clearedIntervals.push(id);
}
function setTimeout(fn, ms) {
  // Deliberately does not run the callback either, for the same reason.
  var id = _nextTimerId++;
  return id;
}
function clearTimeout(id) { STUB.clearedIntervals.push(id); }

/* ── d3 namespace ────────────────────────────────────────────────────── */

var d3 = {
  select: function (target) {
    if (target && target.__tag) return target;
    if (typeof target === "string" && target.charAt(0) === "#") {
      var id = target.slice(1);
      if (!STUB.selections) STUB.selections = {};
      if (!STUB.selections[target]) {
        var el = document.getElementById(id);
        if (el) {
          var sel = Sel("svg", {node: el});
          // Share the FakeEl's _children with the Sel's __children so the module's
          // DOM mutations are visible to all callers.
          if (!el._children) el._children = [];
          sel.__children = el._children;
          el._sel = sel;  // so append/insert on FakeEl can sync back
          STUB.selections[target] = sel;
        }
      }
      return STUB.selections[target];
    }
    // Class or tag selector: walk the fake DOM tree for the first matching element.
    if (typeof target === "string") {
      var svgSel = d3.select("#supervisorMapSvg");
      if (svgSel && svgSel.__children) {
        var isClassSel = target.charAt(0) === ".";
        var selCls = null, selTag = null;
        if (isClassSel) {
          selCls = target.slice(1);
        } else {
          selTag = target;
        }
        var found = null;
        function findFirst(children) {
          for (var i = 0; i < children.length; i++) {
            var c = children[i];
            if (!c.__attrs) continue;
            var tagMatch = !selTag || (c.__tag === selTag || c.__attrs["class"] === selCls);
            var classMatch = !selCls || (c.__attrs["class"] === selCls);
            if ((isClassSel && classMatch) || (!isClassSel && (tagMatch || classMatch))) {
              found = c;
              return;
            }
            if (c.__children) findFirst(c.__children);
          }
        }
        findFirst(svgSel.__children);
        if (found) {
          found.__origin = svgSel;
          return found;
        }
      }
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

// Every match, not the first. The comms overlay appends one path per edge as
// a separate element rather than as one joined selection, so counting them is
// the only way to tell "drew three arcs" from "drew one".
function stubFindAllByClass(sel, cls) {
  var out = [];
  (function walk(node) {
    if (!node) return;
    if (node.__attrs && node.__attrs["class"] === cls) out.push(node);
    var kids = node.__children || [];
    for (var i = 0; i < kids.length; i++) walk(kids[i]);
  })(sel);
  return out;
}

function stubFindNodeSelection() {
  var vp = stubFindByClass(stubSvg(), "map-viewport");
  if (!vp) return null;
  var kids = vp.__children || [];
  for (var i = 0; i < kids.length; i++) {
    if (kids[i].__attrs && kids[i].__attrs["class"] === "map-nodes") {
      // Return the layer group so .each(), .filter(), .selectALL() and
      // __handlers all work on the full set of node data.
      // Populate __data from children if empty (the test pattern where
      // .data() was called on a selectAll result, not on the group itself).
      if (!kids[i].__data || !kids[i].__data.length || kids[i].__data[0] === false) {
        var nodeChildren = kids[i].__children || [];
        var nodeData = [];
        for (var j = 0; j < nodeChildren.length; j++) {
          if (nodeChildren[j].__data &&
              nodeChildren[j].__data !== true &&
              nodeChildren[j].__data !== false) {
            nodeData.push(nodeChildren[j].__data);
          }
        }
        kids[i].__data = nodeData;
      }
      return kids[i];
    }
  }
  // Fallback for direct children of viewport (older layout without layer groups).
  for (var i = 0; i < kids.length; i++) {
    if (kids[i].__attrs && kids[i].__attrs["class"] === "node") return kids[i];
  }
  return null;
}
