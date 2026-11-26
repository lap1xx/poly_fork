const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const depth = require(path.join(root, 'static', 'pm_depth_quote.js'));
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const quoted = input.map(x => depth.quote(x.asks, x.budget, x.schedule));

const nodes = {};
function element(id) {
  if (!nodes[id]) nodes[id] = {textContent:'', value:'', style:{}, dataset:{},
    attrs:{}, classList:{add(){}, remove(){}, toggle(){}},
    setAttribute(k,v){this.attrs[k]=String(v);}, removeAttribute(k){delete this.attrs[k];}};
  return nodes[id];
}
const ctx = {
  document:{getElementById:element, addEventListener(){}},
  localStorage:{getItem(){return null;}},
  window:{PolyArbPmDepthQuote:depth, PolyArbMarketDisplay:{marketTypeLabel:()=> 'MATCH', opportunityCardDisplay:()=> null}},
  console,
};
vm.createContext(ctx);
const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
vm.runInContext(app + '\nthis.testApi={state,pmBookCache,pmQuoteForEnteredAmount,bookmakerQuoteIsFresh,renderHowMuch,recalcHowMuch,loadPmBook,markFilled};', ctx, {filename:'app.js'});
ctx.syncNumberStepperState = () => {};
ctx.setLiveQuoteValue = (node, value) => { node.textContent = String(value); };
ctx.updatePlanRiskNote = () => { element('hm-risk-note').textContent = 'Риск.'; };
const t = ctx.testApi;
const now = Date.now() / 1000;
t.state.wsState = 'connected';
t.state.activeSlug = 'market';
t.state.detailEdited = true;
t.state.calcEdited = true;
element('hm-pm-stake').value = '1.3365';
element('hm-bb-stake').value = '1.50';
const a = {slug:'market', sport:'CS2', market_type:'MATCH', ts:now,
  bk_odds_age_sec:1, bets:[
    {source:'Polymarket', token:'token-a', outcome:'Alpha', o:2.5,
     pm_best_ask:0.4, pm_fee_enabled:true, pm_fee_rate:0.05, pm_fee_exponent:1,
     pm_fee_source:'clob_market_info'},
    {source:'1win', outcome:'Bravo', o:2.5, c:0, s:0, odds_age_sec:1},
  ], pm_book_snapshot:{token:'token-a', received_at:now,
      asks:[{price:0.4,size:2},{price:0.5,size:2},{price:0.6,size:10}],
      bids:[], asks_truncated:false, total_ask_levels:3}};
t.state.arbs = [a];
element('btn-buy-pm').setAttribute('href', 'https://polymarket.com/event/parent');
t.renderHowMuch(a);
assert.strictEqual(element('btn-buy-pm').attrs.href, 'https://polymarket.com/event/parent');
assert.notStrictEqual(element('hm-roi').textContent, '—');
assert.notStrictEqual(element('kpi-depth').textContent, '—');
assert.match(element('hm-shares').textContent, /3/);
assert.match(element('hm-avg').textContent, /43,?3|43\.3/);
assert.match(element('hm-fee').textContent, /0[,.]04/);
assert.match(element('hm-pm-win').textContent, /3[,.]00/);
assert.match(element('hm-payout').textContent, /3[,.]00/);
// Old snapshot-based formula would have yielded only 3.34125 shares for 1.3365 * 2.5.
assert.notStrictEqual(t.pmQuoteForEnteredAmount(a, a.bets[0], 1.3365).shares, 1.3365 * 2.5);
// The scanner's PM slippage reserve reduces estimated payout, not the share count.
a.bets[0].s = 0.1;
t.renderHowMuch(a);
assert.match(element('hm-shares').textContent, /3/);
assert.match(element('hm-pm-win').textContent, /2[,.]70/);
a.bets[0].s = 0;
// Larger manually entered amount is repriced, and shortage is not extrapolated.
element('hm-pm-stake').value = '100';
t.renderHowMuch(a);
assert.strictEqual(element('hm-payout').textContent, '—');
assert.strictEqual(element('hm-roi').textContent, '—');
assert.match(element('hm-risk-note').textContent, /Недостаточн/);
// Stale PM data cannot be used for an active plan.
element('hm-pm-stake').value = '1.3365';
a.pm_book_snapshot.received_at = now - 31;
t.renderHowMuch(a);
assert.strictEqual(element('hm-roi').textContent, '—');
assert.strictEqual(element('hm-shares').textContent, '—');
assert.strictEqual(element('kpi-depth').textContent, '—');
// A fresh REST fallback is tied to the same token, amount and fee schedule.
const full = depth.quote(a.pm_book_snapshot.asks, 1.3365,
  {enabled:true, rate:.05, exponent:1, source:'clob_market_info'});
t.pmBookCache.market = {outcome:'Alpha',token_id:'token-a', fetched_at:Date.now(),
  requested_usd:1.3365, ask_depth_usd:10,
  fee_schedule:{fees_enabled:true,rate:.05,exponent:1,source:'clob_market_info'},
  avg_for_size:{...full, fee_estimate:full.fee}};
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365).source,'rest');
t.pmBookCache.market.token_id = 'other-token';
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365), null);
// A matching token is not enough: exact outcome, amount, fee and TTL also matter.
t.pmBookCache.market.token_id = 'token-a';
t.pmBookCache.market.outcome = 'Other';
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365), null);
t.pmBookCache.market.outcome = 'Alpha';
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3366), null);
t.pmBookCache.market.fee_schedule.rate = .04;
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365), null);
t.pmBookCache.market.fee_schedule.rate = null;
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365), null);
t.pmBookCache.market.fee_schedule.rate = .05;
t.pmBookCache.market.fetched_at = Date.now() - 31000;
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365), null);
t.pmBookCache.market.fetched_at = Date.now();
assert.strictEqual(depth.quote(a.pm_book_snapshot.asks, 1.3365,
  {enabled:true,rate:null,exponent:1,source:'clob_market_info'}), null);
// A newer live book must not be overwritten by an older REST request. If its
// bounded depth is insufficient, return a partial quote rather than invent depth.
a.pm_book_snapshot.received_at = Date.now() / 1000;
a.pm_book_snapshot.asks = [{price:.4,size:1}];
a.pm_book_snapshot.asks_truncated = true;
t.pmBookCache.market.fetched_at = Date.now() - 1000;
let partial = t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365);
assert.strictEqual(partial.source, 'live');
assert.strictEqual(partial.full, false);
assert.strictEqual(partial.truncated, true);
t.pmBookCache.market.fetched_at = Date.now() + 1000;
// A timestamp in the future is not accepted as a REST quote either.
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365).full, false);
t.pmBookCache.market.fetched_at = Date.now();
// A fully filled live quote is usable even when more levels were truncated.
a.pm_book_snapshot.asks = [{price:.4,size:100}];
assert.strictEqual(t.pmQuoteForEnteredAmount(a,a.bets[0],1.3365).source, 'live');
// Connection loss suppresses the bookmaker-dependent ROI, reconnect requires
// timestamps still within their TTL.
t.state.wsState = 'disconnected';
t.renderHowMuch(a);
assert.strictEqual(element('hm-roi').textContent, '—');
assert.strictEqual(element('hm-bb-win').textContent, '—');
t.state.wsState = 'connected';
a.bets[1].odds_age_sec = 1;
a.ts = Date.now() / 1000;
a.bets[1].s = .1;
a.bets[1].c = .1;
a.bets[0].s = .1;
t.renderHowMuch(a);
assert.match(element('hm-bb-win').textContent, /3[,.]19/);
assert.notStrictEqual(element('hm-roi').textContent, '—');
a.bets[1].s = 0; a.bets[1].c = 0; a.bets[0].s = 0;
// Missing or expired bookmaker age also suppresses the combined result.
a.pm_book_snapshot.received_at = now;
a.bets[1].odds_age_sec = 31;
t.renderHowMuch(a);
assert.strictEqual(element('hm-roi').textContent, '—');
a.bets[1].odds_age_sec = null;
a.bk_odds_age_sec = null;
assert.strictEqual(t.bookmakerQuoteIsFresh(a,a.bets[1]), false);

// Manual recording starts with the user's entered amounts, not old scanner stakes.
a.stakes = [20, 30];
element('hm-pm-stake').value = '7.25';
element('hm-bb-stake').value = '11.40';
ctx.renderEditor = () => {};
t.markFilled();
assert.deepStrictEqual(Array.from(t.state.editing.stakes), [7.25, 11.4]);
assert.deepStrictEqual(a.stakes, [20, 30]);
element('hm-pm-stake').value = '1.3365';

async function checkRequestRaces() {
  // Force REST, then switch the active signal before the response arrives.
  a.pm_book_snapshot.received_at = now - 31;
  a.bets[1].odds_age_sec = 1;
  t.state.activeSlug = 'market';
  delete t.pmBookCache.market;
  let resolveResponse;
  ctx.fetch = () => new Promise(resolve => { resolveResponse = resolve; });
  const pending = t.loadPmBook('market');
  assert.ok(resolveResponse);
  t.state.activeSlug = 'another-market';
  resolveResponse({json: async () => ({ok:true, fee_schedule:{fees_enabled:true,rate:.05,exponent:1,source:'clob_market_info'},
    books:[{outcome:'Alpha',token_id:'token-a',avg_for_size:{...full,fee_estimate:full.fee}}]})});
  await pending;
  assert.strictEqual(t.pmBookCache.market, undefined);

  // Even with the same slug, a changed token/outcome must not accept old data.
  t.state.activeSlug = 'market';
  const pending2 = t.loadPmBook('market');
  assert.ok(resolveResponse);
  a.bets[0].token = 'token-b';
  a.bets[0].outcome = 'New outcome';
  resolveResponse({json: async () => ({ok:true, fee_schedule:{fees_enabled:true,rate:.05,exponent:1,source:'clob_market_info'},
    books:[{outcome:'Alpha',token_id:'token-a',avg_for_size:{...full,fee_estimate:full.fee}}]})});
  await pending2;
  assert.strictEqual(t.pmBookCache.market, undefined);

  // Returning to the same market/amount must start a new request even if the
  // old generation is still in flight; old completion must not remove the new key.
  a.bets[0].token = 'token-a';
  a.bets[0].outcome = 'Alpha';
  const resolvers = [];
  ctx.fetch = () => new Promise(resolve => resolvers.push(resolve));
  const first = t.loadPmBook('market');
  t.state.bookRequestId += 1; // selection/input invalidates the old generation
  const second = t.loadPmBook('market');
  assert.strictEqual(resolvers.length, 2);
  const response = () => ({json: async () => ({ok:true,
    fee_schedule:{fees_enabled:true,rate:.05,exponent:1,source:'clob_market_info'},
    books:[{outcome:'Alpha',token_id:'token-a',best_ask:.4,ask_depth_usd:10,asks:[],avg_for_size:{...full,fee_estimate:full.fee}}]})});
  resolvers[0](response());
  await first;
  assert.strictEqual(t.pmBookCache.market, undefined);
  assert.strictEqual(t.state.bookInFlightKeys.size, 1);
  await t.loadPmBook('market');
  assert.strictEqual(resolvers.length, 2); // duplicate current-generation request coalesced
  resolvers[1](response());
  await second;
  assert.strictEqual(t.pmBookCache.market.token_id, 'token-a');
  assert.strictEqual(t.state.bookInFlightKeys.size, 0);
}
checkRequestRaces().then(() => process.stdout.write(JSON.stringify(quoted)))
  .catch(err => { console.error(err); process.exitCode = 1; });
