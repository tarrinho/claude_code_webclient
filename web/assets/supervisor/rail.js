// supervisor/rail.js — dependency-ordered overview of the task DAG.
//
// Additive: the flat #task-tree list (tasks.js) stays exactly as it is for
// per-task detail and click-to-expand. This is an overview strip above it.

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
      (t.depends_on || []).every((d) => !byId.has(d) || placed.has(d))
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
