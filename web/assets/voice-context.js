// Voice session context: the startup status line's data source.
//
// Split out of voice-tooltip.js rather than added to it, for the reason that
// file was split from voice-conversation.js on 2026-09-10 and is pinned under
// 300 lines by test_voice_conversation_js_files_stay_under_300_lines: a file
// that grows past its seam stops being readable in one sitting. This is its
// own seam -- reading an SSE stream and turning server states into English is
// not what the tooltip shell does.
//
// Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md

import {apiFetch} from './api.js?v=2741508';

/** Human wording for one status event from the context stream.
 *
 *  The states are the server's vocabulary; this is the only place they become
 *  English, so a state nobody wrote a phrase for still renders as something
 *  rather than as `undefined`.
 */
export function voiceContextLabel(event) {
  const model = event.model ? ` (${event.model})` : '';
  switch (event.state) {
    case 'initialising': return 'Initialising…';
    case 'summarising': return `Summarising current context${model}…`;
    case 'failed': return `Summary failed${model}`;
    case 'escalating': return `Escalating${model}…`;
    case 'ready': return 'Ready.';
    case 'degraded':
      // Says what was lost and what still works. A session that silently
      // opened without context would look identical to one that has it, and
      // the model would then appear to have forgotten the conversation.
      return `Starting without a summary${event.reason ? ` — ${event.reason}` : ''}. `
           + 'I can still look up anything from the conversation.';
    default: return String(event.state || '').trim() || 'Working…';
  }
}

/** Run the context stream for a new voice session, updating the status line.
 *
 *  Never rejects. A failure here must not stop the session opening: the whole
 *  point of the degraded path is that talking is more important than having an
 *  overview, so a dead stream lands in the same place as an exhausted ladder.
 */
export async function runVoiceContext(chatId, statusLine) {
  try {
    const response = await apiFetch(`/api/chats/${encodeURIComponent(chatId)}/voice/context`, {
      method: 'POST',
    });
    if (!response.ok || !response.body) {
      statusLine.textContent = voiceContextLabel({state: 'degraded'});
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      // SSE frames are separated by a blank line; a partial frame stays in the
      // buffer until the rest of it arrives.
      const frames = buffer.split('\n\n');
      buffer = frames.pop() || '';
      for (const frame of frames) {
        const line = frame.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let event;
        try {
          event = JSON.parse(line.slice(5).trim());
        } catch {
          continue;
        }
        if (event.type === 'status') statusLine.textContent = voiceContextLabel(event);
      }
    }
  } catch {
    statusLine.textContent = voiceContextLabel({state: 'degraded'});
  }
}
