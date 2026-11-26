// Execute the real client filter in a Node VM with a tiny fake DOM; no browser.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const nodes = {
  'f-cs2': {checked: true}, 'f-dota2': {checked: true},
  'f-lol': {checked: true}, 'f-valorant': {checked: true},
  'f-tennis': {checked: true}, 'f-minroi': {value: '0'},
};
const ctx = {
  document: {getElementById: id => nodes[id], addEventListener: () => {}},
  window: {PolyArbSourceFilter: {includesSelectedProvider: () => true}},
  localStorage: {getItem: () => null},
  console,
};
vm.createContext(ctx);
vm.runInContext(source + '\nthis._test = {state, applyFilters};', ctx, {filename: 'app.js'});
ctx.renderArbs = () => {};
ctx.updateHeader = () => {};
ctx._test.state.arbs = [
  {slug: 'valorant-demo', sport: 'VALORANT', phase: 'live', roi: 0.04},
  {slug: 'cs2-demo', sport: 'CS2', phase: 'live', roi: 0.03},
];
ctx._test.applyFilters();
assert.deepStrictEqual(Array.from(ctx._test.state.visible, a => a.slug), ['valorant-demo', 'cs2-demo']);
nodes['f-valorant'].checked = false;
ctx._test.applyFilters();
assert.deepStrictEqual(Array.from(ctx._test.state.visible, a => a.slug), ['cs2-demo']);
nodes['f-valorant'].checked = true;
nodes['f-cs2'].checked = false;
ctx._test.applyFilters();
assert.deepStrictEqual(Array.from(ctx._test.state.visible, a => a.slug), ['valorant-demo']);
// A temporarily older HTML document must not break all filters.
delete nodes['f-valorant'];
ctx._test.applyFilters();
assert.strictEqual(ctx._test.state.visible.length, 0);
console.log('Sports filtering: PASS');
