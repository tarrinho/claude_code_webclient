// Terminal-session transcript viewer.
//
// Reads the JSONL transcripts that the Claude Code CLI writes for each terminal
// session and shows them read-only, with an optional live tail for a session
// that is still running.
//
// Self-mounting on purpose: it injects its own styles, its own launcher button
// and its own panel, so index.html needs a single script tag and nothing else.
// Several sessions are editing app.js / index.html / styles.css concurrently and
// this keeps the feature out of their way.
//
// Everything is rendered with textContent. Transcript text is model and tool
// output, so it must never reach innerHTML.

const POLL_LABEL = 'Following';

const STYLES = `
.tx-launch { position: fixed; right: 1rem; bottom: 1rem; z-index: 40; }
.tx-panel { position: fixed; inset: 0; z-index: 50; display: none;
  background: var(--bg, #fff); color: var(--fg, #111); flex-direction: column; }
.tx-panel[data-open="true"] { display: flex; }
.tx-head { display: flex; align-items: center; gap: .75rem; padding: .75rem 1rem;
  border-bottom: 1px solid var(--border, #d7dde5); flex: 0 0 auto; }
.tx-head h2 { margin: 0; font-size: 1rem; flex: 1 1 auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tx-body { flex: 1 1 auto; overflow-y: auto; padding: 1rem; }
.tx-list { list-style: none; margin: 0; padding: 0; }
.tx-item { width: 100%; text-align: left; display: block; padding: .6rem .75rem;
  margin-bottom: .4rem; border: 1px solid var(--border, #d7dde5); border-radius: 6px;
  background: transparent; color: inherit; font: inherit; cursor: pointer; }
.tx-item:hover, .tx-item:focus-visible { border-color: var(--accent, #2b6cb0); }
.tx-item-title { font-weight: 600; display: block; }
.tx-item-meta { font-size: .8rem; opacity: .7; font-family: ui-monospace, monospace; }
.tx-turn { margin: 0 0 1rem; padding: .6rem .75rem; border-radius: 6px;
  border: 1px solid var(--border, #d7dde5); }
.tx-turn[data-role="user"] { border-left: 3px solid var(--accent, #2b6cb0); }
.tx-turn[data-role="assistant"] { border-left: 3px solid #8a8f98; }
.tx-role { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em;
  opacity: .65; margin-bottom: .35rem; font-family: ui-monospace, monospace; }
.tx-text { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 .5rem; }
.tx-text:last-child { margin-bottom: 0; }
.tx-tool { font-family: ui-monospace, monospace; font-size: .82rem; opacity: .8;
  padding: .15rem 0; overflow-wrap: anywhere; }
.tx-think { font-style: italic; opacity: .65; white-space: pre-wrap;
  overflow-wrap: anywhere; border-left: 2px dotted currentColor; padding-left: .5rem; }
.tx-note { opacity: .7; font-size: .85rem; padding: .5rem 0; }
.tx-earlier { margin-bottom: .75rem; }
.tx-earlier[hidden] { display: none; }
.tx-earlier .tx-item { text-align: center; }
.tx-follow[data-on="true"] { outline: 2px solid var(--accent, #2b6cb0); }
@media (prefers-reduced-motion: reduce) { .tx-panel { transition: none; } }
`;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatWhen(epochSeconds) {
  if (!epochSeconds) return '';
  const d = new Date(epochSeconds * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

function formatSize(bytes) {
  if (!bytes) return '';
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
}

export function mountTranscriptViewer() {
  if (document.getElementById('txPanel')) return;

  const style = el('style');
  style.textContent = STYLES;
  document.head.appendChild(style);

  const launch = el('button', 'btn-icon tx-launch', '🖵');
  launch.type = 'button';
  launch.id = 'txLaunch';
  launch.title = 'Terminal sessions';
  launch.setAttribute('aria-label', 'Terminal sessions');

  const panel = el('div', 'tx-panel');
  panel.id = 'txPanel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-label', 'Terminal sessions');

  const head = el('div', 'tx-head');
  const back = el('button', 'btn-icon', '‹');
  back.type = 'button';
  back.title = 'Back to session list';
  back.setAttribute('aria-label', 'Back to session list');
  back.hidden = true;
  const title = el('h2', null, 'Terminal sessions');
  const follow = el('button', 'btn-icon tx-follow', '⏵');
  follow.type = 'button';
  follow.title = 'Follow this session live';
  follow.setAttribute('aria-label', 'Follow this session live');
  follow.hidden = true;
  const close = el('button', 'btn-icon', '×');
  close.type = 'button';
  close.title = 'Close';
  close.setAttribute('aria-label', 'Close terminal sessions');
  head.append(back, title, follow, close);

  const body = el('div', 'tx-body');
  const live = el('div', 'tx-note');
  live.setAttribute('aria-live', 'polite');

  panel.append(head, body, live);
  document.body.append(launch, panel);

  let stream = null;
  let cursor = 0;      // forward resume point, for the live tail
  let earliest = 0;    // first byte currently loaded, for paging backwards
  let atStart = true;
  let current = null;
  let lastFocus = null;

  // "Load earlier" lives at the top of the scroll area. A long session is read
  // from its tail, so without this most of the conversation is unreachable.
  const earlierWrap = el('div', 'tx-earlier');
  const earlierBtn = el('button', 'tx-item', 'Load earlier messages');
  earlierBtn.type = 'button';
  earlierWrap.appendChild(earlierBtn);

  async function loadEarlier() {
    if (!current || atStart) return;
    earlierBtn.disabled = true;
    earlierBtn.textContent = 'Loading…';
    const anchorHeight = body.scrollHeight;
    try {
      const res = await fetch(
        `/api/transcripts/${encodeURIComponent(current.session_id)}?before=${earliest}`,
        { credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      const page = await res.json();
      earliest = page.start;
      atStart = page.at_start;
      // Prepend in order, directly after the button, then restore the scroll
      // position so the view does not jump.
      const frag = document.createDocumentFragment();
      page.turns.forEach(turn => frag.appendChild(buildTurn(turn)));
      earlierWrap.after(frag);
      body.scrollTop += body.scrollHeight - anchorHeight;
    } catch {
      live.textContent = 'Could not load earlier messages.';
    } finally {
      earlierBtn.disabled = false;
      earlierBtn.textContent = 'Load earlier messages';
      earlierWrap.hidden = atStart;
    }
  }
  earlierBtn.addEventListener('click', loadEarlier);

  function stopFollowing() {
    if (stream) {
      stream.close();
      stream = null;
    }
    follow.dataset.on = 'false';
    follow.textContent = '⏵';
    if (live.textContent === POLL_LABEL) live.textContent = '';
  }

  function buildTurn(turn) {
    const wrap = el('div', 'tx-turn');
    wrap.dataset.role = turn.role;
    const who = turn.role === 'assistant' ? 'assistant' : 'you';
    const bits = [who];
    if (turn.model) bits.push(turn.model);
    if (turn.sidechain) bits.push('subagent');
    wrap.appendChild(el('div', 'tx-role', bits.join(' · ')));

    (turn.blocks || []).forEach(block => {
      if (block.kind === 'tool') {
        wrap.appendChild(el('div', 'tx-tool', `🔧 ${block.text}`));
      } else if (block.kind === 'thinking') {
        wrap.appendChild(el('div', 'tx-think', block.text));
      } else {
        wrap.appendChild(el('p', 'tx-text', block.text));
      }
    });
    return wrap;
  }

  function renderTurn(turn) {
    body.appendChild(buildTurn(turn));
  }

  async function openSession(entry) {
    stopFollowing();
    current = entry;
    body.replaceChildren();
    live.textContent = 'Loading…';
    back.hidden = false;
    follow.hidden = false;
    title.textContent = entry.title || entry.session_id;

    let page;
    try {
      const res = await fetch(`/api/transcripts/${encodeURIComponent(entry.session_id)}`,
        { credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      page = await res.json();
    } catch {
      live.textContent = 'Could not load this conversation.';
      return;
    }

    cursor = page.offset || 0;
    earliest = page.start || 0;
    atStart = page.at_start;
    earlierWrap.hidden = atStart;
    body.appendChild(earlierWrap);
    page.turns.forEach(renderTurn);
    live.textContent = page.turns.length ? '' : 'No conversation recorded yet.';
    body.scrollTop = body.scrollHeight;
  }

  function startFollowing() {
    if (!current || stream) return;
    const url = `/api/transcripts/${encodeURIComponent(current.session_id)}/stream?offset=${cursor}`;
    stream = new EventSource(url, { withCredentials: true });
    follow.dataset.on = 'true';
    follow.textContent = '⏸';
    live.textContent = POLL_LABEL;

    stream.onmessage = event => {
      let payload;
      try { payload = JSON.parse(event.data); } catch { return; }
      if (payload.type === 'turn') {
        const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
        renderTurn(payload.turn);
        if (typeof payload.offset === 'number') cursor = payload.offset;
        if (atBottom) body.scrollTop = body.scrollHeight;
      } else if (payload.type === 'error') {
        live.textContent = payload.error || 'Stream ended.';
        stopFollowing();
      }
    };
    stream.onerror = () => {
      live.textContent = 'Live connection lost.';
      stopFollowing();
    };
  }

  async function showList() {
    stopFollowing();
    current = null;
    back.hidden = true;
    follow.hidden = true;
    title.textContent = 'Terminal sessions';
    body.replaceChildren();
    live.textContent = 'Loading…';

    let entries;
    try {
      const res = await fetch('/api/transcripts?limit=50', { credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      entries = (await res.json()).transcripts || [];
    } catch {
      live.textContent = 'Could not load terminal sessions.';
      return;
    }

    live.textContent = '';
    if (!entries.length) {
      body.appendChild(el('div', 'tx-note', 'No terminal transcripts found.'));
      return;
    }

    const list = el('ul', 'tx-list');
    entries.forEach(entry => {
      const item = el('li');
      const button = el('button', 'tx-item');
      button.type = 'button';
      button.appendChild(el('span', 'tx-item-title', entry.title || entry.session_id));
      const meta = [formatWhen(entry.updated_at), entry.session_id.slice(0, 8), formatSize(entry.size)];
      button.appendChild(el('span', 'tx-item-meta', meta.filter(Boolean).join(' · ')));
      button.addEventListener('click', () => openSession(entry));
      item.appendChild(button);
      list.appendChild(item);
    });
    body.appendChild(list);
  }

  function open() {
    lastFocus = document.activeElement;
    panel.dataset.open = 'true';
    showList();
    close.focus();
  }

  function shut() {
    stopFollowing();
    panel.dataset.open = 'false';
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // The sidebar's History rows ask for a specific conversation by event rather
  // than reaching in for a handle, so the two modules stay independent.
  document.addEventListener('wc:open-transcript', event => {
    const sessionId = event.detail && event.detail.sessionId;
    if (!sessionId) return;
    lastFocus = document.activeElement;
    panel.dataset.open = 'true';
    openSession({session_id: sessionId, title: ''});
  });

  launch.addEventListener('click', open);
  close.addEventListener('click', shut);
  back.addEventListener('click', showList);
  follow.addEventListener('click', () => {
    if (stream) stopFollowing(); else startFollowing();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && panel.dataset.open === 'true') shut();
  });
}

mountTranscriptViewer();
