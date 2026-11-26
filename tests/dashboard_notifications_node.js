const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const nodes = {};
const stored = {};
function element(id) {
  if (!nodes[id]) nodes[id] = {textContent:'', value:'', style:{}, dataset:{},
    attrs:{}, classList:{add(){},remove(){},toggle(){}},
    setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(){}};
  return nodes[id];
}
const notifications = [];
class FakeNotification {
  static permission = 'default';
  static async requestPermission() { this.permission = 'granted'; return this.permission; }
  constructor(title, options) { this.title=title; this.options=options; notifications.push(this); }
  close() { this.closed=true; }
}
class FakeWebSocket {
  static CONNECTING=0; static OPEN=1; static CLOSING=2; static CLOSED=3;
  constructor(url) { this.url=url; this.readyState=1; }
  close() { this.readyState=3; }
}
let focused = false, selected = null;
const ctx = {
  document:{hidden:true,getElementById:element,addEventListener(){}},
  localStorage:{getItem:k=>stored[k] ?? null,setItem:(k,v)=>{stored[k]=v;}},
  window:{Notification:FakeNotification, focus:()=>{focused=true;}},
  location:{protocol:'http:',host:'localhost:8080'},
  WebSocket:FakeWebSocket, performance:{now:()=>1},
  setTimeout:()=>1, clearTimeout(){}, console,
};
vm.createContext(ctx);
const app = fs.readFileSync(path.join(root,'static','app.js'),'utf8');
vm.runInContext(app+'\nthis.testApi={state,connectWS,toggleDesktopNotifications,notifyNewSignals,pmBookAge,formatPmDropQuote};',ctx,{filename:'app.js'});
const t = ctx.testApi;
const marketDisplay = require(path.join(root, 'static', 'market_display.js'));
ctx.syncRuntimeControlsFromStatus = () => {};
ctx.applyFilters = () => { t.state.visible = t.state.arbs.filter(a => a.sport === 'CS2'); };
ctx.renderScanStatus = () => {};
ctx.selectArb = slug => {selected=slug;};
t.state.soundEnabled = false;
function arb(slug, sport='CS2') {
  return {slug,sport,roi:.06,ts:Date.now()/1000,pm_age:1,
    bets:[{source:'Polymarket',token:'pm-'+slug,outcome:'A',pm_age:1},
          {source:'1win',outcome:'B',odds_age_sec:1}]};
}
function status() { return {state:'running',ts:Date.now()/1000}; }
function snapshot(rows, s=status()) { return {type:'snapshot',arbs:rows,scan_status:s}; }
async function run() {
  // Disabled by default and permission is requested only from an explicit toggle.
  assert.strictEqual(t.state.desktopNotificationsEnabled, false);
  assert.strictEqual(FakeNotification.permission, 'default');
  await t.toggleDesktopNotifications();
  assert.strictEqual(FakeNotification.permission, 'granted');
  assert.strictEqual(stored.poly_arb_desktop_notifications, 'on');
  assert.strictEqual(element('btn-desktop-notify').attrs['aria-pressed'], 'true');
  t.connectWS();
  const ws=t.state.ws;
  ws.onopen();
  const a=arb('first');
  ws.onmessage({data:JSON.stringify(snapshot([a]))});
  assert.strictEqual(notifications.length,0,'initial snapshot must not notify');
  const b=arb('second');
  ws.onmessage({data:JSON.stringify(snapshot([a,b]))});
  assert.strictEqual(notifications.length,1,'new eligible signal notifies once');
  assert.match(notifications[0].title, /Новая вилка/);
  assert.match(notifications[0].options.body, /Проверьте/);
  ws.onmessage({data:JSON.stringify(snapshot([a,b]))});
  assert.strictEqual(notifications.length,1,'duplicate snapshot must not notify');
  notifications[0].onclick();
  assert.strictEqual(focused,true);
  assert.strictEqual(selected,'second');
  assert.strictEqual(notifications[0].closed,true);
  const hiddenByFilter=arb('filtered','DOTA2');
  ws.onmessage({data:JSON.stringify(snapshot([a,b,hiddenByFilter]))});
  assert.strictEqual(notifications.length,1,'filtered signal must not notify');
  const stale=arb('stale'); stale.bets[1].odds_age_sec=31;
  ws.onmessage({data:JSON.stringify(snapshot([a,b,stale]))});
  assert.strictEqual(notifications.length,1,'stale bookmaker must not notify');
  const stalePm=arb('stale-pm'); stalePm.bets[0].pm_age=31;
  ws.onmessage({data:JSON.stringify(snapshot([a,b,stalePm]))});
  assert.strictEqual(notifications.length,1,'stale PM must not notify');
  const staleStatus=arb('stale-status');
  ws.onmessage({data:JSON.stringify(snapshot([a,b,staleStatus],{state:'running',ts:Date.now()/1000-11}))});
  assert.strictEqual(notifications.length,1,'stale scanner status must not notify');
  const unchanged=arb('unchanged-ask');
  Object.assign(unchanged.bets[0], {pm_age:58,pm_book_age_sec:1,pm_book_age_source:'local_receipt'});
  ws.onmessage({data:JSON.stringify(snapshot([a,b,unchanged]))});
  assert.strictEqual(notifications.length,2,'fresh receipt with unchanged ask should notify');
  assert.strictEqual(t.pmBookAge(unchanged.bets[0], unchanged),1);
  assert.strictEqual(marketDisplay.legDisplay(unchanged.bets[0],10).quoteAgeSec,1);
  const unknown=arb('unknown-receipt');
  Object.assign(unknown.bets[0], {pm_age:1,pm_book_age_sec:null,pm_book_age_source:'local_receipt'});
  ws.onmessage({data:JSON.stringify(snapshot([a,b,unknown]))});
  assert.strictEqual(notifications.length,2,'unknown receipt must not fall back to recent price change');
  assert.strictEqual(t.pmBookAge(unknown.bets[0], unknown),null);
  assert.strictEqual(marketDisplay.legDisplay(unknown.bets[0],10).quoteAgeSec,null);
  const staleReceipt=arb('stale-receipt');
  Object.assign(staleReceipt.bets[0], {pm_age:1,pm_book_age_sec:31,pm_book_age_source:'local_receipt'});
  ws.onmessage({data:JSON.stringify(snapshot([a,b,staleReceipt]))});
  assert.strictEqual(notifications.length,2,'stale receipt must not be revived by recent price change');
  assert.strictEqual(t.pmBookAge({pm_age:12}, {pm_age:15}),12,'unmarked legacy payload remains compatible');
  assert.match(t.formatPmDropQuote({best_ask:.85,book_age_sec:58}),/без изменения лучшего ask 58\.0с/);
  assert.match(t.formatPmDropQuote({best_ask:.85,book_age_sec:1,book_age_source:'local_receipt'}),/стакан 1\.0с/);
  assert.match(t.formatPmDropQuote({best_ask:.85,book_age_sec:null,book_age_source:'local_receipt'}),/возраст стакана неизвестен/);
  ctx.document.hidden=false;
  const visibleTab=arb('visible-tab');
  ws.onmessage({data:JSON.stringify(snapshot([a,b,visibleTab]))});
  assert.strictEqual(notifications.length,2,'visible tab must not produce a desktop notification');
  await t.toggleDesktopNotifications();
  assert.strictEqual(stored.poly_arb_desktop_notifications,'off');
  assert.strictEqual(element('btn-desktop-notify').attrs['aria-pressed'],'false');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
