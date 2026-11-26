const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const providerLinks = require(path.join(root, 'static', 'provider_links.js'));

const nodes = {};
function element(id) {
  if (!nodes[id]) nodes[id] = {
    attrs: {}, textContent: '', title: '',
    setAttribute(key, value) { this.attrs[key] = String(value); },
    removeAttribute(key) { delete this.attrs[key]; },
  };
  return nodes[id];
}
const requests = [];
const ctx = {
  document: {getElementById: element, addEventListener() {}},
  localStorage: {getItem() { return null; }},
  window: {
    location: {origin: 'http://localhost:8080'},
    POLYARB_ONEWIN_ORIGIN: 'https://f1w-7707.com',
    PolyArbProviderLinks: providerLinks,
    PolyArbMarketDisplay: {opportunityCardDisplay: opportunity => opportunity?.valid ? {} : null},
  },
  URL, AbortController, setTimeout, clearTimeout, console,
  fetch(url) { return new Promise((resolve, reject) => requests.push({url, resolve, reject})); },
};
vm.createContext(ctx);
const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
vm.runInContext(app + '\nthis.testApi={state,pmEventLinkCache,updateEventActionLinks,getActive};', ctx);
const t = ctx.testApi;
const pm = element('btn-buy-pm');
const bk = element('btn-open-bb');
const status = element('event-link-status');
const turn = () => new Promise(resolve => setImmediate(resolve));
const response = url => ({ok:true, json:async () => ({url})});
function row(slug, pmUrl, bkUrl = 'https://f1w-7707.com/betting/match/1') {
  return {slug, sport:'CS2', bets:[
    {source:'Polymarket', event_url:pmUrl}, {source:'1win', event_url:bkUrl},
  ]};
}
function selectLive(a) {
  t.state.arbs = [a]; t.state.activeSlug = a.slug; t.state.activeSnapshotId = null;
  t.updateEventActionLinks(a);
}
function assertEnabled(link, href) {
  assert.strictEqual(link.attrs.href, href);
  assert.strictEqual(link.attrs['aria-disabled'], 'false');
  assert.strictEqual(link.attrs.tabindex, undefined);
}
function assertDisabled(link) {
  assert.strictEqual(link.attrs.href, undefined);
  assert.strictEqual(link.attrs['aria-disabled'], 'true');
  assert.strictEqual(link.attrs.tabindex, '-1');
}

async function run() {
  const direct = row('market-game2', 'https://polymarket.com/event/market', 'https://f1w-7707.com/betting/match/1');
  selectLive(direct);
  assertEnabled(pm, 'https://polymarket.com/event/market');
  assertEnabled(bk, 'https://f1w-7707.com/betting/match/1');
  assert.strictEqual(requests.length, 0);

  // Valid v1 legs take priority, while an unvalidated v1 row falls back to legacy.
  t.state.opportunitiesBySlug.set(direct.slug, {valid:true, legs:[
    {source:'Polymarket', event_url:'https://polymarket.com/event/v1-parent'},
    {source:'1win', event_url:'https://f1w-7707.com/betting/match/v1'},
  ]});
  t.updateEventActionLinks(direct);
  assertEnabled(pm, 'https://polymarket.com/event/v1-parent');
  assertEnabled(bk, 'https://f1w-7707.com/betting/match/v1');
  t.state.opportunitiesBySlug.get(direct.slug).valid = false;
  t.updateEventActionLinks(direct);
  assertEnabled(pm, 'https://polymarket.com/event/market');

  for (const invalid of [
    'http://f1w-7707.com/betting/match/1',
    'https://f1w-7707.com.evil.test/betting/match/1',
    'https://user@f1w-7707.com/betting/match/1',
    'javascript:alert(1)',
  ]) {
    selectLive(row('market-game2', 'https://polymarket.com/event/market', invalid));
    assertDisabled(bk);
  }
  for (const invalid of [
    'http://polymarket.com/event/market', 'https://evil.test/event/market',
    'https://user@polymarket.com/event/market', 'https://polymarket.com/other/market',
    'javascript:alert(1)',
  ]) {
    selectLive(row('market-game2', invalid));
    assertDisabled(pm);
    assert.match(status.textContent, /небезопасна/);
  }
  const bb = {slug:'bb', sport:'CS2', bb_match_id:'123', bb_tournament_id:'456',
    bets:[{source:'Polymarket',event_url:'https://polymarket.com/event/parent'}, {source:'BetBoom'}]};
  selectLive(bb);
  assertEnabled(bk, 'https://betboom.ru/esport/cs2/456/123');
  bb.bb_match_id = '../bad'; t.updateEventActionLinks(bb); assertDisabled(bk);

  // Legacy child URL gets resolved once, without navigating or polling per update.
  const child = row('child-game2', 'https://polymarket.com/event/child-game2');
  selectLive(child); assertDisabled(pm); assert.match(status.textContent, /Определяем/);
  for (let i=0; i<40; i++) t.updateEventActionLinks(child);
  await turn();
  assert.strictEqual(requests.length, 1);
  assert.strictEqual(requests[0].url, '/api/pm_url/child-game2');
  const other = row('other-game2', 'https://polymarket.com/event/other');
  selectLive(other);
  requests[0].resolve(response('https://polymarket.com/event/child'));
  await turn();
  assertEnabled(pm, 'https://polymarket.com/event/other'); // stale response did not replace selection
  selectLive(child);
  assertEnabled(pm, 'https://polymarket.com/event/child');
  assert.strictEqual(requests.length, 1); // cached

  // Same slug but different pinned snapshot: use the current snapshot's URL,
  // never the now-completed live object's URL or its v1 legs.
  const live = row('shared-game1', 'https://polymarket.com/event/shared-game1');
  selectLive(live); await turn();
  assert.strictEqual(requests.length, 2);
  const pinned = {...row('shared-game1', 'https://polymarket.com/event/pinned-parent',
    'https://f1w-7707.com/betting/match/pinned'), snapshot_id:'pin-1', expires_at:Date.now()/1000+300};
  t.state.pinnedSignalSnapshots = [pinned]; t.state.activeSlug = null; t.state.activeSnapshotId = 'pin-1';
  t.updateEventActionLinks(t.getActive());
  assertEnabled(pm, 'https://polymarket.com/event/pinned-parent');
  assertEnabled(bk, 'https://f1w-7707.com/betting/match/pinned');
  requests[1].resolve(response('https://polymarket.com/event/live-parent')); await turn();
  assertEnabled(pm, 'https://polymarket.com/event/pinned-parent');
  assertEnabled(bk, 'https://f1w-7707.com/betting/match/pinned');

  // Pinned snapshots without an event URL use the same safe resolver.
  const pinLegacy = {...row('pin-game1', null), snapshot_id:'pin-2', expires_at:Date.now()/1000+300};
  t.state.pinnedSignalSnapshots = [pinLegacy]; t.state.activeSnapshotId = 'pin-2';
  t.updateEventActionLinks(t.getActive()); await turn();
  assert.strictEqual(requests.length, 3); assertDisabled(pm);
  requests[2].resolve(response('https://polymarket.com/event/pin-parent')); await turn();
  assertEnabled(pm, 'https://polymarket.com/event/pin-parent');

  // Invalid resolver response and failures disable the link and respect backoff.
  const fail = row('failed-game1', null);
  selectLive(fail); await turn();
  requests[3].resolve(response('https://evil.test/event/failed')); await turn();
  assertDisabled(pm); assert.match(status.textContent, /повторим/i);
  for (let i=0; i<40; i++) t.updateEventActionLinks(fail);
  await turn(); assert.strictEqual(requests.length, 4);
  t.pmEventLinkCache.get('failed-game1').expiresAt = 0;
  t.updateEventActionLinks(fail); await turn(); assert.strictEqual(requests.length, 5);
  requests[4].reject(new Error('offline')); await turn();
  assertDisabled(pm); assert.strictEqual(t.pmEventLinkCache.get('failed-game1').status, 'failed');
  console.log('event link tests passed');
}
run().catch(err => { console.error(err); process.exitCode = 1; });
