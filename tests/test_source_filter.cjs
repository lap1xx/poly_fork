'use strict';

const assert = require('node:assert/strict');
const {
  activeSourceIds,
  getBookmakerSourceId,
  includesSelectedProvider,
} = require('../static/source_filter.js');

const pmOneWin = {
  bets: [
    { source: 'Polymarket', outcome: 'Yes' },
    { source: '1win', outcome: 'No' },
  ],
};
const pmBetBoom = {
  bets: [
    { source: 'BetBoom', outcome: 'Yes' },
    { source: 'PM', outcome: 'No' },
  ],
};

assert.equal(getBookmakerSourceId(pmOneWin), '1win');
assert.equal(getBookmakerSourceId(pmBetBoom), 'betboom');
assert.deepEqual(activeSourceIds(null), ['1win']);
assert.deepEqual(
  activeSourceIds(['1win', 'BetBoom', 'Fonbet']), ['1win', 'betboom'],
);
assert.deepEqual(activeSourceIds([]), []);
assert.equal(includesSelectedProvider(pmOneWin, { '1win': true }, ['1win']), true);
assert.equal(includesSelectedProvider(pmOneWin, { '1win': false }, ['1win']), false);
assert.equal(includesSelectedProvider(
  pmBetBoom, { betboom: true }, ['1win', 'BetBoom'],
), true);
assert.equal(includesSelectedProvider(
  pmBetBoom, { betboom: false }, ['1win', 'BetBoom'],
), false);
assert.equal(includesSelectedProvider(pmBetBoom, { betboom: true }, ['1win']), false);

const invalidPairs = [
  { bets: [{ source: 'Polymarket' }, { source: '1win' }, { source: 'BetBoom' }] },
  { bets: [{ source: 'Polymarket' }, { source: 'Polymarket' }] },
  { bets: [{ source: '1win' }, { source: 'BetBoom' }] },
  { bets: [{ source: 'Polymarket' }, { source: 'Fonbet' }] },
  { bets: [{ source: 'Polymarket' }] },
  null,
];
for (const pair of invalidPairs) {
  assert.equal(getBookmakerSourceId(pair), null);
  assert.equal(includesSelectedProvider(
    pair, { '1win': true, betboom: true }, ['1win', 'BetBoom'],
  ), false);
}

// Filtering reads the existing two-leg signal; it never mutates or combines legs.
const before = JSON.stringify(pmOneWin);
assert.equal(includesSelectedProvider(pmOneWin, { '1win': true }, ['1win']), true);
assert.equal(JSON.stringify(pmOneWin), before);

console.log('Dashboard source filter tests: OK');
