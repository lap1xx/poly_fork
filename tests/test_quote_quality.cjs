'use strict';

const assert = require('node:assert/strict');
const { formatAgeAtCalculation } = require('../static/quote_quality.js');

assert.equal(
  formatAgeAtCalculation(23.018, 'Котировка 1win'),
  'Котировка 1win: 23.0 с на момент расчёта',
);
assert.equal(
  formatAgeAtCalculation(0, 'Стакан PM'),
  'Стакан PM: 0.0 с на момент расчёта',
);
for (const missing of [null, undefined, '', 'not-a-time', NaN, Infinity, -0.1]) {
  assert.equal(
    formatAgeAtCalculation(missing, 'Котировка'),
    'Котировка: возраст неизвестен',
  );
}
assert.equal(formatAgeAtCalculation(2, ''), 'Котировка: 2.0 с на момент расчёта');

console.log('Dashboard quote quality tests: OK');
