'use strict';
const assert = require('node:assert/strict');
const { polymarketEventUrl } = require('../static/provider_links.js');
const base = 'http://localhost:8080';
assert.equal(
  polymarketEventUrl('https://polymarket.com/event/example-event', base),
  'https://polymarket.com/event/example-event',
);
assert.equal(
  polymarketEventUrl('https://polymarket.com/event/example-event?foo=bar', base),
  'https://polymarket.com/event/example-event?foo=bar',
);
for (const url of [
  'http://polymarket.com/event/example',
  'https://www.polymarket.com/event/example',
  'https://polymarket.com.evil.test/event/example',
  'https://polymarket.com:8443/event/example',
  'https://user:pass@polymarket.com/event/example',
  'https://polymarket.com/market/example',
  'https://polymarket.com/event/',
  '/event/example',
  'not a URL',
]) {
  assert.equal(polymarketEventUrl(url, base), null, `expected URL to be rejected: ${url}`);
}
console.log('Provider event URL safety tests: OK');
