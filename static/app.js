 /* =====================================================
   Poly-ARB Web · SBП-style app v3
   ===================================================== */
const state = {
  ws: null,
  arbs: [],          // все enriched arbs
  visible: [],       // после фильтров
  activeSlug: null,
  editing: null,
  hideSet: new Set(),
  seenSlugs: new Set(),
  freshSlugs: new Set(),
  phase: 'live',     // 'live' | 'prematch'
  audioCtx: null,
  soundEnabled: localStorage.getItem('poly_arb_sound') !== 'off',
  bkFilter: JSON.parse(localStorage.getItem('poly_arb_bks') || 'null'),
  config: { rate: 85, bank: 2500, pm_fee_pct: 0.04, safe_fork_threshold: 0.45 },
  dailyStats: null,
};

const pmBookCache = {};  // slug -> {best_ask, best_bid, ask_depth_usd}

const BK_LIST = [
  { id: '1win',    name: '1win',       speed: 0, color: 'onewin' },
  { id: 'liga',    name: 'Лига Ставок',speed: 0 },
  { id: 'betboom', name: 'BetBoom',    speed: 0, color: 'bb' },
  { id: 'duel',    name: 'Duel',       speed: 1 },
  { id: 'fonbet',  name: 'Fonbet',     speed: 1 },
  { id: 'olimpbet',name: 'Olimpbet',   speed: 1 },
  { id: 'pari',    name: 'PARI',       speed: 1 },
  { id: 'roobet',  name: 'Roobet',     speed: 1 },
  { id: 'stake',   name: 'Stake',      speed: 2 },
  { id: 'winline', name: 'Winline',    speed: 0 },
];
if (!state.bkFilter) state.bkFilter = Object.fromEntries(BK_LIST.map(b => [b.id, true]));

/* ============= SOUND ============= */
function ensureAudioCtx() {
  if (state.audioCtx) return state.audioCtx;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    state.audioCtx = new Ctx();
  } catch (e) {}
  return state.audioCtx;
}
function playArbChime() {
  if (!state.soundEnabled) return;
  const ctx = ensureAudioCtx(); if (!ctx) return;
  if (ctx.state === 'suspended') ctx.resume();
  const now = ctx.currentTime;
  // приятный трёхзвучный аккорд (C6-E6-G6)
  const notes = [
    { f: 1046.5, t: 0.00, dur: 0.32, gain: 0.14 },
    { f: 1318.5, t: 0.07, dur: 0.42, gain: 0.12 },
    { f: 1568.0, t: 0.14, dur: 0.55, gain: 0.10 },
  ];
  for (const n of notes) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = n.f;
    gain.gain.setValueAtTime(0, now + n.t);
    gain.gain.linearRampToValueAtTime(n.gain, now + n.t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + n.t + n.dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now + n.t);
    osc.stop(now + n.t + n.dur + 0.05);
  }
}
function toggleSound() {
  state.soundEnabled = !state.soundEnabled;
  localStorage.setItem('poly_arb_sound', state.soundEnabled ? 'on' : 'off');
  updateSoundBtn();
  if (state.soundEnabled) { ensureAudioCtx(); playArbChime(); }
}
function updateSoundBtn() {
  const btn = document.getElementById('st-sound'); if (!btn) return;
  btn.textContent = state.soundEnabled ? '🔊' : '🔇';
  btn.title = state.soundEnabled ? 'Звук включён' : 'Звук выключен';
  btn.style.opacity = state.soundEnabled ? '1' : '0.45';
}

/* ============= UTILS ============= */
function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function fmtMoney(n) {
  if (!isFinite(n)) n = 0;
  return Math.round(n).toLocaleString('ru-RU') + ' ₽';
}
function fmtOdds(n) { return (Number(n) || 0).toFixed(3); }
function fmtPct(n, sign = true) {
  const v = (Number(n) || 0) * 100;
  return (sign && v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function fmtAgo(sec) {
  if (!isFinite(sec) || sec < 0) return '—';
  sec = Math.floor(sec);
  if (sec < 60) return sec + 'с';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + 'м ' + s + 'с';
  const h = Math.floor(m / 60);
  return h + 'ч ' + (m % 60) + 'м';
}
function sportClass(s) { return (s || 'other').toLowerCase(); }
function toast(msg, err) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('err', !!err);
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 2400);
}

/* Separate from the per-deal bank in config.json; kept only in this browser. */
function updateBankLimit() {
  const input = document.getElementById('risk-total-bank');
  const output = document.getElementById('risk-limit');
  if (!input || !output) return;
  const bank = Number(input.value);
  const valid = input.value.trim() !== '' && Number.isFinite(bank) && bank >= 0;
  output.textContent = valid ? fmtMoney(bank * 0.4) : '—';
  try {
    if (valid) localStorage.setItem('poly_arb_total_bank', String(bank));
    else localStorage.removeItem('poly_arb_total_bank');
  } catch (e) {}
}

function setPanelStatus(connected) {
  const label = document.getElementById('feed-status');
  const dot = document.getElementById('feed-dot');
  if (label) label.textContent = connected ? 'Панель подключена' : 'Нет связи с панелью';
  if (dot) dot.classList.toggle('connected', connected);
}

/* ============= WS ============= */
function connectWS() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${location.host}/ws`);
  ws.onopen = () => setPanelStatus(true);
  ws.onclose = () => { setPanelStatus(false); setTimeout(connectWS, 1500); };
  ws.onerror = () => setPanelStatus(false);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'snapshot') {
        if (msg.config) state.config = { ...state.config, ...msg.config };
        const newArbs = msg.arbs || [];
        let hasNew = false;
        for (const a of newArbs) {
          if (!state.seenSlugs.has(a.slug)) {
            state.seenSlugs.add(a.slug);
            state.freshSlugs.add(a.slug);
            hasNew = true;
            setTimeout(() => state.freshSlugs.delete(a.slug), 2600);
          }
        }
        state.arbs = newArbs;
        if (state._primed && hasNew) playArbChime();
        state._primed = true;
        applyFilters();
        if (state.activeSlug) refreshDetailThrottled();
      }
    } catch (e) {}
  };
  state.ws = ws;
}

/* ============= FILTERS ============= */
function applyFilters() {
  const sports = new Set();
  if (document.getElementById('f-cs2').checked) sports.add('CS2');
  if (document.getElementById('f-dota2').checked) sports.add('DOTA2');
  if (document.getElementById('f-lol').checked) sports.add('LOL');
  if (document.getElementById('f-tennis').checked) sports.add('TENNIS');
  const minRoi = parseFloat(document.getElementById('f-minroi').value || '0') / 100;

  state.visible = state.arbs.filter(a => {
    if ((a.phase || 'live') !== state.phase) return false;
    if (!sports.has(a.sport)) return false;
    if ((a.roi || 0) < minRoi) return false;
    return true;
  });
  state.visible.sort((a, b) => (b.roi || 0) - (a.roi || 0));
  renderArbs();
  updateHeader();
}

function updateHeader() {
  const totalEl = document.getElementById('st-total');
  const topEl = document.getElementById('st-top-roi');
  totalEl.textContent = state.visible.length;

  if (state.visible.length) {
    topEl.textContent = fmtPct(state.visible[0].roi);
    topEl.style.color = state.visible[0].roi >= 0 ? 'var(--green)' : 'var(--red)';
  } else {
    topEl.textContent = '—';
    topEl.style.color = 'var(--txt2)';
  }
}

/* ============= LIST ============= */
function renderArbs() {
  const box = document.getElementById('arbs-list');
  if (!state.visible.length) {
    box.innerHTML = `<div class="empty">Нет вилок в разделе ${escapeHtml(state.phase.toUpperCase())}<br>по текущим фильтрам...</div>`;
    return;
  }
  box.onclick = (event) => {
    const card = event.target.closest('[data-arb-slug]');
    if (card && box.contains(card)) selectArb(card.dataset.arbSlug);
  };
  box.innerHTML = state.visible.map(a => {
    const legs = a.bets || [];
    const pmLeg = legs.find(l => l.source === 'Polymarket') || {};
    const bbLeg = legs.find(l => l.source && l.source !== 'Polymarket') || {};
    const tags = [];
    tags.push(`<span class="tag ${escapeHtml(sportClass(a.sport))}">${escapeHtml(a.sport || '')}</span>`);
    if (a.market_type) tags.push(`<span class="tag mtype">${escapeHtml(a.market_type)}</span>`);
    if (a.is_live) tags.push(`<span class="tag live">ЛАЙВ</span>`);
    if (a.is_streamer) tags.push(`<span class="tag streamer">СТРИМЕР</span>`);
    const pmAge = (pmLeg && pmLeg.pm_age) || a.pm_age;
    if (pmAge && pmAge > 60) tags.push(`<span class="tag pm-age">PM ${Math.floor(pmAge)}с</span>`);
    const sourceAge = a.source_age_sec == null ? NaN : Number(a.source_age_sec);
    if (Number.isFinite(sourceAge)) {
      const ageLabel = `ДАННЫЕ ${Math.floor(sourceAge)}с`;
      const ageClass = a.source_state === 'stale' ? 'tag pm-age' : 'tag';
      tags.push(`<span class="${ageClass}">${ageLabel}${a.source_state === 'stale' ? ' · УСТАРЕЛИ' : ''}</span>`);
    } else {
      tags.push(`<span class="tag pm-age">СВЕЖЕСТЬ ?</span>`);
    }

    const cls = ['arb'];
    if (a.slug === state.activeSlug) cls.push('active');
    if (state.freshSlugs.has(a.slug)) cls.push('fresh');

    return `
      <div class="${cls.join(' ')}" data-arb-slug="${escapeHtml(a.slug || '')}">
        <div class="arb-head">
          <div class="arb-title">${escapeHtml(a.title ? (a.title + ' · ' + (a.q || '')) : (a.q || a.slug || ''))}</div>
          <div class="arb-roi">${fmtPct(a.roi)}</div>
        </div>
        <div class="arb-tags">${tags.join('')}</div>
        <div class="arb-meta">
          <div class="arb-bks">
            <span class="bk-chip ${bbLeg.source === '1win' ? 'onewin' : 'bb'}">${escapeHtml(bbLeg.source || 'БК')}</span>
            <span class="bk-chip pm">Polymarket</span>
          </div>
          <span>${fmtAgo(a.age_sec)}</span>
        </div>
      </div>
    `;
  }).join('');
}

/* ============= DETAIL ============= */
function selectArb(slug) {
  state.activeSlug = slug;
  state.detailEdited = false;
  state.calcEdited = false;
  const ph = document.getElementById('detail-placeholder');
  const body = document.getElementById('detail-body');
  if (ph) { ph.classList.add('hidden'); ph.style.display = 'none'; }     // ← inline!
  if (body) { body.classList.remove('hidden'); body.style.display = 'block'; }  // ← inline!
  renderArbs();
  refreshDetail();
}

function closeDetail() {
  state.activeSlug = null;
  const ph = document.getElementById('detail-placeholder');
  const body = document.getElementById('detail-body');
  if (ph) { ph.classList.remove('hidden'); ph.style.display = 'flex'; }
  if (body) { body.classList.add('hidden'); body.style.display = 'none'; }
  renderArbs();
}

function getActive() {
  return state.arbs.find(a => a.slug === state.activeSlug);
}

let _lastDetailRefresh = 0;
function refreshDetailThrottled() {
  const now = Date.now();
  if (now - _lastDetailRefresh < 3000) return;
  _lastDetailRefresh = now;
  refreshDetail();
}

function refreshDetail() {
  const a = getActive();
  if (!a) { closeDetail(); return; }

  document.getElementById('det-title').textContent = a.title
    ? (a.title + ' · ' + (a.q || ''))
    : (a.q || a.slug);
  const tags = [`<span class="tag mtype">${escapeHtml(a.market_type || 'МАТЧ')}</span>`,
                `<span class="tag ${escapeHtml(sportClass(a.sport))}">${escapeHtml(a.sport || '')}</span>`];
  if (a.is_streamer) tags.push(`<span class="tag streamer">СТРИМЕР</span>`);
  if (a.source_state === 'stale') tags.push(`<span class="tag pm-age">ДАННЫЕ УСТАРЕЛИ</span>`);
  else if (a.source_state === 'unknown') tags.push(`<span class="tag pm-age">СВЕЖЕСТЬ НЕИЗВЕСТНА</span>`);
  document.getElementById('det-tags').innerHTML = tags.join('');

  const roiEl = document.getElementById('kpi-roi');
  roiEl.textContent = fmtPct(a.roi);
  roiEl.style.color = a.roi >= 0 ? 'var(--green)' : 'var(--red)';
  document.getElementById('kpi-age').textContent = fmtAgo(a.age_sec);

  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};

  const pmAsk = a.pm_best_ask || 0;
  document.getElementById('kpi-depth').textContent = pmAsk ? (pmAsk * 100).toFixed(1) + '¢' : '—';

  const bbO = bbLeg.o || 0;
  const pmO = pmLeg.o || 0;
  document.getElementById('kpi-odds').innerHTML = `${fmtOdds(bbO)}<br><span style="color:var(--txt2);font-size:14px">${fmtOdds(pmO)}</span>`;

  const safeEl = document.getElementById('safe-banner');
  if (a.pm_low_price_flag) {
    safeEl.classList.remove('hidden');
    document.getElementById('signal-note-text').textContent =
      `Наблюдаемая цена ниже фильтра ${((state.config.safe_fork_threshold || 0.45) * 100).toFixed(0)}¢. ` +
      `Это только ценовой сигнал: он не гарантирует исполнение, возврат, разрешение рынка или прибыль.`;
  } else {
    safeEl.classList.add('hidden');
  }

  document.getElementById('pm-side-lbl').textContent = pmLeg.outcome || '—';
  renderHowMuch(a);
  loadPmBook(a.slug);
  loadPriceChart(a.slug);
}

/* ============= PM BOOK ============= */
async function loadPmBook(slug) {
  const requestId = state.bookRequestId = (state.bookRequestId || 0) + 1;
  const tbody = document.getElementById('pm-book-body');
  const foot = document.getElementById('pm-book-foot');
  tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Загрузка…</td></tr>';
  foot.textContent = '—';
  const enteredUsd = parseFloat(document.getElementById('hm-pm-stake')?.value || '0');
  const requestedUsd = Number.isFinite(enteredUsd) && enteredUsd >= 1 && enteredUsd <= 100000
    ? enteredUsd : 50;
  try {
    const r = await fetch(`/api/pm_book/${encodeURIComponent(slug)}?size_usd=${requestedUsd}`);
    const j = await r.json();
    if (requestId !== state.bookRequestId || state.activeSlug !== slug) return;
    if (!j.ok || !j.books || !j.books.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Стакан недоступен</td></tr>';
      return;
    }
    const a = getActive();
    if (!a || a.slug !== slug) return;
    const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
    const book = j.books.find(b => (b.outcome || '').trim() === (pmLeg.outcome || '').trim());
    if (!book) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Исход в стакане не найден</td></tr>';
      return;
    }

    pmBookCache[slug] = {
      outcome: pmLeg.outcome,
      best_ask: book.best_ask,
      best_bid: book.best_bid,
      ask_depth_usd: book.ask_depth_usd,
      avg_for_size: book.avg_for_size,
      requested_usd: requestedUsd,
    };

    const pmUsd = parseFloat(document.getElementById('hm-pm-stake')?.value || '0');

    const asks = (book.asks || []).slice(0, 7);
    let cumUsd = 0;
    let rows = '';
    for (const lvl of asks) {
      // защита: поддержка и объекта {price,size}, и массива [price,size]
      let price, size;
      if (Array.isArray(lvl)) { price = Number(lvl[0]); size = Number(lvl[1]); }
      else { price = Number(lvl.price); size = Number(lvl.size); }
      if (!isFinite(price) || !isFinite(size) || price <= 0 || size <= 0) continue;
      const usd = price * size;
      const inOrder = cumUsd < pmUsd;
      cumUsd += usd;
      rows += '<tr class="' + (inOrder ? 'in-order' : 'out-order') + '">' +
        '<td>' + (price * 100).toFixed(1) + '\u00A2</td>' +
        '<td>' + Math.round(size) + '</td>' +
        '<td>$' + Math.round(usd) + '</td>' +
      '</tr>';
    }
    const newRows = rows || '<tr><td colspan="3" class="empty-cell">пусто</td></tr>';
    if (tbody.innerHTML !== newRows) tbody.innerHTML = newRows;

    const totalUsd = (book.ask_depth_usd !== undefined && book.ask_depth_usd !== null)
      ? book.ask_depth_usd
      : asks.reduce((s, l) => s + l.price * l.size, 0);
    const totalLevels = (book.asks || []).length;
    let footTxt = 'показано ' + Math.min(totalLevels, 7) + ' уровней · глубина ask $' + Math.round(totalUsd).toLocaleString('ru-RU');
    if (book.avg_for_size && !book.avg_for_size.full) footTxt += ' · недостаточно для $' + requestedUsd.toFixed(2);
    foot.textContent = footTxt;
    const avg = book.avg_for_size;
    document.getElementById('hm-avg').textContent = avg && avg.avg_price
      ? (avg.avg_price * 100).toFixed(1) + '¢ · $' + requestedUsd.toFixed(2) + (avg.full ? '' : ' · неполный объём') : '—';
  } catch (e) {
    if (requestId === state.bookRequestId && state.activeSlug === slug) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Ошибка сети</td></tr>';
    }
  }
}

/* ============= PRICE CHART ============= */
async function loadPriceChart(slug) {
  const canvas = document.getElementById('price-chart');
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * (window.devicePixelRatio || 1);
  const h = canvas.height = 180 * (window.devicePixelRatio || 1);
  ctx.scale(1, 1);
  ctx.clearRect(0, 0, w, h);

  try {
    const r = await fetch(`/api/price_history/${encodeURIComponent(slug)}?max_points=120`);
    const j = await r.json();
    const pts = (j.points || []).map(p => p.ask).filter(v => v > 0);
    const foot = document.getElementById('chart-foot');
    const delta = document.getElementById('chart-delta');

    if (pts.length < 2) {
      ctx.fillStyle = '#4a5a52';
      ctx.font = '12px monospace';
      ctx.fillText('Недостаточно данных', w / 2 - 60, h / 2);
      foot.textContent = '—';
      delta.textContent = '—';
      return;
    }

    const min = Math.min(...pts);
    const max = Math.max(...pts);
    const pad = 20;
    const x0 = pad, x1 = w - pad, y0 = pad, y1 = h - pad;
    const xs = pts.map((_, i) => x0 + (i / (pts.length - 1)) * (x1 - x0));
    const ys = pts.map(v => y1 - ((v - min) / (max - min || 1)) * (y1 - y0));

    ctx.strokeStyle = '#1e2824';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = y0 + (i / 4) * (y1 - y0);
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
    }

    ctx.strokeStyle = '#00ff88';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(xs[0], ys[0]);
    for (let i = 1; i < xs.length; i++) ctx.lineTo(xs[i], ys[i]);
    ctx.stroke();

    ctx.fillStyle = '#00ff88';
    ctx.beginPath();
    ctx.arc(xs[xs.length - 1], ys[ys.length - 1], 4, 0, Math.PI * 2);
    ctx.fill();

    const last = pts.slice(-1)[0];
    const prev = pts.slice(0, -1).reverse().find(v => Math.abs(v - last) > 0.001);
    if (prev === undefined) {
      delta.textContent = '\u043D\u0435 \u043C\u0435\u043D\u044F\u043B\u043E\u0441\u044C';
      delta.style.color = 'var(--dim)';
    } else {
      const d = (last - prev) * 100;
      delta.textContent = (d >= 0 ? '+' : '') + d.toFixed(1) + '\u00A2 \u0437\u0430 \u0432\u0440\u0435\u043C\u044F \u0436\u0438\u0437\u043D\u0438';
      delta.style.color = d >= 0 ? 'var(--green)' : 'var(--red)';
    }

    foot.textContent = `${(min * 100).toFixed(1)}¢ — ${(max * 100).toFixed(1)}¢ · ${pts.length} точек`;
  } catch (e) {
    document.getElementById('chart-foot').textContent = 'ошибка';
  }
}

/* ============= HOW MUCH TO BET ============= */
function renderHowMuch(a) {
  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};
  document.getElementById('hm-bb-name').textContent = bbLeg.source || 'Букмекер';
  document.getElementById('hm-rate').textContent = 'курс ' + (a.rate || state.config.rate || 85);

  const stakes = a.stakes || [0, 0];
  let pmSt = 0, bbSt = 0;
  (a.bets || []).forEach((l, i) => {
    if (l.source === 'Polymarket') pmSt = stakes[i] || 0;
    if (l.source && l.source !== 'Polymarket') bbSt = stakes[i] || 0;
  });
  const rate = a.rate || state.config.rate || 85;
  if (!state.detailEdited) {
    document.getElementById('hm-bb-stake').value = Math.round(bbSt);
    document.getElementById('hm-pm-stake').value = (pmSt / rate).toFixed(2);
  }
  // лучшее ask из стакана, а не среднее по $50
  const cachedBook = pmBookCache[state.activeSlug]?.outcome === pmLeg.outcome
    ? pmBookCache[state.activeSlug] : null;
  const pmBestAsk = cachedBook?.best_ask;
  if (pmBestAsk) {
    document.getElementById('hm-price').textContent = (pmBestAsk * 100).toFixed(1) + '¢';
  } else {
    const snapshotAsk = Number(pmLeg.pm_best_ask || a.pm_best_ask);
    document.getElementById('hm-price').textContent = snapshotAsk > 0 && snapshotAsk < 1
      ? (snapshotAsk * 100).toFixed(1) + '¢' : '—';
  }
  const avg = cachedBook?.avg_for_size;
  const enteredUsd = Number(document.getElementById('hm-pm-stake').value);
  document.getElementById('hm-avg').textContent = avg && avg.avg_price && Math.abs(cachedBook.requested_usd - enteredUsd) < 0.005
    ? (avg.avg_price * 100).toFixed(1) + '¢ · $' + cachedBook.requested_usd.toFixed(2) + (avg.full ? '' : ' · неполный объём') : '—';
  document.getElementById('hm-eff').textContent = pmLeg.o ? fmtOdds(pmLeg.o) : '—';

  recalcHowMuch();

  const buyBtn = document.getElementById('btn-buy-pm');
  buyBtn.textContent = `ОТКРЫТЬ POLYMARKET · $${parseFloat(document.getElementById('hm-pm-stake').value || 0).toFixed(2)} ↗`;
  const bkBtn = document.getElementById('btn-open-bb');
  const oneWinUrlReady = bbLeg.source === '1win' && Boolean(a.bk_event_url);
  const betBoomUrlReady = bbLeg.source === 'BetBoom' && Boolean(a.bb_match_id && a.bb_tournament_id);
  bkBtn.disabled = !(oneWinUrlReady || betBoomUrlReady);
  bkBtn.textContent = oneWinUrlReady ? 'ОТКРЫТЬ СОБЫТИЕ 1WIN ↗'
    : betBoomUrlReady ? 'ОТКРЫТЬ СОБЫТИЕ BETBOOM ↗' : 'ОТКРЫТЬ СОБЫТИЕ БК ↗';
  bkBtn.title = bkBtn.disabled ? 'Ссылка на страницу события ещё не найдена' : 'Открыть страницу события без автоматической ставки';
  document.getElementById('hm-bb-name').textContent = bbLeg.source || 'Букмекер';
}

function recalcHowMuch() {
  const a = getActive(); if (!a) return;
  const rate = parseFloat(document.getElementById('hm-rate').textContent.replace('курс ', '')) || state.config.rate || 85;
  const bbStake = parseFloat(document.getElementById('hm-bb-stake').value || '0');
  const pmUsd = parseFloat(document.getElementById('hm-pm-stake').value || '0');
  const pmStakeRub = pmUsd * rate;

  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};
  const pmO = pmLeg.o || 0, bbO = bbLeg.o || 0;

  const pay1 = pmStakeRub * pmO;
  const pay2 = bbStake * bbO;
  const bank = pmStakeRub + bbStake;
  const minPay = Math.min(pay1, pay2);
  const profit = minPay - bank;

  document.getElementById('hm-total').textContent = fmtMoney(bank);
  document.getElementById('hm-payout').textContent = fmtMoney(minPay);
  const profitEl = document.getElementById('hm-profit');
  profitEl.textContent = (profit >= 0 ? '+' : '') + fmtMoney(profit);
  profitEl.style.color = profit >= 0 ? 'var(--green)' : 'var(--red)';

  document.getElementById('btn-buy-pm').textContent =
    `ОТКРЫТЬ POLYMARKET · $${pmUsd.toFixed(2)} ↗`;

  if (!state.calcEdited) syncCalcFromDetail(pmLeg, bbLeg, pmUsd, bbStake, rate);
}

/* ============= RIGHT CALC ============= */
function syncCalcFromDetail(pmLeg, bbLeg, pmUsd, bbStake, rate) {
  document.getElementById('calc-bk-name').textContent = bbLeg.source || '—';
  document.getElementById('calc-bk-odds').value = (bbLeg.o || 0).toFixed(2);
  document.getElementById('calc-bk-stake').value = Math.round(bbStake);
  const a = getActive();
  const cachedAsk = pmBookCache[state.activeSlug]?.outcome === pmLeg.outcome
    ? pmBookCache[state.activeSlug].best_ask : null;
  const rawAsk = Number(cachedAsk || pmLeg.pm_best_ask || (a && a.pm_best_ask));
  document.getElementById('calc-pm-price').value = rawAsk > 0 && rawAsk < 1 ? (rawAsk * 100).toFixed(1) : '0';
  document.getElementById('calc-pm-stake').value = pmUsd.toFixed(2);
  document.getElementById('calc-rate').value = rate.toFixed(2);
  updateCalcHint();
}

function updateCalcHint() {
  const bkO = parseFloat(document.getElementById('calc-bk-odds').value || '0');
  const bkSt = parseFloat(document.getElementById('calc-bk-stake').value || '0');
  const pmP = parseFloat(document.getElementById('calc-pm-price').value || '0') / 100;
  const pmSt = parseFloat(document.getElementById('calc-pm-stake').value || '0');
  const rate = parseFloat(document.getElementById('calc-rate').value || '85');
  const FEE = state.config.pm_fee_pct ?? 0.04;

  const pmO = pmP > 0 ? (1 / pmP) / (1 + FEE) : 0;
  const pmRub = pmSt * rate;
  const bank = bkSt + pmRub;
  const payBK = bkSt * bkO;
  const payPM = pmRub * pmO;
  const minPay = Math.min(payBK, payPM);
  const profit = minPay - bank;
  const roi = bank > 0 ? profit / bank : 0;

  const hint = document.getElementById('calc-hint');
  if (!(bkO > 1 && bkSt > 0 && pmP > 0 && pmP < 1 && pmSt > 0 && rate > 0)) {
    hint.textContent = 'Укажи обе суммы, кэф БК, цену PM и курс';
    hint.style.color = 'var(--dim)';
    return;
  }
  hint.innerHTML = `Банк: <b>${fmtMoney(bank)}</b> · Профит: <b style="color:${profit >= 0 ? 'var(--green)' : 'var(--red)'}">${profit >= 0 ? '+' : ''}${fmtMoney(profit)}</b> · ROI: <b style="color:${roi >= 0 ? 'var(--green)' : 'var(--red)'}">${fmtPct(roi)}</b>`;
  hint.style.color = 'var(--txt)';
}

/* ============= BK FILTER MODAL ============= */
function openBkModal() {
  const grid = document.getElementById('bk-grid');
  grid.innerHTML = BK_LIST.map(b => {
    const on = state.bkFilter[b.id];
    return `<div class="bk-item ${on ? 'on' : ''}" onclick="toggleBk('${b.id}')">
      <div class="mark">✓</div>
      <div class="name">${escapeHtml(b.name)}</div>
      <div class="speed">${b.speed}с</div>
    </div>`;
  }).join('');
  const cnt = BK_LIST.filter(b => state.bkFilter[b.id]).length;
  document.getElementById('bk-sub').textContent = `Отмечено ${cnt} из ${BK_LIST.length}`;
  document.getElementById('modal-bk').classList.remove('hidden');
}
function toggleBk(id) {
  state.bkFilter[id] = !state.bkFilter[id];
  localStorage.setItem('poly_arb_bks', JSON.stringify(state.bkFilter));
  openBkModal();
}
function closeBkModal() { document.getElementById('modal-bk').classList.add('hidden'); }

/* ============= CONFIG MODAL ============= */
async function openConfig() {
  try {
    const r = await fetch('/api/config');
    const j = await r.json();
    document.getElementById('cfg-bank').value = j.bank || 2500;
    document.getElementById('cfg-rate').value = j.rate || 85;
    document.getElementById('cfg-fee').value = ((j.pm_fee_pct ?? 0.04) * 100).toFixed(1);
    document.getElementById('cfg-safe').value = (j.safe_fork_threshold || 0.45);
  } catch (e) {}
  document.getElementById('modal-config').classList.remove('hidden');
}
function closeConfig() { document.getElementById('modal-config').classList.add('hidden'); }
async function saveConfig() {
  const payload = {
    bank: parseFloat(document.getElementById('cfg-bank').value || '2500'),
    rate: parseFloat(document.getElementById('cfg-rate').value || '85'),
    pm_fee_pct: parseFloat(document.getElementById('cfg-fee').value || '4') / 100,
    safe_fork_threshold: parseFloat(document.getElementById('cfg-safe').value || '0.45'),
  };
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (j.ok) {
      state.config = { ...state.config, ...payload };
      toast('Настройки сохранены');
      closeConfig();
    } else {
      toast('Ошибка: ' + (j.err || 'unknown'), true);
    }
  } catch (e) { toast('Сеть: ' + e.message, true); }
}

/* ============= EDIT MODAL ============= */
function openEdit(slug) {
  const a = state.arbs.find(x => x.slug === slug) || getActive();
  if (!a) return;
  state.editing = JSON.parse(JSON.stringify(a));
  renderEditor();
  document.getElementById('modal-edit').classList.remove('hidden');
  document.getElementById('pm-check-panel').classList.add('hidden');
}
function closeEdit() {
  state.editing = null;
  document.getElementById('modal-edit').classList.add('hidden');
}
function renderEditor() {
  const a = state.editing; if (!a) return;
  const legs = a.bets || [];
  const pm = legs.find(l => l.source === 'Polymarket') || {};
  const bb = legs.find(l => l.source && l.source !== 'Polymarket') || {};
  let pmSt = 0, bbSt = 0;
  legs.forEach((l, i) => {
    if (l.source === 'Polymarket') pmSt = (a.stakes || [])[i] || 0;
    if (l.source && l.source !== 'Polymarket') bbSt = (a.stakes || [])[i] || 0;
  });
  const rate = a.rate || state.config.rate || 85;
  document.getElementById('ed-match').textContent = a.title
    ? (a.title + ' — ' + (a.q || ''))
    : (a.q || a.slug);
  document.getElementById('ed-sub').textContent = `${a.market_type || ''} · ${a.sport || ''} · ${a.slug}`;
  const pmUsd = rate > 0 ? pmSt / rate : pmSt;

  document.getElementById('ed-leg-0').innerHTML = `
    <div class="leg-head"><span class="lbl">Плечо 1</span><span class="leg-bk">${escapeHtml(bb.source || 'Букмекер')}</span></div>
    <div class="leg-field"><span class="lbl">Исход</span><input type="text" id="ed-bb-outcome" value="${escapeHtml(bb.outcome || '')}" readonly></div>
    <div class="leg-field"><span class="lbl">Коэффициент</span><input type="number" step="0.01" id="ed-bb-odds" value="${bb.o || 0}"></div>
    <div class="leg-field"><span class="lbl">Ставка, ₽</span><input type="number" step="1" id="ed-bb-stake" value="${bbSt.toFixed(0)}"></div>
  `;
  document.getElementById('ed-leg-1').innerHTML = `
    <div class="leg-head"><span class="lbl">Плечо 2</span><span class="leg-bk">Polymarket</span></div>
    <div class="leg-field"><span class="lbl">Исход</span><input type="text" id="ed-pm-outcome" value="${escapeHtml(pm.outcome || '')}" readonly></div>
    <div class="leg-field"><span class="lbl">Цена, ¢</span><input type="number" step="0.1" id="ed-pm-price" value="${Number(pm.pm_best_ask || a.pm_best_ask) > 0 ? (Number(pm.pm_best_ask || a.pm_best_ask) * 100).toFixed(1) : '0'}"></div>
    <div class="leg-field"><span class="lbl">Ордер, $</span><input type="number" step="0.01" id="ed-pm-amount" value="${pmUsd.toFixed(2)}"></div>
    <div class="leg-field"><span class="lbl">Курс ₽/$</span><input type="number" step="0.01" id="ed-rate" value="${rate.toFixed(2)}"></div>
  `;
  ['ed-bb-odds','ed-bb-stake','ed-pm-price','ed-pm-amount','ed-rate'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', recalcEditor);
  });
  recalcEditor();
}

function recalcEditor() {
  const a = state.editing; if (!a) return;
  const bbO = parseFloat(document.getElementById('ed-bb-odds').value || '0');
  const bbSt = parseFloat(document.getElementById('ed-bb-stake').value || '0');
  const pmPrice = parseFloat(document.getElementById('ed-pm-price').value || '0') / 100;
  const pmUsd = parseFloat(document.getElementById('ed-pm-amount').value || '0');
  const rate = parseFloat(document.getElementById('ed-rate').value || '85');
  const FEE = state.config.pm_fee_pct ?? 0.04;
  const pmO = pmPrice > 0 ? (1 / pmPrice) / (1 + FEE) : 0;
  const pmRub = pmUsd * rate;
  const bank = bbSt + pmRub;
  const pay1 = bbSt * bbO;
  const pay2 = pmRub * pmO;
  const minPay = Math.min(pay1, pay2);
  const profit = minPay - bank;
  const roi = bank > 0 ? profit / bank : 0;
  document.getElementById('ed-bank').textContent = fmtMoney(bank);
  document.getElementById('ed-p1').textContent = (pay1 - bank >= 0 ? '+' : '') + fmtMoney(pay1 - bank);
  document.getElementById('ed-p2').textContent = (pay2 - bank >= 0 ? '+' : '') + fmtMoney(pay2 - bank);
  const roiEl = document.getElementById('ed-roi');
  roiEl.textContent = fmtPct(roi);
  roiEl.style.color = roi >= 0 ? 'var(--green)' : 'var(--red)';
  a._calc = {
    bb_odds: bbO, bb_stake_rub: bbSt,
    pm_price: pmPrice, pm_amount_usd: pmUsd, rate,
    bank, payout: minPay, profit, roi,
  };
}

async function saveTrade() {
  const a = state.editing  ; if (!a || !a._calc) return;
  const payload = {
    slug: a.slug, q: a.q, sport: a.sport, market_type: a.market_type,
    bank: a._calc.bank, profit: a._calc.profit, roi: a._calc.roi,
    payout: a._calc.payout, rate: a._calc.rate,
    legs: [
      { book: ((a.bets.find(b => b.source && b.source !== 'Polymarket') || {}).source) || 'Букмекер', outcome: ((a.bets.find(b => b.source && b.source !== 'Polymarket') || {}).outcome) || '',
        odds: a._calc.bb_odds, stake_rub: a._calc.bb_stake_rub },
      { book: 'Polymarket', outcome: ((a.bets.find(b => b.source === 'Polymarket') || {}).outcome) || '',
        price_usd: a._calc.pm_price, amount_usd: a._calc.pm_amount_usd,
        stake_rub: a._calc.pm_amount_usd * a._calc.rate },
    ],
  };
  try {
    const r = await fetch('/api/trade', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (j.ok) { toast('\u0421\u0434\u0435\u043B\u043A\u0430 \u0441\u043E\u0445\u0440\u0430\u043D\u0435\u043D\u0430'); closeEdit(); loadDaily(); }
    else toast('\u041E\u0448\u0438\u0431\u043A\u0430: ' + (j.err || 'unknown'), true);
  } catch (e) { toast('\u0421\u0435\u0442\u044C: ' + e.message, true); }
}

async function hideCurrent() {
  const a = state.editing; if (!a) return;
  try { await fetch('/api/hide/' + encodeURIComponent(a.slug), { method: 'POST' }); } catch (e) {}
  state.hideSet.add(a.slug);
  closeEdit();
  if (state.activeSlug === a.slug) closeDetail();
}

async function checkPM() {
  const a = state.editing; if (!a) return;
  const panel = document.getElementById('pm-check-panel');
  const body = document.getElementById('pm-check-body');
  panel.classList.remove('hidden');
  body.innerHTML = '<div class="pm-check-loading">\u0417\u0430\u043F\u0440\u0430\u0448\u0438\u0432\u0430\u044E Polymarket...</div>';
  const amtEl = document.getElementById('ed-pm-amount');
  const sizeUsd = parseFloat((amtEl && amtEl.value) || '50');
  try {
    const r = await fetch('/api/pm_book/' + encodeURIComponent(a.slug) + '?size_usd=' + sizeUsd);
    const j = await r.json();
    if (!j.ok) { body.innerHTML = '<div class="pm-check-err">' + escapeHtml(j.err || 'unknown') + '</div>'; return; }
    const pmOutcome = document.getElementById('ed-pm-outcome').value;
    let book = j.books.find(b => (b.outcome || '').trim() === pmOutcome.trim());
    if (!book || !book.best_ask) { body.innerHTML = '<div class="pm-check-err">PM \u0441\u0442\u0430\u043A\u0430\u043D \u043F\u0443\u0441\u0442</div>'; return; }
    const ba = book.best_ask;
    const pmPrice = parseFloat(document.getElementById('ed-pm-price').value || '0') / 100;
    const FEE = state.config.pm_fee_pct ?? 0.04;
    const curOdds = pmPrice > 0 ? (1 / pmPrice) / (1 + FEE) : 0;
    const liveOdds = (1 / ba) / (1 + FEE);
    const diffPct = curOdds > 0 ? ((liveOdds - curOdds) / curOdds * 100) : 0;
    let verdict, cls;
    if (Math.abs(diffPct) < 1.0) { verdict = 'PM \u0441\u043E\u043E\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0443\u0435\u0442 \u0440\u0435\u0434\u0430\u043A\u0442\u043E\u0440\u0443'; cls = 'ok'; }
    else if (diffPct > 0) { verdict = 'PM \u043B\u0443\u0447\u0448\u0435 \u043D\u0430 ' + diffPct.toFixed(2) + '%'; cls = 'better'; }
    else { verdict = 'PM \u0445\u0443\u0436\u0435 \u043D\u0430 ' + Math.abs(diffPct).toFixed(2) + '%'; cls = 'worse'; }
    body.innerHTML =
      '<div class="pm-check-verdict ' + cls + '">' + verdict + '</div>' +
      '<table class="pm-check-table">' +
        '<thead><tr><th>\u0438\u0441\u0445\u043E\u0434</th><th>best bid</th><th>best ask</th><th>k \u0441 \u043A\u043E\u043C\u0438\u0441\u0441\u0438\u0435\u0439</th><th>\u0433\u043B\u0443\u0431\u0438\u043D\u0430 ask</th></tr></thead>' +
        '<tbody>' + j.books.map(b =>
          '<tr><td>' + escapeHtml(b.outcome || '') + '</td>' +
              '<td>' + (b.best_bid ? b.best_bid.toFixed(4) : '\u2014') + '</td>' +
              '<td>' + (b.best_ask ? b.best_ask.toFixed(4) : '\u2014') + '</td>' +
              '<td>k=' + (b.best_ask ? ((1 / b.best_ask) / (1 + FEE)).toFixed(4) : '\u2014') + '</td>' +
              '<td>$' + (b.ask_depth_usd || 0) + '</td></tr>'
        ).join('') + '</tbody>' +
      '</table>';
  } catch (e) { body.innerHTML = '<div class="pm-check-err">' + escapeHtml(e.message) + '</div>'; }
}
function refreshPMNow() { checkPM(); }

function openBookmakerEvent() {
  const a = getActive(); if (!a) return;
  const bkLeg = (a.bets || []).find(leg => leg.source && leg.source !== 'Polymarket');
  if (bkLeg?.source === '1win' && a.bk_event_url) {
    try {
      const eventUrl = new URL(a.bk_event_url, window.location.origin);
      if (eventUrl.protocol !== 'https:') throw new Error('Небезопасный протокол ссылки');
      window.open(eventUrl.href, '_blank', 'noopener,noreferrer');
    } catch (e) {
      toast('Не удалось открыть страницу события 1win', true);
    }
    return;
  }
  if (bkLeg?.source !== 'BetBoom') {
    toast('Ссылка на страницу события ещё не найдена', true);
    return;
  }
  const mid = a.bb_match_id, tour = a.bb_tournament_id;
  if (!(tour && mid)) {
    toast('Точная ссылка на событие BetBoom не найдена', true);
    return;
  }
  const sportMap = { 'CS2': 'cs2', 'DOTA2': 'dota-2', 'LOL': 'lol', 'VALORANT': 'valorant', 'TENNIS': 'tennis' };
  const sp = sportMap[a.sport] || 'esport';
  const url = 'https://betboom.ru/esport/' + sp + '/' + tour + '/' + mid;
  window.open(url, '_blank', 'noopener,noreferrer');
}
async function openPMUrl() {
  const a = getActive(); if (!a) return;
  try {
    const r = await fetch('/api/pm_url/' + encodeURIComponent(a.slug));
    const j = await r.json();
    if (j.url) window.open(j.url, '_blank', 'noopener,noreferrer');
    else toast('PM URL \u043D\u0435 \u043D\u0430\u0439\u0434\u0435\u043D', true);
  } catch (e) { toast('\u041E\u0448\u0438\u0431\u043A\u0430: ' + e.message, true); }
}
function markFilled() {
  const a = getActive();
  if (a) openEdit(a.slug);
}

async function showTrades() {
  const list = document.getElementById('trades-list');
  list.innerHTML = '<div class="empty-small">\u0417\u0430\u0433\u0440\u0443\u0437\u043A\u0430...</div>';
  document.getElementById('modal-trades').classList.remove('hidden');
  try {
    const r = await fetch('/api/trades');
    const j = await r.json();
    const tr = j.trades || [];
    if (!tr.length) { list.innerHTML = '<div class="empty-small">\u041F\u043E\u043A\u0430 \u043F\u0443\u0441\u0442\u043E</div>'; return; }
    list.innerHTML = tr.map(t => {
      const profit = Number(t.profit || 0);
      const cls = profit >= 0 ? 'pos' : 'neg';
      const leg1 = (t.legs && t.legs[0]) ? (t.legs[0].book + ' \u00B7 ' + t.legs[0].outcome) : '';
      const leg2 = (t.legs && t.legs[1]) ? (t.legs[1].book + ' \u00B7 ' + t.legs[1].outcome) : '';
      return '<div class="trade-item">' +
        '<div class="head">' +
          '<span class="slug">' + escapeHtml(t.q || t.slug || '') + '</span>' +
          '<span class="profit ' + cls + '">' + (profit >= 0 ? '+' : '') + Math.round(profit) + ' \u20BD</span>' +
        '</div>' +
        '<div class="legs"><div>1) ' + escapeHtml(leg1) + '</div><div>2) ' + escapeHtml(leg2) + '</div></div>' +
        '<div class="meta">' +
          '<span>' + escapeHtml((t.ts || '').replace('T', ' ')) + '</span>' +
          '<span>\u0431\u0430\u043D\u043A: <b>' + Math.round(Number(t.bank || 0)) + '\u20BD</b></span>' +
          '<span>ROI: <b>' + (Number(t.roi || 0) * 100).toFixed(3) + '%</b></span>' +
        '</div>' +
      '</div>';
    }).join('');
  } catch (e) { list.innerHTML = '<div class="empty-small">\u041E\u0448\u0438\u0431\u043A\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043A\u0438</div>'; }
}
function closeTrades() { document.getElementById('modal-trades').classList.add('hidden'); }
function exportCSV() { window.location.href = '/api/export_csv'; }

async function loadDaily() {
  try {
    const today = new Date().toISOString().slice(0, 10);
    const r = await fetch('/api/daily?date=' + today);
    const j = await r.json();
    state.dailyStats = j;
    updateHeader();
    const c1 = document.getElementById('daily-count'); if (c1) c1.textContent = j.count;
    const c2 = document.getElementById('daily-count2'); if (c2) c2.textContent = j.count;
    const b1 = document.getElementById('daily-bank'); if (b1) b1.textContent = fmtMoney(j.bank);
    const b2 = document.getElementById('daily-bank2'); if (b2) b2.textContent = fmtMoney(j.bank);
    const p1 = document.getElementById('daily-profit'); if (p1) {
      p1.textContent = (j.profit >= 0 ? '+' : '') + fmtMoney(j.profit);
      p1.style.color = j.profit >= 0 ? 'var(--green)' : 'var(--red)';
    }
    const roiTxt = (j.roi >= 0 ? '+' : '') + (j.roi * 100).toFixed(3) + '%';
    const r1 = document.getElementById('daily-roi'); if (r1) {
      r1.textContent = roiTxt;
      r1.style.color = j.roi >= 0 ? 'var(--green)' : 'var(--red)';
    }
    const r2 = document.getElementById('daily-roi2'); if (r2) r2.textContent = roiTxt;
    const fill = Math.max(0, Math.min(1, j.roi / 0.30));
    const arc = document.getElementById('daily-ring-arc');
    if (arc) { const len = 2 * Math.PI * 52; arc.setAttribute('stroke-dasharray', (len * fill) + ' ' + len); }
    const rt = document.getElementById('daily-ring-text');
    if (rt) rt.textContent = (j.roi * 100).toFixed(1) + '%';
  } catch (e) {}
}

async function loadWeekly() {
  const box = document.getElementById('weekly-chart');
  if (!box) return;
  try {
    const r = await fetch('/api/weekly');
    const j = await r.json();
    const days = j.days || [];
    if (!days.length) { box.innerHTML = '<div class="empty-small">\u041D\u0435\u0442 \u0434\u0430\u043D\u043D\u044B\u0445</div>'; return; }
    const maxAbs = Math.max(0.01, ...days.map(d => Math.abs(d.roi || 0)));
    box.innerHTML = '<div class="wk-chart">' + days.map(d => {
      const roi = d.roi || 0;
      const pct = Math.abs(roi) / maxAbs * 100;
      const isProfit = roi >= 0;
      const dt = (d.date || '').slice(5);
      const profitTxt = d.profit ? (d.profit >= 0 ? '+' : '') + Math.round(d.profit) + '\u20BD' : '';
      return '<div class="wk-col">' +
        '<div class="wk-profit ' + (isProfit ? 'pos' : 'neg') + '">' + profitTxt + '</div>' +
        '<div class="wk-bar-wrap"><div class="wk-bar ' + (isProfit ? 'pos' : 'neg') + '" style="height:' + pct + '%"></div></div>' +
        '<div class="wk-date">' + dt + '</div>' +
      '</div>';
    }).join('') + '</div>';
  } catch (e) { box.innerHTML = '<div class="empty-small">\u041E\u0448\u0438\u0431\u043A\u0430</div>'; }
}

async function loadRecent() {
  const box = document.getElementById('recent-trades');
  if (!box) return;
  try {
    const r = await fetch('/api/trades?limit=5');
    const j = await r.json();
    const tr = j.trades || [];
    if (!tr.length) { box.innerHTML = '<div class="empty-small">\u041F\u043E\u043A\u0430 \u043F\u0443\u0441\u0442\u043E</div>'; return; }
    box.innerHTML = tr.map(t => {
      const profit = Number(t.profit || 0);
      const cls = profit >= 0 ? 'pos' : 'neg';
      return '<div class="rt-item">' +
        '<div class="rt-head">' +
          '<div class="rt-slug">' + escapeHtml((t.q || t.slug || '').slice(0, 42)) + '</div>' +
          '<div class="rt-profit ' + cls + '">' + (profit >= 0 ? '+' : '') + Math.round(profit) + '\u20BD</div>' +
        '</div>' +
        '<div class="rt-meta">' +
          '<span>ROI ' + (Number(t.roi || 0) * 100).toFixed(2) + '%</span>' +
          '<span>\u0431\u0430\u043D\u043A ' + Math.round(Number(t.bank || 0)) + '\u20BD</span>' +
        '</div>' +
      '</div>';
    }).join('');
  } catch (e) { box.innerHTML = '<div class="empty-small">\u041E\u0448\u0438\u0431\u043A\u0430</div>'; }
}

function openDaily() {
  loadDaily(); loadWeekly(); loadRecent();
  document.getElementById('modal-daily').classList.remove('hidden');
}
function closeDaily() { document.getElementById('modal-daily').classList.add('hidden'); }

async function shutdownBot() {
  if (!confirm('Закрыть веб-панель? Сканер продолжит работу отдельно.')) return;
  try {
    await fetch('/api/shutdown', { method: 'POST' });
    toast('Панель закрывается...');
  } catch (e) { toast('\u0421\u0435\u0442\u044C: ' + e.message, true); }
}

function init() {
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      state.phase = t.dataset.phase;
      applyFilters();
    });
  });

  ['f-cs2','f-dota2','f-lol','f-tennis'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', applyFilters);
  });
  const minRoiEl = document.getElementById('f-minroi');
  if (minRoiEl) minRoiEl.addEventListener('input', applyFilters);

  const soundBtn = document.getElementById('st-sound');
  if (soundBtn) soundBtn.addEventListener('click', toggleSound);
  updateSoundBtn();

  const cfgBtn = document.getElementById('btn-config');
  if (cfgBtn) cfgBtn.addEventListener('click', openConfig);
  const dailyBtn = document.getElementById('btn-daily');
  if (dailyBtn) dailyBtn.addEventListener('click', openDaily);
  const shutdownBtn = document.getElementById('btn-shutdown');
  if (shutdownBtn) shutdownBtn.addEventListener('click', shutdownBot);

  const detClose = document.getElementById('btn-det-close');
  if (detClose) detClose.addEventListener('click', closeDetail);

  const hmBB = document.getElementById('hm-bb-stake');
  if (hmBB) hmBB.addEventListener('input', () => { state.detailEdited = true; recalcHowMuch(); });
  const hmPM = document.getElementById('hm-pm-stake');
  if (hmPM) hmPM.addEventListener('input', () => {
    state.detailEdited = true;
    state.bookRequestId = (state.bookRequestId || 0) + 1;
    document.getElementById('hm-avg').textContent = '—';
    recalcHowMuch();
    clearTimeout(state.bookRefreshTimer);
    state.bookRefreshTimer = setTimeout(() => { if (state.activeSlug) loadPmBook(state.activeSlug); }, 600);
  });

  const openBB = document.getElementById('btn-open-bb');
  if (openBB) openBB.addEventListener('click', openBookmakerEvent);
  const buyPM = document.getElementById('btn-buy-pm');
  if (buyPM) buyPM.addEventListener('click', openPMUrl);
  const filled = document.getElementById('btn-filled');
  if (filled) filled.addEventListener('click', markFilled);

  ['calc-bk-odds','calc-bk-stake','calc-pm-price','calc-pm-stake','calc-rate'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => { state.calcEdited = true; updateCalcHint(); });
  });
  const resetBtn = document.getElementById('btn-calc-reset');
  if (resetBtn) resetBtn.addEventListener('click', () => {
    state.calcEdited = true;
    document.getElementById('calc-bk-odds').value = 0;
    document.getElementById('calc-bk-stake').value = 0;
    document.getElementById('calc-pm-price').value = 0;
    document.getElementById('calc-pm-stake').value = 0;
    document.getElementById('calc-rate').value = state.config.rate || 85;
    updateCalcHint();
  });
  const myArbsBtn = document.getElementById('btn-my-arbs');
  if (myArbsBtn) myArbsBtn.addEventListener('click', showTrades);

  const totalBank = document.getElementById('risk-total-bank');
  if (totalBank) {
    try { totalBank.value = localStorage.getItem('poly_arb_total_bank') || ''; } catch (e) {}
    totalBank.addEventListener('input', updateBankLimit);
    updateBankLimit();
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeEdit(); closeTrades(); closeDaily(); closeBkModal(); closeConfig();
    }
  });
  document.addEventListener('click', () => ensureAudioCtx(), { once: true });

  setInterval(() => {
    if (state.activeSlug) { loadPriceChart(state.activeSlug); loadPmBook(state.activeSlug); }
  }, 10000);
  setInterval(loadDaily, 30000);
  setInterval(() => { if (state.visible.length) renderArbs(); }, 2000);

  loadDaily();
  connectWS();
}

document.addEventListener('DOMContentLoaded', init);
