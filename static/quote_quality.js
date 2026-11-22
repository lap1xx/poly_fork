(function (root) {
  'use strict';

  function formatAgeAtCalculation(ageSeconds, label) {
    const name = String(label || 'Котировка').trim() || 'Котировка';
    if (ageSeconds === null || ageSeconds === undefined || ageSeconds === '') {
      return `${name}: возраст неизвестен`;
    }
    const age = Number(ageSeconds);
    if (!Number.isFinite(age) || age < 0) {
      return `${name}: возраст неизвестен`;
    }
    return `${name}: ${age.toFixed(1)} с на момент расчёта`;
  }

  const api = { formatAgeAtCalculation };
  root.PolyArbQuoteQuality = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
