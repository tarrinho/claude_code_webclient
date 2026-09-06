// supervisor/rail.js — dependency-ordered overview of the task DAG.
//
// Additive: the flat #task-tree list (tasks.js) stays exactly as it is for
// per-task detail and click-to-expand. This is an overview strip above it.

import { el } from "./dom.js";

// `depends_on` arrives from the API as a JSON-encoded string, not an array --
// db_supervisors.supervisor_task_create stores it via json.dumps(deps or []),
// so every task's column is literally the string "[]" at minimum, never SQL
// NULL and never a real array. `(t.depends_on || [])` does not catch that: a
// truthy string still reaches `.every`, which a string does not have.
// Confirmed against the live database: every existing row is text, not an
// array. tasks.js hits the identical shape and keeps its own copy of this
// parse rather than importing it from here, for the same reason this module
// does not import from tasks.js: the two do not share a direction that avoids
// a cycle, and ES modules do not share scope across files regardless.
function _dependsOnList(value) {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string" || !value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** Topologically layer *tasks* by depends_on. Pure, DOM-free, unit-testable.
 *
 * A task lands in the earliest layer every one of its dependencies has
 * already cleared. A dependency cycle should never reach here -- PlanParser
 * (supervisor.py) excludes self-references -- but must not hang the UI if
 * one somehow does: anything still unplaced after `tasks.length` passes is
 * dumped into one final "unresolved" layer rather than looping forever.
 */
export function computeLayers(tasks) {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const placed = new Map(); // id -> layer index
  const layers = [];
  let remaining = tasks.slice();
  let pass = 0;
  while (remaining.length && pass <= tasks.length) {
    const ready = remaining.filter((t) =>
      _dependsOnList(t.depends_on).every((d) => !byId.has(d) || placed.has(d))
    );
    if (!ready.length) break; // cycle or missing dep -- fall through below
    const layerIndex = layers.length;
    layers.push(ready);
    ready.forEach((t) => placed.set(t.id, layerIndex));
    const readyIds = new Set(ready.map((t) => t.id));
    remaining = remaining.filter((t) => !readyIds.has(t.id));
    pass += 1;
  }
  if (remaining.length) layers.push(remaining); // unresolved: cycle safety
  return layers;
}

export function renderRail(tasks) {
  if (!el.taskRail) return;
  if (!tasks.length) {
    el.taskRail.replaceChildren();
    el.taskRail.hidden = true;
    return;
  }
  el.taskRail.hidden = false;
  const layers = computeLayers(tasks);
  el.taskRail.replaceChildren();
  layers.forEach((layer, index) => {
    const col = document.createElement("div");
    col.className = "rail-layer";
    layer.forEach((t) => {
      const node = document.createElement("span");
      node.className = `task-status-dot ${t.status || "pending"}`;
      node.title = `${t.title || t.id} — ${t.status || "pending"}`;
      col.appendChild(node);
    });
    el.taskRail.appendChild(col);
    if (index < layers.length - 1) {
      const connector = document.createElement("div");
      connector.className = "rail-connector";
      el.taskRail.appendChild(connector);
    }
  });
}
