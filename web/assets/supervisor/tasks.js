// supervisor/tasks.js — the task tree, sending prompts, pause/resume.

import { state } from "./state.js";
import { apiFetch, formatTime } from "./api.js";
import { showGoalBanner } from "./banners.js";
import { $, el } from "./dom.js";
import { addChatMessage, loadSupervisors, showActiveSupervisor } from "./list.js";
import { esc, init } from "./main.js";
import { addLogEntry } from "./stream.js";

  // ── Task tree ────────────────────────────────────────────────────────
  export function renderTaskTree() {
    if (!state.tasks.length) {
      el.taskTree.innerHTML = '<div class="empty-state">No tasks yet. Wait for the supervisor to create a plan.</div>';
      state._expandedTaskId = null;
      return;
    }
    el.taskTree.innerHTML = state.tasks
      .map((t) => {
        const statusClass = t.status || "pending";
        const progress = Math.min(100, Math.round(t.progress_pct || 0));
        const progressClass = progress >= 100 ? "complete" : "";
        const isActive = t.id === state.activeTaskId;
        const isExpanded = t.id === state._expandedTaskId;
        const expandClass = isExpanded ? "expanded" : "";
        const expandIcon = isExpanded ? "▼" : "▶";
        const expandLabel = isExpanded ? "Collapse" : "Expand";
        const depsHtml = t.depends_on && t.depends_on.length
          ? `<div class="task-deps">depends on: ${t.depends_on.map(d => esc(d)).join(", ")}</div>` : "";
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
          <div class="task-progress-bar">
            <div class="task-progress-fill ${progressClass}" style="width:${progress}%"></div>
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
    const progress = Math.min(100, Math.round(task.progress_pct || 0));
    el.detailContent.innerHTML = `
      <div class="detail-section">
        <h3>Task ${esc(task.id)}</h3>
        <div class="detail-value">${esc(task.title)}</div>
      </div>
      <div class="detail-section">
        <h3>Status</h3>
        <div><span class="status-badge ${task.status}">${task.status}</span> &mdash; ${progress}%</div>
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
      ${task.depends_on ? `
      <div class="detail-section">
        <h3>Dependencies</h3>
        <div class="detail-value">${task.depends_on.length ? esc(task.depends_on.join(", ")) : "None"}</div>
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
      const data = await apiFetch("/api/supervisors/" + state.activeSupervisorId + "/send", {
        method: "POST",
        body: { prompt: text },
      });

      if (data.status === "planning") {
        addLogEntry("plan", "Supervisor is planning...");
        addChatMessage("system", "Analyzing your request and creating a plan...");
      }

      // Reload tasks after sending
      setTimeout(() => {
        loadTasks();
      }, 1000);

      // Also reload supervisor list to update status
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
        "/api/supervisors/" + state.activeSupervisorId + "/tasks"
      );
      state.tasks = data.tasks || [];
      renderTaskTree();
      updateOverallProgress();
      if (state.activeTaskId) {
        const task = state.tasks.find((t) => t.id === state.activeTaskId);
        if (task) renderTaskDetail(task.id);
      }
    } catch (e) {
      console.error("Failed to load tasks:", e);
    }
  }

  // ── Pause / Resume ──────────────────────────────────────────────────
  // A supervisor that is planning or running can be put on hold and resumed
  // later. The button sits in the chat panel header because that is the
  // place you look at while a supervisor is working; it shows the pause
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
      await apiFetch(`/api/supervisors/${encodeURIComponent(state.activeSupervisorId)}/${action}`, {
        method: "POST",
      });
      // Refresh the supervisor so status and list update in one call.
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
