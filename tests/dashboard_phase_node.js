// Test real phase labels in a Node VM; this does not open a browser or backend.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const ctx = {
  document: {getElementById: () => null, addEventListener: () => {}},
  window: {}, localStorage: {getItem: () => null}, console,
};
vm.createContext(ctx);
vm.runInContext(source + '\nthis._phaseTest = {phaseTag};', ctx, {filename: 'app.js'});
const tag = ctx._phaseTest.phaseTag;
const attributed = tag({phase:'prematch', phase_source:'1win', phase_start_difference_sec:300});
assert(attributed.includes('ПРЕМАТЧ · 1win'));
assert(attributed.includes('Polymarket на 5.0 мин'));
assert(tag({phase:'prematch'}).includes('ПРЕМАТЧ</span>'));
assert(tag({phase:'unknown', phase_source:'conflict'}).includes('Источники противоречат'));
assert(tag({phase:'unknown'}, true).includes('СТАТУС НЕ ПОДТВЕРЖДЁН'));
assert(!tag({phase:'live', phase_source:'1win'}).includes('· 1win'));
console.log('Phase labels: PASS');
