// supervisor/members.js — the members panel and its picker.

import { apiFetch, formatTime } from "./api.js";
import { $ } from "./dom.js";
import { state } from "./state.js";
import { updateGateMarker } from "./banners.js";

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
  export function membersNotice(message, isError) {
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

  export function membersEmpty(message) {
    const p = document.createElement("p");
    p.className = "members-empty";
    p.textContent = message;
    return p;
  }

  export async function loadMembers(supervisorId) {
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
    state.members = members;
    updateGateMarker(state.activeSupervisor?.status, state.members);
    panel.replaceChildren();
    if (!members.length) {
      panel.appendChild(membersEmpty("No members yet. Use + to add one."));
      return;
    }
    members.forEach((m) => {
      const row = document.createElement("div");
      row.className = "member-row";
      row.dataset.chatId = m.id;
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

  export async function removeMember(supervisorId, chatId) {
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

  export function closeMembersPicker() {
    document.getElementById("membersPickerDialog")?.remove();
  }

  export async function openMembersPicker(supervisorId) {
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

  export async function submitMembers(supervisorId, list) {
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

