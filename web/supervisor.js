// supervisor.js — Supervisor orchestration UI for WebConsole 0.9.0

(function () {
  "use strict";

  // ── State ────────────────────────────────────────────────────────────
  let supervisors = [];
  let activeSupervisorId = null;
  let activeSupervisor = null;
  let tasks = [];
  let activeTaskId = null;
  let chatMessages = [];
  let eventLog = [];
  let sseController = null;
  let csrfToken = "";

  // ── Panel sizing state ──────────────────────────────────────────────
  const PANEL_MIN_WIDTHS = { left: 200, center: 300, right: 200 };
  const PANEL_MIN_HEIGHTS = { bottom: 80 };
  let panelSizes = { left: 320, right: 320, bottom: 200 };
  let lastMinimized = {};

  // ── CSRF ─────────────────────────────────────────────────────────────
  // Read straight from the cookie, and synchronously. The token is only ever
  // in the cookie: wc_csrf is set httponly=false precisely so this script can
  // read it, which is what makes the double-submit check work.
  //
  // This used to ask /api/settings for it, which could not work twice over.
  // getCsrf() awaited apiFetch(), and apiFetch()'s first statement awaited
  // getCsrf(), so the two recursed into each other and fetch was never
  // reached -- every call from this page hung, and because async recursion
  // never throws, the cookie fallback in the catch was unreachable. Underneath
  // that, /api/settings returns no csrf_token field at all, so even unwound it
  // would have stored "" and re-entered on the next call.
  //
  // Being synchronous is the point rather than a tidy-up: with no await here
  // apiFetch cannot re-enter this function, so the bug is gone by
  // construction instead of by remembering not to reintroduce it.
  function getCsrf() {
    if (!csrfToken) {
      csrfToken = document.cookie
        .split("; ")
        .find((c) => c.startsWith("wc_csrf="))
        ?.split("=")[1] || "";
    }
    return csrfToken;
  }

  // ── API helpers ──────────────────────────────────────────────────────
  async function apiFetch(url, opts = {}) {
    const token = getCsrf();
    const headers = opts.headers || {};
    if (token) headers["X-CSRF-Token"] = token;
    if (opts.body && !opts.form) {
      headers["Content-Type"] = "application/json";
    }
    const r = await fetch(url, {
      ...opts,
      headers,
      credentials: "same-origin",
      body: opts.body
        ? typeof opts.body === "string"
          ? opts.body
          : JSON.stringify(opts.body)
        : undefined,
    });
    if (!r.ok) {
      let msg = "API error";
      try {
        const d = await r.json();
        msg = d.detail || msg;
      } catch (_) {}
      throw new Error(msg);
    }
    return r.json();
  }

  // Every timestamp on this page rendered as "Invalid Date". The zone marker was
  // appended unconditionally, but db._now() already returns "...T22:54:00Z" and
  // Date.toISOString() returns "...T22:54:00.000Z" -- so the value became
  // "...00ZZ", which Date cannot parse. `new Date` does not throw on a value it
  // cannot read, it returns an Invalid Date, so the try/catch that looked like a
  // safety net never once fired and toLocaleTimeString printed those two words.
  //
  // Mirrors parseTimestamp in web/assets/conversation.js. Duplicated rather than
  // shared because this page loads a classic script, not a module, and cannot
  // import it; keep the two in step.
  function formatTime(value) {
    if (!value) return "";
    const raw = String(value);
    // A space separator instead of "T" is accepted by Chrome and rejected by
    // Safari, which is what a phone is running.
    const normalised = raw.replace(" ", "T");
    const zoned = /Z$|[+-]\d\d:?\d\d$/.test(normalised)
      ? normalised
      : `${normalised}Z`;
    const parsed = new Date(zoned);
    if (Number.isNaN(parsed.getTime())) {
      // Show the value rather than the words "Invalid Date", stripped of
      // anything that could be markup: callers interpolate this into innerHTML.
      return raw.replace(/[^\w :.+-]/g, "");
    }
    return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // ── DOM refs ─────────────────────────────────────────────────────────
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const el = {
    supervisorList: $("#supervisor-list"),
    taskTree: $("#task-tree"),
    chatMessages: $("#chat-messages"),
    eventLog: $("#event-log"),
    composer: $("#composer"),
    promptInput: $("#prompt-input"),
    sendBtn: $("#send-btn"),
    startScreen: $("#start-screen"),
    supervisorChat: $("#supervisor-chat"),
    detailContent: $("#detail-content"),
    progressBarFill: $("#overall-progress-fill"),
    newSupervisorBtn: $("#new-supervisor-btn"),
    topbarInfo: $("#topbar-info"),
    goalBanner: $("#goal-banner"),
    goalText: $("#goal-text"),
    completionBanner: $("#completion-banner"),
    completionStats: $("#completion-stats"),
    completionSummary: $("#completion-summary"),
    goalDismissBtn: $(".goal-banner-dismiss"),
    completionCloseBtn: $(".completion-close"),
  };

  // ── Supervisor list ──────────────────────────────────────────────────
  async function loadSupervisors() {
    try {
      const data = await apiFetch("/api/supervisors");
      supervisors = data.supervisors || [];
      renderSupervisorList();
    } catch (e) {
      console.error("Failed to load supervisors:", e);
    }
  }

  function renderSupervisorList() {
    if (!supervisors.length) {
      el.supervisorList.innerHTML =
        '<div class="empty-state">No supervisors yet.<br>Click <b>+ New</b> to create one.</div>';
      return;
    }
    el.supervisorList.innerHTML = supervisors
      .map((s) => {
        const statusClass = s.status || "idle";
        return `<div class="supervisor-list-item ${
          s.id === activeSupervisorId ? "active" : ""
        }" data-id="${s.id}">
          <div class="sl-title">${esc(s.title || "Untitled")}</div>
          <div class="sl-status">
            <span class="status-badge ${statusClass}">${statusClass}</span>
            ${s.progress_pct != null ? `<span>${Math.round(s.progress_pct)}%</span>` : ""}
          </div>
        </div>`;
      })
      .join("");

    el.supervisorList.querySelectorAll(".supervisor-list-item").forEach((el) => {
      el.addEventListener("click", () => selectSupervisor(el.dataset.id));
    });
  }

  async function createSupervisor() {
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

  function selectSupervisor(id) {
    activeSupervisorId = id;
    renderSupervisorList();
    showActiveSupervisor();
  }

  async function showActiveSupervisor() {
    if (!activeSupervisorId) return;
    try {
      const data = await apiFetch("/api/supervisors/" + activeSupervisorId);
      activeSupervisor = data.supervisor;
    } catch (e) {
      console.error("Failed to load supervisor:", e);
      return;
    }

    el.startScreen.style.display = "none";
    el.supervisorChat.style.display = "flex";
    el.composer.style.display = "flex";

    // Load chat messages
    try {
      const msgData = await apiFetch(
        "/api/supervisors/" + activeSupervisorId + "/messages"
      );
      chatMessages = msgData.messages || [];
      renderChatMessages();
    } catch (e) {
      chatMessages = [];
    }

    // Load tasks
    try {
      const taskData = await apiFetch(
        "/api/supervisors/" + activeSupervisorId + "/tasks"
      );
      tasks = taskData.tasks || [];
      renderTaskTree();
      updateOverallProgress();
    } catch (e) {
      tasks = [];
      renderTaskTree();
    }

    // Start SSE stream
    connectSSE();
  }

  function renderChatMessages() {
    if (!chatMessages.length) {
      el.chatMessages.innerHTML =
        '<div class="empty-state">No messages yet. Send a prompt to get started.</div>';
      return;
    }
    el.chatMessages.innerHTML = chatMessages
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
    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
  }

  function addChatMessage(role, content, metadata) {
    chatMessages.push({ role, content, created_at: new Date().toISOString(), metadata });
    renderChatMessages();
  }

  // ── Goal banner ──────────────────────────────────────────────────────
  let _currentGoal = null;

  function showGoalBanner(promptText) {
    _currentGoal = promptText;
    el.goalText.textContent = promptText;
    el.goalBanner.hidden = false;
    // Scroll goal banner into view
    el.goalBanner.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function dismissGoalBanner() {
    _currentGoal = null;
    el.goalBanner.hidden = true;
  }

  // ── Completion banner ────────────────────────────────────────────────
  let _completionData = null;

  function showCompletionBanner(eng) {
    // Gather results from all tasks
    const allTasks = tasks;
    const total = allTasks.length;
    const doneCount = allTasks.filter(t => t.status === "done").length;
    const failCount = allTasks.filter(t => t.status === "failed").length;
    const skipCount = allTasks.filter(t => t.status === "blocked").length;
    const pendingCount = total - doneCount - failCount - skipCount;

    el.completionStats.textContent =
      `Completed ${doneCount}/${total} tasks` +
      (failCount ? `, ${failCount} failed` : "") +
      (skipCount ? `, ${skipCount} skipped` : "") +
      (pendingCount ? `, ${pendingCount} pending` : "");

    // Build a short summary of successful task results
    const results = allTasks
      .filter(t => t.status === "done" && t.result)
      .map(t => `Task ${t.id} (${t.title}): ${t.result.substring(0, 200)}`)
      .join("\n\n");

    el.completionSummary.textContent = results || "(No result text available)";
    el.completionBanner.hidden = false;

    // Also add a chat message so the completion shows up in the scrollback too
    addChatMessage("system", `✅ Supervisor completed: ${doneCount}/${total} tasks done.`);
  }

  function dismissCompletionBanner() {
    _completionData = null;
    el.completionBanner.hidden = true;
  }

  // ── Task tree ────────────────────────────────────────────────────────
  function renderTaskTree() {
    if (!tasks.length) {
      el.taskTree.innerHTML = '<div class="empty-state">No tasks yet. Wait for the supervisor to create a plan.</div>';
      return;
    }
    el.taskTree.innerHTML = tasks
      .map((t) => {
        const statusClass = t.status || "pending";
        const progress = Math.min(100, Math.round(t.progress_pct || 0));
        const progressClass = progress >= 100 ? "complete" : "";
        const isActive = t.id === activeTaskId;
        return `<div class="task-item ${isActive ? "active" : ""}" data-task-id="${t.id}">
          <div class="task-header">
            <span class="task-status-dot ${statusClass}"></span>
            <span class="task-title">${esc(t.title || "Untitled")}</span>
          </div>
          ${t.description ? `<div class="task-meta" style="color:#64748b;font-size:11px;padding-left:16px;margin-top:1px;">${esc(t.description.substring(0, 80))}${t.description.length > 80 ? "..." : ""}</div>` : ""}
          <div class="task-meta">
            <span class="status-badge ${statusClass}">${statusClass}</span>
            ${t.model ? `<span>${esc(t.model)}</span>` : ""}
          </div>
          <div class="task-progress-bar">
            <div class="task-progress-fill ${progressClass}" style="width:${progress}%"></div>
          </div>
        </div>`;
      })
      .join("");

    el.taskTree.querySelectorAll(".task-item").forEach((el) => {
      el.addEventListener("click", () => selectTask(el.dataset.taskId));
    });
  }

  function selectTask(taskId) {
    activeTaskId = taskId;
    renderTaskTree();
    renderTaskDetail(taskId);
  }

  function renderTaskDetail(taskId) {
    const task = tasks.find((t) => t.id === taskId);
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

  function updateOverallProgress() {
    if (!tasks.length) {
      el.progressBarFill.style.width = "0%";
      return;
    }
    const total = tasks.reduce((s, t) => s + (t.progress_pct || 0), 0);
    const avg = total / tasks.length;
    el.progressBarFill.style.width = Math.min(100, Math.round(avg)) + "%";
  }

  // ── SSE stream ───────────────────────────────────────────────────────
  function connectSSE() {
    if (sseController) {
      sseController.abort();
    }
    sseController = new AbortController();

    const url =
      "/api/supervisors/" + activeSupervisorId + "/stream";

    const evtSource = new EventSource(url, {
      signal: sseController.signal,
    });

    evtSource.onopen = function () {
      addLogEntry("system", "SSE connected");
    };

    evtSource.onmessage = function (event) {
      try {
        const data = JSON.parse(event.data);
        handleSSEEvent(data);
      } catch (_) {
        // Not JSON — heartbeat or comment
      }
    };

    evtSource.onerror = function (err) {
      if (sseController.signal.aborted) return;
      addLogEntry("system", "Stream reconnecting...");
      evtSource.close();
    };

    window._supervisorSSE = evtSource;
  }

  function handleSSEEvent(data) {
    switch (data.type) {
      case "progress":
        handleProgress(data);
        break;
      case "events":
        handleSSEEvents(data.events || []);
        break;
      case "done":
        addLogEntry("system", "Supervisor finished: " + data.status);
        addChatMessage("system", "Supervisor completed with status: " + data.status);
        // Show the completion banner with task results summary
        if (data.status === "done") {
          // Reload fresh task data to build accurate completion summary
          loadTasks().then(() => {
            // Get the engine from the global if available
            const eng = window._supervisorEngine;
            showCompletionBanner(eng);
          });
        }
        break;
      case "start":
        addLogEntry("system", "Stream started");
        break;
      case "error":
        addLogEntry("error", data.error || "Unknown error");
        break;
    }
  }

  function handleProgress(data) {
    // Update tasks from server data
    if (data.tasks) {
      const oldMap = new Map(tasks.map((t) => [t.id, t]));
      data.tasks.forEach((t) => {
        if (oldMap.has(t.id)) {
          Object.assign(oldMap.get(t.id), t);
        } else {
          tasks.push(t);
          oldMap.set(t.id, t);
        }
      });
      renderTaskTree();
    }
    updateOverallProgress();

    // Update supervisor status if available
    if (data.status && activeSupervisor) {
      activeSupervisor.status = data.status;
      renderSupervisorList();
    }
  }

  function handleSSEEvents(events) {
    events.forEach((e) => {
      if (e.type === "task_start") {
        addLogEntry("task_start", `Task ${e.task_id}: ${e.data.title || ""}`);
      } else if (e.type === "task_done") {
        addLogEntry("task_done", `Task ${e.task_id} completed (${e.data.result_len || 0} chars)`);
      } else if (e.type === "task_error") {
        addLogEntry("task_error", `Task ${e.task_id}: ${e.data.error || "Unknown error"}`);
      }
    });
  }

  function addLogEntry(type, msg) {
    const time = formatTime(new Date().toISOString());
    const div = document.createElement("div");
    div.className = "log-entry";
    div.innerHTML = `
      <span class="log-time">${time}</span>
      <span class="log-type ${type}">${type}</span>
      <span class="log-msg">${esc(msg)}</span>
    `;
    el.eventLog.appendChild(div);
    el.eventLog.scrollTop = el.eventLog.scrollHeight;
    eventLog.push({ type, msg, time });
  }

  // ── Sending prompts ─────────────────────────────────────────────────
  async function sendPrompt() {
    const text = el.promptInput.value.trim();
    if (!text || !activeSupervisorId) return;

    el.promptInput.value = "";
    el.promptInput.disabled = true;
    el.sendBtn.disabled = true;

    addChatMessage("user", text);
    showGoalBanner(text);

    try {
      const data = await apiFetch("/api/supervisors/" + activeSupervisorId + "/send", {
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

  async function loadTasks() {
    if (!activeSupervisorId) return;
    try {
      const data = await apiFetch(
        "/api/supervisors/" + activeSupervisorId + "/tasks"
      );
      tasks = data.tasks || [];
      renderTaskTree();
      updateOverallProgress();
      if (activeTaskId) {
        const task = tasks.find((t) => t.id === activeTaskId);
        if (task) renderTaskDetail(task.id);
      }
    } catch (e) {
      console.error("Failed to load tasks:", e);
    }
  }

  // ── Panel resize ────────────────────────────────────────────────────
  let resizing = null;

  function initResizeHandles() {
    const handles = $$(".resize-handle");
    handles.forEach((handle) => {
      handle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        handle.addEventListener("mousemove", onResizeMove);
        handle.addEventListener("mouseup", onResizeEnd);
        handle.classList.add("active");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      });
    });

    document.addEventListener("mousemove", onResizeMove);
    document.addEventListener("mouseup", onResizeEnd);

    // Minimize / maximize buttons
    $$(".panel-btn").forEach((btn) => {
      btn.addEventListener("click", onPanelBtn);
    });
  }

  function onPanelBtn(e) {
    const btn = e.currentTarget;
    const panel = btn.dataset.panel;
    const maxTarget = btn.dataset.max;

    if (panel) {
      // Minimize / restore
      if (lastMinimized[panel]) {
        restorePanel(panel);
        lastMinimized[panel] = false;
        btn.classList.remove("minimized");
        return;
      }
      minimizePanel(panel);
      lastMinimized[panel] = true;
      btn.classList.add("minimized");
    }

    if (maxTarget) {
      // Maximize / restore single panel
      if (document.body.classList.contains("max-" + maxTarget)) {
        document.body.classList.remove("max-" + maxTarget);
        // When exiting maximize mode, restore any panels that were
        // minimized while maximized — they are CSS-hidden by the
        // max-xxx rules until restorePanel strips that styling.
        Object.keys(lastMinimized).forEach((p) => {
          if (lastMinimized[p] && p !== maxTarget) {
            restorePanel(p);
            lastMinimized[p] = false;
          }
        });
        // Restore button icons for panels we just restored
        $$(".panel-btn[data-panel]").forEach((b) => {
          if (b.dataset.panel && !lastMinimized[b.dataset.panel]) {
            b.classList.remove("minimized");
          }
        });
      } else {
        // Entering maximize mode. If the target panel is minimized,
        // restore it first so the maximize transition feels responsive.
        if (lastMinimized[maxTarget]) {
          restorePanel(maxTarget);
          lastMinimized[maxTarget] = false;
        }
        document.body.classList.add("max-" + maxTarget);
      }
    }
  }

  function minimizePanel(panel) {
    const el = {
      left: $("#panel-left"),
      center: $("#panel-center"),
      right: $("#panel-right"),
      bottom: $("#panel-bottom"),
    }[panel];
    if (!el) return;
    const stored = panelSizes[panel] || (panel === "bottom" ? 200 : 320);
    el.dataset.minimizeSaved = String(stored);
    if (panel === "bottom") {
      el.style.height = "30px";
      el.style.minHeight = "30px";
      el.style.maxHeight = "30px";
    } else {
      el.style.width = "28px";
      el.style.minWidth = "28px";
      el.style.maxWidth = "28px";
    }
  }

  function restorePanel(panel) {
    const el = {
      left: $("#panel-left"),
      center: $("#panel-center"),
      right: $("#panel-right"),
      bottom: $("#panel-bottom"),
    }[panel];
    if (!el || !el.dataset.minimizeSaved) return;
    const size = parseInt(el.dataset.minimizeSaved, 10);
    if (panel === "bottom") {
      el.style.height = size + "px";
      el.style.minHeight = PANEL_MIN_HEIGHTS.bottom + "px";
      el.style.maxHeight = "600px";
    } else {
      el.style.width = size + "px";
      el.style.minWidth = PANEL_MIN_WIDTHS[panel] + "px";
      el.style.maxWidth = "600px";
    }
    delete el.dataset.minimizeSaved;
  }

  function onResizeMove(e) {
    if (!resizing) return;
    if (resizing === "left" || resizing === "center-delta") {
      const newWidth = Math.max(
        PANEL_MIN_WIDTHS.left,
        Math.min(e.clientX, window.innerWidth - PANEL_MIN_WIDTHS.center - PANEL_MIN_WIDTHS.right - 10)
      );
      $("#panel-left").style.width = newWidth + "px";
      $("#panel-left").style.minWidth = PANEL_MIN_WIDTHS.left + "px";
      $("#panel-left").style.maxWidth = "600px";
      panelSizes.left = newWidth;
    }
    if (resizing === "right" || resizing === "center-delta") {
      const newWidth = Math.max(
        PANEL_MIN_WIDTHS.right,
        Math.min(
          window.innerWidth - e.clientX - PANEL_MIN_WIDTHS.left - 10,
          600
        )
      );
      if (newWidth > 0) {
        $("#panel-right").style.width = newWidth + "px";
        $("#panel-right").style.minWidth = PANEL_MIN_WIDTHS.right + "px";
        $("#panel-right").style.maxWidth = "600px";
        panelSizes.right = newWidth;
      }
    }
    if (resizing === "bottom") {
      const fromBottom = window.innerHeight - e.clientY;
      const newHeight = Math.max(
        PANEL_MIN_HEIGHTS.bottom,
        Math.min(fromBottom, window.innerHeight * 0.7)
      );
      $("#panel-bottom").style.height = newHeight + "px";
      $("#panel-bottom").style.minHeight = PANEL_MIN_HEIGHTS.bottom + "px";
      $("#panel-bottom").style.maxHeight = "600px";
      panelSizes.bottom = newHeight;
    }
  }

  function onResizeEnd() {
    resizing = null;
    $$(".resize-handle").forEach((h) => h.classList.remove("active"));
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }

  // Override mousedown on resize handles to set resizing target
  function reinitResizeHandles() {
    // Remove old listeners by cloning
    $$(".resize-handle").forEach((handle) => {
      const newHandle = handle.cloneNode(true);
      handle.parentNode.replaceChild(newHandle, handle);
      newHandle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const type = newHandle.dataset.resize;
        resizing = type;
        newHandle.addEventListener("mousemove", onResizeMove);
        newHandle.addEventListener("mouseup", onResizeEnd);
        newHandle.classList.add("active");
        document.body.style.cursor = type === "bottom"
          ? "row-resize"
          : "col-resize";
        document.body.style.userSelect = "none";
      });
    });

    // Re-bind panel buttons
    $$(".panel-btn").forEach((btn) => {
      btn.replaceWith(btn.cloneNode(true));
    });
    $$(".panel-btn").forEach((btn) => {
      btn.addEventListener("click", onPanelBtn);
    });
  }

  // ── Escape HTML ─────────────────────────────────────────────────────
  function esc(str) {
    if (!str) return "";
    const d = document.createElement("div");
    d.textContent = String(str);
    return d.innerHTML;
  }

  // ── Init ─────────────────────────────────────────────────────────────
  function init() {
    // Version display
    if (el.topbarInfo) el.topbarInfo.textContent = "0.9.1";

    // Event listeners
    el.newSupervisorBtn.addEventListener("click", createSupervisor);
    el.sendBtn.addEventListener("click", sendPrompt);
    el.promptInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendPrompt();
      }
    });
    el.goalDismissBtn?.addEventListener("click", dismissGoalBanner);
    el.completionCloseBtn?.addEventListener("click", dismissCompletionBanner);

    // Panel resize
    initResizeHandles();

    // Load initial supervisor list
    loadSupervisors();

    // Refresh supervisor list every 30s
    setInterval(() => {
      if (activeSupervisorId) {
        loadSupervisors();
        loadTasks();
      }
    }, 30000);
  }

  // Start when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();