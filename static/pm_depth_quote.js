/* Read-only PM depth estimate. The caller must check freshness and token identity. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.PolyArbPmDepthQuote = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  function quote(asks, cashBudget, schedule) {
    const budget = Number(cashBudget);
    const rate = Number(schedule?.rate), exponent = Number(schedule?.exponent);
    if (!Array.isArray(asks) || !Number.isFinite(budget) || budget <= 0 ||
        !schedule || typeof schedule.enabled !== 'boolean' || schedule.source !== 'clob_market_info' ||
        schedule.rate == null || schedule.exponent == null || typeof schedule.rate === 'boolean' ||
        typeof schedule.exponent === 'boolean' || String(schedule.rate).trim() === '' ||
        String(schedule.exponent).trim() === '' ||
        !Number.isFinite(rate) || rate < 0 || rate > 1 || (!schedule.enabled && rate !== 0) ||
        !Number.isFinite(exponent) || exponent <= 0 || exponent > 10) return null;
    let previous = 0, availableCash = 0;
    const levels = [];
    for (const row of asks) {
      const price = Number(Array.isArray(row) ? row[0] : row?.price);
      const size = Number(Array.isArray(row) ? row[1] : row?.size);
      if (!Number.isFinite(price) || !Number.isFinite(size) ||
          price <= 0 || price >= 1 || size <= 0 || price < previous) return null;
      const fee = rate * Math.pow(price * (1 - price), exponent);
      const unitCost = price + fee;
      availableCash += unitCost * size;
      if (!Number.isFinite(availableCash)) return null;
      levels.push({price, size, fee, unitCost});
      previous = price;
    }
    let remaining = budget, gross = 0, fees = 0, shares = 0;
    const tolerance = budget * 1e-12;
    for (const level of levels) {
      const take = Math.min(level.size, remaining / level.unitCost);
      if (take <= 0) continue;
      shares += take;
      gross += take * level.price;
      fees += take * level.fee;
      remaining -= take * level.unitCost;
      if (remaining <= tolerance) break;
    }
    const total = gross + fees;
    if (!(shares > 0) || !(total > 0) || ![shares, gross, fees, total].every(Number.isFinite)) return null;
    const odds = shares / total;
    if (!Number.isFinite(odds)) return null;
    return {
      avg_price: gross / shares, shares, fee: fees, total_cost: total,
      odds_eff: odds, full: remaining <= tolerance,
      unfilled_usd: Math.max(0, remaining), available_cash_usd: availableCash,
    };
  }
  function position(pmQuote, pmStake, bookmakerStake, bookmakerOdds, commission = 0, slippage = 0, pmSlippage = 0) {
    const pm = Number(pmStake), bk = Number(bookmakerStake), odds = Number(bookmakerOdds);
    const fee = Number(commission), slip = Number(slippage), pmSlip = Number(pmSlippage);
    const shares = Number(pmQuote?.shares), cost = Number(pmQuote?.total_cost);
    if (!pmQuote || pmQuote.full !== true || ![pm, bk, odds, fee, slip, pmSlip, shares, cost].every(Number.isFinite)
        || pm <= 0 || bk <= 0 || odds <= 1 || fee < 0 || fee >= 1 || slip < 0 || slip >= 1
        || pmSlip < 0 || pmSlip >= 1 || shares / cost * (1 - pmSlip) <= 1
        || shares <= 0 || cost <= 0 || Math.abs(cost - pm) > Math.max(0.000002, pm * 1e-10)) return null;
    const slipped = odds * (1 - slip);
    if (slipped <= 1) return null;
    const effectiveOdds = 1 + (slipped - 1) * (1 - fee);
    const bank = pm + bk, pmPayout = shares * (1 - pmSlip), bkPayout = bk * effectiveOdds;
    const minProfit = Math.min(pmPayout, bkPayout) - bank;
    if (![bank, pmPayout, bkPayout, minProfit].every(Number.isFinite)) return null;
    return {bank, pm_payout: pmPayout, bookmaker_payout: bkPayout,
      minimum_profit: minProfit, minimum_roi: minProfit / bank};
  }
  return {quote, position};
});
