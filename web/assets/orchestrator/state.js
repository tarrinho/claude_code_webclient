// orchestrator/state.js — the conversation state every module reads and writes.
//
// One exported object rather than one export per value, and that is forced
// rather than stylistic. An ES module export is a *live binding*: an importing
// module sees updates but cannot assign to it, so `supervisors = [...]` in
// another file is a syntax error. Seventeen of these twenty-one are reassigned
// somewhere, so exporting them individually would have broken the split in a
// way that only shows up at the first write.
//
// Mutating `state.x` works because the object identity never changes. The cost
// is the `state.` prefix everywhere, which is also the benefit: at a call site
// it is now obvious that a value is shared across modules rather than local to
// the file you are reading.
export const state = {
  supervisors: [],
  activeSupervisorId: null,
  activeSupervisor: null,
  tasks: [],
  // The run's spend, as returned alongside the task list. Null until the
  // first successful load: "not fetched yet" and "nothing spent" render
  // differently, and a orchestrator really can have spent nothing.
  cost: null,
  activeTaskId: null,
  chatMessages: [],
  eventLog: [],
  members: [],

  // The live EventSource, held so it can be closed. It used to be an
  // AbortController, which EventSource ignores -- see connectSSE().
  sseStream: null,
  csrfToken: "",

  // The 30s refresh poller, held rather than left bare. rules.md §4 names a
  // bare `setInterval` as the failure case: nothing can stop it, and it doubles
  // the moment its enclosing setup runs twice. This one was missed by the sweep
  // that fixed every other timer (`3a68c5c`) because that sweep, §4's grep and
  // the test enforcing it all looked only at `web/assets/*.js` -- and this was
  // the one client script that lived directly in `web/`. Moving it under
  // assets/ in 0.10.0 is what closed that blind spot.
  _refreshTimer: null,

  // A 1s ticker that re-renders the task tree while a task is running, so its
  // elapsed time counts up between the 30s polls above. Held the same way and
  // for the same reason as `_refreshTimer`: a bare setInterval cannot be
  // stopped and doubles if its setup runs twice. Started/stopped by
  // renderTaskTree itself rather than kept running always, since there is
  // nothing to tick while every task is pending, done, or failed.
  _tickTimer: null,

  // Smart-scroll state for chat and event log: follow along only when the
  // user is at the bottom (within 20px), otherwise let them read freely.
  // A floating button invites them back to the latest when they've scrolled up.
  chatScrollFollow: true,
  logScrollFollow: true,
  _chatScrollBtn: null,
  _logScrollBtn: null,

  // Notification badge count: SSE events that arrive while user is scrolled up.
  _unreadCount: 0,

  // Goal banner shrink: transitions to slim strip when user scrolls past it.
  _goalShrunk: false,

  // Set when the user expands the slim strip back by hand. Without it the very
  // next streamed message re-shrank the banner they had just expanded, so the
  // restore arrow looked like it did nothing. Cleared when a new goal is set.
  _goalUserRestored: false,

  // Expanded task row: only one detail row open at a time.
  _expandedTaskId: null,

  // Track previous orchestrator statuses so we can flash badges on change.
  _prevStatuses: {},

  // Panel sizing.
  panelSizes: { left: 320, right: 320, bottom: 200 },
  lastMinimized: {},
};

// Not part of `state`: these are constants, never assigned, so they need no
// shared mutable home and are clearer as plain exports.
export const PANEL_MIN_WIDTHS = { left: 200, center: 300, right: 200 };
export const PANEL_MIN_HEIGHTS = { bottom: 80 };
