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
  config: {
    rate: 1, bank: 100, currency: 'USDT', pm_fee_pct: 0.04,
    safe_fork_threshold: 0.45, min_roi_pct: 3,
    enabled_bk_sources: ['1win'], available_bk_sources: ['1win', 'BetBoom'],
  },
  dailyStats: null,
};

const pmBookCache = {};  // slug -> {best_ask, best_bid, ask_depth_usd}
const quoteFlashTimers = new WeakMap();

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

/* ============= UTILS ============= */
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
function sportClass(s) { return (s || 'other').toLowerCase(); }
function toast(msg, err) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('err', !!err);
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 2400);
}

/* The same saved USDT bank drives scanner stake sizes and the dashboard's 40% guide. */
function getTotalBank() {
  const input = document.getElementById('risk-total-bank');
  const bank = Number(state.config.bank ?? input?.value);
  return Number.isFinite(bank) && bank > 0 ? bank : null;
}

function getPlanCap() {
  const totalBank = getTotalBank();
  return totalBank === null ? null : totalBank * 0.4;
}

function updatePlanRiskNote(exposureUsdt) {
  const note = document.getElementById('hm-risk-note');
  if (!note) return;
  const cap = getPlanCap();
  note.classList.remove('safe', 'warn');
  if (cap === null) {
    note.textContent = 'Укажите общий банк в настройках, чтобы применить 40%-ориентир к плану.';
    return;
  }
  if (exposureUsdt > cap + 0.01) {
    note.classList.add('warn');
    note.textContent = `План ${fmtMoney(exposureUsdt)} выше 40%-ориентира ${fmtMoney(cap)} на ${fmtMoney(exposureUsdt - cap)}. Проверьте риск вручную.`;
  } else if (state.suggestedCapped) {
    note.classList.add('safe');
    note.textContent = `Рекомендация уменьшена до 40%-ориентира (${fmtMoney(cap)}); перепроверьте глубину стакана для нового объёма.`;
  } else {
    note.classList.add('safe');
    note.textContent = `План ${fmtMoney(exposureUsdt)} в пределах ориентира ${fmtMoney(cap)} на один план.`;
  }
}

function updateBankLimit() {
  const input = document.getElementById('risk-total-bank');
  const output = document.getElementById('risk-limit');
  if (!input || !output) return;
  const bank = getTotalBank();
  output.textContent = bank !== null ? fmtMoney(bank * 0.4) : '—';
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
    for (const a of newArbs) {
      if (a && a.slug && !state.seenSlugs.has(a.slug)) {
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
      timings.push(`feed→расчёт ${Number(status.event_to_snapshot_ms).toFixed(1)} мс`);
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
            currentPmText += ` · REST ${currentPm.rest_refresh}`;
          }
          parts.push(currentPmText);
        }
        if (pm.best_ask != null) {
          let pmText = `PM ${pm.outcome || ''} ask ${(Number(pm.best_ask) * 100).toFixed(1)}¢`;
          if (pm.average_ask != null) pmText += ` / avg ${(Number(pm.average_ask) * 100).toFixed(1)}¢`;
          if (pm.book_age_sec != null) pmText += ` · стакан ${Number(pm.book_age_sec).toFixed(1)}с`;
          parts.push(pmText);
        }
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

function updateArbCard(card, a) {
  // Dual-read is presentation-only: filters and the selected detail/action path stay on legacy arbs.
  const legacy = a;
  const opportunity = state.opportunitiesBySlug.get(String(legacy.slug || ''));
  a = window.PolyArbMarketDisplay.opportunityCardDisplay(opportunity, legacy) || legacy;
  const slug = String(a.slug || '');
  const legs = Array.isArray(a.bets) ? a.bets : [];
  const pmLeg = legs.find(l => l && l.source === 'Polymarket') || {};
  const bbLeg = legs.find(l => l && l.source && l.source !== 'Polymarket') || {};
  const phase = ['live', 'prematch', 'unknown'].includes(a.phase) ? a.phase : 'unknown';
  const phaseLabels = { live: 'LIVE', prematch: 'ПРЕМАТЧ', unknown: 'СТАТУС ?' };
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

  const tags = [`<span class="tag ${escapeHtml(sportClass(a.sport))}">${escapeHtml(a.sport || '')}</span>`];
  tags.push(`<span class="tag phase-${phase}">${phaseLabels[phase]}</span>`);
  if (a.is_streamer) tags.push('<span class="tag streamer">СТРИМЕР</span>');
  const pmAge = pmLeg.pm_age || a.pm_age;
  if (pmAge && pmAge > 60) tags.push(`<span class="tag pm-age">PM ${Math.floor(pmAge)}с</span>`);
  const sourceAge = a.source_age_sec == null ? NaN : Number(a.source_age_sec);
  if (Number.isFinite(sourceAge)) {
    const ageLabel = `ДАННЫЕ ${Math.floor(sourceAge)}с`;
    const ageClass = a.source_state === 'stale' ? 'tag pm-age' : 'tag';
    tags.push(`<span class="${ageClass}">${ageLabel}${a.source_state === 'stale' ? ' · УСТАРЕЛИ' : ''}</span>`);
  } else {
    tags.push('<span class="tag pm-age">СВЕЖЕСТЬ ?</span>');
  }
  const opportunityStatusLabels = {
    stale: 'КОТИРОВКИ УСТАРЕЛИ',
    suspended: 'ВОЗМОЖНОСТЬ ПРИОСТАНОВЛЕНА',
    withdrawn: 'ВОЗМОЖНОСТЬ СНЯТА',
  };
  if (opportunityStatusLabels[a.opportunity_status]) {
    tags.push(`<span class="tag pm-age">${opportunityStatusLabels[a.opportunity_status]}</span>`);
  }
  setCardMarkup(card, '.arb-tags', tags.join(''), '_renderedTags');
  setCardText(card, '.arb-market-value', window.PolyArbMarketDisplay.marketTypeLabel(a.market_type));

  const bkClass = bbLeg.source === '1win' ? 'onewin' : bbLeg.source === 'Polymarket' ? 'other' : 'bb';
  const chips = `<span class="bk-chip ${bkClass}">${escapeHtml(bbLeg.source || 'БК')}</span>` +
    '<span class="bk-chip pm">Polymarket</span>';
  setCardMarkup(card, '.arb-bks', chips, '_renderedBooks');
  const stakes = Array.isArray(a.stakes) ? a.stakes : [];
  const legRows = legs.map((leg, i) => renderArbLegRow(leg, stakes[i])).filter(Boolean).join('');
  setCardMarkup(card, '.arb-stakes', legRows, '_renderedStakes');
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
  const phase = ['live', 'prematch', 'unknown'].includes(row.phase) ? row.phase : 'unknown';
  const phaseLabels = { live: 'LIVE', prematch: 'ПРЕМАТЧ', unknown: 'СТАТУС ?' };

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
  tags.push(`<span class="tag phase-${phase}">${phaseLabels[phase]}</span>`);
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
  state.activeSlug = slug;
  state.activeSnapshotId = null;
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
  state.activeSlug = null;
  state.activeSnapshotId = String(snapshotId);
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
  state.activeSlug = null;
  state.activeSnapshotId = null;
  const ph = document.getElementById('detail-placeholder');
  const body = document.getElementById('detail-body');
  if (ph) { ph.classList.remove('hidden'); ph.style.display = 'flex'; }
  if (body) { body.classList.add('hidden'); body.style.display = 'none'; }
  renderArbs();
}

function getActive() {
  if (state.activeSnapshotId) {
    const row = currentPinnedSnapshots().find(item => pinnedSnapshotId(item) === state.activeSnapshotId);
    return row ? { ...row, inactive_snapshot: true } : null;
  }
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
  const frozen = Boolean(a.inactive_snapshot);

  const eventTitle = a.title && a.q && a.title !== a.q
    ? `${a.title} · ${a.q}` : (a.title || a.q || a.slug);
  document.getElementById('det-title').textContent = eventTitle + (frozen ? ' · ЗАКРЕПЛЁННЫЙ СНИМОК' : '');
  const tags = [`<span class="tag mtype">${escapeHtml(window.PolyArbMarketDisplay.marketTypeLabel(a.market_type))}</span>`,
                `<span class="tag ${escapeHtml(sportClass(a.sport))}">${escapeHtml(a.sport || '')}</span>`];
  const detailPhase = ['live','prematch','unknown'].includes(a.phase) ? a.phase : 'unknown';
  const detailPhaseLabel = { live: 'LIVE', prematch: 'ПРЕМАТЧ', unknown: 'СТАТУС НЕ ПОДТВЕРЖДЁН' }[detailPhase];
  tags.push(`<span class="tag phase-${detailPhase}">${detailPhaseLabel}</span>`);
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
  if (frozen) {
    state.bookRequestId = (state.bookRequestId || 0) + 1;
    state.chartRequestId = (state.chartRequestId || 0) + 1;
    document.getElementById('pm-book-title').textContent = 'ЗАПОМНЕННАЯ КОТИРОВКА';
    document.getElementById('pm-book-body').innerHTML = '<tr><td colspan="3" class="empty-cell">Актуальный стакан не загружается для снимка</td></tr>';
    document.getElementById('pm-book-foot').textContent = 'Значения сохранены на момент фиксации';
    const canvas = document.getElementById('price-chart');
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = '#4a5a52';
    context.font = '12px monospace';
    context.fillText('История цен не обновляется для снимка', 12, 24);
    document.getElementById('chart-title').textContent = 'ИСТОРИЯ НЕ ЗАГРУЖАЕТСЯ';
    document.getElementById('chart-foot').textContent = 'Снимок не содержит живую историю котировок';
    document.getElementById('chart-delta').textContent = 'неактивно';
  } else {
    document.getElementById('pm-book-title').textContent = 'СТАКАН POLYMARKET';
    document.getElementById('chart-title').textContent = 'ИСТОРИЯ ЦЕНЫ';
    loadPmBook(a.slug);
    loadPriceChart(a.slug);
  }
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
    renderHowMuch(a);

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
    foot.textContent = footTxt;
    const avg = book.avg_for_size;
    document.getElementById('hm-avg').textContent = avg && avg.avg_price
      ? (avg.avg_price * 100).toFixed(1) + '¢ · ' + requestedUsd.toFixed(2) + ' USDT' + (avg.full ? '' : ' · неполный объём') : '—';
  } catch (e) {
    if (requestId === state.bookRequestId && state.activeSlug === slug) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Ошибка сети</td></tr>';
    }
  }
}

/* ============= PRICE CHART ============= */
async function loadPriceChart(slug) {
  const requestId = state.chartRequestId = (state.chartRequestId || 0) + 1;
  const canvas = document.getElementById('price-chart');
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * (window.devicePixelRatio || 1);
  const h = canvas.height = 180 * (window.devicePixelRatio || 1);
  ctx.scale(1, 1);
  ctx.clearRect(0, 0, w, h);

  try {
    const r = await fetch(`/api/price_history/${encodeURIComponent(slug)}?max_points=120`);
    const j = await r.json();
    if (requestId !== state.chartRequestId || state.activeSlug !== slug) return;
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
  document.getElementById('kpi-odds-label').textContent = frozen ? 'КЭФЫ В СНИМКЕ' : 'КЭФЫ В РАСЧЁТЕ';
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
  const cachedBook = !frozen && pmBookCache[state.activeSlug]?.outcome === pmLeg.outcome
    ? pmBookCache[state.activeSlug] : null;
  const pmBestAsk = Number(cachedBook?.best_ask || pmLeg.pm_best_ask || a.pm_best_ask || 0);
  const pmPriceLabel = document.getElementById('hm-pm-price-label');
  if (pmPriceLabel) pmPriceLabel.textContent = frozen ? 'ASK В СНИМКЕ' : 'ЦЕНА ДОЛИ · ASK';
  const pmPriceText = pmBestAsk > 0 && pmBestAsk < 1 ? (pmBestAsk * 100).toFixed(1) + '¢' : '—';
  setLiveQuoteValue(document.getElementById('hm-pm-price'), pmPriceText, quoteScope);
  const avg = cachedBook?.avg_for_size;
  const enteredUsd = Number(document.getElementById('hm-pm-stake').value);
  if (frozen) {
    const capturedAvg = Number(pmLeg.pm_avg_ask || 0);
    document.getElementById('hm-avg-label').textContent = 'Средняя при фиксации';
    document.getElementById('hm-avg').textContent = capturedAvg > 0
      ? (capturedAvg * 100).toFixed(1) + '¢ · сохранённое значение' : '—';
  } else {
    document.getElementById('hm-avg-label').textContent = 'Средняя по стакану';
    document.getElementById('hm-avg').textContent = avg && avg.avg_price && Math.abs(cachedBook.requested_usd - enteredUsd) < 0.005
      ? (avg.avg_price * 100).toFixed(1) + '¢ · ' + cachedBook.requested_usd.toFixed(2) + ' USDT' + (avg.full ? '' : ' · неполный объём') : '—';
  }
  const sharesPrice = Number(pmLeg.pm_avg_ask || pmLeg.pm_best_ask || a.pm_best_ask || 0);
  const capturedShares = Number(pmLeg.pm_target_shares || 0);
  const shares = frozen && capturedShares > 0
    ? capturedShares : (sharesPrice > 0 ? pmSt / sharesPrice : 0);
  document.getElementById('hm-shares-label').textContent = frozen ? 'Доли в снимке' : 'Оценка долей PM';
  document.getElementById('hm-shares').textContent = shares > 0
    ? `${shares.toLocaleString('ru-RU', { maximumFractionDigits: 4 })} долей` : '—';
  setLiveQuoteValue(document.getElementById('hm-eff'), pmLeg.o ? fmtOdds(pmLeg.o) : '—', quoteScope);

  recalcHowMuch();

  const actionHint = document.getElementById('action-hint');
  if (actionHint) actionHint.textContent = frozen
    ? 'Это сохранённый неактивный снимок, не текущие котировки. Ссылки открывают площадки вручную; перед любыми действиями сверьте исходы и цены. Автоматических ставок нет.'
    : 'Оценка по снимку; после изменения суммы требуется перепроверить обе стороны и глубину стакана. Сделка записывается вручную.';

  const buyBtn = document.getElementById('btn-buy-pm');
  buyBtn.textContent = `ОТКРЫТЬ POLYMARKET · ${parseFloat(document.getElementById('hm-pm-stake').value || 0).toFixed(2)} USDT ↗`;
  const filledBtn = document.getElementById('btn-filled');
  filledBtn.disabled = frozen;
  filledBtn.title = frozen ? 'Неактивный снимок не является фактической сделкой' : 'Записать исполненную вручную сделку';
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
  const frozen = Boolean(a.inactive_snapshot);
  const bbStake = parseFloat(document.getElementById('hm-bb-stake').value || '0');
  const pmStakeUsdt = parseFloat(document.getElementById('hm-pm-stake').value || '0');

  const pmLeg = (a.bets || []).find(l => l.source === 'Polymarket') || {};
  const bbLeg = (a.bets || []).find(l => l.source && l.source !== 'Polymarket') || {};
  const pmO = pmLeg.o || 0, bbO = bbLeg.o || 0;

  const pay1 = pmStakeUsdt * pmO;
  const pay2 = bbStake * bbO;
  const bank = pmStakeUsdt + bbStake;
  const minPay = Math.min(pay1, pay2);
  const profit = minPay - bank;

  document.getElementById('hm-total').textContent = fmtMoney(bank);
  document.getElementById('hm-payout').textContent = fmtMoney(minPay);
  const profitEl = document.getElementById('hm-profit');
  profitEl.textContent = (profit >= 0 ? '+' : '') + fmtMoney(profit);
  profitEl.style.color = profit >= 0 ? 'var(--green)' : 'var(--red)';
  if (frozen) {
    document.getElementById('hm-risk-note').textContent = 'Суммы ставок и котировки заморожены из снимка; изменение общего банка на сохранённые значения не влияет.';
  } else {
    updatePlanRiskNote(bank);
  }

  document.getElementById('btn-buy-pm').textContent =
    `ОТКРЫТЬ POLYMARKET · ${pmStakeUsdt.toFixed(2)} USDT ↗`;

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
  const rawAsk = Number(cachedAsk || pmLeg.pm_best_ask || (a && a.pm_best_ask));
  document.getElementById('calc-pm-price').value = rawAsk > 0 && rawAsk < 1 ? (rawAsk * 100).toFixed(1) : '0';
  document.getElementById('calc-pm-stake').value = pmStakeUsdt.toFixed(2);
  updateCalcHint();
}

function updateCalcHint() {
  const bkO = parseFloat(document.getElementById('calc-bk-odds').value || '0');
  const bkSt = parseFloat(document.getElementById('calc-bk-stake').value || '0');
  const pmP = parseFloat(document.getElementById('calc-pm-price').value || '0') / 100;
  const pmSt = parseFloat(document.getElementById('calc-pm-stake').value || '0');
  const FEE = state.config.pm_fee_pct ?? 0.04;

  const pmO = pmP > 0 ? (1 / pmP) / (1 + FEE) : 0;
  const bank = bkSt + pmSt;
  const payBK = bkSt * bkO;
  const payPM = pmSt * pmO;
  const minPay = Math.min(payBK, payPM);
  const profit = minPay - bank;
  const roi = bank > 0 ? profit / bank : 0;

  const hint = document.getElementById('calc-hint');
  if (!(bkO > 1 && bkSt > 0 && pmP > 0 && pmP < 1 && pmSt > 0)) {
    hint.textContent = 'Укажи обе суммы в USDT, кэф БК и цену PM';
    hint.style.color = 'var(--dim)';
    return;
  }
  const cap = getPlanCap();
  const overCap = cap !== null && bank > cap + 0.01;
  const riskText = cap === null
    ? 'Общий банк не задан: контроль 40% не рассчитан.'
    : overCap ? `ПРЕДУПРЕЖДЕНИЕ: выше 40%-ориентира ${fmtMoney(cap)}.`
      : `В пределах 40%-ориентира ${fmtMoney(cap)} на один план.`;
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
  const statusRoi = Number(scanStatus?.min_roi_pct);
  if (Number.isFinite(statusRoi) && statusRoi >= 0 && statusRoi <= 100) {
    state.config.active_min_roi_pct = statusRoi;
  }
  const roiInput = document.getElementById('f-minroi');
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
      <span class="mark">✓</span>
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
    document.getElementById('cfg-bank').value = j.bank || 100;
    document.getElementById('cfg-fee').value = ((j.pm_fee_pct ?? 0.04) * 100).toFixed(1);
    document.getElementById('cfg-safe').value = (j.safe_fork_threshold || 0.45);
    syncRuntimeControlsFromStatus(state.scanStatus);
  } catch (e) {}
  document.getElementById('modal-config').classList.remove('hidden');
}
function closeConfig() { document.getElementById('modal-config').classList.add('hidden'); }
async function saveConfig() {
  const payload = {
    bank: parseFloat(document.getElementById('cfg-bank').value || '100'),
    currency: 'USDT',
    pm_fee_pct: parseFloat(document.getElementById('cfg-fee').value || '4') / 100,
    safe_fork_threshold: parseFloat(document.getElementById('cfg-safe').value || '0.45'),
  };
  if (await postRuntimeConfig(payload, 'Настройки сохранены')) {
    const bankInput = document.getElementById('risk-total-bank');
    if (bankInput) bankInput.value = Number(state.config.bank || 100).toFixed(2);
    updateBankLimit();
    closeConfig();
  }
}
async function saveMinRoi() {
  const input = document.getElementById('f-minroi');
  const value = Number(input?.value);
  if (!Number.isFinite(value) || value < 0 || value > 100) {
    toast('Минимальный ROI должен быть от 0 до 100%', true);
    syncRuntimeControlsFromStatus(state.scanStatus);
    return;
  }
  if (!await postRuntimeConfig(
    { min_roi_pct: value }, `Порог ROI ${value}% передан сканеру`,
  )) syncRuntimeControlsFromStatus(state.scanStatus);
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
  const FEE = state.config.pm_fee_pct ?? 0.04;
  const pmO = pmPrice > 0 ? (1 / pmPrice) / (1 + FEE) : 0;
  const bank = bbSt + pmUsd;
  const pay1 = bbSt * bbO;
  const pay2 = pmUsd * pmO;
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
    bb_odds: bbO, bb_stake_usdt: bbSt,
    pm_price: pmPrice, pm_amount_usdt: pmUsd, rate: 1.0,
    bank, payout: minPay, profit, roi,
  };
}

async function saveTrade() {
  const a = state.editing  ; if (!a || !a._calc) return;
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
              '<td>' + Number(b.ask_depth_usd || 0).toFixed(2) + ' USDT</td></tr>'
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
      const allowedOrigin = window.POLYARB_ONEWIN_ORIGIN;
      if (eventUrl.protocol !== 'https:' || eventUrl.username || eventUrl.password
          || !allowedOrigin || eventUrl.origin !== allowedOrigin) {
        throw new Error('Недоверенный адрес события 1win');
      }
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
  if (a.pm_event_url) {
    try {
      const eventUrl = new URL(a.pm_event_url, window.location.origin);
      if (eventUrl.protocol !== 'https:' || eventUrl.hostname !== 'polymarket.com') {
        throw new Error('Небезопасная ссылка на Polymarket');
      }
      window.open(eventUrl.href, '_blank', 'noopener,noreferrer');
    } catch (e) {
      toast('Не удалось открыть страницу события Polymarket', true);
    }
    return;
  }
  try {
    const r = await fetch('/api/pm_url/' + encodeURIComponent(a.slug));
    const j = await r.json();
    if (j.url) window.open(j.url, '_blank', 'noopener,noreferrer');
    else toast('PM URL \u043D\u0435 \u043D\u0430\u0439\u0434\u0435\u043D', true);
  } catch (e) { toast('\u041E\u0448\u0438\u0431\u043A\u0430: ' + e.message, true); }
}
function markFilled() {
  const a = getActive();
  if (!a) return;
  if (a.inactive_snapshot) {
    toast('Неактивный снимок нельзя записать как исполненную сделку', true);
    return;
  }
  openEdit(a.slug);
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
  if (minRoiEl) {
    minRoiEl.addEventListener('input', applyFilters);
    minRoiEl.addEventListener('change', saveMinRoi);
  }
  const bkSettingsBtn = document.getElementById('btn-bk-settings');
  if (bkSettingsBtn) bkSettingsBtn.addEventListener('click', openBkModal);

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
  if (hmBB) hmBB.addEventListener('input', () => { state.detailEdited = true; state.suggestedCapped = false; recalcHowMuch(); });
  const hmPM = document.getElementById('hm-pm-stake');
  if (hmPM) hmPM.addEventListener('input', () => {
    state.detailEdited = true;
    state.suggestedCapped = false;
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
    if (state.activeSlug) { loadPriceChart(state.activeSlug); loadPmBook(state.activeSlug); }
  }, 10000);
  setInterval(checkWsFreshness, 5000);
  document.addEventListener('visibilitychange', checkWsFreshness);
  setInterval(loadDaily, 30000);
  setInterval(() => {
    renderArbs();
    if (state.activeSnapshotId) refreshDetail();
  }, 1000);

  loadDaily();
  loadRuntimeConfig();
  connectWS();
}

document.addEventListener('DOMContentLoaded', init);
