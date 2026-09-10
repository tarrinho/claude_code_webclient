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

/** The most recent measured value, for metrics whose peak says nothing.
 *
 *  Disk fills over days rather than spiking, and load1 is already a kernel
 *  average, so "peak" on those two is either the same number as now or a
 *  one-bucket blip. Where the current reading is the interesting one, print
 *  that instead. Holes are skipped: the last thing measured, not the last
 *  bucket drawn.
 */
const lastOf = format => entry => {
  for (let i = entry.values.length - 1; i >= 0; i -= 1) {
    if (entry.values[i] !== null && entry.values[i] !== undefined) {
      return `now ${format(entry.values[i])}`;
    }
  }
  return 'no reading';
};

/**
 * One series per host, all on the same bucket axis.
 *
 * seriesFrom() builds several series out of one host's row list -- average
 * against peak. This builds one series per *host* out of one column, which is
 * what a merged chart needs: four machines' CPU on a single pair of axes
 * instead of four charts the eye has to compare by memory.
 *
 * `buckets` is the spine, and every host is read through it by bucket key
 * rather than by position. The API aligns the hosts before sending them, so
 * positions would usually work -- "usually" being the failure mode where a
 * host that reported one extra bucket silently shifts its whole line sideways.
 *
 * Missing and null readings stay null, for the reason seriesFrom keeps them:
 * a zero here draws a confident idle machine over the window nobody measured.
 */
export function hostSeries(buckets, hosts, field) {
  const series = hosts.map(host => {
    const byBucket = new Map((host.rows || []).map(row => [row.bucket, row]));
    const values = buckets.map(bucket => {
      const row = byBucket.get(bucket);
      if (!row) return null;
      const raw = row[field];
      if (raw === null || raw === undefined) return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n : null;
    });
    const real = values.filter(v => v !== null);
    return {
      key: host.key,
      values,
      measured: real.length,
      total: real.reduce((a, b) => a + b, 0),
      peak: real.length ? Math.max(...real) : 0,
      mean: real.length ? real.reduce((a, b) => a + b, 0) / real.length : 0,
    };
  });
  return {buckets, series};
}

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

/** This host's numbers as tables, under the merged charts.
 *
 *  The charts this used to draw moved into `renderAllHosts`: CPU, memory,
 *  disk and load are now one chart each with a line per machine, so drawing
 *  them again here would put the same measurement on the page twice under two
 *  different scales. What stays is the per-metric table -- exact figures,
 *  average and peak side by side, which is the thing a merged chart cannot
 *  show for four hosts at once -- plus WebConsole's own memory, which has no
 *  counterpart on a transport and so has nothing to merge with.
 */
export function renderHostHistory(container, payload) {
  container.replaceChildren();
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
  seriesTable(container, cpu, {
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    caption: 'CPU', formatCell: pct,
  });

  const mem = seriesFrom(rows, [
    {key: 'avg', field: 'mem_pct'},
    {key: 'peak', field: 'mem_max'},
  ]);
  seriesTable(container, mem, {
    labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
    caption: 'Memory', formatCell: pct,
  });

  const disk = seriesFrom(rows, [
    {key: 'avg', field: 'disk_pct'},
    {key: 'peak', field: 'disk_pct_max'},
  ]);
  if (disk.series.some(s => s.peak > 0)) {
    seriesTable(container, disk, {
      labelFor: key => (key === 'peak' ? 'Peak' : 'Average'),
      caption: 'Disk', formatCell: pct,
    });
  }

  const load = seriesFrom(rows, [
    {key: '1', field: 'load1'},
    {key: '5', field: 'load5'},
    {key: '15', field: 'load15'},
  ]);
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

/** Load per core, to two decimals. 1.00 is "as many runnable tasks as cores". */
export function perCore(v) {
  return (Number(v) || 0).toFixed(2);
}

/**
 * Four charts, one per metric, with a line per machine.
 *
 * The layout this replaces was a block of four charts per host, which meant
 * comparing two machines' CPU was a matter of remembering one picture while
 * looking at another -- and the y-axes were fitted per chart, so two lines of
 * the same height often meant different numbers. One chart per metric puts
 * every host on one scale, which is the comparison the page exists to make.
 *
 * Load is the exception that needed work: raw load1 is not comparable across
 * machines of different sizes, so this charts load divided by the host's own
 * core count. The division happens in SQL (`load_per_core`), because the core
 * count is per host and the average is per bucket.
 *
 * Average and peak are not drawn as two lines any more. Four hosts times two
 * lines is eight lines on one pair of axes, which is unreadable; the peak
 * survives as a number in the legend for the two metrics where a spike means
 * something (CPU and memory), and the other two print the latest reading.
 */
export function renderAllHosts(container, {history, transports}) {
  container.replaceChildren();
  const rows = (history && history.series) || [];

  // The local host leads, then the transports in the order the table above
  // lists them, so a colour means the same machine in every chart.
  const byHost = (history && history.transports) || {};
  const hosts = [{key: 'local', label: 'This console', rows}];
  for (const transport of transports || []) {
    hosts.push({
      key: transport.id,
      label: transport.name || transport.id,
      rows: byHost[transport.id] || [],
    });
  }

  // The union of every host's buckets, not the local host's alone. The API
  // aligns the transports onto the local series, so with local samples
  // present the two are the same list -- but a console restarted minutes ago
  // has no local history yet, and taking the spine from it would drop every
  // transport's chart on the grounds that *this* machine was not sampled.
  // Bucket keys are timestamps, so sorting them as strings is chronological.
  const buckets = [...new Set(hosts.flatMap(
    host => (host.rows || []).map(row => row.bucket)))].sort();
  if (!buckets.length) {
    const every = (history && history.sample_interval_s) || 60;
    container.appendChild(el('p', 'stat-empty',
      `No samples stored for this period yet. The server records one every ${every}s ` +
      'while it is running, so history starts from its last restart.'));
    return;
  }
  const labels = new Map(hosts.map(host => [host.key, host.label]));
  const colors = new Map(hosts.map((host, i) => [host.key, slotColor(i + 1)]));

  const chart = (field, options) => {
    const built = hostSeries(buckets, hosts, field);
    // A host with nothing measured is left out rather than drawn flat. An
    // empty line in the legend and a zero across the bottom is the shape of a
    // reading, and there was no reading -- the transport table above says
    // "--" for those, which is the honest form of the same fact.
    built.series = built.series.filter(entry => entry.measured > 0);
    if (!built.series.length) return;
    lineChart(container, built, {
      colorFor: key => colors.get(key) || slotColor(8),
      labelFor: key => labels.get(key) || key,
      ...options,
    });
  };

  chart('cpu_pct', {
    title: 'CPU', formatValue: pct, formatTip: pct,
    axisMax: 100, summarize: peakOf(pct),
  });
  chart('mem_pct', {
    title: 'Memory', formatValue: pct, formatTip: pct,
    axisMax: 100, summarize: peakOf(pct),
  });
  chart('disk_pct', {
    title: 'Disk', formatValue: pct, formatTip: pct,
    axisMax: 100, summarize: lastOf(pct),
  });
  // No axisMax: load has no ceiling. Per core it usually sits under 1, and
  // pinning the axis at 1 would flatten exactly the excursions worth seeing.
  chart('load_per_core', {
    title: 'Load per core', formatValue: perCore, formatTip: perCore,
    summarize: lastOf(perCore),
  });

  container.appendChild(el('p', 'srv-note',
    'One line per machine. Load is divided by each host’s own core '
    + 'count, so the hosts are comparable; a host whose core count is '
    + 'unknown is left out of that chart. A break in a line is an interval '
    + 'with no sample, not a reading of zero.'));
}

/** The live cards. History is no longer drawn here: the merged charts and the
 *  per-metric tables render into their own containers, above and below the
 *  transport table respectively, so each of the three can be placed on the
 *  page independently of the other two. */
export function renderServer(container, {live}) {
  container.replaceChildren();
  if (live && live.available === false) {
    container.appendChild(el('p', 'stat-empty',
      'Host statistics are unavailable: this server could not read /proc. ' +
      'That reading is Linux-only.'));
    return;
  }
  if (live) renderLive(container, live);
}
