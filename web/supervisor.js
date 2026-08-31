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
  // The live EventSource, held so it can be closed. It used to be an
  // AbortController, which EventSource ignores -- see connectSSE().
  let sseStream = null;
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
        // `error` first: app.py's HTTPException handler returns
        // {"error": "..."} for every JSON client, so reading only `detail`
        // meant every failure on this page displayed the words "API error"
        // and never the reason. That exact mismatch is registry entry #15,
        // found once before in the machine-edit form and fixed there only.
        msg = d.error || d.detail || msg;
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
    pauseResumeBtn: $("#pauseResumeBtn"),
  };

  // ── Supervisor list ──────────────────────────────────────────────────
  // Sort order for the supervisor list. Default "newest first" (by id,
  // which is a UUID, so it approximates creation time), but a toggle
  // makes "last active" the order so recently-used supervisors stay visible.
  let supervisorSortMode = "newest"; // "newest" | "updated"
  function setSupervisorSort(mode) {
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

  async function loadSupervisors() {
    try {
      const data = await apiFetch("/api/supervisors");
      supervisors = data.supervisors || [];
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

  function renderSupervisorList() {
    // An edit in progress outranks a refresh; the poll catches up when it ends.
    if (renamingId) return;
    if (!supervisors.length) {
      el.supervisorList.innerHTML =
        '<div class="empty-state">No supervisors yet.<br>Click <b>+ New</b> to create one.</div>';
      return;
    }
    // Sort the list. Default newest-first (UUID ≈ creation time),
    // but "updated" sorts by last activity so the most-recently-used
    // supervisors stay at the top.
    const sorted = supervisors
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
        return `<div class="supervisor-list-item ${
          s.id === activeSupervisorId ? "active" : ""
        }" data-id="${s.id}">
          <div class="sl-title">${esc(s.title || "Untitled")}</div>
          <div class="sl-status">
            <span class="status-badge ${statusClass}">${statusClass}</span>
            ${s.progress_pct != null ? `<span>${Math.round(s.progress_pct)}%</span>` : ""}
            ${timeLabel ? `<span style="margin-left:4px">${timeLabel}</span>` : ""}
          </div>
        </div>`;
      })
      .join("");

    el.supervisorList.querySelectorAll(".supervisor-list-item").forEach((row) => {
      row.addEventListener("click", () => selectSupervisor(row.dataset.id));

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

  function startRename(id, titleEl) {
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

  // Which supervisor was last open, so a reload returns to it. The page had no
  // notion of this: selectSupervisor was reachable only from a click on a list
  // row or immediately after creating one, so opening /supervisor.html always
  // showed the start screen and left an existing conversation invisible --
  // messages were never even fetched, because showActiveSupervisor is what
  // fetches them. The main app has done this with wc_last_chat all along.
  const LAST_OPEN_KEY = "wc_last_supervisor";

  function rememberOpen(id) {
    try {
      localStorage.setItem(LAST_OPEN_KEY, id);
    } catch (e) {
      // Private mode or a full quota. Losing the memory of which supervisor was
      // open must not stop it being opened.
      console.warn("could not remember the open supervisor:", e);
    }
  }

  function restoreOpen() {
    if (activeSupervisorId || !supervisors.length) return;
    let wanted = null;
    try {
      wanted = localStorage.getItem(LAST_OPEN_KEY);
    } catch (e) {
      console.warn("could not read the last open supervisor:", e);
    }
    // The remembered one if it still exists, otherwise the most recent, because
    // an empty centre panel next to a populated list reads as a broken page.
    const found = supervisors.find((s) => s.id === wanted);
    selectSupervisor((found || supervisors[0]).id);
  }

  function selectSupervisor(id) {
    activeSupervisorId = id;
    rememberOpen(id);
    renderSupervisorList();
    showActiveSupervisor();
  }

  async function showActiveSupervisor() {
    if (!activeSupervisorId) return;
    try {
      const data = await apiFetch("/api/supervisors/" + activeSupervisorId);
      activeSupervisor = data.supervisor;
      // The pause button reflects what the server says, not what the engine
      // had last time — the engine may have been restarted between page loads.
      updatePauseResumeBtn(activeSupervisor.status);
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
      // Said out loud. This used to assign [] and not re-render, so a failed
      // fetch looked exactly like a supervisor that had never been asked
      // anything -- and the request the user had just sent was simply absent.
      chatMessages = [];
      renderChatMessages();
      addChatMessage("system", "Could not load this conversation: " + e.message);
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
          ${t.depends_on && t.depends_on.length ? `<div class="task-deps">depends on: ${t.depends_on.map(d => esc(d)).join(", ")}</div>` : ""}
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
    // close(), not AbortController.abort(). EventSource's init dictionary
    // accepts only `withCredentials`; a `signal` member is silently ignored,
    // so the previous teardown never did anything. Verified in Chromium rather
    // than read off the spec: after abort() the stream is still readyState 1
    // (OPEN), and only close() reaches 2. Every supervisor switch therefore
    // left a stream open on both ends, and the stale one kept delivering into
    // handleSSEEvent for a supervisor the user had already left.
    if (sseStream) {
      sseStream.close();
      sseStream = null;
    }

    const url =
      "/api/supervisors/" + activeSupervisorId + "/stream";

    const evtSource = new EventSource(url);
    sseStream = evtSource;

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

    evtSource.onerror = function () {
      // A stream we replaced is not an error worth reporting.
      if (sseStream !== evtSource) return;
      // Deliberately no close() here. EventSource reconnects on its own after
      // a transient failure, and closing it is precisely what prevents that --
      // so the old code announced a reconnection and then made it impossible,
      // leaving the page silently dead until a reload.
      addLogEntry("system", "Stream interrupted; reconnecting...");
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
      updatePauseResumeBtn(data.status);
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
        // Show all minimize buttons now that their panels are visible
        allMinimizeButtons.forEach((b) => (b.style.display = ""));
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
        // Hide minimize buttons on panels that are about to be hidden by
        // CSS (so clicking them doesn't restore → instantly re-hidden).
        allMinimizeButtons.forEach((b) => {
          if (b.dataset.panel && b.dataset.panel !== maxTarget) {
            b.style.display = "none";
          }
        });
      }
    }
  }

  // Reference to minimize buttons for hiding/showing during maximize transitions.
  // Each panel has one minimize button with data-panel set and no data-max.
  const allMinimizeButtons = (() => {
    const all = $$(".panel-btn[data-panel]");
    return Array.from(all).filter((b) => !b.dataset.max);
  })();

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

  // ── Members ──────────────────────────────────────────────────────────
  // The conversations and agents a supervisor watches. Deliberately separate
  // from the task tree: a task is work the supervisor invented and runs
  // headless, a member is work that already existed and belongs to someone.
  //
  // Nothing here dispatches on its own. A member can be prompted, but only by
  // a person clicking -- driving a live agent types into its terminal, and the
  // threat model's F-02 is narrowed rather than closed, so the human deciding
  // to press send is the check that mechanism still has.

  // This page has no toast. createSupervisor reports failures with alert(), so
  // errors follow that; the quieter outcomes get an inline line instead, since
  // an alert for "already a member" would be worse than the information.
  //
  // Calling a showToast() that does not exist is what broke this feature on the
  // real page while the tests passed: the harness supplied one, so the tests
  // were asserting against a contract the file never had.
  function membersNotice(message, isError) {
    if (isError) {
      alert(message);
      return;
    }
    const panel = document.getElementById("membersPanel");
    if (!panel) return;
    const note = document.createElement("p");
    note.className = "members-note";
    note.textContent = message;
    panel.prepend(note);
    setTimeout(() => note.remove(), 5000);
  }

  function membersEmpty(message) {
    const p = document.createElement("p");
    p.className = "members-empty";
    p.textContent = message;
    return p;
  }

  async function loadMembers(supervisorId) {
    const panel = document.getElementById("membersPanel");
    if (!panel || !supervisorId) return;
    let members = [];
    try {
      // apiFetch here returns the PARSED BODY and throws on a non-2xx. It is
      // not app.js's apiFetch, which returns a Response and resolves for 4xx.
      // Checking `.ok` on a parsed body finds undefined and fails every call.
      members = (await apiFetch(
        `/api/supervisors/${encodeURIComponent(supervisorId)}/members`)).members || [];
    } catch (err) {
      // The panel keeps whatever it last showed rather than going blank: one
      // failed poll is not evidence that the membership changed.
      if (!panel.children.length) panel.replaceChildren(membersEmpty(err.message));
      return;
    }
    panel.replaceChildren();
    if (!members.length) {
      panel.appendChild(membersEmpty("No members yet. Use + to add one."));
      return;
    }
    members.forEach((m) => {
      const row = document.createElement("div");
      row.className = "member-row";
      const name = document.createElement("span");
      name.className = "member-title";
      name.textContent = m.title || "Untitled";
      const badge = document.createElement("span");
      // The reason is more useful than the status when there is one: "failed"
      // says what to do, "waiting" only says that something is true.
      badge.className = `member-status member-${m.reason || m.status || "idle"}`;
      badge.textContent = m.reason || m.status || "idle";
      // Heartbeat: a timestamp so the user knows when this member last
      // produced output. A row that says "working" but hasn't moved in
      // hours is ambiguous; a row that shows the time tells them whether
      // the agent is alive or stuck.
      let heartbeat = null;
      if (m.last_seen) {
        try { heartbeat = new Date(m.last_seen); } catch (_) {}
      }
      if (heartbeat && !Number.isNaN(heartbeat.getTime())) {
        const span = document.createElement("span");
        span.className = "member-heartbeat";
        span.textContent = formatTime(m.last_seen);
        span.title = heartbeat.toLocaleString();
        row.appendChild(span);
      }
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "panel-btn";
      drop.textContent = "\u00d7";
      drop.title = `Stop watching ${m.title || "this conversation"}`;
      drop.setAttribute("aria-label", `Stop watching ${m.title || "this conversation"}`);
      drop.addEventListener("click", () => removeMember(supervisorId, m.id));
      row.append(badge, drop);
      if (m.preview) row.title = m.preview;
      panel.appendChild(row);
    });
  }

  async function removeMember(supervisorId, chatId) {
    try {
      await apiFetch(
        `/api/supervisors/${encodeURIComponent(supervisorId)}/members/` +
          encodeURIComponent(chatId),
        { method: "DELETE" });
      // Removing membership never deletes the conversation, so say that
      // plainly -- an ambiguous message here invites a nervous double-check.
      membersNotice("Removed. The conversation itself is untouched.");
    } catch (err) {
      membersNotice(err.message, true);
    }
    loadMembers(supervisorId);
  }

  function closeMembersPicker() {
    document.getElementById("membersPickerDialog")?.remove();
  }

  async function openMembersPicker(supervisorId) {
    closeMembersPicker();
    const backdrop = document.createElement("div");
    backdrop.className = "dialog-backdrop open";
    backdrop.id = "membersPickerDialog";
    backdrop.setAttribute("role", "dialog");
    backdrop.setAttribute("aria-modal", "true");
    backdrop.setAttribute("aria-label", "Add members to this supervisor");
    const panel = document.createElement("div");
    panel.className = "dialog";
    const heading = document.createElement("h2");
    heading.textContent = "Add members";
    const help = document.createElement("p");
    help.textContent = "Loading\u2026";
    panel.append(heading, help);
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeMembersPicker();
    });

    let agents = [];
    let chats = [];
    let existing = new Set();
    try {
      const [sessionsBody, chatsBody, membersBody] = await Promise.all([
        apiFetch("/api/sessions"),
        apiFetch("/api/chats"),
        apiFetch(`/api/supervisors/${encodeURIComponent(supervisorId)}/members`),
      ]);
      // A live agent that already has a conversation is offered as that
      // conversation, not twice: /api/sessions filters linked sessions out and
      // marks web chats with webchat, so the two groups cannot overlap.
      agents = (sessionsBody.sessions || []).filter((s) => s.sessionId && !s.webchat);
      chats = chatsBody.chats || [];
      existing = new Set((membersBody.members || []).map((m) => m.id));
    } catch (err) {
      help.textContent = err.message;
      return;
    }

    if (!agents.length && !chats.length) {
      help.textContent = "Nothing to add yet \u2014 no agents or conversations.";
      return;
    }
    help.textContent = "Pick the agents and conversations this supervisor should watch.";

    const filter = document.createElement("input");
    filter.type = "search";
    filter.placeholder = "Filter by name\u2026";
    filter.setAttribute("aria-label", "Filter members");
    panel.appendChild(filter);

    const list = document.createElement("div");
    list.className = "members-picker-list";

    const addGroup = (title, items, kind) => {
      if (!items.length) return;
      const label = document.createElement("h3");
      label.textContent = title;
      list.appendChild(label);
      items.forEach((item) => {
        const refId = kind === "session" ? item.sessionId : item.id;
        const name = (kind === "session" ? item.name : item.title) || refId;
        const row = document.createElement("label");
        row.className = "members-picker-row";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.dataset.kind = kind;
        box.dataset.refId = refId;
        // An existing member is shown ticked and disabled: the dialog states
        // the current membership rather than offering a click that does
        // nothing and reads as broken.
        if (existing.has(refId)) {
          box.checked = true;
          box.disabled = true;
        }
        const text = document.createElement("span");
        text.textContent = existing.has(refId) ? `${name} (already a member)` : name;
        row.dataset.search = name.toLowerCase();
        row.append(box, text);
        list.appendChild(row);
      });
    };
    addGroup("Live agents", agents, "session");
    addGroup("Conversations", chats, "chat");
    panel.appendChild(list);

    filter.addEventListener("input", () => {
      const needle = filter.value.trim().toLowerCase();
      list.querySelectorAll(".members-picker-row").forEach((row) => {
        row.hidden = Boolean(needle) && !row.dataset.search.includes(needle);
      });
    });

    const actions = document.createElement("div");
    actions.className = "dialog-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", closeMembersPicker);
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.dataset.action = "confirm-members";
    confirm.textContent = "Add";
    confirm.addEventListener("click", () => submitMembers(supervisorId, list));
    actions.append(cancel, confirm);
    panel.appendChild(actions);
    filter.focus();
  }

  async function submitMembers(supervisorId, list) {
    const chosen = [...list.querySelectorAll("input[type=checkbox]")]
      .filter((b) => b.checked && !b.disabled)
      .map((b) => ({ kind: b.dataset.kind, ref_id: b.dataset.refId }));
    if (!chosen.length) {
      // An empty POST is a 400 the user did nothing to deserve.
      closeMembersPicker();
      return;
    }
    // Held rather than shown here: loadMembers below replaces the panel's
    // children, so a note written now would be wiped by its own refresh.
    let pending;
    try {
      const result = await apiFetch(
        `/api/supervisors/${encodeURIComponent(supervisorId)}/members`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ members: chosen }),
        });
      const added = (result.added || []).length;
      const already = (result.already_members || []).length;
      const failed = (result.failed || []).length;
      if (failed) {
        // Both halves, always. Nine added and one refused must not look like
        // ten added, and must not look like a total failure either.
        pending = { message: `Added ${added}. ${failed} could not be added.`,
                    isError: true };
      } else if (added) {
        pending = { message: `Added ${added} member${added === 1 ? "" : "s"}.` };
      } else {
        pending = { message: `Already ${already === 1 ? "a member" : "members"}.` };
      }
    } catch (err) {
      pending = { message: err.message, isError: true };
    }
    closeMembersPicker();
    await loadMembers(supervisorId);
    if (pending) membersNotice(pending.message, pending.isError);
  }
  // ── Members end ──────────────────────────────────────────────────────

  // ── Init ─────────────────────────────────────────────────────────────
  function init() {
    // Version display
    if (el.topbarInfo) el.topbarInfo.textContent = "0.9.2";

    // Event listeners
    el.newSupervisorBtn.addEventListener("click", createSupervisor);
    el.sendBtn.addEventListener("click", sendPrompt);
    el.promptInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendPrompt();
      }
    });

    // Auto-grow the composer textarea as the user types. Starts at 38px,
    // expands up to 200px, collapses back when cleared. A soft clamp so a
    // long prompt does not swallow the chat while still fitting the text.
    function autoGrowComposer() {
      if (!el.promptInput) return;
      el.promptInput.style.height = "38px";
      const needed = el.promptInput.scrollHeight;
      el.promptInput.style.height = Math.min(200, Math.max(38, needed)) + "px";
    }
    el.promptInput.addEventListener("input", autoGrowComposer);

    el.goalDismissBtn?.addEventListener("click", dismissGoalBanner);
    document
      .getElementById("addMembersBtn")
      ?.addEventListener("click", () => {
        if (activeSupervisorId) openMembersPicker(activeSupervisorId);
        else membersNotice("Pick a supervisor first.", true);
      });
    el.completionCloseBtn?.addEventListener("click", dismissCompletionBanner);
    el.pauseResumeBtn?.addEventListener("click", togglePauseResume);

    // Sort toggle — cycles between newest-first and last-active.
    // Clicking the same button toggles; the saved preference survives reloads.
    const sortEl = el.sortToggleBtn || $("#sortToggleBtn");
    if (sortEl) {
      sortEl.addEventListener("click", () => {
        supervisorSortMode = supervisorSortMode === "updated" ? "newest" : "updated";
        setSupervisorSort(supervisorSortMode);
      });
    }

    // Panel resize
    initResizeHandles();

    // Load initial supervisor list
    loadSupervisors();

    // Refresh every 30s. The list refresh is deliberately outside the guard:
    // it used to sit inside it, so with nothing selected -- the state the page
    // opens in -- the list never updated at all, and a supervisor created
    // anywhere else appeared only after a manual reload. Tasks genuinely need
    // an active supervisor; the list does not.
    setInterval(() => {
      loadSupervisors();
      if (activeSupervisorId) {
        loadTasks();
      }
    }, 30000);
  }

  // ── Pause / Resume ──────────────────────────────────────────────────
  // A supervisor that is planning or running can be put on hold and resumed
  // later. The button sits in the chat panel header because that is the
  // place you look at while a supervisor is working; it shows the pause
  // symbol when paused so you know the opposite action is available.
  function updatePauseResumeBtn(status) {
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

  async function togglePauseResume() {
    if (!activeSupervisorId) return;
    if (!el.pauseResumeBtn) return;
    // Decide which action to take from the current button text.
    const action = el.pauseResumeBtn.textContent === "⏸" ? "pause" : "resume";
    try {
      await apiFetch(`/api/supervisors/${encodeURIComponent(activeSupervisorId)}/${action}`, {
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
})();