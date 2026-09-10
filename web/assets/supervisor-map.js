/** Supervisor Map — D3 horizontal (left-to-right) mind map renderer. */

// Every colour here is read from a CSS custom property, with the old literal
// kept as the fallback argument. The map was the one SVG in this app that
// ignored the theme: statuses, the neutral machine fill and every node outline
// were hardcoded, and the outline literal was `#fff` -- a white ring on a
// white panel in light mode, i.e. invisible on exactly the theme where it was
// most needed. Read per render rather than once at module load, because the
// theme toggle changes the variables under a page that is already loaded.
const STATUS_VAR = {
  running: "--map-running",
  busy: "--map-busy",
  waiting: "--map-waiting",
  idle: "--map-idle",
  error: "--map-error",
  done: "--map-done",
  transport: "--map-transport",
};
const STATUS_FALLBACK = {
  running: "#10b981",
  busy: "#f59e0b",
  waiting: "#f97316",
  idle: "#3b82f6",
  error: "#ef4444",
  done: "#6b7280",
  transport: "#9ca3af",
};

/** Zoom transition length, or 0 when the reader has asked for less motion.
 *
 *  styles.css already neutralises CSS transitions under
 *  prefers-reduced-motion, but d3's `.transition().duration(300)` animates
 *  attributes from JavaScript and that rule cannot reach it -- so the map's
 *  Fit, +/- and percentage buttons kept animating regardless. Reading the
 *  query per call rather than caching it: a reader can change the setting
 *  without reloading the page.
 */
function _motionMs(ms) {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : ms;
  } catch (_) {
    return ms;
  }
}

/** One theme variable, with a literal fallback for a stylesheet that has not
 *  loaded (or a test environment with no computed styles at all). */
function _cssVar(name, fallback) {
  try {
    const value = getComputedStyle(document.body).getPropertyValue(name);
    const trimmed = value ? value.trim() : "";
    return trimmed || fallback;
  } catch (_) {
    return fallback;
  }
}

function statusColor(status) {
  const key = STATUS_VAR[status] ? status : "idle";
  return _cssVar(STATUS_VAR[key], STATUS_FALLBACK[key]);
}
const STATUS_LABEL = {
  running: "Running",
  busy: "Busy",
  waiting: "Waiting",
  idle: "Idle",
  error: "Error",
  done: "Done",
  transport: "Transport",
};

// _tree is gone from this list: it held the layout only so that zoomToFit
// could call `_tree.bounds()`, which is not a d3 API and always threw. The
// layout is now a local, and the bounds are computed from _root's own nodes.
let _svg, _zoom, _data, _root, _viewport, _collapsed = new Set();
// The zoom transform in force, kept across re-renders so a background refresh
// does not reset the reader's view. Null means "no view chosen yet", which is
// what makes the first render after an open fit to the tree.
let _lastTransform = null;
// The node the detail drawer is currently describing, so its action buttons
// know what they act on.
let _detailNode = null;
// The viewBox dimensions the current render used. zoomToFit must reason in
// those units, not in whatever getBoundingClientRect reports now: the two agree
// at render time and diverge as soon as the panel is resized, and using the
// live pixel box against viewBox coordinates would fit the tree to the wrong
// rectangle.
let _canvas = {w: 800, h: 400};
// ── Layout geometry ────────────────────────────────────────────────────
// Module-level and shared by the render and by zoomToFit, because those two
// disagreeing was the bug this replaced: the render drew each node through the
// margins while zoomToFit measured raw `d.x`/`d.y`, so the fit centred a
// rectangle the map never drew and the whole tree sat (90, 40) layout units
// right-and-down of centre, scaled. One definition, used by both, is the only
// arrangement that cannot drift.
//
// Left-to-right: depth runs across, siblings run down. _nodeXY maps d.y (depth)
// to horizontal and d.x (sibling) to vertical, which is d3's standard swap for
// a horizontal tree.
//
// This was top-down until 2026-09-10, and the labels are what settle it. The
// label block below places text beside each node with `dy: "0.35em"` and
// `x: ±10` -- vertically centred, extending sideways -- and its own comment
// says "labels sit to the left of each node". That is the left-to-right idiom,
// and under a top-down layout it collides with itself: siblings sat 23px apart
// horizontally while a truncated 20-character label needs about 124px, so
// measured on a 12-task fan-out, 11 of 11 adjacent label pairs overlapped. The
// same tree left-to-right overlaps 0 of 11, because labels then stack down the
// axis that has room.
//
// The constants are measured, not chosen. Because zoomToFit normalises the
// whole tree into the panel, raising LEVEL_GAP barely widens the on-screen
// column (104px at 100 rising only to 117px at 180) while it shrinks the row
// gap (20.8px down to 13.0px) -- and 180 with SIBLING_GAP 20 collides
// outright. What matters is the ratio, not the magnitude. 120/24 gives the
// widest safe row gap at 21.7px against a 14px line box, holds that through a
// 30-way fan-out, and still clears 16.0px at 50 leaves.
const SIBLING_GAP = 24;   // nodeSize dx -> vertical: between sibling rows
const LEVEL_GAP = 120;    // nodeSize dy -> horizontal: between depth columns
const MARGIN_LEFT = 60;   // so the root circle clears the panel edge
const MARGIN_TOP = 40;
// zoomToFit's breathing room, module-level so a test can pin what NODE_EXTENT
// contributes independently of it. See test_the_fit_needs_no_padding_to_keep
// _ink_inside.
const FIT_PAD = 80;
// The largest distance a node's ink reaches from its own centre: r 6 plus the
// r+3 halo at depth 1, and r 8 at the root. zoomToFit fits the ink rather than
// the centres, because a bounds built from centres leaves the outermost circle
// half outside the viewBox and half a circle outside is what the browser test
// measures with getBoundingClientRect.
//
// FIT_PAD is 80, so it already leaves 40 units of slack each side and covers
// these 12 on its own: removing NODE_EXTENT changes not a single pixel at
// today's padding. That made it unfalsifiable at first -- a mutation removing
// it failed no test. What it is actually for is making the fit correct
// *independently* of FIT_PAD, so the guarantee is not "the padding happens to
// be generous", and that is now pinned by evaluating the module with
// FIT_PAD = 0, where these 12 units are the only slack there is. See
// test_the_fit_needs_no_padding_to_keep_ink_inside.
const NODE_EXTENT = 12;

/** Where a node is actually drawn, in viewBox units.
 *
 *  The one definition of that, deliberately. The render used to inline this
 *  expression in its transform while zoomToFit recomputed bounds from raw
 *  `d.x`/`d.y`, and the two silently described different pictures. Anything
 *  that needs to know where a node is calls this. */
function _nodeXY(d) {
  return {x: MARGIN_LEFT + d.y + LEVEL_GAP, y: MARGIN_TOP + d.x};
}
// Fallbacks only. The real canvas is measured from the SVG's own client box on
// every render (see _canvasSize): these applied when the panel was ~380x930,
// so the layout used a fraction of it and three of the tree's four quadrants
// fell outside the drawable area with no way to pan them back.
const WIDTH = 800;
const HEIGHT = 400;

/** The SVG's real pixel box, falling back to the fixed pair when unmeasurable
 *  (detached node, display:none panel, or a test with no layout engine). */
function _canvasSize(svgEl) {
  const box = svgEl && svgEl.getBoundingClientRect
    ? svgEl.getBoundingClientRect()
    : null;
  const w = box && box.width > 0 ? Math.round(box.width) : WIDTH;
  const h = box && box.height > 0 ? Math.round(box.height) : HEIGHT;
  return {w, h};
}

/** A one-line description of the whole figure, for the SVG's aria-label. */
function _mapSummary(data) {
  let nodes = 0;
  (function count(node) {
    nodes += 1;
    (node.children || []).forEach(count);
  })(data);
  const groups = (data.children || []).length;
  return `Supervisor map: ${groups} group${groups === 1 ? "" : "s"}, `
    + `${nodes - 1} node${nodes - 1 === 1 ? "" : "s"}`;
}

export function renderSupervisorMap(data) {
  _data = data;
  _root = null;
  // _collapsed deliberately survives this call. It used to be cleared here,
  // which made collapsing impossible rather than temporary: the click handler
  // records the id, mutates the node's children and re-renders, and the
  // re-render rebuilt the hierarchy from `data` with every child present and
  // an empty set to check against. So the only visible effect of collapsing a
  // node was a full redraw that changed nothing. closeSupervisorMap clears it.
  //
  // The current view survives too, for the same kind of reason: the map now
  // refreshes itself every few seconds, and re-fitting on each tick would
  // yank a zoomed-in reader back to the whole tree without being asked.
  const restoreTransform = _lastTransform;

  if (!data || !data.children || data.children.length === 0) {
    _svg = d3.select("#supervisorMapSvg");
    if (_svg && _svg.node()) {
      const {w, h} = _canvasSize(_svg.node());
      _svg.attr("viewBox", `0 0 ${w} ${h}`)
        .attr("preserveAspectRatio", "xMidYMid meet")
        .attr("role", "tree")
        .attr("aria-label", "Supervisor map: nothing running");
      _svg.selectAll("*").remove();
      _viewport = null;
      _zoom = d3.zoom().scaleExtent([0.2, 5]);
      _svg.call(_zoom);
    }
    const statusEl = document.getElementById("mapStatusEmpty");
    if (statusEl) statusEl.hidden = false;
    return;
  }

  // Hide empty-state message
  const statusEl = document.getElementById("mapStatusEmpty");
  if (statusEl) statusEl.hidden = true;

  _svg = d3.select("#supervisorMapSvg");
  const {w: CANVAS_W, h: CANVAS_H} = _canvasSize(_svg.node());
  // A viewBox is what ties the coordinate system to the rendered box. Without
  // one, `width:100%` CSS stretched the element while its interior stayed a
  // 400x400 space, so anything past 400 units was simply outside the drawable
  // area -- unreachable, because zoom did not work either (below).
  _svg.attr("viewBox", `0 0 ${CANVAS_W} ${CANVAS_H}`)
    .attr("preserveAspectRatio", "xMidYMid meet");
  // Without these the figure reaches a screen reader as a stack of unlabelled
  // groups with no statement of what it is. Each node already carries its own
  // aria-label; this names the whole thing and counts what is in it, which is
  // the part no individual node can say.
  _svg.attr("role", "tree")
    .attr("aria-label", _mapSummary(data));
  _canvas = {w: CANVAS_W, h: CANVAS_H};

  _svg.selectAll("*").remove();
  // Opaque background rect so the SVG captures all pointer events — prevents
  // underlying main-content elements (empty-state, chat messages) from stealing
  // clicks that land inside the SVG viewport. Sized to the measured box, not
  // to 400x400, or it covers only the top region of a tall panel.
  const bgFill = _cssVar("--panel2", "#fff");
  _svg.append("rect")
    .attr("class", "map-bg")
    .attr("x", 0).attr("y", 0)
    .attr("width", CANVAS_W).attr("height", CANVAS_H)
    .attr("fill", bgFill);

  // Everything except the background joins onto this group, and this group is
  // what zoom transforms. A `transform` on a root <svg> is not rendered, so
  // without a container there is nothing for a zoom handler to move -- which
  // is half of why zoom did nothing. The other half was that no
  // `.on("zoom", ...)` handler existed at all.
  _viewport = _svg.append("g").attr("class", "map-viewport");

  _zoom = d3.zoom()
    .scaleExtent([0.2, 5])
    .on("zoom", (event) => {
      // The line whose absence made every zoom control inert: wheel, drag,
      // Fit, +/- and the percentage buttons all mutate this transform, and
      // nothing else writes to the viewport's transform.
      _viewport.attr("transform", event.transform);
      _lastTransform = event.transform;
    });
  _svg.call(_zoom);

  const root = d3.hierarchy(data, d => d.children || []);
  // Re-apply what the user collapsed. This has to happen before the layout
  // runs: d3.tree() assigns positions to whatever is in `children` at the
  // time, so collapsing after the fact would leave gaps where the hidden
  // branches were.
  root.each(d => {
    if (d.children && _collapsed.has(d.data.id)) {
      d._children = d.children;
      d.children = null;
    }
  });

  // Fixed node spacing rather than fit-to-canvas: nodeSize([20, 30]) puts
  // 20px between siblings and 30px between levels, and the transform below
  // maps d.x to horizontal and d.y to vertical.
  //
  // This used to also call .size([CANVAS_H - verticalPadding, CANVAS_W -
  // horizontalGap]) immediately before .nodeSize(). Those are mutually
  // exclusive in d3-hierarchy -- one flag, two setters, both writing dx/dy,
  // and the later call decides how they are read -- so the .size() values
  // were overwritten and the canvas dimensions never reached the layout. The
  // rendered result has always been the nodeSize one; removing the dead call
  // changes nothing on screen and stops the code claiming otherwise. See
  // tests/test_qa_d3_stub_fidelity.py, which pins the exclusion so the stub
  // cannot hide a repeat of this.
  //
  // Fixed spacing is the decision, not a leftover: the map is zoomable and
  // pannable and zoomToFit() frames it, so the layout does not need to know
  // the canvas size. Fit-to-canvas was a requirement of the radial layout --
  // the radius had to come from the measured box -- and it does not survive
  // the change to horizontal. If it is ever wanted back, replace .nodeSize()
  // with .size(); never add one alongside the other, since d3 silently keeps
  // only the last of the two.
  const tree = d3.tree()
    .separation((a, b) => a.parent === b.parent ? 1 : 1.2)
    .nodeSize([SIBLING_GAP, LEVEL_GAP]);
  tree(root);
  _root = root;

  // Depth map: center=0, transport=1, machine=2, orchestrator=2, chat=3
  const node = _viewport.selectAll(".node")
    .data(root.descendants(), d => d.data.id)
    .join("g")
    .attr("class", "node")
    .attr("tabindex", "0")
    .attr("role", "button")
    // Horizontal: translate left, then down. _nodeXY is the single definition
    // of where a node sits, so zoomToFit measures the same points the render
    // draws.
    .attr("transform", d => {
      const {x, y} = _nodeXY(d);
      return `translate(${x}, ${y})`;
    })
    .attr("cursor", d =>
      (d.children || d._children || _hasDetail(d.data.type)) ? "pointer" : "default"
    )
    .attr("aria-label", d =>
      `${d.data.type || "node"} · ${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label || ""}`
    );

  // ── Node shapes (per-spec shapes) ───────────────────────────────
  // Center: large dark circle
  // Transport: medium ring (hollow)
  // Machine: medium filled circle (neutral)
  // Orchestrator: small filled circle with ring if expandable
  // Chat (leaf): small filled circle

  node.each(function(d) {
    const g = d3.select(this);
    const r = nodeRadius(d);
    const fillColor = statusColor(d.data.status);
    const outline = _cssVar("--map-outline", "#fff");
    const neutral = _cssVar("--map-machine", "#9ca3af");

    // Glow ring for expandable nodes
    if (d._children || d.children) {
      g.append("circle")
        .attr("r", r + 3)
        .attr("fill", "none")
        .attr("stroke", outline)
        .attr("stroke-width", 1.5)
        .attr("stroke-dasharray", "2 2");
    }

    // Main circle
    if (d.depth === 0) {
      // Center node
      g.append("circle")
        .attr("r", 8)
        .attr("fill", _cssVar("--map-label", "#1a1a2e"))
        .attr("stroke", statusColor("running"))
        .attr("stroke-width", 2);
    } else if (d.data.type === "transport") {
      // Transport: medium ring
      g.append("circle")
        .attr("r", r)
        .attr("fill", "none")
        .attr("stroke", fillColor)
        .attr("stroke-width", 2);
    } else if (d.data.type === "machine") {
      // Machine: medium filled neutral
      g.append("circle")
        .attr("r", r)
        .attr("fill", neutral)
        .attr("stroke", outline)
        .attr("stroke-width", 1);
    } else if (d.data.type === "more") {
      // Overflow marker: hollow and dashed, so it does not read as a thing
      // that is running. It reports how many nodes the server left out of
      // this group; there is nothing behind it to open.
      g.append("circle")
        .attr("r", r)
        .attr("fill", "none")
        .attr("stroke", neutral)
        .attr("stroke-width", 1)
        .attr("stroke-dasharray", "2 2");
    } else if (d.data.type === "session") {
      // Terminal session: square, the one node kind that is not a circle.
      // These are shells a person is sitting at, not work the console
      // started, and telling them apart at a glance is the point of the map.
      g.append("rect")
        .attr("x", -r).attr("y", -r)
        .attr("width", r * 2).attr("height", r * 2)
        .attr("fill", fillColor)
        .attr("stroke", outline)
        .attr("stroke-width", 1);
    } else {
      // Orchestrator or chat: small filled
      g.append("circle")
        .attr("r", r)
        .attr("fill", fillColor)
        .attr("stroke", d._children || d.children ? outline : "none")
        .attr("stroke-width", 1);
    }
  });

  // ── Labels (right-aligned, left of the node) ────────────────────
  // Horizontal layout: labels sit to the left of each node (or right for the
  // root's first child, which starts at the far left edge). Labels are
  // right-aligned so the node sits naturally to the right of the text.
  node.filter(d => d.depth > 0)
    .append("text")
    .attr("dy", "0.35em")
    .attr("x", d => (d.children ? 10 : -10))
    .attr("text-anchor", d => (d.children ? "start" : "end"))
    .text(d => {
      const name = d.data.label || "";
      return name.length > 20 ? name.slice(0, 18) + "…" : name;
    })
    .attr("font-size", "11px")
    .attr("fill", () => _cssVar("--map-label", "#1a1a2e"));

  // ── Tooltip (hover) ─────────────────────────────────────────────
  node.on("mouseenter", function(event, d) {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) {
      let text = `${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label}`;
      // Center node: show operational stats, not just label
      if (d.depth === 0) {
        const totalChildren = (d.children?.length || 0) + (d._children?.length || 0);
        text += ` · ${totalChildren} group${totalChildren !== 1 ? 's' : ''}`;
        // Count machines, not groups that contain one. This filtered the
        // root's direct children and took `.length`, so three backends behind
        // one transport reported "1 machine" -- a number that is wrong in the
        // direction that matters, since underreporting capacity is the thing
        // this tooltip exists to avoid.
        let machineCount = 0;
        (function countMachines(node) {
          if (!node) return;
          if (node.data && node.data.type === "machine") machineCount += 1;
          // Both lists: a collapsed branch's machines are still there, and a
          // count that changed when the user collapsed something would look
          // like the map losing track of the fleet.
          (node.children || []).forEach(countMachines);
          (node._children || []).forEach(countMachines);
        })(d);
        if (machineCount) text += ` · ${machineCount} machine${machineCount !== 1 ? 's' : ''}`;
      }
      tooltip.textContent = text;
      tooltip.hidden = false;
      tooltip.style.left = event.pageX + 10 + "px";
      tooltip.style.top = event.pageY - 28 + "px";
    }
  }).on("mouseleave", function() {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) tooltip.hidden = true;
  });

  // ── Click handler ───────────────────────────────────────────────
  node.on("click", function(event, d) {
    event.stopPropagation();
    // Center node: tooltip on click (no drawer, no expand/collapse)
    if (d.depth === 0) return;
    // Expand/collapse for nodes with children
    if (d.children || d._children) {
      if (d.children) {
        d._children = d.children;
        d.children = null;
        _collapsed.add(d.data.id);
      } else {
        d.children = d._children;
        d._children = null;
        _collapsed.delete(d.data.id);
      }
      renderSupervisorMap(_data);
    } else if (_hasDetail(d.data.type)) {
      // Leaf nodes: open detail drawer (Fix 2)
      showDetail(d.data);
    }
  });

  // ── Keyboard accessibility (Fix 1) ──────────────────────────────
  node.on("keydown", function(event, d) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      if (d.children || d._children) {
        // Toggle expand/collapse on Enter/Space
        if (d.children) {
          d._children = d.children;
          d.children = null;
          _collapsed.add(d.data.id);
        } else {
          d.children = d._children;
          d._children = null;
          _collapsed.delete(d.data.id);
        }
        renderSupervisorMap(_data);
      } else if (_hasDetail(d.data.type)) {
        showDetail(d.data);
      }
    }
  });

  // ── Click empty SVG background to close drawer (Fix 5) ──────────
  _svg.on("click", function(event) {
    // The background rect counts as background: it is painted over the whole
    // viewport, so it -- not the <svg> -- is the click target for every empty
    // spot on the map, and testing only for the svg element meant this handler
    // could never fire once that rect existed.
    const target = event.target;
    const cls = target && target.getAttribute ? target.getAttribute("class") : null;
    if (target === this || (target && target.tagName === "svg") || cls === "map-bg") {
      hideDetail();
    }
  });

  // Fit on the first render of an open panel; restore the reader's own view
  // on every refresh after that.
  if (restoreTransform) {
    _svg.call(_zoom.transform, restoreTransform);
  } else {
    zoomToFit();
  }
}

/** Node kinds with something to show in the drawer. "more" is excluded on
 *  purpose: the nodes it stands for are not in this response, so opening it
 *  could only show the count already printed on its label. */
function _hasDetail(type) {
  return type === "chat" || type === "machine" || type === "task"
    || type === "session";
}

function nodeRadius(d) {
  if (d.depth === 1) return 6;        // transport
  if (d.data.type === "machine") return 5;
  if (d.data.type === "session") return 5;
  if (d.data.type === "more") return 6;
  return 4;                           // orchestrator, chat or task
}

// ── Detail drawer (Fix 2: actual data) ─────────────────────────────────
async function showDetail(nodeData) {
  const drawer = document.getElementById("mapDetailDrawer");
  if (!drawer) return;

  document.getElementById("mapDetailTitle").textContent = nodeData.label;
  const statusEl = document.getElementById("mapDetailStatus");
  statusEl.textContent = STATUS_LABEL[nodeData.status] || nodeData.status;
  statusEl.style.color = statusColor(nodeData.status);

  // Fill detail fields from node metadata
  const metaEl = document.getElementById("mapDetailMeta");
  let metaParts = [`Type: ${nodeData.type}`];
  if (nodeData.machine_label) metaParts.push(`Machine: ${nodeData.machine_label}`);
  // Only nodes backed by this host carry these -- an SSH-proxied machine is a
  // different host's memory, which the backend deliberately does not measure
  // here, so its absence is not an error to paper over with a placeholder.
  if (nodeData.capacity_total !== undefined) {
    metaParts.push(
      nodeData.capacity_total === null
        ? "Capacity: could not be measured"
        : `Capacity: ${nodeData.capacity_existing} / ${nodeData.capacity_total} agents`
    );
  }
  // Three states the tree carries and the drawer used to drop on the floor.
  // Each is the reason a node's colour is what it is, so a drawer that omits
  // them leaves the user guessing why a conversation is red.
  if (nodeData.degraded_reason) {
    metaParts.push(`Degraded: ${nodeData.degraded_reason}`);
  }
  if (nodeData.queued) {
    metaParts.push(`Queued: ${nodeData.queued} prompt${nodeData.queued === 1 ? "" : "s"}`);
  }
  if (nodeData.voice_mode) metaParts.push("Voice mode");
  metaParts.push(`ID: ${nodeData.id}`);
  metaEl.textContent = metaParts.join(" · ");

  // Last message: fetch from chat last message if available
  const msgEl = document.getElementById("mapDetailMessage");
  if (nodeData.last_message) {
    const content = nodeData.last_message;
    msgEl.textContent = content.length > 200
      ? content.slice(0, 197) + "…"
      : content;
    msgEl.hidden = false;
  } else if (nodeData.updated_at) {
    msgEl.textContent = `Last updated: ${nodeData.updated_at}`;
    msgEl.hidden = false;
  } else if (nodeData.type === "chat") {
    msgEl.textContent = "No recent activity.";
    msgEl.hidden = false;
  } else {
    msgEl.textContent = "";
    msgEl.hidden = true;
  }

  _detailNode = nodeData;
  _renderDetailActions(nodeData);
  drawer.hidden = false;
  _trapFocus(drawer);
}

// ── Drawer focus ───────────────────────────────────────────────────────
// The drawer opened with focus left wherever it was -- on the SVG node, or
// nowhere at all after a mouse click -- so a keyboard reader had to Tab
// forwards through the whole map to reach a drawer that had just appeared in
// front of them, and Tab from inside it walked straight back out into the map
// behind. Escape was already handled; this is the rest of it.
let _drawerReturnFocus = null;

function _drawerFocusables(drawer) {
  return Array.from(
    drawer.querySelectorAll("button, [href], input, select, textarea, [tabindex]")
  ).filter(el => !el.hidden && el.tabIndex !== -1 && !el.disabled);
}

function _trapFocus(drawer) {
  _drawerReturnFocus = document.activeElement;
  const focusable = _drawerFocusables(drawer);
  if (focusable.length) focusable[0].focus();
}

document.getElementById("mapDetailDrawer")?.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const drawer = document.getElementById("mapDetailDrawer");
  if (!drawer || drawer.hidden) return;
  const focusable = _drawerFocusables(drawer);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  // Only the two edges are redirected. Tabbing between the drawer's own
  // controls is left to the browser, which already does it correctly.
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

/** Show only the actions that apply to this node kind, and say plainly why
 *  the drawer is otherwise read-only: a map that can report a stuck turn but
 *  not reach it or stop it makes the user find the conversation by hand. */
function _renderDetailActions(nodeData) {
  const openBtn = document.getElementById("mapDetailOpen");
  const stopBtn = document.getElementById("mapDetailStop");
  const status = document.getElementById("mapDetailActionStatus");
  if (status) { status.textContent = ""; status.hidden = true; }
  // Only a conversation has a conversation to open. A task's id is a task id,
  // a machine is not a conversation, and a terminal session lives in a
  // terminal -- offering "Open" for any of them would 404 or do nothing.
  if (openBtn) openBtn.hidden = nodeData.type !== "chat";
  // Stop is offered only where there is something to stop. The endpoint is
  // per-conversation, so a running task is not stoppable from here either.
  const busy = nodeData.status === "running" || nodeData.status === "busy";
  if (stopBtn) {
    stopBtn.hidden = !(nodeData.type === "chat" && busy);
    stopBtn.disabled = false;
    stopBtn.textContent = "Stop";
  }
}

function _setActionStatus(text) {
  const status = document.getElementById("mapDetailActionStatus");
  if (!status) return;
  status.textContent = text;
  status.hidden = !text;
}

document.getElementById("mapDetailOpen")?.addEventListener("click", () => {
  if (!_detailNode || _detailNode.type !== "chat") return;
  // Dispatched rather than imported: app.js imports this module, so importing
  // its selectChat back would be a cycle.
  document.dispatchEvent(new CustomEvent("wc:map-open-chat", {
    detail: {id: _detailNode.id, type: _detailNode.type},
  }));
});

document.getElementById("mapDetailStop")?.addEventListener("click", async () => {
  const node = _detailNode;
  if (!node || node.type !== "chat") return;
  const btn = document.getElementById("mapDetailStop");
  if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
  try {
    const res = await fetch(`/api/chats/${encodeURIComponent(node.id)}/stop`, {
      method: "POST", credentials: "same-origin",
    });
    if (!res.ok) throw new Error(String(res.status));
    const body = await res.json();
    // "stopped: false" is not a failure -- the turn finished between the map
    // being drawn and the button being pressed. Saying "Stopped" there would
    // claim an action that did not happen.
    _setActionStatus(body.stopped ? "Stopped." : "Nothing was running.");
    if (btn) btn.hidden = true;
  } catch {
    _setActionStatus("Could not stop it. Try again.");
    if (btn) { btn.disabled = false; btn.textContent = "Stop"; }
  }
});

function hideDetail() {
  const drawer = document.getElementById("mapDetailDrawer");
  if (drawer) drawer.hidden = true;
  _detailNode = null;
  // Back where they were, not to the top of the document: closing a drawer
  // should leave a keyboard reader on the node they were reading about.
  if (_drawerReturnFocus && typeof _drawerReturnFocus.focus === "function"
      && document.body && document.body.contains
      && document.body.contains(_drawerReturnFocus)) {
    _drawerReturnFocus.focus();
  }
  _drawerReturnFocus = null;
}

export function closeSupervisorMap() {
  if (_svg) _svg.selectAll("*").remove();
  _viewport = null;  // removed above; keep the handle from outliving the node
  hideDetail();
  _data = null;
  _collapsed.clear();
  // Closing the panel is the one place a view is deliberately forgotten:
  // reopening the map should fit the tree, not restore a zoom from earlier.
  _lastTransform = null;
}

export function zoomToFit() {
  if (!_svg || !_root) return;
  try {
    // Measured through _nodeXY, so these are the points the render actually
    // draws. This used to read raw d.x/d.y while the transform added
    // MARGIN_LEFT + LEVEL_GAP and MARGIN_TOP, so the fit centred a rectangle
    // offset by (90, 40) from the real one and the tree sat that far
    // right-and-down of centre -- (90*scale, 40*scale) on screen. At the
    // scales this hits in a real panel that is a few pixels, which is why it
    // read as "a node is 9px outside the box" rather than as an obvious
    // misalignment.
    //
    // NODE_EXTENT grows the bounds to the edge of a node's ink rather than its
    // centre. Fitting centres leaves the outermost circle half outside the
    // viewBox, and half a circle outside is exactly what the browser test
    // measures with getBoundingClientRect.
    const nodes = _root.descendants();
    if (!nodes.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const d of nodes) {
      const {x, y} = _nodeXY(d);
      minX = Math.min(minX, x - NODE_EXTENT);
      maxX = Math.max(maxX, x + NODE_EXTENT);
      minY = Math.min(minY, y - NODE_EXTENT);
      maxY = Math.max(maxY, y + NODE_EXTENT);
    }
    // Add a padding around the tree so nodes don't touch the viewBox edge.
    const width = maxX - minX + FIT_PAD;
    const height = maxY - minY + FIT_PAD;
    if (width <= 0 || height <= 0) return;
    const box = _canvas;
    const scale = Math.min(box.w / width, box.h / height, 2);
    const tx = box.w / 2 - ((minX + maxX) / 2) * scale;
    const ty = box.h / 2 - ((minY + maxY) / 2) * scale;
    _svg.transition().duration(_motionMs(300))
      .call(_zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  } catch (_) {
    // Graceful degrade: scroll into view if zoom-to-fit fails
    const svgEl = document.getElementById("supervisorMapSvg");
    if (svgEl) svgEl.scrollIntoView({ behavior: "smooth" });
  }
}

// Close button
document.getElementById("supervisorMapClose")
  ?.addEventListener("click", () => {
    closeSupervisorMap();
    const panel = document.getElementById("supervisorMapPanel");
    if (panel) panel.hidden = true;
  });

// Detail close
document.getElementById("mapDetailClose")?.addEventListener("click", hideDetail);

// Zoom buttons: fit-to-view plus the continuous in/out pair. The five
// explicit-percentage presets (50/100/150/200/500%) were removed at Pedro's
// request -- fit-to-view and +/- cover the same range without a five-button
// row, and scaleTo/scaleBy below are what those presets called anyway.
document.getElementById("mapFitBtn")?.addEventListener("click", zoomToFit);
document.getElementById("mapZoomInBtn")?.addEventListener("click", () => {
  if (_svg) _svg.transition().call(_zoom.scaleBy, 1.5);
});
document.getElementById("mapZoomOutBtn")?.addEventListener("click", () => {
  if (_svg) _svg.transition().call(_zoom.scaleBy, 0.75);
});

// Keyboard: Escape closes detail or map
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const drawer = document.getElementById("mapDetailDrawer");
    if (drawer && !drawer.hidden) {
      e.preventDefault();
      hideDetail();
      return;
    }
    const panel = document.getElementById("supervisorMapPanel");
    if (panel && !panel.hidden) {
      e.preventDefault();
      closeSupervisorMap();
      panel.hidden = true;
    }
  }
});

export default { renderSupervisorMap, closeSupervisorMap, zoomToFit };
