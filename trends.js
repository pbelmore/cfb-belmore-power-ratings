// Trend charts for index.html -- hand-rolled SVG line charts, no library.
// Renders a per-team rating trend (this season vs. last season) and a
// per-conference average-rating trend, both fed from the same
// ratings_history.json rows already fetched by index.html.

(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  }

  function niceTicks(min, max, count) {
    // Power scores are 0-100. A degenerate (all-equal) range -- e.g. every
    // team still at 0.0 in week 1 -- needs a small buffer in scale with
    // that, not a fixed +-1 (which reads as basically zero on this axis).
    if (min === max) { min -= 5; max += 5; }
    const span = max - min;
    const rawStep = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
    const norm = rawStep / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const niceMin = Math.floor(min / step) * step;
    const niceMax = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = niceMin; v <= niceMax + step / 2; v += step) ticks.push(v);
    return ticks;
  }

  // series: [{ name, color, points: [value|null, ...] }] aligned to xLabels
  function renderLineChart(container, { series, xLabels, valueFmt }) {
    container.replaceChildren();
    const width = Math.max(280, container.clientWidth || 600);
    const height = 260;
    const padL = 46, padR = 16, padT = 16, padB = 26;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;

    const allValues = series.flatMap(s => s.points.filter(v => v != null));
    if (!allValues.length) {
      container.innerHTML = '<p class="status">Not enough data yet.</p>';
      return;
    }
    const ticks = niceTicks(Math.min(...allValues, 0), Math.max(...allValues), 4);
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];

    const n = xLabels.length;
    const xAt = i => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const yAt = v => padT + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;

    const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}` });

    ticks.forEach(t => {
      const y = yAt(t);
      svg.appendChild(svgEl('line', { x1: padL, x2: width - padR, y1: y, y2: y, stroke: 'var(--grid)', 'stroke-width': 1 }));
      const label = svgEl('text', { x: padL - 8, y: y + 3, 'text-anchor': 'end', class: 'chart-axis' });
      label.textContent = valueFmt(t);
      svg.appendChild(label);
    });

    svg.appendChild(svgEl('line', { x1: padL, x2: width - padR, y1: padT + plotH, y2: padT + plotH, stroke: 'var(--axis-line)', 'stroke-width': 1 }));

    const labelEvery = Math.max(1, Math.ceil(n / 6));
    xLabels.forEach((lab, i) => {
      if (i % labelEvery !== 0 && i !== n - 1) return;
      const t = svgEl('text', { x: xAt(i), y: height - 6, 'text-anchor': 'middle', class: 'chart-axis' });
      t.textContent = lab;
      svg.appendChild(t);
    });

    series.forEach(s => {
      let d = '';
      s.points.forEach((v, i) => {
        if (v == null) return;
        d += (d ? 'L' : 'M') + xAt(i) + ',' + yAt(v) + ' ';
      });
      svg.appendChild(svgEl('path', { d, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
      for (let i = n - 1; i >= 0; i--) {
        if (s.points[i] != null) {
          svg.appendChild(svgEl('circle', { cx: xAt(i), cy: yAt(s.points[i]), r: 4, fill: s.color, stroke: 'var(--bg)', 'stroke-width': 2 }));
          break;
        }
      }
    });

    const crosshair = svgEl('line', { x1: padL, x2: padL, y1: padT, y2: padT + plotH, stroke: 'var(--axis-line)', 'stroke-width': 1, opacity: 0 });
    svg.appendChild(crosshair);
    const hoverDots = series.map(s => {
      const dot = svgEl('circle', { r: 5, fill: s.color, stroke: 'var(--bg)', 'stroke-width': 2, opacity: 0 });
      svg.appendChild(dot);
      return dot;
    });

    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    wrap.appendChild(svg);

    const tooltip = document.createElement('div');
    tooltip.className = 'chart-tooltip';
    tooltip.hidden = true;
    wrap.appendChild(tooltip);

    function showAt(clientX) {
      const rect = svg.getBoundingClientRect();
      const scaleX = width / rect.width;
      const px = (clientX - rect.left) * scaleX;
      let idx = Math.round(((px - padL) / plotW) * (n - 1));
      idx = Math.max(0, Math.min(n - 1, idx));

      crosshair.setAttribute('x1', xAt(idx));
      crosshair.setAttribute('x2', xAt(idx));
      crosshair.setAttribute('opacity', 1);

      tooltip.replaceChildren();
      const head = document.createElement('div');
      head.className = 'chart-tooltip-head';
      head.textContent = xLabels[idx];
      tooltip.appendChild(head);

      series.forEach((s, si) => {
        const v = s.points[idx];
        hoverDots[si].setAttribute('opacity', v == null ? 0 : 1);
        if (v != null) {
          hoverDots[si].setAttribute('cx', xAt(idx));
          hoverDots[si].setAttribute('cy', yAt(v));
        } else {
          return;
        }
        const row = document.createElement('div');
        row.className = 'chart-tooltip-row';
        const key = document.createElement('span');
        key.className = 'chart-tooltip-key';
        key.style.background = s.color;
        const val = document.createElement('strong');
        val.textContent = valueFmt(v);
        const name = document.createElement('span');
        name.className = 'chart-tooltip-name';
        name.textContent = s.name;
        row.append(key, val, name);
        tooltip.appendChild(row);
      });

      tooltip.hidden = false;
      const relLeft = (xAt(idx) / width) * rect.width;
      tooltip.style.left = Math.min(Math.max(relLeft - 60, 0), Math.max(rect.width - 140, 0)) + 'px';
    }

    svg.addEventListener('pointermove', e => showAt(e.clientX));
    svg.addEventListener('pointerleave', () => {
      crosshair.setAttribute('opacity', 0);
      hoverDots.forEach(d => d.setAttribute('opacity', 0));
      tooltip.hidden = true;
    });

    container.appendChild(wrap);

    if (series.length > 1) {
      const legend = document.createElement('div');
      legend.className = 'chart-legend';
      series.forEach(s => {
        const item = document.createElement('span');
        item.className = 'chart-legend-item';
        const key = document.createElement('span');
        key.className = 'chart-legend-key';
        key.style.background = s.color;
        const label = document.createElement('span');
        label.textContent = s.name;
        item.append(key, label);
        legend.appendChild(item);
      });
      container.appendChild(legend);
    }
  }

  // The accessibility twin of every chart: same data, as a plain table.
  function renderTableView(container, xLabels, series, valueFmt) {
    container.replaceChildren();
    const details = document.createElement('details');
    details.className = 'chart-table-toggle';
    const summary = document.createElement('summary');
    summary.textContent = 'Show data table';
    details.appendChild(summary);

    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    headRow.appendChild(document.createElement('th'));
    series.forEach(s => {
      const th = document.createElement('th');
      th.textContent = s.name;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    xLabels.forEach((lab, i) => {
      const tr = document.createElement('tr');
      const th = document.createElement('td');
      th.textContent = lab;
      tr.appendChild(th);
      series.forEach(s => {
        const td = document.createElement('td');
        const v = s.points[i];
        td.textContent = v == null ? '—' : valueFmt(v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    details.appendChild(table);
    container.appendChild(details);
  }

  function buildSeasonTeamIndex(rows) {
    const index = {};
    for (const r of rows) {
      (index[r.season] ??= {});
      (index[r.season][r.team] ??= []).push(r);
    }
    for (const season in index) {
      for (const team in index[season]) {
        index[season][team].sort((a, b) => (a.as_of < b.as_of ? -1 : a.as_of > b.as_of ? 1 : 0));
      }
    }
    return index;
  }

  function weekLabels(n) {
    return Array.from({ length: n }, (_, i) => (i === n - 1 ? 'Final' : `Wk ${i + 1}`));
  }

  function renderTeamTrend(rows, latestSeason) {
    const card = document.getElementById('team-trend-card');
    const bySeasonTeam = buildSeasonTeamIndex(rows);
    const teams = Object.keys(bySeasonTeam[latestSeason] || {}).sort();
    if (!teams.length) return;
    card.hidden = false;

    const select = document.getElementById('team-select');
    select.replaceChildren(...teams.map(t => new Option(t, t)));

    const seasonRows = rows.filter(r => r.season === latestSeason);
    const latestAsOf = seasonRows.reduce((a, r) => (r.as_of > a ? r.as_of : a), seasonRows[0].as_of);
    const top = seasonRows.filter(r => r.as_of === latestAsOf).sort((a, b) => b.power_score - a.power_score)[0];
    select.value = top ? top.team : teams[0];

    function draw() {
      const team = select.value;
      const curr = bySeasonTeam[latestSeason]?.[team] || [];
      const prev = bySeasonTeam[latestSeason - 1]?.[team] || [];
      const n = Math.max(curr.length, prev.length);
      const xLabels = weekLabels(n);
      const series = [{
        name: String(latestSeason),
        color: 'var(--series-1)',
        points: Array.from({ length: n }, (_, i) => curr[i] ? curr[i].power_score : null),
      }];
      if (prev.length) {
        series.push({
          name: String(latestSeason - 1),
          color: 'var(--series-2)',
          points: Array.from({ length: n }, (_, i) => prev[i] ? prev[i].power_score : null),
        });
      }
      renderLineChart(document.getElementById('team-chart'), { series, xLabels, valueFmt: v => v.toFixed(1) });
      renderTableView(document.getElementById('team-chart-table'), xLabels, series, v => v.toFixed(1));
    }

    select.addEventListener('change', draw);
    draw();
    window.addEventListener('resize', debounce(draw));
  }

  function renderConferenceTrend(rows, latestSeason) {
    const card = document.getElementById('conf-trend-card');
    const seasonRows = rows.filter(r => r.season === latestSeason);
    const conferences = [...new Set(seasonRows.map(r => r.conference).filter(Boolean))].sort();
    if (!conferences.length) return;
    card.hidden = false;

    const select = document.getElementById('conf-select');
    select.replaceChildren(...conferences.map(c => new Option(c, c)));

    const asOfs = [...new Set(seasonRows.map(r => r.as_of))].sort();
    const latestAsOf = asOfs[asOfs.length - 1];
    const topRow = seasonRows.filter(r => r.as_of === latestAsOf).sort((a, b) => b.power_score - a.power_score)[0];
    select.value = topRow ? topRow.conference : conferences[0];

    function draw() {
      const conf = select.value;
      const points = asOfs.map(asOf => {
        const teamRows = seasonRows.filter(r => r.as_of === asOf && r.conference === conf);
        if (!teamRows.length) return null;
        return teamRows.reduce((sum, r) => sum + r.power_score, 0) / teamRows.length;
      });
      const xLabels = weekLabels(asOfs.length);
      const series = [{ name: conf, color: 'var(--series-1)', points }];
      renderLineChart(document.getElementById('conf-chart'), { series, xLabels, valueFmt: v => v.toFixed(1) });
      renderTableView(document.getElementById('conf-chart-table'), xLabels, series, v => v.toFixed(1));
    }

    select.addEventListener('change', draw);
    draw();
    window.addEventListener('resize', debounce(draw));
  }

  function debounce(fn, ms = 150) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  window.renderTrends = function (rows, latestSeason) {
    renderTeamTrend(rows, latestSeason);
    renderConferenceTrend(rows, latestSeason);
  };

  window.renderBlurb = async function () {
    const card = document.getElementById('blurb-card');
    try {
      const res = await fetch('data/weekly_blurb.txt', { cache: 'no-store' });
      if (!res.ok) throw new Error('not found');
      const text = await res.text();
      if (!text.trim()) throw new Error('empty');
      document.getElementById('blurb-text').textContent = text;
      card.hidden = false;
      document.getElementById('blurb-copy').addEventListener('click', async (e) => {
        await navigator.clipboard.writeText(text);
        const btn = e.currentTarget;
        const old = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = old; }, 1500);
      });
    } catch {
      card.hidden = true;
    }
  };
})();
