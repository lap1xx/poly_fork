"""Pure scanner payout/ROI calculations with injectable PM book operations."""
import math
import time

def resolve_usd_rub_rate(cbr_rate, configured_rate):
    """Prefer fresh CBR data; fall back only to a positive finite configured rate."""
    for candidate in (cbr_rate, configured_rate):
        try:
            rate = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and 0 < rate <= 100_000:
            return rate
    return None


def _risk_capped_plan_bank(total_bank, fraction):
    try:
        bank = float(total_bank)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(bank) or bank <= 0:
        return 0.0
    return bank * fraction


def eff_mult(odds, comm, slip):
    try:
        odds, comm, slip = float(odds), float(comm), float(slip)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(x) for x in (odds, comm, slip)):
        return 0.0
    if odds <= 1.0 or not (0.0 <= comm < 1.0) or not (0.0 <= slip < 1.0):
        return 0.0
    x = odds * (1.0 - slip)
    if x <= 1.0:
        return 0.0
    return 1.0 + (x - 1.0) * (1.0 - comm)


def compute(bets, bank):
    try:
        bank = float(bank)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bank) or bank <= 0 or not bets:
        return None
    try:
        ms = [eff_mult(b["o"], b.get("c", 0.0), b.get("s", 0.0)) for b in bets]
    except (KeyError, TypeError, AttributeError):
        return None
    if any(m <= 0 for m in ms):
        return None
    inv = sum(1.0 / m for m in ms)
    if not math.isfinite(inv) or inv <= 0:
        return None
    roi = 1.0 / inv - 1.0
    stakes = [bank * (1.0 / m) / inv for m in ms]
    payout = bank / inv
    return {"roi": roi, "stakes": stakes, "payout": payout,
            "profit": payout - bank, "bank": bank}


def alert_due(changed, last_alert_ts, now, alert_gap):
    """Allow changed signals after the minimum gap and repeat reminders on schedule."""
    gap = max(0.0, float(alert_gap))
    if changed:
        return last_alert_ts is None or gap == 0.0 or now - last_alert_ts >= gap
    return last_alert_ts is not None and gap > 0.0 and now - last_alert_ts >= gap


def refine_stakes(
    bets, result, rate, *, max_iterations, best_ask, walk_book_with_fee, compute,
):
    if not result:
        return None
    bank = sum(float(x) for x in result.get("stakes", []))
    if not math.isfinite(bank) or bank <= 0:
        return None
    for _ in range(max_iterations):
        changed = False
        for i, b in enumerate(bets):
            if b.get("source") != "Polymarket":
                continue
            stake_usdt = result["stakes"][i]
            if stake_usdt <= 0:
                continue
            ba = best_ask(b.get("token", ""))
            if ba <= 0:
                return None
            try:
                fee_rate = float(b["pm_fee_rate"])
                fee_exponent = float(b["pm_fee_exponent"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            depth_quote = walk_book_with_fee(
                b.get("token", ""), stake_usdt, fee_rate, fee_exponent
            )
            if not depth_quote:
                return None
            avg = depth_quote["avg_price"]
            shares = depth_quote["shares"]
            if b.get("pm_book_age_source") == "local_receipt":
                try:
                    times = (float(b["pm_book_received_at"]),
                             float(depth_quote["book_received_at"]))
                    now = time.time()
                    if not all(math.isfinite(ts) and 0 < ts <= now for ts in times):
                        raise ValueError("invalid receipt timestamp")
                    b["pm_book_received_at"] = min(times)
                except (KeyError, TypeError, ValueError, OverflowError):
                    # Once provenance is missing, a newer book cannot prove
                    # the age of the earlier inputs used in this calculation.
                    b["pm_book_received_at"] = None
            # Preserve the initial candidate-sizing probe fields, but publish
            # the depth price/size used by this stake-refinement iteration.
            b["pm_refined_avg_ask"] = float(avg)
            b["pm_refined_shares"] = float(shares)
            b["pm_refined_stake_usd"] = float(stake_usdt)
            # Both legs are denominated in USDT; a PM share costs its ask in
            # USDT-equivalent and pays one USDT per winning share.
            b["pm_fee_estimated_usd"] = depth_quote["fee"]
            new_o = depth_quote["effective_multiplier"]
            if abs(new_o - b["o"]) / b["o"] > 0.0005:
                b["o"] = new_o
                changed = True
        if not changed:
            break
        result = compute(bets, bank)
        if result is None:
            return None
    # The last recomputed allocation has not been repriced against the PM
    # quote it implies. Reject rather than publish mismatched quote and stakes.
    if changed:
        return None
    return result
