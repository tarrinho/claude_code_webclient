// supervisor/list.js — the supervisor list, its ordering, and rename.

import { state } from "./state.js";
import { apiFetch, formatTime } from "./api.js";
import { clearBadge } from "./banners.js";
import { $, el } from "./dom.js";
import { esc, showScrollBtn } from "./main.js";
import { connectSSE } from "./stream.js";
import { renderTaskTree, updateOverallProgress, updatePauseResumeBtn } from "./tasks.js";

  // ── Supervisor list ──────────────────────────────────────────────────
  // Sort order for the supervisor list. Default "newest first" (by id,
  // which is a UUID, so it approximates creation time), but a toggle
  // makes "last active" the order so recently-used supervisors stay visible.
  let supervisorSortMode = "newest"; // "newest" | "updated"
  export function setSupervisorSort(mode) {
    supervisorSortMode = mode;
    try { localStorage.setItem("wc_supervisor_sort", mode); } catch (e) {}
    renderSupervisorList();
  }
  (function loadSavedSort() {
    try {
      const v = localStorage.getItem("wc_supervisor_sort");
      if (v === "updated") supervisorSortMode = v;
    } catch (e) {}
  })();

  export async function loadSupervisors() {
    try {
      const data = await apiFetch("/api/supervisors");
      state.supervisors = data.supervisors || [];
      renderSupervisorList();
      restoreOpen();
    } catch (e) {
      console.error("Failed to load supervisors:", e);
    }
  }

  // ── Rename ────────────────────────────────────────────────────────────────
  // A supervisor's name appears only in this list, so the control lives on the
  // row rather than in a detail header the page does not have.
  //
  // renamingId suppresses the refresh below. setInterval calls
  // loadSupervisors(), which rebuilds this list wholesale, so without it a
  // half-typed name is wiped by a poll the user cannot see coming -- and the
  // edit simply vanishes, which reads like the page ignoring them.
  let renamingId = null;

  export function renderSupervisorList() {
    // An edit in progress outranks a refresh; the poll catches up when it ends.
    if (renamingId) return;
    if (!state.supervisors.length) {
      el.supervisorList.innerHTML =
        '<div class="empty-state">No supervisors yet.<br>Click <b>+ New</b> to create one.</div>';
      return;
    }
    // Sort the list. Default newest-first (UUID ≈ creation time),
    // but "updated" sorts by last activity so the most-recently-used
    // supervisors stay at the top.
    const sorted = state.supervisors
      .map((s) => ({ ...s }))
      .sort((a, b) => {
        if (supervisorSortMode === "updated") {
          return (b.updated_at || b.created_at || "")
            .localeCompare(a.updated_at || a.created_at || "");
        }
        return (b.created_at || "").localeCompare(a.created_at || "");
      });
    el.supervisorList.innerHTML = sorted
      .map((s) => {
        const statusClass = s.status || "idle";
        // Show relative time if updated_at differs from created_at —
        // a freshly-created supervisor shows only the status badge.
        let timeLabel = "";
        if (s.updated_at && s.created_at && s.updated_at !== s.created_at) {
          timeLabel = `&middot; ${formatTime(s.updated_at)}`;
        }
        const degradedTitle = s.degraded
          ? ` title="${esc(s.degraded_reason || 'Some state may be stale')}"`
          : "";
        const degradedGlyph = s.degraded ? " ⚠" : "";
        return `<div class="supervisor-list-item ${
          s.id === state.activeSupervisorId ? "active" : ""
        }" data-id="${esc(s.id)}">
          <div class="sl-title">${esc(s.title || "Untitled")}</div>
          <div class="sl-status">
            <span class="status-badge ${esc(statusClass)}"${degradedTitle}>${esc(statusClass)}${degradedGlyph}</span>
            ${s.progress_pct != null ? `<span>${Math.round(s.progress_pct)}%</span>` : ""}
            ${timeLabel ? `<span style="margin-left:4px">${timeLabel}</span>` : ""}
          </div>
        </div>`;
      })
      .join("");

    el.supervisorList.querySelectorAll(".supervisor-list-item").forEach((row) => {
      row.addEventListener("click", () => selectSupervisor(row.dataset.id));

      // Status pulse: flash badge when the supervisor's status changed.
      const rowId = row.dataset.id;
      const sup = state.supervisors.find((s) => s.id === rowId);
      if (sup && state._prevStatuses[rowId] && state._prevStatuses[rowId] !== (sup.status || "idle")) {
        const badge = row.querySelector(".status-badge");
        if (badge) {
          badge.classList.add("flash");
          setTimeout(() => badge.classList.remove("flash"), 700);
        }
      }
      state._prevStatuses[rowId] = sup?.status || "idle";

      const titleEl = row.querySelector(".sl-title");
      if (!titleEl) return;
      // Built with createElement rather than added to the template string
      // above: a name the user typed is the last value to interpolate into
      // markup, and rules.md wants innerHTML writes kept down, not up.
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sl-rename";
      btn.textContent = "\u270E";
      btn.setAttribute(
        "aria-label", `Rename ${titleEl.textContent || "this supervisor"}`);
      btn.addEventListener("click", (event) => {
        // The row's own click selects the supervisor. Without this, renaming
        // one you are not looking at also switches to it.
        event.stopPropagation();
        startRename(row.dataset.id, titleEl);
      });
      row.appendChild(btn);
    });
  }

  export function startRename(id, titleEl) {
    if (!titleEl || renamingId) return;
    renamingId = id;
    const original = titleEl.textContent;

    const input = document.createElement("input");
    input.type = "text";
    input.className = "sl-rename-input";
    input.value = original;
    // The server slices the title to 200 characters. Without this the user
    // types past the limit and is truncated with no indication, so the rename
    // reads as having half worked.
    input.maxLength = 200;
    input.setAttribute("aria-label", "Supervisor name");
    titleEl.replaceWith(input);
    input.focus();
    input.select();

    let settled = false;
    const finish = async (commit) => {
      if (settled) return;   // blur fires after Enter; only the first counts
      settled = true;
      const next = input.value.trim();
      renamingId = null;
      // Redraw from server state either way, so a cancelled or rejected edit
      // cannot leave a stale input behind.
      if (!commit || !next || next === original) {
        await loadSupervisors();
        return;
      }
      try {
        await apiFetch(`/api/supervisors/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: { title: next },
        });
      } catch (err) {
        // apiFetch here throws on non-2xx rather than returning a Response,
        // and this page has no showToast -- alert is what createSupervisor and
        // membersNotice already use for a failure the user has to see.
        alert(`Could not rename: ${err.message}`);
      }
      await loadSupervisors();
    };

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", (event) => event.stopPropagation());
  }

  export async function createSupervisor() {
    try {
      const data = await apiFetch("/api/supervisors", {
        method: "POST",
        body: { title: "New Supervisor" },
      });
      await loadSupervisors();
      selectSupervisor(data.id);
    } catch (e) {
      alert("Failed to create supervisor: " + e.message);
    }
  }

  // Which supervisor was last open, so a reload returns to it. The page had no
  // notion of this: selectSupervisor was reachable only from a click on a list
  // row or immediately after creating one, so opening /supervisor.html always
  // showed the start screen and left an existing conversation invisible --
  // messages were never even fetched, because showActiveSupervisor is what
  // fetches them. The main app has done this with wc_last_chat all along.
  export const LAST_OPEN_KEY = "wc_last_supervisor";

  export function rememberOpen(id) {
    try {
      localStorage.setItem(LAST_OPEN_KEY, id);
    } catch (e) {
      // Private mode or a full quota. Losing the memory of which supervisor was
      // open must not stop it being opened.
      console.warn("could not remember the open supervisor:", e);
    }
  }

  export function restoreOpen() {
    if (state.activeSupervisorId || !state.supervisors.length) return;
    let wanted = null;
    try {
      wanted = localStorage.getItem(LAST_OPEN_KEY);
    } catch (e) {
      console.warn("could not read the last open supervisor:", e);
    }
    // The remembered one if it still exists, otherwise the most recent, because
    // an empty centre panel next to a populated list reads as a broken page.
    const found = state.supervisors.find((s) => s.id === wanted);
    selectSupervisor((found || state.supervisors[0]).id);
  }

  export function selectSupervisor(id) {
    state.activeSupervisorId = id;
    // The count belongs to the conversation you were reading, not to the one
    // you just opened.
    clearBadge();
    rememberOpen(id);
    renderSupervisorList();
    // Same for the event log. addLogEntry only ever appends to the DOM, so
    // without this the previous supervisor's events stayed on screen and
    // interleaved with the new one's, undivided -- two runs presented as one.
    // Immediately before showActiveSupervisor, which reconnects SSE and
    // starts filling it again.
    if (el.eventLog) el.eventLog.innerHTML = "";
    state.eventLog.length = 0;
    showActiveSupervisor();
  }

  export async function showActiveSupervisor() {
    if (!state.activeSupervisorId) return;
    try {
      const data = await apiFetch("/api/supervisors/" + state.activeSupervisorId);
      state.activeSupervisor = data.supervisor;
      // The pause button reflects what the server says, not what the engine
      // had last time — the engine may have been restarted between page loads.
      updatePauseResumeBtn(state.activeSupervisor.status);
    } catch (e) {
      console.error("Failed to load supervisor:", e);
      return;
    }

    // Render the shell immediately (status, chat area) so the UI feels
    // instant.  Messages and tasks load in parallel and fill in after.
    el.startScreen.style.display = "none";
    el.supervisorChat.style.display = "flex";
    el.composer.style.display = "flex";

    // Load messages and tasks in parallel — neither blocks the other.
    const [msgData, taskData] = await Promise.allSettled([
      apiFetch("/api/supervisors/" + state.activeSupervisorId + "/messages"),
      apiFetch("/api/supervisors/" + state.activeSupervisorId + "/tasks"),
    ]);

    if (msgData.status === "fulfilled") {
      state.chatMessages = msgData.value.messages || [];
      renderChatMessages();
    } else {
      state.chatMessages = [];
      addChatMessage("system", "Could not load chat messages: " + (msgData.reason || "Unknown error"));
      renderChatMessages();
    }

    if (taskData.status === "fulfilled") {
      state.tasks = taskData.value.tasks || [];
      renderTaskTree();
      updateOverallProgress();
    } else {
      state.tasks = [];
      renderTaskTree();
    }

    // Start SSE stream
    connectSSE();
  }

  export function renderChatMessages() {
    if (!state.chatMessages.length) {
      el.chatMessages.innerHTML =
        '<div class="empty-state">No messages yet. Send a prompt to get started.</div>';
      return;
    }
    el.chatMessages.innerHTML = state.chatMessages
      .map((m) => {
        if (m.role === "user") {
          return `<div class="chat-message user"><strong>You:</strong><br>${esc(m.content || "")}</div>`;
        } else if (m.role === "supervisor") {
          const content = m.content || "";
          if (content.includes("<<PLAN")) {
            return `<div class="chat-message supervisor"><strong>Supervisor:</strong><br><div class="plan-block">${esc(content)}</div></div>`;
          }
          return `<div class="chat-message supervisor"><strong>Supervisor:</strong><br>${esc(content)}</div>`;
        }
        return `<div class="chat-message system">${esc(m.content || "")}</div>`;
      })
      .join("");
    if (state.chatScrollFollow) {
      el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
    } else {
      showScrollBtn(state._chatScrollBtn);
    }
  }

  export function addChatMessage(role, content, metadata) {
    state.chatMessages.push({ role, content, created_at: new Date().toISOString(), metadata });
    renderChatMessages();
  }

