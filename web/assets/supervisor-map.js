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

let _svg, _zoom, _data, _collapsed = new Set();
const WIDTH = 400;
const HEIGHT = 400;
const RADIUS = Math.min(WIDTH, HEIGHT) / 2 - 50;

export function renderSupervisorMap(data) {
  _data = data;

  if (!data || !data.children || data.children.length === 0) {
    // Always create the SVG element so the DOM query for it succeeds.
    _svg = d3.select("#supervisorMapSvg")
      .attr("width", WIDTH)
      .attr("height", HEIGHT);
    if (!_svg || _svg.node() === null) {
      // The SVG element doesn't exist in DOM yet — this shouldn't happen
      // but guard anyway.
      const statusEl = document.getElementById("mapStatusEmpty");
      if (statusEl) statusEl.hidden = false;
    } else {
      _svg.selectAll("*").remove();
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

  _svg = d3.select("#supervisorMapSvg")
    .attr("width", WIDTH)
    .attr("height", HEIGHT);

  _svg.selectAll("*").remove();
  _zoom = d3.zoom().scaleExtent([0.2, 5]);
  _svg.call(_zoom);

  const root = d3.hierarchy(data, d => d.children || []);
  root.x0 = 0;
  root.y0 = 0;

  _tree = d3.tree().separation((a, b) => a.parent === b.parent ? 1 : 1.2);
  _tree(root);

  const node = _svg.selectAll(".node")
    .data(root.descendants(), d => d.data.id)
    .join("g")
    .attr("class", "node")
    .attr("transform", d => `rotate(${d.x * 180 / Math.PI - 90}) translate(${d.y},0)`)
    .attr("cursor", d => d.children || d._children ? "pointer" : "default");

  node.append("circle")
    .attr("r", d => d.depth === 0 ? 8 : d.depth === 1 ? 6 : 4)
    .attr("fill", d => STATUS_COLOR[d.data.status] || STATUS_COLOR.idle)
    .attr("stroke", d => d._children || d.children ? "#fff" : "none")
    .attr("stroke-width", 2);

  node.append("text")
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

  node.on("mouseenter", function(event, d) {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) {
      tooltip.textContent = `${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label}`;
      tooltip.hidden = false;
      tooltip.style.left = event.pageX + 10 + "px";
      tooltip.style.top = event.pageY - 28 + "px";
    }
  }).on("mouseleave", function() {
    const tooltip = document.getElementById("mapTooltip");
    if (tooltip) tooltip.hidden = true;
  });

  node.on("click", function(event, d) {
    event.stopPropagation();
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
    } else if (d.data.type === "chat") {
      showDetail(d.data);
    }
  }).attr("aria-label", d =>
    `${d.data.type} · ${STATUS_LABEL[d.data.status] || d.data.status} · ${d.data.label}`
  );

  _svg.on("click", function(event) {
    if (event.target === this) hideDetail();
  });
}

function showDetail(nodeData) {
  const drawer = document.getElementById("mapDetailDrawer");
  if (!drawer) return;
  document.getElementById("mapDetailTitle").textContent = nodeData.label;
  const statusEl = document.getElementById("mapDetailStatus");
  statusEl.textContent = STATUS_LABEL[nodeData.status] || nodeData.status;
  statusEl.style.color = STATUS_COLOR[nodeData.status] || STATUS_COLOR.idle;
  const msgEl = document.getElementById("mapDetailMessage");
  msgEl.textContent = nodeData.type === "chat"
    ? "Click a chat to see last message (future)."
    : "";
  const metaEl = document.getElementById("mapDetailMeta");
  metaEl.textContent = `Type: ${nodeData.type} · ID: ${nodeData.id}`;
  drawer.hidden = false;
}

function hideDetail() {
  const drawer = document.getElementById("mapDetailDrawer");
  if (drawer) drawer.hidden = true;
}

export function closeSupervisorMap() {
  if (_svg) _svg.selectAll("*").remove();
  hideDetail();
  _data = null;
  _collapsed.clear();
}

export function zoomToFit() {
  if (!_svg) return;
  const svgEl = document.getElementById("supervisorMapSvg");
  if (svgEl) svgEl.scrollIntoView({ behavior: "smooth" });
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

// Zoom buttons
document.getElementById("mapFitBtn")?.addEventListener("click", zoomToFit);
document.getElementById("mapZoomInBtn")?.addEventListener("click", () => {
  if (_svg) _svg.transition().call(_zoom.scaleBy, 1.5);
});
document.getElementById("mapZoomOutBtn")?.addEventListener("click", () => {
  if (_svg) _svg.transition().call(_zoom.scaleBy, 0.75);
});

export default { renderSupervisorMap, closeSupervisorMap, zoomToFit };
