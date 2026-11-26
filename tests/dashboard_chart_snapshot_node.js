const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const nodes = {};
const calls = [];
const context = {};
for (const name of ['setTransform','clearRect','fillText','beginPath','moveTo','lineTo','stroke','arc','fill']) {
  context[name] = (...args) => calls.push({name, args});
}
function element(id) {
  if (!nodes[id]) nodes[id] = {textContent:'', innerHTML:'', hidden:false, style:{}};
  return nodes[id];
}
const canvas = element('price-chart');
Object.assign(canvas, {clientWidth:840, clientHeight:108, width:480, height:220, getContext:() => context});
const requests = [];
let observer;
class FakeResizeObserver {
  constructor(fn) { this.callback = fn; observer = this; }
  observe(target) { this.target = target; }
}
const ctx = {
  document:{getElementById:element, addEventListener(){}},
  localStorage:{getItem(){return null;}},
  window:{devicePixelRatio:2, ResizeObserver:FakeResizeObserver},
  fetch(url){return new Promise(resolve => requests.push({url, resolve}));},
  console,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8') +
  '\nthis.testApi={state,showSnapshotMarketData,showLiveMarketData,preparePriceChart,drawPriceChart,loadPriceChart,observePriceChartResize};', ctx);
const t = ctx.testApi;

async function run() {
  t.showSnapshotMarketData();
  assert.strictEqual(element('pm-book-title').textContent, 'СТАКАН POLYMARKET · СНИМОК');
  assert.strictEqual(element('pm-book-head').hidden, true);
  assert.match(element('pm-book-body').innerHTML, /актуальный стакан/i);
  assert.match(element('pm-book-body').innerHTML, /snapshot-empty-cell/);
  assert.match(element('pm-book-foot').textContent, /на момент снимка/);
  assert.strictEqual(canvas.hidden, true);
  assert.strictEqual(element('chart-snapshot-placeholder').hidden, false);
  assert.strictEqual(element('chart-title').textContent, 'ИСТОРИЯ ЦЕНЫ');
  assert.strictEqual(requests.length, 0);
  assert.strictEqual(calls.length, 0); // no text inside a stretched canvas

  t.showLiveMarketData();
  assert.strictEqual(element('pm-book-head').hidden, false);
  assert.strictEqual(canvas.hidden, false);
  assert.strictEqual(element('chart-snapshot-placeholder').hidden, true);
  t.drawPriceChart([]);
  assert.strictEqual(canvas.width, 1680);
  assert.strictEqual(canvas.height, 216); // based on actual 108 CSS pixels, not hard-coded 180
  assert.deepStrictEqual(calls.find(x => x.name === 'setTransform').args, [2,0,0,2,0,0]);
  const label = calls.find(x => x.name === 'fillText');
  assert.deepStrictEqual(label.args, ['Недостаточно данных',420,54,820]);
  assert.strictEqual(context.font, '12px system-ui, sans-serif');
  assert.strictEqual(context.textAlign, 'center');

  // Fractional DPR and a narrow chart retain matching horizontal/vertical scales.
  calls.length = 0; canvas.clientWidth = 220; canvas.clientHeight = 108;
  ctx.window.devicePixelRatio = 1.25;
  t.drawPriceChart([0.35,0.4,0.3]);
  assert.strictEqual(canvas.width, 275);
  assert.strictEqual(canvas.height, 135);
  assert.deepStrictEqual(calls.find(x => x.name === 'setTransform').args, [1.25,0,0,1.25,0,0]);
  assert.strictEqual(element('chart-foot').textContent, '30.0¢ — 40.0¢ · 3 точек');
  assert.ok(calls.some(x => x.name === 'lineTo'));

  // Old responses cannot draw a graph on an inactive snapshot.
  t.state.activeSlug = 'old';
  const stale = t.loadPriceChart('old');
  assert.strictEqual(requests.length, 1);
  t.state.activeSlug = null; t.state.activeSnapshotId = 'snap'; t.state.chartRequestId += 1;
  t.showSnapshotMarketData();
  requests[0].resolve({json:async () => ({points:[{ask:0.2},{ask:0.3}]})});
  await stale;
  assert.strictEqual(t.state.priceChartData, null);
  assert.strictEqual(canvas.hidden, true);
  assert.strictEqual(element('chart-snapshot-placeholder').hidden, false);

  // Resize reuses the current data and does not issue network requests.
  t.state.activeSlug = 'current'; t.state.activeSnapshotId = null; t.showLiveMarketData();
  const current = t.loadPriceChart('current');
  assert.strictEqual(requests.length, 2);
  requests[1].resolve({json:async () => ({points:[{ask:0.45},{ask:0.5}]})});
  await current;
  assert.strictEqual(t.state.priceChartData.slug, 'current');
  t.observePriceChartResize();
  assert.strictEqual(observer.target, canvas);
  canvas.clientWidth = 600; calls.length = 0; observer.callback();
  assert.strictEqual(canvas.width, 750);
  assert.ok(calls.some(x => x.name === 'lineTo'));
  assert.strictEqual(requests.length, 2);
  t.showSnapshotMarketData(); calls.length = 0; observer.callback();
  assert.strictEqual(calls.length, 0);
  assert.strictEqual(requests.length, 2);
  console.log('chart and snapshot tests passed');
}
run().catch(err => {console.error(err); process.exitCode = 1;});
