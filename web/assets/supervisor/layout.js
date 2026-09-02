// supervisor/layout.js — panel sizing, resize handles, minimise.

import { state } from "./state.js";
import { PANEL_MIN_WIDTHS, PANEL_MIN_HEIGHTS } from "./state.js";
import { $, $$, el } from "./dom.js";

  // ── Panel sizing state ──────────────────────────────────────────────

  // ── Panel resize ────────────────────────────────────────────────────
  let resizing = null;

  export function initResizeHandles() {
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

  export function onPanelBtn(e) {
    const btn = e.currentTarget;
    const panel = btn.dataset.panel;
    const maxTarget = btn.dataset.max;

    if (panel) {
      // Minimize / restore
      if (state.lastMinimized[panel]) {
        restorePanel(panel);
        state.lastMinimized[panel] = false;
        btn.classList.remove("minimized");
        return;
      }
      minimizePanel(panel);
      state.lastMinimized[panel] = true;
      btn.classList.add("minimized");
    }

    if (maxTarget) {
      // Maximize / restore single panel
      if (document.body.classList.contains("max-" + maxTarget)) {
        document.body.classList.remove("max-" + maxTarget);
        // When exiting maximize mode, restore any panels that were
        // minimized while maximized — they are CSS-hidden by the
        // max-xxx rules until restorePanel strips that styling.
        Object.keys(state.lastMinimized).forEach((p) => {
          if (state.lastMinimized[p] && p !== maxTarget) {
            restorePanel(p);
            state.lastMinimized[p] = false;
          }
        });
        // Show all minimize buttons now that their panels are visible
        allMinimizeButtons.forEach((b) => (b.style.display = ""));
        // Restore button icons for panels we just restored
        $$(".panel-btn[data-panel]").forEach((b) => {
          if (b.dataset.panel && !state.lastMinimized[b.dataset.panel]) {
            b.classList.remove("minimized");
          }
        });
      } else {
        // Entering maximize mode. If the target panel is minimized,
        // restore it first so the maximize transition feels responsive.
        if (state.lastMinimized[maxTarget]) {
          restorePanel(maxTarget);
          state.lastMinimized[maxTarget] = false;
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
  export const allMinimizeButtons = (() => {
    const all = $$(".panel-btn[data-panel]");
    return Array.from(all).filter((b) => !b.dataset.max);
  })();

  export function minimizePanel(panel) {
    const el = {
      left: $("#panel-left"),
      center: $("#panel-center"),
      right: $("#panel-right"),
      bottom: $("#panel-bottom"),
    }[panel];
    if (!el) return;
    const stored = state.panelSizes[panel] || (panel === "bottom" ? 200 : 320);
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

  export function restorePanel(panel) {
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

  export function onResizeMove(e) {
    if (!resizing) return;
    if (resizing === "left" || resizing === "center-delta") {
      const newWidth = Math.max(
        PANEL_MIN_WIDTHS.left,
        Math.min(e.clientX, window.innerWidth - PANEL_MIN_WIDTHS.center - PANEL_MIN_WIDTHS.right - 10)
      );
      $("#panel-left").style.width = newWidth + "px";
      $("#panel-left").style.minWidth = PANEL_MIN_WIDTHS.left + "px";
      $("#panel-left").style.maxWidth = "600px";
      state.panelSizes.left = newWidth;
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
        state.panelSizes.right = newWidth;
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
      state.panelSizes.bottom = newHeight;
    }
  }

  export function onResizeEnd() {
    resizing = null;
    // Symmetric with the mousedown handler: these are on document now, so
    // they outlive the gesture unless removed here. Leaving them attached
    // would stack a new pair on every drag.
    document.removeEventListener("mousemove", onResizeMove);
    document.removeEventListener("mouseup", onResizeEnd);
    $$(".resize-handle").forEach((h) => h.classList.remove("active"));
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }

  // Override mousedown on resize handles to set resizing target
  export function reinitResizeHandles() {
    // Remove old listeners by cloning
    $$(".resize-handle").forEach((handle) => {
      const newHandle = handle.cloneNode(true);
      handle.parentNode.replaceChild(newHandle, handle);
      newHandle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const type = newHandle.dataset.resize;
        resizing = type;
        // On document, not on the handle. The handle is 5px wide, so a drag
        // faster than the pointer can stay inside it left the strip and the
        // move events stopped arriving -- the drag died mid-gesture with no
        // sign of why. Released in onResizeEnd.
        document.addEventListener("mousemove", onResizeMove);
        document.addEventListener("mouseup", onResizeEnd);
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

