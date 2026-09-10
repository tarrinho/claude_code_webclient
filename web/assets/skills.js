// ── Skills ────────────────────────────────────────────────────────────────────────

import {apiFetch} from './api.js?v=1';
import {state} from './app.js?v=56';

// This file is loaded as its own <script type="module"> in index.html and
// does not share app.js's own `const byId` (ES modules do not share
// top-level scope across files), so it needs its own -- same pattern as
// orchestrator.js/device-alerts.js/usage.js/server-stats.js.
const byId = id => document.getElementById(id);
// The filter text lives in the input element, not in a variable shared with
// app.js. app.js declares its own `_skillFilter` and assigns it on every
// keystroke, but ES modules do not share top-level scope -- so reading that
// name here was a ReferenceError and the whole panel died on its first render,
// with no request ever leaving the browser. Reading the DOM removes the shared
// mutable state rather than exporting it: the input is the source of truth, and
// app.js's copy was write-only in any case.
const skillFilter = () => byId('skillSearch')?.value ?? '';

// Which source groups are collapsed. Also left behind by the app.js split:
// app.js declares `const _collapsedSkillGroups` and never touches it again,
// while every read and write is here -- so `_renderSkills` threw on it too,
// one line after the filter. Two dangling references in the same render path,
// which is why fixing only the first changed nothing a user could see.
const _collapsedSkillGroups = new Set();

let _skillsData = null;          // last successful /api/skills payload
let _skillsFetchedFor = null;    // chat id the payload was fetched for

function _skillsNotice(text) {
  const notice = document.createElement('div');
  notice.className = 'skills-notice';
  notice.textContent = text;
  return notice;
}

/** Placeholder rows so the panel does not flash empty while fetching. */
export function _renderSkillSkeleton() {
  const list = byId('skillsList');
  if (!list) return;
  const rows = Array.from({length: 5}, () => {
    const row = document.createElement('div');
    row.className = 'skill-skeleton';
    return row;
  });
  list.replaceChildren(...rows);
  byId('skillsCount').textContent = 'Loading…';
}

function _skillMatches(skill, needle) {
  if (!needle) return true;
  return skill.name.toLowerCase().includes(needle)
    || (skill.description || '').toLowerCase().includes(needle);
}

/** One collapsed/expandable card. Long descriptions stay behind a disclosure. */
function _buildSkillCard(skill, isPlugin) {
  const item = document.createElement('li');
  item.className = skill.active ? 'skill-card skill-card-active' : 'skill-card';

  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.className = 'skill-summary-row';

  const heading = document.createElement('span');
  heading.className = 'skill-name';
  // The group header already names the plugin, so drop the redundant prefix.
  heading.textContent = isPlugin ? skill.name.split(':').slice(1).join(':') : skill.name;
  summary.appendChild(heading);

  if (skill.active) {
    const badge = document.createElement('span');
    badge.className = 'skill-badge skill-active';
    badge.textContent = 'Active';
    summary.appendChild(badge);
  }

  const line = document.createElement('span');
  line.className = 'skill-summary';
  line.textContent = skill.summary || 'No description provided.';
  summary.appendChild(line);

  const full = document.createElement('p');
  full.className = 'skill-description';
  full.textContent = skill.description || 'No description provided.';

  details.append(summary, full);
  item.appendChild(details);
  return item;
}

export function _renderSkills() {
  const list = byId('skillsList');
  if (!list || !_skillsData) return;
  const needle = skillFilter().trim().toLowerCase();
  const all = _skillsData.skills || [];
  const sources = _skillsData.sources || [];
  const shown = all.filter(skill => _skillMatches(skill, needle));

  // Count line: absolute totals when browsing, match count when filtering.
  const count = byId('skillsCount');
  if (needle) {
    count.textContent = `${shown.length} of ${all.length} skills`;
  } else {
    const active = _skillsData.active_count || 0;
    count.textContent = active
      ? `${all.length} skills · ${active} active`
      : `${all.length} skills`;
  }

  // Which conversation the "Active" badges refer to.
  const session = byId('skillsSession');
  if (_skillsData.session_id) {
    const title = state.currentChat?.title;
    session.textContent = title
      ? `Activity shown for "${title}".`
      : 'Activity shown for the current conversation.';
    session.hidden = false;
  } else {
    session.textContent = 'Open a conversation to see which skills it has used.';
    session.hidden = false;
  }

  if (!all.length) {
    list.replaceChildren(_skillsNotice('No skills found. Add one under ~/.claude/skills.'));
    return;
  }
  if (!shown.length) {
    list.replaceChildren(_skillsNotice(`No skills match "${skillFilter().trim()}".`));
    return;
  }

  const groups = sources.map(source => {
    const items = shown.filter(skill => skill.source === source.id);
    if (!items.length) return null;

    const section = document.createElement('section');
    section.className = 'skill-group';

    // Filtering always expands, so matches are never hidden behind a collapse.
    const collapsed = !needle && _collapsedSkillGroups.has(source.id);
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'skill-group-head';
    head.setAttribute('aria-expanded', String(!collapsed));
    head.addEventListener('click', () => {
      if (_collapsedSkillGroups.has(source.id)) _collapsedSkillGroups.delete(source.id);
      else _collapsedSkillGroups.add(source.id);
      _renderSkills();
    });

    const caret = document.createElement('span');
    caret.className = 'skill-group-caret';
    caret.setAttribute('aria-hidden', 'true');
    caret.textContent = '▸';
    const label = document.createElement('span');
    label.className = 'skill-group-label';
    label.textContent = source.label;
    const tally = document.createElement('span');
    tally.className = 'skill-group-count';
    tally.textContent = needle ? `${items.length} of ${source.count}` : String(source.count);
    head.append(caret, label, tally);
    section.appendChild(head);

    if (!collapsed) {
      const items_el = document.createElement('ul');
      items_el.className = 'skill-group-items';
      const isPlugin = source.id.startsWith('plugin:');
      // Active skills first, so session activity is visible without scrolling.
      const ordered = [...items].sort((a, b) => Number(b.active) - Number(a.active));
      ordered.forEach(skill => items_el.appendChild(_buildSkillCard(skill, isPlugin)));
      section.appendChild(items_el);
    }
    return section;
  }).filter(Boolean);

  list.replaceChildren(...groups);
}

export async function loadSkills(force = false) {
  const list = byId('skillsList');
  if (!list) return;
  const chatId = state.currentChat?.id || '';
  // Reuse the payload unless the conversation changed -- activity is per session.
  if (!force && _skillsData && _skillsFetchedFor === chatId) {
    _renderSkills();
    return;
  }
  _renderSkillSkeleton();
  try {
    const query = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : '';
    const response = await apiFetch(`/api/skills${query}`);
    if (!response.ok) throw new Error('Could not load skills');
    _skillsData = await response.json();
    _skillsFetchedFor = chatId;
    _renderSkills();
  } catch (error) {
    _skillsData = null;
    _skillsFetchedFor = null;
    byId('skillsCount').textContent = '';
    byId('skillsSession').hidden = true;
    list.replaceChildren(_skillsNotice(error.message));
  }
}