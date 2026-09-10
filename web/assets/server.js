// Server statistics: the health of the machine WebConsole runs on.
//
// Distinct from the Statistics tab next door, which charts what Claude cost.
// This one answers a different question -- is the box in trouble, and is
// WebConsole the reason -- so it leads with a live reading and keeps history
// underneath rather than the other way round.
//
// Charts are borrowed wholesale from stats.js so both pages share one visual
// language and one x-axis convention. What this file adds is the unit
// handling: percentages are pinned to a 0-100 axis and bytes are rendered as
// bytes, neither of which the token-count defaults get right.

import {lineChart, seriesTable, exact, slotColor} from './stats.js';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Bytes in the largest unit that keeps the number readable. */
export function bytes(n) {
  const v = Number(n) || 0;
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(1)} GiB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MiB`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KiB`;
  return `${v} B`;
}

export function pct(n) {
  return `${(Number(n) || 0).toFixed(0)}%`;
}

/** Load average, to two decimals. Module scope because two chart builders
 *  use it now -- it was a local inside renderHistory, so the transport
 *  charts referenced an undefined name and would have thrown at render
 *  time while the syntax gate, which only parses, stayed green. */
export function loadFmt(v) {
  return (Number(v) || 0).toFixed(2);
}

/** "3d 4h", "4h 12m", "12m" -- the two largest units that are non-zero. */
export function duration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

/**
 * Build chart series straight from the wide rows /api/system/series returns.
 *
 * stats.js's toSeries() expects long-format (bucket, key, value) rows and
 * pivots them. The system table is already one row per bucket with a column
 * per metric, so pivoting it would mean unpivoting it first.
 */
export function seriesFrom(rows, defs) {
  const buckets = rows.map(row => row.bucket);
  const series = defs.map(def => {
    // Null is preserved rather than coerced. The series now arrives on a
    // continuous bucket spine, and a bucket the sampler never wrote comes back
    // with null metrics because no measurement was taken. `Number(null) || 0`
    // turned that into a reading of zero, which would draw the box at 0% CPU
    // and 0% memory across precisely the windows it was not being sampled --
    // an invented measurement, and the chart looks most confident exactly
    // where it knows least.
    const values = rows.map(row => {
      const raw = row[def.field];
      if (raw === null || raw === undefined) return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n : null;
    });
    // Summary figures describe what was measured, so they skip the holes. An
    // outage must not drag the mean towards zero, or a page of green averages
    // would be the visible effect of the sampler having stopped.
    const real = values.filter(v => v !== null);
    return {
      key: def.key,
      values,
      total: real.reduce((a, b) => a + b, 0),
      peak: real.length ? Math.max(...real) : 0,
      mean: real.length ? real.reduce((a, b) => a + b, 0) / real.length : 0,
    };
  });
  return {buckets, series};
}

/** A gauge's headline is its peak, not its total. */
const peakOf = format => entry => `peak ${format(entry.peak)}`;

function card(grid, {label, value, detail, ratio}) {
  const box = el('div', 'srv-card');
  box.appendChild(el('div', 'srv-card-label', label));
  box.appendChild(el('div', 'srv-card-value', value));
  if (typeof ratio === 'number') {
    const track = el('div', 'srv-meter');
    const fill = el('span', 'srv-meter-fill');
    const clamped = Math.max(0, Math.min(100, ratio));
    fill.style.width = `${clamped}%`;
    // Three bands rather than a gradient: the point of the bar is to be
    // readable at a glance on a phone, and a continuous hue ramp is exactly
    // the thing colour-vision deficiency flattens.
    if (clamped >= 90) fill.classList.add('srv-meter-bad');
    else if (clamped >= 75) fill.classList.add('srv-meter-warn');
    track.appendChild(fill);
    box.appendChild(track);
  }
  if (detail) box.appendChild(el('div', 'srv-card-detail', detail));
  grid.appendChild(box);
}

function renderLive(container, live) {
  const grid = el('div', 'srv-cards');

  const cores = live.ncpu ? ` · ${live.ncpu} cores` : '';
  card(grid, {
    label: 'CPU',
    value: pct(live.cpu_pct),
    ratio: live.cpu_pct,
    detail: `load ${(live.load || []).map(v => v.toFixed(2)).join(' ')}${cores}`,
  });

  card(grid, {
    label: 'Memory',
    value: pct(live.mem_pct),
    ratio: live.mem_pct,
    detail: `${bytes(live.mem_used)} of ${bytes(live.mem_total)} · ${bytes(live.mem_avail)} available`,
  });

  const disk = live.disk || {};
  card(grid, {
    label: 'Disk',
    value: pct(disk.pct),
    ratio: disk.pct,
    detail: `${bytes(disk.used)} of ${bytes(disk.total)}${disk.path ? ` · ${disk.path}` : ''}`,
  });

  if (live.swap_total) {
    card(grid, {
      label: 'Swap',
      value: pct(live.swap_pct),
      ratio: live.swap_pct,
      detail: `${bytes(live.swap_used)} of ${bytes(live.swap_total)}`,
    });
  }

  const proc = live.proc || {};
  const bits = [];
  if (proc.threads) bits.push(`${proc.threads} threads`);
  if (proc.fds) bits.push(`${proc.fds} files`);
  if (proc.pid) bits.push(`pid ${proc.pid}`);
  card(grid, {
    label: 'WebConsole',
    value: bytes(proc.rss),
    detail: bits.join(' · ') || 'resident memory',
  });

  card(grid, {
    label: 'Uptime',
    value: duration(live.uptime_s),
    detail: 'since last boot',
  });

  container.appendChild(grid);

  const info = live.info || {};
  const facts = [];
  if (info.hostname) facts.push(info.hostname);
  if (info.cpu_model) facts.push(info.cpu_model);
  if (info.cpu_threads) {
    const physical = info.cpu_cores ? `${info.cpu_cores}c/` : '';
    facts.push(`${physical}${info.cpu_threads}t`);
  }
  if (info.kernel) facts.push(`Linux ${info.kernel}`);
  if (info.arch) facts.push(info.arch);
  if (facts.length) container.appendChild(el('p', 'srv-host', facts.join(' · ')));
}

function renderHistory(container, payload) {
  const rows = payload.series || [];
  if (!rows.length) {
    const every = payload.sample_interval_s || 60;
    container.appendChild(el('p', 'stat-empty',
      `No samples stored for this period yet. The server records one every ${every}s ` +
      'while it is running, so history starts from its last restart.'));
    return;
  }

  const cpu = seriesFrom(rows, [
    {key: 'avg', field: 'cpu_pct'},
    {key: 'peak', field: 'cpu_max'},
  ]);
  lineChart(container, cpu, {
    title: 'CPU over time',
    colorFor: key => slotColor(key === 'peak' ? 2 : 1),
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    formatValue: pct, formatTip: pct, axisMax: 100, summarize: peakOf(pct),
  });
  seriesTable(container, cpu, {
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    caption: 'CPU', formatCell: pct,
  });

  const mem = seriesFrom(rows, [
    {key: 'avg', field: 'mem_pct'},
    {key: 'peak', field: 'mem_max'},
  ]);
  lineChart(container, mem, {
    title: 'Memory over time',
    colorFor: key => slotColor(key === 'peak' ? 2 : 3),
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    formatValue: pct, formatTip: pct, axisMax: 100, summarize: peakOf(pct),
  });
  seriesTable(container, mem, {
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    caption: 'Memory', formatCell: pct,
  });

  const disk = seriesFrom(rows, [
    {key: 'avg', field: 'disk_pct'},
    {key: 'peak', field: 'disk_pct_max'},
  ]);
  if (disk.series.some(s => s.peak > 0)) {
    lineChart(container, disk, {
      title: 'Disk over time',
      colorFor: key => slotColor(key === 'peak' ? 2 : 4),
      labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
      formatValue: pct, formatTip: pct, axisMax: 100, summarize: peakOf(pct),
    });
    seriesTable(container, disk, {
      labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
      caption: 'Disk', formatCell: pct,
    });
  }

  // Load is charted unpinned: it has no ceiling, and the number that matters
  // is its size relative to the core count rather than to 100.
  const load = seriesFrom(rows, [
    {key: '1', field: 'load1'},
    {key: '5', field: 'load5'},
    {key: '15', field: 'load15'},
  ]);
  lineChart(container, load, {
    title: 'Load average over time',
    colorFor: key => slotColor({1: 1, 5: 3, 15: 4}[key] || 8),
    labelFor: key => `${key} min`,
    formatValue: loadFmt, formatTip: loadFmt,
    summarize: peakOf(loadFmt),
  });
  seriesTable(container, load, {
    labelFor: key => `${key} min`, caption: 'Load average', formatCell: loadFmt,
  });

  const proc = seriesFrom(rows, [
    {key: 'avg', field: 'proc_rss'},
    {key: 'peak', field: 'proc_rss_max'},
  ]);
  if (proc.series.some(s => s.peak > 0)) {
    lineChart(container, proc, {
      title: 'WebConsole memory over time',
      colorFor: key => slotColor(key === 'peak' ? 2 : 5),
      labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
      formatValue: bytes, formatTip: bytes, summarize: peakOf(bytes),
    });
    seriesTable(container, proc, {
      labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
      caption: 'WebConsole memory', formatCell: bytes,
    });
  }

  const samples = rows.reduce((sum, row) => sum + (row.samples || 0), 0);
  container.appendChild(el('p', 'srv-note',
    `${exact(samples)} samples across ${rows.length} periods · ` +
    `kept for ${payload.retention_days} days`));
}

/** One transport's charts: the same four the host below gets.
 *
 *  Deliberately the same `lineChart`/`seriesFrom` pair rather than a lighter
 *  variant. These graphs are read against the host's own, and two chart
 *  builders would eventually disagree about axis scaling or how a gap is
 *  drawn -- at which point comparing them silently stops being valid.
 *
 *  What is left out on purpose: the per-metric `seriesTable` the host gets.
 *  Four transports times four tables is a page nobody reads, and the current
 *  figures are already in the summary table above.
 */
export function renderTransportHistory(container, rows) {
  if (!rows || !rows.length) {
    container.appendChild(el('p', 'stat-empty',
      'No samples stored for this period. A transport is sampled only while '
      + 'its tunnel is connected.'));
    return;
  }

  const cpu = seriesFrom(rows, [
    {key: 'avg', field: 'cpu_pct'},
    {key: 'peak', field: 'cpu_max'},
  ]);
  lineChart(container, cpu, {
    title: 'CPU over time',
    colorFor: key => slotColor(key === 'peak' ? 2 : 1),
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    formatValue: pct, formatTip: pct, axisMax: 100, summarize: peakOf(pct),
  });

  const mem = seriesFrom(rows, [
    {key: 'avg', field: 'mem_pct'},
    {key: 'peak', field: 'mem_max'},
  ]);
  lineChart(container, mem, {
    title: 'Memory over time',
    colorFor: key => slotColor(key === 'peak' ? 2 : 3),
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    formatValue: pct, formatTip: pct, axisMax: 100, summarize: peakOf(pct),
  });

  const disk = seriesFrom(rows, [
    {key: 'avg', field: 'disk_pct'},
    {key: 'peak', field: 'disk_pct_max'},
  ]);
  lineChart(container, disk, {
    title: 'Disk over time',
    colorFor: key => slotColor(key === 'peak' ? 2 : 4),
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    formatValue: pct, formatTip: pct, axisMax: 100, summarize: peakOf(pct),
  });

  const load = seriesFrom(rows, [
    {key: '1', field: 'load1'},
    {key: '5', field: 'load5'},
    {key: '15', field: 'load15'},
  ]);
  lineChart(container, load, {
    title: 'Load average over time',
    colorFor: key => slotColor({1: 1, 5: 3, 15: 4}[key] || 8),
    labelFor: key => `${key} min`,
    // No axisMax: load is not a percentage and a transport with more cores
    // than this host can legitimately sit above 1.
    formatValue: loadFmt, formatTip: loadFmt, summarize: peakOf(loadFmt),
  });
}

export function renderServer(container, {live, history}) {
  container.replaceChildren();
  if (live && live.available === false) {
    container.appendChild(el('p', 'stat-empty',
      'Host statistics are unavailable: this server could not read /proc. ' +
      'That reading is Linux-only.'));
    return;
  }
  if (live) renderLive(container, live);
  if (history) renderHistory(container, history);
}
