(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.PolyBankrollPlanner = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function fitSuggestedStakes(bookmakerRub, polymarketRub, rubPerUsd, capRub) {
    const rate = Number(rubPerUsd);
    if (!Number.isFinite(rate) || rate <= 0) {
      return { bookmakerRub: 0, polymarketUsd: 0, exposureRub: 0, capped: false };
    }

    let bookmaker = Number(bookmakerRub);
    let polymarket = Number(polymarketRub);
    bookmaker = Number.isFinite(bookmaker) ? Math.max(0, bookmaker) : 0;
    polymarket = Number.isFinite(polymarket) ? Math.max(0, polymarket) : 0;

    const hasCap = capRub !== null && capRub !== undefined
      && Number.isFinite(Number(capRub)) && Number(capRub) >= 0;
    const cap = hasCap ? Number(capRub) : null;
    const proposedExposure = bookmaker + polymarket;
    let capped = false;

    if (hasCap && proposedExposure > cap && proposedExposure > 0) {
      const scale = cap / proposedExposure;
      bookmaker *= scale;
      polymarket *= scale;
      capped = true;
    }

    // With a cap, round both displayed legs down. Nearest rounding can exceed
    // the cap even when the unrounded total is just below it.
    let bookmakerOut = hasCap ? Math.floor(bookmaker + 1e-9) : Math.round(bookmaker);
    let polymarketUsd = hasCap
      ? Math.floor((polymarket / rate) * 100 + 1e-9) / 100
      : Number((polymarket / rate).toFixed(2));

    if (hasCap) {
      // Floating-point guard: preserve the invariant on the actual displayed values.
      for (let i = 0; i < 1000; i += 1) {
        const displayedExposure = bookmakerOut + polymarketUsd * rate;
        if (displayedExposure <= cap + 1e-8) break;
        if (polymarketUsd >= 0.01) {
          polymarketUsd = Number((polymarketUsd - 0.01).toFixed(2));
        } else if (bookmakerOut >= 1) {
          bookmakerOut -= 1;
        } else {
          break;
        }
      }
    }

    return {
      bookmakerRub: bookmakerOut,
      polymarketUsd,
      exposureRub: bookmakerOut + polymarketUsd * rate,
      capped,
    };
  }

  return { fitSuggestedStakes };
});
