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

  // Cached by `rows` array identity -- `rows` is fetched once by index.html
  // and reused by reference for the rest of the page's life, but this
  // index gets rebuilt (a full group+sort of the entire history, not just
  // the current season) on every week/season/resize redraw without this.
  // As history grows toward hundreds of thousands of rows, that turns a
  // cheap redraw into an increasingly expensive full-dataset rebuild whose
  // result never actually changes for the page's lifetime.
  let cachedRows = null;
  let cachedIndex = null;

  function buildSeasonTeamIndex(rows) {
    if (rows === cachedRows) return cachedIndex;
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
    cachedRows = rows;
    cachedIndex = index;
    return index;
  }

  // Plain "Wk N" for every point -- no special-cased "Final" label. The
  // charts truncate to whatever week is selected, so the last visible
  // point isn't necessarily the season's actual end; the standings
  // dropdown already carries the "End of Regular Season" / "End of Bowl
  // Season" / "Latest" labeling for that.
  function weekLabels(n) {
    return Array.from({ length: n }, (_, i) => `Wk ${i + 1}`);
  }

  function renderTeamTrend(rows, season, upToAsOf) {
    const card = document.getElementById('team-trend-card');
    const bySeasonTeam = buildSeasonTeamIndex(rows);
    const teams = Object.keys(bySeasonTeam[season] || {}).sort();
    if (!teams.length) return;
    card.hidden = false;

    const select = document.getElementById('team-select');
    const priorSelection = select.dataset.season === String(season) ? select.value : null;
    select.replaceChildren(...teams.map(t => new Option(t, t)));
    select.dataset.season = String(season);

    const seasonRows = rows.filter(r => r.season === season && r.as_of === upToAsOf);
    const top = seasonRows.sort((a, b) => b.power_score - a.power_score)[0];
    select.value = priorSelection && teams.includes(priorSelection) ? priorSelection : (top ? top.team : teams[0]);

    function draw() {
      const team = select.value;
      // Truncated to the currently selected week -- this season's line
      // stops there rather than running through the full season, so the
      // chart matches whatever the standings table is showing. Last
      // season's comparison line stays complete; it's already history.
      const curr = (bySeasonTeam[season]?.[team] || []).filter(r => r.as_of <= upToAsOf);
      const prev = bySeasonTeam[season - 1]?.[team] || [];
      const n = Math.max(curr.length, prev.length);
      const xLabels = weekLabels(n);
      const series = [{
        name: String(season),
        color: 'var(--series-1)',
        points: Array.from({ length: n }, (_, i) => curr[i] ? curr[i].power_score : null),
      }];
      if (prev.length) {
        series.push({
          name: String(season - 1),
          color: 'var(--series-2)',
          points: Array.from({ length: n }, (_, i) => prev[i] ? prev[i].power_score : null),
        });
      }
      renderLineChart(document.getElementById('team-chart'), { series, xLabels, valueFmt: v => v.toFixed(1) });
      renderTableView(document.getElementById('team-chart-table'), xLabels, series, v => v.toFixed(1));
    }

    select.onchange = draw;
    draw();
    currentTeamDraw = draw;
  }

  function renderConferenceTrend(rows, season, upToAsOf) {
    const card = document.getElementById('conf-trend-card');
    const seasonRows = rows.filter(r => r.season === season);
    const conferences = [...new Set(seasonRows.map(r => r.conference).filter(Boolean))].sort();
    if (!conferences.length) return;
    card.hidden = false;

    const select = document.getElementById('conf-select');
    const priorSelection = select.dataset.season === String(season) ? select.value : null;
    select.replaceChildren(...conferences.map(c => new Option(c, c)));
    select.dataset.season = String(season);

    const topRow = seasonRows.filter(r => r.as_of === upToAsOf).sort((a, b) => b.power_score - a.power_score)[0];
    select.value = priorSelection && conferences.includes(priorSelection) ? priorSelection : (topRow ? topRow.conference : conferences[0]);

    function draw() {
      const conf = select.value;
      // Same truncation as the team chart -- only weeks through the
      // selected one.
      const asOfs = [...new Set(seasonRows.map(r => r.as_of))].sort().filter(a => a <= upToAsOf);
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

    select.onchange = draw;
    draw();
    currentConfDraw = draw;
  }

  function debounce(fn, ms = 150) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  // renderTeamTrend/renderConferenceTrend now run on every week change, not
  // just once per season -- attaching a resize listener inside them (the
  // old approach) would stack up a new stale listener on every click, each
  // redrawing whatever week/team was current when it was attached. One
  // listener here, always calling whichever `draw` closure is current.
  let currentTeamDraw = null;
  let currentConfDraw = null;
  window.addEventListener('resize', debounce(() => {
    currentTeamDraw?.();
    currentConfDraw?.();
  }));

  window.renderTrends = function (rows, season, upToAsOf) {
    renderTeamTrend(rows, season, upToAsOf);
    renderConferenceTrend(rows, season, upToAsOf);
  };
})();
