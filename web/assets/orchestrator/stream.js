// orchestrator/stream.js — the SSE connection and its event handling.

import { state } from "./state.js";
import { formatTime } from "./api.js";
import { incrementBadge, showCompletionBanner, shrinkGoalBanner, updateGateMarker } from "./banners.js";
import { $, el } from "./dom.js";
import { addChatMessage, renderChatMessages, renderSupervisorList } from "./list.js";
import { esc, showScrollBtn } from "./main.js";
import { loadTasks, renderTaskTree, updateOverallProgress, updatePauseResumeBtn } from "./tasks.js";

  // ── SSE stream ───────────────────────────────────────────────────────
  export function connectSSE() {
    // close(), not AbortController.abort(). EventSource's init dictionary
    // accepts only `withCredentials`; a `signal` member is silently ignored,
    // so the previous teardown never did anything. Verified in Chromium rather
    // than read off the spec: after abort() the stream is still readyState 1
    // (OPEN), and only close() reaches 2. Every orchestrator switch therefore
    // left a stream open on both ends, and the stale one kept delivering into
    // handleSSEEvent for a orchestrator the user had already left.
    if (state.sseStream) {
      state.sseStream.close();
      state.sseStream = null;
    }

    const url =
      "/api/orchestrators/" + state.activeSupervisorId + "/stream";

    const evtSource = new EventSource(url);
    state.sseStream = evtSource;

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
      if (state.sseStream !== evtSource) return;
      // Deliberately no close() here. EventSource reconnects on its own after
      // a transient failure, and closing it is precisely what prevents that --
      // so the old code announced a reconnection and then made it impossible,
      // leaving the page silently dead until a reload.
      addLogEntry("system", "Stream interrupted; reconnecting...");
    };

    window._supervisorSSE = evtSource;
  }

  export function handleSSEEvent(data) {
    switch (data.type) {
      case "progress":
        handleProgress(data);
        break;
      case "status":
        handleStatusUpdate(data);
        break;
      case "events":
        handleSSEEvents(data.events || []);
        break;
      case "messages":
        handleSSEMessages(data.messages || []);
        break;
      case "done":
        addLogEntry("system", "Orchestrator finished: " + data.status);
        addChatMessage("system", "Orchestrator completed with status: " + data.status);
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

  export function handleStatusUpdate(data) {
    if (data.status && state.activeSupervisor) {
      state.activeSupervisor.status = data.status;
      // Sync to the supervisors array — renderSupervisorList() renders from
      // that array, not from activeSupervisor. Without this the sidebar
      // badge stays stale for up to 30s (the poll interval).
      const sup = state.supervisors.find(s => s.id === state.activeSupervisorId);
      if (sup) sup.status = data.status;
      renderSupervisorList();
      updatePauseResumeBtn(data.status);
      updateOverallProgress();
      updateGateMarker(data.status, state.members);
    }
  }

  export function handleProgress(data) {
    // Update tasks from server data
    if (data.tasks) {
      const oldMap = new Map(state.tasks.map((t) => [t.id, t]));
      data.tasks.forEach((t) => {
        if (oldMap.has(t.id)) {
          Object.assign(oldMap.get(t.id), t);
        } else {
          state.tasks.push(t);
          oldMap.set(t.id, t);
        }
      });
      renderTaskTree();
    }
    updateOverallProgress();

    // Update orchestrator status if available
    if (data.status && state.activeSupervisor) {
      state.activeSupervisor.status = data.status;
      // Same fix as handleStatusUpdate — renderSupervisorList() renders from
      // the supervisors array, so we must keep it in sync.
      const sup = state.supervisors.find(s => s.id === state.activeSupervisorId);
      if (sup) sup.status = data.status;
      renderSupervisorList();
      updatePauseResumeBtn(data.status);
      updateGateMarker(data.status, state.members);
    }
  }

  export function handleSSEEvents(events) {
    if (events.length) incrementBadge(events.length);
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

  // Append incoming messages to the chat and render. New messages arrive
  // from the SSE stream (plan text, task results) so the chat stays live.
  export function handleSSEMessages(msgs) {
    // Filter out messages we already have (dedup by content).
    const known = new Set(state.chatMessages.map(m => m.content));
    const newMsgs = msgs.filter(m => !known.has(m.content || ""));
    if (newMsgs.length) {
      newMsgs.forEach(m => {
        try {
          state.chatMessages.push({
            role: m.role,
            content: m.content,
            created_at: m.created_at || new Date().toISOString(),
            metadata: m.metadata ? JSON.parse(m.metadata) : undefined,
          });
        } catch (_) { /* skip malformed */ }
      });
      renderChatMessages();
      // A message the user has scrolled away from is the main thing a badge is
      // for; counting only log events missed it.
      incrementBadge(newMsgs.length);
      // New chat content pushes the goal above the fold, so compact it. Guarded
      // on chatScrollFollow only: when the user has scrolled up they are not
      // looking at the banner and moving it under them is disorienting.
      if (state.chatScrollFollow) {
        shrinkGoalBanner();
      }
    }
  }

  export function addLogEntry(type, msg) {
    const time = formatTime(new Date().toISOString());
    const div = document.createElement("div");
    div.className = "log-entry";
    div.innerHTML = `
      <span class="log-time">${time}</span>
      <span class="log-type ${type}">${type}</span>
      <span class="log-msg">${esc(msg)}</span>
    `;
    el.eventLog.appendChild(div);
    if (state.logScrollFollow) {
      el.eventLog.scrollTop = el.eventLog.scrollHeight;
    } else {
      showScrollBtn(state._logScrollBtn);
    }
    state.eventLog.push({ type, msg, time });
  }

