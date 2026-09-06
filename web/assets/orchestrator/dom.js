// orchestrator/dom.js — the element handles every module reaches for.
//
// A leaf module on purpose. These lived in main.js, which imports every
// other module, so every user of `$$` or `el` closed a cycle -- and a
// cycle is only harmless while nothing touches the binding during module
// evaluation. layout.js does: `allMinimizeButtons` is an IIFE that runs
// as the module loads, and it threw `Cannot access '$$' before
// initialization` because layout.js is evaluated before main.js finishes.
// With no imports of its own this module is always initialised first.


  // ── DOM refs ─────────────────────────────────────────────────────────
  export const $ = (s) => document.querySelector(s);
  export const $$ = (s) => document.querySelectorAll(s);

  export const el = {
    supervisorList: $("#orchestrator-list"),
    taskTree: $("#task-tree"),
    taskRail: $("#task-rail"),
    chatMessages: $("#chat-messages"),
    eventLog: $("#event-log"),
    composer: $("#composer"),
    promptInput: $("#prompt-input"),
    sendBtn: $("#send-btn"),
    startScreen: $("#start-screen"),
    supervisorChat: $("#orchestrator-chat"),
    detailContent: $("#detail-content"),
    progressBarFill: $("#overall-progress-fill"),
    newSupervisorBtn: $("#new-orchestrator-btn"),
    topbarInfo: $("#topbar-info"),
    goalBanner: $("#goal-banner"),
    goalText: $("#goal-text"),
    completionBanner: $("#completion-banner"),
    completionStats: $("#completion-stats"),
    completionSummary: $("#completion-summary"),
    goalDismissBtn: $(".goal-banner-dismiss"),
    topbarBadge: $("#topbar-badge"),
    topbarGate: $("#topbar-gate"),
    goalRestoreBtn: $(".goal-banner-restore"),
    completionCloseBtn: $(".completion-close"),
    pauseResumeBtn: $("#pauseResumeBtn"),
  };

