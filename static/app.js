 /* =====================================================
   Poly-ARB Web · SBП-style app v3
   ===================================================== */
const state = {
  ws: null,
  wsRetryMs: 1000,
  wsReconnectTimer: null,
  wsGeneration: 0,
  wsLastMessageAt: 0,
  wsState: 'connecting',
  arbs: [],          // все enriched arbs
  opportunitiesBySlug: new Map(), // validated Opportunity v1 rows for list-card dual-read
  visible: [],       // после фильтров
  activeSlug: null,
  activeSnapshotId: null,
  editing: null,
  hideSet: new Set(),
  seenSlugs: new Set(),
  freshSlugs: new Set(),
  phase: 'all',      // 'all' | 'live' | 'prematch' | 'unknown'
  scanStatus: null,
  pinnedSignalSnapshots: [],
  suggestedCapped: false,
  audioCtx: null,
  soundEnabled: localStorage.getItem('poly_arb_sound') !== 'off',
  desktopNotificationsEnabled: localStorage.getItem('poly_arb_desktop_notifications') === 'on',
  config: {
    rate: 1, bank: 100, currency: 'USDT',
    safe_fork_threshold: 0.45, min_roi_pct: 3,
    enabled_bk_sources: ['1win'], available_bk_sources: ['1win', 'BetBoom'],
  },
  dailyStats: null,
};

const pmBookCache = {};  // slug -> {best_ask, best_bid, ask_depth_usd}
const quoteFlashTimers = new WeakMap();
// Отдельный, ограниченный кэш ссылок не участвует в получении котировок.
const pmEventLinkCache = new Map();

const BK_LIST = [
  { id: '1win',    name: '1win',    speed: 0, color: 'onewin' },
  { id: 'betboom', name: 'BetBoom', speed: 0, color: 'bb' },
];
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

function updateDesktopNotificationsBtn() {
  const btn = document.getElementById('btn-desktop-notify');
  if (!btn) return;
  const enabled = state.desktopNotificationsEnabled && window.Notification?.permission === 'granted';
  btn.classList.toggle('notify-enabled', Boolean(enabled));
  btn.setAttribute('aria-pressed', String(Boolean(enabled)));
  btn.setAttribute('aria-label', enabled ? 'Уведомления включены' : 'Уведомления выключены');
  btn.title = `Уведомления ${enabled ? 'включены' : 'выключены'} · новые свежие вилки при скрытой вкладке`;
}

async function toggleDesktopNotifications() {
  if (state.desktopNotificationPermissionPending) return;
  if (state.desktopNotificationsEnabled && window.Notification?.permission === 'granted') {
    state.desktopNotificationsEnabled = false;
    localStorage.setItem('poly_arb_desktop_notifications', 'off');
    updateDesktopNotificationsBtn();
    return;
  }
  if (!window.Notification || typeof window.Notification.requestPermission !== 'function') {
    toast('Уведомления недоступны в этом браузере', true);
    return;
  }
  state.desktopNotificationPermissionPending = true;
  try {
    const permission = await window.Notification.requestPermission();
    state.desktopNotificationsEnabled = permission === 'granted';
    localStorage.setItem('poly_arb_desktop_notifications', state.desktopNotificationsEnabled ? 'on' : 'off');
    if (!state.desktopNotificationsEnabled) toast('Разрешение на уведомления не выдано', true);
  } catch (e) {
    state.desktopNotificationsEnabled = false;
    toast('Не удалось включить уведомления', true);
  } finally {
    state.desktopNotificationPermissionPending = false;
    updateDesktopNotificationsBtn();
  }
}

function notifyNewSignals(rows) {
  if (!state.desktopNotificationsEnabled || !document.hidden || state.wsState !== 'connected'
      || !window.Notification || window.Notification.permission !== 'granted') return;
  const now = Date.now() / 1000;
  const statusAt = Number(state.scanStatus?.ts);
  if (state.scanStatus?.state !== 'running' || !Number.isFinite(statusAt)
      || now - statusAt < -0.5 || now - statusAt > 10) return;
  const visible = new Set(state.visible.map(row => row.slug));
  const fresh = rows.filter(a => {
    if (!a?.slug || !visible.has(a.slug) || a.inactive_snapshot || a.roi == null || !Number.isFinite(Number(a.roi))) return false;
    const pm = (a.bets || []).find(leg => leg.source === 'Polymarket');
    const bk = (a.bets || []).find(leg => leg.source && leg.source !== 'Polymarket');
    if (!pm || !bookmakerQuoteIsFresh(a, bk)) return false;
    const ageValue = pmBookAge(pm, a);
    const elapsed = now - Number(a.ts);
    if (ageValue == null || ageValue === '') return false;
    const age = Number(ageValue);
    return Number.isFinite(age) && age >= 0 && Number.isFinite(elapsed) && elapsed >= -0.5
      && age + Math.max(0, elapsed) <= runtimeFreshnessLimit('polymarket_book_ttl_sec', 30);
  });
  if (!fresh.length) return;
  const first = fresh[0];
  const title = fresh.length === 1 ? `Новая вилка · ${fmtPct(first.roi)}` : `Новые вилки: ${fresh.length}`;
  const eventName = String(first.title || first.q || first.slug).slice(0, 140);
  try {
    const notification = new window.Notification(title, {
      body: `${eventName}\nПроверьте котировки и исходы вручную.`, tag: 'poly-arb-new-signals',
    });
    notification.onclick = () => {
      window.focus();
      if (state.arbs.some(a => a.slug === first.slug)) selectArb(first.slug);
      notification.close();
    };
  } catch (e) { /* Notifications are optional; scanner operation must continue. */ }
}

/* ============= UTILS ============= */
function pmBookAge(pm, arb) {
  if (pm.pm_book_age_sec != null) return pm.pm_book_age_sec;
  // New snapshots explicitly identify the receipt-age semantics. An unknown
  // receipt age must not be replaced by the age of an unchanged best ask.
  if (pm.pm_book_age_source === 'local_receipt') return null;
  return pm.pm_age ?? arb.pm_age;
}

function formatPmDropQuote(pm) {
  if (pm.best_ask == null) return null;
  let text = `последний PM ${pm.outcome || ''} ask ${(Number(pm.best_ask) * 100).toFixed(1)}¢`;
  if (pm.average_ask != null) text += ` / avg ${(Number(pm.average_ask) * 100).toFixed(1)}¢`;
  if (pm.book_age_sec != null) {
    const label = pm.book_age_source === 'local_receipt' ? 'стакан' : 'без изменения лучшего ask';
    text += ` · ${label} ${Number(pm.book_age_sec).toFixed(1)}с`;
  } else if (pm.book_age_source === 'local_receipt') {
    text += ' · возраст стакана неизвестен';
  }
  return text;
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function fmtMoney(n) {
  if (!isFinite(n)) n = 0;
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USDT';
}
function fmtHistoricalAmount(n, currency) {
  const amount = Number(n);
  const unit = String(currency || 'RUB').toUpperCase() === 'USDT' ? 'USDT' : '₽';
  return (Number.isFinite(amount) ? amount.toFixed(2) : '0.00') + ' ' + unit;
}
function fmtOdds(n) { return (Number(n) || 0).toFixed(3); }
function fmtPct(n, sign = true) {
  const v = (Number(n) || 0) * 100;
  return (sign && v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function normalizePmFeeSchedule(schedule) {
  const enabled = schedule?.enabled ?? schedule?.fees_enabled;
  if (!schedule || typeof enabled !== 'boolean'
      || schedule.source !== 'clob_market_info') return null;
  if (schedule.rate == null || schedule.exponent == null
      || typeof schedule.rate === 'boolean' || typeof schedule.exponent === 'boolean'
      || String(schedule.rate).trim() === '' || String(schedule.exponent).trim() === '') return null;
  const rate = Number(schedule.rate);
  const exponent = Number(schedule.exponent);
  if (!Number.isFinite(rate) || rate < 0 || rate > 1
      || !Number.isFinite(exponent) || exponent <= 0 || exponent > 10
      || (!enabled && rate !== 0)) return null;
  return { enabled, rate, exponent, source: schedule.source };
}
function getPmFeeSchedule(pmLeg) {
  if (!pmLeg) return null;
  return normalizePmFeeSchedule({
    enabled: pmLeg.pm_fee_enabled,
    rate: pmLeg.pm_fee_rate,
    exponent: pmLeg.pm_fee_exponent,
    source: pmLeg.pm_fee_source,
  });
}
function pmFeePerShare(price, schedule) {
  if (!schedule || !Number.isFinite(price) || price <= 0 || price >= 1) return null;
  return schedule.enabled
    ? schedule.rate * Math.pow(price * (1 - price), schedule.exponent) : 0;
}
function pmEffectiveOdds(price, schedule) {
  const fee = pmFeePerShare(price, schedule);
  return fee === null ? null : 1 / (price + fee);
}
function setLiveQuoteValue(element, value, scope) {
  if (!element) return;
  const nextValue = String(value == null ? '—' : value);
  const nextScope = String(scope || '');
  const sameScope = element.dataset.quoteFlashScope === nextScope;
  const previousValue = element.dataset.quoteFlashValue;
  if (element.textContent !== nextValue) element.textContent = nextValue;

  const previousTimer = quoteFlashTimers.get(element);
  if (!sameScope) {
    if (previousTimer) clearTimeout(previousTimer);
    element.classList.remove('quote-change-flash');
  } else if (previousValue !== undefined && previousValue !== nextValue) {
    if (previousTimer) clearTimeout(previousTimer);
    element.classList.remove('quote-change-flash');
    void element.offsetWidth;
    element.classList.add('quote-change-flash');
    quoteFlashTimers.set(element, setTimeout(() => {
      element.classList.remove('quote-change-flash');
      quoteFlashTimers.delete(element);
    }, 1200));
  }
  element.dataset.quoteFlashScope = nextScope;
  element.dataset.quoteFlashValue = nextValue;
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
function ageExceedsTtl(ageValue, ttlValue) {
  const age = ageValue == null || ageValue === '' ? NaN : Number(ageValue);
  const ttl = ttlValue == null || ttlValue === '' ? NaN : Number(ttlValue);
  return Number.isFinite(age) && age >= 0 && Number.isFinite(ttl) && age > ttl;
}
function freshnessBadge(label, ageValue, ttlValue) {
  const age = ageValue == null || ageValue === '' ? NaN : Number(ageValue);
  if (!Number.isFinite(age) || age < 0) {
    return `<span class="tag">${escapeHtml(label)} ?</span>`;
  }
  const stale = ageExceedsTtl(age, ttlValue);
  return `<span class="tag${stale ? ' pm-age' : ''}">${escapeHtml(label)} ${fmtAgo(age)}${stale ? ' · УСТАРЕЛИ' : ''}</span>`;
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

/* The saved USDT bank and read-only runtime risk fraction drive the plan guide. */
function getTotalBank() {
  const input = document.getElementById('risk-total-bank');
  const bank = Number(state.config.bank ?? input?.value);
  return Number.isFinite(bank) && bank > 0 ? bank : null;
}

function getPlanCapFraction() {
  const fraction = Number(state.config.runtime_limits?.safety?.bankroll_plan_fraction);
  return Number.isFinite(fraction) && fraction >= 0 && fraction <= 1 ? fraction : 0.4;
}
function getPlanCapPercent() { return Math.round(getPlanCapFraction() * 100); }

function getPlanCap() {
  const totalBank = getTotalBank();
  return totalBank === null ? null : totalBank * getPlanCapFraction();
}

function updatePlanRiskNote(exposureUsdt) {
  const note = document.getElementById('hm-risk-note');
  if (!note) return;
  const cap = getPlanCap();
  const capPercent = getPlanCapPercent();
  note.classList.remove('safe', 'warn');
  if (cap === null) {
    note.textContent = `Укажите общий банк в настройках, чтобы применить ${capPercent}%-ориентир к плану.`;
    return;
  }
  if (exposureUsdt > cap + 0.01) {
    note.classList.add('warn');
    note.textContent = `План ${fmtMoney(exposureUsdt)} выше ${capPercent}%-ориентира ${fmtMoney(cap)} на ${fmtMoney(exposureUsdt - cap)}. Проверьте риск вручную.`;
  } else if (state.suggestedCapped) {
    note.classList.add('safe');
    note.textContent = `Рекомендация уменьшена до ${capPercent}%-ориентира (${fmtMoney(cap)}); перепроверьте глубину стакана для нового объёма.`;
  } else {
    note.classList.add('safe');
    note.textContent = `План ${fmtMoney(exposureUsdt)} в пределах ${capPercent}%-ориентира ${fmtMoney(cap)} на один план.`;
  }
}

function updateBankLimit() {
  const input = document.getElementById('risk-total-bank');
  const output = document.getElementById('risk-limit');
  if (!input || !output) return;
  const bank = getTotalBank();
  output.textContent = bank !== null ? fmtMoney(bank * getPlanCapFraction()) : '—';
  const capLabel = document.getElementById('risk-limit-label');
  if (capLabel) capLabel.textContent = `${getPlanCapPercent()}% на один план`;
  const riskHint = document.getElementById('risk-hint');
  if (riskHint) riskHint.textContent = `${getPlanCapPercent()}% — read-only ориентир суммы одного плана. Автоматических ставок нет.`;
  if (state.activeSlug) {
    if (!state.detailEdited) renderHowMuch(getActive());
    else recalcHowMuch();
  }
  if (state.calcEdited) updateCalcHint();
}

function setPanelStatus(status, retryMs = 0) {
  const label = document.getElementById('feed-status');
  const dot = document.getElementById('feed-dot');
  const labels = {
    connecting: 'Подключение…',
    connected: 'Панель подключена',
    stale: 'Поток устарел',
    disconnected: 'Нет связи с панелью',
  };
  state.wsState = status;
  if (label) {
    label.textContent = labels[status] || labels.disconnected;
    const last = state.wsLastMessageAt
      ? new Date(state.wsLastMessageAt).toLocaleTimeString('ru-RU')
      : 'нет сообщений';
    const retry = retryMs ? ` Повторное подключение через ${Math.ceil(retryMs / 1000)} с.` : '';
    label.title = `Последнее сообщение панели: ${last}.${retry}`;
  }
  if (dot) {
    dot.classList.toggle('connected', status === 'connected');
    dot.classList.toggle('disconnected', status === 'disconnected');
    dot.classList.toggle('stale', status === 'stale');
  }
  if (status !== 'connected' && state.activeSlug && getActive()) renderHowMuch(getActive());
}

/* ============= WS ============= */
function scheduleWsReconnect() {
  if (state.wsReconnectTimer) return;
  const delay = state.wsRetryMs;
  state.wsRetryMs = Math.min(15000, Math.max(1000, Math.round(delay * 1.7)));
  setPanelStatus('disconnected', delay);
  state.wsReconnectTimer = setTimeout(() => {
    state.wsReconnectTimer = null;
    connectWS();
  }, delay);
}

function connectWS() {
  if (state.ws && [WebSocket.CONNECTING, WebSocket.OPEN].includes(state.ws.readyState)) return;
  if (state.wsReconnectTimer) {
    clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = null;
  }

  const generation = ++state.wsGeneration;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  setPanelStatus('connecting');
  const ws = new WebSocket(`${protocol}//${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    if (generation !== state.wsGeneration) return;
    state.wsLastMessageAt = Date.now();
    setPanelStatus('connected');
  };
  ws.onclose = () => {
    if (generation !== state.wsGeneration) return;
    state.ws = null;
    scheduleWsReconnect();
  };
  ws.onerror = () => {
    if (generation === state.wsGeneration) {
      try { ws.close(); } catch (e) {}
    }
  };
  ws.onmessage = (ev) => {
    if (generation !== state.wsGeneration) return;
    const uiRenderStarted = performance.now();
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    state.wsLastMessageAt = Date.now();
    state.wsRetryMs = 1000;
    setPanelStatus('connected');
    if (msg.type === 'heartbeat') return;
    if (msg.type !== 'snapshot') return;

    state.scanStatus = msg.scan_status || null;
    if (msg.config) state.config = { ...state.config, ...msg.config };
    syncRuntimeControlsFromStatus(state.scanStatus);
    const reportedBank = Number(state.scanStatus?.bank_usdt || msg.config?.bank || 0);
    const bankInput = document.getElementById('risk-total-bank');
    if (reportedBank > 0 && Number.isFinite(reportedBank)) {
      const changed = Math.abs(Number(state.config.bank) - reportedBank) >= 0.005;
      state.config.bank = reportedBank;
      if (bankInput) bankInput.value = reportedBank.toFixed(2);
      if (changed) updateBankLimit();
    }
    state.pinnedSignalSnapshots = Array.isArray(state.scanStatus?.pinned_signal_snapshots)
      ? state.scanStatus.pinned_signal_snapshots : [];
    state.opportunitiesBySlug = window.PolyArbMarketDisplay
      ? window.PolyArbMarketDisplay.indexOpportunities(msg.opportunities_v1)
      : new Map();
    const newArbs = Array.isArray(msg.arbs) ? msg.arbs : [];
    let hasNew = false;
    const newlySeen = [];
    for (const a of newArbs) {
      if (a && a.slug && !state.seenSlugs.has(a.slug)) {
        state.seenSlugs.add(a.slug);
        state.freshSlugs.add(a.slug);
        newlySeen.push(a);
        hasNew = true;
        setTimeout(() => state.freshSlugs.delete(a.slug), 2600);
      }
    }
    state.arbs = newArbs;
    if (state._primed && hasNew) playArbChime();
    applyFilters();
    if (state._primed && newlySeen.length) notifyNewSignals(newlySeen);
    state._primed = true;
    if (state.activeSlug || state.activeSnapshotId) refreshDetailThrottled();
    if (state.scanStatus) {
      state.scanStatus.ui_render_ms = Math.round((performance.now() - uiRenderStarted) * 10) / 10;
    }
    renderScanStatus();
  };
}

function checkWsFreshness() {
  const ws = state.ws;
  if (document.hidden || !ws || ws.readyState !== WebSocket.OPEN || !state.wsLastMessageAt) return;
  if (Date.now() - state.wsLastMessageAt <= 35000) return;
  setPanelStatus('stale');
  try { ws.close(4000, 'heartbeat timeout'); } catch (e) {}
}

function activeBookmakerList() {
  return BK_LIST;
}

/* ============= FILTERS ============= */
function applyFilters() {
  const sports = new Set();
  if (document.getElementById('f-cs2').checked) sports.add('CS2');
  if (document.getElementById('f-dota2').checked) sports.add('DOTA2');
  if (document.getElementById('f-lol').checked) sports.add('LOL');
  if (document.getElementById('f-valorant')?.checked) sports.add('VALORANT');
  if (document.getElementById('f-tennis').checked) sports.add('TENNIS');
  const minRoi = parseFloat(document.getElementById('f-minroi').value || '0') / 100;

  state.visible = state.arbs.filter(a => {
    const phase = ['live','prematch','unknown'].includes(a.phase) ? a.phase : 'unknown';
    if (state.phase !== 'all' && phase !== state.phase) return false;
    if (!sports.has(a.sport)) return false;
    if ((a.roi || 0) < minRoi) return false;
    if (!window.PolyArbSourceFilter.includesSelectedProvider(
      a, null, state.scanStatus?.enabled_bk_sources,
    )) return false;
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

  const phaseCounts = { all: state.arbs.length, live: 0, prematch: 0, unknown: 0 };
  for (const arb of state.arbs) {
    const phase = ['live','prematch','unknown'].includes(arb.phase) ? arb.phase : 'unknown';
    phaseCounts[phase] += 1;
  }
  for (const phase of Object.keys(phaseCounts)) {
    const count = document.getElementById(`phase-${phase}-count`);
    if (count) count.textContent = phaseCounts[phase];
  }

  if (state.visible.length) {
    topEl.textContent = fmtPct(state.visible[0].roi);
    topEl.style.color = state.visible[0].roi >= 0 ? 'var(--green)' : 'var(--red)';
  } else {
    topEl.textContent = '—';
    topEl.style.color = 'var(--txt2)';
  }
}

const SCAN_STATUS_STALE_SECONDS = 60;

function renderScanStatus() {
  const box = document.getElementById('scan-health');
  const status = state.scanStatus;
  if (!box) return;
  box.classList.remove('running', 'stale');
  if (!status || !Number.isFinite(Number(status.ts))) {
    document.getElementById('scan-health-mode').textContent = 'ОЖИДАНИЕ СКАНЕРА';
    document.getElementById('scan-health-label').textContent = 'Ожидание сканера';
    document.getElementById('scan-health-time').textContent = '—';
    document.getElementById('scan-checked').textContent = '—';
    document.getElementById('scan-checked-label').textContent = 'в пакете / каталог';
    document.getElementById('scan-quotes').textContent = '—';
    document.getElementById('scan-signals').textContent = '—';
    document.getElementById('scan-live').textContent = '—';
    document.getElementById('scan-prematch').textContent = '—';
    document.getElementById('scan-unknown').textContent = '—';
    document.getElementById('scan-reasons').textContent = 'Сканер ещё не передал статус цикла.';
    return;
  }

  const age = Math.max(0, Date.now() / 1000 - Number(status.ts));
  const starting = status.state === 'starting' && age <= SCAN_STATUS_STALE_SECONDS;
  const active = (status.state === 'running' || starting) && age <= SCAN_STATUS_STALE_SECONDS;
  box.classList.add(active ? 'running' : 'stale');
  document.getElementById('scan-health-mode').textContent = status.update_mode === 'event'
    ? 'СОБЫТИЙНОЕ ОБНОВЛЕНИЕ' : 'ЦИКЛ СКАНИРОВАНИЯ';
  document.getElementById('scan-health-label').textContent = starting
    ? 'Сканер запускается'
    : active
      ? status.update_mode === 'event'
      ? `Сканер активен · событие ${Number(status.cycle || 0)}`
      : `Сканер активен · цикл ${Number(status.cycle || 0)}`
    : status.state === 'stopped' ? 'Сканер остановлен' : 'Статус сканера устарел';
  document.getElementById('scan-health-time').textContent =
    new Date(Number(status.ts) * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const checks = status.checks || {};
  const phases = status.phase_counts || {};
  document.getElementById('scan-checked').textContent = status.update_mode === 'event'
    ? `${Number(status.processed_markets || 0)}/${Number(status.pm_markets || 0)}`
    : Number(status.pm_markets || 0);
  document.getElementById('scan-checked-label').textContent = status.update_mode === 'event'
    ? 'в пакете / каталог' : 'рынков проверено';
  document.getElementById('scan-quotes').textContent = Number(checks.with_fresh_bookmaker || 0);
  document.getElementById('scan-signals').textContent = Number(status.signals || 0);
  document.getElementById('scan-live').textContent = Number(phases.live || 0);
  document.getElementById('scan-prematch').textContent = Number(phases.prematch || 0);
  document.getElementById('scan-unknown').textContent = Number(phases.unknown || 0);

  const reasonLabels = [
    ['no_fresh_bookmaker', 'нет свежих котировок'],
    ['pm_no_ask', 'нет ask в PM-стакане'],
    ['pm_depth_short', 'недостаточно глубины PM'],
    ['pm_depth_rejects', 'объём не прошёл проверку стакана'],
    ['pm_impact_rejected', 'слишком высокий impact'],
    ['no_opposite_bk_market', 'нет парного исхода букмекера'],
    ['invalid_market_or_no_quotes', 'некорректная пара рынка'],
    ['invalid_calculation', 'ошибка расчёта, кандидат отклонён'],
    ['below_roi', 'ниже порога ROI'],
  ];
  const reasons = reasonLabels
    .map(([key, label]) => ({ count: Number(checks[key] || 0), label }))
    .filter(item => item.count > 0)
    .sort((a, b) => b.count - a.count)
    .map(item => `${item.label}: ${item.count}`);
  const effective = status.runtime_settings || {};
  const effectiveSources = effective.sources || {};
  const sourceLabel = key => effectiveSources[key] === 'cli' ? 'CLI' : 'config/default';
  if (effective.bank_usdt != null || effective.min_roi_pct != null) {
    reasons.unshift(
      `Активные настройки: банк ${Number(effective.bank_usdt ?? status.bank_usdt ?? 0).toFixed(2)} USDT (${sourceLabel('bank')}) · ` +
      `ROI ${Number(effective.min_roi_pct ?? status.min_roi_pct ?? 0).toFixed(2)}% (${sourceLabel('min_roi_pct')}) · ` +
      `poll ${Number(effective.poll_sec ?? 0).toFixed(2)} с (${sourceLabel('poll')}) · ` +
      `каталог ${Number(effective.watch_sec ?? 0)} с (${sourceLabel('watch_sec')}) · ` +
      `повтор ${Number(effective.alert_gap_sec ?? 0)} с (${sourceLabel('alert_gap')})`
    );
  }
  const runtimeLimits = status.runtime_limits || {};
  const freshnessLimits = runtimeLimits.freshness || {};
  const catalogLimits = runtimeLimits.catalog || {};
  const safetyLimits = runtimeLimits.safety || {};
  if (Object.keys(runtimeLimits).length) {
    const oneWinAgeSource = freshnessLimits.onewin_odds_age_source === 'env'
      ? 'переменная среды' : 'общий default';
    reasons.unshift(
      `Только чтение: PM TTL ${Number(freshnessLimits.polymarket_book_ttl_sec || 0)} с ` +
      `(диагностика ${Number(freshnessLimits.polymarket_stale_diagnostic_sec || 0)} с), ` +
      `источник stale после ${Number(freshnessLimits.dashboard_source_stale_sec || 0)} с · ` +
      `1win TTL ${Number(freshnessLimits.onewin_odds_max_age_sec || 0)} с (${oneWinAgeSource}) · ` +
      `Gamma ${Number(catalogLimits.gamma_events_per_page || 0)}/страницу × ${Number(catalogLimits.gamma_max_pages_per_query || 0)} страниц, ` +
      `${Number(catalogLimits.gamma_retries_per_page || 0)} повтора, ${Number(catalogLimits.gamma_workers || 0)} workers · ` +
      `риск-план ${Number(safetyLimits.bankroll_plan_pct || 0)}% банка`
    );
  }
  const configWarnings = Array.isArray(status.config_warnings) ? status.config_warnings : [];
  for (const warning of configWarnings.slice(0, 3).reverse()) {
    reasons.unshift(`Предупреждение настроек: ${warning}`);
  }
  if (status.pm_catalog_complete === false) {
    reasons.unshift(status.pm_catalog_retained
      ? 'каталог Polymarket неполный; прежние события временно сохранены'
      : 'каталог Polymarket неполный; состав событий может быть неполным');
  }
  const catalogFetch = status.pm_catalog_fetch || {};
  const catalogQueries = Number(catalogFetch.query_count || 0);
  const catalogFailures = Number(catalogFetch.failed_queries || 0);
  const catalogMarkets = Number(catalogFetch.markets || 0);
  if (catalogQueries > 0) {
    const errorTypes = Object.entries(catalogFetch.error_counts || {})
      .map(([name, count]) => `${name} ${Number(count)}`).slice(0, 2);
    const failedTags = (catalogFetch.failed_tags || []).slice(0, 3);
    const invalidRows = Number(catalogFetch.invalid_event_rows || 0);
    reasons.unshift(
      `Gamma keyset: страниц ${Number(catalogFetch.total_pages || 0)} · ` +
      `курсоры ${Number(catalogFetch.cursor_exhausted_queries || 0)}/${catalogQueries} · ` +
      `лимит ${Number(catalogFetch.page_limit || 0)} · рынков ${catalogMarkets} · ` +
      `${Number(catalogFetch.elapsed_ms || 0).toFixed(0)} мс` +
      (catalogFailures ? ` · неполных запросов ${catalogFailures}${errorTypes.length ? ` (${errorTypes.join(', ')})` : ''}` : '') +
      (invalidRows ? ` · пропущено event rows ${invalidRows}` : '') +
      (!catalogFailures && !invalidRows && catalogMarkets === 0 ? ' · активных рынков нет' : '') +
      (failedTags.length ? ` · теги ${failedTags.join('/')}` : '')
    );
  }
  const sources = Object.entries(status.source_counts || {})
    .filter(([, count]) => Number(count) > 0)
    .map(([name, count]) => `${name} ${Number(count)}`);
  if (sources.length) reasons.unshift(`Котировки: ${sources.join(' · ')}`);
  const matching = status.matching_stats || {};
  if (matching.pm_markets != null) {
    const freshPairsLabel = matching.fresh_1win_pairs == null
      ? '' : ` · свежие пары 1win ${Number(matching.fresh_1win_pairs || 0)}/${Number(matching.bound_1win || 0)}`;
    reasons.unshift(
      `Сопоставление PM: ${Number(matching.matched_pm_markets || 0)}/${Number(matching.pm_markets || 0)} · ` +
      `без пары ${Number(matching.unmatched_pm_markets || 0)} · ` +
      `1win/BetBoom ${Number(matching.bound_1win || 0)}/${Number(matching.bound_betboom || 0)} · ` +
      `каталоги ${Number(matching.onewin_catalog_matches || 0)}/${Number(matching.betboom_catalog_matches || 0)}` +
      freshPairsLabel
    );
  }
  const pairReasonLabels = {
    disconnected: 'нет соединения', not_bound: 'нет привязки',
    missing_match_details: 'нет данных матча', missing_market_group: 'нет группы рынка',
    ambiguous_market_groups: 'неоднозначные группы', ambiguous_side_quotes: 'неоднозначные исходы',
    unsupported_outcomes: 'лишние или нераспознанные исходы',
    one_side_not_fresh: 'одна сторона без свежей цены', no_fresh_sides: 'нет свежих сторон',
    diagnostic_error: 'ошибка диагностики',
  };
  const pairDiagnostics = matching.onewin_pair_diagnostics || {};
  const pairReasonCounts = Object.entries(pairDiagnostics.reason_counts || {})
    .filter(([reason, count]) => reason !== 'fresh' && Number(count) > 0)
    .map(([reason, count]) => `${pairReasonLabels[reason] || reason} ${Number(count)}`);
  if (pairReasonCounts.length) {
    reasons.unshift(`1win · привязанные PM-рынки: ${pairReasonCounts.join(' · ')}`);
  }
  const oneWinHealth = (status.bookmaker_feed_health || {})['1win'];
  if (oneWinHealth) {
    reasons.unshift(
      `1win feed ${oneWinHealth.connected ? 'соединён' : 'нет соединения'} · ` +
      `событий ${Number(oneWinHealth.catalog_matches || 0)} · ` +
      `в кэше свеж./устар. котировок ${Number(oneWinHealth.fresh_open_quotes || 0)}/` +
      `${Number(oneWinHealth.stale_open_quotes || 0)} · ` +
      `обновлений цены ${Number(oneWinHealth.valid_price_updates_received || 0)}`
    );
  }
  if (status.best_candidate_roi_pct != null && Number.isFinite(Number(status.best_candidate_roi_pct))) {
    const candidateAge = status.best_candidate_age_sec == null
      ? '' : ` · измерен ${Number(status.best_candidate_age_sec).toFixed(1)} с назад`;
    const batchCandidate = status.best_candidate_batch_roi_pct == null
      ? 'в пакете нет пригодной пары'
      : `ROI пакета ${Number(status.best_candidate_batch_roi_pct).toFixed(2)}%`;
    reasons.unshift(
      `Лучший ROI-кандидат в кэше ${Number(status.best_candidate_roi_pct).toFixed(2)}%` +
      candidateAge + ` · ${batchCandidate}`
    );
  }
  const transportLabels = {
    scanner_ws: 'прямой WebSocket',
    file_fallback: 'файловый fallback',
  };
  if (transportLabels[status.delivery_transport]) {
    const stateAge = status.scanner_state_age_ms == null
      ? '' : ` · snapshot ${Number(status.scanner_state_age_ms).toFixed(1)} мс`;
    reasons.unshift(`Доставка: ${transportLabels[status.delivery_transport]}${stateAge}`);
  }
  if (status.update_mode === 'event') {
    const timings = [];
    if (status.event_to_snapshot_ms != null && Number.isFinite(Number(status.event_to_snapshot_ms))) {
      timings.push(`очередь→снимок ${Number(status.event_to_snapshot_ms).toFixed(1)} мс`);
    }
    const sourceTimings = status.source_event_to_snapshot_ms || {};
    for (const source of ['1win']) {
      const value = Number(sourceTimings[source]);
      if (Number.isFinite(value)) {
        timings.push(`1win callback→снимок ${value.toFixed(1)} мс`);
      }
    }
    if (status.scanner_to_backend_ms != null && Number.isFinite(Number(status.scanner_to_backend_ms))) {
      timings.push(`сканер→сервер ${Number(status.scanner_to_backend_ms).toFixed(1)} мс`);
    }
    if (status.ui_render_ms != null && Number.isFinite(Number(status.ui_render_ms))) {
      timings.push(`DOM ${Number(status.ui_render_ms).toFixed(1)} мс`);
    }
    reasons.unshift(
      `Событийный пакет: ${Number(status.source_updates || 0)} обновл. · ` +
      `обработано ${Number(status.processed_markets || 0)}/${Number(status.pm_markets || 0)} · ` +
      `очередь ${Number(status.pending_updates || 0)}` +
      (timings.length ? ` · ${timings.join(' · ')}` : '')
    );
  }
  document.getElementById('scan-reasons').textContent = reasons.length
    ? reasons.join(' · ')
    : (status.signals ? 'Сигналы переданы в радар.'
      : status.update_mode === 'event' && Number(status.source_updates || 0) === 0
        ? 'Ожидание обновлений котировок; проверка свежести активных сигналов идёт отдельно.'
        : 'В этом цикле причины отсева не зафиксированы.');

  const dropBox = document.getElementById('scan-signal-drop');
  const recentDrops = Array.isArray(status.recent_signal_drops)
    ? status.recent_signal_drops.filter(drop => drop && typeof drop === 'object').slice(0, 5) : [];
  if (dropBox) {
    if (!recentDrops.length) {
      dropBox.textContent = '';
      dropBox.classList.add('hidden');
    } else {
      const reasonLabels = {
        market_removed: 'рынок исчез из PM-каталога',
        no_fresh_bookmaker_pair: 'нет свежей полной пары котировок БК',
        below_roi_threshold: 'ROI ниже установленного порога',
        pm_book_stale: 'PM-стакан устарел по TTL',
        pm_book_missing: 'PM-стакан отсутствует',
        pm_book_empty: 'PM-стакан пустой',
        pm_ask_unavailable: 'в PM не осталось исполнимого ask',
        pm_depth_short: 'недостаточная глубина PM',
        pm_impact_rejected: 'PM impact выше лимита',
        pm_depth_rejected: 'объём не прошёл PM depth check',
        no_opposite_bookmaker_outcome: 'нет противоположного исхода БК',
        invalid_candidate: 'кандидат не прошёл проверку расчёта',
        no_valid_candidate: 'нет валидной пары исходов',
      };
      const bookStateLabels = {
        stale_book: 'устарел', missing_book: 'отсутствует',
        missing_timestamp: 'без timestamp', empty_book: 'пустой', empty_asks: 'без asks',
      };
      const formatDrop = (drop) => {
        const pm = drop.pm_quote || {};
        const bookmaker = drop.bookmaker_quote || {};
        const age = status.ts != null && drop.dropped_at != null
          ? Math.max(0, Number(status.ts) - Number(drop.dropped_at)) : null;
        const parts = [reasonLabels[drop.reason] || 'сигнал перестал проходить проверку'];
        if (drop.signal_snapshot_age_sec != null) {
          parts.push(`последний расчёт ${Number(drop.signal_snapshot_age_sec).toFixed(1)}с до снятия`);
        }
        const currentPm = drop.current_pm_book;
        if (currentPm) {
          let currentPmText = `PM book ${bookStateLabels[currentPm.state] || currentPm.state}`;
          if (currentPm.age_sec != null) currentPmText += ` · ${Number(currentPm.age_sec).toFixed(1)}с`;
          if (currentPm.ask_levels != null) currentPmText += ` · asks ${Number(currentPm.ask_levels)}`;
          if (currentPm.rest_refresh && currentPm.rest_refresh !== 'not_requested') {
            if (currentPm.rest_refresh === 'queued') {
              const waiting = currentPm.rest_queue_age_sec;
              currentPmText += ` · REST в очереди${waiting != null ? ` ${Number(waiting).toFixed(1)}с` : ''}`;
            } else if (currentPm.rest_refresh === 'fetching') {
              const elapsed = currentPm.rest_fetch_age_sec;
              const waiting = currentPm.rest_queue_age_sec;
              currentPmText += ` · REST запрос выполняется${elapsed != null ? ` ${Number(elapsed).toFixed(1)}с` : ''}`;
              if (waiting != null && Number(waiting) > 0) currentPmText += ` (очередь ${Number(waiting).toFixed(1)}с)`;
            } else {
              currentPmText += ` · REST ${currentPm.rest_refresh}`;
            }
          }
          parts.push(currentPmText);
        }
        const currentPair = drop.current_onewin_pair;
        if (currentPair) {
          let pairText = `1win сейчас: ${pairReasonLabels[currentPair.reason] || currentPair.reason || 'неизвестно'}`;
          if (currentPair.odds_subscription_sent === false) pairText += ' · подписка на odds не отправлена';
          if (Array.isArray(currentPair.sides)) {
            const sideLabels = {
              fresh: 'свежая', stale: 'устарела', closed: 'закрыта',
              invalid_price: 'нет цены', missing_timestamp: 'нет времени цены',
              invalid_timestamp: 'некорректное время',
            };
            const sides = currentPair.sides.map(side => {
              const states = Object.entries(sideLabels)
                .filter(([key]) => Number(side[key]) > 0)
                .map(([key, label]) => `${label}${Number(side[key]) > 1 ? ` ×${Number(side[key])}` : ''}`);
              const ageText = side.last_quote_age_sec == null ? '' : ` ${Number(side.last_quote_age_sec).toFixed(1)}с`;
              return `${side.outcome || 'исход'}: ${states.join(', ') || 'нет котировки'}${ageText}`;
            });
            pairText += ` · ${sides.join(' / ')}`;
          }
          if (Number(currentPair.unmapped_outcomes) > 0) pairText += ` · нераспознанных исходов ${Number(currentPair.unmapped_outcomes)}`;
          parts.push(pairText);
        }
        const pmText = formatPmDropQuote(pm);
        if (pmText) parts.push(pmText);
        if (bookmaker.odds != null) {
          let bkText = `последний ${bookmaker.source || 'БК'} ${bookmaker.outcome || ''} ${Number(bookmaker.odds).toFixed(2)}×`;
          if (bookmaker.age_sec != null) bkText += ` · котировка ${Number(bookmaker.age_sec).toFixed(1)}с`;
          parts.push(bkText);
        }
        if (drop.reason === 'below_roi_threshold' && drop.current_best_roi_pct != null) {
          parts.push(`ROI ${Number(drop.current_best_roi_pct).toFixed(2)}% < ${Number(drop.min_roi_pct || 0).toFixed(2)}%`);
        } else if (drop.previous_roi_pct != null) {
          parts.push(`последний ROI ${Number(drop.previous_roi_pct).toFixed(2)}%`);
        }
        const title = String(drop.q || drop.slug || 'Событие').slice(0, 90);
        return `Снят${age == null ? '' : ` ${age.toFixed(0)}с назад`}: ${title} — ${parts.join(' · ')}`;
      };
      dropBox.textContent = `СНЯТЫЕ СИГНАЛЫ — НЕ АКТИВНЫЕ (до 5 минут)\n${recentDrops.map(formatDrop).join('\n')}`;
      dropBox.classList.remove('hidden');
    }
  }
}

/* ============= LIST ============= */
function createArbCard() {
  const card = document.createElement('div');
  card.className = 'arb';
  card.innerHTML = '<div class="arb-head"><div class="arb-title"></div><div class="arb-roi"></div></div>' +
    '<div class="arb-tags"></div><div class="arb-market"><span class="arb-market-label">РЫНОК</span><b class="arb-market-value"></b></div>' +
    '<div class="arb-meta"><div class="arb-bks"></div><span class="arb-age"></span></div>' +
    '<div class="arb-stakes"></div>';
  return card;
}

function renderArbLegRow(leg, stake) {
  if (!leg || !leg.source) return '';
  const view = window.PolyArbMarketDisplay.legDisplay(leg, stake);
  const amount = view.stake > 0 ? `${view.stake.toFixed(2)} USDT` : '—';
  const odds = view.odds > 0 ? `${fmtOdds(view.odds)}×` : '—';
  const oddsLabel = view.isPolymarket ? 'ЭФФ. КЭФ' : 'КЭФ';
  const pmPrice = view.pmPriceCents == null
    ? '' : `<small class="arb-pm-price">${view.pmPriceCents.toFixed(1)}¢ цена</small>`;
  const ageLabel = view.isPolymarket ? 'Стакан PM' : 'Котировка';
  const ageText = window.PolyArbQuoteQuality
    ? window.PolyArbQuoteQuality.formatAgeAtCalculation(view.quoteAgeSec, ageLabel)
    : `${ageLabel}: возраст неизвестен`;
  return `<div class="arb-leg ${view.isPolymarket ? 'pm' : 'bookmaker'}">` +
    `<div class="arb-leg-head"><span class="arb-leg-source">${escapeHtml(view.source)}</span>` +
    `<b class="arb-leg-outcome">${escapeHtml(view.outcome)}</b></div>` +
    `<div class="arb-leg-values"><span><small>СТАВКА</small><b>${amount}</b></span>` +
    `<span><small>${oddsLabel}</small><b>${odds}${pmPrice}</b></span></div>` +
    `<div class="arb-leg-age">${escapeHtml(ageText)}</div></div>`;
}

function setCardText(card, selector, value) {
  const node = card.querySelector(selector);
  const text = String(value == null ? '' : value);
  if (node && node.textContent !== text) node.textContent = text;
}

function setCardMarkup(card, selector, markup, cacheKey) {
  const node = card.querySelector(selector);
  if (node && node[cacheKey] !== markup) {
    node.innerHTML = markup;
    node[cacheKey] = markup;
  }
}

function phaseTag(row, detailed = false) {
  const phase = ['live', 'prematch', 'unknown'].includes(row.phase) ? row.phase : 'unknown';
  const labels = { live: 'LIVE', prematch: 'ПРЕМАТЧ',
    unknown: detailed ? 'СТАТУС НЕ ПОДТВЕРЖДЁН' : 'СТАТУС ?' };
  let label = labels[phase];
  let title = '';
  if (phase === 'prematch' && row.phase_source === '1win') {
    label += ' · 1win';
    title = 'Прематч по данным 1win';
    const diff = Number(row.phase_start_difference_sec);
    if (row.phase_start_difference_sec != null && Number.isFinite(diff) && diff !== 0) {
      title += `; время начала отличается от Polymarket на ${Math.abs(diff / 60).toFixed(1)} мин`;
    }
  } else if (phase === 'unknown' && row.phase_source === 'conflict') {
    title = 'Источники противоречат друг другу';
  }
  const attr = title ? ` title="${escapeHtml(title)}"` : '';
  return `<span class="tag phase-${phase}"${attr}>${label}</span>`;
}

function updateArbCard(card, a) {
  // Dual-read drives list values and validated event-link actions; other detail/calculator data remains legacy-backed.
  const legacy = a;
  const opportunity = state.opportunitiesBySlug.get(String(legacy.slug || ''));
  a = window.PolyArbMarketDisplay.opportunityCardDisplay(opportunity, legacy) || legacy;
  const slug = String(a.slug || '');
  const legs = Array.isArray(a.bets) ? a.bets : [];
  const pmLeg = legs.find(l => l && l.source === 'Polymarket') || {};
  const bbLeg = legs.find(l => l && l.source && l.source !== 'Polymarket') || {};
  const title = a.title && a.q && a.title !== a.q ? `${a.title} · ${a.q}` : (a.title || a.q || slug);
  const classes = ['arb'];
  if (slug === state.activeSlug) classes.push('active');
  if (state.freshSlugs.has(slug)) classes.push('fresh');

  if (card.className !== classes.join(' ')) card.className = classes.join(' ');
  card.dataset.arbSlug = slug;
  card.setAttribute('role', 'button');
  card.tabIndex = 0;
  card.setAttribute('aria-pressed', String(slug === state.activeSlug));
  card.setAttribute('aria-label', `${title}; ROI ${fmtPct(a.roi)}; нажмите Enter, чтобы открыть детали`);
  setCardText(card, '.arb-title', title);
  setCardText(card, '.arb-roi', fmtPct(a.roi));
  setCardText(card, '.arb-age', fmtAgo(a.age_sec));

  const tags = [phaseTag(a)];
  const freshness = (state.config && state.config.runtime_limits && state.config.runtime_limits.freshness)
    || (state.scanStatus && state.scanStatus.runtime_limits && state.scanStatus.runtime_limits.freshness)
    || {};
  const pmAge = pmBookAge(pmLeg, a);
  const bookmakerSource = String(bbLeg.source || a.bk_source || 'БК');
  const bookmakerAge = bbLeg.odds_age_sec != null ? bbLeg.odds_age_sec : a.bk_odds_age_sec;
  const pmStale = ageExceedsTtl(pmAge, freshness.polymarket_book_ttl_sec);
  const bookmakerTtl = bookmakerSource.toLowerCase() === '1win'
    ? freshness.onewin_odds_max_age_sec : null;
  const bookmakerStale = ageExceedsTtl(bookmakerAge, bookmakerTtl);
  const hasStaleLeg = pmStale || bookmakerStale;
  tags.push(freshnessBadge('PM', pmAge, freshness.polymarket_book_ttl_sec));
  tags.push(freshnessBadge(bookmakerSource, bookmakerAge, bookmakerTtl));
  const sourceAge = a.source_age_sec == null ? NaN : Number(a.source_age_sec);
  if (a.source_state === 'stale' && !hasStaleLeg) {
    const sourceLabel = Number.isFinite(sourceAge)
      ? `ДАННЫЕ ${fmtAgo(sourceAge)} · УСТАРЕЛИ` : 'ДАННЫЕ УСТАРЕЛИ';
    tags.push(`<span class="tag pm-age">${sourceLabel}</span>`);
  }
  const opportunityStatusLabels = {
    stale: 'КОТИРОВКИ УСТАРЕЛИ',
    suspended: 'ВОЗМОЖНОСТЬ ПРИОСТАНОВЛЕНА',
    withdrawn: 'ВОЗМОЖНОСТЬ СНЯТА',
  };
  if (opportunityStatusLabels[a.opportunity_status]
      && !(a.opportunity_status === 'stale' && hasStaleLeg)
      && !(a.opportunity_status === 'stale' && a.source_state === 'stale')) {
    tags.push(`<span class="tag pm-age">${opportunityStatusLabels[a.opportunity_status]}</span>`);
  }
  setCardMarkup(card, '.arb-tags', tags.join(''), '_renderedTags');
  setCardText(card, '.arb-market-value', window.PolyArbMarketDisplay.marketTypeLabel(a.market_type));

  const bkClass = bbLeg.source === '1win' ? 'onewin' : bbLeg.source === 'Polymarket' ? 'other' : 'bb';
  const chips = `<span class="bk-chip ${bkClass}">${escapeHtml(bbLeg.source || 'БК')}</span>` +
    '<span class="bk-chip pm">Polymarket</span>';
  setCardMarkup(card, '.arb-bks', chips, '_renderedBooks');
  const pmAsk = Number(pmLeg.pm_best_ask || a.pm_best_ask || 0);
  const pmAskText = pmAsk > 0 && pmAsk < 1 ? `${(pmAsk * 100).toFixed(1)}¢` : '—';
  const pmOdds = Number(pmLeg.o || 0);
  const bkOdds = Number(bbLeg.o || 0);
  const quickValues = `<span class="arb-quick-value pm">PM ${pmAskText}` +
    `${pmOdds > 0 ? ` · эфф. ${fmtOdds(pmOdds)}×` : ''}</span>` +
    `<span class="arb-quick-value bookmaker">${escapeHtml(bbLeg.source || 'БК')} ` +
    `${bkOdds > 0 ? `${fmtOdds(bkOdds)}×` : '—'}</span>`;
  setCardMarkup(card, '.arb-stakes', quickValues, '_renderedStakes');
}

function pinnedSnapshotId(row) {
  const bets = Array.isArray(row.bets) ? row.bets : [];
  const pmLeg = bets.find(leg => leg && leg.source === 'Polymarket') || {};
  const bookmakerLeg = bets.find(leg => leg && leg.source && leg.source !== 'Polymarket') || {};
  return String(row.snapshot_id || [
    row.slug || 'snapshot', pmLeg.token || '', pmLeg.outcome || '',
    bookmakerLeg.source || '', bookmakerLeg.outcome || '',
    row.locked_at || row.ts || '', row.expires_at || '',
  ].join('|'));
}

function currentPinnedSnapshots() {
  const now = Date.now() / 1000;
  const rows = Array.isArray(state.pinnedSignalSnapshots)
    ? state.pinnedSignalSnapshots.filter(row => row && typeof row === 'object' && Number(row.expires_at) > now)
    : [];
  state.pinnedSignalSnapshots = rows;
  return rows.slice().sort((a, b) => Number(b.locked_at || b.ts || 0) - Number(a.locked_at || a.ts || 0));
}

function updatePinnedSnapshotCard(card, row) {
  const id = pinnedSnapshotId(row);
  const selected = id === state.activeSnapshotId;
  const bets = Array.isArray(row.bets) ? row.bets : [];
  const stakes = Array.isArray(row.stakes) ? row.stakes : [];
  const pmLeg = bets.find(leg => leg && leg.source === 'Polymarket') || {};
  const bookmakerLeg = bets.find(leg => leg && leg.source && leg.source !== 'Polymarket') || {};
  const pmPrice = Number(pmLeg.pm_avg_ask || pmLeg.pm_best_ask || row.pm_best_ask || 0);
  const roiPct = Number(row.snapshot_roi_pct ?? Number(row.roi || 0) * 100);
  const expiresIn = Math.max(0, Math.ceil(Number(row.expires_at) - Date.now() / 1000));
  const lockedAt = Number(row.locked_at || row.ts || 0);
  const lockedText = lockedAt > 0 ? new Date(lockedAt * 1000).toLocaleTimeString('ru-RU') : '—';
  const title = row.title && row.q && row.title !== row.q
    ? `${row.title} · ${row.q}` : (row.title || row.q || row.slug || 'Событие');
  card.className = `arb pinned-snapshot-card${selected ? ' active' : ''}`;
  card.dataset.pinnedSnapshotId = id;
  delete card.dataset.arbSlug;
  card.setAttribute('role', 'button');
  card.tabIndex = 0;
  card.setAttribute('aria-pressed', String(selected));
  card.setAttribute('aria-label', `${title}; замороженный неактивный снимок; ROI ${roiPct.toFixed(2)}%; до снятия ${expiresIn} секунд; нажмите Enter, чтобы открыть сохранённые детали`);
  setCardText(card, '.arb-title', title);
  setCardText(card, '.arb-roi', `${roiPct.toFixed(2)}%`);
  setCardText(card, '.arb-age', `СНИМОК ${lockedText} · ${Math.floor(expiresIn / 60)}:${String(expiresIn % 60).padStart(2, '0')} ДО СНЯТИЯ`);

  const tags = [`<span class="tag ${escapeHtml(sportClass(row.sport))}">${escapeHtml(row.sport || '')}</span>`];
  tags.push(phaseTag(row));
  tags.push('<span class="tag snapshot-lock">СНИМОК · НЕАКТИВНО</span>');
  setCardMarkup(card, '.arb-tags', tags.join(''), '_renderedTags');
  setCardText(card, '.arb-market-value', window.PolyArbMarketDisplay.marketTypeLabel(row.market_type));

  const bkClass = bookmakerLeg.source === '1win' ? 'onewin' : 'bb';
  const chips = `<span class="bk-chip ${bkClass}">${escapeHtml(bookmakerLeg.source || 'БК')}</span>` +
    '<span class="bk-chip pm">Polymarket</span>';
  setCardMarkup(card, '.arb-bks', chips, '_renderedBooks');

  const legRows = bets.map((leg, index) => renderArbLegRow(leg, stakes[index])).filter(Boolean).join('');
  setCardMarkup(card, '.arb-stakes', legRows, '_renderedStakes');
}

function renderArbs() {
  const box = document.getElementById('arbs-list');
  if (!box) return;
  const items = state.visible.filter(a => a && a.slug);
  const pinnedItems = currentPinnedSnapshots();
  if (state.activeSnapshotId && !pinnedItems.some(row => pinnedSnapshotId(row) === state.activeSnapshotId)) {
    closeDetail();
    return;
  }
  if (!items.length && !pinnedItems.length) {
    const phaseNames = { all: 'всех фаз', live: 'LIVE', prematch: 'прематча', unknown: 'неподтверждённого статуса' };
    const message = state.arbs.length
      ? 'Проверьте фильтры спорта и минимального ROI.'
      : (state.scanStatus ? 'Причины и число проверенных рынков показаны в сводке цикла выше.' : 'Ожидание первого статуса сканера.');
    const markup = `<div class="empty"><b>${state.arbs.length ? 'Нет совпадений с фильтрами' : 'Нет прошедших проверку сигналов'}</b><br>` +
      `Раздел: ${escapeHtml(phaseNames[state.phase] || phaseNames.all)}.<small>${escapeHtml(message)}</small></div>`;
    if (box.dataset.emptyMarkup !== markup) {
      box.innerHTML = markup;
      box.dataset.emptyMarkup = markup;
    }
    return;
  }

  if (!box.dataset.eventsBound) {
    box.addEventListener('click', event => {
      const card = event.target.closest('[data-arb-slug], [data-pinned-snapshot-id]');
      if (!card || !box.contains(card)) return;
      if (card.dataset.pinnedSnapshotId) selectPinnedSnapshot(card.dataset.pinnedSnapshotId);
      else selectArb(card.dataset.arbSlug);
    });
    box.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const card = event.target.closest('[data-arb-slug], [data-pinned-snapshot-id]');
      if (!card || !box.contains(card)) return;
      event.preventDefault();
      if (card.dataset.pinnedSnapshotId) selectPinnedSnapshot(card.dataset.pinnedSnapshotId);
      else selectArb(card.dataset.arbSlug);
    });
    box.dataset.eventsBound = 'true';
  }

  delete box.dataset.emptyMarkup;
  box.querySelector(':scope > .empty')?.remove();
  if (!items.length && state.arbs.length) {
    const phaseNames = { all: 'всех фаз', live: 'LIVE', prematch: 'прематча', unknown: 'неподтверждённого статуса' };
    const empty = document.createElement('div');
    empty.className = 'empty active-empty';
    empty.innerHTML = `<b>Нет активных вилок по фильтрам</b><br>Раздел: ${escapeHtml(phaseNames[state.phase] || phaseNames.all)}.<small>Ниже сохранены неактивные снимки ROI ≥ 5%.</small>`;
    box.insertBefore(empty, box.firstChild);
  }
  const existing = new Map(Array.from(box.querySelectorAll(':scope > .arb[data-arb-slug]'))
    .map(card => [card.dataset.arbSlug, card]));
  const existingPinned = new Map(Array.from(box.querySelectorAll(':scope > .arb[data-pinned-snapshot-id]'))
    .map(card => [card.dataset.pinnedSnapshotId, card]));
  let position = 0;
  for (const a of items) {
    const slug = String(a.slug);
    let card = existing.get(slug);
    if (card) existing.delete(slug);
    else card = createArbCard();
    updateArbCard(card, a);
    const current = box.children[position];
    if (current !== card) box.insertBefore(card, current || null);
    position += 1;
  }
  for (const row of pinnedItems) {
    const id = pinnedSnapshotId(row);
    let card = existingPinned.get(id);
    if (card) existingPinned.delete(id);
    else card = createArbCard();
    updatePinnedSnapshotCard(card, row);
    const current = box.children[position];
    if (current !== card) box.insertBefore(card, current || null);
    position += 1;
  }
  for (const card of existing.values()) card.remove();
  for (const card of existingPinned.values()) card.remove();
}

/* ============= DETAIL ============= */
function selectArb(slug) {
  state.bookRequestId = (state.bookRequestId || 0) + 1;
  state.chartRequestId = (state.chartRequestId || 0) + 1;
  state.priceChartData = null;
  state.activeSlug = slug;
  state.activeSnapshotId = null;
  setEventActionLink(document.getElementById('btn-open-bb'), null, 'Определяется ссылка события');
  setEventActionLink(document.getElementById('btn-buy-pm'), null, 'Определяется ссылка события');
  state.detailEdited = false;
  state.calcEdited = false;
  state.suggestedCapped = false;
  const ph = document.getElementById('detail-placeholder');
  const body = document.getElementById('detail-body');
  if (ph) { ph.classList.add('hidden'); ph.style.display = 'none'; }     // ← inline!
  if (body) { body.classList.remove('hidden'); body.style.display = 'block'; }  // ← inline!
  renderArbs();
  refreshDetail();
}

function selectPinnedSnapshot(snapshotId) {
  const row = currentPinnedSnapshots().find(item => pinnedSnapshotId(item) === String(snapshotId));
  if (!row) return;
  state.bookRequestId = (state.bookRequestId || 0) + 1;
  state.chartRequestId = (state.chartRequestId || 0) + 1;
  state.priceChartData = null;
  state.activeSlug = null;
  state.activeSnapshotId = String(snapshotId);
  setEventActionLink(document.getElementById('btn-open-bb'), null, 'Определяется ссылка события');
  setEventActionLink(document.getElementById('btn-buy-pm'), null, 'Определяется ссылка события');
  state.detailEdited = false;
  state.calcEdited = false;
  state.suggestedCapped = false;
  const ph = document.getElementById('detail-placeholder');
  const body = document.getElementById('detail-body');
  if (ph) { ph.classList.add('hidden'); ph.style.display = 'none'; }
  if (body) { body.classList.remove('hidden'); body.style.display = 'block'; }
  renderArbs();
  refreshDetail();
}

function closeDetail() {
  state.bookRequestId = (state.bookRequestId || 0) + 1;
  state.chartRequestId = (state.chartRequestId || 0) + 1;
  state.priceChartData = null;
  state.activeSlug = null;
  state.activeSnapshotId = null;
  const ph = document.getElementById('detail-placeholder');
  const body = document.getElementById('detail-body');
  if (ph) { ph.classList.remove('hidden'); ph.style.display = 'flex'; }
  if (body) { body.classList.add('hidden'); body.style.display = 'none'; }
  setEventActionLink(document.getElementById('btn-open-bb'), null, 'Сначала выберите событие');
  setEventActionLink(document.getElementById('btn-buy-pm'), null, 'Сначала выберите событие');
  renderArbs();
}

function getActive() {
  if (state.activeSnapshotId) {
    const row = currentPinnedSnapshots().find(item => pinnedSnapshotId(item) === state.activeSnapshotId);
    return row ? { ...row, inactive_snapshot: true } : null;
  }
  return state.arbs.find(a => a.slug === state.activeSlug);
}

function activeOpportunityEventUrl(active, source) {
  if (!active || active.inactive_snapshot || !state.opportunitiesBySlug
      || !window.PolyArbMarketDisplay) return null;
  const opportunity = state.opportunitiesBySlug.get(String(active.slug || ''));
  const adapted = window.PolyArbMarketDisplay.opportunityCardDisplay(
    opportunity, active,
  );
  if (!adapted) return null;
  const leg = (opportunity.legs || []).find(item => item && item.source === source);
  return leg && typeof leg.event_url === 'string' && leg.event_url.trim()
    ? leg.event_url.trim() : null;
}

function refreshDetailThrottled() {
  // Apply every scanner snapshot immediately; REST book/chart refreshes are
  // deliberately not coupled to this live quote render.
  refreshDetail({ loadRemote: false });
}

function refreshDetail(options = {}) {
  const loadRemote = options.loadRemote !== false;
  const a = getActive();
  if (!a) { closeDetail(); return; }
  const frozen = Boolean(a.inactive_snapshot);

  const eventTitle = a.title && a.q && a.title !== a.q
    ? `${a.title} · ${a.q}` : (a.title || a.q || a.slug);
  document.getElementById('det-title').textContent = eventTitle + (frozen ? ' · ЗАКРЕПЛЁННЫЙ СНИМОК' : '');
  const tags = [`<span class="tag mtype">${escapeHtml(window.PolyArbMarketDisplay.marketTypeLabel(a.market_type))}</span>`,
                `<span class="tag ${escapeHtml(sportClass(a.sport))}">${escapeHtml(a.sport || '')}</span>`];
  tags.push(phaseTag(a, true));
  if (a.is_streamer) tags.push(`<span class="tag streamer">СТРИМЕР</span>`);
  if (a.source_state === 'stale') tags.push(`<span class="tag pm-age">ДАННЫЕ УСТАРЕЛИ</span>`);
  else if (a.source_state === 'unknown') tags.push(`<span class="tag pm-age">СВЕЖЕСТЬ НЕИЗВЕСТНА</span>`);
  if (frozen) tags.push('<span class="tag snapshot-lock">СНИМОК · НЕАКТИВНО</span>');
  document.getElementById('det-tags').innerHTML = tags.join('');

  const lockedAt = Number(a.locked_at || a.ts || 0);
  const remaining = Math.max(0, Math.ceil(Number(a.expires_at || 0) - Date.now() / 1000));
  const snapshotWarning = document.getElementById('snapshot-warning');
  if (snapshotWarning) {
    snapshotWarning.classList.toggle('hidden', !frozen);
    if (frozen) {
      const lockedText = lockedAt > 0 ? new Date(lockedAt * 1000).toLocaleTimeString('ru-RU') : '—';
      snapshotWarning.textContent = `ЗАМОРОЖЕННЫЙ СНИМОК · НЕАКТИВЕН · зафиксирован ${lockedText} · будет удалён через ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}. Сохранённые котировки не являются текущими — перепроверьте цены на площадках.`;
    }
  }

  const roiEl = document.getElementById('kpi-roi');
  roiEl.textContent = fmtPct(a.roi);
  roiEl.style.color = a.roi >= 0 ? 'var(--green)' : 'var(--red)';
  document.getElementById('kpi-age').textContent = frozen
    ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}` : fmtAgo(a.age_sec);
  document.getElementById('kpi-age-label').textContent = frozen ? 'СНИМУТ ЧЕРЕЗ' : 'ЖИВЁТ';

  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};

  const pmAsk = Number(pmLeg.pm_best_ask || a.pm_best_ask || 0);
  document.getElementById('kpi-depth-label').textContent = frozen ? 'ASK В СНИМКЕ' : 'ЛУЧШИЙ ASK';
  document.getElementById('kpi-depth').textContent = pmAsk ? (pmAsk * 100).toFixed(1) + '¢' : '—';

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
  updateEventActionLinks(a);
  if (frozen) {
    state.bookRequestId = (state.bookRequestId || 0) + 1;
    state.chartRequestId = (state.chartRequestId || 0) + 1;
    showSnapshotMarketData();
  } else {
    showLiveMarketData();
    const showingLive = renderLivePmBook(a);
    const bookFoot = document.getElementById('pm-book-foot');
    if (!showingLive && bookFoot?.textContent.startsWith('LIVE-кэш')) {
      bookFoot.textContent = 'LIVE-стакан устарел · ожидание обновления REST';
      document.getElementById('pm-book-body').innerHTML = '<tr><td colspan="3" class="empty-cell">Стакан устарел</td></tr>';
    }
    if (loadRemote) {
      loadPmBook(a.slug);
      loadPriceChart(a.slug);
    }
  }
}

function showSnapshotMarketData() {
  state.priceChartData = null;
  document.getElementById('pm-book-title').textContent = 'СТАКАН POLYMARKET · СНИМОК';
  document.getElementById('pm-book-head').hidden = true;
  document.getElementById('pm-book-body').innerHTML = '<tr><td colspan="3" class="empty-cell snapshot-empty-cell">Актуальный стакан для сохранённого снимка не загружается.</td></tr>';
  document.getElementById('pm-book-foot').textContent = 'Котировки в расчёте сохранены на момент снимка';
  document.getElementById('price-chart').hidden = true;
  document.getElementById('chart-snapshot-placeholder').hidden = false;
  document.getElementById('chart-title').textContent = 'ИСТОРИЯ ЦЕНЫ';
  document.getElementById('chart-foot').textContent = 'Снимок не содержит актуальной истории котировок';
  document.getElementById('chart-delta').textContent = 'неактивно';
}

function showLiveMarketData() {
  document.getElementById('pm-book-title').textContent = 'СТАКАН POLYMARKET';
  document.getElementById('pm-book-head').hidden = false;
  document.getElementById('price-chart').hidden = false;
  document.getElementById('chart-snapshot-placeholder').hidden = true;
  document.getElementById('chart-title').textContent = 'ИСТОРИЯ ЦЕНЫ';
}

/* ============= PM BOOK ============= */
function runtimeFreshnessLimit(name, fallback) {
  const freshness = state.scanStatus?.runtime_limits?.freshness
    || state.config?.runtime_limits?.freshness || {};
  const value = Number(freshness[name]);
  return Number.isFinite(value) && value > 0 && value <= 600 ? value : fallback;
}

function livePmBookFor(a, pmLeg) {
  const book = a?.pm_book_snapshot;
  if (!a || a.inactive_snapshot || state.wsState !== 'connected' || !pmLeg?.token
      || !book || String(book.token) !== String(pmLeg.token) || !Array.isArray(book.asks)) return null;
  const receivedAt = Number(book.received_at);
  const age = Date.now() / 1000 - receivedAt;
  const ttl = runtimeFreshnessLimit('polymarket_book_ttl_sec', 30);
  if (!Number.isFinite(receivedAt) || receivedAt <= 0 || !Number.isFinite(age)
      || age < -0.5 || age > ttl) return null;
  return book;
}

function bookmakerQuoteIsFresh(a, bbLeg) {
  if (!a || !bbLeg || state.wsState !== 'connected') return false;
  const snapshotAt = Number(a.ts);
  const ageValue = bbLeg.odds_age_sec ?? a.bk_odds_age_sec;
  if (ageValue == null || ageValue === '') return false;
  const quotedAge = Number(ageValue);
  const elapsed = Date.now() / 1000 - snapshotAt;
  const ttl = bbLeg.source === '1win'
    ? runtimeFreshnessLimit('onewin_odds_max_age_sec', 30)
    : runtimeFreshnessLimit('dashboard_source_stale_sec', 10);
  return Number.isFinite(snapshotAt) && snapshotAt > 0 && Number.isFinite(quotedAge)
    && quotedAge >= 0 && Number.isFinite(elapsed) && elapsed >= -0.5
    && Math.max(0, elapsed) + quotedAge <= ttl;
}

function pmQuoteForEnteredAmount(a, pmLeg, amount) {
  const stake = Number(amount);
  if (!a?.slug || !pmLeg?.token || !Number.isFinite(stake) || stake < 1 || stake > 100000) return null;
  const live = livePmBookFor(a, pmLeg);
  const schedule = getPmFeeSchedule(pmLeg);
  const liveQuote = live && schedule && window.PolyArbPmDepthQuote
    ? window.PolyArbPmDepthQuote.quote(live.asks, stake, schedule) : null;
  if (liveQuote && (liveQuote.full || !live.asks_truncated)) {
    return {...liveQuote, source: 'live', truncated: Boolean(live.asks_truncated)};
  }
  const cached = pmBookCache[a.slug];
  const ageMs = Date.now() - Number(cached?.fetched_at);
  const avg = cached?.avg_for_size;
  const cachedSchedule = normalizePmFeeSchedule(cached?.fee_schedule);
  const feeScheduleMatches = !schedule || (cachedSchedule && schedule.enabled === cachedSchedule.enabled
    && schedule.rate === cachedSchedule.rate && schedule.exponent === cachedSchedule.exponent);
  const newerLiveBook = live && Number(live.received_at) * 1000 > Number(cached?.fetched_at);
  if (cached && String(cached.token_id) === String(pmLeg.token)
      && cached.outcome === pmLeg.outcome && Number.isFinite(ageMs) && ageMs >= 0
      && ageMs <= runtimeFreshnessLimit('polymarket_book_ttl_sec', 30) * 1000
      && !newerLiveBook
      && Math.abs(Number(cached.requested_usd) - stake) <= Math.max(1e-9, stake * 1e-12)
      && cachedSchedule && feeScheduleMatches
      && avg && Number.isFinite(Number(avg.shares)) && Number(avg.shares) > 0
      && Number.isFinite(Number(avg.total_cost)) && Number(avg.total_cost) > 0
      && avg.fee_estimate != null && Number.isFinite(Number(avg.fee_estimate)) && Number(avg.fee_estimate) >= 0
      && Number.isFinite(Number(avg.avg_price)) && Number(avg.avg_price) > 0 && Number(avg.avg_price) < 1
      && Number.isFinite(Number(avg.odds_eff)) && Number(avg.odds_eff) > 0
      && typeof avg.full === 'boolean') {
    return {
      ...avg, shares: Number(avg.shares), fee: Number(avg.fee_estimate),
      available_cash_usd: Number(cached.ask_depth_usd), source: 'rest', truncated: false,
    };
  }
  return liveQuote ? {...liveQuote, source: 'live', truncated: Boolean(live.asks_truncated)} : null;
}

function renderLivePmBook(a) {
  const pmLeg = (a?.bets || []).find(leg => leg.source === 'Polymarket') || {};
  const book = livePmBookFor(a, pmLeg);
  if (!book) return false;
  const pmUsd = Number(document.getElementById('hm-pm-stake')?.value || 0);
  let cumulative = 0;
  const rows = book.asks.slice(0, 7).map(level => {
    const price = Number(level.price), size = Number(level.size);
    if (!Number.isFinite(price) || !Number.isFinite(size) || price <= 0 || price >= 1 || size <= 0) return '';
    const inOrder = cumulative < pmUsd;
    const cost = price * size;
    cumulative += cost;
    return `<tr class="${inOrder ? 'in-order' : 'out-order'}"><td>${(price * 100).toFixed(1)}¢</td><td>${Math.round(size)}</td><td>${cost.toFixed(2)}</td></tr>`;
  }).join('');
  const tbody = document.getElementById('pm-book-body');
  const html = rows || '<tr><td colspan="3" class="empty-cell">пусто</td></tr>';
  if (tbody.innerHTML !== html) tbody.innerHTML = html;
  const age = Math.max(0, Date.now() / 1000 - Number(book.received_at));
  const suffix = book.asks_truncated ? ' · передана часть уровней' : '';
  document.getElementById('pm-book-foot').textContent =
    `LIVE-кэш сканера · ${age.toFixed(1)}с · уровней ${Math.min(book.asks.length, 7)}/${Number(book.total_ask_levels || book.asks.length)}${suffix}`;
  return true;
}

async function loadPmBook(slug) {
  const enteredUsd = parseFloat(document.getElementById('hm-pm-stake')?.value || '0');
  const requestedUsd = Number.isFinite(enteredUsd) && enteredUsd >= 1 && enteredUsd <= 100000
    ? enteredUsd : 50;
  const active = getActive();
  const activePmLeg = (active?.bets || []).find(leg => leg.source === 'Polymarket') || {};
  if (!active || active.inactive_snapshot || active.slug !== slug || !activePmLeg.token) return;
  const requestedToken = String(activePmLeg.token);
  const requestedOutcome = String(activePmLeg.outcome || '');
  const liveBook = active?.slug === slug ? livePmBookFor(active, activePmLeg) : null;
  const liveQuote = liveBook && window.PolyArbPmDepthQuote
    ? window.PolyArbPmDepthQuote.quote(liveBook.asks, requestedUsd, getPmFeeSchedule(activePmLeg)) : null;
  if (liveBook && liveQuote && (liveQuote.full || !liveBook.asks_truncated)) {
    renderLivePmBook(active);
    return;
  }
  if (!state.bookInFlightKeys) state.bookInFlightKeys = new Map();
  const requestKey = JSON.stringify([slug, requestedToken, requestedOutcome, requestedUsd]);
  if (state.bookInFlightKeys.has(requestKey) && state.bookInFlightKeys.get(requestKey) === state.bookRequestId) return;
  const requestId = state.bookRequestId = (state.bookRequestId || 0) + 1;
  state.bookInFlightKeys.set(requestKey, requestId);
  const tbody = document.getElementById('pm-book-body');
  const foot = document.getElementById('pm-book-foot');
  if (!pmBookCache[slug]) {
    tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Загрузка…</td></tr>';
    foot.textContent = 'Получение REST-стакана…';
  }
  const requestStartedAt = Date.now();
  try {
    const r = await fetch(`/api/pm_book/${encodeURIComponent(slug)}?size_usd=${requestedUsd}`);
    const j = await r.json();
    if (requestId !== state.bookRequestId || state.activeSlug !== slug) return;
    if (!j.ok || !j.books || !j.books.length) {
      if (!pmBookCache[slug]) tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Стакан недоступен</td></tr>';
      foot.textContent = pmBookCache[slug]
        ? 'REST не обновился · показаны предыдущие данные'
        : 'Стакан недоступен';
      return;
    }
    const a = getActive();
    if (!a || a.slug !== slug) return;
    const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
    if (String(pmLeg.token || '') !== requestedToken || String(pmLeg.outcome || '') !== requestedOutcome) return;
    const book = j.books.find(b => pmLeg.token && String(b.token_id) === String(pmLeg.token)
      && (b.outcome || '').trim() === (pmLeg.outcome || '').trim());
    if (!book) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Исход в стакане не найден</td></tr>';
      return;
    }

    pmBookCache[slug] = {
      outcome: pmLeg.outcome,
      token_id: book.token_id,
      fee_schedule: j.fee_schedule,
      best_ask: book.best_ask,
      best_bid: book.best_bid,
      ask_depth_usd: book.ask_depth_usd,
      avg_for_size: book.avg_for_size,
      requested_usd: requestedUsd,
      fetched_at: requestStartedAt,
      fetch_ms: Number(j.fetch_ms),
    };
    renderHowMuch(a);
    if (renderLivePmBook(a)) return;

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
        '<td>' + usd.toFixed(2) + '</td>' +
      '</tr>';
    }
    const newRows = rows || '<tr><td colspan="3" class="empty-cell">пусто</td></tr>';
    if (tbody.innerHTML !== newRows) tbody.innerHTML = newRows;

    const totalUsd = (book.ask_depth_usd !== undefined && book.ask_depth_usd !== null)
      ? book.ask_depth_usd
      : asks.reduce((s, l) => s + l.price * l.size, 0);
    const totalLevels = (book.asks || []).length;
    let footTxt = 'показано ' + Math.min(totalLevels, 7) + ' уровней · глубина ask ' + Number(totalUsd).toFixed(2) + ' USDT';
    if (book.avg_for_size && !book.avg_for_size.full) footTxt += ' · недостаточно для ' + requestedUsd.toFixed(2) + ' USDT';
    if (Number.isFinite(Number(j.fetch_ms))) footTxt += ` · API ${Number(j.fetch_ms).toFixed(0)} мс`;
    foot.textContent = footTxt;
    const avg = book.avg_for_size;
    document.getElementById('hm-avg').textContent = avg && avg.avg_price
      ? (avg.avg_price * 100).toFixed(1) + '¢ · ' + requestedUsd.toFixed(2) + ' USDT' + (avg.full ? '' : ' · неполный объём') : '—';
  } catch (e) {
    if (requestId === state.bookRequestId && state.activeSlug === slug) {
      if (!pmBookCache[slug]) tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Ошибка сети</td></tr>';
      foot.textContent = pmBookCache[slug]
        ? 'Ошибка обновления REST · показаны предыдущие данные'
        : 'Ошибка сети';
    }
  } finally {
    if (state.bookInFlightKeys.get(requestKey) === requestId) state.bookInFlightKeys.delete(requestKey);
  }
}

/* ============= PRICE CHART ============= */
function preparePriceChart(canvas) {
  if (!canvas || canvas.hidden) return null;
  const w = Number(canvas.clientWidth), h = Number(canvas.clientHeight);
  if (!(w > 0 && h > 0)) return null;
  const deviceScale = Number(window.devicePixelRatio);
  const scale = Number.isFinite(deviceScale) && deviceScale > 0 ? Math.min(deviceScale, 4) : 1;
  canvas.width = Math.max(1, Math.round(w * scale));
  canvas.height = Math.max(1, Math.round(h * scale));
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(canvas.width / w, 0, 0, canvas.height / h, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function observePriceChartResize() {
  const canvas = document.getElementById('price-chart');
  if (!canvas || typeof window.ResizeObserver !== 'function') return;
  state.chartResizeObserver = new window.ResizeObserver(() => {
    const data = state.priceChartData;
    if (data && state.activeSlug === data.slug && !canvas.hidden) drawPriceChart(data.points);
  });
  state.chartResizeObserver.observe(canvas);
}

function drawPriceChart(pts) {
  const chart = preparePriceChart(document.getElementById('price-chart'));
  if (!chart) return;
  const { ctx, w, h } = chart;
  const foot = document.getElementById('chart-foot');
  const delta = document.getElementById('chart-delta');

  if (pts.length < 2) {
    ctx.fillStyle = '#b8c8d1';
    ctx.font = '12px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Недостаточно данных', w / 2, h / 2, Math.max(1, w - 20));
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
}

async function loadPriceChart(slug) {
  const requestId = state.chartRequestId = (state.chartRequestId || 0) + 1;
  state.priceChartData = null;
  preparePriceChart(document.getElementById('price-chart'));
  try {
    const r = await fetch(`/api/price_history/${encodeURIComponent(slug)}?max_points=120`);
    const j = await r.json();
    if (requestId !== state.chartRequestId || state.activeSlug !== slug) return;
    const pts = (j.points || []).map(p => p.ask).filter(v => v > 0);
    state.priceChartData = { slug, points: pts };
    drawPriceChart(pts);
  } catch (e) {
    if (requestId === state.chartRequestId && state.activeSlug === slug) {
      document.getElementById('chart-foot').textContent = 'ошибка';
    }
  }
}

/* ============= HOW MUCH TO BET ============= */
function renderHowMuch(a) {
  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};
  const frozen = Boolean(a.inactive_snapshot);
  document.getElementById('hm-bb-name').textContent = bbLeg.source || 'Букмекер';
  document.getElementById('hm-bb-outcome').textContent = bbLeg.outcome || '—';
  document.getElementById('hm-pm-outcome').textContent = pmLeg.outcome || '—';
  const quoteScope = frozen
    ? `snapshot:${state.activeSnapshotId || a.slug}` : `active:${a.slug}`;
  const bookmakerOdds = bbLeg.o ? `${fmtOdds(bbLeg.o)}×` : '—';
  const polymarketOdds = pmLeg.o ? `${fmtOdds(pmLeg.o)}×` : '—';
  setLiveQuoteValue(document.getElementById('hm-bb-odds'), bookmakerOdds, quoteScope);
  document.getElementById('kpi-bookmaker-label').textContent = bbLeg.source || 'БК';
  setLiveQuoteValue(document.getElementById('kpi-bookmaker-odds'), bookmakerOdds, quoteScope);
  setLiveQuoteValue(document.getElementById('kpi-polymarket-odds'), polymarketOdds, quoteScope);
  document.getElementById('market-label').textContent = window.PolyArbMarketDisplay.marketTypeLabel(a.market_type);
  document.getElementById('market-bk-name').textContent = bbLeg.source || 'Букмекер';
  document.getElementById('market-bk-outcome').textContent = bbLeg.outcome || '—';
  document.getElementById('market-pm-outcome').textContent = pmLeg.outcome || '—';
  document.getElementById('kpi-odds-label').textContent = frozen ? 'КЭФЫ В СНИМКЕ' : 'КЭФЫ СКАНЕРА';
  document.getElementById('hm-title').textContent = frozen ? 'СОХРАНЁННЫЕ СТАВКИ' : 'СКОЛЬКО СТАВИТЬ';
  document.getElementById('hm-rate').textContent = frozen ? 'СУММЫ ЗАМОРОЖЕНЫ · USDT' : 'суммы в USDT';

  const stakes = a.stakes || [0, 0];
  let pmSt = 0, bbSt = 0;
  (a.bets || []).forEach((l, i) => {
    if (l.source === 'Polymarket') pmSt = stakes[i] || 0;
    if (l.source && l.source !== 'Polymarket') bbSt = stakes[i] || 0;
  });
  const bbInput = document.getElementById('hm-bb-stake');
  const pmInput = document.getElementById('hm-pm-stake');
  bbInput.readOnly = frozen;
  pmInput.readOnly = frozen;
  syncNumberStepperState(bbInput);
  syncNumberStepperState(pmInput);
  if (frozen) {
    bbInput.value = Number(bbSt).toFixed(2);
    pmInput.value = Number(pmSt).toFixed(2);
  } else if (!state.detailEdited) {
    const cap = getPlanCap();
    const fitStakes = window.PolyBankrollPlanner?.fitSuggestedStakes;
    const plan = typeof fitStakes === 'function'
      ? fitStakes(bbSt, pmSt, cap)
      : { bookmakerUsdt: 0, polymarketUsdt: 0, capped: false };
    state.suggestedCapped = plan.capped;
    document.getElementById('hm-bb-stake').value = Number(plan.bookmakerUsdt).toFixed(2);
    document.getElementById('hm-pm-stake').value = Number(plan.polymarketUsdt).toFixed(2);
  }
  // лучшее ask из стакана, а не среднее по $50
  const candidateCache = pmBookCache[state.activeSlug];
  const cachedBook = !frozen && candidateCache?.outcome === pmLeg.outcome
    && String(candidateCache?.token_id) === String(pmLeg.token) ? candidateCache : null;
  const liveBook = !frozen ? livePmBookFor(a, pmLeg) : null;
  const pmBestAsk = Number(liveBook?.asks?.[0]?.price || pmLeg.pm_best_ask || a.pm_best_ask || cachedBook?.best_ask || 0);
  const pmPriceLabel = document.getElementById('hm-pm-price-label');
  if (pmPriceLabel) pmPriceLabel.textContent = frozen ? 'ASK В СНИМКЕ' : 'ЦЕНА ДОЛИ · ASK';
  const pmPriceText = pmBestAsk > 0 && pmBestAsk < 1 ? (pmBestAsk * 100).toFixed(1) + '¢' : '—';
  setLiveQuoteValue(document.getElementById('hm-pm-price'), pmPriceText, quoteScope);
  const enteredUsd = Number(document.getElementById('hm-pm-stake').value);
  const depthQuote = !frozen ? pmQuoteForEnteredAmount(a, pmLeg, enteredUsd) : null;
  if (frozen) {
    const capturedAvg = Number(pmLeg.pm_refined_avg_ask || pmLeg.pm_avg_ask || 0);
    document.getElementById('hm-avg-label').textContent = 'Средняя при фиксации';
    document.getElementById('hm-avg').textContent = capturedAvg > 0
      ? (capturedAvg * 100).toFixed(1) + '¢ · сохранённое значение' : '—';
  } else {
    document.getElementById('hm-avg-label').textContent = 'Средняя по стакану';
    document.getElementById('hm-avg').textContent = depthQuote && Number(depthQuote.avg_price) > 0
      ? (Number(depthQuote.avg_price) * 100).toFixed(1) + '¢ · ' + enteredUsd.toFixed(2) + ' USDT' + (depthQuote.full ? '' : ' · неполный объём') : '—';
  }
  const sharesPrice = Number(pmLeg.pm_avg_ask || pmLeg.pm_best_ask || a.pm_best_ask || 0);
  const capturedShares = Number(pmLeg.pm_refined_shares || pmLeg.pm_target_shares || 0);
  const shares = frozen ? (capturedShares > 0 ? capturedShares : (sharesPrice > 0 ? pmSt / sharesPrice : 0))
    : (depthQuote?.full ? Number(depthQuote.shares) : 0);
  document.getElementById('hm-shares-label').textContent = frozen ? 'Доли в снимке' : 'Оценка долей PM';
  document.getElementById('hm-shares').textContent = shares > 0
    ? `${shares.toLocaleString('ru-RU', { maximumFractionDigits: 4 })} долей` : '—';
  const pmSlippage = Number(pmLeg.s ?? 0);
  const baseOdds = frozen ? Number(pmLeg.o) : (depthQuote?.full ? Number(depthQuote.odds_eff) : 0);
  const effectiveOdds = Number.isFinite(pmSlippage) && pmSlippage >= 0 && pmSlippage < 1
    ? baseOdds * (1 - pmSlippage) : 0;
  setLiveQuoteValue(document.getElementById('hm-eff'), effectiveOdds > 0 ? fmtOdds(effectiveOdds) : '—', quoteScope);
  const fee = frozen && pmLeg.pm_fee_estimated_usd != null
    ? Number(pmLeg.pm_fee_estimated_usd) : (!frozen && depthQuote?.full ? Number(depthQuote.fee) : NaN);
  const feeEl = document.getElementById('hm-fee');
  if (feeEl) feeEl.textContent = Number.isFinite(fee) && fee >= 0 ? fmtMoney(fee) : '—';
  const depth = Number(depthQuote?.available_cash_usd);
  if (!frozen) {
    const hasDepth = Number.isFinite(depth) && depth > 0;
    document.getElementById('kpi-depth-label').textContent = hasDepth && depthQuote.truncated ? 'МИН. ГЛУБИНА PM' : 'ДОСТУПНО В PM';
    document.getElementById('kpi-depth').textContent = hasDepth
      ? (depthQuote.truncated ? '≥ ' : '') + fmtMoney(depth) : '—';
  }

  recalcHowMuch();

  const actionHint = document.getElementById('action-hint');
  if (actionHint) actionHint.textContent = frozen
    ? 'Это сохранённый неактивный снимок, не текущие котировки. Ссылки открывают площадки вручную; перед любыми действиями сверьте исходы и цены. Автоматических ставок нет.'
    : `${depthQuote?.source === 'live' ? 'Оценка по LIVE-кэшу сканера.' : depthQuote?.source === 'rest' ? 'Оценка по резервному REST-стакану.' : 'Ожидание свежего расчёта PM.'} ` +
      'Это не гарантия исполнения: перед действиями сверьте обе стороны, комиссию и объём. Сделка записывается вручную.';

  const buyBtn = document.getElementById('btn-buy-pm');
  buyBtn.textContent = `ОТКРЫТЬ POLYMARKET · ${parseFloat(document.getElementById('hm-pm-stake').value || 0).toFixed(2)} USDT ↗`;
  const filledBtn = document.getElementById('btn-filled');
  filledBtn.disabled = frozen;
  filledBtn.title = frozen ? 'Неактивный снимок не является фактической сделкой' : 'Записать исполненную вручную сделку';
  document.getElementById('hm-bb-name').textContent = bbLeg.source || 'Букмекер';
}

function recalcHowMuch() {
  const a = getActive(); if (!a) return;
  const frozen = Boolean(a.inactive_snapshot);
  const bbStake = parseFloat(document.getElementById('hm-bb-stake').value || '0');
  const pmStakeUsdt = parseFloat(document.getElementById('hm-pm-stake').value || '0');

  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};
  const bbO = Number(bbLeg.o), commission = Number(bbLeg.c ?? 0), slippage = Number(bbLeg.s ?? 0);
  const pmSlippage = Number(pmLeg.s ?? 0);
  const validAmounts = Number.isFinite(pmStakeUsdt) && pmStakeUsdt > 0
    && Number.isFinite(bbStake) && bbStake > 0;
  const bank = validAmounts ? pmStakeUsdt + bbStake : 0;
  const freshBookmaker = frozen || bookmakerQuoteIsFresh(a, bbLeg);
  const quote = frozen
    ? {full: true, shares: pmStakeUsdt * Number(pmLeg.o), total_cost: pmStakeUsdt}
    : pmQuoteForEnteredAmount(a, pmLeg, pmStakeUsdt);
  const position = validAmounts && freshBookmaker && window.PolyArbPmDepthQuote
    ? window.PolyArbPmDepthQuote.position(quote, pmStakeUsdt, bbStake, bbO, commission, slippage, pmSlippage) : null;
  const pmWinEl = document.getElementById('hm-pm-win');
  const bookmakerWinEl = document.getElementById('hm-bb-win');
  const validPm = quote?.full === true && Number.isFinite(Number(quote.shares)) && Number(quote.shares) > 0
    && Number.isFinite(Number(quote.total_cost))
    && Math.abs(Number(quote.total_cost) - pmStakeUsdt) <= Math.max(0.000002, pmStakeUsdt * 1e-10);
  if (pmWinEl) pmWinEl.textContent = validPm && Number.isFinite(pmSlippage) && pmSlippage >= 0 && pmSlippage < 1
    ? fmtMoney(Number(quote.shares) * (1 - pmSlippage)) : '—';
  const adjustedBkOdds = bbO * (1 - slippage);
  const validBk = freshBookmaker && Number.isFinite(bbStake) && bbStake > 0 && Number.isFinite(adjustedBkOdds)
    && adjustedBkOdds > 1 && Number.isFinite(commission) && commission >= 0 && commission < 1
    && Number.isFinite(slippage) && slippage >= 0 && slippage < 1;
  const bookmakerPayout = validBk ? bbStake * (1 + (adjustedBkOdds - 1) * (1 - commission)) : NaN;
  if (bookmakerWinEl) bookmakerWinEl.textContent = Number.isFinite(bookmakerPayout)
    ? fmtMoney(bookmakerPayout) : '—';

  document.getElementById('hm-total').textContent = validAmounts ? fmtMoney(bank) : '—';
  document.getElementById('hm-payout').textContent = position
    ? fmtMoney(Math.min(position.pm_payout, position.bookmaker_payout)) : '—';
  const profitEl = document.getElementById('hm-profit');
  profitEl.textContent = position ? (position.minimum_profit >= 0 ? '+' : '') + fmtMoney(position.minimum_profit) : '—';
  profitEl.style.color = position ? (position.minimum_profit >= 0 ? 'var(--green)' : 'var(--red)') : 'var(--dim)';
  const roiEl = document.getElementById('hm-roi');
  if (roiEl) {
    roiEl.textContent = position ? fmtPct(position.minimum_roi) : '—';
    roiEl.style.color = position ? (position.minimum_roi >= 0 ? 'var(--green)' : 'var(--red)') : 'var(--dim)';
  }
  if (frozen) {
    document.getElementById('hm-risk-note').textContent = 'Суммы ставок и котировки заморожены из снимка; изменение общего банка на сохранённые значения не влияет.';
  } else {
    updatePlanRiskNote(bank);
    let issue = '';
    if (!validAmounts) issue = 'Введите положительные суммы на обе стороны.';
    else if (!freshBookmaker) issue = 'Котировка БК устарела или не подтверждена; общий расчёт скрыт.';
    else if (pmStakeUsdt < 1 || pmStakeUsdt > 100000) issue = 'Для расчёта PM укажите сумму от 1 до 100 000 USDT.';
    else if (!quote) issue = 'Ожидание свежего стакана и комиссии для введённой суммы.';
    else if (!quote.full) issue = quote.truncated
      ? 'Переданной части стакана недостаточно; ожидается REST или уменьшите сумму.'
      : 'Недостаточная глубина PM для введённой суммы.';
    else if (!position) issue = 'Не удалось подтвердить расчёт сумм и коэффициентов.';
    if (issue) document.getElementById('hm-risk-note').textContent += ` ${issue}`;
  }

  document.getElementById('btn-buy-pm').textContent =
    `ОТКРЫТЬ POLYMARKET · ${(Number.isFinite(pmStakeUsdt) ? pmStakeUsdt : 0).toFixed(2)} USDT ↗`;

  if (!state.calcEdited) syncCalcFromDetail(pmLeg, bbLeg, pmStakeUsdt, bbStake);
}

/* ============= RIGHT CALC ============= */
function syncCalcFromDetail(pmLeg, bbLeg, pmStakeUsdt, bbStake) {
  document.getElementById('calc-bk-name').textContent = bbLeg.source || '—';
  document.getElementById('calc-bk-odds').value = (bbLeg.o || 0).toFixed(2);
  document.getElementById('calc-bk-stake').value = Number(bbStake).toFixed(2);
  const a = getActive();
  const cachedAsk = pmBookCache[state.activeSlug]?.outcome === pmLeg.outcome
    ? pmBookCache[state.activeSlug].best_ask : null;
  const rawAsk = Number(pmLeg.pm_best_ask || (a && a.pm_best_ask) || cachedAsk);
  document.getElementById('calc-pm-price').value = rawAsk > 0 && rawAsk < 1 ? (rawAsk * 100).toFixed(1) : '0';
  document.getElementById('calc-pm-stake').value = pmStakeUsdt.toFixed(2);
  updateCalcHint();
}

function updateCalcHint() {
  const bkO = parseFloat(document.getElementById('calc-bk-odds').value || '0');
  const bkSt = parseFloat(document.getElementById('calc-bk-stake').value || '0');
  const pmP = parseFloat(document.getElementById('calc-pm-price').value || '0') / 100;
  const pmSt = parseFloat(document.getElementById('calc-pm-stake').value || '0');
  const active = getActive();
  const pmLeg = (active?.bets || []).find(leg => leg?.source === 'Polymarket');
  const feeSchedule = getPmFeeSchedule(pmLeg);
  const pmO = pmP > 0 ? pmEffectiveOdds(pmP, feeSchedule) : 0;
  const bank = bkSt + pmSt;
  const payBK = bkSt * bkO;
  const payPM = pmSt * (pmO || 0);
  const minPay = Math.min(payBK, payPM);
  const profit = minPay - bank;
  const roi = bank > 0 ? profit / bank : 0;

  const hint = document.getElementById('calc-hint');
  if (!(bkO > 1 && bkSt > 0 && pmP > 0 && pmP < 1 && pmSt > 0)) {
    hint.textContent = 'Укажи обе суммы в USDT, кэф БК и цену PM';
    hint.style.color = 'var(--dim)';
    return;
  }
  if (pmO === null) {
    hint.textContent = 'Расчёт PM недоступен: нет проверенного fee schedule этого рынка из CLOB API.';
    hint.style.color = 'var(--red)';
    return;
  }
  const cap = getPlanCap();
  const overCap = cap !== null && bank > cap + 0.01;
  const capPercent = getPlanCapPercent();
  const riskText = cap === null
    ? `Общий банк не задан: контроль ${capPercent}% не рассчитан.`
    : overCap ? `ПРЕДУПРЕЖДЕНИЕ: выше ${capPercent}%-ориентира ${fmtMoney(cap)}.`
      : `В пределах ${capPercent}%-ориентира ${fmtMoney(cap)} на один план.`;
  const riskClass = overCap ? 'warn' : cap !== null ? 'safe' : '';
  hint.innerHTML = `Банк: <b>${fmtMoney(bank)}</b> · Профит: <b style="color:${profit >= 0 ? 'var(--green)' : 'var(--red)'}">${profit >= 0 ? '+' : ''}${fmtMoney(profit)}</b> · ROI: <b style="color:${roi >= 0 ? 'var(--green)' : 'var(--red)'}">${fmtPct(roi)}</b><br><span class="risk-inline ${riskClass}">${riskText}</span>`;
  hint.style.color = 'var(--txt)';
}

/* ============= LIVE SCANNER CONTROLS ============= */
function canonicalSources(sources) {
  const known = new Set(BK_LIST.map(bookmaker => bookmaker.id));
  return [...new Set((Array.isArray(sources) ? sources : [])
    .map(source => window.PolyArbSourceFilter.sourceId(source))
    .filter(id => id && known.has(id)))];
}
function configuredBookmakerIds() {
  return canonicalSources(state.config.enabled_bk_sources || ['1win']);
}
function availableBookmakerIds() {
  const sources = Array.isArray(state.scanStatus?.available_bk_sources)
    ? state.scanStatus.available_bk_sources : state.config.available_bk_sources;
  return canonicalSources(sources || ['1win']);
}
function activeBookmakerIds() {
  const sources = Array.isArray(state.scanStatus?.enabled_bk_sources)
    ? state.scanStatus.enabled_bk_sources : state.config.active_bk_sources;
  return canonicalSources(sources || configuredBookmakerIds());
}
function sourceNames(ids) {
  return BK_LIST.filter(bookmaker => ids.includes(bookmaker.id))
    .map(bookmaker => bookmaker.name);
}
function updateSourceStatus() {
  const el = document.getElementById('source-status');
  if (!el) return;
  const requested = configuredBookmakerIds();
  const available = availableBookmakerIds();
  const active = activeBookmakerIds();
  const running = Boolean(state.scanStatus?.state === 'running' || state.config.scanner_running);
  if (!running) {
    el.textContent = `Сохранено: ${sourceNames(requested).join(' + ') || 'не выбрано'}`;
    return;
  }
  const waiting = requested.some(id => !active.includes(id)) || active.some(id => !requested.includes(id));
  el.textContent = waiting
    ? `Применяется: ${sourceNames(requested).join(' + ') || 'не выбрано'}`
    : `Активно: ${sourceNames(active).join(' + ') || 'ожидание feed'}`;
  el.title = available.length
    ? `Доступно в этом запуске: ${sourceNames(available).join(' + ')}`
    : 'В текущем запуске нет доступных bookmaker feeds';
}
function syncRuntimeControlsFromStatus(scanStatus) {
  if (scanStatus && Array.isArray(scanStatus.enabled_bk_sources)) {
    state.config.active_bk_sources = scanStatus.enabled_bk_sources;
  }
  if (scanStatus && scanStatus.runtime_settings) {
    state.config.active_runtime_settings = scanStatus.runtime_settings;
  }
  const statusRoi = Number(scanStatus?.min_roi_pct);
  if (Number.isFinite(statusRoi) && statusRoi >= 0 && statusRoi <= 100) {
    state.config.active_min_roi_pct = statusRoi;
  }
  const roiInput = document.getElementById('f-minroi');
  const roiLimits = state.config.config_limits?.min_roi_pct;
  if (roiInput && roiLimits) {
    roiInput.min = roiLimits.minimum;
    roiInput.max = roiLimits.maximum;
  }
  if (roiInput) {
    roiInput.title = scanStatus?.runtime_settings?.sources?.min_roi_pct === 'cli'
      ? 'Сейчас действует CLI override; сохранённый порог начнёт действовать после запуска без --min-roi'
      : 'Порог сканера применяется без перезапуска';
  }
  const currentRoi = Number(state.config.active_min_roi_pct ?? state.config.min_roi_pct ?? 3);
  if (roiInput && Number.isFinite(currentRoi) && document.activeElement !== roiInput) {
    roiInput.value = String(currentRoi);
  }
  updateSourceStatus();
}
async function loadRuntimeConfig() {
  try {
    const r = await fetch('/api/config');
    if (!r.ok) return;
    const config = await r.json();
    state.config = { ...state.config, ...config };
    const roiInput = document.getElementById('f-minroi');
    if (roiInput && document.activeElement !== roiInput) {
      roiInput.value = String(Number(config.active_min_roi_pct ?? config.min_roi_pct ?? 3));
    }
    const bankInput = document.getElementById('risk-total-bank');
    if (bankInput && Number(config.bank) > 0) bankInput.value = Number(config.bank).toFixed(2);
    updateBankLimit();
    updateSourceStatus();
    applyFilters();
  } catch (e) {
    updateSourceStatus();
  }
}
async function postRuntimeConfig(payload, successMessage) {
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok || !j.ok) {
      toast('Ошибка: ' + (j.err || `HTTP ${r.status}`), true);
      return false;
    }
    state.config = { ...state.config, ...(j.config || {}) };
    if (Object.prototype.hasOwnProperty.call(payload, 'min_roi_pct')) {
      state.config.active_min_roi_pct = Number(payload.min_roi_pct);
    }
    if (Array.isArray(payload.enabled_bk_sources)) {
      state.config.enabled_bk_sources = payload.enabled_bk_sources;
    }
    if (successMessage) toast(successMessage);
    syncRuntimeControlsFromStatus(state.scanStatus);
    applyFilters();
    return true;
  } catch (e) {
    toast('Сеть: ' + e.message, true);
    return false;
  }
}
function openBkModal() {
  const selected = new Set(configuredBookmakerIds());
  const available = new Set(availableBookmakerIds());
  const grid = document.getElementById('bk-grid');
  grid.innerHTML = activeBookmakerList().map(bookmaker => {
    const on = selected.has(bookmaker.id) && available.has(bookmaker.id);
    const disabled = !available.has(bookmaker.id);
    const source = bookmaker.id === 'betboom' ? 'BetBoom' : '1win';
    return `<button class="bk-item ${on ? 'on' : ''} ${disabled ? 'disabled' : ''}"
      type="button" aria-pressed="${on}" ${disabled ? 'disabled' : ''}
      onclick="toggleBk('${bookmaker.id}')">
      <span class="mark" aria-hidden="true"></span>
      <span class="name">${escapeHtml(bookmaker.name)}</span>
      <span class="speed">${disabled ? 'недоступен' : on ? 'включён' : 'выключен'}</span>
    </button>`;
  }).join('') || '<div class="bk-empty">В текущем запуске нет доступных feeds.</div>';
  const active = activeBookmakerIds();
  const running = Boolean(state.scanStatus?.state === 'running' || state.config.scanner_running);
  const pending = running && (
    [...selected].some(id => !active.includes(id)) || active.some(id => !selected.has(id))
  );
  document.getElementById('bk-sub').textContent = !running
    ? `Сохранено: ${sourceNames([...selected]).join(' + ') || '—'} · применится при запуске сканера`
    : pending
      ? `Запрошено: ${sourceNames([...selected]).join(' + ') || '—'} · сканер применяет изменение`
      : `Активно: ${sourceNames(active).join(' + ') || 'ожидание feed'} · изменения применяются без перезапуска`;
  document.getElementById('modal-bk').classList.remove('hidden');
}
async function toggleBk(id) {
  if (!availableBookmakerIds().includes(id)) return;
  const next = new Set(configuredBookmakerIds().filter(source => availableBookmakerIds().includes(source)));
  if (next.has(id)) next.delete(id); else next.add(id);
  if (!next.size) {
    toast('Оставьте включённым хотя бы один источник котировок', true);
    return;
  }
  const payload = [...next].map(source => source === 'betboom' ? 'BetBoom' : '1win');
  if (await postRuntimeConfig({ enabled_bk_sources: payload }, 'Источники переданы сканеру')) {
    openBkModal();
  }
}
function closeBkModal() { document.getElementById('modal-bk').classList.add('hidden'); }

/* ============= CONFIG MODAL ============= */
async function openConfig() {
  try {
    const r = await fetch('/api/config');
    const j = await r.json();
    state.config = { ...state.config, ...j };
    const bankInput = document.getElementById('risk-total-bank');
    if (bankInput) bankInput.value = Number(j.bank || 100).toFixed(2);
    document.getElementById('cfg-bank').value = j.bank ?? 100;
    document.getElementById('cfg-safe').value = j.safe_fork_threshold ?? 0.45;
    document.getElementById('cfg-poll').value = j.poll ?? 0.3;
    document.getElementById('cfg-watch-sec').value = j.watch_sec ?? 20;
    document.getElementById('cfg-alert-gap').value = j.alert_gap ?? 120;
    const fieldLimits = {
      'cfg-bank': 'bank', 'cfg-safe': 'safe_fork_threshold',
      'cfg-poll': 'poll', 'cfg-watch-sec': 'watch_sec', 'cfg-alert-gap': 'alert_gap',
    };
    for (const [id, key] of Object.entries(fieldLimits)) {
      const input = document.getElementById(id);
      const limits = j.config_limits?.[key];
      if (input && limits) {
        input.min = limits.minimum;
        input.max = limits.maximum;
      }
    }
    const active = j.active_runtime_settings || {};
    const sources = active.sources || {};
    const activeStatus = document.getElementById('cfg-active-status');
    if (activeStatus) {
      activeStatus.textContent = j.scanner_running && Object.keys(active).length
        ? `Сейчас активно: банк ${Number(active.bank_usdt ?? j.bank).toFixed(2)} USDT (${sources.bank === 'cli' ? 'CLI override' : 'config/default'}), ROI ${Number(active.min_roi_pct ?? j.min_roi_pct).toFixed(2)}% (${sources.min_roi_pct === 'cli' ? 'CLI override' : 'config/default'}), обработка ${Number(active.poll_sec ?? j.poll)} с (${sources.poll === 'cli' ? 'CLI override' : 'config/default'}), каталог ${Number(active.watch_sec ?? j.watch_sec)} с (${sources.watch_sec === 'cli' ? 'CLI override' : 'config/default'}), повтор сигнала ${Number(active.alert_gap_sec ?? j.alert_gap)} с (${sources.alert_gap === 'cli' ? 'CLI override' : 'config/default'}). CLI override имеет приоритет над сохранённым значением.`
        : 'Сканер не передаёт активные настройки. Значения сохранятся в config.json и применятся при запуске.';
    }
    const warningBox = document.getElementById('cfg-warning');
    const warnings = Array.isArray(j.config_warnings) ? j.config_warnings : [];
    if (warningBox) {
      warningBox.textContent = warnings.join(' ');
      warningBox.classList.toggle('hidden', warnings.length === 0);
    }
    const runtimeLimits = j.runtime_limits || {};
    const freshness = runtimeLimits.freshness || {};
    const catalog = runtimeLimits.catalog || {};
    const safety = runtimeLimits.safety || {};
    const limitsBox = document.getElementById('cfg-runtime-limits');
    if (limitsBox) {
      const ageSource = freshness.onewin_odds_age_source === 'env'
        ? 'переменная среды' : 'общий default';
      limitsBox.textContent = `Фиксированные runtime-лимиты (только чтение): PM book TTL ${Number(freshness.polymarket_book_ttl_sec || 0)} с; диагностика устаревшего PM book ${Number(freshness.polymarket_stale_diagnostic_sec || 0)} с; 1win TTL ${Number(freshness.onewin_odds_max_age_sec || 0)} с (${ageSource}); stale-порог источника в панели ${Number(freshness.dashboard_source_stale_sec || 0)} с; Gamma ${Number(catalog.gamma_events_per_page || 0)} событий/страницу, максимум ${Number(catalog.gamma_max_pages_per_query || 0)} страниц/запрос, ${Number(catalog.gamma_retries_per_page || 0)} повторов, ${Number(catalog.gamma_workers || 0)} workers; safety cap плана ${Number(safety.bankroll_plan_pct || 0)}% банка. Это только отображение действующих лимитов, значения здесь не меняются.`;
    }
    syncRuntimeControlsFromStatus(state.scanStatus);
  } catch (e) {}
  document.getElementById('modal-config').classList.remove('hidden');
}
function closeConfig() { document.getElementById('modal-config').classList.add('hidden'); }
async function saveConfig() {
  const limits = state.config.config_limits || {};
  const fields = [
    ['bank', 'cfg-bank'], ['safe_fork_threshold', 'cfg-safe'], ['poll', 'cfg-poll'],
    ['watch_sec', 'cfg-watch-sec'], ['alert_gap', 'cfg-alert-gap'],
  ];
  const values = {};
  for (const [key, id] of fields) {
    const input = document.getElementById(id);
    const value = Number(input?.value);
    const bound = limits[key] || { minimum: Number(input?.min), maximum: Number(input?.max) };
    if (!Number.isFinite(value) || value < bound.minimum || value > bound.maximum
        || (['watch_sec', 'alert_gap'].includes(key) && !Number.isInteger(value))) {
      toast(`${key}: значение вне допустимого диапазона`, true);
      return;
    }
    values[key] = value;
  }
  const payload = {
    ...values,
  };
  const activeSources = state.config.active_runtime_settings?.sources || {};
  const cliOverrides = Object.keys(payload).filter(key => activeSources[key] === 'cli');
  const successMessage = cliOverrides.length
    ? `Сохранено. Сейчас действует CLI override для: ${cliOverrides.join(', ')}; эти поля применятся без override.`
    : 'Настройки сохранены';
  if (await postRuntimeConfig(payload, successMessage)) {
    const bankInput = document.getElementById('risk-total-bank');
    if (bankInput) bankInput.value = Number(state.config.bank || 100).toFixed(2);
    updateBankLimit();
    closeConfig();
  }
}
async function saveMinRoi() {
  const input = document.getElementById('f-minroi');
  const value = Number(input?.value);
  const limits = state.config.config_limits?.min_roi_pct || { minimum: 0, maximum: 100 };
  if (!Number.isFinite(value) || value < limits.minimum || value > limits.maximum) {
    toast(`Минимальный ROI должен быть от ${limits.minimum} до ${limits.maximum}%`, true);
    syncRuntimeControlsFromStatus(state.scanStatus);
    return;
  }
  const cliOverride = state.config.active_runtime_settings?.sources?.min_roi_pct === 'cli';
  const message = cliOverride
    ? `Порог ROI ${value}% сохранён; сейчас действует CLI override`
    : `Порог ROI ${value}% передан сканеру`;
  if (!await postRuntimeConfig({ min_roi_pct: value }, message)) {
    syncRuntimeControlsFromStatus(state.scanStatus);
  }
}

/* ============= EDIT MODAL ============= */
function openEdit(slug, initialStakes = null) {
  const a = state.arbs.find(x => x.slug === slug) || getActive();
  if (!a) return;
  state.editing = JSON.parse(JSON.stringify(a));
  if (initialStakes) {
    state.editing.stakes = (state.editing.bets || []).map(leg => {
      const value = Number(leg.source === 'Polymarket' ? initialStakes.pm : initialStakes.bookmaker);
      return Number.isFinite(value) && value > 0 ? value : 0;
    });
  }
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
  document.getElementById('ed-match').textContent = a.title
    ? (a.title + ' — ' + (a.q || ''))
    : (a.q || a.slug);
  document.getElementById('ed-sub').textContent = `${a.market_type || ''} · ${a.sport || ''} · ${a.slug}`;
  const pmUsdt = pmSt;

  document.getElementById('ed-leg-0').innerHTML = `
    <div class="leg-head"><span class="lbl">Плечо 1</span><span class="leg-bk">${escapeHtml(bb.source || 'Букмекер')}</span></div>
    <div class="leg-field"><span class="lbl">Исход</span><input type="text" id="ed-bb-outcome" value="${escapeHtml(bb.outcome || '')}" readonly></div>
    <div class="leg-field"><span class="lbl">Коэффициент</span><input type="number" step="0.01" id="ed-bb-odds" value="${bb.o || 0}"></div>
    <div class="leg-field"><span class="lbl">Ставка, USDT</span><input type="number" step="0.01" id="ed-bb-stake" value="${bbSt.toFixed(2)}"></div>
  `;
  document.getElementById('ed-leg-1').innerHTML = `
    <div class="leg-head"><span class="lbl">Плечо 2</span><span class="leg-bk">Polymarket</span></div>
    <div class="leg-field"><span class="lbl">Исход</span><input type="text" id="ed-pm-outcome" value="${escapeHtml(pm.outcome || '')}" readonly></div>
    <div class="leg-field"><span class="lbl">Цена, ¢</span><input type="number" step="0.1" id="ed-pm-price" value="${Number(pm.pm_best_ask || a.pm_best_ask) > 0 ? (Number(pm.pm_best_ask || a.pm_best_ask) * 100).toFixed(1) : '0'}"></div>
    <div class="leg-field"><span class="lbl">Ордер, USDT</span><input type="number" step="0.01" id="ed-pm-amount" value="${pmUsdt.toFixed(2)}"></div>
  `;
  ['ed-bb-odds','ed-bb-stake','ed-pm-price','ed-pm-amount'].forEach(id => {
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
  const pmLeg = (a.bets || []).find(leg => leg?.source === 'Polymarket');
  const feeSchedule = getPmFeeSchedule(pmLeg);
  const pmO = pmPrice > 0 ? pmEffectiveOdds(pmPrice, feeSchedule) : 0;
  const bank = bbSt + pmUsd;
  const pay1 = bbSt * bbO;
  const pay2 = pmUsd * (pmO || 0);
  const minPay = Math.min(pay1, pay2);
  const profit = minPay - bank;
  const roi = bank > 0 ? profit / bank : 0;
  document.getElementById('ed-bank').textContent = fmtMoney(bank);
  document.getElementById('ed-p1').textContent = (pay1 - bank >= 0 ? '+' : '') + fmtMoney(pay1 - bank);
  document.getElementById('ed-p2').textContent = (pay2 - bank >= 0 ? '+' : '') + fmtMoney(pay2 - bank);
  const roiEl = document.getElementById('ed-roi');
  const feeValid = pmO !== null;
  roiEl.textContent = feeValid ? fmtPct(roi) : '—';
  roiEl.style.color = feeValid ? (roi >= 0 ? 'var(--green)' : 'var(--red)') : 'var(--red)';
  if (!feeValid) document.getElementById('ed-p1').textContent = 'Нет schedule комиссии CLOB';
  a._calc = {
    bb_odds: bbO, bb_stake_usdt: bbSt,
    pm_price: pmPrice, pm_amount_usdt: pmUsd, rate: 1.0,
    bank, payout: minPay, profit, roi, fee_valid: feeValid,
  };
}

async function saveTrade() {
  const a = state.editing  ; if (!a || !a._calc) return;
  if (!a._calc.fee_valid) {
    toast('Сделка не сохранена: нет проверенного fee schedule рынка', true);
    return;
  }
  const payload = {
    slug: a.slug, q: a.q, sport: a.sport, market_type: a.market_type,
    bank: a._calc.bank, profit: a._calc.profit, roi: a._calc.roi,
    payout: a._calc.payout, rate: 1.0, currency: 'USDT',
    legs: [
      { book: ((a.bets.find(b => b.source && b.source !== 'Polymarket') || {}).source) || 'Букмекер', outcome: ((a.bets.find(b => b.source && b.source !== 'Polymarket') || {}).outcome) || '',
        odds: a._calc.bb_odds, stake_usdt: a._calc.bb_stake_usdt },
      { book: 'Polymarket', outcome: ((a.bets.find(b => b.source === 'Polymarket') || {}).outcome) || '',
        price_usd: a._calc.pm_price, amount_usd: a._calc.pm_amount_usdt,
        amount_usdt: a._calc.pm_amount_usdt, stake_usdt: a._calc.pm_amount_usdt },
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
    if (!j.ok) {
      const errText = j.err === 'fee_schedule_unavailable'
        ? 'Нет проверенного расписания комиссии для этого рынка в CLOB API.'
        : (j.err || 'Ошибка получения данных');
      body.innerHTML = '<div class="pm-check-err">' + escapeHtml(errText) + '</div>';
      return;
    }
    const pmOutcome = document.getElementById('ed-pm-outcome').value;
    let book = j.books.find(b => (b.outcome || '').trim() === pmOutcome.trim());
    if (!book || !book.best_ask) { body.innerHTML = '<div class="pm-check-err">PM \u0441\u0442\u0430\u043A\u0430\u043D \u043F\u0443\u0441\u0442</div>'; return; }
    const ba = book.best_ask;
    const pmPrice = parseFloat(document.getElementById('ed-pm-price').value || '0') / 100;
    const schedule = normalizePmFeeSchedule(j.fee_schedule);
    if (!schedule) {
      body.innerHTML = '<div class="pm-check-err">CLOB вернул неполный или невалидный fee schedule</div>';
      return;
    }
    const curOdds = pmEffectiveOdds(pmPrice, schedule);
    const liveOdds = pmEffectiveOdds(ba, schedule);
    if (curOdds === null || liveOdds === null) {
      body.innerHTML = '<div class="pm-check-err">Нет проверенного fee schedule рынка</div>';
      return;
    }
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
              '<td>k=' + (b.best_ask ? pmEffectiveOdds(b.best_ask, schedule).toFixed(4) : '\u2014') + '</td>' +
              '<td>' + Number(b.ask_depth_usd || 0).toFixed(2) + ' USDT</td></tr>'
        ).join('') + '</tbody>' +
      '</table>';
  } catch (e) { body.innerHTML = '<div class="pm-check-err">' + escapeHtml(e.message) + '</div>'; }
}
function refreshPMNow() { checkPM(); }

function setEventActionLink(element, url, unavailableReason) {
  if (!element) return;
  if (url) {
    element.setAttribute('href', url);
    element.setAttribute('aria-disabled', 'false');
    element.removeAttribute('tabindex');
    element.title = 'Открыть страницу события без автоматической ставки';
  } else {
    element.removeAttribute('href');
    element.setAttribute('aria-disabled', 'true');
    element.setAttribute('tabindex', '-1');
    element.title = unavailableReason || 'Ссылка на событие недоступна';
  }
}

function bookmakerEventUrl(a) {
  const bkLeg = (a.bets || []).find(leg => leg.source && leg.source !== 'Polymarket');
  const oneWinEventUrl = activeOpportunityEventUrl(a, '1win')
    || bkLeg?.event_url || a.bk_event_url;
  if (bkLeg?.source === '1win' && oneWinEventUrl) {
    try {
      const eventUrl = new URL(oneWinEventUrl, window.location.origin);
      const allowedOrigin = window.POLYARB_ONEWIN_ORIGIN;
      if (eventUrl.protocol !== 'https:' || eventUrl.username || eventUrl.password
          || !allowedOrigin || eventUrl.origin !== allowedOrigin) return null;
      return eventUrl.href;
    } catch (_error) { return null; }
  }
  if (bkLeg?.source !== 'BetBoom') return null;
  const mid = String(a.bb_match_id || ''), tour = String(a.bb_tournament_id || '');
  if (!/^[a-zA-Z0-9_-]+$/.test(mid) || !/^[a-zA-Z0-9_-]+$/.test(tour)) return null;
  const sportMap = { 'CS2': 'cs2', 'DOTA2': 'dota-2', 'LOL': 'lol', 'VALORANT': 'valorant', 'TENNIS': 'tennis' };
  const sp = sportMap[a.sport] || 'esport';
  return 'https://betboom.ru/esport/' + sp + '/' + tour + '/' + mid;
}
function polymarketUrlUsesMarketSlug(value, marketSlug) {
  try {
    const url = new URL(String(value || ''), window.location.origin);
    const lastSegment = decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() || '');
    return lastSegment === String(marketSlug || '');
  } catch (_error) {
    return false;
  }
}

function updateEventActionLinks(a) {
  const bkLeg = (a.bets || []).find(leg => leg.source && leg.source !== 'Polymarket');
  const bkElement = document.getElementById('btn-open-bb');
  const sourceLabel = bkLeg?.source === '1win' ? '1WIN' : bkLeg?.source === 'BetBoom' ? 'BETBOOM' : 'БК';
  if (bkElement) bkElement.textContent = `ОТКРЫТЬ СОБЫТИЕ ${sourceLabel} ↗`;
  const bkUrl = bookmakerEventUrl(a);
  setEventActionLink(bkElement, bkUrl, 'Ссылка на страницу события БК не найдена или недопустима');

  const pmElement = document.getElementById('btn-buy-pm');
  const status = document.getElementById('event-link-status');
  const pmLeg = (a.bets || []).find(leg => leg.source === 'Polymarket');
  const pmEventUrl = activeOpportunityEventUrl(a, 'Polymarket')
    || pmLeg?.event_url || a.pm_event_url;
  const validate = value => window.PolyArbProviderLinks?.polymarketEventUrl(value, window.location.origin) || null;
  const safeDirectUrl = pmEventUrl ? validate(pmEventUrl) : null;
  if (pmEventUrl && !safeDirectUrl) {
    setEventActionLink(pmElement, null, 'Недопустимая ссылка Polymarket');
    if (status) status.textContent = 'Ссылка Polymarket недоступна или небезопасна.';
    return;
  }
  if (safeDirectUrl && !polymarketUrlUsesMarketSlug(safeDirectUrl, a.slug)) {
    setEventActionLink(pmElement, safeDirectUrl);
    if (status) status.textContent = '';
    return;
  }
  const slug = String(a.slug || '');
  if (!slug || slug.length > 300 || !window.PolyArbProviderLinks?.polymarketEventUrl) {
    setEventActionLink(pmElement, null, 'Ссылка Polymarket недоступна');
    if (status) status.textContent = 'Ссылка Polymarket недоступна.';
    return;
  }

  let cached = pmEventLinkCache.get(slug);
  const now = Date.now();
  // Успех держим 5 минут; ошибки повторяем не чаще одного раза в 30 секунд.
  if (cached && cached.status !== 'pending' && now >= cached.expiresAt) {
    pmEventLinkCache.delete(slug);
    cached = null;
  }
  if (cached?.status === 'ready') {
    setEventActionLink(pmElement, cached.url);
    if (status) status.textContent = '';
    return;
  }
  if (cached?.status === 'failed') {
    setEventActionLink(pmElement, null, 'Ссылка Polymarket не найдена; будет повторная попытка');
    if (status) status.textContent = 'Ссылка Polymarket не найдена; повторим попытку автоматически.';
    return;
  }
  setEventActionLink(pmElement, null, 'Определяется ссылка Polymarket');
  if (status) status.textContent = 'Определяем ссылку Polymarket…';
  if (cached?.status === 'pending') return;

  // Сетевое разрешение запускается только для выбранного рынка и не блокирует UI.
  // Одна заявка на slug, в том числе при частых обновлениях live и снимков.
  if (pmEventLinkCache.size >= 200) {
    for (const [key, entry] of pmEventLinkCache) {
      if (entry.status !== 'pending') { pmEventLinkCache.delete(key); break; }
    }
  }
  const pending = { status: 'pending' };
  pmEventLinkCache.set(slug, pending);
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), 12000) : null;
  Promise.resolve().then(() => fetch('/api/pm_url/' + encodeURIComponent(slug),
    controller ? { signal: controller.signal } : {}))
    .then(response => {
      if (!response.ok) throw new Error('Не удалось определить ссылку');
      return response.json();
    })
    .then(data => validate(data?.url))
    .catch(() => null)
    .then(url => {
      if (timeout !== null) clearTimeout(timeout);
      if (pmEventLinkCache.get(slug) !== pending) return;
      pmEventLinkCache.set(slug, url
        ? { status: 'ready', url, expiresAt: Date.now() + 300000 }
        : { status: 'failed', expiresAt: Date.now() + 30000 });
      // Никогда не применять результат к старой карточке: заново читаем
      // текущий live-сигнал или закреплённый снимок и его актуальные URL.
      const current = getActive();
      if (current && String(current.slug || '') === slug) updateEventActionLinks(current);
    });
}
function markFilled() {
  const a = getActive();
  if (!a) return;
  if (a.inactive_snapshot) {
    toast('Неактивный снимок нельзя записать как исполненную сделку', true);
    return;
  }
  openEdit(a.slug, {
    pm: document.getElementById('hm-pm-stake').value,
    bookmaker: document.getElementById('hm-bb-stake').value,
  });
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
          '<span class="profit ' + cls + '">' + (profit >= 0 ? '+' : '') + fmtHistoricalAmount(profit, t.currency) + '</span>' +
        '</div>' +
        '<div class="legs"><div>1) ' + escapeHtml(leg1) + '</div><div>2) ' + escapeHtml(leg2) + '</div></div>' +
        '<div class="meta">' +
          '<span>' + escapeHtml((t.ts || '').replace('T', ' ')) + '</span>' +
          '<span>\u0431\u0430\u043D\u043A: <b>' + fmtHistoricalAmount(t.bank, t.currency) + '</b></span>' +
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
      const profitTxt = d.profit ? (d.profit >= 0 ? '+' : '') + fmtMoney(d.profit) : '';
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
          '<div class="rt-profit ' + cls + '">' + (profit >= 0 ? '+' : '') + fmtHistoricalAmount(profit, t.currency) + '</div>' +
        '</div>' +
        '<div class="rt-meta">' +
          '<span>ROI ' + (Number(t.roi || 0) * 100).toFixed(2) + '%</span>' +
          '<span>\u0431\u0430\u043D\u043A ' + fmtHistoricalAmount(t.bank, t.currency) + '</span>' +
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

function syncNumberStepperState(input) {
  const wrapper = input && input.closest('.number-control');
  if (!wrapper) return;
  wrapper.querySelectorAll('.number-step-button').forEach(button => {
    button.disabled = Boolean(input.disabled || input.readOnly);
  });
}

function enhanceNumberSteppers(root = document) {
  const inputs = root.matches && root.matches('input[type="number"]')
    ? [root] : Array.from(root.querySelectorAll('input[type="number"]'));
  inputs.forEach(input => {
    if (input.dataset.numberStepperReady || input.disabled || input.readOnly || !input.parentNode) return;
    input.dataset.numberStepperReady = 'true';
    const wrapper = document.createElement('span');
    wrapper.className = 'number-control';
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    const buttons = document.createElement('span');
    buttons.className = 'number-control-buttons';
    const label = input.getAttribute('aria-label') || input.id.replace(/-/g, ' ') || 'значение';
    const addStepButton = (direction, name, path) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `number-step-button ${name}`;
      button.setAttribute('aria-label', `${direction > 0 ? 'Увеличить' : 'Уменьшить'} ${label}`);
      button.innerHTML = `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="${path}"/></svg>`;
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        if (input.disabled || input.readOnly) return;
        const previous = input.value;
        try {
          if (direction > 0) input.stepUp();
          else input.stepDown();
        } catch (error) {
          return;
        }
        if (input.value !== previous) {
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
        }
      });
      buttons.appendChild(button);
    };
    addStepButton(1, 'up', 'm3 7.5 3-3 3 3');
    addStepButton(-1, 'down', 'm3 4.5 3 3 3-3');
    wrapper.appendChild(buttons);
    syncNumberStepperState(input);
  });
}

function init() {
  enhanceNumberSteppers();
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      state.phase = t.dataset.phase;
      applyFilters();
    });
  });

  ['f-cs2','f-dota2','f-lol','f-valorant','f-tennis'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', applyFilters);
  });
  const minRoiEl = document.getElementById('f-minroi');
  if (minRoiEl) {
    minRoiEl.addEventListener('input', applyFilters);
    minRoiEl.addEventListener('change', saveMinRoi);
  }
  const bkSettingsBtn = document.getElementById('btn-bk-settings');
  if (bkSettingsBtn) bkSettingsBtn.addEventListener('click', openBkModal);

  const scanDetailsToggle = document.getElementById('btn-scan-details');
  const scanDetails = document.getElementById('scan-health-details');
  if (scanDetailsToggle && scanDetails) {
    scanDetailsToggle.addEventListener('click', () => {
      const expanded = scanDetailsToggle.getAttribute('aria-expanded') !== 'true';
      scanDetailsToggle.setAttribute('aria-expanded', String(expanded));
      scanDetails.hidden = !expanded;
      scanDetailsToggle.innerHTML = expanded
        ? 'СВЕРНУТЬ <span aria-hidden="true">⌃</span>'
        : 'ДЕТАЛИ <span aria-hidden="true">⌄</span>';
    });
  }

  const grid = document.querySelector('.grid');
  const toolsToggle = document.getElementById('btn-tools-toggle');
  const toolsPanel = document.getElementById('col-calc');
  if (grid && toolsToggle && toolsPanel) {
    const savedToolsCollapsed = localStorage.getItem('poly_arb_tools_collapsed');
    const setToolsCollapsed = collapsed => {
      grid.classList.toggle('tools-collapsed', collapsed);
      toolsToggle.setAttribute('aria-expanded', String(!collapsed));
      toolsToggle.setAttribute('aria-label', collapsed ? 'Показать инструменты' : 'Скрыть инструменты');
      toolsToggle.innerHTML = collapsed
        ? 'ИНСТРУМЕНТЫ <span aria-hidden="true">⌗</span>'
        : 'СКРЫТЬ ИНСТРУМЕНТЫ <span aria-hidden="true">⌃</span>';
    };
    setToolsCollapsed(savedToolsCollapsed === null ? true : savedToolsCollapsed !== 'false');
    toolsToggle.addEventListener('click', () => {
      const collapsed = !grid.classList.contains('tools-collapsed');
      setToolsCollapsed(collapsed);
      localStorage.setItem('poly_arb_tools_collapsed', String(collapsed));
    });
  }

  const soundBtn = document.getElementById('st-sound');
  if (soundBtn) soundBtn.addEventListener('click', toggleSound);
  updateSoundBtn();
  const desktopNotifyBtn = document.getElementById('btn-desktop-notify');
  if (desktopNotifyBtn) desktopNotifyBtn.addEventListener('click', toggleDesktopNotifications);
  updateDesktopNotificationsBtn();

  const cfgBtn = document.getElementById('btn-config');
  if (cfgBtn) cfgBtn.addEventListener('click', openConfig);
  const dailyBtn = document.getElementById('btn-daily');
  if (dailyBtn) dailyBtn.addEventListener('click', openDaily);
  const shutdownBtn = document.getElementById('btn-shutdown');
  if (shutdownBtn) shutdownBtn.addEventListener('click', shutdownBot);

  const detClose = document.getElementById('btn-det-close');
  if (detClose) detClose.addEventListener('click', closeDetail);

  observePriceChartResize();

  const hmBB = document.getElementById('hm-bb-stake');
  if (hmBB) hmBB.addEventListener('input', () => { state.detailEdited = true; state.suggestedCapped = false; recalcHowMuch(); });
  const hmPM = document.getElementById('hm-pm-stake');
  if (hmPM) hmPM.addEventListener('input', () => {
    state.detailEdited = true;
    state.suggestedCapped = false;
    state.bookRequestId = (state.bookRequestId || 0) + 1;
    const active = getActive();
    if (active) {
      renderHowMuch(active);
      renderLivePmBook(active);
    }
    clearTimeout(state.bookRefreshTimer);
    state.bookRefreshTimer = setTimeout(() => { if (state.activeSlug) loadPmBook(state.activeSlug); }, 600);
  });

  const filled = document.getElementById('btn-filled');
  if (filled) filled.addEventListener('click', markFilled);

  ['calc-bk-odds','calc-bk-stake','calc-pm-price','calc-pm-stake'].forEach(id => {
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
    updateCalcHint();
  });
  const myArbsBtn = document.getElementById('btn-my-arbs');
  if (myArbsBtn) myArbsBtn.addEventListener('click', showTrades);

  const totalBank = document.getElementById('risk-total-bank');
  if (totalBank) {
    totalBank.value = Number(state.config.bank || 100).toFixed(2);
    updateBankLimit();
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeEdit(); closeTrades(); closeDaily(); closeBkModal(); closeConfig();
    }
  });
  document.addEventListener('click', () => ensureAudioCtx(), { once: true });

  setInterval(() => {
    if (state.activeSlug) loadPmBook(state.activeSlug);
  }, 5000);
  setInterval(() => {
    if (state.activeSlug) loadPriceChart(state.activeSlug);
  }, 30000);
  setInterval(checkWsFreshness, 5000);
  document.addEventListener('visibilitychange', checkWsFreshness);
  setInterval(loadDaily, 30000);
  setInterval(() => {
    renderArbs();
    if (state.activeSnapshotId) refreshDetail();
    else if (state.activeSlug) refreshDetail({loadRemote: false});
  }, 1000);

  loadDaily();
  loadRuntimeConfig();
  connectWS();
}

document.addEventListener('DOMContentLoaded', init);
