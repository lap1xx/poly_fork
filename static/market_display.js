(function (root) {
  const MARKET_LABELS = Object.freeze({
    MATCH: 'Исход матча',
    MAP1: 'Победитель карты 1',
    MAP2: 'Победитель карты 2',
    MAP3: 'Победитель карты 3',
    MAP4: 'Победитель карты 4',
    MAP5: 'Победитель карты 5',
    SET2: 'Исход сета 2',
    SET3: 'Исход сета 3',
    TOTAL: 'Тотал',
    HANDICAP: 'Фора',
  });

  function marketTypeLabel(value) {
    const raw = String(value == null ? '' : value).trim();
    if (!raw) return 'Тип рынка не указан';
    const key = raw.toUpperCase().replace(/[\s_-]+/g, '');
    return MARKET_LABELS[key] || `Другой рынок · ${raw}`;
  }

  function numberOrZero(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : 0;
  }

  function legDisplay(leg, stake) {
    const source = String(leg && leg.source || '');
    const isPolymarket = source === 'Polymarket';
    const rawPrice = isPolymarket
      ? numberOrZero(leg.pm_avg_ask || leg.pm_best_ask)
      : 0;
    const amount = Number(stake);
    const quoteAge = isPolymarket
      ? (leg.pm_book_age_sec ?? leg.pm_age ?? null)
      : (leg.odds_age_sec ?? null);
    return {
      source,
      isPolymarket,
      outcome: String(leg && leg.outcome || 'исход не указан'),
      stake: Number.isFinite(amount) && amount > 0 ? amount : 0,
      odds: numberOrZero(leg && leg.o),
      pmPriceCents: rawPrice > 0 && rawPrice < 1 ? rawPrice * 100 : null,
      quoteAgeSec: quoteAge,
    };
  }

  const api = Object.freeze({ marketTypeLabel, legDisplay });
  if (root) root.PolyArbMarketDisplay = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
