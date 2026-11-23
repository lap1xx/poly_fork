import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import onewin_bridge
import polyarb_v4
from opportunity import normalize_scanner_snapshot


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "quote_quality_replay.json"


class QuoteQualityReplayTests(unittest.TestCase):
    """Deterministic replay of captured read-only quotes; no provider calls."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.capture_time = self.fixture["captured_at_epoch"]
        self.pm_books = {
            item["outcome"]: item
            for item in self.fixture["polymarket"]["books"]
        }
        self.onewin_quotes = {
            item["outcome"]: item
            for item in self.fixture["onewin"]["quotes"]
        }
        self.tokens = [item["token_id"] for item in self.pm_books.values()]
        with polyarb_v4.LOCK:
            for item in self.pm_books.values():
                polyarb_v4.BOOKS[item["token_id"]] = {
                    "bids": [],
                    "asks": [tuple(level) for level in item["asks"]],
                    "ts": item["local_received_at_epoch"],
                    "last_change": item["local_received_at_epoch"],
                    "source_ts": item["source_event_timestamp_epoch"],
                }

    def tearDown(self):
        with polyarb_v4.LOCK:
            for token in self.tokens:
                polyarb_v4.BOOKS.pop(token, None)

    def _replay_pair(self, pm_outcome, fee_rate=0.05, fee_exponent=1.0):
        pm_book = self.pm_books[pm_outcome]
        bookmaker_outcome = next(
            outcome for outcome in self.onewin_quotes if outcome != pm_outcome
        )
        bets = [
            {
                "source": "Polymarket",
                "outcome": pm_outcome,
                "token": pm_book["token_id"],
                "o": 1.0 / pm_book["asks"][0][0],
                "c": 0.0,
                "s": 0.0,
                "pm_fee_enabled": fee_rate > 0.0,
                "pm_fee_rate": fee_rate,
                "pm_fee_exponent": fee_exponent,
                "pm_fee_source": "clob_market_info",
            },
            {
                "source": "1win",
                "outcome": bookmaker_outcome,
                "o": self.onewin_quotes[bookmaker_outcome]["odds"],
                "c": 0.0,
                "s": 0.0,
            },
        ]
        bank = self.fixture["planned_stake_usdt"]
        initial = polyarb_v4.compute(bets, bank)
        with patch.object(polyarb_v4.time, "time", return_value=self.capture_time):
            refined = polyarb_v4.refine_stakes(bets, initial, 1.0)
        return bets, refined

    def _make_onewin_stream(self):
        stream = onewin_bridge.OneWinStream()
        match_id = 42554135
        match_key = onewin_bridge._key(match_id)
        slug = self.fixture["market_slug"]
        home, away = self.onewin_quotes
        stream._connected.set()
        stream.matches[match_key] = {"home": home, "away": away}
        stream.bindings[slug] = {
            "match_id": match_id,
            "kind": "winner",
            "arg": None,
            "swap": False,
            "pm_home": home,
            "pm_away": away,
        }
        odds = {}
        for index, outcome in enumerate((home, away), start=1):
            quote = self.onewin_quotes[outcome]
            odds[str(index)] = {
                "name": outcome,
                "cf": quote["odds"],
                "status": 1,
                "receivedAt": quote["local_received_at_epoch"],
            }
        stream.stakes[match_key] = {
            "winner": {"name": "Winner", "odds": odds},
        }
        return stream, slug, match_key

    def test_captured_quote_ages_use_local_receipts_and_keep_provider_time_unknown(self):
        first_quote = next(iter(self.onewin_quotes.values()))
        one_win_age = self.capture_time - first_quote["local_received_at_epoch"]
        self.assertAlmostEqual(one_win_age, 24.007, places=3)
        self.assertIsNone(self.fixture["onewin"]["provider_timestamp"])
        self.assertLessEqual(
            one_win_age, self.fixture["onewin"]["quote_ttl_seconds"]
        )

        first_book = next(iter(self.pm_books.values()))
        with patch.object(polyarb_v4.time, "time", return_value=self.capture_time):
            diagnostic = polyarb_v4.pm_book_diagnostic(first_book["token_id"])
        self.assertEqual(diagnostic["book_state"], "ask_available")
        self.assertAlmostEqual(diagnostic["book_age_sec"], 7.2, places=1)
        self.assertAlmostEqual(diagnostic["source_event_age_sec"], 7.4, places=1)

    def test_live_1win_reader_accepts_captured_quote_then_rejects_it_after_ttl(self):
        stream, slug, _match_key = self._make_onewin_stream()
        with patch.object(onewin_bridge.time, "time", return_value=self.capture_time):
            fresh = stream.live_bc(slug)
        self.assertEqual(set(fresh), set(self.onewin_quotes))
        expected_age = (
            self.capture_time
            - self.onewin_quotes["100 Thieves"]["local_received_at_epoch"]
        )
        self.assertAlmostEqual(
            fresh["100 Thieves"]["age_sec"], expected_age, places=6
        )

        latest_received = min(
            quote["local_received_at_epoch"]
            for quote in self.onewin_quotes.values()
        )
        stale_now = latest_received + self.fixture["onewin"]["quote_ttl_seconds"] + 0.001
        with patch.object(onewin_bridge.time, "time", return_value=stale_now):
            stale = stream.live_bc(slug)
        self.assertEqual(stale, {})
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(has_fresh_bookmaker=False),
            "no_fresh_bookmaker_pair",
        )

    def test_onewin_age_survives_scanner_candidate_and_opportunity_replay(self):
        stream, slug, match_key = self._make_onewin_stream()
        quote_received_at = self.capture_time - 29.94
        for odd in stream.stakes[match_key]["winner"]["odds"].values():
            odd["receivedAt"] = quote_received_at
            odd["cf"] = 10.0 if odd["name"] == "Chinggis Warriors" else 2.0

        market = {
            "slug": slug,
            "q": self.fixture["question"],
            "o": list(self.pm_books),
            "t": self.tokens,
            "condition_id": "replay-market-condition",
        }
        with patch.object(onewin_bridge, "ODDS_MAX_AGE_SECONDS", 30.0), \
             patch.object(onewin_bridge.time, "time", return_value=self.capture_time), \
             patch.object(polyarb_v4, "_maybe_refresh_stale", return_value=False), \
             patch.object(polyarb_v4, "fetch_pm_fee_schedule", return_value={
                 "fees_enabled": True, "rate": 0.05, "exponent": 1.0,
                 "source": "clob_market_info",
             }):
            live_quotes = stream.live_bc(slug)
            pairs = polyarb_v4.build_arb_candidates(
                market,
                live_quotes,
                slip=0.0,
                bank=self.fixture["planned_stake_usdt"],
                bk_source="1win",
                market_slug=slug,
            )
            bets = next(pair for pair in pairs if pair[0]["outcome"] == "100 Thieves")
            calculation = polyarb_v4.compute(
                bets, self.fixture["planned_stake_usdt"]
            )
            refined = polyarb_v4.refine_stakes(bets, calculation, 1.0)

        bookmaker_age = next(
            leg["odds_age_sec"] for leg in bets if leg["source"] == "1win"
        )
        self.assertAlmostEqual(bookmaker_age, 29.94, places=6)
        serialized_bets = polyarb_v4._serialize_candidate_bets(bets)
        candidate = {
            "slug": slug,
            "q": self.fixture["question"],
            "sport": "cs2",
            "market_type": "winner",
            "phase": "prematch",
            "currency": "USDT",
            "roi": float(refined["roi"]),
            "stakes": [float(value) for value in refined["stakes"]],
            "profit": float(refined["profit"]),
            "ts": self.capture_time,
            "bk_source": "1win",
            "bk_odds_age_sec": bookmaker_age,
            "bets": serialized_bets,
        }
        payload = {
            "type": "scanner.snapshot", "schema_version": 1,
            "scanner_id": "polyarb_v4", "session_id": "1win-candidate-replay",
            "seq": 1, "sent_at": self.capture_time,
            "arbs": {slug: candidate},
        }
        normalized = normalize_scanner_snapshot(
            payload, now_epoch=self.capture_time + 0.07
        )
        self.assertEqual(normalized["rejected"], [])
        self.assertEqual(normalized["opportunities"][0]["status"], "stale")
        self.assertAlmostEqual(
            normalized["opportunities"][0]["legs"][1]["age_ms"],
            30_010.0,
            places=3,
        )

    def test_duplicate_1win_winner_groups_remain_fail_closed(self):
        stream, slug, match_key = self._make_onewin_stream()
        group = stream.stakes[match_key]["winner"]
        stream.stakes[match_key] = {"winner-1": group, "winner-2": copy.deepcopy(group)}
        with patch.object(onewin_bridge.time, "time", return_value=self.capture_time):
            self.assertEqual(stream.live_bc(slug), {})

    def test_size_aware_replay_uses_opposite_1win_outcome_and_never_calls_it_a_signal(self):
        for pm_outcome in self.pm_books:
            with self.subTest(pm_outcome=pm_outcome):
                bets, result = self._replay_pair(pm_outcome)
                self.assertIsNotNone(result)
                self.assertEqual([bet["source"] for bet in bets], ["Polymarket", "1win"])
                self.assertNotEqual(bets[0]["outcome"], bets[1]["outcome"])
                self.assertAlmostEqual(result["bank"], self.fixture["planned_stake_usdt"])
                self.assertLess(result["roi"], 0.0)
                self.assertEqual(
                    polyarb_v4._classify_signal_drop_reason(
                        has_fresh_bookmaker=True,
                        best_roi=result["roi"],
                        min_roi=0.03,
                    ),
                    "below_roi_threshold",
                )

    def test_pm_fee_reduces_replayed_roi_and_is_not_hidden_by_best_ask(self):
        _bets_with_fee, with_fee = self._replay_pair("100 Thieves")
        _bets_without_fee, without_fee = self._replay_pair(
            "100 Thieves", fee_rate=0.0
        )
        self.assertIsNotNone(with_fee)
        self.assertIsNotNone(without_fee)
        self.assertLess(with_fee["roi"], without_fee["roi"])

    def test_captured_pm_quote_uses_fee_adjusted_multiplier_and_consistent_calculation(self):
        bets, result = self._replay_pair("100 Thieves")
        self.assertIsNotNone(result)
        pm_leg = bets[0]
        bookmaker_leg = bets[1]
        captured_ask = self.pm_books["100 Thieves"]["asks"][0][0]
        fee_per_share = polyarb_v4.pm_fee_per_share(captured_ask, 0.05, 1.0)
        expected_pm_multiplier = 1.0 / (captured_ask + fee_per_share)
        expected_inverse_sum = (
            1.0 / expected_pm_multiplier + 1.0 / bookmaker_leg["o"]
        )
        expected_roi = 1.0 / expected_inverse_sum - 1.0
        expected_payout = self.fixture["planned_stake_usdt"] / expected_inverse_sum

        self.assertAlmostEqual(pm_leg["pm_refined_avg_ask"], captured_ask, places=8)
        self.assertAlmostEqual(pm_leg["o"], expected_pm_multiplier, places=8)
        self.assertAlmostEqual(result["roi"], expected_roi, places=10)
        self.assertAlmostEqual(result["payout"], expected_payout, places=8)
        self.assertAlmostEqual(result["profit"], expected_payout - result["bank"], places=8)
        self.assertAlmostEqual(result["roi"], result["profit"] / result["bank"], places=10)
        self.assertAlmostEqual(sum(result["stakes"]), self.fixture["planned_stake_usdt"], places=8)
        self.assertAlmostEqual(pm_leg["pm_refined_stake_usd"], result["stakes"][0], places=2)

    def test_insufficient_total_ask_depth_rejects_refinement_and_is_diagnosable(self):
        pm_book = self.pm_books["100 Thieves"]
        token = pm_book["token_id"]
        with polyarb_v4.LOCK:
            polyarb_v4.BOOKS[token]["asks"] = [(pm_book["asks"][0][0], 0.001)]
        bets = [
            {
                "source": "Polymarket", "outcome": "100 Thieves", "token": token,
                "o": 1.0 / pm_book["asks"][0][0], "c": 0.0, "s": 0.0,
            },
            {
                "source": "1win", "outcome": "Chinggis Warriors",
                "o": self.onewin_quotes["Chinggis Warriors"]["odds"],
                "c": 0.0, "s": 0.0,
            },
        ]
        initial = polyarb_v4.compute(bets, self.fixture["planned_stake_usdt"])
        with patch.object(polyarb_v4.time, "time", return_value=self.capture_time):
            refined = polyarb_v4.refine_stakes(bets, initial, 1.0)
        self.assertIsNone(refined)
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=True,
                candidate_diagnostics={"pm_depth_short": 1},
            ),
            "pm_depth_short",
        )

    def test_stale_pm_book_is_reported_as_a_distinct_signal_removal_reason(self):
        pm_book = self.pm_books["100 Thieves"]
        stale_now = (
            pm_book["local_received_at_epoch"]
            + self.fixture["polymarket"]["book_ttl_seconds"]
            + 0.001
        )
        diagnostic = polyarb_v4.pm_book_diagnostic(pm_book["token_id"], stale_now)
        self.assertEqual(diagnostic["book_state"], "stale_book")
        self.assertEqual(
            polyarb_v4._classify_signal_drop_reason(
                has_fresh_bookmaker=True,
                pm_diagnostic=diagnostic,
            ),
            "pm_book_stale",
        )


if __name__ == "__main__":
    unittest.main()
