// orchestrator/plan.js — the plan-approval dialog: propose, edit, and run.
//
// The approval gate this whole design exists for. On 2026-08-30 the previous
// orchestrator parsed a plan into nothing, reported nothing, and executed
// anyway. This dialog is the last place that failure could still hide: a
// plan that cannot be read comes back with its raw text and its errors, not
// a silent empty table, and Run stays disabled while any error stands.
//
// Built with createElement/textContent throughout, the same way
// members.js's picker is -- never innerHTML with an interpolated value, so
// nothing typed into a title or a prompt can break out of its own cell.

import { apiFetch } from "./api.js";
import { loadSupervisors, selectSupervisor } from "./list.js";
import { loadTasks } from "./tasks.js";

// Which orchestrator this dialog is proposing a plan for. Set when the
// dialog opens, cleared (implicitly, by the next open) rather than reset on
// close -- a closed dialog has no listeners left to act on it either way.
let planSupervisorId = null;

// The rows currently shown in the table, kept as plain objects rather than
// re-read from the DOM on every action: `depends_on` needs to be an array to
// validate against, and the DOM only ever holds the comma-separated text a
// person typed.
let planRows = [];

// Human-readable reasons the plan (or the current table state) cannot run
// yet. Rendered as one `.plan-error` per entry -- never batched into one
// element -- so a single failure (the common case) is addressable as one
// node, which is what a test asserting on it needs.
let planErrors = [];

let _rowSeq = 0;

function _nextRowId() {
  _rowSeq += 1;
  let id = `task-${_rowSeq}`;
  // Vanishingly unlikely to collide with a server-issued id, but a duplicate
  // id is exactly the failure this dialog exists to catch, so a locally
  // generated one must not be able to cause it.
  const existing = new Set(planRows.map((r) => r.id));
  while (existing.has(id)) {
    _rowSeq += 1;
    id = `task-${_rowSeq}`;
  }
  return id;
}

// ── Cycle check ──────────────────────────────────────────────────────────
// Mirrors orchestrator._has_cycle in orchestrator.py so a cycle introduced
// by hand-editing the table (never reaching the server until Run is
// clicked) is caught immediately instead of only at that final POST.
function _hasCycle(rows) {
  const graph = {};
  rows.forEach((r) => { graph[r.id] = r.depends_on || []; });
  const seen = new Set();
  const stack = new Set();
  function visit(node) {
    if (stack.has(node)) return true;
    if (seen.has(node)) return false;
    seen.add(node);
    stack.add(node);
    for (const dep of graph[node] || []) {
      if (visit(dep)) return true;
    }
    stack.delete(node);
    return false;
  }
  return Object.keys(graph).some((n) => visit(n));
}

// Re-derives `planErrors` from the table's current, possibly hand-edited,
// state. Only the checks that do not need the server (an allowlist lookup
// stays server-side, at /run) -- title/prompt presence, unknown or
// duplicate ids, and a dependency cycle -- mirroring validate_plan's own
// non-network checks in orchestrator.py.
function _recomputeClientErrors() {
  const errors = [];
  const ids = planRows.map((r) => r.id);
  const seen = new Set();
  planRows.forEach((row, i) => {
    if (!row.title.trim() || !row.prompt.trim()) {
      errors.push(`task ${i} needs both a title and a prompt`);
    }
    (row.depends_on || []).forEach((d) => {
      if (!ids.includes(d)) errors.push(`task ${i} depends on unknown task ${d}`);
    });
    if (seen.has(row.id)) errors.push(`task ${i}: id ${row.id} is already used`);
    seen.add(row.id);
  });
  if (_hasCycle(planRows)) errors.push("the plan has a dependency cycle");
  planErrors = errors;
  _renderPlanErrors();
}

// ── Dialog lifecycle ────────────────────────────────────────────────────

export function closePlanEditor() {
  document.getElementById("planDialog")?.remove();
}

/** Create a new orchestrator and open the plan dialog for it.
 *
 * A second entry point into the same POST /api/orchestrators that "+ New"
 * in the topbar uses (see list.js's createSupervisor), not a new creation
 * path -- this one lands in the approval gate instead of the chat composer.
 */
export async function startNewRun() {
  let id;
  try {
    const data = await apiFetch("/api/orchestrators", {
      method: "POST",
      body: { title: "New run" },
    });
    id = data.id;
  } catch (e) {
    alert("Could not start a new run: " + e.message);
    return;
  }
  await loadSupervisors();
  selectSupervisor(id);
  openPlanEditor(id);
}

/** Open the plan-approval dialog for an already-selected orchestrator. */
export function openPlanEditor(supervisorId) {
  closePlanEditor();
  planSupervisorId = supervisorId;
  planRows = [];
  planErrors = [];
  _rowSeq = 0;

  const backdrop = document.createElement("div");
  backdrop.className = "dialog-backdrop open";
  backdrop.id = "planDialog";
  backdrop.setAttribute("role", "dialog");
  backdrop.setAttribute("aria-modal", "true");
  backdrop.setAttribute("aria-label", "Propose and approve a plan");

  const panel = document.createElement("div");
  panel.className = "dialog plan-dialog";

  const heading = document.createElement("h2");
  heading.textContent = "New run";
  const help = document.createElement("p");
  help.textContent =
    "Paste the plan the orchestrator proposed, or write one by hand, then " +
    "Validate. Nothing runs until you approve the table below and click Run.";

  const textarea = document.createElement("textarea");
  textarea.id = "planRaw";
  textarea.rows = 6;
  textarea.placeholder =
    '[{"id": "1", "title": "...", "prompt": "...", "depends_on": [], "model": null}]';
  textarea.setAttribute("aria-label", "Plan JSON");

  const validateRow = document.createElement("div");
  validateRow.className = "dialog-actions";
  const validateBtn = document.createElement("button");
  validateBtn.type = "button";
  validateBtn.id = "validatePlan";
  validateBtn.textContent = "Validate";
  validateBtn.addEventListener("click", () => _validatePlanRaw(textarea.value));
  validateRow.appendChild(validateBtn);

  const errorsBox = document.createElement("div");
  errorsBox.id = "planErrors";

  const table = document.createElement("table");
  table.id = "planTable";
  table.className = "plan-table";
  table.hidden = true;
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Title</th><th>Prompt</th><th>Depends on</th><th>Model</th><th></th></tr>";
  const tbody = document.createElement("tbody");
  tbody.id = "planTableBody";
  table.append(thead, tbody);

  const actions = document.createElement("div");
  actions.className = "dialog-actions";
  const addRowBtn = document.createElement("button");
  addRowBtn.type = "button";
  addRowBtn.id = "addPlanRow";
  addRowBtn.textContent = "+ Add row";
  addRowBtn.addEventListener("click", () => {
    planRows.push({ id: _nextRowId(), title: "", prompt: "", depends_on: [], model: "" });
    _renderPlanTable();
    _recomputeClientErrors();
  });
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", closePlanEditor);
  const runBtn = document.createElement("button");
  runBtn.type = "button";
  runBtn.id = "runPlan";
  runBtn.textContent = "Run";
  runBtn.disabled = true;
  runBtn.addEventListener("click", _runApprovedPlan);
  actions.append(addRowBtn, cancelBtn, runBtn);

  panel.append(heading, help, textarea, validateRow, errorsBox, table, actions);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);

  // Clicking the backdrop closes, matching every other dialog on this page.
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closePlanEditor();
  });

  textarea.focus();
}

// ── Validate ─────────────────────────────────────────────────────────────

async function _validatePlanRaw(raw) {
  if (!planSupervisorId) return;
  try {
    const data = await apiFetch(
      `/api/orchestrators/${encodeURIComponent(planSupervisorId)}/plan`,
      { method: "POST", body: { raw } },
    );
    planRows = (data.rows || []).map((r) => ({
      id: String(r.id),
      title: r.title || "",
      prompt: r.prompt || "",
      depends_on: Array.isArray(r.depends_on) ? r.depends_on.map(String) : [],
      model: r.model || "",
    }));
    planErrors = data.errors || [];
  } catch (e) {
    // apiFetch throws on a non-2xx (see api.js) -- a 404/500 here is not the
    // plan being unreadable, it is this request failing outright, but the
    // operator still needs to see it and Run must still stay unavailable.
    planRows = [];
    planErrors = [e.message];
  }
  _renderPlanErrors();
  _renderPlanTable();
}

function _renderPlanErrors() {
  const box = document.getElementById("planErrors");
  if (!box) return;
  box.replaceChildren();
  planErrors.forEach((msg) => {
    const p = document.createElement("div");
    p.className = "plan-error";
    p.textContent = msg;
    box.appendChild(p);
  });
  _updateRunAvailability();
}

// ── Table ────────────────────────────────────────────────────────────────

function _renderPlanTable() {
  const table = document.getElementById("planTable");
  const body = document.getElementById("planTableBody");
  if (!table || !body) return;
  table.hidden = !planRows.length;
  body.replaceChildren();

  planRows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.dataset.rowId = row.id;

    const titleTd = document.createElement("td");
    const idLabel = document.createElement("span");
    idLabel.className = "plan-row-id";
    idLabel.textContent = row.id;
    const titleInput = document.createElement("input");
    titleInput.type = "text";
    titleInput.value = row.title;
    titleInput.setAttribute("aria-label", "Task title");
    titleInput.addEventListener("input", () => {
      row.title = titleInput.value;
      _recomputeClientErrors();
    });
    titleTd.append(idLabel, titleInput);

    const promptTd = document.createElement("td");
    const promptInput = document.createElement("textarea");
    promptInput.rows = 2;
    promptInput.value = row.prompt;
    promptInput.setAttribute("aria-label", "Task prompt");
    promptInput.addEventListener("input", () => {
      row.prompt = promptInput.value;
      _recomputeClientErrors();
    });
    promptTd.appendChild(promptInput);

    const depsTd = document.createElement("td");
    const depsInput = document.createElement("input");
    depsInput.type = "text";
    depsInput.value = row.depends_on.join(", ");
    depsInput.placeholder = "task ids, comma-separated";
    depsInput.setAttribute("aria-label", "Depends on");
    depsInput.addEventListener("input", () => {
      row.depends_on = depsInput.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      _recomputeClientErrors();
    });
    depsTd.appendChild(depsInput);

    const modelTd = document.createElement("td");
    const modelInput = document.createElement("input");
    modelInput.type = "text";
    modelInput.value = row.model || "";
    modelInput.placeholder = "auto";
    modelInput.setAttribute("aria-label", "Model");
    modelInput.addEventListener("input", () => {
      row.model = modelInput.value.trim();
      _recomputeClientErrors();
    });
    modelTd.appendChild(modelInput);

    const delTd = document.createElement("td");
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "plan-row-delete";
    delBtn.textContent = "×";
    delBtn.title = "Delete this row";
    delBtn.setAttribute("aria-label", `Delete row ${row.title || row.id}`);
    delBtn.addEventListener("click", () => {
      planRows = planRows.filter((r) => r.id !== row.id);
      _renderPlanTable();
      _recomputeClientErrors();
    });
    delTd.appendChild(delBtn);

    tr.append(titleTd, promptTd, depsTd, modelTd, delTd);
    body.appendChild(tr);
  });

  _updateRunAvailability();
}

function _updateRunAvailability() {
  const runBtn = document.getElementById("runPlan");
  if (!runBtn) return;
  // Disabled while any error stands, and while there is nothing to run --
  // an empty table is not an error, but it is not a plan either.
  runBtn.disabled = planErrors.length > 0 || planRows.length === 0;
}

// ── Run ──────────────────────────────────────────────────────────────────

async function _runApprovedPlan() {
  if (!planSupervisorId || planErrors.length || !planRows.length) return;
  const runBtn = document.getElementById("runPlan");
  if (runBtn) runBtn.disabled = true;
  try {
    await apiFetch(
      `/api/orchestrators/${encodeURIComponent(planSupervisorId)}/run`,
      {
        method: "POST",
        body: {
          rows: planRows.map((r) => ({
            id: r.id,
            title: r.title,
            prompt: r.prompt,
            depends_on: r.depends_on,
            model: r.model || null,
          })),
        },
      },
    );
  } catch (e) {
    // The server re-checks everything /plan already did (a hand-edited
    // model, in particular, is only re-validated here) -- so a rejection at
    // this point is real and must reopen the gate, not just alert and move on.
    planErrors = [e.message];
    _renderPlanErrors();
    if (runBtn) runBtn.disabled = true;
    return;
  }
  // The run view is the task tree, chat and cost strip already on this page
  // -- showActiveSupervisor() (run via selectSupervisor in startNewRun)
  // already made them visible. Closing the dialog is what reveals them;
  // loadTasks() is what fills them in immediately rather than waiting for
  // the 30s poll.
  closePlanEditor();
  await loadTasks();
}
