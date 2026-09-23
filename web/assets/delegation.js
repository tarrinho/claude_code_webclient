// delegation.js — Settings > Delegation: the spec 2.6 benchmark matrix.
//
// Each row is one (model, task_type) pair with its five measured columns.
// The ladder shown per task type is DERIVED server-side by walking the table;
// it is never stored, so the page cannot show a ladder the generator would
// not produce.
//
// Every value below that comes from the database (model id, task_type,
// column values) is written with .textContent / .value, never innerHTML --
// same rule specs.js follows for spec titles and paths, because a model id
// or task type rendered straight into HTML is a stored-XSS hole.
import {apiFetch} from './api.js?v=2741508';
import {notifyResult} from './server-stats.js?v=1683277';
// This module calls showToast in 13 places and never imported it, so every one
// of them raised `ReferenceError: showToast is not defined` -- including the
// handlers that report why a write was refused. A rejected PUT therefore threw
// inside its own error path and the server's reason never reached the screen:
// turning delegation on answered 400 with a full explanation of which rows
// break spec 1.1, and the page showed nothing at all.
//
// Static import of app.js is safe here and is what server-stats.js already
// does: app.js reaches this module through a dynamic `await import()`, so
// there is no static cycle between the two.
import {showToast} from './app.js?v=5926217';

const byId = id => document.getElementById(id);

// Fallback only. `GET /api/delegation` names the true list in
// `editable_columns` (routes/delegation.py's `_EDITABLE`, which is one of
// three coordinated Python copies -- see that module's docstring); this
// array exists solely for an older cached response that predates that field,
// so the table still renders instead of coming up broken or empty. Every
// live render derives its columns from the response via `_resolveColumns`.
const _DEFAULT_COLUMNS = ['accuracy', 'n', 'cost_per_1m_tokens', 'median_latency_s', 'max_context'];

/** The columns to render for one `GET /api/delegation` response.
 *
 *  Reads `payload.editable_columns` rather than hardcoding a fourth copy of
 *  the five measured column names (spec 11 already flags three Python
 *  copies of this list, kept in sync by a test; this used to be an
 *  uncovered fourth). Falls back to `_DEFAULT_COLUMNS` when the field is
 *  missing, not an array, or empty -- an older cached response, from before
 *  this field existed, must still render the ordinary table rather than a
 *  broken or empty one. */
function _resolveColumns(payload) {
  const cols = payload && Array.isArray(payload.editable_columns)
    ? payload.editable_columns
    : null;
  return cols && cols.length ? cols : _DEFAULT_COLUMNS;
}

/** The server's reason for refusing a write, or `fallback` if it gave none.
 *
 *  This app's error contract is `{"error": "..."}` -- `app.py`'s
 *  `handle_http_exception` serialises every `HTTPException` that way, so
 *  `detail` is the FastAPI-side name and never reaches the browser. This
 *  file read `data.detail` in both of its write paths, so every refusal the
 *  server took care to explain arrived as `undefined` and was replaced by a
 *  generic fallback: a rejected cell edit said "Could not save" instead of
 *  naming the broken invariant and the column, which spec 1.1 requires it to
 *  name. `app.js` already reads `data.error`; this matches it.
 *
 *  `detail` is still read as a second choice rather than dropped: a response
 *  from a plain FastAPI error path that never reached the custom handler
 *  carries that shape, and a real reason under either key beats a fallback. */
function _errorMessage(data, fallback) {
  const body = data && typeof data === 'object' ? data : {};
  const reason = body.error || body.detail;
  return typeof reason === 'string' && reason.trim() ? reason : fallback;
}

/** Pure: the status band's plain-English first line. No DOM, so it can be
 *  read straight out of a `GET /api/delegation` response's `operational`
 *  and `ladders` fields (the latter's key count is the task-type total --
 *  same scoping the ladders/blockers fields already use, so a type with no
 *  rows yet does not inflate the count). */
function _statusHeadline(operationalCount, totalCount) {
  if (totalCount <= 0) return 'No task types are in the table yet.';
  const noun = `task type${totalCount === 1 ? '' : 's'}`;
  if (operationalCount <= 0) {
    return `Nothing is routing. 0 of ${totalCount} ${noun} operational.`;
  }
  if (operationalCount >= totalCount) {
    return `Everything is routing. ${operationalCount} of ${totalCount} ${noun} operational.`;
  }
  return `${operationalCount} of ${totalCount} ${noun} operational.`;
}

/** Pure: how many of a set of rows' editable cells actually carry a
 *  measurement, out of how many exist -- the status band's "measured cells
 *  out of total" fact. `null`/`undefined` both count as TBD, matching the
 *  rest of this file's "empty cell means unmeasured" convention. */
function _measuredCellsSummary(rows, columns) {
  const list = Array.isArray(rows) ? rows : [];
  const cols = Array.isArray(columns) ? columns : [];
  let measured = 0;
  list.forEach(row => {
    cols.forEach(column => {
      if (row[column] !== null && row[column] !== undefined) measured += 1;
    });
  });
  return {measured, total: list.length * cols.length};
}

/** Pure: the display lines for one task type's blocker entry (the
 *  `payload.blockers[taskType]` shape routes/delegation.py's
 *  `_blockers_by_task_type` returns: `{policy, data}`). The policy reason
 *  (if any) comes first, then every data-invariant problem -- an operator
 *  fixing data still needs to see a live policy hold, and vice versa.
 *  Returns `[]` for a clean or already-operational type, never a placeholder
 *  string, so the caller can render nothing instead of an empty warning. */
function _blockerLines(entry) {
  if (!entry) return [];
  const lines = [];
  if (entry.policy) lines.push(entry.policy);
  (Array.isArray(entry.data) ? entry.data : []).forEach(line => {
    if (line) lines.push(line);
  });
  return lines;
}

/** The `warnings` channel: wrong, but not blocking. Today this is a
 *  worst-case path over spec 5.1's ceiling while the enforcement knob is off.
 *  Kept separate from `_blockerLines` on purpose -- "this is why you cannot
 *  flip" and "this is over the ceiling and we are not stopping you" are
 *  different sentences, and merging them would make turning the knob off read
 *  as the breach having gone away. */
function _warningLines(entry) {
  if (!entry) return [];
  return (Array.isArray(entry.warnings) ? entry.warnings : []).filter(Boolean);
}

/** All five measured fields for one row, formatted for a hover tooltip.
 *  Spec 9.2: "Hover tooltips on rung values in the settings page show all
 *  five measured fields for the current task type ... the same five the
 *  section 3 tooltip shows, from the same row." Assigned to `.title`, a DOM
 *  property, not parsed as HTML -- the same escaping guarantee `.textContent`
 *  has. */
function _rungTooltipText(row) {
  const shown = value => (value === null || value === undefined ? 'TBD' : value);
  return [
    `accuracy: ${shown(row.accuracy)}`,
    `n: ${shown(row.n)}`,
    `cost_per_1m_tokens: ${shown(row.cost_per_1m_tokens)}`,
    `median_latency_s: ${shown(row.median_latency_s)}`,
    `max_context: ${shown(row.max_context)}`,
  ].join(' · ');
}

/** The ladder for one task type, each rung its own element carrying the
 *  five-field tooltip from the row it came from -- not a plain text run, so
 *  each rung can hold its own `title`. */
function _ladderElement(ladder, rowsForType) {
  const wrap = document.createElement('span');
  wrap.className = 'delegation-ladder';
  if (!ladder.length) {
    wrap.appendChild(document.createTextNode('no ladder yet'));
    return wrap;
  }
  ladder.forEach((model, i) => {
    // Numbered to match the pinned editor's rungs, so the same ladder reads
    // the same way whether or not it is pinned.
    const num = document.createElement('span');
    num.className = 'delegation-rung-num';
    num.textContent = String(i + 1);
    num.setAttribute('aria-hidden', 'true');
    wrap.appendChild(num);
    const rung = document.createElement('span');
    rung.className = 'delegation-rung';
    rung.textContent = model;
    const row = rowsForType.find(r => r.model === model);
    rung.title = row ? _rungTooltipText(row)
      : 'no row in the table for this rung';
    wrap.appendChild(rung);
  });
  return wrap;
}

/** Inline ladder editor for one task type.
 *
 *  Always shows a pin action so the operator can pin the ladder:
 *  when not pinned: a "Pin" button that copies the generated ladder;
 *  when pinned: the ladder as <select> rungs, Sync/Revert/Save controls.
 *
 *  The model list comes from rowsForType so every selectable model is one
 *  that actually exists in the matrix — no free-text model ids. */
function _ladderEditor(taskType, pinned, generated, rowsForType, isPinned, payload) {
  const row = document.createElement('p');
  row.className = 'delegation-ladder-editor';
  row.dataset.taskType = taskType;

  if (!isPinned) {
    // The static ladder now lives on this row rather than on a line of its
    // own above it, so an unpinned type reads "Ladder: a > b > c   [Pin]".
    const label = document.createElement('span');
    label.className = 'delegation-ladder-label';
    label.textContent = 'Ladder:';
    row.append(label, _ladderElement(pinned, rowsForType));
    // "Pin" button: set the pin to the generated ladder.
    const pinBtn = document.createElement('button');
    pinBtn.type = 'button';
    pinBtn.className = 'delegation-ladder-pin';
    pinBtn.textContent = 'Pin';
    pinBtn.title = 'Pin the current (generated) ladder so it survives re-benchmarking';
    pinBtn.addEventListener('click', async () => {
      if (generated.length === 0) {
        showToast('No model data yet — cannot pin an empty ladder', 'error');
        return;
      }
      try {
        const response = await apiFetch('/api/delegation/ladder', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({task_type: taskType, rungs: generated}),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(_errorMessage(data, 'Could not pin'));
        }
        const data = await response.json().catch(() => ({}));
        if (data.problems && data.problems.length) {
          notifyResult('Pin set with warnings: ' + data.problems.join('; '), 'warning');
        } else {
          showToast('Ladder pinned');
        }
        await _refreshDelegation();
      } catch (error) {
        showToast(error.message, 'error');
      }
    });
    pinBtn.classList.add('delegation-ladder-push');
    row.appendChild(pinBtn);
    return row;
  }

  // ── Pinned ladder editor ──────────────────────────────────────────
  const badge = document.createElement('span');
  badge.className = 'delegation-pin-badge';
  badge.textContent = 'PINNED';
  badge.title = 'This ladder is pinned (operator override). '
              + 'It survives re-benchmarking.';
  row.appendChild(badge);

  // Sync, Revert and Save are grouped and pushed to the end of the row, so the
  // merged line reads left to right as "this is the ladder" then "these act on
  // it". Sync in particular used to be appended before the rungs, which put a
  // control that rewrites every rung to the left of the rungs it rewrites.
  const actions = document.createElement('span');
  actions.className = 'delegation-ladder-actions delegation-ladder-push';

  // Sync from generated button.
  if (generated.length > 0) {
    const syncBtn = document.createElement('button');
    syncBtn.type = 'button';
    syncBtn.className = 'delegation-ladder-sync';
    syncBtn.textContent = 'Sync';
    syncBtn.title = 'Copy the generated ladder into the editor';
    syncBtn.addEventListener('click', () => {
      // Replace all rung selects with the generated ladder.
      const selects = row.querySelectorAll('.delegation-rung-select');
      selects.forEach((sel, i) => {
        if (i < generated.length) {
          sel.value = generated[i];
          // Show/hide remove button.
          const remove = sel.parentElement.querySelector('.delegation-rung-remove');
          if (remove) remove.style.display = '';
        } else {
          sel.value = '';
        }
      });
    });
    actions.appendChild(syncBtn);
  }

  // Rung selectors.
  const rungs = isPinned ? pinned : generated;
  // Collect available models from the matrix rows.
  const availableModels = rowsForType.map(r => r.model);

  rungs.forEach((model, i) => {
    const wrapper = document.createElement('span');
    wrapper.className = 'delegation-rung-wrap';

    // The rung's position, stated rather than implied. A ladder is an order --
    // cheapest first, each rung tried before the next - and on one merged line
    // of similar-looking selects that order was carried only by left-to-right
    // position, which is not something a reader should have to infer.
    const num = document.createElement('span');
    num.className = 'delegation-rung-num';
    num.textContent = String(i + 1);
    num.setAttribute('aria-hidden', 'true');
    wrapper.appendChild(num);

    const select = document.createElement('select');
    select.className = 'delegation-rung-select';
    select.setAttribute('aria-label', `Rung ${i + 1} model`);

    // Option: "(empty)" as default.
    const emptyOpt = document.createElement('option');
    emptyOpt.value = '';
    emptyOpt.textContent = '(empty)';
    select.appendChild(emptyOpt);

    // Option: the current rung value if it's not in the matrix (warn).
    if (model && !availableModels.includes(model)) {
      const warnOpt = document.createElement('option');
      warnOpt.value = model;
      warnOpt.textContent = model + ' ⚠';
      warnOpt.disabled = true;
      warnOpt.selected = true;
      select.appendChild(warnOpt);
      wrapper.classList.add('delegation-rung-warn');
    }

    // Populate all available models.
    availableModels.sort().forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === model) opt.selected = true;
      select.appendChild(opt);
    });

    wrapper.appendChild(select);

    // Remove button (only when pinned).
    if (isPinned) {
      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'delegation-rung-remove';
      removeBtn.textContent = '×';
      removeBtn.title = 'Remove this rung';
      removeBtn.addEventListener('click', () => {
        select.value = '';
        removeBtn.style.display = 'none';
      });
      wrapper.appendChild(removeBtn);
    }

    row.appendChild(wrapper);

    if (i < rungs.length - 1) {
      row.appendChild(document.createTextNode(' → '));
    }
  });

  // Add rung button (only when pinned).
  if (isPinned) {
    const maxAttempts = (payload.config && payload.config.attempts_and_caps
      && payload.config.attempts_and_caps.max_attempts_generation
      && payload.config.attempts_and_caps.max_attempts_generation.value)
      || 3;

    // Count how many non-empty selects exist.
    const allSelects = row.querySelectorAll('.delegation-rung-select');
    let nonEmptyCount = 0;
    allSelects.forEach(s => { if (s.value) nonEmptyCount++; });

    if (nonEmptyCount < maxAttempts) {
      const addBtn = document.createElement('button');
      addBtn.type = 'button';
      addBtn.className = 'delegation-rung-add';
      addBtn.textContent = '+';
      addBtn.title = 'Add rung (max ' + maxAttempts + ')';
      addBtn.addEventListener('click', () => {
        // Insert a new <select> before the add button.
        const newSelect = document.createElement('select');
        newSelect.className = 'delegation-rung-select delegation-rung-new';
        newSelect.setAttribute('aria-label', `New rung model`);

        const emptyOpt = document.createElement('option');
        emptyOpt.value = '';
        emptyOpt.textContent = '(empty)';
        newSelect.appendChild(emptyOpt);

        availableModels.sort().forEach(m => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          newSelect.appendChild(opt);
        });

        const wrapper = document.createElement('span');
        wrapper.className = 'delegation-rung-wrap';
        wrapper.appendChild(newSelect);

        // Remove button.
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'delegation-rung-remove';
        removeBtn.textContent = '×';
        removeBtn.title = 'Remove this rung';
        removeBtn.addEventListener('click', () => {
          newSelect.value = '';
          removeBtn.style.display = 'none';
        });
        wrapper.appendChild(removeBtn);

        row.insertBefore(wrapper, addBtn);
      });
      row.appendChild(addBtn);
    }

    // Revert to generated button (only when pinned and differs from generated).
    const differs = !isPinned || pinned.join('\n') !== generated.join('\n');
    if (differs) {
      const revertBtn = document.createElement('button');
      revertBtn.type = 'button';
      revertBtn.className = 'delegation-ladder-revert';
      revertBtn.textContent = 'Revert';
      revertBtn.title = 'Clear the pin and regenerate the ladder';
      revertBtn.addEventListener('click', async () => {
        try {
          const response = await apiFetch('/api/delegation/ladder', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({task_type: taskType}),
          });
          if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(_errorMessage(data, 'Could not revert pin'));
          }
          await _refreshDelegation();
        } catch (error) {
          showToast(error.message, 'error');
        }
      });
      actions.appendChild(revertBtn);
    }

    // Save button (only when pinned).
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'delegation-ladder-save';
    saveBtn.textContent = 'Save';
    saveBtn.title = 'Save changes to the pinned ladder';
    saveBtn.addEventListener('click', async () => {
      const selects = row.querySelectorAll('.delegation-rung-select');
      const rungs = [];
      selects.forEach(s => {
        if (s.value.trim()) rungs.push(s.value.trim());
      });
      if (rungs.length === 0) {
        showToast('At least one rung required', 'error');
        return;
      }
      try {
        const response = await apiFetch('/api/delegation/ladder', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({task_type: taskType, rungs}),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(_errorMessage(data, 'Could not save pin'));
        }
        const data = await response.json().catch(() => ({}));
        if (data.problems && data.problems.length) {
          notifyResult('Pin saved with warnings: ' + data.problems.join('; '), 'warning');
        } else {
          showToast('Pin saved');
        }
        await _refreshDelegation();
      } catch (error) {
        showToast(error.message, 'error');
      }
    });
    actions.appendChild(saveBtn);
  }

  // Appended last so the group lands at the end of the row regardless of which
  // of the three buttons this state actually rendered. Skipped when empty, so
  // a row with no applicable actions does not carry a stray flex spacer.
  if (actions.childElementCount) row.appendChild(actions);

  return row;
}

/** One editable cell: an input bound to one (model, task_type, column). */
function _cell(row, column) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'delegation-cell';
  input.value = row[column] === null || row[column] === undefined ? '' : row[column];
  input.setAttribute('aria-label', `${column} for ${row.model} on ${row.task_type}`);
  input.addEventListener('change', () => _saveRow(row, column, input));
  return input;
}

async function _saveRow(row, column, input) {
  const raw = input.value.trim();
  // An empty cell is TBD, which is NOT zero: TBD makes the row ineligible,
  // zero is a real measurement and for cost means free.
  const value = raw === '' ? null : Number(raw);
  if (raw !== '' && Number.isNaN(value)) {
    notifyResult(`${column} must be a number or empty`, 'error');
    input.value = row[column] === null ? '' : row[column];
    return;
  }
  try {
    const response = await apiFetch('/api/delegation/row', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: row.model, task_type: row.task_type, [column]: value}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(_errorMessage(data, 'Could not save'));
    }
    row[column] = value;
    loadDelegation(true);
  } catch (error) {
    notifyResult(error.message, 'error');
    input.value = row[column] === null ? '' : row[column];
  }
}

/** Flip a task type's operational flag from its card's `toggle-knob`.
 *  Reverts `aria-pressed` on a rejected write -- the same "server truth over
 *  local patching" stance specs.js takes for a rejected status change, now
 *  expressed through the knob's own state attribute rather than a
 *  checkbox's `.checked`. */
async function _setOperational(taskType, operational, knob) {
  knob.setAttribute('aria-pressed', String(operational));
  try {
    const response = await apiFetch('/api/delegation/operational', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task_type: taskType, operational}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(_errorMessage(data, 'Could not change the operational flag'));
    }
    loadDelegation(true);
  } catch (error) {
    knob.setAttribute('aria-pressed', String(!operational));
    notifyResult(error.message, 'error');
  }
}

/** Whether this card's knob can move, and why not.
 *
 *  Three rules, and the two exclusions matter as much as the rule itself:
 *
 *  - A LIVE type is never blocked. A blocker explains why a type cannot be
 *    turned ON; turning one OFF is always allowed, and greying the knob out
 *    would strand an operator with a type they could not disable.
 *  - WARNINGS never block. An over-ceiling type with enforcement off is
 *    allowed to flip -- that is the whole point of the knob being off -- so
 *    disabling on a warning would silently reimpose the enforcement the
 *    operator deliberately switched off.
 *  - Otherwise a policy hold or a data blocker disables it, because the
 *    server will refuse the click anyway.
 */
function _knobBlockedReason(entry, live) {
  if (live || !entry) return null;
  if (entry.policy) return entry.policy;
  const data = Array.isArray(entry.data) ? entry.data.filter(Boolean) : [];
  return data.length ? data[0] : null;
}

/** The standard toggle-knob (web/index.html:558, :588 -- `.app-setting-row`'s
 *  App-tab settings), built here rather than reused from markup because one
 *  is needed per card and each must carry its own task type in its click
 *  handler's closure.
 *
 *  Disabled when the server would refuse the flip. Until 2026-09-17 every
 *  knob was clickable regardless: six of the ten task types animated across,
 *  were refused, and snapped back with a toast. The blockers needed to
 *  prevent that were already in the payload; the knob simply did not read
 *  them. */
function _operationalKnob(taskType, live, entry) {
  const knob = document.createElement('button');
  knob.type = 'button';
  knob.className = 'toggle-knob';
  knob.setAttribute('aria-pressed', String(live));
  knob.setAttribute('aria-label', `${taskType} operational`);
  const blocked = _knobBlockedReason(entry, live);
  if (blocked) {
    knob.disabled = true;
    // The reason travels with the control, not only in the box below it --
    // the knob is what the operator reaches for first.
    knob.title = blocked;
    knob.setAttribute('aria-describedby', `delegationBlocker-${taskType}`);
  }
  const track = document.createElement('span');
  track.className = 'knob-track';
  const thumb = document.createElement('span');
  thumb.className = 'knob-thumb';
  track.appendChild(thumb);
  knob.appendChild(track);
  const label = document.createElement('span');
  label.className = 'knob-label';
  knob.appendChild(label);
  knob.addEventListener('click', () => {
    const next = knob.getAttribute('aria-pressed') !== 'true';
    _setOperational(taskType, next, knob);
  });
  return knob;
}

/** The blocker box for one card: one line per policy/data reason from
 *  `_blockerLines`, or nothing at all -- CSS hides `.delegation-blocker`
 *  when it has no children, so a clean or already-operational type shows no
 *  empty box. */
/** One labelled group of blocker lines, or nothing when the group is empty. */
function _blockerGroup(wrap, label, lines, extraClass) {
  if (!lines.length) return;
  const heading = document.createElement('p');
  heading.className = 'delegation-blocker-heading';
  heading.textContent = label;
  wrap.appendChild(heading);
  lines.forEach(text => {
    const p = document.createElement('p');
    p.className = `delegation-blocker-line${extraClass ? ' ' + extraClass : ''}`;
    p.textContent = text;
    wrap.appendChild(p);
  });
}

/** The blocker box for one card, grouped by KIND rather than run together.
 *
 *  A policy hold, a data invariant and an unenforced warning are three
 *  different situations needing three different responses -- a decision, a
 *  measurement, and nothing at all -- and as a flat list they read as one
 *  undifferentiated wall of reasons. The headings are what let an operator
 *  tell "I must decide this" from "I must measure this" from "this is only
 *  being reported".
 *
 *  CSS hides `.delegation-blocker` when it has no children, so a clean type
 *  still shows no empty box. */
function _blockerElement(entry, taskType) {
  const wrap = document.createElement('div');
  wrap.className = 'delegation-blocker';
  if (taskType) wrap.id = `delegationBlocker-${taskType}`;
  const policy = entry && entry.policy ? [entry.policy] : [];
  const data = (Array.isArray(entry && entry.data) ? entry.data : [])
    .filter(Boolean);
  const allWarnings = _warningLines(entry);
  // Budget warnings (carry a $) travel with latency ceiling warnings but
  // need their own heading so an operator can tell them apart.
  const budgetWarnings = allWarnings.filter(w => w.startsWith('tree cost'));
  const latencyWarnings = allWarnings.filter(w => !w.startsWith('tree cost'));
  _blockerGroup(wrap, 'policy — a decision, not data', policy);
  _blockerGroup(wrap, 'data — needs a measurement or a cheaper rung', data);
  _blockerGroup(wrap, 'budget — reported, not enforced',
                budgetWarnings, 'delegation-warning-line');
  _blockerGroup(wrap, 'latency ceiling — reported, not enforced',
                latencyWarnings, 'delegation-warning-line');
  return wrap;
}

/** Poll one forced cell run until it stops running.
 *
 *  Spec 9's re-measure takes roughly six minutes (three repeats at the
 *  benchmark's own per-repeat cost), so the poll interval is generous on
 *  purpose: a faster poll would not make the measurement finish any sooner,
 *  it would only hit the endpoint harder. The row is refreshed on completion
 *  (`_refreshDelegation`) rather than patched in place, because a re-measure
 *  can also change `dormant`, the ladder and the reorder flag -- all of
 *  which live outside the one row that was clicked. */
async function _pollCell(cellRunId, button) {
  let bar;
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 5000));
    let response;
    try {
      response = await apiFetch(`/api/delegation/benchmark/cell/${cellRunId}`);
    } catch (error) {
      button.textContent = 'Re-measure';
      button.disabled = false;
      notifyResult(error.message, 'error');
      return;
    }
    if (!response.ok) {
      button.textContent = 'Re-measure';
      button.disabled = false;
      return;
    }
    const state = await response.json();
    if (state.status !== 'running') {
      button.textContent = state.status === 'ok' ? 'Done' : 'Failed';
      button.disabled = false;
      if (bar) bar.remove();
      await _refreshDelegation();
      return;
    }
    // Progress bar: [N/M] percentage with elapsed time.
    if (state.progress_m && state.progress_m > 0) {
      const pct = Math.round((state.progress_n / state.progress_m) * 100);
      if (!bar) {
        bar = document.createElement('span');
        bar.className = 'delegation-progress';
        bar.title = 'Progress';
        button.parentNode.insertBefore(bar, button.nextSibling);
      }
      bar.textContent = `${pct}% (${state.progress_n}/${state.progress_m}) · ${Math.round(state.elapsed_s || 0)}s`;
    }
  }
}

/** The per-row Re-measure control (spec 9): forces one cell now, under load,
 *  rather than waiting for the next nightly sweep. Runs immediately on
 *  click, so the button disables itself and shows progress for the whole
 *  ~6 minutes rather than reading as unresponsive. */
function _remeasureButton(row) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'delegation-remeasure';
  button.textContent = 'Re-measure';
  button.title = 'Measure this cell now (~6 min). The result is recorded as '
               + 'measured under load.';
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Measuring…';
    let response;
    try {
      response = await apiFetch('/api/delegation/benchmark/cell', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: row.model, task_type: row.task_type}),
      });
    } catch (error) {
      button.textContent = 'Re-measure';
      button.disabled = false;
      notifyResult(error.message, 'error');
      return;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      button.textContent = _errorMessage(data, 'Failed');
      button.disabled = false;
      return;
    }
    const {cell_run_id: cellRunId} = await response.json();
    _pollCell(cellRunId, button);
  });
  return button;
}

/** The per-row Acknowledge control. Only rendered for a `reorder_flagged`
 *  row (see `_modelTable`'s caller) -- an unflagged row has nothing to
 *  acknowledge, and drawing the button unconditionally would make a renderer
 *  bug that always shows it indistinguishable from correct behaviour.
 *
 *  The highlight this clears comes back only from a later measurement that
 *  reorders the ladder again: not on reload, not on the next sweep, not with
 *  time. That is enforced server-side (`benchmark_reorder.acknowledge`); this
 *  button only calls it and reloads. */
function _ackButton(row) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'delegation-ack';
  button.textContent = 'Acknowledge';
  button.title = 'This measurement reordered the ladder. Acknowledging clears '
               + 'the highlight until a later measurement reorders it again.';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await apiFetch('/api/delegation/capability/ack', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: row.model, task_type: row.task_type}),
      });
    } catch (error) {
      notifyResult(error.message, 'error');
    }
    await _refreshDelegation();
  });
  return button;
}

/** The model-rows table for one card: one row per (model, task_type), one
 *  editable cell per measured column, plus the Re-measure control and (only
 *  when flagged) the Acknowledge control.
 *
 *  Rule 1 (spec 9): a dormant cell is never dropped from this table -- it is
 *  marked (`delegation-row-dormant`, the `dormant` tag on the model name) and
 *  stays exactly where it was, because a silently skipped cell is
 *  indistinguishable from a cell nobody thought to measure.
 *  Rule 2: the reorder highlight (`delegation-row-reordered`) and its
 *  Acknowledge button are driven only by `row.reorder_flagged`, which this
 *  file never sets client-side -- it only ever reads it. */
/** Pure: value-for-money per model, for one task type's rows.
 *
 *  Returns a Map of model -> `{kind: 'pct', value: N}` | `{kind: 'free'}` |
 *  `{kind: 'none'}`. Advisory only. Nothing here feeds routing: the ladder is
 *  still derived server-side by `CapabilityTable.generated_ladder`, which
 *  sorts on cost alone and uses accuracy as a ratchet. This column exists so
 *  an operator can SEE the trade the ladder is making and pin a different one
 *  if they disagree -- a score that silently reordered anything would be a
 *  second routing authority, which spec 2.6 deliberately does not have.
 *
 *  The number is `accuracy / cost_per_1m_tokens`, scaled so the best-value
 *  priced row in this task type reads 100%. The ladder's own sort multiplies
 *  cost by an expected token count per task type; that factor is the same for
 *  every row here and so cancels in the ratio, which is why this needs no
 *  task-size constant and cannot drift from one.
 *
 *  Two cases are deliberately not percentages:
 *
 *  - A priced-at-zero row (a local model) has no finite value ratio. It
 *    reports 'free' and is kept OUT of the denominator. Including an infinite
 *    value would drive every paid model to 0%, which is the opposite of
 *    informative.
 *  - An unmeasured accuracy or an absent price reports 'none', never 0%. Zero
 *    would read as "measured, and terrible"; the truth is "not measured",
 *    and conflating those is the mistake `is_ladder_eligible` exists to avoid.
 */
export function valueScores(rowsForType) {
  const out = new Map();
  const finite = [];
  (rowsForType || []).forEach(row => {
    const accuracy = row.accuracy;
    const cost = row.cost_per_1m_tokens;
    if (accuracy === null || accuracy === undefined
        || cost === null || cost === undefined) {
      out.set(row.model, {kind: 'none'});
      return;
    }
    if (cost === 0) {
      out.set(row.model, {kind: 'free'});
      return;
    }
    const value = accuracy / cost;
    if (!Number.isFinite(value) || value < 0) {
      out.set(row.model, {kind: 'none'});
      return;
    }
    finite.push(value);
    out.set(row.model, {kind: 'pct', raw: value});
  });
  const best = finite.length ? Math.max(...finite) : 0;
  out.forEach((entry, model) => {
    if (entry.kind !== 'pct') return;
    // best === 0 means every priced row measured 0 accuracy: they are all
    // equally worthless, and 0% says that better than a division by zero.
    out.set(model, {kind: 'pct', value: best > 0 ? Math.round(entry.raw / best * 100) : 0});
  });
  return out;
}

/** The cell text for one `valueScores` entry. */
function _scoreText(entry) {
  if (!entry || entry.kind === 'none') return '—';
  if (entry.kind === 'free') return 'free';
  return `${entry.value}%`;
}

function _modelTable(rowsForType, columns) {
  const table = document.createElement('table');
  table.className = 'delegation-model-table';
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  const modelHead = document.createElement('th');
  modelHead.textContent = 'Model';
  headRow.appendChild(modelHead);
  const scoreHead = document.createElement('th');
  scoreHead.textContent = 'best';
  scoreHead.className = 'delegation-score-head';
  scoreHead.title = 'Value for money: accuracy divided by cost, scaled so the '
                  + 'best-value priced model for this task type is 100%. '
                  + 'Advisory only -- it does not change routing. The ladder '
                  + 'is still built cheapest-first, keeping a model only while '
                  + 'it is not less accurate than the rung above it, so a '
                  + 'low-scoring model can still hold a rung.';
  headRow.appendChild(scoreHead);
  columns.forEach(column => {
    const th = document.createElement('th');
    th.textContent = column;
    headRow.appendChild(th);
  });
  const actionHead = document.createElement('th');
  actionHead.textContent = '';
  headRow.appendChild(actionHead);
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  const scores = valueScores(rowsForType);
  rowsForType.forEach(row => {
    const tr = document.createElement('tr');
    if (row.reorder_flagged) tr.classList.add('delegation-row-reordered');
    if (row.dormant) tr.classList.add('delegation-row-dormant');

    const modelCell = document.createElement('td');
    modelCell.className = 'delegation-model-name';
    modelCell.textContent = row.model;
    if (row.dormant) {
      const tag = document.createElement('span');
      tag.className = 'delegation-dormant-tag';
      tag.textContent = ' dormant';
      // A silently skipped cell is indistinguishable from one nobody thought
      // to measure, which is the failure this whole subsystem exists to stop.
      tag.title = 'Failed three consecutive sweeps; no longer attempted. '
                + 'Re-measure to clear.';
      modelCell.appendChild(tag);
    }
    tr.appendChild(modelCell);

    const entry = scores.get(row.model);
    const scoreCell = document.createElement('td');
    scoreCell.className = 'delegation-score';
    scoreCell.textContent = _scoreText(entry);
    if (entry && entry.kind === 'pct' && entry.value === 100) {
      scoreCell.classList.add('delegation-score-best');
    }
    if (entry && entry.kind === 'free') {
      scoreCell.classList.add('delegation-score-free');
      scoreCell.title = 'Costs nothing to run, so it has no value-for-money '
                      + 'ratio and is left out of the 100% comparison.';
    }
    if (entry && entry.kind === 'none') {
      scoreCell.title = 'No score: this row has no measured accuracy or no '
                      + 'price. Not the same as scoring zero.';
    }
    tr.appendChild(scoreCell);

    columns.forEach(column => {
      const td = document.createElement('td');
      td.appendChild(_cell(row, column));
      // A cost with a stated basis is marked, because an assumed rate that
      // looks measured is the defect this column exists to end -- terra has
      // carried a rate assumed from luna since 2026-09-17 while luna moved
      // eight times away from it, and nothing on this page could say so.
      if (column === 'cost_per_1m_tokens' && row.cost_basis) {
        const mark = document.createElement('span');
        mark.className = 'delegation-cost-basis';
        mark.textContent = ' †';
        mark.title = `Not measured: ${row.cost_basis}`;
        td.appendChild(mark);
      }
      tr.appendChild(td);
    });

    const actions = document.createElement('td');
    actions.className = 'delegation-actions';
    actions.appendChild(_remeasureButton(row));
    if (row.reorder_flagged) actions.appendChild(_ackButton(row));
    tr.appendChild(actions);

    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

/** One card for one task type: heading + operational knob, the derived
 *  ladder (readable, each rung still carrying its tooltip), any blocker
 *  reason, and the editable model-rows table. */
function _card(taskType, rowsForType, payload) {
  const card = document.createElement('div');
  card.className = 'delegation-card';

  const head = document.createElement('div');
  head.className = 'delegation-card-head';
  const heading = document.createElement('h4');
  heading.className = 'delegation-card-title';
  heading.textContent = taskType;
  head.appendChild(heading);

  // Budget warning badge — dollar icon when the type's tree cost
  // exceeds BUDGET_USD but enforcement is off.
  const blockerEntryForKnob = (payload.blockers || {})[taskType];
  if (blockerEntryForKnob && blockerEntryForKnob.over_budget) {
    const badge = document.createElement('span');
    badge.className = 'delegation-budget-badge';
    badge.textContent = '$';
    badge.title = 'Tree cost exceeds budget';
    head.appendChild(badge);
  }

  const live = (payload.operational || []).includes(taskType);
  head.appendChild(_operationalKnob(taskType, live, blockerEntryForKnob));
  card.appendChild(head);

  const ladder = (payload.ladders || {})[taskType] || [];
  const generated = (payload.generated_ladders || {})[taskType] || [];
  const pins = payload.pins || {};
  const isPinned = taskType in pins;

  // One line, not two. This used to print the ladder twice for a pinned type:
  // once as static text and again directly underneath as the selects that
  // edit it. That is safe to merge because CapabilityTable.ladder() returns
  // `list(self._pins[task_type])` verbatim when a pin exists, so the effective
  // ladder and the pinned one cannot disagree -- if that ever stops being
  // true, this has to show the effective ladder with the pin marked, not the
  // pin alone. Unpinned, the editor is just a "Pin" button, so the same row
  // carries the static rungs and that button.
  card.appendChild(
    _ladderEditor(taskType, ladder, generated, rowsForType, isPinned, payload));

  const blockerEntry = (payload.blockers || {})[taskType];
  card.appendChild(_blockerElement(blockerEntry, taskType));

  card.appendChild(_modelRows(taskType, rowsForType, _resolveColumns(payload)));

  return card;
}

/** The model table, behind a hide/unhide disclosure.
 *
 *  Collapsed by default. Eleven task types times the models measured on each
 *  is a page you scroll rather than read, and the parts that answer "can this
 *  go live" -- the knob, the ladder, the blockers -- are all above this point
 *  and stay visible when it is shut.
 *
 *  The row count lives in the summary, which is the whole reason a shut table
 *  is safe to arrive at. Settings > Specs shipped collapsible groups whose
 *  headers said only the group name, and with one group holding everything it
 *  read as an empty panel (see specs.js `_makeGroup`). A summary that states
 *  what it is hiding does not have that failure mode.
 *
 *  <details> rather than a button and a class: it is the element the browser
 *  already implements this with -- keyboard, focus, and the open attribute for
 *  free -- and it is what the specs gallery uses, so the two pages behave the
 *  same way. */
function _modelRows(taskType, rowsForType, columns) {
  const details = document.createElement('details');
  details.className = 'delegation-models';
  details.dataset.taskType = taskType;

  const summary = document.createElement('summary');
  summary.className = 'delegation-models-summary';
  const count = rowsForType.length;
  summary.textContent = `Models · ${count}`;
  // The accessible name says what the control does; the visible text says what
  // is behind it. A summary reading "Models · 5" alone leaves a screen-reader
  // user to infer that it toggles.
  summary.setAttribute(
    'aria-label',
    `Show or hide the ${count} model row${count === 1 ? '' : 's'} for ${taskType}`);

  details.appendChild(summary);
  details.appendChild(_modelTable(rowsForType, columns));
  return details;
}

// ── Read-only config overview (spec 9.2's first sentence) ──────────────────
//
// The kill switch (9.1), the tunables from 5 and 10, and the cost ceiling
// (2.7). Read only, by operator ruling on this task: the matrix cells are
// the only editable part of this page. Where the server has no single
// source for a value it says so in `note` rather than a number being
// invented here.
const _CONFIG_SECTIONS = [
  ['attempts_and_caps', 'Attempts and caps'],
  ['cost_ceiling', 'Cost ceiling'],
  ['observability', 'Observability'],
];

function _configItemText(item) {
  if (item.value === null || item.value === undefined) {
    return item.note || 'not available';
  }
  return Array.isArray(item.value) ? item.value.join(', ') : String(item.value);
}

function _renderConfig(config, host) {
  if (!host) return;
  host.replaceChildren();
  if (!config) return;

  const killSwitch = document.createElement('div');
  killSwitch.className = 'delegation-config-section';
  const ksTitle = document.createElement('h4');
  ksTitle.textContent = `Kill switch (spec ${config.kill_switch.section})`;
  killSwitch.appendChild(ksTitle);
  const ksLine = document.createElement('p');
  ksLine.textContent = config.kill_switch.available ? 'Available'
    : (config.kill_switch.note || 'not available');
  killSwitch.appendChild(ksLine);
  host.appendChild(killSwitch);

  _CONFIG_SECTIONS.forEach(([key, label]) => {
    const section = config[key];
    if (!section) return;
    const box = document.createElement('div');
    box.className = 'delegation-config-section';
    const heading = document.createElement('h4');
    heading.textContent = `${label} (spec ${section.section})`;
    box.appendChild(heading);
    const list = document.createElement('dl');
    Object.keys(section).forEach(itemKey => {
      if (itemKey === 'section') return;
      const item = section[itemKey];
      const dt = document.createElement('dt');
      dt.textContent = itemKey.replace(/_/g, ' ');
      const dd = document.createElement('dd');
      dd.textContent = _configItemText(item);
      if (item.source) dd.title = `source: ${item.source}`;
      list.appendChild(dt);
      list.appendChild(dd);
    });
    box.appendChild(list);
    host.appendChild(box);
  });
}

/** The status band: the plain-English headline plus the compact facts row
 *  (measured coverage, latency ceiling, tree budget). Reads the same
 *  `config` block `_renderConfig` shows in full further down the page --
 *  this is the condensed version an operator checks first. */
/** Turn spec 5.1's combined latency ceiling into a blocking invariant, or
 *  back off. Global rather than per-card: the ceiling is one number for every
 *  task type, so a per-card knob would imply nine independent settings.
 *
 *  Same optimistic-then-revert shape as `_setOperational`: the knob shows the
 *  requested state immediately and snaps back if the server refuses, which it
 *  will when enabling enforcement would invalidate an already-operational
 *  type. */
async function _setCeilingEnforcement(enabled, knob) {
  knob.setAttribute('aria-pressed', String(enabled));
  try {
    const res = await apiFetch('/api/delegation/ceiling-enforcement', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      knob.setAttribute('aria-pressed', String(!enabled));
      showToast(_errorMessage(
        data, 'Could not change latency ceiling enforcement'), 'error');
      return;
    }
    await _refreshDelegation();
  } catch (err) {
    knob.setAttribute('aria-pressed', String(!enabled));
    showToast('Could not change latency ceiling enforcement', 'error');
  }
}

/** The status band's enforcement knob, built from the same `toggle-knob`
 *  component the per-card operational knobs use. */
/** A knob's caption: its name, and under it the value and enforcement state.
 *
 *  One line of 11px mono made the name, the number and the word "enforced"
 *  equally weighted, so the band read as a row of similar-looking facts and
 *  the switches did not stand out as things you could change. Splitting the
 *  name onto its own line is what lets the value stay quiet underneath it. */
function _knobText(name, detail) {
  const text = document.createElement('span');
  text.className = 'delegation-knob-text';
  const nameEl = document.createElement('span');
  nameEl.className = 'delegation-knob-name';
  nameEl.textContent = name;
  const detailEl = document.createElement('span');
  detailEl.className = 'delegation-knob-detail';
  detailEl.textContent = detail;
  text.append(nameEl, detailEl);
  return text;
}

function _ceilingKnob(state) {
  const wrap = document.createElement('div');
  wrap.className = 'delegation-ceiling-knob';
  // Carries the ceiling's VALUE as well as its state, because this replaces
  // the plain `latency ceiling: 1500s` fact rather than sitting beside it --
  // two facts both opening with "latency ceiling" read as two unrelated
  // settings, and the knob looked like it governed something else.
  const value = state.ceiling_s === null || state.ceiling_s === undefined
    ? '—' : `${state.ceiling_s}s`;
  const label = _knobText(
    'Latency ceiling',
    state.enabled ? `${value} — enforced` : `${value} — reported, not enforced`);
  const knob = document.createElement('button');
  knob.type = 'button';
  knob.className = 'toggle-knob';
  knob.setAttribute('aria-pressed', String(Boolean(state.enabled)));
  knob.setAttribute('aria-label', 'enforce the combined latency ceiling');
  knob.title = state.enabled
    ? 'A task type whose worst-case path exceeds the ceiling cannot go '
      + 'operational, and a leaf is stopped before a stage that would exceed it.'
    : 'Breaches are computed and shown, but do not block. Off by default: the '
      + 'ceiling is derived from the most expensive operational task type and '
      + 'nothing is operational yet.';
  const track = document.createElement('span');
  track.className = 'knob-track';
  const thumb = document.createElement('span');
  thumb.className = 'knob-thumb';
  track.appendChild(thumb);
  knob.appendChild(track);
  knob.addEventListener('click', () => {
    _setCeilingEnforcement(
      knob.getAttribute('aria-pressed') !== 'true', knob);
  });
  // Knob before caption: the control is the point of the cell, and a switch
  // that trails its own label reads as an afterthought.
  wrap.append(knob, label);
  return wrap;
}

/** Turn spec 2.7's tree budget into a blocking invariant, or back off.
 *
 *  The mirror of `_setCeilingEnforcement`, and global for the same reason:
 *  the budget is one number for every task type. Same optimistic-then-revert
 *  shape -- the server refuses to enable when an already-operational type is
 *  over budget, because storing the flag first would leave a deployment that
 *  refuses to start on its next restart. */
async function _setBudgetEnforcement(enabled, knob) {
  knob.setAttribute('aria-pressed', String(enabled));
  try {
    const res = await apiFetch('/api/delegation/budget-enforcement', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      knob.setAttribute('aria-pressed', String(!enabled));
      showToast(_errorMessage(
        data, 'Could not change tree budget enforcement'), 'error');
      return;
    }
    await _refreshDelegation();
  } catch (err) {
    knob.setAttribute('aria-pressed', String(!enabled));
    showToast('Could not change tree budget enforcement', 'error');
  }
}

/** The status band's budget knob. Built the same way as `_ceilingKnob`, and
 *  carrying the budget's VALUE for the same reason: it replaces the plain
 *  `tree budget: $5.25` fact rather than sitting beside it.
 *
 *  The value comes from the config block rather than from the enforcement
 *  payload, because the server already publishes it there -- unlike the
 *  ceiling, which sends `ceiling_s` with its knob state. */
function _budgetKnob(state, budgetUsd) {
  const wrap = document.createElement('div');
  wrap.className = 'delegation-budget-knob';
  const value = budgetUsd === null || budgetUsd === undefined
    ? '—' : `$${budgetUsd} / tree`;
  const label = _knobText(
    'Tree budget',
    state.enabled ? `${value} — enforced` : `${value} — reported, not enforced`);
  const knob = document.createElement('button');
  knob.type = 'button';
  knob.className = 'toggle-knob';
  knob.setAttribute('aria-pressed', String(Boolean(state.enabled)));
  knob.setAttribute('aria-label', 'enforce the tree cost budget');
  knob.title = state.enabled
    ? 'A task type whose ladder costs more than the budget cannot go '
      + 'operational, and this deployment refuses to start if one already is.'
    : 'Overruns are computed and shown, but do not block. Off by default: the '
      + 'tree cost is computed from an assumed leaf count, so an over-estimate '
      + 'would refuse task types that would in fact fit.';
  const track = document.createElement('span');
  track.className = 'knob-track';
  const thumb = document.createElement('span');
  thumb.className = 'knob-thumb';
  track.appendChild(thumb);
  knob.appendChild(track);
  knob.addEventListener('click', () => {
    _setBudgetEnforcement(
      knob.getAttribute('aria-pressed') !== 'true', knob);
  });
  // Knob first, matching _ceilingKnob.
  wrap.append(knob, label);
  return wrap;
}

/** Spec 9.1's one global kill switch.
 *
 *  Global by construction, not by convenience: 9.1 rejects per-gate toggles
 *  because each one multiplies the reachable states and every combination is a
 *  configuration nobody has tested. One switch, two states, both verifiable.
 *
 *  Turning it ON can be refused by the server -- switching on means the table
 *  is validated at the next boot, so a table that breaks spec 1.1 would leave
 *  a deployment that will not start. Turning it OFF is never refused. Same
 *  optimistic-then-revert shape as the other knobs.  */
async function _setDelegationEnabled(enabled, knob) {
  knob.setAttribute('aria-pressed', String(enabled));
  try {
    const res = await apiFetch('/api/delegation/enabled', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      knob.setAttribute('aria-pressed', String(!enabled));
      showToast(_errorMessage(data, 'Could not change delegation'), 'error');
      return;
    }
    await _refreshDelegation();
  } catch (err) {
    knob.setAttribute('aria-pressed', String(!enabled));
    showToast('Could not change delegation', 'error');
  }
}

/** The master knob. Rendered above the facts row rather than inside it,
 *  because it governs everything below and a switch that sits among the
 *  measurements reads as one more measurement. */
function _enabledKnob(state) {
  const wrap = document.createElement('div');
  wrap.className = 'delegation-enabled-knob';
  const label = document.createElement('span');
  label.className = 'delegation-master-label';
  label.textContent = 'Delegation';
  if (!state.enabled) {
    // The off state has to say what it costs. "Delegation" alone next to a
    // dark switch is not a warning, and this switch turning off means nothing
    // on the rest of the page is in effect.
    const off = document.createElement('span');
    off.className = 'delegation-master-off';
    off.textContent = 'OFF — nothing below is in effect';
    label.appendChild(off);
  }
  const knob = document.createElement('button');
  knob.type = 'button';
  knob.className = 'toggle-knob';
  knob.setAttribute('aria-pressed', String(Boolean(state.enabled)));
  knob.setAttribute('aria-label', 'enable the delegation design');
  knob.title = state.enabled
    ? 'The capability table is validated at startup and the shadow recorder '
      + 'writes a decision for every orchestrator task.'
    : 'Off: the table is NOT validated at startup, so a broken table cannot '
      + 'stop the console booting, and nothing in this design records or runs. '
      + 'The page below still shows what the table WOULD produce.';
  const track = document.createElement('span');
  track.className = 'knob-track';
  const thumb = document.createElement('span');
  thumb.className = 'knob-thumb';
  track.appendChild(thumb);
  knob.appendChild(track);
  knob.addEventListener('click', () => {
    _setDelegationEnabled(knob.getAttribute('aria-pressed') !== 'true', knob);
  });
  wrap.appendChild(label);
  wrap.appendChild(knob);
  return wrap;
}

function _renderStatus(payload) {
  const line = byId('delegationStatusLine');
  const facts = byId('delegationFacts');
  const totalTypes = Object.keys(payload.ladders || {}).length;
  const opCount = (payload.operational || []).length;
  if (line) line.textContent = _statusHeadline(opCount, totalTypes);
  if (!facts) return;
  facts.replaceChildren();

  const columns = _resolveColumns(payload);
  const {measured, total} = _measuredCellsSummary(payload.rows || [], columns);
  const cfg = payload.config || {};
  const ceiling = cfg.attempts_and_caps && cfg.attempts_and_caps.combined_latency_ceiling_s
    ? cfg.attempts_and_caps.combined_latency_ceiling_s.value : null;
  const budget = cfg.cost_ceiling && cfg.cost_ceiling.budget_usd_per_tree
    ? cfg.cost_ceiling.budget_usd_per_tree.value : null;

  // The ceiling fact is rendered by `_ceilingKnob` when the server sent the
  // enforcement state, so it is omitted here rather than printed twice.
  const items = [`${measured} of ${total} cells measured`];
  if (!payload.ceiling_enforcement) {
    items.push(ceiling === null || ceiling === undefined
      ? 'latency ceiling: not available' : `latency ceiling: ${ceiling}s`);
  }
  // Same rule as the ceiling above: rendered by `_budgetKnob` when the server
  // sent the enforcement state, so it is omitted here rather than printed
  // twice.
  if (!payload.budget_enforcement) {
    items.push(budget === null || budget === undefined
      ? 'tree budget: not available' : `tree budget: $${budget}`);
  }
  // The band is a strip, not a flat wrap of similar-looking spans: the master
  // switch sits in its own cell, the two enforcement knobs follow it, and the
  // read-only facts are pushed to the far end. That ordering used to be a CSS
  // `order:-1` on the master knob while the markup appended it last -- the
  // meaning lived in the stylesheet, so reading the JS told you the wrong
  // order. Here the DOM says what it means.
  const strip = document.createElement('div');
  strip.className = 'delegation-strip';
  const master = document.createElement('div');
  master.className = 'delegation-strip-master';
  const rest = document.createElement('div');
  rest.className = 'delegation-strip-rest';
  if (payload.enabled) master.appendChild(_enabledKnob(payload.enabled));
  if (payload.ceiling_enforcement) {
    rest.appendChild(_ceilingKnob(payload.ceiling_enforcement));
  }
  if (payload.budget_enforcement) {
    rest.appendChild(_budgetKnob(payload.budget_enforcement, budget));
  }
  const factWrap = document.createElement('span');
  factWrap.className = 'delegation-strip-facts';
  items.forEach(text => {
    const span = document.createElement('span');
    span.className = 'delegation-fact';
    span.textContent = text;
    factWrap.appendChild(span);
  });
  rest.appendChild(factWrap);
  // A strip with no master cell would leave an empty tinted box, so it is only
  // added when the server actually sent the switch's state.
  if (master.childElementCount) strip.appendChild(master);
  strip.appendChild(rest);
  facts.appendChild(strip);
  // The whole panel is marked when the design is off, so a ladder on this page
  // cannot be mistaken for one that is in effect. It is still SHOWN -- you
  // need to read the table to fix it before turning the switch back on.
  const panel = byId('panelDelegation');
  if (panel && payload.enabled) {
    panel.classList.toggle('delegation-off', !payload.enabled.enabled);
  }
}

/** "⟳ Refresh" row: same disable/status-text/timeout shape as Backends'
 *  `_refreshBackends` (app.js), against this panel's own button and status
 *  ids rather than a shared helper -- the two panels reload different data
 *  through different functions. */
async function _refreshDelegation() {
  const btn = byId('delegationRefreshBtn');
  const status = byId('delegationRefreshStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Refreshing…';
  try {
    await loadDelegation(true);
    if (status) status.textContent = 'Refreshed';
  } catch (error) {
    if (status) status.textContent = 'Refresh failed';
  } finally {
    if (btn) btn.disabled = false;
    setTimeout(() => { if (status) status.textContent = ''; }, 3000);
  }
}
byId('delegationRefreshBtn')?.addEventListener('click', _refreshDelegation);

export async function loadDelegation(force = false) {
  const host = byId('delegationMatrix');
  if (!host) return null;
  if (!force && host.children.length) return null;

  let payload;
  try {
    const response = await apiFetch('/api/delegation');
    if (!response.ok) throw new Error('Could not load the delegation table');
    payload = await response.json();
  } catch (error) {
    notifyResult(error.message, 'error');
    return null;
  }

  _renderStatus(payload);
  _renderConfig(payload.config, byId('delegationConfig'));

  // Which model tables were open, so a re-render does not shut them.
  //
  // Every save re-renders this panel: `_saveRow` calls loadDelegation so the
  // ladder reflects the value just written. With the tables collapsed by
  // default and the DOM rebuilt from scratch, editing one cell snapped shut
  // the table being edited -- so the second edit of a session was made against
  // a table the operator had to re-open every time. Found by
  // test_qa_delegation_all_task_types_e2e, whose restore step could not reach
  // the cell it had just written.
  //
  // Keyed by task type rather than by index: a re-render can add or remove a
  // card, and an index would then reopen a different task type's table than
  // the one the operator opened.
  const wasOpen = new Set(
    Array.from(host.querySelectorAll('.delegation-models[open]'))
      .map(el => el.dataset.taskType));

  host.replaceChildren();
  const byType = {};
  (payload.rows || []).forEach(row => {
    (byType[row.task_type] = byType[row.task_type] || []).push(row);
  });

  Object.keys(byType).sort().forEach(taskType => {
    host.appendChild(_card(taskType, byType[taskType], payload));
  });

  wasOpen.forEach(taskType => {
    const group = host.querySelector(
      `.delegation-models[data-task-type="${CSS.escape(taskType)}"]`);
    if (group) group.open = true;
  });

  return (payload.rows || []).length;
}
