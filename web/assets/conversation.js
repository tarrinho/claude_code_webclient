// Transcript rendering, drafts, and deterministic SSE stream lifecycle.

const STREAM_LABELS = {
  ready: 'Ready',
  connecting: 'Connecting…',
  thinking: 'Thinking…',
  retrying: 'Retrying…',
  responding: 'Responding…',
  stopped: 'Stopped',
  failed: 'Failed',
};
const ACTIVE_STATES = new Set(['connecting', 'thinking', 'retrying', 'responding']);
let _queueManuallyHidden = false;

// Focusing a text input opens the on-screen keyboard on a touch device, and on a
// phone that keyboard covers most of the conversation. So opening a chat must
// not do it: the keyboard belongs to the moment the user taps the composer,
// which is the only moment they have said they want to type. On a pointer device
// the focus costs nothing and being able to type straight away is the point, so
// the behaviour is kept there rather than removed for everyone.
//
// Matched on `(hover: hover) and (pointer: fine)` rather than on touch
// capability. A laptop with a touchscreen reports touch support and still wants
// the focus; a phone reports `pointer: coarse` and `hover: none`. Touch support
// is a property of the hardware, and what matters here is how the user is
// actually driving it.
//
// Read per call, not captured once: a tablet with a keyboard attached or removed
// changes the answer, and there is no reason to make that need a reload.
const POINTER_KEYBOARD =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(hover: hover) and (pointer: fine)')
    : null;

export function prefersAutoFocus() {
  // No matchMedia means a non-browser context (a test harness stub); keep the
  // old behaviour there rather than silently changing what tests observe.
  return POINTER_KEYBOARD ? POINTER_KEYBOARD.matches : true;
}

export function parseTimestamp(iso) {
  if (!iso) return null;
  const value = /Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// Markdown image links, PDF links, and bare paths ending in a supported file
// extension. Kept deliberately narrow: anything matched here becomes a request
// for a file, so a loose pattern would turn ordinary prose into fetches.
//
// The bare-path branch lists the characters a path may contain rather than
// using \S+. \S+ also matches punctuation around a path, so `shot.png` would
// be requested with the backtick attached and could never be found.
const IMAGE_REF =
  /!\[([^\]]*)\]\(([^)\s]+)\)|([A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*\.(?:png|jpe?g|gif|webp|svg|pdf))\b/gi;

/** The chat whose workspace image paths resolve against. Set by the controller. */
let _imageChatId = null;
export function setImageContext(chatId) { _imageChatId = chatId; }

function imageChip(label, path) {
  const isPdf = path.toLowerCase().endsWith('.pdf');
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = isPdf ? 'file-chip pdf-chip' : 'image-chip';
  chip.textContent = label || path.split('/').pop();
  chip.title = isPdf ? `Open ${path}` : `Show ${path}`;
  chip.addEventListener('click', () => (
    isPdf ? openPdfViewer(path, label) : openImageViewer(path, label)
  ));
  return chip;
}

export function openPdfViewer(path, label) {
  if (!_imageChatId) return;
  const back = document.createElement('div');
  back.className = 'image-viewer pdf-viewer';
  back.setAttribute('role', 'dialog');
  back.setAttribute('aria-modal', 'true');
  back.setAttribute('aria-label', label || path);

  const frame = document.createElement('iframe');
  frame.title = label || path;
  frame.src = `/api/chats/${encodeURIComponent(_imageChatId)}/file?path=${encodeURIComponent(path)}`;

  const cap = document.createElement('div');
  cap.className = 'image-viewer-cap';
  cap.textContent = path;

  const shut = () => {
    back.remove();
    document.removeEventListener('keydown', onKey);
  };
  function onKey(event) { if (event.key === 'Escape') shut(); }
  back.addEventListener('click', event => { if (event.target === back) shut(); });
  document.addEventListener('keydown', onKey);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'image-viewer-x';
  close.textContent = '×';
  close.setAttribute('aria-label', 'Close PDF');
  close.addEventListener('click', shut);

  back.append(frame, cap, close);
  document.body.appendChild(back);
  close.focus();
}

/** Full-size viewer. A CSS tooltip cannot be dismissed, zoomed or scrolled,
 *  and a screenshot is usually taller than the message it sits in. */
export function openImageViewer(path, label) {
  if (!_imageChatId) return;
  const back = document.createElement('div');
  back.className = 'image-viewer';
  back.setAttribute('role', 'dialog');
  back.setAttribute('aria-modal', 'true');
  back.setAttribute('aria-label', label || path);

  const img = document.createElement('img');
  img.alt = label || path;
  img.src = `/api/chats/${encodeURIComponent(_imageChatId)}/file?path=${encodeURIComponent(path)}`;

  const cap = document.createElement('div');
  cap.className = 'image-viewer-cap';
  cap.textContent = path;

  img.addEventListener('error', () => { cap.textContent = `Could not load ${path}`; });

  const shut = () => {
    back.remove();
    document.removeEventListener('keydown', onKey);
  };
  function onKey(event) { if (event.key === 'Escape') shut(); }
  back.addEventListener('click', event => { if (event.target === back) shut(); });
  document.addEventListener('keydown', onKey);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'image-viewer-x';
  close.textContent = '\u00d7';
  close.setAttribute('aria-label', 'Close image');
  close.addEventListener('click', shut);

  back.append(img, cap, close);
  document.body.appendChild(back);
  close.focus();
}

function renderProse(container, text) {
  let last = 0;
  IMAGE_REF.lastIndex = 0;
  for (let m = IMAGE_REF.exec(text); m; m = IMAGE_REF.exec(text)) {
    if (m.index > last) {
      container.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    const path = m[2] || m[3];
    container.appendChild(imageChip(m[1], path));
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    container.appendChild(document.createTextNode(text.slice(last)));
  }
}

export function renderSafeText(container, text) {
  container.replaceChildren();
  String(text).split(/```/).forEach((part, index) => {
    if (index % 2) {
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      const firstNewline = part.indexOf('\n');
      code.textContent = firstNewline >= 0 ? part.slice(firstNewline + 1) : part;
      pre.appendChild(code);
      container.appendChild(pre);
    } else if (part) {
      // Only prose is scanned: a path inside a fenced block is being shown as
      // text, not offered as a thing to open.
      renderProse(container, part);
    }
  });
}

// Module scope, not controller scope, and deliberately so. The timer below
// belongs to the page rather than to one controller, so a second call to the
// factory must replace it instead of adding another -- rules.md §4 names a
// bare setInterval with no handle as the failure case, and app.js had the same
// shape at its chat poller: it only failed to accumulate because its enclosing
// block happened to run once, which is a property of where the call sat rather
// than of the code.
let _lastCommandTimer = null;

// ── Turn-duration estimate, per chat ─────────────────────────────────────
// A running turn has no signal for "N% done" from the CLI itself -- a single
// `claude -p` call reports nothing until it finishes. So this is elapsed
// time divided by how long this chat's own past turns typically took, the
// only basis available that is not invented outright. Shown as elapsed time
// alone until there is at least one finished turn in this chat to estimate
// from, and always labelled "(est.)" once it is -- never presented as a
// measurement. Same reasoning and shape as the orchestrator pane's identical
// feature (web/assets/orchestrator/tasks.js's _progressView).
//
// Kept in memory only, per chat id, for this page load -- there is no
// server-side average to fall back on without a new query, and matching the
// orchestrator version's own choice keeps the two consistent.
const _TURN_HISTORY_MAX = 20;
const _turnDurationsByChat = new Map(); // chatId -> number[] (seconds)

function _recordTurnDuration(chatId, seconds) {
  if (!chatId || !(seconds > 0)) return;
  const list = _turnDurationsByChat.get(chatId) || [];
  list.push(seconds);
  if (list.length > _TURN_HISTORY_MAX) list.shift();
  _turnDurationsByChat.set(chatId, list);
}

function _estimatedTurnPct(chatId, elapsedSeconds) {
  const list = _turnDurationsByChat.get(chatId);
  if (!list || !list.length) return null;
  const avg = list.reduce((a, b) => a + b, 0) / list.length;
  if (!(avg > 0)) return null;
  // Clamped so it never claims 100 -- that is reserved for a turn the
  // server has actually finished.
  return Math.min(99, Math.round((elapsedSeconds / avg) * 100));
}

// Ticks the composer status once a second while a turn is active, so the
// elapsed/estimate suffix counts up between renders instead of only
// updating on the next stream event. Module scope and guarded like
// `_lastCommandTimer` above, for the same reason: a bare `setInterval` here
// would double if this factory ever ran twice.
let _turnTicker = null;

// ── Queued-prompt full-text tooltip ──────────────────────────────────────
// A queue row's own text is a single ellipsised line (`.queue-text`), and
// `title` -- the only way to read the rest of it before this -- does not
// exist on a touch device at all. Same pattern as app.js's
// auto-answer-tooltip (position:fixed, appended to <body>, tap the same row
// again to close), reused here rather than invented fresh: this file cannot
// import app.js's version (app.js imports from here, not the other way
// round), and the interaction is small enough that duplicating it is
// cheaper and clearer than threading a shared module through both for one
// function.
let _queueTooltipAnchor = null;

// Set by the close (x) button, cleared by the toggle button or a chat
// switch. Module-level like the tooltip state above -- one controller
// instance per page. A signature of the current rows (count + held count)
// is kept alongside it: closing hides the panel, but new information (a
// prompt added, or one going held) still has to break through the manual
// hide rather than staying invisible until the user thinks to check again.
let _queueSignature = '';

function _queueTooltipEl() {
  let el = document.getElementById('queueTooltip');
  if (!el) {
    el = document.createElement('div');
    el.id = 'queueTooltip';
    el.className = 'queue-tooltip';
    el.setAttribute('role', 'tooltip');
    el.hidden = true;
    document.body.appendChild(el);
  }
  return el;
}

function closeQueueTooltip() {
  const el = document.getElementById('queueTooltip');
  if (!el || el.hidden) return;
  el.hidden = true;
  _queueTooltipAnchor = null;
  document.removeEventListener('click', _onDocumentClickForQueueTooltip, true);
}

function _onDocumentClickForQueueTooltip(event) {
  const el = document.getElementById('queueTooltip');
  if (el?.contains(event.target)) return;
  // Capture phase, ahead of a row's own bubble-phase click -- without this
  // exclusion a click meant to close the tooltip would close it here first,
  // and the toggle below (seeing no anchor left) would read that as "open"
  // and undo the close in the same click.
  if (event.target.closest?.('.queue-text')) return;
  closeQueueTooltip();
}

function _toggleQueueTooltip(anchor, text) {
  // A second tap on the same row closes it rather than re-showing it --
  // otherwise there is no way to dismiss it without tapping elsewhere first.
  if (_queueTooltipAnchor === anchor) {
    closeQueueTooltip();
    return;
  }
  const el = _queueTooltipEl();
  el.textContent = text;
  const width = Math.min(320, window.innerWidth - 16);
  el.style.width = `${width}px`;
  el.hidden = false;
  _queueTooltipAnchor = anchor;
  const rect = anchor.getBoundingClientRect();
  const height = el.getBoundingClientRect().height;
  el.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
  // Flips above the row when there is not enough room below, same escape
  // hatch the auto-answer tooltip and stats.js's chart tooltip both use.
  el.style.top = rect.bottom + height + 6 > window.innerHeight
    ? `${Math.max(8, rect.top - height - 6)}px`
    : `${rect.bottom + 6}px`;
  document.addEventListener('click', _onDocumentClickForQueueTooltip, true);
}

export function createConversationController(dependencies) {
  const {
    state,
    elements,
    apiFetch,
    storageGet,
    storageSet,
    storageRemove,
    showToast,
    onChatLoaded,
    refreshChats,
  } = dependencies;

  let abortController = null;
  let lastAttempt = null;
  let following = true;
  // The turn lives on the server, so leaving a conversation detaches a viewer
  // rather than stopping work. `liveSource` is the reattachment stream opened
  // when a conversation is opened while a turn is already in flight, and
  // `viewingChatId` guards every render: an event that arrives after the user
  // has moved on must not be drawn into the conversation now on screen.
  let liveSource = null;
  let viewingChatId = null;
  // True while an abort is a detach rather than a stop. Without it, switching
  // conversation reported "Response stopped" for a turn that was still running
  // -- the abort looks identical from the catch block.
  let detaching = false;
  // True from the moment send() accepts a prompt until its finally clears it.
  // See send() for why the stream state cannot serve as this guard.
  let _sending = false;

  // Only the newest MESSAGE_PAGE_SIZE messages load by default -- a chat with
  // thousands of turns used to fetch, send, and render every one of them on
  // every open and every post-turn refresh. `oldestLoadedId` is the cursor for
  // "load more" (the server's `before_id`); `hasOlderMessages` says whether
  // that control has anything left to show. Both reset on every full render,
  // since a fresh render (opening a chat, a post-turn refresh) always starts
  // back at the newest page.
  const MESSAGE_PAGE_SIZE = 50;
  let oldestLoadedId = null;
  let hasOlderMessages = false;
  let loadingOlderMessages = false;

  function draftKey(chatId) { return `wc_draft_${chatId}`; }

  function formatTime(iso) {
    const date = parseTimestamp(iso);
    if (!date) return '';
    const diff = Math.max(0, (Date.now() - date.getTime()) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return date.toLocaleDateString();
  }

  // When the turn on screen started, so a completion can say how long it ran.
  // Null while nothing is running.
  let turnStartedAt = null;

  /** "Finished · 12s", or just "Finished" if we never saw it start.
   *
   * A turn reattached from another device, or picked up by the transcript
   * sync, has no start time here -- reporting one would be inventing it.
   */
  function finishedLabel() {
    if (!turnStartedAt) return 'Finished';
    const seconds = Math.round((Date.now() - turnStartedAt) / 1000);
    if (seconds < 1) return 'Finished';
    if (seconds < 60) return `Finished · ${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return `Finished · ${minutes}m ${seconds % 60}s`;
  }

  // " · 12s" while there is no history for this chat yet, or " · 43% (est.)"
  // once there is. Empty once the turn has no start time or no chat is open.
  function _turnProgressSuffix() {
    const chatId = state.currentChat?.id;
    if (!turnStartedAt || !chatId) return '';
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - turnStartedAt) / 1000));
    const pct = _estimatedTurnPct(chatId, elapsedSeconds);
    if (pct !== null) return ` · ${pct}% (est.)`;
    if (elapsedSeconds < 60) return ` · ${elapsedSeconds}s`;
    const minutes = Math.floor(elapsedSeconds / 60);
    return ` · ${minutes}m ${elapsedSeconds % 60}s`;
  }

  // The base label (e.g. "Responding…", or a retry's custom detail) without
  // the progress suffix, so the ticker below can re-append a fresh suffix
  // every second without losing whatever the last real status event said.
  let _lastActiveLabel = '';

  function _renderComposerStatusWithProgress() {
    elements.composerStatus.textContent = _lastActiveLabel + _turnProgressSuffix();
  }

  function _manageTurnTicker(active) {
    if (active && !_turnTicker) {
      _turnTicker = setInterval(_renderComposerStatusWithProgress, 1000);
    } else if (!active && _turnTicker) {
      clearInterval(_turnTicker);
      _turnTicker = null;
    }
  }

  function setStreamState(next, detail = '') {
    const previous = state.streamState;
    state.streamState = next;
    const label = detail || STREAM_LABELS[next];
    elements.runState.textContent = label;
    elements.runState.dataset.state = next;
    // A finished turn used to announce itself by disappearing: the status went
    // straight to '' and the only evidence the work had ended was the absence
    // of "Responding…". That reads the same as a turn that never started, and
    // on a phone the reply itself may be scrolled off. Say it ended, and for
    // how long it ran -- but only on the way DOWN from an active state, or
    // merely opening a conversation would claim something had just completed.
    if (next === 'ready') {
      // Recorded here, not only where the turn's own success path already
      // knows it succeeded: `attach()`'s reattach-and-follow path also lands
      // on 'ready' from an active state, with no separate "it succeeded" hook
      // of its own -- and a genuinely finished turn is exactly what a real
      // duration sample should come from, wherever the transition is driven
      // from. `stopped`/`failed` are different next-states, so they never
      // reach here.
      if (ACTIVE_STATES.has(previous) && turnStartedAt && state.currentChat?.id) {
        _recordTurnDuration(
          state.currentChat.id,
          Math.round((Date.now() - turnStartedAt) / 1000),
        );
      }
      elements.composerStatus.textContent =
        ACTIVE_STATES.has(previous) ? finishedLabel() : '';
    } else {
      _lastActiveLabel = label;
      elements.composerStatus.textContent = label + _turnProgressSuffix();
    }
    const active = ACTIVE_STATES.has(next);
    // Stamped only on the transition INTO activity, so a turn that moves
    // connecting -> thinking -> responding is timed from when it actually
    // began rather than from its last internal step. There is deliberately no
    // reset on the way out: entering an active state always re-stamps this, so
    // a clearing line would be code no test could ever justify -- mutation
    // testing removed one and nothing failed.
    if (active && !ACTIVE_STATES.has(previous)) turnStartedAt = Date.now();
    _manageTurnTicker(active);
    elements.sendButton.classList.toggle('stop', active);
    elements.sendButton.textContent = active ? '■' : '➜';
    elements.sendButton.setAttribute('aria-label', active ? 'Stop response' : 'Send message');
    elements.composerInput.disabled = active;
    elements.retryButton.style.display = ['failed', 'stopped'].includes(next) && lastAttempt ? 'inline' : 'none';
  }

  // Focus the composer only where a physical keyboard is being used. See
  // prefersAutoFocus for why this is not simply removed.
  function focusComposer() {
    if (prefersAutoFocus()) elements.composerInput.focus();
  }

  function autoResize() {
    elements.composerInput.style.height = 'auto';
    elements.composerInput.style.height = `${Math.min(elements.composerInput.scrollHeight, 150)}px`;
  }

  function isNearBottom() {
    const area = elements.messages;
    return area.scrollHeight - area.scrollTop - area.clientHeight < 96;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      elements.messages.scrollTop = elements.messages.scrollHeight;
      following = true;
      elements.jumpButton.hidden = true;
    });
  }

  function followNewContent(shouldFollow) {
    if (shouldFollow || following) scrollToBottom();
    else elements.jumpButton.hidden = false;
  }

  // `asks` marks a message that put a question to the user. The server decides
  // it and sends the answer; this must not re-derive it, or the conversation and
  // the orchestrator panel will eventually disagree about the same message.
  //
  // Streamed rows are built empty and filled as tokens arrive, so they carry
  // false until the turn ends and refreshCurrent() reloads from the server. The
  // mark is for finding a question later, which is exactly the case where the
  // reload has already happened.

  function createMessage(role, content, time, asks = false) {
    const row = document.createElement('article');
    row.className = `message ${role}`;
    const avatar = document.createElement('div');
    avatar.className = 'msg-avatar';
    avatar.textContent = role === 'user' ? 'U' : '✶';
    avatar.setAttribute('aria-hidden', 'true');
    const body = document.createElement('div');
    body.className = 'message-body';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    renderSafeText(bubble, content || '');
    body.appendChild(bubble);
    if (asks) {
      // Built with createElement and textContent, like every other label here:
      // this sits beside agent output and must never be a markup sink.
      const marker = document.createElement('span');
      marker.className = 'msg-asks';
      marker.textContent = 'Asked you a question';
      // A badge told apart only by colour is invisible to a screen reader and
      // to about one man in twelve, so it carries its own words.
      marker.setAttribute('aria-label', 'This message asked you a question');
      body.appendChild(marker);
    }
    if (time) {
      const stamp = document.createElement('div');
      stamp.className = 'msg-time';
      stamp.textContent = formatTime(time);
      stamp.title = parseTimestamp(time)?.toLocaleString() || '';
      body.appendChild(stamp);
    }
    if (role === 'assistant' && content) {
      const tools = document.createElement('div');
      tools.className = 'message-tools';
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.textContent = 'Copy';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(content);
          copy.textContent = 'Copied';
          setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
        } catch {
          showToast('Could not copy response', 'error');
        }
      });
      tools.appendChild(copy);
      body.appendChild(tools);
    }
    row.append(avatar, body);
    return row;
  }

  function createLoadMoreButton() {
    const wrap = document.createElement('div');
    wrap.className = 'load-more-wrap';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'load-more-btn';
    btn.textContent = `Load Previous ${MESSAGE_PAGE_SIZE}`;
    btn.addEventListener('click', loadOlderMessages);
    wrap.appendChild(btn);
    return wrap;
  }

  // Prepends the previous page of history above what is currently shown,
  // without disturbing the messages already on screen or the user's place
  // among them.
  async function loadOlderMessages() {
    if (loadingOlderMessages || !hasOlderMessages) return;
    const chatId = state.currentChat?.id;
    if (!chatId || oldestLoadedId == null) return;
    loadingOlderMessages = true;
    const oldBtn = elements.messages.querySelector('.load-more-btn');
    if (oldBtn) { oldBtn.disabled = true; oldBtn.textContent = 'Loading…'; }
    try {
      const response = await apiFetch(
        `/api/chats/${encodeURIComponent(chatId)}?before_id=${oldestLoadedId}` +
        `&limit=${MESSAGE_PAGE_SIZE}`
      );
      if (!response.ok) return;
      const data = await response.json();
      // The conversation may have switched while this was in flight.
      if (viewingChatId !== chatId) return;
      const older = data.messages || [];
      const container = elements.messages;
      const oldWrap = container.querySelector('.load-more-wrap');
      const previousScrollTop = container.scrollTop;
      const previousScrollHeight = container.scrollHeight;
      if (oldWrap) oldWrap.remove();
      hasOlderMessages = Boolean(data.has_more);
      if (older.length) oldestLoadedId = older[0].id;
      const fragment = document.createDocumentFragment();
      if (hasOlderMessages) fragment.appendChild(createLoadMoreButton());
      older.forEach(message => fragment.appendChild(
        createMessage(message.role, message.content, message.created_at,
                      message.question)
      ));
      container.insertBefore(fragment, container.firstChild);
      // A prepend leaves the browser's own scroll position pointed at
      // whatever is now in the middle of the conversation; hold the same
      // content under the viewport instead of jumping the user around.
      container.scrollTop = previousScrollTop + (container.scrollHeight - previousScrollHeight);
    } finally {
      loadingOlderMessages = false;
    }
  }

  function renderMessages(messages, hasMore = false) {
    // Voice temp chats: only the tooltip, never the workspace right-panel.
    if (state.currentChat?.voice_mode) return;
    elements.messages.replaceChildren();
    oldestLoadedId = messages.length ? messages[0].id : null;
    hasOlderMessages = Boolean(hasMore);
    if (!messages.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      const strong = document.createElement('strong');
      strong.textContent = 'This workspace is ready';
      const text = document.createElement('p');
      text.textContent = 'Ask Claude to inspect, explain, or change something.';
      empty.append(strong, text);
      elements.messages.appendChild(empty);
    } else {
      if (hasOlderMessages) elements.messages.appendChild(createLoadMoreButton());
      messages.forEach(message => elements.messages.appendChild(
        createMessage(message.role, message.content, message.created_at,
                      message.question)
      ));
    }
    scrollToBottom();
  }

  // ── The last request, kept in the strip ─────────────────────────────────────
  // Answers "what did I ask here?" without scrolling, which matters most on a
  // phone where the conversation shows two or three messages at a time.

  // How many requests the picker offers. Ten is the ask; it is also about as
  // many two-line rows as fit without the popover needing its own scrollbar on
  // a phone, which is where this strip earns its space.
  const REQUEST_HISTORY_MAX = 10;

  let requestHistory = [];  // [{text, at}], newest first, capped
  // Which entry the strip shows. null means "follow the newest", which is the
  // state the bar was built for; a number pins that index.
  let pinnedRequest = null;

  function shownCommand() {
    if (pinnedRequest === null) return requestHistory[0] || null;
    return requestHistory[pinnedRequest] || requestHistory[0] || null;
  }

  function setRequestHistory(messages, {keepPin = false} = {}) {
    // What the pin currently points at, captured before the list is rebuilt.
    // A pin is an *index*, and an index into a list that has just grown at the
    // front silently addresses a different request -- so it is re-found by
    // value below rather than carried across.
    const wasPinned = keepPin && pinnedRequest !== null
      ? requestHistory[pinnedRequest] || null
      : null;

    // Walk back rather than filter-then-reverse: the newest are wanted and the
    // list can be thousands long.
    const found = [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (!message || message.role !== 'user') continue;
      const text = (message.content || '').trim();
      if (!text) continue;
      found.push({text, at: message.created_at || null});
      if (found.length >= REQUEST_HISTORY_MAX) break;
    }
    requestHistory = found;

    // refreshCurrent() runs after every completed turn, so dropping the pin
    // here would make pinning useless in the only situation it is for: keeping
    // an earlier request in view while later ones run. Re-found by text and
    // timestamp; if it has aged out of the ten, following the latest is the
    // honest fallback.
    pinnedRequest = null;
    if (wasPinned) {
      const at = requestHistory.findIndex(
        (entry) => entry.text === wasPinned.text && entry.at === wasPinned.at);
      if (at >= 0) pinnedRequest = at;
    }
    renderLastCommand();
  }

  function pushRequest(text, at) {
    const clean = (text || '').trim();
    if (!clean) return;
    requestHistory.unshift({text: clean, at: at || null});
    requestHistory = requestHistory.slice(0, REQUEST_HISTORY_MAX);
    // Sending something new returns the strip to following the newest. The bar
    // is labelled as the last request; leaving an older one pinned while the
    // user has just asked something else would make it state the opposite.
    pinnedRequest = null;
    renderLastCommand();
  }

  function renderLastCommand() {
    const bar = elements.lastCommandBar;
    if (!bar) return;
    const current = shownCommand();
    if (!current) {
      bar.hidden = true;
      closeRequestMenu();
      return;
    }
    bar.hidden = false;
    const pinned = pinnedRequest !== null && pinnedRequest > 0;
    bar.dataset.pinned = pinned ? '1' : '0';
    // textContent, never innerHTML: this is the user's own prompt coming back
    // from the database and must not be interpreted as markup.
    elements.lastCommandText.textContent = current.text;
    // The full text on hover, since the line is a single ellipsised row.
    elements.lastCommandText.title = pinned
      ? `${current.text}\n\n(pinned — click to choose another or return to the latest)`
      : `${current.text}\n\n(click to see recent requests)`;
    if (elements.lastCommandGlyph) {
      // The glyph carries the distinction as well as the colour, so it survives
      // a monochrome display and does not rely on hue alone.
      elements.lastCommandGlyph.textContent = pinned ? '\u{1F4CC}' : '➷';
    }
    elements.lastCommandWhen.textContent =
      (pinned ? 'pinned · ' : '') + (current.at ? formatTime(current.at) : '');
    if (!elements.lastCommandMenu?.hidden) renderRequestMenu();
  }

  // ── The recent-request picker ───────────────────────────────────────────────

  function closeRequestMenu() {
    const menu = elements.lastCommandMenu;
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    elements.lastCommandText?.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onDocumentClickForMenu, true);
  }

  function onDocumentClickForMenu(event) {
    // Capture phase, and only closes for a click genuinely outside the strip:
    // the row buttons live inside it, so their own clicks must reach them.
    if (elements.lastCommandBar?.contains(event.target)) return;
    closeRequestMenu();
  }

  function openRequestMenu() {
    const menu = elements.lastCommandMenu;
    if (!menu) return;
    renderRequestMenu();
    menu.hidden = false;
    elements.lastCommandText?.setAttribute('aria-expanded', 'true');
    document.addEventListener('click', onDocumentClickForMenu, true);
    (menu.querySelector('[aria-selected="true"]') || menu.querySelector('button'))
      ?.focus();
  }

  function toggleRequestMenu() {
    if (elements.lastCommandMenu?.hidden) openRequestMenu();
    else closeRequestMenu();
  }

  function renderRequestMenu() {
    const menu = elements.lastCommandMenu;
    if (!menu) return;
    // replaceChildren + createElement throughout: every row holds a prompt the
    // user typed, and this file's rule is that such text never reaches markup.
    menu.replaceChildren();

    if (!requestHistory.length) {
      const empty = document.createElement('p');
      empty.className = 'lastcmd-empty';
      empty.textContent = 'No requests in this conversation yet.';
      menu.appendChild(empty);
      return;
    }

    if (pinnedRequest !== null && pinnedRequest > 0) {
      menu.appendChild(buildRequestRow({
        text: 'Follow the latest request',
        at: null,
        index: null,
        latest: true,
      }));
    }

    requestHistory.forEach((entry, index) => {
      menu.appendChild(buildRequestRow({
        text: entry.text,
        at: entry.at,
        index,
        selected: index === (pinnedRequest === null ? 0 : pinnedRequest),
      }));
    });
  }

  function buildRequestRow({text, at, index, selected, latest}) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'lastcmd-item' + (latest ? ' lastcmd-item-latest' : '');
    row.setAttribute('role', 'option');
    row.setAttribute('aria-selected', selected ? 'true' : 'false');
    if (index !== null && index !== undefined) row.dataset.index = String(index);

    const label = document.createElement('span');
    label.className = 'lastcmd-item-text';
    label.textContent = text;
    row.appendChild(label);

    const when = document.createElement('span');
    when.className = 'lastcmd-item-when';
    when.textContent = at ? formatTime(at) : '';
    row.appendChild(when);

    // The full prompt on hover, since a row clamps to two lines.
    if (!latest) row.title = text;

    row.addEventListener('click', () => {
      pinnedRequest = latest ? null : Number(row.dataset.index);
      renderLastCommand();
      closeRequestMenu();
      elements.lastCommandText?.focus();
    });
    return row;
  }

  function onRequestMenuKeydown(event) {
    const menu = elements.lastCommandMenu;
    if (!menu || menu.hidden) return;
    const rows = [...menu.querySelectorAll('button')];
    if (!rows.length) return;
    const at = rows.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      closeRequestMenu();
      elements.lastCommandText?.focus();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      rows[at < 0 ? 0 : (at + 1) % rows.length].focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      rows[at <= 0 ? rows.length - 1 : at - 1].focus();
    } else if (event.key === 'Home') {
      event.preventDefault();
      rows[0].focus();
    } else if (event.key === 'End') {
      event.preventDefault();
      rows[rows.length - 1].focus();
    }
  }

  elements.lastCommandText?.addEventListener('click', toggleRequestMenu);
  elements.lastCommandBar?.addEventListener('keydown', onRequestMenuKeydown);


  // "2m ago" would otherwise sit there saying 2m for an hour. Only rewrites the
  // timestamp, and only while something is shown.
  //
  // Cleared before being replaced: the closure below reads *this* controller's
  // `lastCommand`, so leaving a previous one running would keep repainting from
  // state nobody reads any more, on top of the accumulation §4 forbids.
  if (_lastCommandTimer) clearInterval(_lastCommandTimer);
  _lastCommandTimer = setInterval(
    () => { if (shownCommand()) renderLastCommand(); }, 30000,
  );

  function persistDraft() {
    if (state.currentChat?.id) storageSet(draftKey(state.currentChat.id), elements.composerInput.value);
  }

  function restoreDraft(chatId) {
    elements.composerInput.value = storageGet(draftKey(chatId)) || '';
    autoResize();
  }

  async function selectChat(chat) {
    // No guard: switching away used to be refused because it destroyed the
    // turn -- the reader was the turn's owner. The server owns it now, so
    // leaving is just detaching, and the turn keeps running either way.
    detach();
    persistDraft();
    // Image paths in a message resolve against the chat's own workspace, so
    // the renderer needs to know which chat it is drawing before it draws.
    setImageContext(chat.id);
    const response = await apiFetch(`/api/chats/${encodeURIComponent(chat.id)}`);
    if (!response.ok) throw new Error('Could not open conversation');
    const data = await response.json();
    state.currentChat = data.chat;
    viewingChatId = data.chat.id;
    setRequestHistory(data.messages || []);
    renderMessages(data.messages || [], data.has_more);
    restoreDraft(chat.id);
    setStreamState('ready');
    onChatLoaded(data.chat);
    if (data.chat.running) {
      // A turn is in flight here. Attach from 0 so the answer written while we
      // were elsewhere is replayed before the live tail -- the stored messages
      // do not include it yet, because nothing is persisted until the turn ends.
      attach(data.chat.id, 0);
    }
    // A dismissal belongs to the conversation it was made in -- carrying it
    // over would leave a *different* chat's queue silently hidden the first
    // time you happened to open it after closing another one's.
    _queueManuallyHidden = false;
    _queueSignature = '';
    // Always, not only when the count is non-zero: opening a conversation has
    // to clear a panel left over from the previous one.
    refreshQueue(data.chat.id);
    // Opening a conversation is navigation, not an intent to type.
    focusComposer();
    return true;
  }

  // ── Queued prompts ──────────────────────────────────────────────────────────
  // A prompt sent while a turn was running waits on the server. Held prompts are
  // the ones whose predecessor failed: they are deliberately not sent on, so
  // they need somewhere to be seen and acted on rather than only counted.

  /** Show the queue for the open conversation, and say so when there is none.
   *
   * The click used to be `if (state.currentChat?.id) refreshQueue(...)`, so
   * pressing it with no conversation open did nothing at all and pressing it
   * with an empty queue also did nothing -- indistinguishable from a broken
   * button. Every press now ends in either an open panel or a sentence.
   */
  async function openQueuePanel() {
    const chatId = state.currentChat?.id;
    if (!chatId) {
      showToast('Open a conversation to see its queued prompts');
      return;
    }
    _queueManuallyHidden = false;
    let rows = null;
    try {
      const response = await apiFetch(
        `/api/chats/${encodeURIComponent(chatId)}/queue`);
      if (!response.ok) throw new Error('Could not load the queue');
      const payload = await response.json();
      if (state.currentChat?.id !== chatId) return;
      rows = payload.queue || [];
      renderQueue(chatId, payload);
    } catch (error) {
      showToast(error.message, 'error');
      return;
    }
    if (!rows.length) {
      showToast('Nothing is queued in this conversation');
      return;
    }
    // Open the overlay panel.
    const overlay = elements.queueBar;
    const backdrop = elements.queueBackdrop;
    if (overlay) overlay.hidden = false;
    if (backdrop) backdrop.setAttribute('aria-hidden', 'false');
  }

  async function refreshQueue(chatId) {
    if (!elements.queueBar) return;
    if (!chatId) return hideQueue(true);
    let payload;
    try {
      const response = await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/queue`);
      if (!response.ok) return hideQueue(true);
      payload = await response.json();
    } catch { return hideQueue(true); }
    if (state.currentChat?.id !== chatId) return;
    renderQueue(chatId, payload);
  }

  function hideQueue(resetToggle = false) {
    if (!elements.queueBar) return;
    elements.queueBar.hidden = true;
    if (elements.queueBackdrop) elements.queueBackdrop.setAttribute('aria-hidden', 'true');
    if (elements.queueList) elements.queueList.textContent = '';
    // A row's tooltip has no meaning once the row it points at is gone.
    closeQueueTooltip();
    // Only on "nothing queued at all" / can't reach the server, not on a
    // manual close -- the toggle button's whole job is staying visible
    // after a close so there is still a way back in.
    if (resetToggle) updateQueueToggle([]);
  }

  // Kept visible whenever there is something to check, independent of
  // whether the panel itself is currently shown -- "add a button to check
  // the queue" was the ask this exists for: before this, the only way to
  // see it again after closing was to wait for the next state change.
  function updateQueueToggle(rows) {
    const held = rows.filter(row => row.state === 'held').length;
    const label = held
      ? `${held} of ${rows.length} queued prompts held — show queue`
      : `${rows.length} queued prompt${rows.length === 1 ? '' : 's'} — show queue`;

    // Composer-level toggle (text badge)
    if (elements.queueToggle) {
      if (rows.length) elements.queueToggle.removeAttribute('hidden');
      else elements.queueToggle.setAttribute('hidden', '');
      elements.queueToggle.textContent = rows.length
        ? `Queue (${rows.length})`
        : 'Queue';
      elements.queueToggle.dataset.held = held ? 'yes' : 'no';
      elements.queueToggle.title = label;
      elements.queueToggle.setAttribute('aria-label', label);
    }
    // Topbar button stays present so the queue entry point is always available.
    // Its badge is empty when there are no queued prompts.
    if (elements.queueToggleTop) {
      elements.queueToggleTop.textContent = rows.length ? `${rows.length}` : '📋';
      elements.queueToggleTop.dataset.held = held ? 'yes' : 'no';
      elements.queueToggleTop.title = rows.length ? label : 'Queued prompts';
      elements.queueToggleTop.setAttribute(
        'aria-label', rows.length ? label : 'Show queued prompts',
      );
    }
  }

  function renderQueue(chatId, payload) {
    const rows = payload.queue || [];
    updateQueueToggle(rows);
    if (!rows.length) {
      _queueManuallyHidden = false;
      _queueSignature = '';
      return hideQueue();
    }
    const held = rows.filter(row => row.state === 'held').length;
    _queueSignature = `${rows.length}:${held}`;
    // The 5-6s poll calls this again while a tooltip may still be open --
    // replacing the list below would leave it pointing at a detached node,
    // the same hazard renderAutoAnswerMenu already guards against.
    closeQueueTooltip();
    // No auto-open — the overlay is only shown when the user clicks the
    // queue toggle button.  renderQueue here only updates badge and list
    // content so it stays fresh when the panel opens next.
    elements.queueBar.dataset.held = held ? 'yes' : 'no';
    elements.queueTag.textContent = held
      ? `${held} held`
      : `${rows.length} of ${payload.max} queued`;
    elements.queueNote.textContent = held
      ? 'The turn in front of these failed, so they were not sent. Send or discard each one.'
      : 'These send automatically, one at a time, as the current response finishes.';

    elements.queueList.textContent = '';
    rows.forEach((row, index) => {
      const item = document.createElement('li');
      item.className = 'queue-item';
      if (row.state === 'held') item.classList.add('queue-item-held');
      item.dataset.queueId = String(row.id);

      const text = document.createElement('span');
      text.className = 'queue-text';
      // textContent, never innerHTML: this is the user's own prompt coming back
      // from the database and must not be interpreted as markup.
      text.textContent = row.prompt;
      // The full prompt on hover, since the line is a single ellipsised row --
      // kept for desktop, but hover does not exist on touch, so the row is
      // also tappable: same tooltip pattern as the auto-answer log's rows.
      text.title = row.prompt;
      text.tabIndex = 0;
      text.setAttribute('role', 'button');
      text.setAttribute('aria-label', 'Show the full queued prompt');
      text.addEventListener('click', event => {
        event.stopPropagation();
        _toggleQueueTooltip(text, row.prompt);
      });
      text.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        _toggleQueueTooltip(text, row.prompt);
      });

      const position = document.createElement('span');
      position.className = 'queue-pos';
      position.textContent = row.state === 'held' ? 'held' : `#${index + 1}`;

      const actions = document.createElement('span');
      actions.className = 'queue-actions';
      if (row.state === 'held') {
        const send = document.createElement('button');
        send.type = 'button';
        send.className = 'queue-btn';
        send.textContent = 'Send';
        send.addEventListener('click', () => releaseQueued(chatId, row.id));
        actions.appendChild(send);
      }
      const drop = document.createElement('button');
      drop.type = 'button';
      drop.className = 'queue-btn queue-btn-drop';
      drop.textContent = 'Discard';
      drop.setAttribute('aria-label', `Discard queued prompt ${index + 1}`);
      drop.addEventListener('click', () => discardQueued(chatId, row.id));
      actions.appendChild(drop);

      item.append(position, text, actions);
      elements.queueList.appendChild(item);
    });
  }

  async function discardQueued(chatId, queueId) {
    try {
      const response = await apiFetch(
        `/api/chats/${encodeURIComponent(chatId)}/queue/${queueId}`,
        {method: 'DELETE'},
      );
      if (!response.ok) throw new Error('Could not discard it');
      showToast('Discarded');
    } catch (error) {
      showToast(error.message, 'error');
    }
    await refreshQueue(chatId);
    await refreshChats();
  }

  async function releaseQueued(chatId, queueId) {
    try {
      const response = await apiFetch(
        `/api/chats/${encodeURIComponent(chatId)}/queue/${queueId}/release`,
        {method: 'POST'},
      );
      if (!response.ok) throw new Error('Could not send it');
      const data = await response.json();
      if (data.started) {
        showToast('Sending now');
        attach(chatId, 0);
      } else {
        showToast('Queued — it will send when the current response finishes');
      }
    } catch (error) {
      showToast(error.message, 'error');
    }
    await refreshQueue(chatId);
    await refreshChats();
  }

  function detach() {
    // Closes the viewer, never the turn. The abort is why this is safe: the
    // request it cancels is a follower, and the server does not care.
    detaching = true;
    if (liveSource) { liveSource.close(); liveSource = null; }
    if (abortController) { abortController.abort(); abortController = null; }
  }

  function attach(chatId, since) {
    // Voice temp chats render only in the tooltip overlay — skip the
    // live SSE workspace feed so voice conversation never bleeds into
    // the right panel.
    if (state.currentChat?.voice_mode) return;
    detach();
    let bubble = null;
    let text = '';
    let seq = since || 0;
    setStreamState('thinking');
    const url = `/api/chats/${encodeURIComponent(chatId)}/live?since=${seq}`;
    const source = new EventSource(url, {withCredentials: true});
    liveSource = source;
    source.onmessage = event => {
      // Guard every render: this stream can outlive the user's attention.
      if (viewingChatId !== chatId) return;
      let payload;
      try { payload = JSON.parse(event.data); } catch { return; }
      if (payload.seq) seq = payload.seq;
      if (payload.type === 'text') {
        const shouldFollow = isNearBottom();
        if (!bubble) {
          const row = createMessage('assistant', '', '');
          bubble = row.querySelector('.msg-bubble');
          elements.messages.appendChild(row);
        }
        text += payload.content || '';
        renderSafeText(bubble, text);
        setStreamState('responding');
        followNewContent(shouldFollow);
      } else if (payload.type === 'status' && payload.status === 'api_retry') {
        setStreamState('retrying');
      } else if (payload.type === 'gone') {
        // The buffer was reaped: the answer is on disk, not on screen.
        source.close(); liveSource = null;
        refreshCurrent();
        setStreamState('ready');
      } else if (payload.type === 'status' && payload.status === 'waiting_for_slot') {
        setStreamState('thinking', payload.error || 'Waiting for a free slot…');
      } else if (payload.type === 'error') {
        source.close(); liveSource = null;
        setStreamState('failed');
        showToast(payload.error || 'The turn failed', 'error');
        refreshQueue(chatId);
        refreshChats();
      } else if (payload.type === 'done') {
        source.close(); liveSource = null;
        setStreamState('ready');
        // Reload from the server rather than keeping what was streamed: the
        // turn has persisted the canonical text by the time `done` arrives.
        refreshCurrent();
        refreshQueue(chatId);
        refreshChats();
      }
    };
    source.onerror = () => {
      source.close();
      if (liveSource === source) liveSource = null;
      if (viewingChatId === chatId) setStreamState('ready');
    };
  }

  async function refreshCurrent() {
    if (!state.currentChat) return;
    // Voice temp chats live only in the tooltip overlay — never render
    // their messages in the right-panel workspace.
    if (state.currentChat?.voice_mode) return;
    const response = await apiFetch(`/api/chats/${encodeURIComponent(state.currentChat.id)}`);
    if (!response.ok) return;
    const data = await response.json();
    state.currentChat = data.chat;
    // keepPin: this runs after every completed turn, and a pin the user set is
    // theirs to clear.
    setRequestHistory(data.messages || [], {keepPin: true});
    renderMessages(data.messages || [], data.has_more);
    onChatLoaded(data.chat);
  }

  async function send(forcedContent) {
    if (ACTIVE_STATES.has(state.streamState)) return stop();
    const chatId = state.currentChat?.id;
    const content = (forcedContent ?? elements.composerInput.value).trim();
    if (!chatId || !content) return;
    // The stream state alone is not a lock: it only becomes active after the
    // first await below, so Enter and a Send click landing in the same tick --
    // or the voice bridge calling send() while a click is already in flight --
    // both got past it and posted the same prompt twice. One submission per
    // chat is in flight at a time; the flag is cleared in the finally.
    if (_sending) return;
    _sending = true;

    const model = elements.modelPicker?.value || null;
    lastAttempt = {chatId, content, model};
    elements.retryButton.style.display = 'none';
    const empty = elements.messages.querySelector('.empty-state');
    if (empty) empty.remove();
    // Voice temp chats: never render user/assistant text in the workspace —
    // tooltip only.
    const isVoice = state.currentChat?.voice_mode;
    const sentAt = new Date().toISOString();
    if (!isVoice) {
      elements.messages.appendChild(createMessage('user', content, sentAt));
      pushRequest(content, sentAt);
      scrollToBottom();
    }
    setStreamState('connecting');
    viewingChatId = chatId;
    detaching = false;
    abortController = new AbortController();

    let assistantRow = null;
    let assistantBubble = null;
    let fullText = '';
    let accepted = false;
    let succeeded = false;

    try {
      const response = await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/stream`, {
        method: 'POST',
        signal: abortController.signal,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content, model}),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || 'Could not start response');
      }
      accepted = true;
      elements.composerInput.value = '';
      storageRemove(draftKey(chatId));
      autoResize();
      setStreamState('thinking');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let streamCompleted = false;
      streamLoop: while (true) {
        const result = await reader.read();
        if (result.done) break;
        buffer += decoder.decode(result.value, {stream: true});
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop();
        for (const block of blocks) {
          const line = block.split('\n').find(value => value.startsWith('data: '));
          if (!line) continue;
          let event;
          try { event = JSON.parse(line.slice(6)); } catch { continue; }
          if (event.type === 'queued') {
            // A turn was already running here, so the prompt is waiting its
            // turn on the server rather than being refused.
            showToast(`Queued — position ${event.position}. It will send when the current response finishes.`);
            setStreamState('ready');
            elements.composerInput.value = '';
            storageRemove(draftKey(chatId));
            autoResize();
            await refreshQueue(chatId);
            await refreshChats();
            return;
          }
          if (event.type === 'gone') {
            await refreshCurrent();
            return;
          }
          if (event.type === 'text') {
            const shouldFollow = isNearBottom();
            window.voiceConversation?.onReplyChunk(event.content || '');
            if (viewingChatId === chatId && !isVoice) {
              if (!assistantRow) {
                assistantRow = createMessage('assistant', '', '');
                assistantBubble = assistantRow.querySelector('.msg-bubble');
                elements.messages.appendChild(assistantRow);
              }
              fullText += event.content || '';
              renderSafeText(assistantBubble, fullText);
              setStreamState('responding');
              followNewContent(shouldFollow);
            }
          } else if (event.type === 'status' && event.status === 'waiting_for_slot') {
            // Otherwise this is indistinguishable from a slow model.
            setStreamState('thinking', event.error || 'Waiting for a free slot…');
          } else if (event.type === 'status' && event.status === 'api_retry') {
            const delay = event.retry_delay_ms ? Math.ceil(event.retry_delay_ms / 1000) : null;
            const detail = `Retry ${event.attempt || '?'}/${event.max_retries || '?'}${delay ? ` in ${delay}s` : ''}…`;
            setStreamState('retrying', detail);
          } else if (event.type === 'error') {
            throw new Error(event.error || 'Claude failed');
          } else if (event.type === 'done') {
            streamCompleted = true;
            window.voiceConversation?.onReplyDone();
            break streamLoop;
          }
        }
      }
      if (!streamCompleted) throw new Error('Response stream ended before completion');
      succeeded = true;
    } catch (error) {
      window.voiceConversation?.onReplyError();
      if (error.name === 'AbortError') {
        if (detaching) {
          // The user changed conversation. The turn is the server's and is
          // still running, so say nothing and claim nothing.
          return;
        }
        if (!fullText) {
          elements.composerInput.value = content;
          storageSet(draftKey(chatId), content);
          autoResize();
        }
        setStreamState('stopped');
        showToast('Response stopped');
      } else {
        if (!accepted || !fullText) {
          elements.composerInput.value = content;
          storageSet(draftKey(chatId), content);
          autoResize();
        }
        setStreamState('failed');
        showToast(error.message, 'error');
      }
    } finally {
      _sending = false;
      abortController = null;
      if (assistantRow && !fullText) assistantRow.remove();
      await refreshQueue(chatId);
      await refreshChats();
      if (succeeded) {
        lastAttempt = null;
        if (elements.modelPicker) elements.modelPicker.value = '';
        setStreamState('ready');
        await refreshCurrent();
      }
      // Same rule as opening a chat: a turn finishing is not the user asking to
      // type. On a phone this fired after every single reply.
      focusComposer();
    }
  }

  async function stop() {
    // Aborting the reader only detaches now, so a stop has to be requested.
    // Without this the button would look like it worked while the turn carried
    // on spending tokens in the background.
    const chatId = state.currentChat?.id;
    detach();
    detaching = false;   // this one really is a stop
    setStreamState('stopped');
    if (!chatId) return;
    try {
      await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/stop`, {method: 'POST'});
    } catch {
      showToast('Could not confirm the stop — the turn may still be running', 'error');
    }
    await refreshQueue(chatId);
    await refreshChats();
  }

  function retry() {
    if (lastAttempt && state.currentChat?.id === lastAttempt.chatId) {
      if (elements.modelPicker) elements.modelPicker.value = lastAttempt.model || '';
      send(lastAttempt.content);
    }
  }

  function destroy() {
    // Leaving the page detaches; the turn is the server's and continues.
    detach();
  }

  elements.composerInput.addEventListener('input', () => {
    autoResize();
    persistDraft();
  });
  elements.composerInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  elements.sendButton.addEventListener('click', () => send());
  elements.retryButton.addEventListener('click', retry);
  // Expose send() on window so the voice-* modules (voice-engine.js,
  // voice-tooltip.js) can call it.
  window.__webConsoleSend = send;

  elements.messages.addEventListener('scroll', () => {
    following = isNearBottom();
    elements.jumpButton.hidden = following;
  }, {passive: true});
  elements.jumpButton.addEventListener('click', scrollToBottom);

  elements.queueClose?.addEventListener('click', () => {
    _queueManuallyHidden = true;
    hideQueue();
  });
  elements.queueToggle?.addEventListener('click', () => openQueuePanel());
  // Topbar button: opens the same queue panel from anywhere.
  elements.queueToggleTop?.addEventListener('click', () => openQueuePanel());
  // Backdrop click closes the overlay (clicking on queue content does not).
  elements.queueBackdrop?.addEventListener('click', () => {
    if (elements.queueBar?.hidden) return;
    hideQueue();
  });

  setStreamState('ready');
  return {selectChat, send, stop, retry, restoreDraft, persistDraft, refreshCurrent, destroy, setStreamState};
}
