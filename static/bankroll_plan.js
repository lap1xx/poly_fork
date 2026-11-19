(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.PolyBankrollPlanner = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function fitSuggestedStakes(bookmakerUsdt, polymarketUsdt, capUsdt) {
    let bookmaker = Number(bookmakerUsdt);
    let polymarket = Number(polymarketUsdt);
    bookmaker = Number.isFinite(bookmaker) ? Math.max(0, bookmaker) : 0;
    polymarket = Number.isFinite(polymarket) ? Math.max(0, polymarket) : 0;

    const hasCap = capUsdt !== null && capUsdt !== undefined
      && Number.isFinite(Number(capUsdt)) && Number(capUsdt) >= 0;
    const cap = hasCap ? Number(capUsdt) : null;
    const proposedExposure = bookmaker + polymarket;
    let capped = false;

    if (hasCap && proposedExposure > cap && proposedExposure > 0) {
      const scale = cap / proposedExposure;
      bookmaker *= scale;
      polymarket *= scale;
      capped = true;
    }

    let bookmakerOut = hasCap
      ? Math.floor(bookmaker * 100 + 1e-8) / 100
      : Number(bookmaker.toFixed(2));
    let polymarketOut = hasCap
      ? Math.floor(polymarket * 100 + 1e-8) / 100
      : Number(polymarket.toFixed(2));

    if (hasCap) {
      for (let i = 0; i < 1000; i += 1) {
        if (bookmakerOut + polymarketOut <= cap + 1e-8) break;
        if (polymarketOut >= 0.01) polymarketOut = Number((polymarketOut - 0.01).toFixed(2));
        else if (bookmakerOut >= 0.01) bookmakerOut = Number((bookmakerOut - 0.01).toFixed(2));
        else break;
      }
    }

    return {
      bookmakerUsdt: bookmakerOut,
      polymarketUsdt: polymarketOut,
      exposureUsdt: Number((bookmakerOut + polymarketOut).toFixed(2)),
      capped,
    };
  }

  return { fitSuggestedStakes };
});
