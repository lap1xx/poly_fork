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
      ? (leg.pm_book_age_sec ?? (leg.pm_book_age_source === 'local_receipt' ? null : leg.pm_age) ?? null)
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

  function opportunityCardDisplay(opportunity, legacy) {
    if (!opportunity || opportunity.schema_version !== 1
        || !['active', 'stale', 'suspended', 'withdrawn'].includes(opportunity.status)
        || typeof opportunity.id !== 'string' || !opportunity.id
        || !legacy || !Array.isArray(legacy.bets)
        || legacy.bets.length !== 2 || !Array.isArray(legacy.stakes)
        || legacy.stakes.length !== 2) return null;

    const market = opportunity.market;
    const mapping = opportunity.mapping;
    const calculation = opportunity.calculation;
    const legs = opportunity.legs;
    if (!market || typeof market.slug !== 'string' || market.slug !== legacy.slug
        || typeof market.question !== 'string' || !market.question
        || typeof market.sport !== 'string' || !market.sport
        || typeof market.market_type !== 'string' || !market.market_type
        || !['live', 'prematch', 'unknown'].includes(market.phase)
        || !mapping || mapping.status !== 'accepted'
        || !calculation || typeof calculation.roi_fraction !== 'number'
        || !Number.isFinite(calculation.roi_fraction)
        || typeof calculation.total_stake !== 'number'
        || !Number.isFinite(calculation.total_stake) || calculation.total_stake < 0
        || typeof calculation.projected_profit !== 'number'
        || !Number.isFinite(calculation.projected_profit)
        || !Array.isArray(legs) || legs.length !== 2) return null;

    const displayLegs = [];
    const stakes = [];
    let pmAgeSec = null;
    let bookmakerAgeSec = null;
    for (const leg of legs) {
      if (!leg || typeof leg.source !== 'string' || !leg.source
          || typeof leg.outcome !== 'string' || !leg.outcome
          || typeof leg.age_ms !== 'number' || !Number.isFinite(leg.age_ms)
          || leg.age_ms < 0 || typeof leg.stake !== 'number'
          || !Number.isFinite(leg.stake) || leg.stake < 0) return null;

      const isPm = leg.source_kind === 'prediction_market';
      if (isPm) {
        if (leg.price_unit !== 'probability' || typeof leg.price !== 'number'
            || !Number.isFinite(leg.price) || leg.price <= 0 || leg.price > 1
            || !leg.token_id) return null;
      } else if (leg.source_kind !== 'bookmaker'
          || leg.price_unit !== 'decimal_odds' || typeof leg.price !== 'number'
          || !Number.isFinite(leg.price) || leg.price <= 1) {
        return null;
      }

      const sourceKey = String(leg.source).trim().toLowerCase();
      const outcomeKey = String(leg.outcome).trim().toLowerCase();
      const legacyIndex = legacy.bets.findIndex(candidate => candidate
        && String(candidate.source || '').trim().toLowerCase() === sourceKey
        && String(candidate.outcome || '').trim().toLowerCase() === outcomeKey);
      if (legacyIndex < 0) return null;
      const legacyLeg = legacy.bets[legacyIndex];
      if (isPm && String(legacyLeg.token || '') !== String(leg.token_id)) return null;
      const quoteAgeSec = leg.age_ms / 1000;
      const effectiveOdds = isPm ? legacyLeg.o : leg.price;
      const pmFeeSchedule = opportunity.calculation.pm_fee_schedule;
      if (typeof effectiveOdds !== 'number' || !Number.isFinite(effectiveOdds)) return null;

      displayLegs.push({
        source: leg.source,
        outcome: leg.outcome,
        token: isPm ? leg.token_id : legacyLeg.token,
        o: effectiveOdds,
        pm_best_ask: isPm ? (legacyLeg.pm_best_ask ?? leg.price) : undefined,
        pm_avg_ask: isPm ? leg.price : undefined,
        pm_book_age_sec: isPm ? quoteAgeSec : undefined,
        pm_book_age_source: isPm ? legacyLeg.pm_book_age_source : undefined,
        pm_age: isPm ? (legacyLeg.pm_age ?? quoteAgeSec) : undefined,
        pm_fee_enabled: isPm && pmFeeSchedule ? pmFeeSchedule.enabled : undefined,
        pm_fee_rate: isPm && pmFeeSchedule ? pmFeeSchedule.rate : undefined,
        pm_fee_exponent: isPm && pmFeeSchedule ? pmFeeSchedule.exponent : undefined,
        pm_fee_source: isPm && pmFeeSchedule ? pmFeeSchedule.source : undefined,
        odds_age_sec: isPm ? undefined : quoteAgeSec,
        event_url: typeof leg.event_url === 'string' ? leg.event_url : undefined,
      });
      stakes.push(leg.stake);
      if (isPm) pmAgeSec = quoteAgeSec;
      else bookmakerAgeSec = quoteAgeSec;
    }
    if (pmAgeSec === null || bookmakerAgeSec === null) return null;

    return {
      ...legacy,
      slug: market.slug,
      q: market.question,
      sport: market.sport,
      market_type: market.market_type,
      phase: market.phase,
      roi: calculation.roi_fraction,
      bank: typeof calculation.total_stake === 'number'
        ? calculation.total_stake : legacy.bank,
      profit: typeof calculation.projected_profit === 'number'
        ? calculation.projected_profit : legacy.profit,
      bets: displayLegs,
      stakes,
      pm_age: pmAgeSec,
      source_age_sec: bookmakerAgeSec,
      opportunity_status: opportunity.status,
    };
  }

  function indexOpportunities(envelope) {
    const indexed = new Map();
    if (!envelope || envelope.schema_version !== 1
        || !Array.isArray(envelope.opportunities)) return indexed;
    for (const opportunity of envelope.opportunities) {
      const slug = opportunity && opportunity.market && opportunity.market.slug;
      if (typeof slug === 'string' && slug) indexed.set(slug, opportunity);
    }
    return indexed;
  }

  const api = Object.freeze({
    marketTypeLabel, legDisplay, opportunityCardDisplay, indexOpportunities,
  });
  if (root) root.PolyArbMarketDisplay = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
