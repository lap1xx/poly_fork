(function (root) {
  'use strict';

  function sourceId(source) {
    const normalized = String(source || '').trim().toLowerCase().replace(/[\s_-]+/g, '');
    if (normalized === 'polymarket' || normalized === 'pm') return 'polymarket';
    if (normalized === '1win' || normalized === 'onewin') return '1win';
    if (normalized === 'betboom') return 'betboom';
    return null;
  }

  function getBookmakerSourceId(arb) {
    const legs = arb && Array.isArray(arb.bets) ? arb.bets : [];
    if (legs.length !== 2) return null;

    const polymarketLegs = legs.filter(leg => sourceId(leg && leg.source) === 'polymarket');
    if (polymarketLegs.length !== 1) return null;

    const bookmakerLeg = legs.find(leg => sourceId(leg && leg.source) !== 'polymarket');
    const bookmakerId = sourceId(bookmakerLeg && bookmakerLeg.source);
    return bookmakerId === '1win' || bookmakerId === 'betboom' ? bookmakerId : null;
  }

  function activeSourceIds(enabledSources, supportedIds) {
    const supported = Array.isArray(supportedIds) ? supportedIds : ['1win', 'betboom'];
    if (!Array.isArray(enabledSources)) {
      return supported.includes('1win') ? ['1win'] : [];
    }
    return [...new Set(enabledSources
      .map(sourceId)
      .filter(id => id && supported.includes(id)))];
  }

  function includesSelectedProvider(arb, selected, enabledSources) {
    const id = getBookmakerSourceId(arb);
    if (!id) return false;
    if (!activeSourceIds(enabledSources).includes(id)) return false;
    return !selected || selected[id] !== false;
  }

  const api = {
    sourceId, getBookmakerSourceId, activeSourceIds, includesSelectedProvider,
  };
  root.PolyArbSourceFilter = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
