'use strict';
const assert = require('node:assert/strict');
const { fitSuggestedStakes } = require('../static/bankroll_plan.js');

const nearLimit = fitSuggestedStakes(99.51, 0.49, 85, 100);
assert.equal(nearLimit.capped, false);
assert.ok(nearLimit.exposureRub <= 100 + 1e-8);
assert.equal(nearLimit.bookmakerRub, 99);
assert.equal(nearLimit.polymarketUsd, 0);

const scaled = fitSuggestedStakes(1500, 850, 85, 2000);
assert.equal(scaled.capped, true);
assert.ok(scaled.exposureRub <= 2000 + 1e-8);
assert.ok(scaled.bookmakerRub > 0);
assert.ok(scaled.polymarketUsd > 0);

const alreadyUnder = fitSuggestedStakes(700, 850, 85, 2000);
assert.equal(alreadyUnder.capped, false);
assert.ok(alreadyUnder.exposureRub <= 1550 + 1e-8);
assert.ok(alreadyUnder.exposureRub <= 2000 + 1e-8);

const uncapped = fitSuggestedStakes(1000.4, 850.3, 85, null);
assert.equal(uncapped.bookmakerRub, 1000);
assert.equal(uncapped.polymarketUsd, 10);

const invalidRate = fitSuggestedStakes(1000, 500, 0, 1000);
assert.deepEqual(invalidRate, {
  bookmakerRub: 0, polymarketUsd: 0, exposureRub: 0, capped: false,
});

console.log('bankroll planner tests: OK');
