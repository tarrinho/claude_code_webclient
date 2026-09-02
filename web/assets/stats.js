// Statistics: usage over time, drawn as inline SVG.
//
// Hand-rolled rather than Chart.js. The CSP here is script-src 'self', so a CDN
// is out and the library would have to be vendored -- 201KB to draw two stacked
// area charts, against roughly 8KB here, on a console whose whole point is
// being usable over a phone connection. Chart.js also renders to <canvas>,
// which cannot be read by a screen reader and does not inherit the theme
// variables this page already defines; SVG does both.
//
// Colours come from the validated categorical palette (slots 1-3), stepped
// separately for each surface and checked against both: worst all-pairs CVD
// deltaE 9.2 light / 9.4 dark, normal-vision 24.0 / 20.9. On the light surface
// aqua sits below 3:1, which obligates the relief rule -- hence the always-on
// legend, the direct labels, and the table below every chart.

const NS = 'http://www.w3.org/2000/svg';

// Terminal dwarfs the website by three orders of magnitude on a real machine,
// so these are never stacked into one series.
const SOURCES = [
  {key: 'cli', label: 'Terminal', slot: 1},
  {key: 'anthropic', label: 'Anthropic API', slot: 2},
  {key: 'anthropic-compatible', label: 'Website', slot: 3},
];

function svg(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Compact a number for an axis or a label, e.g. 1_047_875_622 -> "1.0B". */
export function abbrev(n) {
  const v = Number(n) || 0;
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

/** Full-precision grouping, for the title attribute and the table. */
export function exact(n) {
  return (Number(n) || 0).toLocaleString();
}

/**
 * Turn the server's flat (bucket, key, value) rows into aligned series.
 *
 * Every series is filled across the union of buckets, so a source that was
 * silent for a day contributes a zero rather than a gap -- an unfilled gap
 * makes a line jump between non-adjacent points and reads as continuous
 * activity that never happened.
 */
export function toSeries(rows, keyField, valueOf, spine = null) {
  // The spine is the complete bucket axis for the window, including buckets no
  // row falls into. Without it the axis is built from the rows themselves, so a
  // bucket nobody wrote is not merely unfilled -- it is absent, and the chart,
  // which places points by index, renders its neighbours adjacent. That is the
  // same error this function already avoids between series, one dimension over:
  // filling a silent source with zeros is pointless if a silent *hour* has no
  // column to be zero in.
  //
  // Unioned rather than trusted outright, so a row outside the spine still
  // appears: a bucket holding data is evidence, and a spine that disagrees with
  // it is the thing to distrust.
  const seen = new Set(rows.map(r => r.bucket));
  const buckets = [...new Set([...(spine || []), ...seen])].sort();
  const byKey = new Map();
  for (const row of rows) {
    const key = row[keyField];
    if (!byKey.has(key)) byKey.set(key, new Map());
    const at = byKey.get(key);
    // The server already merges duplicates, but a caller that changes the
    // grouping must not silently lose rows here.
    at.set(row.bucket, (at.get(row.bucket) || 0) + valueOf(row));
  }
  const series = [...byKey.entries()].map(([key, at]) => ({
    key,
    values: buckets.map(b => at.get(b) || 0),
    total: [...at.values()].reduce((a, b) => a + b, 0),
  }));
  series.sort((a, b) => b.total - a.total);
  return {buckets, series};
}

/**
 * Split values into runs of consecutive real readings, dropping the holes.
 *
 * Returns [[index, value], ...] per run, so each run can be drawn as its own
 * polyline and a hole becomes a break rather than a point.
 *
 * A null is an interval where nothing was measured -- the sampler was not
 * running. Plotting it as a value would draw the machine at 0% CPU through
 * exactly the windows the console was down, and joining the line across it is
 * wrong the other way: the line would span the outage as though the readings
 * either side were consecutive, which is the gap this is meant to expose.
 *
 * Its own function because it is the whole of the decision and it is pure,
 * which is what lets it be tested without a chart around it.
 */
export function segments(values) {
  const runs = [];
  let run = [];
  (values || []).forEach((v, i) => {
    // NaN and Infinity are holes too. They reach here from a metric that was
    // stored but is not a number, and y() would place them off the canvas.
    if (v === null || v === undefined || !Number.isFinite(Number(v))) {
      if (run.length) runs.push(run);
      run = [];
      return;
    }
    run.push([i, Number(v)]);
  });
  if (run.length) runs.push(run);
  return runs;
}

/** Short axis label for a bucket key: "2026-08-29" -> "Aug 29". */
function bucketLabel(bucket) {
  const [date, hour] = bucket.split('T');
  const parts = date.split('-');
  if (parts.length === 2) return `${parts[0]}-${parts[1]}`;   // month bucket
  const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][Number(parts[1]) - 1] || '';
  const day = `${month} ${Number(parts[2])}`;
  if (hour === undefined) return day;
  // An hour bucket is "13"; a half-hour bucket is already "13:30". Appending
  // ":00" to the latter produced "Aug 30 13:30:00", which reads as a seconds
  // field on an axis that has no seconds.
  return hour.includes(':') ? `${day} ${hour}` : `${day} ${hour}:00`;
}

/**
 * A grouped line chart with a crosshair and a shared tooltip.
 *
 * Lines rather than a stack: these series are compared, not summed, and a
 * stacked band would make each one's magnitude depend on the ones below it.
 */
export function lineChart(container, {buckets, series}, {
  title, colorFor, labelFor,
  // Token counts are the default because they were the first caller, but the
  // Server page charts percentages and bytes, where abbrev() is actively
  // wrong: it renders 8.2e9 bytes as "8.2B", which reads as billion.
  formatValue = abbrev,
  formatTip = exact,
  // Pin the y axis when the scale is inherently bounded. Without this a box
  // idling at 3% CPU draws a line across the top of the chart, which is a
  // truthful shape and a completely misleading picture.
  axisMax = null,
  // A total is meaningless for a gauge: summing CPU percentages across
  // buckets produces a number with no unit. Gauge callers pass their own.
  summarize = null,
}) {
  const W = 720, H = 260;
  const pad = {top: 16, right: 16, bottom: 34, left: 52};
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const figure = el('figure', 'stat-figure');
  figure.appendChild(el('figcaption', 'stat-caption', title));

  // Null means "not measured", so it must not reach Math.max: a single null
  // read as 0 is harmless to the scale, but read as NaN it poisons it and the
  // whole chart renders blank.
  const measured = series.flatMap(s => s.values).filter(v => v !== null
    && v !== undefined && Number.isFinite(Number(v)));
  const max = axisMax || Math.max(1, ...measured);
  const x = i => buckets.length < 2
    ? pad.left + plotW / 2
    : pad.left + (i / (buckets.length - 1)) * plotW;
  const y = v => pad.top + plotH - (v / max) * plotH;

  const root = svg('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'stat-svg',
    role: 'img', 'aria-label': `${title}. ${series.length} series over ${buckets.length} points.`,
  });

  // Horizontal grid + y labels. Recessive on purpose: the data is the subject.
  for (let i = 0; i <= 4; i += 1) {
    const value = (max / 4) * i;
    const yy = y(value);
    root.appendChild(svg('line', {
      x1: pad.left, x2: W - pad.right, y1: yy, y2: yy, class: 'stat-grid',
    }));
    const label = svg('text', {x: pad.left - 8, y: yy + 4, class: 'stat-axis stat-axis-y'});
    label.textContent = formatValue(value);
    root.appendChild(label);
  }

  // X labels, thinned so they never collide.
  const step = Math.max(1, Math.ceil(buckets.length / 7));
  buckets.forEach((bucket, i) => {
    if (i % step && i !== buckets.length - 1) return;
    const label = svg('text', {x: x(i), y: H - 12, class: 'stat-axis stat-axis-x'});
    label.textContent = bucketLabel(bucket);
    root.appendChild(label);
  });

  series.forEach(entry => {
    // Split on nulls and draw one polyline per run of real readings, rather
    // than one line through everything. A null is an interval where nothing
    // was measured -- the sampler was not running -- and plotting it as a
    // value would draw the machine sitting at 0% CPU through exactly the
    // windows the console was down. Joining across it is just as wrong in the
    // other direction: the line would span the outage as though the readings
    // either side were consecutive, which is the gap this whole change exists
    // to stop the chart from hiding.
    segments(entry.values).forEach(segment => {
      root.appendChild(svg('polyline', {
        points: segment.map(([i, v]) => `${x(i)},${y(v)}`).join(' '),
        class: 'stat-line', stroke: colorFor(entry.key), fill: 'none',
      }));
      // A run of one has no line to draw, so it needs a dot or it is invisible.
      // This is not only the single-bucket case any more: an isolated reading
      // between two outages is a run of one in the middle of a wide chart, and
      // silently dropping it would under-report the machine having been up.
      if (segment.length === 1) {
        const [i, v] = segment[0];
        root.appendChild(svg('circle', {
          cx: x(i), cy: y(v), r: 4,
          fill: colorFor(entry.key), class: 'stat-dot',
        }));
      }
    });
  });

  // Crosshair + tooltip. An SVG chart is interactive by default; without this
  // the reader can see a shape but never a value.
  const crosshair = svg('line', {
    y1: pad.top, y2: pad.top + plotH, class: 'stat-crosshair', 'stroke-width': 1,
  });
  crosshair.style.display = 'none';
  root.appendChild(crosshair);

  const tip = el('div', 'stat-tip');
  tip.hidden = true;
  const wrap = el('div', 'stat-plot');
  wrap.appendChild(root);
  wrap.appendChild(tip);

  function nearest(clientX) {
    const box = root.getBoundingClientRect();
    if (!box.width) return 0;
    const local = ((clientX - box.left) / box.width) * W;
    let best = 0, bestDist = Infinity;
    buckets.forEach((_, i) => {
      const dist = Math.abs(x(i) - local);
      if (dist < bestDist) { bestDist = dist; best = i; }
    });
    return best;
  }

  function showAt(event) {
    if (!buckets.length) return;
    const i = nearest(event.clientX);
    crosshair.setAttribute('x1', x(i));
    crosshair.setAttribute('x2', x(i));
    crosshair.style.display = '';
    tip.replaceChildren();
    tip.appendChild(el('div', 'stat-tip-head', bucketLabel(buckets[i])));
    series.forEach(entry => {
      const row = el('div', 'stat-tip-row');
      const swatch = el('span', 'stat-swatch');
      swatch.style.background = colorFor(entry.key);
      row.appendChild(swatch);
      row.appendChild(el('span', 'stat-tip-name', labelFor(entry.key)));
      // A null bucket was never measured. formatTip is exact() by default,
      // which renders null as "0" -- so the tooltip would state a reading for
      // an interval that has none, and it is the one place the user goes to
      // check a specific moment.
      const value = entry.values[i];
      const shown = (value === null || value === undefined)
        ? 'no data' : formatTip(value);
      row.appendChild(el('span', 'stat-tip-value', shown));
      tip.appendChild(row);
    });
    tip.hidden = false;
    // Flip before the right edge so the tooltip never leaves the panel.
    const frac = x(i) / W;
    tip.style.left = `${Math.min(frac * 100, 72)}%`;
  }

  function hide() {
    crosshair.style.display = 'none';
    tip.hidden = true;
  }

  wrap.addEventListener('pointermove', showAt);
  wrap.addEventListener('pointerleave', hide);
  figure.appendChild(wrap);

  // Legend: always present for two or more series, so identity is never
  // carried by colour alone.
  if (series.length > 1) {
    const legend = el('div', 'stat-legend');
    series.forEach(entry => {
      const item = el('span', 'stat-legend-item');
      const swatch = el('span', 'stat-swatch');
      swatch.style.background = colorFor(entry.key);
      item.appendChild(swatch);
      const note = summarize ? summarize(entry) : abbrev(entry.total);
      item.appendChild(el('span', null, `${labelFor(entry.key)} · ${note}`));
      legend.appendChild(item);
    });
    figure.appendChild(legend);
  }

  container.appendChild(figure);
}

/** The table that backs every chart -- the relief rule, and the a11y fallback. */
export function seriesTable(container, {buckets, series},
                            {labelFor, caption, formatCell = exact}) {
  const box = document.createElement('details');
  box.className = 'stat-table-wrap';
  const summary = document.createElement('summary');
  summary.textContent = `${caption} as a table`;
  box.appendChild(summary);

  const table = el('table', 'stat-table');
  const head = el('tr');
  head.appendChild(el('th', null, 'Period'));
  series.forEach(s => head.appendChild(el('th', null, labelFor(s.key))));
  table.appendChild(el('thead')).appendChild(head);

  const body = el('tbody');
  buckets.forEach((bucket, i) => {
    const row = el('tr');
    row.appendChild(el('th', null, bucketLabel(bucket)));
    series.forEach(s => row.appendChild(el('td', null, formatCell(s.values[i]))));
    body.appendChild(row);
  });
  table.appendChild(body);
  box.appendChild(table);
  container.appendChild(box);
}

/** Read a palette slot for the current theme off the stylesheet. */
export function slotColor(slot) {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(`--series-${slot}`).trim();
  return value || '#8b949e';
}

export function renderStats(container, payload) {
  container.replaceChildren();
  const rows = payload.series || [];
  if (!rows.length) {
    container.appendChild(el('p', 'stat-empty',
      'No usage recorded in this period yet.'));
    return;
  }

  const known = new Map(SOURCES.map(s => [s.key, s]));
  const sourceLabel = key => known.get(key)?.label || key;
  const sourceColor = key => slotColor(known.get(key)?.slot ?? 8);

  // The spine is the full bucket axis for the window. An hour with no usage
  // genuinely is zero tokens, so these fill with zeros -- unlike the Server
  // page, where a missing bucket means nothing was measured and stays null.
  const spine = payload.spine || null;
  const tokens = toSeries(rows, 'provider',
    r => (r.input_tokens || 0) + (r.output_tokens || 0), spine);
  lineChart(container, tokens, {
    title: 'Tokens over time, by source',
    colorFor: sourceColor, labelFor: sourceLabel,
  });
  seriesTable(container, tokens,
    {labelFor: sourceLabel, caption: 'Tokens by source'});

  const requests = toSeries(rows, 'provider', r => r.requests || 0, spine);
  lineChart(container, requests, {
    title: 'Requests over time, by source',
    colorFor: sourceColor, labelFor: sourceLabel,
  });
  seriesTable(container, requests,
    {labelFor: sourceLabel, caption: 'Requests by source'});

  const modelRows = payload.models || [];
  if (modelRows.length) {
    const models = toSeries(modelRows, 'model',
      r => (r.input_tokens || 0) + (r.output_tokens || 0), spine);
    // Colour follows the entity by rank within this chart only; the series are
    // already sorted by total, so a range change cannot repaint a survivor
    // differently from how it was drawn a moment ago.
    const order = new Map(models.series.map((s, i) => [s.key, i + 1]));
    const modelColor = key => slotColor(Math.min(order.get(key) || 8, 8));
    lineChart(container, models, {
      title: 'Tokens over time, by model',
      colorFor: modelColor, labelFor: k => k,
    });
    seriesTable(container, models, {labelFor: k => k, caption: 'Tokens by model'});
  }
}
