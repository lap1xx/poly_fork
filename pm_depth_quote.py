"""Read-only estimates for a cash-sized PM buy and a manual bookmaker hedge.

These functions operate on a supplied book snapshot. Callers must separately
verify the token, quote freshness, fee metadata and market/side matching.
A result is an estimate, not an execution guarantee.
"""
import math

from arb_calculations import eff_mult
from pm_fees import fee_per_share


def quote_pm_asks(asks, cash_budget, rate, exponent):
    """Walk a complete, ascending ask snapshot using a fee-inclusive budget.

    Return a partial quote with full=False when displayed depth is insufficient.
    Malformed or unsorted levels and unavailable fee data fail closed. The
    caller must not interpret a truncated snapshot as the complete market.
    """
    try:
        cash_budget, rate, exponent = float(cash_budget), float(rate), float(exponent)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not all(math.isfinite(x) for x in (cash_budget, rate, exponent))
            or cash_budget <= 0.0 or not 0.0 <= rate <= 1.0
            or not 0.0 < exponent <= 10.0 or not isinstance(asks, (list, tuple))):
        return None

    levels = []
    previous_price = 0.0
    available_cash = 0.0
    for row in asks:
        try:
            price, size = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError, KeyError, OverflowError):
            return None
        if (not math.isfinite(price) or not math.isfinite(size)
                or not 0.0 < price < 1.0 or size <= 0.0 or price < previous_price):
            return None
        fee = fee_per_share(price, rate, exponent)
        if fee is None:
            return None
        unit_cost = price + fee
        available_cash += unit_cost * size
        if not math.isfinite(available_cash):
            return None
        levels.append((price, size, fee, unit_cost))
        previous_price = price

    remaining = cash_budget
    gross_cost = fee_total = shares = 0.0
    tolerance = cash_budget * 1e-12
    for price, available, fee, unit_cost in levels:
        take = min(available, remaining / unit_cost)
        if take <= 0.0:
            continue
        shares += take
        gross_cost += take * price
        fee_total += take * fee
        remaining -= take * unit_cost
        if remaining <= tolerance:
            break
    if shares <= 0.0:
        return None
    total_cost = gross_cost + fee_total
    if not all(math.isfinite(x) for x in (shares, gross_cost, fee_total, total_cost)) or total_cost <= 0:
        return None
    odds_eff = shares / total_cost
    if not math.isfinite(odds_eff):
        return None
    return {
        "avg_price": gross_cost / shares,
        "shares": shares,
        "fee": fee_total,
        "total_cost": total_cost,
        "odds_eff": odds_eff,
        "full": remaining <= tolerance,
        "unfilled_usd": max(0.0, remaining),
        "available_cash_usd": available_cash,
    }


def estimate_manual_position(asks, pm_stake, bookmaker_stake, bookmaker_odds,
                             fee_rate, fee_exponent, bookmaker_commission=0.0,
                             bookmaker_slippage=0.0, pm_slippage=0.0):
    """Estimate both payouts for exact entered stakes, rejecting partial depth.

    Currency is USDT-equivalent and one winning PM share pays one unit. This
    deliberately does not verify book age, binding, fees metadata or execution.
    """
    try:
        pm_stake = float(pm_stake)
        bookmaker_stake = float(bookmaker_stake)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(pm_stake) or not math.isfinite(bookmaker_stake)
            or pm_stake <= 0.0 or bookmaker_stake <= 0.0):
        return None
    try:
        multiplier = eff_mult(bookmaker_odds, bookmaker_commission, bookmaker_slippage)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(multiplier) or multiplier <= 0.0:
        return None
    quote = quote_pm_asks(asks, pm_stake, fee_rate, fee_exponent)
    if not quote or not quote["full"]:
        return None
    try:
        pm_multiplier = eff_mult(quote["odds_eff"], 0.0, pm_slippage)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(pm_multiplier) or pm_multiplier <= 0.0:
        return None
    bank = pm_stake + bookmaker_stake
    pm_payout = pm_stake * pm_multiplier
    bookmaker_payout = bookmaker_stake * multiplier
    pm_profit = pm_payout - bank
    bookmaker_profit = bookmaker_payout - bank
    values = (bank, pm_payout, bookmaker_payout, pm_profit, bookmaker_profit)
    if not all(math.isfinite(x) for x in values):
        return None
    return {
        "pm_quote": quote,
        "bank": bank,
        "pm_payout": pm_payout,
        "bookmaker_payout": bookmaker_payout,
        "pm_profit": pm_profit,
        "bookmaker_profit": bookmaker_profit,
        "minimum_profit": min(pm_profit, bookmaker_profit),
        "minimum_roi": min(pm_profit, bookmaker_profit) / bank,
    }
