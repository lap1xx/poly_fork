(function (root) {
  function polymarketEventUrl(value, baseUrl) {
    try {
      const url = new URL(String(value || ''), baseUrl);
      if (url.protocol !== 'https:'
          || url.origin !== 'https://polymarket.com'
          || url.username || url.password
          || !/^\/event\/[^/]+(?:\/|$)/.test(url.pathname)) return null;
      return url.href;
    } catch (_error) {
      return null;
    }
  }

  const api = Object.freeze({ polymarketEventUrl });
  if (root) root.PolyArbProviderLinks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
