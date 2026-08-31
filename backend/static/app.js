/* Market Scanner front end. No build step, no framework - the whole app is
   small enough that the DOM work stays readable and loads instantly. */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  status: null,
  scan: null,
  sessionId: null,
  sessions: [],
  pendingTrade: null,
};

/* ---------- helpers ---------- */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.message)) || res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

const fmt = {
  money(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const sign = v < 0 ? '-' : '';
    return sign + '$' + Math.abs(v).toLocaleString('en-US',
      { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  signed(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString('en-US',
      { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  pct(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(digits) + '%';
  },
  num(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return Number(v).toLocaleString('en-US',
      { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  time(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined,
      { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  },
  date(iso) {
    if (!iso) return '—';
    const d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : ''));
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  },
  ago(iso) {
    if (!iso) return '';
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (Number.isNaN(mins)) return '';
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
  },
};

/** Escape text before it goes anywhere near innerHTML. Headlines and notes
 *  come from external feeds, so nothing untrusted is ever interpolated raw. */
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function cls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : 'dim'; }

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6500 : 3800);
}

function convictionColor(v) {
  if (v >= 70) return 'var(--up-bright)';
  if (v >= 45) return 'var(--warn)';
  return 'var(--neutral)';
}

function convictionCell(v) {
  return `<div class="conv">
    <div class="conv-bar"><div class="conv-fill" style="width:${Math.max(v, 2)}%;background:${convictionColor(v)}"></div></div>
    <span class="conv-num" style="color:${convictionColor(v)}">${v}</span>
  </div>`;
}

/* ---------- navigation ---------- */
$$('nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    $$('nav button').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    $$('.view').forEach((v) => v.classList.remove('active'));
    $(`#view-${btn.dataset.view}`).classList.add('active');
    if (btn.dataset.view === 'paper') loadSessions();
    if (btn.dataset.view === 'history') loadSaved();
  });
});

/* ---------- status ---------- */
async function loadStatus() {
  try {
    const s = await api('/api/status');
    state.status = s;
    const dot = $('#statusChip .dot');
    dot.className = 'dot' + (s.provider === 'demo' || s.degraded ? ' sim' : s.realtime ? '' : ' delayed');
    const quality = s.provider === 'demo' ? 'SIMULATED DATA'
      : s.degraded ? 'SIMULATED FALLBACK'
      : s.realtime ? 'real-time' : 'delayed';
    $('#statusText').textContent = `${s.provider} · ${quality}`;
    $('#statusChip').title = s.data_note;
    if (s.provider === 'demo' || s.degraded) {
      const note = $('#scanNote');
      note.hidden = false;
      note.textContent = s.data_note;
    }
  } catch (err) {
    $('#statusText').textContent = 'offline';
    $('#statusChip .dot').className = 'dot sim';
  }
}

/* ---------- dashboard ---------- */
async function loadDashboard() {
  try {
    const brief = await api('/api/market/brief');
    $('#briefTime').textContent = fmt.ago(brief.generated_at);
    $('#narrative').textContent = brief.narrative || 'No summary available.';

    $('#indexTiles').innerHTML = brief.indices.map((q) => `
      <div class="tile">
        <div class="sym">${esc(q.symbol)}</div>
        <div class="px">${fmt.num(q.last)}</div>
        <div class="chg ${cls(q.change_pct)}">${fmt.pct(q.change_pct)} ${q.change !== null ? `(${fmt.signed(q.change)})` : ''}</div>
      </div>`).join('');

    $('#headlines').innerHTML = brief.headlines.length ? brief.headlines.map((h) => `
      <div class="headline">
        ${h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener noreferrer">${esc(h.headline)}</a>`
                : `<span>${esc(h.headline)}</span>`}
        <div class="meta">
          <span class="badge ${h.tone}">${h.tone}</span>
          <span class="badge ${h.impact}">${h.impact} impact</span>
          <span>${esc(h.source)}</span>
          <span>${fmt.ago(h.published)}</span>
        </div>
      </div>`).join('')
      : '<div class="empty">No headlines available from this provider.</div>';

    $('#catalysts').innerHTML = brief.catalysts.length ? brief.catalysts.map((c) => `
      <div class="cat-item">
        <div class="cat-date">${fmt.date(c.date)} ${esc(c.time_et)}</div>
        <div>
          <div><span class="badge ${c.importance}">${c.importance}</span> ${esc(c.title)}</div>
          <div class="cat-note">${esc(c.note)}</div>
        </div>
      </div>`).join('')
      : '<div class="empty">No scheduled catalysts in the window.</div>';
  } catch (err) {
    $('#narrative').textContent = `Could not load market brief: ${err.message}`;
  }
}

/* ---------- scanner ---------- */
$('#runScan').addEventListener('click', runScan);

async function runScan(save = false) {
  const custom = $('#scanCustom').value.trim();
  const preset = custom || $('#scanPreset').value;
  const params = new URLSearchParams({
    preset,
    min_dte: $('#scanMinDte').value,
    max_dte: $('#scanMaxDte').value,
    min_conviction: $('#scanMinConv').value,
    limit: '60',
    save: save ? 'true' : 'false',
  });
  $('#scanBody').innerHTML = `<tr><td colspan="14"><div class="loading"><div class="spinner"></div>Scanning ${esc(preset)}…</div></td></tr>`;
  try {
    const result = await api(`/api/scan?${params}`);
    state.scan = result;
    renderScan(result);
    // A scan can be the first thing that discovers the provider is down, so
    // re-read status to keep the header chip truthful.
    if (result.degraded && !state.status?.degraded) loadStatus();
    if (save) toast(`Scan saved (#${result.saved_scan_id})`, 'success');
  } catch (err) {
    $('#scanBody').innerHTML = `<tr><td colspan="14"><div class="empty">Scan failed: ${esc(err.message)}</div></td></tr>`;
    toast(err.message, 'error');
  }
}

$('#saveScan').addEventListener('click', () => runScan(true));

function renderScan(r) {
  $('#scanMeta').textContent =
    `${r.ideas.length} ideas · ${r.universe.length} symbols · ${r.min_dte}-${r.max_dte} DTE · ${r.elapsed_seconds}s · ${fmt.ago(r.generated_at)}`;

  if (r.degraded) {
    const note = $('#scanNote');
    note.hidden = false;
    note.textContent = 'Some symbols fell back to simulated data because the live provider did not return them. Rows built on simulated prices are not tradeable signals.';
  }

  if (!r.ideas.length) {
    $('#scanBody').innerHTML = '<tr><td colspan="14"><div class="empty">No setups cleared the filters. Try a wider universe, a longer DTE window, or a lower conviction floor.</div></td></tr>';
    return;
  }

  $('#scanBody').innerHTML = r.ideas.map((idea, i) => `
    <tr class="clickable" data-idx="${i}">
      <td><strong>${esc(idea.symbol)}</strong><div class="tiny faint mono">${fmt.num(idea.underlying_price)}</div></td>
      <td>${esc(idea.label)}<div class="tiny faint">${esc(idea.expiration)}</div></td>
      <td><span class="badge ${idea.direction}">${idea.direction}</span></td>
      <td>${convictionCell(idea.conviction)}</td>
      <td class="num mono">${idea.dte}</td>
      <td class="num mono ${idea.net_cost > 0 ? '' : 'up'}">${idea.net_cost > 0 ? fmt.money(idea.net_cost) : fmt.money(Math.abs(idea.net_cost)) + ' cr'}</td>
      <td class="num mono up">${idea.max_profit === null ? 'unbounded'
        : idea.risk_reward === null ? `<span title="Theoretical maximum: requires the underlying to go to zero">${fmt.money(idea.max_profit, 0)}<span class="faint">*</span></span>`
        : fmt.money(idea.max_profit, 0)}</td>
      <td class="num mono down">${fmt.money(idea.max_loss, 0)}</td>
      <td class="num mono">${idea.risk_reward === null ? '—' : idea.risk_reward.toFixed(2)}</td>
      <td class="num mono">${idea.prob_profit === null ? '—' : (idea.prob_profit * 100).toFixed(0) + '%'}</td>
      <td class="num mono ${cls(idea.ev_model)}">${fmt.signed(idea.ev_model, 0)}</td>
      <td><span class="badge ${idea.iv_regime}">${idea.iv_regime}</span></td>
      <td class="tiny dim">${esc((idea.regime || '').replace('_', ' '))}</td>
      <td class="num"><button class="btn sm primary" data-trade="${i}">Trade</button></td>
    </tr>`).join('');

  $$('#scanBody tr.clickable').forEach((row) => {
    row.addEventListener('click', (e) => {
      if (e.target.dataset.trade !== undefined) return;
      toggleDetail(row, r.ideas[Number(row.dataset.idx)]);
    });
  });
  $$('#scanBody [data-trade]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openTradeDialog(r.ideas[Number(btn.dataset.trade)]);
    });
  });
}

function toggleDetail(row, idea) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains('detail-row')) { next.remove(); return; }
  $$('#scanBody tr.detail-row').forEach((el) => el.remove());

  const tr = document.createElement('tr');
  tr.className = 'detail-row';
  const td = document.createElement('td');
  td.colSpan = 14;
  td.innerHTML = detailHtml(idea);
  tr.appendChild(td);
  row.after(tr);
  drawPayoff(td.querySelector('svg'), idea);
}

function detailHtml(idea) {
  const factors = (idea.factors || [])
    .slice()
    .sort((a, b) => Math.abs(b.score * b.weight) - Math.abs(a.score * a.weight))
    .map((f) => {
      const pct = Math.min(Math.abs(f.score), 1) * 50;
      const left = f.score >= 0 ? 50 : 50 - pct;
      const color = f.score >= 0 ? 'var(--up)' : 'var(--down)';
      return `<div class="factor">
        <div class="fname">${esc(f.name.replace(/_/g, ' '))}</div>
        <div class="fbar"><i style="left:${left}%;width:${pct}%;background:${color}"></i></div>
        <div class="fdetail">${esc(f.detail)}</div>
      </div>`;
    }).join('');

  const legs = (idea.legs || []).map((l) => `
    <tr>
      <td><span class="badge ${l.action === 'buy' ? 'long' : 'short'}">${l.action}</span></td>
      <td class="mono">${fmt.num(l.strike)} ${esc(l.kind)}</td>
      <td class="mono">${esc(l.expiration)}</td>
      <td class="num mono">${fmt.money(l.price)}</td>
      <td class="num mono faint">${l.bid === null ? '—' : fmt.num(l.bid)} / ${l.ask === null ? '—' : fmt.num(l.ask)}</td>
      <td class="num mono faint">${l.delta === null ? '—' : l.delta.toFixed(3)}</td>
      <td class="num mono faint">${l.open_interest === null ? '—' : Number(l.open_interest).toLocaleString()}</td>
    </tr>`).join('');

  const warnings = (idea.warnings || []).map((w) => `<div class="warn-box">${esc(w)}</div>`).join('');
  const sentiment = idea.news_sentiment === null || idea.news_sentiment === undefined ? null : idea.news_sentiment;

  return `<div class="detail">
    <div>
      <h3>Why this trade</h3>
      <p class="prose">${esc(idea.rationale)}</p>
      <h3 style="margin-top:14px">Risk</h3>
      <p class="prose dim">${esc(idea.risk_note)}</p>
      <h3 style="margin-top:14px">Exit plan</h3>
      <p class="prose dim">${esc(idea.exit_plan)}</p>
      ${warnings}
    </div>
    <div>
      <h3>Expiry payoff</h3>
      <svg class="chart"></svg>
      <div class="row tiny faint" style="margin-top:6px">
        <span>Breakeven: <span class="mono">${(idea.breakevens || []).map((b) => fmt.num(b)).join(', ') || '—'}</span></span>
        <span>Expected move: <span class="mono">±${fmt.num(idea.expected_move)}</span></span>
        <span>P&amp;L at one-sigma move: <span class="mono ${cls(idea.reward_at_expected_move)}">${fmt.signed(idea.reward_at_expected_move, 0)}</span></span>
      </div>
    </div>

    <div class="full">
      <h3>Contracts</h3>
      <table class="legs">
        <thead><tr><th>Action</th><th>Strike</th><th>Expiry</th><th class="num">Fill</th><th class="num">Bid / Ask</th><th class="num">Delta</th><th class="num">OI</th></tr></thead>
        <tbody>${legs}</tbody>
      </table>
      <div class="row tiny faint" style="margin-top:8px">
        <span>Net delta <span class="mono">${fmt.num(idea.net_delta, 1)}</span></span>
        <span>Net theta <span class="mono">${fmt.signed(idea.net_theta)}/day</span></span>
        <span>Net vega <span class="mono">${fmt.signed(idea.net_vega)}</span></span>
        <span>${esc(idea.liquidity)}</span>
      </div>
    </div>

    <div class="full">
      <h3>Conviction breakdown <span class="faint" style="text-transform:none;letter-spacing:0">— bias ${idea.bias >= 0 ? '+' : ''}${(idea.bias || 0).toFixed(2)}, ${Math.round((idea.agreement || 0) * 100)}% factor agreement, ${Math.round((idea.quality || 0) * 100)}% liquidity quality${sentiment !== null ? `, news tone ${sentiment >= 0 ? '+' : ''}${sentiment.toFixed(2)}` : ''}</span></h3>
      ${factors}
      <div class="row tiny faint" style="margin-top:10px">
        <span>Probability of profit (risk-neutral): <span class="mono">${idea.prob_profit === null ? '—' : (idea.prob_profit * 100).toFixed(1) + '%'}</span></span>
        <span>EV at market pricing: <span class="mono ${cls(idea.ev_risk_neutral)}">${fmt.signed(idea.ev_risk_neutral)}</span></span>
        <span>EV if the model's read is right: <span class="mono ${cls(idea.ev_model)}">${fmt.signed(idea.ev_model)}</span></span>
      </div>
    </div>
  </div>`;
}

/* ---------- payoff chart ---------- */
function drawPayoff(svg, idea) {
  if (!svg) return;
  const legs = idea.legs || [];
  const spot = idea.underlying_price;
  const lo = spot * 0.88;
  const hi = spot * 1.12;
  const points = [];
  for (let i = 0; i <= 90; i++) {
    const price = lo + (hi - lo) * (i / 90);
    let pnl = 0;
    for (const leg of legs) {
      const intrinsic = leg.kind === 'call'
        ? Math.max(price - leg.strike, 0)
        : Math.max(leg.strike - price, 0);
      pnl += (leg.action === 'buy' ? 1 : -1) * (intrinsic - leg.price) * leg.quantity * 100;
    }
    points.push({ price, pnl });
  }
  drawLine(svg, points.map((p) => p.price), points.map((p) => p.pnl), {
    zeroLine: true, marker: spot, colorBySign: true,
  });
}

/** Minimal SVG line chart. Kept hand-rolled so the page has zero dependencies
 *  and renders instantly even on a slow connection. */
function drawLine(svg, xs, ys, opts = {}) {
  const w = svg.clientWidth || 560;
  const h = svg.clientHeight || 190;
  const pad = { l: 52, r: 12, t: 12, b: 22 };
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const padY = (yMax - yMin) * 0.1;
  yMin -= padY; yMax += padY;

  const X = (v) => pad.l + (v - xMin) / (xMax - xMin || 1) * (w - pad.l - pad.r);
  const Y = (v) => h - pad.b - (v - yMin) / (yMax - yMin || 1) * (h - pad.t - pad.b);

  const parts = [];
  // horizontal gridlines + y labels
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * (i / 4);
    const y = Y(v);
    parts.push(`<line x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}" stroke="#262c3d" stroke-width="1"/>`);
    parts.push(`<text x="${pad.l - 6}" y="${y + 3.5}" fill="#5c6478" font-size="9.5" text-anchor="end" font-family="ui-monospace,monospace">${Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)}</text>`);
  }
  // x labels
  for (let i = 0; i <= 4; i++) {
    const v = xMin + (xMax - xMin) * (i / 4);
    parts.push(`<text x="${X(v)}" y="${h - 6}" fill="#5c6478" font-size="9.5" text-anchor="middle" font-family="ui-monospace,monospace">${xMax > 3000 ? new Date(v).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : v.toFixed(0)}</text>`);
  }
  if (opts.zeroLine && yMin < 0 && yMax > 0) {
    parts.push(`<line x1="${pad.l}" y1="${Y(0)}" x2="${w - pad.r}" y2="${Y(0)}" stroke="#5c6478" stroke-width="1" stroke-dasharray="3,3"/>`);
  }
  if (opts.marker !== undefined && opts.marker >= xMin && opts.marker <= xMax) {
    parts.push(`<line x1="${X(opts.marker)}" y1="${pad.t}" x2="${X(opts.marker)}" y2="${h - pad.b}" stroke="#4c8dff" stroke-width="1" stroke-dasharray="2,3" opacity="0.7"/>`);
  }

  if (opts.colorBySign) {
    // Split the path where it crosses zero so profit and loss render separately.
    let run = [];
    let sign = ys[0] >= 0;
    const flush = () => {
      if (run.length > 1) {
        parts.push(`<polyline fill="none" stroke="${sign ? '#2fd486' : '#ff5c60'}" stroke-width="2" points="${run.join(' ')}"/>`);
      }
      run = [];
    };
    for (let i = 0; i < xs.length; i++) {
      const s = ys[i] >= 0;
      if (s !== sign) { flush(); sign = s; }
      run.push(`${X(xs[i])},${Y(ys[i])}`);
    }
    flush();
  } else {
    const last = ys[ys.length - 1], first = ys[0];
    const color = last >= first ? '#2fd486' : '#ff5c60';
    const pts = xs.map((x, i) => `${X(x)},${Y(ys[i])}`).join(' ');
    parts.push(`<polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>`);
  }

  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = parts.join('');
}

/* ---------- equities ---------- */
$('#runEquities').addEventListener('click', async () => {
  $('#eqBody').innerHTML = '<tr><td colspan="15"><div class="loading"><div class="spinner"></div>Ranking…</div></td></tr>';
  try {
    const r = await api(`/api/equities?preset=${encodeURIComponent($('#eqPreset').value)}&limit=60`);
    $('#eqMeta').textContent = `${r.equities.length} names · ${fmt.ago(r.generated_at)}`;
    if (!r.equities.length) {
      $('#eqBody').innerHTML = '<tr><td colspan="15"><div class="empty">Nothing scored above zero in this universe.</div></td></tr>';
      return;
    }
    $('#eqBody').innerHTML = r.equities.map((e, i) => `
      <tr class="clickable" data-eq="${i}">
        <td><strong>${esc(e.symbol)}</strong></td>
        <td><span class="badge ${e.kind}">${e.kind}</span></td>
        <td><span class="badge ${e.direction}">${e.direction}</span></td>
        <td class="num">${convictionCell(e.score)}</td>
        <td><span class="mono" style="color:${e.trend_grade === 'A' ? 'var(--up-bright)' : e.trend_grade === 'B' ? 'var(--warn)' : 'var(--text-dim)'}">${e.trend_grade}</span></td>
        <td class="num mono">${fmt.num(e.price)}</td>
        <td class="num mono ${cls(e.change_pct)}">${fmt.pct(e.change_pct)}</td>
        <td class="num mono">${fmt.num(e.entry)}</td>
        <td class="num mono down">${fmt.num(e.stop)}</td>
        <td class="num mono up">${fmt.num(e.target)}</td>
        <td class="num mono">${fmt.num(e.risk_reward, 1)}</td>
        <td class="num mono faint">${fmt.num(e.atr_pct, 1)}</td>
        <td class="num mono faint">${e.rsi === null ? '—' : e.rsi.toFixed(0)}</td>
        <td class="num mono faint">${e.sharpe_60d === null ? '—' : e.sharpe_60d.toFixed(2)}</td>
        <td class="num"><button class="btn sm" data-eqtrade="${i}">Buy</button></td>
      </tr>`).join('');

    $$('#eqBody tr.clickable').forEach((row) => {
      row.addEventListener('click', (ev) => {
        if (ev.target.dataset.eqtrade !== undefined) return;
        const e = r.equities[Number(row.dataset.eq)];
        const next = row.nextElementSibling;
        if (next && next.classList.contains('detail-row')) { next.remove(); return; }
        $$('#eqBody tr.detail-row').forEach((el) => el.remove());
        const tr = document.createElement('tr');
        tr.className = 'detail-row';
        tr.innerHTML = `<td colspan="15"><div class="detail">
          <div class="full">
            <h3>Read</h3>
            <p class="prose">${esc(e.rationale)}</p>
            ${(e.reasons || []).map((x) => `<div class="tiny dim">• ${esc(x)}</div>`).join('')}
            <div class="row tiny faint" style="margin-top:10px">
              <span>Horizon ${esc(e.horizon)}</span>
              <span>60-day max drawdown ${fmt.pct(e.max_drawdown_60d)}</span>
              <span>Avg dollar volume ${e.avg_dollar_volume ? '$' + (e.avg_dollar_volume / 1e6).toFixed(0) + 'M' : '—'}</span>
              <span>Rel. strength vs SPY ${e.rel_strength === null ? '—' : fmt.pct(e.rel_strength)}</span>
            </div>
          </div></div></td>`;
        row.after(tr);
      });
    });

    $$('#eqBody [data-eqtrade]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const e = r.equities[Number(btn.dataset.eqtrade)];
        openTradeDialog({
          symbol: e.symbol,
          label: e.direction === 'short' ? 'Short stock' : 'Long stock',
          equity: true,
          direction: e.direction,
          legs: [{ symbol: e.symbol, action: e.direction === 'short' ? 'sell' : 'buy', quantity: 1 }],
          net_cost: e.price * 100,
          underlying_price: e.price,
        });
      });
    });
  } catch (err) {
    $('#eqBody').innerHTML = `<tr><td colspan="15"><div class="empty">Failed: ${esc(err.message)}</div></td></tr>`;
  }
});

/* ---------- paper trading ---------- */
const sessionDialog = $('#sessionDialog');
$('#newSessionBtn').addEventListener('click', () => {
  $('#dlgCash').value = state.status?.default_cash ?? 25000;
  $('#dlgName').value = '';
  $('#dlgNotes').value = '';
  sessionDialog.showModal();
});
$('#dlgCancel').addEventListener('click', () => sessionDialog.close());
$('#dlgCreate').addEventListener('click', async () => {
  const cash = Number($('#dlgCash').value);
  if (!(cash > 0)) { toast('Starting cash must be greater than zero.', 'error'); return; }
  try {
    const s = await api('/api/paper/sessions', {
      method: 'POST',
      body: JSON.stringify({
        name: $('#dlgName').value || `Session ${new Date().toLocaleDateString()}`,
        starting_cash: cash,
        notes: $('#dlgNotes').value,
      }),
    });
    sessionDialog.close();
    state.sessionId = s.id;
    await loadSessions();
    toast(`Session "${s.name}" created with ${fmt.money(s.starting_cash, 0)}`, 'success');
  } catch (err) { toast(err.message, 'error'); }
});

async function loadSessions() {
  try {
    state.sessions = await api('/api/paper/sessions');
  } catch { state.sessions = []; }

  const sel = $('#sessionSelect');
  if (!state.sessions.length) {
    sel.innerHTML = '<option value="">No sessions yet — create one to start</option>';
    $('#paperStats').innerHTML = '';
    $('#positionsBody').innerHTML = '<tr><td colspan="11"><div class="empty">Create a paper session to begin trading.</div></td></tr>';
    $('#ordersBody').innerHTML = '';
    $('#perfStats').innerHTML = '<div class="empty">No trades yet.</div>';
    $('#equityChart').innerHTML = '';
    return;
  }
  if (!state.sessionId || !state.sessions.some((s) => s.id === state.sessionId)) {
    state.sessionId = state.sessions[0].id;
  }
  sel.innerHTML = state.sessions.map((s) => `
    <option value="${s.id}" ${s.id === state.sessionId ? 'selected' : ''}>
      ${esc(s.name)} — ${fmt.money(s.starting_cash, 0)} start · ${s.order_count} fills · ${s.status}
    </option>`).join('');
  await loadPortfolio();
}

$('#sessionSelect').addEventListener('change', (e) => {
  state.sessionId = Number(e.target.value);
  loadPortfolio();
});
$('#refreshPaper').addEventListener('click', () => loadPortfolio(true));

$('#settleBtn').addEventListener('click', async () => {
  if (!state.sessionId) return;
  try {
    const r = await api(`/api/paper/sessions/${state.sessionId}/settle`, { method: 'POST' });
    toast(r.settled ? `Settled ${r.settled} expired contract(s), cash effect ${fmt.signed(r.cash_effect)}`
                    : 'Nothing to settle.', 'success');
    loadPortfolio();
  } catch (err) { toast(err.message, 'error'); }
});

$('#closeSessionBtn').addEventListener('click', async () => {
  if (!state.sessionId) return;
  try {
    await api(`/api/paper/sessions/${state.sessionId}/close`, { method: 'POST' });
    toast('Session closed. Its history stays saved.', 'success');
    loadSessions();
  } catch (err) { toast(err.message, 'error'); }
});

$('#deleteSessionBtn').addEventListener('click', async () => {
  if (!state.sessionId) return;
  const s = state.sessions.find((x) => x.id === state.sessionId);
  if (!confirm(`Delete "${s?.name}" and all of its orders permanently? This cannot be undone.`)) return;
  try {
    await api(`/api/paper/sessions/${state.sessionId}`, { method: 'DELETE' });
    state.sessionId = null;
    toast('Session deleted.', 'success');
    loadSessions();
  } catch (err) { toast(err.message, 'error'); }
});

async function loadPortfolio(snapshot = false) {
  if (!state.sessionId) return;
  try {
    const [p, orders, perf, curve] = await Promise.all([
      api(`/api/paper/sessions/${state.sessionId}?snapshot=${snapshot}`),
      api(`/api/paper/sessions/${state.sessionId}/orders`),
      api(`/api/paper/sessions/${state.sessionId}/performance`),
      api(`/api/paper/sessions/${state.sessionId}/curve`),
    ]);
    renderPortfolio(p, orders, perf, curve);
  } catch (err) { toast(err.message, 'error'); }
}

function renderPortfolio(p, orders, perf, curve) {
  $('#paperStats').innerHTML = `
    <div class="stat"><div class="k">Total equity</div><div class="v">${fmt.money(p.total_equity)}</div></div>
    <div class="stat"><div class="k">Total P&amp;L</div><div class="v ${cls(p.total_pnl)}">${fmt.signed(p.total_pnl)} <span style="font-size:12px">${fmt.pct(p.total_pnl_pct)}</span></div></div>
    <div class="stat"><div class="k">Cash / buying power</div><div class="v">${fmt.money(p.cash, 0)}<span style="font-size:12px" class="dim"> / ${fmt.money(p.buying_power, 0)}</span></div></div>
    <div class="stat"><div class="k">Realized / unrealized</div><div class="v"><span class="${cls(p.realized_pnl)}">${fmt.signed(p.realized_pnl, 0)}</span> <span class="dim" style="font-size:12px">/</span> <span class="${cls(p.unrealized_pnl)}" style="font-size:14px">${fmt.signed(p.unrealized_pnl, 0)}</span></div></div>`;

  // Only offer "close spread" where the group actually has more than one leg.
  const groupSizes = {};
  p.positions.forEach((x) => {
    if (x.group_id) groupSizes[x.group_id] = (groupSizes[x.group_id] || 0) + 1;
  });

  $('#positionsBody').innerHTML = p.positions.length ? p.positions.map((pos) => `
    <tr>
      <td class="mono">${esc(pos.symbol)}${pos.underlying && pos.asset_type === 'option'
        ? `<div class="tiny faint">${esc(pos.underlying)} ${fmt.num(pos.strike)} ${esc(pos.kind)}</div>` : ''}</td>
      <td class="tiny dim">${esc(pos.asset_type)}</td>
      <td class="num mono ${pos.quantity > 0 ? 'up' : 'down'}">${pos.quantity > 0 ? '+' : ''}${fmt.num(pos.quantity, 0)}</td>
      <td class="num mono">${fmt.num(pos.avg_price)}</td>
      <td class="num mono">${fmt.num(pos.mark)}${pos.stale_mark ? '<span class="tiny faint" title="No live quote; showing entry price"> ⚠</span>' : ''}</td>
      <td class="num mono">${fmt.money(pos.market_value)}</td>
      <td class="num mono ${cls(pos.unrealized_pnl)}">${fmt.signed(pos.unrealized_pnl)}</td>
      <td class="num mono ${cls(pos.unrealized_pnl)}">${pos.unrealized_pct === null ? '—' : fmt.pct(pos.unrealized_pct)}</td>
      <td class="num mono ${pos.dte !== null && pos.dte <= 0 ? 'down' : ''}">${pos.dte === null ? '—' : pos.dte}</td>
      <td class="tiny dim">${esc((pos.strategy || '').replace(/_/g, ' '))}</td>
      <td class="num">
        <button class="btn sm" data-close-pos="${esc(pos.symbol)}">Close</button>
        ${pos.group_id && groupSizes[pos.group_id] > 1 ? `<button class="btn sm" data-close-grp="${esc(pos.group_id)}" title="Close every leg of this spread">Close spread</button>` : ''}
      </td>
    </tr>`).join('')
    : '<tr><td colspan="11"><div class="empty">No open positions. Send an idea here from the Options Scanner.</div></td></tr>';

  $$('#positionsBody [data-close-pos]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        const r = await api(`/api/paper/sessions/${state.sessionId}/close-position`, {
          method: 'POST', body: JSON.stringify({ symbol: btn.dataset.closePos }),
        });
        toast(`Closed. Realized ${fmt.signed(r.realized_pnl)}`, 'success');
        loadPortfolio();
      } catch (err) { toast(err.message, 'error'); }
    });
  });
  $$('#positionsBody [data-close-grp]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        const r = await api(`/api/paper/sessions/${state.sessionId}/close-group`, {
          method: 'POST', body: JSON.stringify({ group_id: btn.dataset.closeGrp }),
        });
        toast(`Spread closed. Realized ${fmt.signed(r.realized_pnl)}`, 'success');
        loadPortfolio();
      } catch (err) { toast(err.message, 'error'); }
    });
  });

  $('#ordersBody').innerHTML = orders.length ? orders.map((o) => `
    <tr>
      <td class="tiny dim nowrap">${fmt.time(o.filled_at || o.created_at)}</td>
      <td class="mono tiny">${esc(o.symbol)}</td>
      <td><span class="badge ${o.side === 'buy' ? 'long' : 'short'}">${o.side}</span></td>
      <td class="num mono">${fmt.num(o.quantity, 0)}</td>
      <td class="num mono">${fmt.num(o.fill_price)}</td>
      <td class="num mono faint">${fmt.money(o.commission)}</td>
      <td class="num mono ${cls(o.realized_pnl)}">${o.realized_pnl ? fmt.signed(o.realized_pnl) : '—'}</td>
      <td class="tiny dim">${esc((o.strategy || '').replace(/_/g, ' '))}</td>
      <td class="tiny faint">${esc(o.note || '')}</td>
    </tr>`).join('')
    : '<tr><td colspan="9"><div class="empty">No orders yet.</div></td></tr>';

  $('#perfStats').innerHTML = perf.trades ? `
    <div class="grid cols-4" style="margin-bottom:12px">
      <div class="stat"><div class="k">Round trips</div><div class="v">${perf.trades}</div></div>
      <div class="stat"><div class="k">Win rate</div><div class="v">${perf.win_rate}%</div></div>
      <div class="stat"><div class="k">Profit factor</div><div class="v">${perf.profit_factor ?? '—'}</div></div>
      <div class="stat"><div class="k">Net closed P&amp;L</div><div class="v ${cls(perf.net_pnl)}">${fmt.signed(perf.net_pnl, 0)}</div></div>
    </div>
    <div class="row tiny faint">
      <span>Avg win ${fmt.signed(perf.avg_win)}</span>
      <span>Avg loss ${fmt.signed(perf.avg_loss)}</span>
      <span>Best ${fmt.signed(perf.best)}</span>
      <span>Worst ${fmt.signed(perf.worst)}</span>
    </div>`
    : '<div class="empty">No closed trades yet. Performance appears once you close a position.</div>';

  if (curve.length > 1) {
    drawLine($('#equityChart'),
      curve.map((c) => new Date(c.taken_at).getTime()),
      curve.map((c) => c.total_equity));
  } else {
    $('#equityChart').innerHTML = '<text x="50%" y="50%" fill="#5c6478" font-size="12" text-anchor="middle">Refresh marks to record equity points over time</text>';
  }
}

/* ---------- trade dialog ---------- */
const tradeDialog = $('#tradeDialog');
$('#tradeCancel').addEventListener('click', () => tradeDialog.close());

async function openTradeDialog(idea) {
  if (!state.sessions.length) {
    try { state.sessions = await api('/api/paper/sessions'); } catch { state.sessions = []; }
  }
  const active = state.sessions.filter((s) => s.status === 'active');
  if (!active.length) {
    toast('Create a paper trading session first (Paper Trading tab).', 'error');
    return;
  }
  state.pendingTrade = idea;
  $('#tradeTitle').textContent = `${idea.symbol} · ${idea.label}`;
  const cost = idea.net_cost;
  $('#tradeSummary').innerHTML = idea.equity
    ? `Buys at the live offer. 1 "contract" here means 100 shares.`
    : `${cost > 0 ? 'Net debit' : 'Net credit'} ${fmt.money(Math.abs(cost))} per spread · max loss ${fmt.money(idea.max_loss)} · ${(idea.legs || []).length} leg(s) · expires ${esc(idea.expiration || '')}.
       Fills cross part of the bid/ask spread, so the price may differ slightly from the quote above.`;
  $('#tradeSession').innerHTML = active.map((s) =>
    `<option value="${s.id}" ${s.id === state.sessionId ? 'selected' : ''}>${esc(s.name)} — ${fmt.money(s.cash, 0)} cash</option>`).join('');
  $('#tradeQty').value = 1;
  tradeDialog.showModal();
}

$('#tradeConfirm').addEventListener('click', async () => {
  const idea = state.pendingTrade;
  if (!idea) return;
  const sessionId = Number($('#tradeSession').value);
  const qty = Math.max(1, Number($('#tradeQty').value) || 1);
  const legs = (idea.legs || []).map((l) => ({
    symbol: l.symbol,
    side: l.action,
    quantity: (l.quantity || 1) * qty * (idea.equity ? 100 : 1),
    asset_type: idea.equity ? 'equity' : 'option',
  }));
  try {
    const r = await api(`/api/paper/sessions/${sessionId}/orders`, {
      method: 'POST',
      body: JSON.stringify({ legs, strategy: idea.strategy || 'manual', note: idea.label }),
    });
    tradeDialog.close();
    state.sessionId = sessionId;
    toast(`Filled: ${r.net_debit > 0 ? 'debit' : 'credit'} ${fmt.money(Math.abs(r.net_debit))}` +
          (r.margin_reserved ? `, ${fmt.money(r.margin_reserved, 0)} margin reserved` : ''), 'success');
    await loadSessions();
  } catch (err) { toast(err.message, 'error'); }
});

/* ---------- backtest ---------- */
$('#runBacktest').addEventListener('click', async () => {
  const body = {
    symbols: $('#btSymbols').value.split(/[,\s]+/).filter(Boolean),
    mode: $('#btMode').value,
    lookback_days: Number($('#btLookback').value),
    hold_days: Number($('#btHold').value),
    dte: Number($('#btDte').value),
    min_conviction: Number($('#btConv').value),
    starting_cash: Number($('#btCash').value),
    profit_target_pct: Number($('#btTarget').value),
    stop_loss_pct: Number($('#btStop').value),
    iv_premium: Number($('#btIvPrem').value),
    spread_pct: Number($('#btSpread').value) / 100,
    risk_per_trade_pct: Number($('#btRisk').value),
  };
  if (!body.symbols.length) { toast('Enter at least one symbol.', 'error'); return; }

  $('#btStats').innerHTML = '<div class="loading" style="grid-column:1/-1"><div class="spinner"></div>Replaying history…</div>';
  $('#btCurveCard').hidden = true;
  $('#btTradesCard').hidden = true;
  try {
    const r = await api('/api/backtest', { method: 'POST', body: JSON.stringify(body) });
    renderBacktest(r);
  } catch (err) {
    $('#btStats').innerHTML = `<div class="empty" style="grid-column:1/-1">Backtest failed: ${esc(err.message)}</div>`;
  }
});

function renderBacktest(r) {
  const s = r.stats;
  $('#btMethod').innerHTML = `<div class="info-box"><strong>Method:</strong> ${esc(r.method)}
    ${(r.warnings || []).map((w) => `<div style="margin-top:5px">⚠ ${esc(w)}</div>`).join('')}</div>`;

  if (!s.trades) {
    $('#btStats').innerHTML = `<div class="empty" style="grid-column:1/-1">${esc(s.note || 'No trades.')}</div>`;
    return;
  }

  $('#btStats').innerHTML = `
    <div class="stat"><div class="k">Net P&amp;L</div><div class="v ${cls(s.net_pnl)}">${fmt.signed(s.net_pnl, 0)}</div></div>
    <div class="stat"><div class="k">Return</div><div class="v ${cls(s.return_pct)}">${fmt.pct(s.return_pct)}</div></div>
    <div class="stat"><div class="k">Trades / win rate</div><div class="v">${s.trades} <span style="font-size:13px" class="dim">/ ${s.win_rate}%</span></div></div>
    <div class="stat"><div class="k">Profit factor</div><div class="v">${s.profit_factor ?? '—'}</div></div>
    <div class="stat"><div class="k">Expectancy / trade</div><div class="v ${cls(s.expectancy)}">${fmt.signed(s.expectancy, 0)}</div></div>
    <div class="stat"><div class="k">Max drawdown</div><div class="v down">${fmt.pct(s.max_drawdown_pct)}</div></div>
    <div class="stat"><div class="k">Sharpe</div><div class="v">${s.sharpe ?? '—'}</div></div>
    <div class="stat"><div class="k">Avg hold / commissions</div><div class="v">${s.avg_hold_days}d <span style="font-size:13px" class="dim">/ ${fmt.money(s.total_commission, 0)}</span></div></div>`;

  $('#btCurveCard').hidden = false;
  drawLine($('#btChart'),
    r.equity_curve.map((c) => new Date(c.date + 'T00:00:00').getTime()),
    r.equity_curve.map((c) => c.equity));

  $('#btTradesCard').hidden = false;
  $('#btBody').innerHTML = r.trades.slice(0, 300).map((t) => `
    <tr>
      <td class="tiny dim nowrap">${esc(t.entry_date)}</td>
      <td class="tiny dim nowrap">${esc(t.exit_date)}</td>
      <td class="mono">${esc(t.symbol)}</td>
      <td class="tiny">${esc(t.strategy.replace(/_/g, ' '))}</td>
      <td class="num mono">${t.conviction}</td>
      <td class="num mono">${fmt.money(t.entry_cost, 0)}</td>
      <td class="num mono ${cls(t.pnl)}">${fmt.signed(t.pnl, 0)}</td>
      <td class="num mono ${cls(t.pnl)}">${fmt.pct(t.pnl_pct, 1)}</td>
      <td class="num mono ${cls(t.underlying_move_pct)}">${fmt.pct(t.underlying_move_pct, 1)}</td>
      <td class="tiny dim">${esc(t.exit_reason)}</td>
    </tr>`).join('');
}

/* ---------- saved ---------- */
async function loadSaved() {
  try {
    const [scans, tests] = await Promise.all([
      api('/api/scan/saved'), api('/api/backtest/saved'),
    ]);
    $('#savedScansBody').innerHTML = scans.length ? scans.map((s) => `
      <tr><td class="tiny dim nowrap">${fmt.time(s.created_at)}</td>
      <td>${esc(s.label || '')}</td>
      <td class="num"><button class="btn sm" data-scan="${s.id}">View</button>
      <button class="btn sm danger" data-delscan="${s.id}">Delete</button></td></tr>`).join('')
      : '<tr><td colspan="3"><div class="empty">No saved scans. Use "Save result" on the scanner.</div></td></tr>';

    $('#savedBtBody').innerHTML = tests.length ? tests.map((s) => `
      <tr><td class="tiny dim nowrap">${fmt.time(s.created_at)}</td>
      <td>${esc(s.label || '')}</td>
      <td class="num"><button class="btn sm" data-bt="${s.id}">View</button>
      <button class="btn sm danger" data-delbt="${s.id}">Delete</button></td></tr>`).join('')
      : '<tr><td colspan="3"><div class="empty">No saved backtests yet.</div></td></tr>';

    $$('[data-scan]').forEach((b) => b.addEventListener('click', async () => {
      const r = await api(`/api/scan/saved/${b.dataset.scan}`);
      $('#savedDetailCard').hidden = false;
      $('#savedDetailTitle').textContent = `Scan from ${fmt.time(r.created_at)} — ${r.label || ''}`;
      $('#savedDetail').innerHTML = `<p class="prose dim">${esc(r.result.narrative || '')}</p>
        <div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Strategy</th><th>Conviction</th>
        <th class="num">Cost</th><th class="num">POP</th><th class="num">EV model</th></tr></thead><tbody>
        ${(r.result.ideas || []).map((i) => `<tr><td><strong>${esc(i.symbol)}</strong></td><td>${esc(i.label)}</td>
        <td>${convictionCell(i.conviction)}</td><td class="num mono">${fmt.money(i.net_cost)}</td>
        <td class="num mono">${i.prob_profit === null ? '—' : (i.prob_profit * 100).toFixed(0) + '%'}</td>
        <td class="num mono ${cls(i.ev_model)}">${fmt.signed(i.ev_model, 0)}</td></tr>`).join('')}
        </tbody></table></div>`;
      $('#savedDetailCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }));

    $$('[data-bt]').forEach((b) => b.addEventListener('click', async () => {
      const r = await api(`/api/backtest/saved/${b.dataset.bt}`);
      $('#savedDetailCard').hidden = false;
      $('#savedDetailTitle').textContent = `Backtest from ${fmt.time(r.created_at)} — ${r.label || ''}`;
      const s = r.result.stats || {};
      $('#savedDetail').innerHTML = `<div class="info-box">${esc(r.result.method || '')}</div>
        <div class="grid cols-4">
        <div class="stat"><div class="k">Net P&amp;L</div><div class="v ${cls(s.net_pnl)}">${fmt.signed(s.net_pnl, 0)}</div></div>
        <div class="stat"><div class="k">Return</div><div class="v ${cls(s.return_pct)}">${fmt.pct(s.return_pct)}</div></div>
        <div class="stat"><div class="k">Trades</div><div class="v">${s.trades ?? 0}</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v">${s.win_rate ?? '—'}%</div></div></div>`;
      $('#savedDetailCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }));

    $$('[data-delscan]').forEach((b) => b.addEventListener('click', async () => {
      await api(`/api/scan/saved/${b.dataset.delscan}`, { method: 'DELETE' });
      loadSaved();
    }));
    $$('[data-delbt]').forEach((b) => b.addEventListener('click', async () => {
      await api(`/api/backtest/saved/${b.dataset.delbt}`, { method: 'DELETE' });
      loadSaved();
    }));
  } catch (err) { toast(err.message, 'error'); }
}

/* ---------- boot ---------- */
(async function init() {
  await loadStatus();
  await loadDashboard();
  loadSessions();
  // Refresh the dashboard periodically; the server caches upstream calls so
  // this costs one request, not one per symbol.
  setInterval(loadDashboard, 60000);
})();
