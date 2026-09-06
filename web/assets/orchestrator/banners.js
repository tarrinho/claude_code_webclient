// orchestrator/banners.js — goal, completion and the unread badge.

import { state } from "./state.js";
import { $, el } from "./dom.js";
import { addChatMessage } from "./list.js";

  // ── Goal banner ──────────────────────────────────────────────────────
  let _currentGoal = null;

  export function showGoalBanner(promptText) {
    _currentGoal = promptText;
    // A new goal starts expanded, whatever the user did to the last one.
    state._goalShrunk = false;
    state._goalUserRestored = false;
    el.goalBanner.classList.remove("slim");
    if (el.goalRestoreBtn) el.goalRestoreBtn.hidden = true;
    el.goalText.textContent = promptText;
    el.goalBanner.hidden = false;
    // Scroll goal banner into view
    el.goalBanner.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  export function dismissGoalBanner() {
    _currentGoal = null;
    state._goalShrunk = false;
    state._goalUserRestored = false;
    el.goalBanner.classList.remove("slim");
    el.goalBanner.hidden = true;
    if (el.goalRestoreBtn) el.goalRestoreBtn.hidden = true;
  }

  export function restoreGoalBanner() {
    state._goalShrunk = false;
    // Sticky: this is the user overruling the auto-shrink, so it has to
    // outlast the next message.
    state._goalUserRestored = true;
    el.goalBanner.classList.remove("slim");
    if (el.goalRestoreBtn) el.goalRestoreBtn.hidden = true;
  }

  export function shrinkGoalBanner() {
    if (!_currentGoal || state._goalShrunk || state._goalUserRestored) return;
    state._goalShrunk = true;
    el.goalBanner.classList.add("slim");
    if (el.goalRestoreBtn) el.goalRestoreBtn.hidden = false;
  }

  // ── Notification badge ────────────────────────────────────────────

  export function updateNotificationBadge() {
    const badge = el.topbarBadge;
    if (!badge) return;
    if (state._unreadCount > 0) {
      badge.textContent = state._unreadCount > 99 ? "99+" : String(state._unreadCount);
      badge.classList.add("visible");
      badge.hidden = false;
    } else {
      badge.classList.remove("visible");
      badge.hidden = true;
    }
  }

  // Count only what the user cannot currently see. While they are parked at
  // the bottom the content is already in front of them, so a badge would just
  // be noise they have to clear.
  export function incrementBadge(n) {
    if (state.chatScrollFollow) return;
    state._unreadCount += n > 0 ? n : 1;
    updateNotificationBadge();
  }

  export function clearBadge() {
    if (state._unreadCount === 0) return;
    state._unreadCount = 0;
    updateNotificationBadge();
  }

  // ── Completion banner ────────────────────────────────────────────────
  let _completionData = null;

  export function showCompletionBanner(eng) {
    // Gather results from all tasks
    const allTasks = state.tasks;
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
    addChatMessage("system", `✅ Orchestrator completed: ${doneCount}/${total} tasks done.`);
  }

  export function dismissCompletionBanner() {
    _completionData = null;
    el.completionBanner.hidden = true;
  }

  // ── Human-gate marker ────────────────────────────────────────────────
  // The one topbar element every body.max-* state leaves visible, so this is
  // the only reliable place to say "something needs you" regardless of which
  // panel is currently maximized. Two things actually gate a orchestrator on a
  // person -- the engine paused, or a watched member whose own status is
  // "waiting" -- see docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md
  // for why the task DAG itself never does (subtasks run one-shot,
  // non-interactive turns and never wait on anyone).

  export function updateGateMarker(supervisorStatus, members) {
    const badge = el.topbarGate;
    if (!badge) return;
    const waitingMember = (members || []).find((m) => m.status === "waiting");
    const gate = supervisorStatus === "paused"
      ? { kind: "paused" }
      : waitingMember
        ? { kind: "member", member: waitingMember }
        : null;
    if (!gate) {
      badge.classList.remove("visible");
      badge.hidden = true;
      badge.onclick = null;
      return;
    }
    badge.classList.add("visible");
    badge.hidden = false;
    badge.textContent = "!";
    badge.title = gate.kind === "paused"
      ? "Paused — resume when ready"
      : `Waiting on you: ${gate.member.title || gate.member.id}`;
    badge.onclick = () => {
      document.body.classList.remove("max-left", "max-right", "max-center", "max-bottom");
      if (gate.kind === "paused") {
        el.pauseResumeBtn?.focus();
      } else {
        const row = document.querySelector(
          `.member-row[data-chat-id="${CSS.escape(gate.member.id)}"]`
        ) || document.getElementById("membersPanel");
        row?.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    };
  }

