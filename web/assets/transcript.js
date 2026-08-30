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
/* Only used when there is no toolbar to sit in -- see mountLauncher. Floating
   bottom-right put a transparent 34px button over the send button's own
   bottom-right corner, so a click that landed a few pixels low opened the
   transcript list instead of sending. Kept above the composer here so the
   fallback cannot reintroduce that. */
.tx-launch-floating { position: fixed; right: 1rem; bottom: 7.5rem; z-index: 40;
  background: var(--panel, #fff); }
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
/* A tool call with its input folded underneath. The marker is the only
   affordance saying there is more to see, so it stays visible. */
details.tx-tool { opacity: 1; }
.tx-tool-head { cursor: pointer; opacity: .8; list-style: revert; }
.tx-tool-head::marker { color: var(--accent, #2b6cb0); }
/* Smaller than the conversation on purpose: this is reference detail, and it
   must not compete with what was actually said. */
.tx-tool-detail, .tx-result-body {
  font-family: ui-monospace, monospace; font-size: .72rem; line-height: 1.45;
  white-space: pre-wrap; overflow-wrap: anywhere; margin: .3rem 0 .4rem;
  padding: .4rem .55rem; border-radius: 5px; max-height: 22rem; overflow: auto;
  background: var(--code-bg, rgba(127, 127, 127, .1));
  border-left: 2px solid var(--line, #d7dde5); }
.tx-result { margin: .1rem 0 .35rem; }
.tx-result-head { cursor: pointer; font-family: ui-monospace, monospace;
  font-size: .72rem; opacity: .6; list-style: revert; }
.tx-result-head::marker { color: var(--accent, #2b6cb0); }
.tx-result-error > .tx-result-head { color: #c0392b; opacity: .85; }
.tx-result-error .tx-result-body { border-left-color: #c0392b; }
.tx-clip { display: block; opacity: .6; font-style: italic; }
/* Replayed output, not something the operator typed. */
.tx-turn[data-tool-output="true"] { border-left-color: #b0b6bd; opacity: .9; }
.tx-think { font-style: italic; opacity: .65; white-space: pre-wrap;
  overflow-wrap: anywhere; border-left: 2px dotted currentColor; padding-left: .5rem; }
.tx-note { opacity: .7; font-size: .85rem; padding: .5rem 0; }
.tx-msg { margin: 0 0 .75rem; padding: .55rem .7rem; border-radius: 6px;
  border: 1px solid var(--line, #d7dde5); border-left: 3px solid #8a8f98; }
.tx-msg[data-mine="true"] { border-left-color: var(--accent, #2b6cb0); }
.tx-route { font-family: ui-monospace, monospace; font-size: .78rem;
  opacity: .75; margin-bottom: .3rem; }
.tx-arrow { opacity: .55; padding: 0 .3rem; }
.tx-when { float: right; opacity: .55; font-weight: 400; }
.tx-msg-body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .9rem; }
.tx-msg-body.clipped { display: -webkit-box; -webkit-line-clamp: 6;
  -webkit-box-orient: vertical; overflow: hidden; }
.tx-more { background: none; border: 0; color: var(--accent, #2b6cb0);
  cursor: pointer; font: inherit; font-size: .8rem; padding: .2rem 0; }
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

/** Put the launcher in the toolbar, or float it clear of the composer.
 *
 * It used to be `position: fixed; right: 1rem; bottom: 1rem`, which is where
 * the composer's send button already is. `.btn-icon` draws no background, so
 * the result was an invisible 34px target sitting over the send button's
 * bottom-right corner: a click a few pixels low opened the transcript list
 * instead of sending the message, with nothing on screen to explain why.
 *
 * The toolbar is the honest home for it -- it is a persistent control and it
 * belongs beside the other persistent controls, where it also reads as a
 * button rather than as a glyph floating over the conversation. Appending to
 * an existing element keeps this feature self-mounting, so index.html is still
 * untouched and the sessions editing it are unaffected.
 */
function mountLauncher(launch) {
  const settings = document.getElementById('settingsBtn');
  if (settings && settings.parentNode) {
    settings.parentNode.insertBefore(launch, settings);
    return;
  }
  // No toolbar: fall back to floating, above the composer rather than on it.
  launch.classList.add('tx-launch-floating');
  document.body.appendChild(launch);
}

export function mountTranscriptViewer() {
  if (document.getElementById('txPanel')) return;

  const style = el('style');
  style.textContent = STYLES;
  document.head.appendChild(style);

  const launch = el('button', 'btn-icon', '🖵');
  launch.type = 'button';
  launch.id = 'txLaunch';
  launch.title = 'Terminal sessions';
  launch.setAttribute('aria-label', 'Terminal sessions');
  mountLauncher(launch);

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
  const traffic = el('button', 'btn-icon', '⇄');
  traffic.type = 'button';
  traffic.title = 'Messages between sessions';
  traffic.setAttribute('aria-label', 'Messages between sessions');

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
  head.append(back, title, traffic, follow, close);

  const body = el('div', 'tx-body');
  const live = el('div', 'tx-note');
  live.setAttribute('aria-live', 'polite');

  panel.append(head, body, live);
  // The launcher was placed by mountLauncher; only the panel is body-level.
  document.body.append(panel);

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

  // A question used to render as the bare word "AskUserQuestion", which told
  // the reader nothing. Show the question and every option that was offered.
  function buildQuestion(block) {
    const card = el('div', 'tx-question');
    card.dataset.questionId = block.id || '';
    (block.questions || []).forEach(entry => {
      const head = el('div', 'tx-q-head');
      if (entry.header) head.appendChild(el('span', 'tx-q-tag', entry.header));
      head.appendChild(el('span', 'tx-q-ask', entry.question || 'Question'));
      card.appendChild(head);
      if (entry.multi_select) {
        card.appendChild(el('div', 'tx-q-note', 'Choose one or more'));
      }
      const list = el('ul', 'tx-q-options');
      (entry.options || []).forEach(option => {
        const item = el('li', 'tx-q-option');
        item.appendChild(el('span', 'tx-q-label', option.label));
        if (option.description) {
          item.appendChild(el('span', 'tx-q-desc', option.description));
        }
        list.appendChild(item);
      });
      if (list.childElementCount) card.appendChild(list);
    });
    // The pending state is filled in later, once the answer (or its absence)
    // is known: a question with no answer is still waiting on the terminal.
    card.appendChild(el('div', 'tx-q-status', 'Waiting for an answer in the terminal'));
    return card;
  }

  function buildAnswer(block) {
    const row = el('div', `tx-answer tx-answer-${block.status || 'resolved'}`);
    const label = {answered: 'Answered', declined: 'Declined'}[block.status] || 'Resolved';
    row.appendChild(el('span', 'tx-a-tag', label));
    row.appendChild(el('span', 'tx-a-text', block.text || ''));
    // Mark the question it belongs to as no longer waiting.
    const asked = block.id
      ? body.querySelector(`.tx-question[data-question-id="${block.id}"]`)
      : null;
    const status = asked?.querySelector('.tx-q-status');
    if (status) {
      status.textContent = label === 'Answered'
        ? 'Answered in the terminal'
        : `${label} in the terminal`;
      status.classList.add('tx-q-done');
    }
    return row;
  }

  // A tool call renders as its one-line headline, with the input it actually
  // ran folded underneath. The headline alone was often uninformative --
  // "Bash(Stage 15 docs + version sweep)" is a label written for a human and
  // says nothing about the command -- but putting the full input inline would
  // bury the conversation in shell scripts.
  function buildTool(block) {
    const head = `🔧 ${block.text}`;
    if (!block.detail) return el('div', 'tx-tool', head);
    const box = document.createElement('details');
    box.className = 'tx-tool tx-tool-open';
    const summary = document.createElement('summary');
    summary.className = 'tx-tool-head';
    summary.textContent = head;
    box.appendChild(summary);
    // textContent, never innerHTML: this is attacker-influenced text in the
    // sense that it is whatever was typed or generated, and it routinely
    // contains angle brackets.
    const pre = el('pre', 'tx-tool-detail', block.detail);
    if (block.detail_truncated) pre.appendChild(el('span', 'tx-clip', '\n… truncated'));
    box.appendChild(pre);
    return box;
  }

  // Tool output, folded away. Shown at all because a run of checks is mostly
  // its output -- the pipeline stages in this project produce nothing else.
  function buildResult(block) {
    const box = document.createElement('details');
    box.className = block.error ? 'tx-result tx-result-error' : 'tx-result';
    const summary = document.createElement('summary');
    summary.className = 'tx-result-head';
    const lines = block.text.split('\n').length;
    summary.textContent = block.error
      ? `⚠ output · ${lines} line${lines === 1 ? '' : 's'}`
      : `output · ${lines} line${lines === 1 ? '' : 's'}`;
    box.appendChild(summary);
    const pre = el('pre', 'tx-result-body', block.text);
    if (block.truncated) pre.appendChild(el('span', 'tx-clip', '\n… truncated'));
    box.appendChild(pre);
    return box;
  }

  function buildTurn(turn) {
    const wrap = el('div', 'tx-turn');
    wrap.dataset.role = turn.role;
    // Tool output arrives inside a user record, so labelling it by role alone
    // would credit the operator with output they never typed.
    if (turn.tool_output) wrap.dataset.toolOutput = 'true';
    const who = turn.tool_output
      ? 'tool output'
      : (turn.role === 'assistant' ? 'assistant' : 'you');
    const bits = [who];
    if (turn.model) bits.push(turn.model);
    if (turn.sidechain) bits.push('subagent');
    wrap.appendChild(el('div', 'tx-role', bits.join(' · ')));

    (turn.blocks || []).forEach(block => {
      if (block.kind === 'question') {
        wrap.appendChild(buildQuestion(block));
      } else if (block.kind === 'answer') {
        wrap.appendChild(buildAnswer(block));
      } else if (block.kind === 'tool') {
        wrap.appendChild(buildTool(block));
      } else if (block.kind === 'result') {
        wrap.appendChild(buildResult(block));
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

  // Messages the concurrent sessions sent each other, newest first. Each is
  // recorded at both ends and in several record shapes; the server collapses
  // that to one row per message, so this renders a single conversation rather
  // than one session's view of it.
  async function showTraffic() {
    stopFollowing();
    current = null;
    back.hidden = false;
    follow.hidden = true;
    title.textContent = 'Messages between sessions';
    body.replaceChildren();
    live.textContent = 'Loading…';

    let messages;
    try {
      const res = await fetch('/api/agent-traffic?limit=200', {credentials: 'same-origin'});
      if (!res.ok) throw new Error(String(res.status));
      messages = (await res.json()).messages || [];
    } catch {
      live.textContent = 'Could not load session messages.';
      return;
    }

    live.textContent = '';
    if (!messages.length) {
      body.appendChild(el('div', 'tx-note', 'No messages between sessions yet.'));
      return;
    }

    messages.forEach(msg => {
      const wrap = el('div', 'tx-msg');
      const route = el('div', 'tx-route');
      route.appendChild(el('span', null, msg.sender || '?'));
      route.appendChild(el('span', 'tx-arrow', '→'));
      route.appendChild(el('span', null, msg.recipient || '?'));
      if (msg.timestamp) {
        const when = new Date(msg.timestamp);
        route.appendChild(el('span', 'tx-when',
          Number.isNaN(when.getTime()) ? '' : when.toLocaleString()));
      }
      wrap.appendChild(route);

      // Messages run long; clip and let the reader open the ones they want.
      const text = el('div', 'tx-msg-body clipped', msg.text || '');
      wrap.appendChild(text);
      if ((msg.text || '').length > 320) {
        const more = el('button', 'tx-more', 'Show more');
        more.type = 'button';
        more.addEventListener('click', () => {
          const clipped = text.classList.toggle('clipped');
          more.textContent = clipped ? 'Show more' : 'Show less';
        });
        wrap.appendChild(more);
      }
      body.appendChild(wrap);
    });
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
  traffic.addEventListener('click', showTraffic);
  follow.addEventListener('click', () => {
    if (stream) stopFollowing(); else startFollowing();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && panel.dataset.open === 'true') shut();
  });
}

mountTranscriptViewer();
