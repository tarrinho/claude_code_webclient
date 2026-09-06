// orchestrator/api.js — CSRF and the fetch wrapper every module calls.

import { state } from "./state.js";
import { $ } from "./dom.js";

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

  export function getCsrf() {
    if (!state.csrfToken) {
      state.csrfToken = document.cookie
        .split("; ")
        .find((c) => c.startsWith("wc_csrf="))
        ?.split("=")[1] || "";
    }
    return state.csrfToken;
  }

  // ── API helpers ──────────────────────────────────────────────────────
  export async function apiFetch(url, opts = {}) {
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
  export function formatTime(value) {
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

