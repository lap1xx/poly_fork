# Legacy field semantics and migration notes

**Scope:** the supported read-only scanner path `polyarb_v4.py` → scanner snapshot → canonical `web_app.py` → dashboard. This is a field contract, not an authorization to place bets. `server/monitor.py` and saved trade-history records are separate legacy contexts and must not be assumed to share the scanner snapshot meaning of identically named fields.

## Financial fields

| Legacy field | Current meaning | Canonical mapping / migration rule |
|---|---|---|
| Config `bank` / scanner input `bank` | The account-wide configured bankroll in USDT. The scanner applies `_risk_capped_plan_bank()` before candidate calculation. | Do not map to `calculation.total_stake`; keep bankroll/limit separate and explicitly named. |
| Snapshot `total_bank` | Account-wide configured bankroll passed to the scanner, in USDT. | Not the opportunity's invested amount. Preserve only as account/config context if a future contract needs it. |
| Snapshot `bank` | For the main production scanner, `sum(stakes)` for this opportunity, in USDT. This differs from config `bank`; a separate older/demo producer can use the name differently. | `calculation.total_stake`; validate against the sum of normalized leg stakes within a documented tolerance. |
| `bets` + `stakes` | Parallel positional arrays: `stakes[i]` is the planned monetary allocation for `bets[i]`. Current production currency is USDT. | A leg's `stake` belongs inside that leg object. Never assume index 0 is a particular source; match by leg identity during transitional conversion. |
| `bets[].s` | Slippage parameter consumed by `eff_mult`; it is not a stake. | Do not map to `legs[].stake`. |
| `bets[].c` | Commission/commission-like fraction consumed by `eff_mult`; not the stake or price. | Preserve as a calculation input only if model-specific metadata is explicitly added. |
| `bets[].o` | Type-dependent effective decimal multiplier. For a bookmaker leg it is bookmaker decimal odds. For Polymarket it is a fee-adjusted odds-like multiplier, not a probability, raw ask, or bookmaker quote. `compute()` applies `eff_mult(o,c,s)` and computes a proportional allocation, payout, profit, and ROI. | Do not map directly to canonical `legs[].price`. For PM, use the typed quote price and retain fee/model assumptions separately. Do not apply the PM fee twice. |
| Snapshot `payout` | Modelled gross common payout returned by the scanner's allocation calculation, in the same currency as the opportunity stake. | It is not net profit and not an executed/settled return. Not currently a required Opportunity v1 field. |
| Snapshot `profit` | `payout - bank` after the scanner's calculation/refinement; projected net profit for the modelled pair, in the same currency as `bank`. | `calculation.projected_profit`; this is a pre-execution estimate, not realized P&L or a guarantee of fill. Saved-trade history also uses `profit` for a user-recorded calculation, so that context must remain distinct. |
| Snapshot `roi` | Dimensionless fraction from the scanner: `1 / sum(1 / effective_multiplier_i) - 1`; for a consistent calculation this equals `profit / bank`. `0.04` means 4%. | `calculation.roi_fraction`; format as a percent only at presentation time. Add an explicit consistency tolerance before rejecting records where `roi` differs from `profit / total_stake`. Historical aggregate ROI is separately recomputed as `sum(profit) / sum(bank)`. |

## Polymarket quote fields

| Legacy field | Current meaning | Migration rule |
|---|---|---|
| `pm_best_ask` / `pm_best_bid` | Top level ask/bid for one PM token, in probability/USDT-per-share units on `[0,1]`; not cents. | Keep top-of-book separate from a depth-aware price. UI may render cents by multiplying by 100. |
| `pm_avg_ask` | `walk_book()` depth-walk average price for the sizing probe `pm_target_shares`, in the same units as best ask. It is not a simple mean of levels. | Do not silently replace with best ask. Record the size basis when the value is migrated. |
| `pm_target_usd` | Probe amount: default 50 USDT; with a positive scanner plan bank, half that amount clamped to 20–200 USDT. It is used to form the initial book-depth probe. | Informational sizing metadata, not necessarily the final allocated PM leg stake. |
| `pm_target_shares` | Initial probe shares, calculated as `pm_target_usd / pm_best_ask`. It is an estimate of share quantity, not USD, an order, a fill, or a guarantee of executable size. It excludes the fee model. | Keep distinct from the final leg stake and from executed shares. Consumers must recompute against a current book and chosen amount. |
| `pm_age` / `pm_book_age_sec` | Seconds since the local PM order book's last change (with timestamp fallback), not an absolute timestamp and not the age of the whole opportunity. The producer currently serializes both names for the PM leg/root compatibility path. | Normalize to one age duration only with a defined source/precedence; do not treat it as provider event time. |

### Known PM price/calculation mismatch to resolve before deeper UI migration

`build_arb_candidates()` first measures `pm_avg_ask` at the sizing probe. Later `refine_stakes()` derives new PM effective odds from a depth walk sized from the calculated PM allocation, but currently updates `bets[].o` and recomputes the allocation without writing that refined average price back to `pm_avg_ask` or changing the original target-share fields. Therefore the serialized PM `price` (which the normalizer currently takes from `pm_avg_ask`, falling back to `pm_best_ask`) may describe the initial probe while ROI/profit use the refined effective multiplier. It can be harmless where both walks see identical prices, but it is not a guaranteed identity. The next calculation migration step must add a multi-level-book regression that makes the probe size differ from the allocated stake, then decide whether to publish a separately named `refined_avg_ask`/size basis or update the legacy average field and explicitly preserve its old probe value.

## Event links

- `bk_event_url` is a legacy snapshot-level link for the bookmaker leg. For 1win it comes from that source binding's native `event_url`; for BetBoom the UI primarily constructs the destination from the BetBoom match and tournament IDs.
- `pm_event_url` is a Polymarket market-event URL built from the market slug. `/api/pm_url/{slug}` is a fallback builder for the same destination.
- These links are not interchangeable. The canonical link belongs to a specific leg (`legs[].event_url`), not to the market as an untyped global link. A missing provider URL should remain absent rather than borrow the other leg's URL. Any action must select the leg by `source` and validate HTTPS plus that provider's exact allowlisted origin before opening it.
- Current dashboard checks 1win against its configured origin; both direct and `/api/pm_url` Polymarket destinations now pass through the same exact `https://polymarket.com` origin/path validation before opening. The helper rejects credentials, non-HTTPS URLs, subdomains, suffix lookalikes, non-default ports, and paths outside `/event/`; regression cases run in Node/CI. BetBoom's ID-built route is a separate source-specific rule.

## Safe migration order

1. Keep `web_app.py` canonical and keep the current dual-read card boundary: details, calculator, filters, links/actions, pinned snapshots, and history remain legacy-backed.
2. Schema descriptions and a replay assertion now pin stake sum/order, ROI units, and PM top ask vs depth average; add an explicit multi-level PM fee/effective-multiplier replay before changing any calculation source.
3. Resolve the multi-level PM refinement mismatch and define a quote-size basis for each published average price.
4. Keep source-bound event URL mapping as a prerequisite for moving actions to v1; the current PM direct/fallback allowlist validation is covered by `tests/test_provider_links.cjs`.
5. Only then consider moving calculations or actions to Opportunity v1; maintain read-only behavior throughout.
