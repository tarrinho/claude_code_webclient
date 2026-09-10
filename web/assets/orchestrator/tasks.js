// orchestrator/tasks.js — the task tree, sending prompts, pause/resume.

import { state } from "./state.js";
import { apiFetch, formatTime } from "./api.js";
import { abbrevTokens, formatUsd } from "../format.js?v=6021618";
import { showGoalBanner } from "./banners.js";
import { $, el } from "./dom.js";
import { addChatMessage, loadSupervisors, showActiveSupervisor } from "./list.js";
import { esc, init } from "./main.js";
import { addLogEntry } from "./stream.js";
import { renderRail } from "./rail.js";

  // `depends_on` arrives from the API as a JSON-encoded string, not an array:
  // db_supervisors.orchestrator_task_create stores it via json.dumps(deps or []),
  // and loadTasks assigns the API response straight into state.tasks with no
  // transform. Confirmed against the real database -- every existing task's
  // column is literally the four-character string "[]". So `t.depends_on.map`
  // threw for every task, on every render, before this was ever an array here;
  // found while double-checking this file's own progress feature, since that
  // code sits in the same per-task callback and never ran either -- the whole
  // task tree render throws before reaching it.
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

  // Seconds since `iso`, or null if `iso` does not parse. The one real,
  // observed number a running task has: the moment its DB row was last
  // written to "running" (updated_at, stamped by orchestrator_task_update).
  function _elapsedSeconds(iso) {
    const started = Date.parse(iso);
    if (Number.isNaN(started)) return null;
    return Math.max(0, Math.floor((Date.now() - started) / 1000));
  }

  function _formatSeconds(secs) {
    if (secs < 60) return `${secs}s`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  }

  // Elapsed time since `iso`, as "Ns" or "MmSs".
  function _elapsedLabel(iso) {
    const secs = _elapsedSeconds(iso);
    return secs === null ? "" : _formatSeconds(secs);
  }

  // An *estimated* completion percentage for the task currently running,
  // clamped so it never claims 100 -- that number is reserved for a task the
  // engine has actually marked done.
  //
  // Pedro asked to see the percentage moving, not just elapsed time. There is
  // still no CLI signal for "40% through this turn" -- a single claude -p call
  // reports nothing until it finishes -- so this is not a measurement, it is
  // elapsed time divided by how long this orchestrator's own finished tasks
  // typically took, which is the only basis available that is not invented
  // outright. Labelled "(est.)" everywhere it is shown for that reason: this
  // codebase's own rule is to never report what was not observed, and an
  // estimate presented as a measurement is exactly that.
  //
  // Returns null -- not a guessed number -- when there is no history yet to
  // estimate from, so the caller falls back to the honest elapsed-time label
  // instead of a percentage with nothing behind it.
  function _estimatedProgressPct(task) {
    if (task.status !== "running") return null;
    const elapsed = _elapsedSeconds(task.updated_at);
    if (elapsed === null) return null;
    const durations = state.tasks
      .filter((t) => t.status === "done" && t.created_at && t.updated_at)
      .map((t) => (Date.parse(t.updated_at) - Date.parse(t.created_at)) / 1000)
      .filter((d) => Number.isFinite(d) && d > 0);
    if (!durations.length) return null;
    const avg = durations.reduce((a, b) => a + b, 0) / durations.length;
    return Math.min(99, Math.round((elapsed / avg) * 100));
  }

  // The label and bar-fill percentage for one task, in one place so the tree
  // row and the detail panel can never disagree about what a task is showing.
  function _progressView(task) {
    const measured = Math.min(100, Math.round(task.progress_pct || 0));
    if (task.status !== "running") {
      return {pct: measured, label: `${measured}%`};
    }
    const estimate = _estimatedProgressPct(task);
    if (estimate === null) {
      return {pct: 0, label: _elapsedLabel(task.updated_at) || "running"};
    }
    return {pct: estimate, label: `${estimate}% (est.)`};
  }

  // Re-renders every second while a task is running, so the elapsed label
  // above actually counts up between the 30s poll in main.js. Self-stopping:
  // idle the moment nothing is running, rather than ticking forever against
  // a plan that finished.
  function _manageTicker() {
    const anyRunning = state.tasks.some((t) => t.status === "running");
    if (anyRunning && !state._tickTimer) {
      state._tickTimer = setInterval(renderTaskTree, 1000);
    } else if (!anyRunning && state._tickTimer) {
      clearInterval(state._tickTimer);
      state._tickTimer = null;
    }
  }

  // ── Task tree ────────────────────────────────────────────────────────
  export function renderTaskTree() {
    renderRail(state.tasks);
    if (!state.tasks.length) {
      el.taskTree.innerHTML = '<div class="empty-state">No tasks yet. Wait for the orchestrator to create a plan.</div>';
      state._expandedTaskId = null;
      if (state._tickTimer) {
        clearInterval(state._tickTimer);
        state._tickTimer = null;
      }
      return;
    }
    el.taskTree.innerHTML = state.tasks
      .map((t) => {
        // Escaped even though every value written to this field today is one
        // of a handful of literals the orchestrator engine's own control flow
        // assigns (never raw model output) -- rules.md's audit found it
        // interpolated unescaped and flagged it as a silent invariant gap: not
        // exploitable while that stays true, but nothing here enforces it, and
        // a future change that lets a task carry a freeform status would turn
        // this into a stored XSS with no visible signal at the change site.
        const statusClass = esc(t.status || "pending");
        const view = _progressView(t);
        const progress = view.pct;
        const progressClass = progress >= 100 ? "complete" : "";
        const progressLabel = esc(view.label);
        const isActive = t.id === state.activeTaskId;
        const isExpanded = t.id === state._expandedTaskId;
        const expandClass = isExpanded ? "expanded" : "";
        const expandIcon = isExpanded ? "▼" : "▶";
        const expandLabel = isExpanded ? "Collapse" : "Expand";
        const deps = _dependsOnList(t.depends_on);
        const depsHtml = deps.length
          ? `<div class="task-deps">depends on: ${deps.map(d => esc(d)).join(", ")}</div>` : "";
        const descHtml = t.description
          ? `<div class="task-meta" style="color:#64748b;font-size:11px;padding-left:16px;margin-top:1px;">${esc(t.description.substring(0, 80))}${t.description.length > 80 ? "..." : ""}</div>` : "";
        const resultHtml = (t.status === "done" && t.result)
          ? esc(t.result.substring(0, 200)) : "";
        const modelHtml = t.model ? esc(t.model) : "";
        return `<div class="task-item ${isActive ? "active" : ""}" data-task-id="${t.id}">
          <div class="task-header">
            <button class="expand-toggle" data-expand="${t.id}" title="${expandLabel}">${expandIcon}</button>
            <span class="task-status-dot ${statusClass}"></span>
            <span class="task-title">${esc(t.title || "Untitled")}</span>
          </div>
          ${descHtml}${depsHtml}
          <div class="task-meta">
            <span class="status-badge ${statusClass}">${statusClass}</span>
            ${modelHtml ? `<span>${modelHtml}</span>` : ""}
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <div class="task-progress-bar" style="flex:1;">
              <div class="task-progress-fill ${progressClass}" style="width:${progress}%"></div>
            </div>
            <span style="font-size:10px;color:#64748b;min-width:30px;text-align:right;">${progressLabel}</span>
          </div>
        </div>
        <div class="task-detail-row ${expandClass}" data-detail="${t.id}">
          <div class="task-detail-inner">
            ${resultHtml ? `<div><strong>Result:</strong> ${resultHtml}${t.result && t.result.length > 200 ? "…" : ""}</div>` : ""}
          </div>
        </div>`;
      })
      .join("");

    el.taskTree.querySelectorAll(".task-item").forEach((el) => {
      el.addEventListener("click", (e) => {
        // Don't open when clicking expand toggle
        if (e.target.closest(".expand-toggle")) return;
        selectTask(el.dataset.taskId);
      });
    });
    el.taskTree.querySelectorAll(".expand-toggle").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = btn.dataset.expand;
        if (state._expandedTaskId === id) {
          state._expandedTaskId = null;
        } else {
          state._expandedTaskId = id;
        }
        renderTaskTree();
      });
    });

    _manageTicker();
  }

  export function selectTask(taskId) {
    state.activeTaskId = taskId;
    renderTaskTree();
    renderTaskDetail(taskId);
  }

  export function renderTaskDetail(taskId) {
    const task = state.tasks.find((t) => t.id === taskId);
    if (!task) {
      el.detailContent.innerHTML = '<div class="empty-state">Task not found</div>';
      return;
    }
    const progressLabel = esc(_progressView(task).label);
    el.detailContent.innerHTML = `
      <div class="detail-section">
        <h3>Task ${esc(task.id)}</h3>
        <div class="detail-value">${esc(task.title)}</div>
      </div>
      <div class="detail-section">
        <h3>Status</h3>
        <div><span class="status-badge ${esc(task.status)}">${esc(task.status)}</span> &mdash; ${progressLabel}</div>
      </div>
      <div class="detail-section">
        <h3>Model</h3>
        <div class="detail-value">${task.model ? esc(task.model) : "<i>auto</i>"}</div>
      </div>
      ${task.description ? `
      <div class="detail-section">
        <h3>Description</h3>
        <div class="detail-value">${esc(task.description)}</div>
      </div>` : ""}
      ${task.depends_on != null ? `
      <div class="detail-section">
        <h3>Dependencies</h3>
        <div class="detail-value">${(() => {
          const deps = _dependsOnList(task.depends_on);
          return deps.length ? esc(deps.join(", ")) : "None";
        })()}</div>
      </div>` : ""}
      ${task.result ? `
      <div class="detail-section">
        <h3>Result</h3>
        <div class="detail-result">${esc(task.result)}</div>
      </div>` : ""}
      <div class="detail-section">
        <h3>Timeline</h3>
        <div class="detail-value">
          Created: ${formatTime(task.created_at)}<br>
          Updated: ${formatTime(task.updated_at)}
        </div>
      </div>
    `;
  }

  export function updateOverallProgress() {
    if (!state.tasks.length) {
      el.progressBarFill.style.width = "0%";
      return;
    }
    const total = state.tasks.reduce((s, t) => s + (t.progress_pct || 0), 0);
    const avg = total / state.tasks.length;
    el.progressBarFill.style.width = Math.min(100, Math.round(avg)) + "%";
  }

  /** Render what the run has spent, from the figure the tasks endpoint
   *  returns alongside the task list.
   *
   *  The engine has always recorded usage for every turn it spends -- one row
   *  per model, `origin="orchestrator"`, keyed on the synthetic chat id -- and
   *  nothing ever read it back per run, so the page showing a orchestrator's
   *  progress could not say what that progress had cost.
   */
  export function renderRunCost() {
    if (!el.runCost) return;
    const cost = state.cost;
    // No turns yet is not a figure worth a row: an unstarted orchestrator
    // showing "0 turns · $0.0000" is noise, and the element reappears the
    // moment the planning turn lands.
    if (!cost || !cost.turns) {
      el.runCost.hidden = true;
      el.runCost.replaceChildren();
      return;
    }
    const parts = [];
    parts.push(_costPart(
      `${cost.turns} turn${cost.turns === 1 ? "" : "s"}`, "run-cost-turns",
    ));
    parts.push(_costPart(
      `${abbrevTokens(cost.input_tokens)} in`, "run-cost-tokens",
      `${cost.input_tokens} input tokens`,
    ));
    parts.push(_costPart(
      `${abbrevTokens(cost.output_tokens)} out`, "run-cost-tokens",
      `${cost.output_tokens} output tokens`,
    ));
    const money = formatUsd(cost.cost_usd);
    if (money !== null) {
      parts.push(_costPart(money, "run-cost-usd", cost.cost_note || undefined));
    }
    if (cost.errors) {
      parts.push(_costPart(
        `${cost.errors} failed`, "run-cost-errors",
        "Turns that errored. They spent tokens and are counted here.",
      ));
    }
    el.runCost.replaceChildren(...parts);
    // Drives the asterisk in the stylesheet, and carries the explanation for
    // it on the row rather than only on the figure.
    if (cost.cost_partial) {
      el.runCost.dataset.partial = "yes";
      el.runCost.title = cost.cost_note || "This total is incomplete.";
    } else {
      delete el.runCost.dataset.partial;
      el.runCost.removeAttribute("title");
    }
    el.runCost.hidden = false;
  }

  function _costPart(text, className, title) {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    if (title) span.title = title;
    return span;
  }

  // ── Sending prompts ─────────────────────────────────────────────────
  export async function sendPrompt() {
    const text = el.promptInput.value.trim();
    if (!text || !state.activeSupervisorId) return;

    el.promptInput.value = "";
    el.promptInput.disabled = true;
    el.sendBtn.disabled = true;

    addChatMessage("user", text);
    showGoalBanner(text);

    try {
      const data = await apiFetch("/api/orchestrators/" + state.activeSupervisorId + "/send", {
        method: "POST",
        body: { prompt: text },
      });

      if (data.status === "planning") {
        addLogEntry("plan", "Orchestrator is planning...");
        addChatMessage("system", "Analyzing your request and creating a plan...");
      }

      // Reload tasks after sending
      setTimeout(() => {
        loadTasks();
      }, 1000);

      // Also reload orchestrator list to update status
      loadSupervisors();
    } catch (e) {
      addChatMessage("system", "Error: " + e.message);
      addLogEntry("error", e.message);
    } finally {
      el.promptInput.disabled = false;
      el.sendBtn.disabled = false;
      el.promptInput.focus();
    }
  }

  export async function loadTasks() {
    if (!state.activeSupervisorId) return;
    try {
      const data = await apiFetch(
        "/api/orchestrators/" + state.activeSupervisorId + "/tasks"
      );
      state.tasks = data.tasks || [];
      state.cost = data.cost || null;
      renderTaskTree();
      updateOverallProgress();
      renderRunCost();
      if (state.activeTaskId) {
        const task = state.tasks.find((t) => t.id === state.activeTaskId);
        if (task) renderTaskDetail(task.id);
      }
    } catch (e) {
      console.error("Failed to load tasks:", e);
    }
  }

  // ── Pause / Resume ──────────────────────────────────────────────────
  // A orchestrator that is planning or running can be put on hold and resumed
  // later. The button sits in the chat panel header because that is the
  // place you look at while a orchestrator is working; it shows the pause
  // symbol when paused so you know the opposite action is available.
  export function updatePauseResumeBtn(status) {
    if (!el.pauseResumeBtn) return;
    if (status === "paused") {
      el.pauseResumeBtn.hidden = false;
      el.pauseResumeBtn.textContent = "▶"; // play
      el.pauseResumeBtn.title = "Resume";
      el.pauseResumeBtn.setAttribute("aria-label", "Resume");
    } else if (status === "planning" || status === "running") {
      el.pauseResumeBtn.hidden = false;
      el.pauseResumeBtn.textContent = "⏸"; // pause
      el.pauseResumeBtn.title = "Pause";
      el.pauseResumeBtn.setAttribute("aria-label", "Pause");
    } else {
      el.pauseResumeBtn.hidden = true;
    }
  }

  export async function togglePauseResume() {
    if (!state.activeSupervisorId) return;
    if (!el.pauseResumeBtn) return;
    // Decide which action to take from the current button text.
    const action = el.pauseResumeBtn.textContent === "⏸" ? "pause" : "resume";
    try {
      await apiFetch(`/api/orchestrators/${encodeURIComponent(state.activeSupervisorId)}/${action}`, {
        method: "POST",
      });
      // Refresh the orchestrator so status and list update in one call.
      await showActiveSupervisor();
    } catch (err) {
      alert(`Could not ${action}: ${err.message}`);
    }
  }

  // Start when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
