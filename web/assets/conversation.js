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

export function parseTimestamp(iso) {
  if (!iso) return null;
  const value = /Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// Markdown ![alt](path) and bare paths ending in an image extension. Kept
// deliberately narrow: anything matched here becomes a request for a file, so
// a loose pattern would turn ordinary prose into fetches.
//
// The bare-path branch lists the characters a path may contain rather than
// using \S+. \S+ also matches the punctuation around a path, so a filename
// written in prose as `shot.png` was requested with the backtick attached and
// could never be found.
const IMAGE_REF =
  /!\[([^\]]*)\]\(([^)\s]+)\)|([A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*\.(?:png|jpe?g|gif|webp|svg))\b/gi;

/** The chat whose workspace image paths resolve against. Set by the controller. */
let _imageChatId = null;
export function setImageContext(chatId) { _imageChatId = chatId; }

function imageChip(label, path) {
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'image-chip';
  chip.textContent = label || path.split('/').pop();
  chip.title = `Show ${path}`;
  chip.addEventListener('click', () => openImageViewer(path, label));
  return chip;
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
      elements.composerStatus.textContent =
        ACTIVE_STATES.has(previous) ? finishedLabel() : '';
    } else {
      elements.composerStatus.textContent = label;
    }
    const active = ACTIVE_STATES.has(next);
    // Stamped only on the transition INTO activity, so a turn that moves
    // connecting -> thinking -> responding is timed from when it actually
    // began rather than from its last internal step. There is deliberately no
    // reset on the way out: entering an active state always re-stamps this, so
    // a clearing line would be code no test could ever justify -- mutation
    // testing removed one and nothing failed.
    if (active && !ACTIVE_STATES.has(previous)) turnStartedAt = Date.now();
    elements.sendButton.classList.toggle('stop', active);
    elements.sendButton.textContent = active ? '■' : '➜';
    elements.sendButton.setAttribute('aria-label', active ? 'Stop response' : 'Send message');
    elements.composerInput.disabled = active;
    elements.retryButton.style.display = ['failed', 'stopped'].includes(next) && lastAttempt ? 'inline' : 'none';
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

  function createMessage(role, content, time) {
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

  function renderMessages(messages) {
    elements.messages.replaceChildren();
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
      messages.forEach(message => elements.messages.appendChild(
        createMessage(message.role, message.content, message.created_at)
      ));
    }
    scrollToBottom();
  }

  // ── The last request, kept in the strip ─────────────────────────────────────
  // Answers "what did I ask here?" without scrolling, which matters most on a
  // phone where the conversation shows two or three messages at a time.

  let lastCommand = null;   // {text, at} or null

  function setLastCommand(text, at) {
    const clean = (text || '').trim();
    lastCommand = clean ? {text: clean, at: at || null} : null;
    renderLastCommand();
  }

  function renderLastCommand() {
    const bar = elements.lastCommandBar;
    if (!bar) return;
    if (!lastCommand) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    // textContent, never innerHTML: this is the user's own prompt coming back
    // from the database and must not be interpreted as markup.
    elements.lastCommandText.textContent = lastCommand.text;
    // The full text on hover, since the line is a single ellipsised row.
    elements.lastCommandText.title = lastCommand.text;
    elements.lastCommandWhen.textContent =
      lastCommand.at ? formatTime(lastCommand.at) : '';
  }

  function lastCommandFrom(messages) {
    // Walk back rather than filter: the newest user message is wanted and the
    // list can be long.
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index] && messages[index].role === 'user') return messages[index];
    }
    return null;
  }

  // "2m ago" would otherwise sit there saying 2m for an hour. Only rewrites the
  // timestamp, and only while something is shown.
  //
  // Cleared before being replaced: the closure below reads *this* controller's
  // `lastCommand`, so leaving a previous one running would keep repainting from
  // state nobody reads any more, on top of the accumulation §4 forbids.
  if (_lastCommandTimer) clearInterval(_lastCommandTimer);
  _lastCommandTimer = setInterval(
    () => { if (lastCommand) renderLastCommand(); }, 30000,
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
    const opened = lastCommandFrom(data.messages || []);
    setLastCommand(opened && opened.content, opened && opened.created_at);
    renderMessages(data.messages || []);
    restoreDraft(chat.id);
    setStreamState('ready');
    onChatLoaded(data.chat);
    if (data.chat.running) {
      // A turn is in flight here. Attach from 0 so the answer written while we
      // were elsewhere is replayed before the live tail -- the stored messages
      // do not include it yet, because nothing is persisted until the turn ends.
      attach(data.chat.id, 0);
    }
    // Always, not only when the count is non-zero: opening a conversation has
    // to clear a panel left over from the previous one.
    refreshQueue(data.chat.id);
    elements.composerInput.focus();
    return true;
  }

  // ── Queued prompts ──────────────────────────────────────────────────────────
  // A prompt sent while a turn was running waits on the server. Held prompts are
  // the ones whose predecessor failed: they are deliberately not sent on, so
  // they need somewhere to be seen and acted on rather than only counted.

  async function refreshQueue(chatId) {
    if (!elements.queueBar) return;
    if (!chatId) return hideQueue();
    let payload;
    try {
      const response = await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/queue`);
      if (!response.ok) return hideQueue();
      payload = await response.json();
    } catch { return hideQueue(); }
    if (state.currentChat?.id !== chatId) return;
    renderQueue(chatId, payload);
  }

  function hideQueue() {
    if (!elements.queueBar) return;
    elements.queueBar.hidden = true;
    if (elements.queueList) elements.queueList.textContent = '';
  }

  function renderQueue(chatId, payload) {
    const rows = payload.queue || [];
    if (!rows.length) return hideQueue();
    const held = rows.filter(row => row.state === 'held').length;
    elements.queueBar.hidden = false;
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
      text.title = row.prompt;

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
    const response = await apiFetch(`/api/chats/${encodeURIComponent(state.currentChat.id)}`);
    if (!response.ok) return;
    const data = await response.json();
    state.currentChat = data.chat;
    const latest = lastCommandFrom(data.messages || []);
    setLastCommand(latest && latest.content, latest && latest.created_at);
    renderMessages(data.messages || []);
    onChatLoaded(data.chat);
  }

  async function send(forcedContent) {
    if (ACTIVE_STATES.has(state.streamState)) return stop();
    const chatId = state.currentChat?.id;
    const content = (forcedContent ?? elements.composerInput.value).trim();
    if (!chatId || !content) return;

    const model = elements.modelPicker?.value || null;
    lastAttempt = {chatId, content, model};
    elements.retryButton.style.display = 'none';
    const empty = elements.messages.querySelector('.empty-state');
    if (empty) empty.remove();
    const sentAt = new Date().toISOString();
    elements.messages.appendChild(createMessage('user', content, sentAt));
    setLastCommand(content, sentAt);
    scrollToBottom();
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
            if (!assistantRow) {
              assistantRow = createMessage('assistant', '', '');
              assistantBubble = assistantRow.querySelector('.msg-bubble');
              elements.messages.appendChild(assistantRow);
            }
            fullText += event.content || '';
            if (viewingChatId === chatId) {
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
            break streamLoop;
          }
        }
      }
      if (!streamCompleted) throw new Error('Response stream ended before completion');
      succeeded = true;
    } catch (error) {
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
      elements.composerInput.focus();
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
  elements.messages.addEventListener('scroll', () => {
    following = isNearBottom();
    elements.jumpButton.hidden = following;
  }, {passive: true});
  elements.jumpButton.addEventListener('click', scrollToBottom);

  setStreamState('ready');
  return {selectChat, send, stop, retry, restoreDraft, persistDraft, refreshCurrent, destroy, setStreamState};
}
