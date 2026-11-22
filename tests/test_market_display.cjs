'use strict';
const assert = require('node:assert/strict');
const { marketTypeLabel, legDisplay } = require('../static/market_display.js');

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
console.log('Dashboard market display tests: OK');
