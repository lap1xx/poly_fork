'use strict';
const assert = require('node:assert/strict');
const {
  marketTypeLabel, legDisplay, opportunityCardDisplay, indexOpportunities,
} = require('../static/market_display.js');

assert.equal(marketTypeLabel('MATCH'), 'Исход матча');
assert.equal(marketTypeLabel('MAP3'), 'Победитель карты 3');
assert.equal(marketTypeLabel(' set-2 '), 'Исход сета 2');
assert.equal(marketTypeLabel('TOTAL'), 'Тотал');
assert.equal(marketTypeLabel('HANDICAP'), 'Фора');
assert.equal(marketTypeLabel('player-prop'), 'Другой рынок · player-prop');
assert.equal(marketTypeLabel(''), 'Тип рынка не указан');

const bookmakerLeg = legDisplay({
  source: '1win', outcome: 'Chinggis Warriors', o: 5.13, odds_age_sec: 2.4,
}, 40);
assert.deepEqual(bookmakerLeg, {
  source: '1win', isPolymarket: false, outcome: 'Chinggis Warriors',
  stake: 40, odds: 5.13, pmPriceCents: null, quoteAgeSec: 2.4,
});

const pmLeg = legDisplay({
  source: 'Polymarket', outcome: '100 Thieves', o: 1.15,
  pm_best_ask: 0.83, pm_avg_ask: 0.84, pm_book_age_sec: 1.7,
}, 60);
assert.deepEqual(pmLeg, {
  source: 'Polymarket', isPolymarket: true, outcome: '100 Thieves',
  stake: 60, odds: 1.15, pmPriceCents: 84, quoteAgeSec: 1.7,
});
assert.equal(legDisplay({ source: '1win', o: 2.2 }, -5).stake, 0);
assert.equal(legDisplay({ source: 'Polymarket', pm_best_ask: 1.2 }, 0).pmPriceCents, null);

const legacyArb = {
  slug: 'dual-read-event', q: 'Legacy question', sport: 'legacy-sport',
  market_type: 'legacy-type', phase: 'unknown', age_sec: 4,
  bets: [
    { source: 'Polymarket', outcome: 'Alpha', token: 'pm-dual-token', o: 1.15, pm_best_ask: 0.82 },
    { source: '1win', outcome: 'Bravo', o: 4.2 },
  ],
  stakes: [20, 20], roi: 0.03, source_state: 'fresh', is_streamer: true,
};
const opportunityV1 = {
  schema_version: 1, id: 'opp_v1_dual-read', status: 'stale',
  market: {
    slug: 'dual-read-event', question: 'Normalized question', sport: 'cs2',
    market_type: 'MATCH', phase: 'prematch',
  },
  mapping: { status: 'accepted' },
  legs: [
    {
      source: 'Polymarket', source_kind: 'prediction_market', outcome: 'Alpha',
      price: 0.83, price_unit: 'probability', stake: 22.5, age_ms: 1070,
      token_id: 'pm-dual-token', event_url: 'https://polymarket.com/event/dual-read-event',
    },
    {
      source: '1win', source_kind: 'bookmaker', outcome: 'Bravo',
      price: 4.2, price_unit: 'decimal_odds', stake: 17.5, age_ms: 30010,
      event_url: 'https://f1w-7707.com/betting/match/esport/dual-read-event',
    },
  ],
  calculation: {
    roi_fraction: 0.04, total_stake: 40, projected_profit: 1.6,
    pm_fee_schedule: {
      enabled: true, rate: 0.05, exponent: 1,
      source: 'clob_market_info',
    },
  },
};
const dualReadCard = opportunityCardDisplay(opportunityV1, legacyArb);
assert.equal(dualReadCard.q, 'Normalized question');
assert.equal(dualReadCard.sport, 'cs2');
assert.equal(dualReadCard.roi, 0.04);
assert.equal(dualReadCard.opportunity_status, 'stale');
assert.deepEqual(dualReadCard.stakes, [22.5, 17.5]);
assert.equal(dualReadCard.bets[0].o, 1.15); // retain legacy effective-odds semantics
assert.equal(dualReadCard.bets[0].pm_avg_ask, 0.83);
assert.equal(dualReadCard.bets[0].pm_book_age_sec, 1.07);
assert.equal(dualReadCard.bets[0].event_url, opportunityV1.legs[0].event_url);
assert.equal(dualReadCard.bets[0].pm_fee_rate, 0.05);
assert.equal(dualReadCard.bets[0].pm_fee_exponent, 1);
assert.equal(dualReadCard.bets[1].o, 4.2);
assert.equal(dualReadCard.bets[1].odds_age_sec, 30.01);
assert.equal(dualReadCard.bets[1].event_url, opportunityV1.legs[1].event_url);
assert.equal(dualReadCard.source_age_sec, 30.01);
assert.equal(dualReadCard.age_sec, 4); // UI-only metadata remains legacy sourced
assert.equal(dualReadCard.is_streamer, true);
assert.equal(opportunityCardDisplay({ ...opportunityV1, schema_version: 2 }, legacyArb), null);
assert.equal(
  opportunityCardDisplay({ ...opportunityV1, market: { ...opportunityV1.market, slug: 'other' } }, legacyArb),
  null,
);
assert.equal(
  opportunityCardDisplay({ ...opportunityV1, mapping: { status: 'ambiguous' } }, legacyArb),
  null,
);
const indexed = indexOpportunities({ schema_version: 1, opportunities: [opportunityV1] });
assert.equal(indexed.get('dual-read-event'), opportunityV1);
assert.equal(indexOpportunities({ schema_version: 2, opportunities: [opportunityV1] }).size, 0);
console.log('Dashboard market display tests: OK');
