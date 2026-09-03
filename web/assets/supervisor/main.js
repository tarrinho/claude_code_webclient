// supervisor/main.js — Supervisor orchestration UI.
//
// An ES module rather than an IIFE: a module has its own scope, so the
// wrapper that used to provide one is gone. It lives under /assets/ so
// StaticFiles serves it, which is what let _serve_supervisor_js be deleted
// from app.py -- that route existed only to set the MIME type by hand.
//
// Body indentation is still the IIFE's. Dedenting is a separate commit:
// eleven multi-line template literals carry HTML, and their leading
// whitespace is content, so that change deserves its own verification.


  // ── State ────────────────────────────────────────────────────────────
  // The live EventSource, held so it can be closed. It used to be an
  // AbortController, which EventSource ignores -- see connectSSE().

  // The 30s refresh poller, held rather than left bare. rules.md §4 names a
  // bare `setInterval` as the failure case: nothing can stop it, and it doubles
  // the moment its enclosing setup runs twice. This one was missed by the sweep
  // that fixed every other timer (`3a68c5c`) because that sweep, §4's grep and
  // the test enforcing it all looked only at `web/assets/*.js` -- and this is
  // the one client script that lives directly in `web/`.

  // Smart-scroll state for chat and event log: follow along only when the
  // user is at the bottom (within 20px), otherwise let them read freely.
  // A floating button invites them back to the latest when they've scrolled up.

  // Notification badge count: SSE events that arrive while user is scrolled up.

  // Goal banner shrink: transitions to slim strip when user scrolls past it.

  // Set when the user expands the slim strip back by hand. Without it the very
  // next streamed message re-shrank the banner they had just expanded, so the
  // restore arrow looked like it did nothing. Cleared when a new goal is set.

  // Expanded task row: only one detail row open at a time.

  // Track previous supervisor statuses so we can flash badges on change.

  // ── Panel sizing state ──────────────────────────────────────────────

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
import { state, PANEL_MIN_WIDTHS, PANEL_MIN_HEIGHTS } from "./state.js";
import { clearBadge, dismissCompletionBanner, dismissGoalBanner, restoreGoalBanner } from "./banners.js";
import { $, el } from "./dom.js";
import { initResizeHandles } from "./layout.js";
import { createSupervisor, loadSupervisors, setSupervisorSort } from "./list.js";
import { membersNotice, openMembersPicker } from "./members.js";
import { loadTasks, sendPrompt, togglePauseResume } from "./tasks.js";

  // ── State ────────────────────────────────────────────────────────────
  // The live EventSource, held so it can be closed. It used to be an
  // AbortController, which EventSource ignores -- see connectSSE().

  // The 30s refresh poller, held rather than left bare. rules.md §4 names a
  // bare `setInterval` as the failure case: nothing can stop it, and it doubles
  // the moment its enclosing setup runs twice. This one was missed by the sweep
  // that fixed every other timer (`3a68c5c`) because that sweep, §4's grep and
  // the test enforcing it all looked only at `web/assets/*.js` -- and this is
  // the one client script that lives directly in `web/`.

  // Smart-scroll state for chat and event log: follow along only when the
  // user is at the bottom (within 20px), otherwise let them read freely.
  // A floating button invites them back to the latest when they've scrolled up.

  // Notification badge count: SSE events that arrive while user is scrolled up.

  // Goal banner shrink: transitions to slim strip when user scrolls past it.

  // Set when the user expands the slim strip back by hand. Without it the very
  // next streamed message re-shrank the banner they had just expanded, so the
  // restore arrow looked like it did nothing. Cleared when a new goal is set.

  // Expanded task row: only one detail row open at a time.

  // Track previous supervisor statuses so we can flash badges on change.

  // ── Escape HTML ─────────────────────────────────────────────────────
  export function esc(str) {
    if (!str) return "";
    const d = document.createElement("div");
    d.textContent = String(str);
    return d.innerHTML;
  }

  // ── Init ─────────────────────────────────────────────────────────────
  export function init() {
    // Version display
    if (el.topbarInfo) el.topbarInfo.textContent = "0.10.4";

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
    el.goalRestoreBtn?.addEventListener("click", restoreGoalBanner);

    // Keyboard shortcuts — global.
    //
    // Whether a shortcut may fire while the user is typing is decided per
    // shortcut, not globally. The unmodified digits are the ones that matter:
    // without a guard, every "1" typed into the composer threw focus at the
    // task tree, which made the composer unusable for any prompt containing a
    // number -- a worse bug than the one the shortcut fixed.
    const PANEL_KEYS = {
      "1": () => el.taskTree,
      "2": () => el.chatMessages,
      "3": () => el.eventLog,
      "4": () => el.promptInput,
    };

    document.addEventListener("keydown", (e) => {
      // Send. Deliberately allowed while typing: the composer is where you
      // are when you want it.
      if ((e.ctrlKey || e.altKey) && e.key === "Enter") {
        e.preventDefault();
        sendPrompt();
        return;
      }
      // New supervisor. Both cases, so Shift does not swallow it.
      if (e.ctrlKey && (e.key === "n" || e.key === "N")) {
        e.preventDefault();
        createSupervisor();
        return;
      }
      // Escape closes whichever banner is up. Also allowed while typing --
      // dismissing a banner should not cost you the composer.
      if (e.key === "Escape") {
        if (!el.goalBanner.hidden) dismissGoalBanner();
        else if (!el.completionBanner.hidden) dismissCompletionBanner();
        return;
      }
      // Panel focus: never while typing, never as part of a chord.
      if (isTypingTarget(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
      const panel = PANEL_KEYS[e.key];
      if (!panel) return;
      const node = panel();
      if (!node) return;
      e.preventDefault();
      node.focus();
    });
    document
      .getElementById("addMembersBtn")
      ?.addEventListener("click", () => {
        if (state.activeSupervisorId) openMembersPicker(state.activeSupervisorId);
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
    // Guarded, not merely assigned: `init()` runs on DOMContentLoaded, which
    // fires once per document today -- but that is a property of where the call
    // sits rather than of the code, and this page is loaded in an iframe whose
    // `src` the console resets each time the supervisor pane opens. The guard
    // makes a second `init()` idempotent instead of doubling the poll rate.
    if (!state._refreshTimer) {
      state._refreshTimer = setInterval(() => {
        loadSupervisors();
        if (state.activeSupervisorId) {
          loadTasks();
        }
      }, 30000);
    }

    // Stop the poller with the document rather than leaving it to be torn down
    // implicitly. The iframe is reset rather than navigated, so `pagehide` is
    // the event that reliably fires for it; a poll that outlives its document
    // is the thing the handle exists to prevent.
    window.addEventListener("pagehide", () => {
      if (state._refreshTimer) {
        clearInterval(state._refreshTimer);
        state._refreshTimer = null;
      }
    });

    // Smart scroll: follow along only when the user is at the bottom (within
    // 20px). A floating button invites them back when they've scrolled up.
    state._chatScrollBtn = document.getElementById("chat-scroll-btn");
    state._logScrollBtn = document.getElementById("log-scroll-btn");

    if (state._chatScrollBtn) {
      state._chatScrollBtn.addEventListener("click", () => {
        el.chatMessages.scrollTo({ top: el.chatMessages.scrollHeight, behavior: "smooth" });
        state.chatScrollFollow = true;
        hideScrollBtn(state._chatScrollBtn);
        // Explicit, not left to the scroll handler: the smooth scroll may be
        // interrupted before it ever reports reaching the bottom.
        clearBadge();
      });
    }
    if (state._logScrollBtn) {
      state._logScrollBtn.addEventListener("click", () => {
        el.eventLog.scrollTo({ top: el.eventLog.scrollHeight, behavior: "smooth" });
        state.logScrollFollow = true;
        hideScrollBtn(state._logScrollBtn);
      });
    }

    if (el.chatMessages) {
      el.chatMessages.addEventListener("scroll", function () {
        if (isNearBottom(el.chatMessages)) {
          state.chatScrollFollow = true;
          hideScrollBtn(state._chatScrollBtn);
          // Back at the bottom: they have caught up by definition.
          clearBadge();
        } else {
          state.chatScrollFollow = false;
          showScrollBtn(state._chatScrollBtn);
        }
      });
    }
    if (el.eventLog) {
      el.eventLog.addEventListener("scroll", function () {
        if (isNearBottom(el.eventLog)) {
          state.logScrollFollow = true;
          hideScrollBtn(state._logScrollBtn);
        } else {
          state.logScrollFollow = false;
          showScrollBtn(state._logScrollBtn);
        }
      });
    }
  }

  // ── Keyboard helpers ───────────────────────────────────────────────

  // Whether a keystroke on this element is the user writing text. Bare-key
  // shortcuts must stand down for these, or they eat the keystroke.
  export function isTypingTarget(node) {
    if (!node) return false;
    const tag = (node.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select"
      || node.isContentEditable === true;
  }

  // ── Scroll helpers ─────────────────────────────────────────────────

  export function isNearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= 20;
  }

  export function showScrollBtn(btn) {
    if (btn) btn.classList.add("visible");
  }

  export function hideScrollBtn(btn) {
    if (btn) btn.classList.remove("visible");
  }

