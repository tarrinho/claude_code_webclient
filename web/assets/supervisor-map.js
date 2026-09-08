/** Supervisor Map — D3 radial mind map renderer. */
const STATUS_COLOR = {
  running: "#10b981",
  busy: "#f59e0b",
  waiting: "#f97316",
  idle: "#3b82f6",
  error: "#ef4444",
  done: "#6b7280",
  transport: "#9ca3af",
};
const STATUS_LABEL = {
  running: "Running",
  busy: "Busy",
  waiting: "Waiting",
  idle: "Idle",
  error: "Error",
  done: "Done",
  transport: "Transport",
};

let _svg, _zoom, _data, _tree, _root, _viewport, _collapsed = new Set();
// The viewBox dimensions the current render used. zoomToFit must reason in
// those units, not in whatever getBoundingClientRect reports now: the two agree
// at render time and diverge as soon as the panel is resized, and using the
// live pixel box against viewBox coordinates would fit the tree to the wrong
// rectangle.
let _canvas = {w: 400, h: 400};
// Fallbacks only. The real canvas is measured from the SVG's own client box on
// every render (see _canvasSize): these applied when the panel was ~380x930,
// so the layout used a fraction of it and three of the tree's four quadrants
// fell outside the drawable area with no way to pan them back.
const WIDTH = 400;
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

export function renderSupervisorMap(data) {
  _data = data;
  _root = null;
  _collapsed = new Set();  // clear stale collapsed state on each render

  if (!data || !data.children || data.children.length === 0) {
    _svg = d3.select("#supervisorMapSvg");
    if (_svg && _svg.node()) {
      const {w, h} = _canvasSize(_svg.node());
      _svg.attr("viewBox", `0 0 ${w} ${h}`)
        .attr("preserveAspectRatio", "xMidYMid meet");
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
  _canvas = {w: CANVAS_W, h: CANVAS_H};

  _svg.selectAll("*").remove();
  // Opaque background rect so the SVG captures all pointer events — prevents
  // underlying main-content elements (empty-state, chat messages) from stealing
  // clicks that land inside the SVG viewport. Sized to the measured box, not
  // to 400x400, or it covers only the top region of a tall panel.
  const bgFill = getComputedStyle(document.body).getPropertyValue("--panel2").trim() || "#fff";
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
  //
  // The centering translate is the other half of the geometry fix: the tree
  // layout is polar about (0,0), and the root used to be special-cased to
  // `translate(200,200)` while its own children radiated from the corner.
  // Centering the whole group instead puts root and descendants in one
  // coordinate system, which is also what zoomToFit's math already assumed.
  _viewport = _svg.append("g").attr("class", "map-viewport");

  _zoom = d3.zoom()
    .scaleExtent([0.2, 5])
    .on("zoom", (event) => {
      // The line whose absence made every zoom control inert: wheel, drag,
      // Fit, +/- and the percentage buttons all mutate this transform, and
      // nothing else writes to the viewport's transform.
      _viewport.attr("transform", event.transform);
    });
  _svg.call(_zoom);
  // Centring is expressed as the initial zoom transform rather than as a fixed
  // translate on the group, so there is exactly one thing positioning the tree.
  // Baking translate(w/2,h/2) into the group as well would apply the centring
  // twice, because zoomToFit computes absolute tx/ty from the box centre.
  _svg.call(_zoom.transform, d3.zoomIdentity.translate(CANVAS_W / 2, CANVAS_H / 2));

  const root = d3.hierarchy(data, d => d.children || []);
  root.x0 = 0;
  root.y0 = 0;

  // .size([2*PI, RADIUS]) is not optional for a radial layout: d3.tree()
  // defaults to size([1, 1]) absent this call, so every node's angle (d.x)
  // and radius (d.y) were fractions between 0 and 1 -- the whole tree
  // rendered as a cluster within about one square pixel near the SVG's
  // origin instead of spreading across the panel. Confirmed in a real
  // browser: a machine node's own transform read
  // `rotate(-61.35deg) translate(1,0)`, i.e. radius 1 (one CSS pixel, since
  // this SVG carries no viewBox), which is what made it unreliable to click
  // -- Playwright resolved the element correctly, but its rendered footprint
  // was too small to reliably beat whatever painted behind the SVG at that
  // exact pixel.
  // Derived from the measured box, so a tall panel spreads the tree across it
  // instead of over a 400x400 corner. The 40 leaves room for leaf labels,
  // which are drawn outside their node's radius.
  const RADIUS = Math.max(40, Math.min(CANVAS_W, CANVAS_H) / 2 - 40);
  _tree = d3.tree()
    .size([2 * Math.PI, RADIUS])
    .separation((a, b) => a.parent === b.parent ? 1 : 1.2);
  _tree(root);
  _root = root;

  // Depth map: center=0, transport=1, machine=2, orchestrator=2, chat=3
  const node = _viewport.selectAll(".node")
    .data(root.descendants(), d => d.data.id)
    .join("g")
    .attr("class", "node")
    .attr("tabindex", "0")
    .attr("role", "button")
    // One formula for every node, root included. The root used to be
    // special-cased to translate(200,200) -- a hardcoded point in the SVG's
    // own coordinates -- while its children were placed polar about (0,0), so
    // the centre circle sat detached from the tree radiating out of the
    // corner. The root's radius (d.y) is 0, so this formula already puts it at
    // the group's origin; centring is the group's job, not the node's.
    .attr("transform", d => `rotate(${d.x * 180 / Math.PI - 90}) translate(${d.y},0)`)
    .attr("cursor", d =>
      (d.children || d._children || _hasDetail(d.data.type)) ? "pointer" : "default"
    )
    .attr("aria-label", d =>
      `${d.data.type || "node"} · ${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label || ""}`
    );

  // ── Node shapes (Fix 4: per-spec shapes) ────────────────────────
  // Center: large dark circle
  // Transport: medium ring (hollow)
  // Machine: medium filled circle (neutral)
  // Orchestrator: small filled circle with ring if expandable
  // Chat (leaf): small filled circle

  node.each(function(d) {
    const g = d3.select(this);
    const r = nodeRadius(d);
    const fillColor = STATUS_COLOR[d.data.status] || STATUS_COLOR.idle;

    // Glow ring for expandable nodes
    if (d._children || d.children) {
      g.append("circle")
        .attr("r", r + 3)
        .attr("fill", "none")
        .attr("stroke", "#fff")
        .attr("stroke-width", 1.5)
        .attr("stroke-dasharray", "2 2");
    }

    // Main circle
    if (d.depth === 0) {
      // Center node
      g.append("circle")
        .attr("r", 8)
        .attr("fill", getComputedStyle(document.body).getPropertyValue("--fg").trim() || "#1a1a2e")
        .attr("stroke", STATUS_COLOR.running)
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
        .attr("fill", "#9ca3af")
        .attr("stroke", "#fff")
        .attr("stroke-width", 1);
    } else if (d.data.type === "more") {
      // Overflow marker: hollow and dashed, so it does not read as a thing
      // that is running. It reports how many nodes the server left out of
      // this group; there is nothing behind it to open.
      g.append("circle")
        .attr("r", r)
        .attr("fill", "none")
        .attr("stroke", "#9ca3af")
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
        .attr("stroke", "#fff")
        .attr("stroke-width", 1);
    } else {
      // Orchestrator or chat: small filled
      g.append("circle")
        .attr("r", r)
        .attr("fill", fillColor)
        .attr("stroke", d._children || d.children ? "#fff" : "none")
        .attr("stroke-width", 1);
    }
  });

  // Labels (skip center node)
  node.filter(d => d.depth > 0)
    .append("text")
    .attr("dy", "0.35em")
    .attr("x", d => d.children ? 10 : -10)
    .attr("text-anchor", d => d.children ? "start" : "end")
    .text(d => {
      const name = d.data.label || "";
      return name.length > 20 ? name.slice(0, 18) + "…" : name;
    })
    .attr("font-size", "11px")
    .attr("fill", d => {
      const bg = getComputedStyle(document.body).getPropertyValue("--fg").trim();
      return bg || "#1a1a2e";
    });

  // ── Tooltip (hover) ─────────────────────────────────────────────
  node.on("mouseenter", function(event, d) {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) {
      let text = `${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label}`;
      // Center node: show operational stats, not just label
      if (d.depth === 0) {
        const totalChildren = (d.children?.length || 0) + (d._children?.length || 0);
        text += ` · ${totalChildren} group${totalChildren !== 1 ? 's' : ''}`;
        const machineCount = (d.children || d._children || [])
          .filter(c => c.data.type === 'machine' || c.children?.some?.(gc => gc.data.type === 'machine'))
          .length;
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

  // Zoom to fit after render
  zoomToFit();
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
  statusEl.style.color = STATUS_COLOR[nodeData.status] || STATUS_COLOR.idle;

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

  drawer.hidden = false;
}

function hideDetail() {
  const drawer = document.getElementById("mapDetailDrawer");
  if (drawer) drawer.hidden = true;
}

export function closeSupervisorMap() {
  if (_svg) _svg.selectAll("*").remove();
  _viewport = null;  // removed above; keep the handle from outliving the node
  hideDetail();
  _data = null;
  _collapsed.clear();
}

export function zoomToFit() {
  if (!_svg || !_root) return;
  try {
    // d3.tree()'s layout API has no .bounds() -- calling it always threw,
    // which sent every call straight to the scrollIntoView fallback below and
    // meant zoom-to-fit had never actually run. The real bounds have to come
    // from converting each node's polar position (angle=d.x, radius=d.y) to
    // the Cartesian point it is actually rendered at, matching the node
    // transform above exactly: `rotate(d.x*180/PI - 90) translate(d.y,0)` is
    // equivalent to plotting (d.y*cos(theta), d.y*sin(theta)) with
    // theta = d.x - PI/2.
    const nodes = _root.descendants();
    if (!nodes.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const d of nodes) {
      const theta = d.x - Math.PI / 2;
      const px = d.y * Math.cos(theta);
      const py = d.y * Math.sin(theta);
      minX = Math.min(minX, px); maxX = Math.max(maxX, px);
      minY = Math.min(minY, py); maxY = Math.max(maxY, py);
    }
    const width = maxX - minX + 80;
    const height = maxY - minY + 80;
    if (width <= 0 || height <= 0) return;
    const box = _canvas;
    const scale = Math.min(box.w / width, box.h / height, 2);
    const tx = box.w / 2 - ((minX + maxX) / 2) * scale;
    const ty = box.h / 2 - ((minY + maxY) / 2) * scale;
    _svg.transition().duration(300)
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

// Zoom buttons (Fix 3: full set)
document.getElementById("mapFitBtn")?.addEventListener("click", zoomToFit);
// Explicit zoom percentage buttons
document.querySelectorAll(".mapZoomPct").forEach(btn => {
  btn.addEventListener("click", () => {
    if (!_svg || !_zoom) return;
    const target = parseFloat(btn.dataset.zoom);
    if (!(target >= 0.2 && target <= 5)) return;
    // data-zoom is an absolute zoom level ("50%", "100%"), so scaleTo is the
    // right call: it sets k to the target and keeps the viewport's centre
    // fixed. The previous arithmetic multiplied a translation already
    // expressed in scaled pixels (currentTransform.x) by the target scale and
    // subtracted it from the box centre, which is not a meaningful quantity --
    // pressing 100% from a panned view threw the map off-screen.
    _svg.transition().duration(300).call(_zoom.scaleTo, target);
  });
});
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
