'use strict';
const assert = require('node:assert/strict');
const { fitSuggestedStakes } = require('../static/bankroll_plan.js');

const nearLimit = fitSuggestedStakes(99.51, 0.49, 100);
assert.equal(nearLimit.capped, false);
assert.ok(nearLimit.exposureUsdt <= 100 + 1e-8);
assert.equal(nearLimit.bookmakerUsdt, 99.51);
assert.equal(nearLimit.polymarketUsdt, 0.49);

const scaled = fitSuggestedStakes(150, 85, 100);
assert.equal(scaled.capped, true);
assert.ok(scaled.exposureUsdt <= 100 + 1e-8);
assert.ok(scaled.bookmakerUsdt > 0);
assert.ok(scaled.polymarketUsdt > 0);

const alreadyUnder = fitSuggestedStakes(70, 85, 200);
assert.equal(alreadyUnder.capped, false);
assert.equal(alreadyUnder.exposureUsdt, 155);

const uncapped = fitSuggestedStakes(1000.4, 850.3, null);
assert.equal(uncapped.bookmakerUsdt, 1000.4);
assert.equal(uncapped.polymarketUsdt, 850.3);

const invalidInputs = fitSuggestedStakes(NaN, 10, 0);
assert.deepEqual(invalidInputs, {
  bookmakerUsdt: 0, polymarketUsdt: 0, exposureUsdt: 0, capped: true,
});

console.log('USDT bankroll planner tests: OK');
