import importlib
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from opportunity import normalize_scanner_snapshot


class OneWinFeedTests(unittest.TestCase):
    def setUp(self):
        self.ow = importlib.import_module("onewin_bridge")
        self.stream = self.ow.OneWinStream()
        self.match = {
            "id": 12345,
            "sportId": 142,
            "homeTeam": {"id": 1, "name": "Team Alpha"},
            "awayTeam": {"id": 2, "name": "Team Bravo"},
        }
        self.stream._store_matches([self.match])
        self.slug = "cs2-team-alpha-team-bravo-map3-winner-2026-11-19"

    def test_ready_requires_connected_odds_socket(self):
        self.assertFalse(self.stream.is_ready())
        self.stream._connected.set()
        self.assertTrue(self.stream.is_ready())

    def test_initial_subscription_skips_unbound_catalog_events(self):
        self.stream._set_initial_ids()
        self.assertEqual(self.stream._desired_ids, {})

    def test_ws_url_uses_match_odds_path_and_runtime_options(self):
        url = self.ow.build_ws_url(
            "https://api-gateway.top-parser.com", "ru-RU", "public-partner-id"
        )
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "wss")
        self.assertEqual(parsed.path, "/push-server-v2/")
        self.assertEqual(parse_qs(parsed.query), {
            "Language": ["ru-RU"],
            "externalPartnerId": ["public-partner-id"],
            "EIO": ["4"],
            "transport": ["websocket"],
        })

    def test_binding_contains_only_normal_1win_event_page_url(self):
        match = {
            "id": 43445591,
            "slug": "downpour-vs-vawpirchik",
            "sportId": 142,
            "sport": {"id": 142, "isEsport": True, "sportType": "esport"},
            "homeTeam": {"name": "downpour", "slug": "downpour"},
            "awayTeam": {"name": "Vawpirchik", "slug": "vawpirchik"},
        }
        self.stream._store_matches([match])
        binding = self.stream.bind(
            "cs2-downpour-vawpirchik-winner", ["downpour", "Vawpirchik"]
        )
        self.assertEqual(
            binding["event_url"],
            "https://f1w-7707.com/betting/match/esport/downpour-vs-vawpirchik-43445591",
        )
        self.assertNotIn("stake", urlsplit(binding["event_url"]).query.lower())

    def test_subscription_packet_is_read_only_odds_subscription(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
            def send(self, message):
                self.sent.append(message)

        ws = FakeSocket()
        self.stream._ws = ws
        self.stream._connected.set()
        self.stream._subscribe_ids(ws, [12345])
        decoded = [json.loads(message[2:]) for message in ws.sent]
        self.assertEqual(len(decoded), 3)  # info and odds for base/all groups
        odds = [packet[1] for packet in decoded
                if packet[1]["messageType"] == "subscribe-match-odds"]
        self.assertEqual({x["data"]["isBaseOddsGroups"] for x in odds}, {True, False})
        self.assertTrue(all(x["data"]["matchIds"] == [12345] for x in odds))
        self.assertFalse(any("make-bet" in message or "betslip" in message
                             for message in ws.sent))

    def test_pm_market_kind_fails_closed_for_sets_props_and_unknown_slugs(self):
        cases = {
            "cs2-team-alpha-team-bravo-2026-11-19": ("winner", None, None),
            "cs2-team-alpha-team-bravo-2026-11-19-game1": ("map_winner", 1, None),
            "cs2-team-alpha-team-bravo-map3-winner-2026-11-19": ("map_winner", 3, None),
            "atp-player-a-player-b-2026-11-19-set-2-winner": (
                "unsupported", None, "set_period_not_supported"
            ),
            "dota2-a-b-2026-11-19-game3-any-player-ultra-kill": (
                "unsupported", None, "special_or_player_prop"
            ),
            "cs2-team-alpha-team-bravo-map3-total": ("unsupported", None, "total"),
            "cs2-team-alpha-team-bravo-2026-11-19-player-most-kills": (
                "unsupported", None, "special_or_player_prop"
            ),
            "cs2-team-alpha-team-bravo-2026-11-19-unrecognized-market": (
                "unsupported", None, "unknown_market_signature"
            ),
        }
        for slug, expected in cases.items():
            with self.subTest(slug=slug):
                self.assertEqual(self.ow.pm_market_kind(slug), expected)

    def test_snapshot_maps_open_map_winner_odds_to_pm_teams(self):
        binding = self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.assertIsNotNone(binding)
        self.assertEqual(binding["kind"], "map_winner")
        self.stream._connected.set()
        self.stream._handle_socket_message("42" + json.dumps([
            "u", {
                "messageType": "match-odds-snapshot",
                "data": {
                    "matchId": 12345,
                    "isBaseOddsGroups": True,
                    "oddsGroups": [{
                        "id": "map3-winner",
                        "name": "Map 3. Winner",
                        "isBase": True,
                        "oddsList": [
                            {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                            {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                        ],
                    }],
                },
            },
        ]))
        result = self.stream.live_bc(self.slug)
        self.assertEqual(result["Team Alpha"]["o"], 1.91)
        self.assertEqual(result["Team Bravo"]["o"], 2.07)
        self.assertLessEqual(result["Team Alpha"]["age_sec"], self.ow.ODDS_MAX_AGE_SECONDS)
        self.assertLessEqual(result["Team Bravo"]["age_sec"], self.ow.ODDS_MAX_AGE_SECONDS)

    def test_unrounded_onewin_age_crosses_ttl_in_opportunity_replay(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        quote_received_at = 2_000_000_000.0
        base = {
            "matchId": 12345,
            "isBaseOddsGroups": True,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [
                    {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                    {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                ],
            }],
        }
        with patch.object(self.ow.time, "time", return_value=quote_received_at):
            self.stream._on_match_odds(base, snapshot=True)

        sampled_at = quote_received_at + 29.94
        with patch.object(self.ow.time, "time", return_value=sampled_at), \
             patch.object(self.ow, "ODDS_MAX_AGE_SECONDS", 30.0):
            live = self.stream.live_bc(self.slug)
        precise_age = live["Team Bravo"]["age_sec"]
        self.assertAlmostEqual(precise_age, 29.94, places=6)

        candidate = {
            "slug": self.slug,
            "q": "Team Alpha vs Team Bravo",
            "sport": "cs2",
            "market_type": "winner",
            "phase": "prematch",
            "currency": "USDT",
            "roi": 0.04,
            "stakes": [20.0, 20.0],
            "profit": 1.6,
            "ts": sampled_at,
            "bk_source": "1win",
            "bk_odds_age_sec": precise_age,
            "bets": [
                {
                    "source": "Polymarket", "outcome": "Team Alpha",
                    "token": "pm-team-alpha", "o": 1 / (0.4 * 1.04),
                    "pm_best_ask": 0.4, "pm_avg_ask": 0.4,
                    "pm_book_age_sec": 1.0,
                },
                {
                    "source": "1win", "outcome": "Team Bravo",
                    "o": live["Team Bravo"]["o"],
                    "odds_age_sec": precise_age,
                },
            ],
        }
        envelope = {
            "type": "scanner.snapshot", "schema_version": 1,
            "scanner_id": "polyarb_v4", "session_id": "onewin-ttl-replay",
            "seq": 1, "sent_at": sampled_at, "arbs": {self.slug: candidate},
        }
        result = normalize_scanner_snapshot(
            envelope, now_epoch=sampled_at + 0.07
        )
        self.assertEqual(result["rejected"], [])
        opportunity = result["opportunities"][0]
        self.assertEqual(opportunity["status"], "stale")
        self.assertAlmostEqual(opportunity["legs"][1]["age_ms"], 30_010.0, places=3)

    def test_delta_updates_price_and_blocked_or_deleted_odds_are_not_used(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        base = {
            "matchId": 12345,
            "isBaseOddsGroups": True,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [
                    {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                    {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                ],
            }],
        }
        self.stream._on_match_odds(base, snapshot=True)
        delta = {
            "matchId": 12345,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [{"id": 11, "cf": 1.75, "status": 1}],
            }],
        }
        self.stream._on_match_odds(delta)
        self.assertEqual(self.stream.live_bc(self.slug)["Team Alpha"]["o"], 1.75)
        blocked = {
            "matchId": 12345,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [{"id": 11, "status": 0}],
            }],
        }
        self.stream._on_match_odds(blocked)
        self.assertEqual(self.stream.live_bc(self.slug), {})

    def test_status_only_delta_does_not_rejuvenate_a_stale_price(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        base = {
            "matchId": 12345,
            "isBaseOddsGroups": True,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [
                    {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                    {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                ],
            }],
        }
        self.stream._on_match_odds(base, snapshot=True)
        stale_at = time.time() - self.ow.ODDS_MAX_AGE_SECONDS - 1
        self.stream.stakes["12345"]["map3-winner"]["odds"]["11"]["receivedAt"] = stale_at
        self.stream._on_match_odds({
            "matchId": 12345,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner",
                "oddsList": [{"id": 11, "status": 1}],
            }],
        })
        odd = self.stream.stakes["12345"]["map3-winner"]["odds"]["11"]
        self.assertEqual(odd["receivedAt"], stale_at)
        self.assertEqual(odd["cf"], 1.91)
        self.assertEqual(self.stream.live_bc(self.slug), {})
        health = self.stream.snapshot_health()
        self.assertEqual(health["odds_snapshots_received"], 1)
        self.assertEqual(health["odds_deltas_received"], 1)
        self.assertEqual(health["valid_price_updates_received"], 2)
        self.assertEqual(health["fresh_open_quotes"], 1)
        self.assertEqual(health["stale_open_quotes"], 1)

    def test_valid_price_message_uses_local_receipt_time_not_provider_timestamp(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        base = {
            "matchId": 12345,
            "isBaseOddsGroups": True,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [
                    {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                    {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                ],
            }],
        }
        repeated_price = {
            "matchId": 12345,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner",
                "oddsList": [{
                    "id": 11, "name": "Team Alpha", "cf": 1.91,
                    "status": 1, "timestamp": 1,
                }],
            }],
        }
        with patch.object(self.ow.time, "time", side_effect=[1000.0, 1010.0, 1012.0]):
            self.stream._on_match_odds(base, snapshot=True)
            self.stream._on_match_odds(repeated_price)
            quote = self.stream.live_bc(self.slug)["Team Alpha"]

        self.assertEqual(quote["o"], 1.91)
        self.assertAlmostEqual(quote["age_sec"], 2.0)

    def test_ambiguous_duplicate_market_groups_fail_closed(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        received_at = time.time()
        group = {
            "name": "Map 3. Winner", "isBase": True,
            "odds": {
                "1": {"name": "Team Alpha", "cf": 1.9, "status": 1, "receivedAt": received_at},
                "2": {"name": "Team Bravo", "cf": 2.1, "status": 1, "receivedAt": received_at},
            },
        }
        self.stream.stakes["12345"] = {"g1": dict(group), "g2": dict(group)}
        self.assertEqual(self.stream.live_bc(self.slug), {})

    def test_stale_open_odds_are_rejected(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        base = {
            "matchId": 12345,
            "isBaseOddsGroups": True,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [
                    {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                    {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                ],
            }],
        }
        self.stream._on_match_odds(base, snapshot=True)
        with self.stream.lock:
            for odd in self.stream.stakes["12345"]["map3-winner"]["odds"].values():
                odd["receivedAt"] = time.time() - self.ow.ODDS_MAX_AGE_SECONDS - 1
        self.assertEqual(self.stream.live_bc(self.slug), {})

    def test_binding_rejects_ambiguous_duplicate_events(self):
        duplicate = dict(self.match)
        duplicate["id"] = 12346
        self.stream._store_matches([duplicate])
        self.assertIsNone(self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"]))
        diagnostic = self.stream.snapshot_match_diagnostics()[self.slug]
        self.assertEqual(diagnostic["reason"], "ambiguous_event")
        self.assertEqual(len(diagnostic["top_candidates"]), 2)

    def test_name_normalization_handles_punctuation_and_cyrillic_transliteration(self):
        self.assertEqual(self.ow._name_score("Виртус.pro", "Virtus Pro"), 1.0)
        self.assertEqual(self.ow._name_score("NAVI Esports", "Navi"), 1.0)

    def test_team_name_subset_is_not_accepted_as_a_match(self):
        self.assertEqual(
            self.ow._name_score("Team Spirit", "Team Spirit Academy"), 0.0
        )

    def test_curated_alias_group_matches_known_name_variant(self):
        previous = self.ow._TEAM_ALIAS_INDEX
        try:
            self.ow._TEAM_ALIAS_INDEX = self.ow._build_alias_index(
                [["Natus Vincere", "NaVi"]]
            )
            self.assertEqual(self.ow._name_score("Natus Vincere", "NaVi"), 1.0)
        finally:
            self.ow._TEAM_ALIAS_INDEX = previous

    def test_live_diagnostic_full_name_aliases_are_loaded(self):
        pairs = [
            ("NIP", "Ninjas in Pyjamas"),
            ("Carlos Alcaraz", "Карлос Алькарас"),
            ("Flavio Cobolli", "Флавио Коболли"),
            ("Daniil Medvedev", "Даниил Медведев"),
            ("Frances Tiafoe", "Френсис Тиафо"),
            ("Anastasia Zolotareva", "Анастасия Золотарева"),
            ("Irene Burillo Escorihuela", "Ирен Бурильо-Эскориуэла"),
            ("Valeriya Strakhova", "Страхова, Валерия"),
            ("Veronika Erjavec", "Вероника Эрьявец"),
        ]
        previous = self.ow._TEAM_ALIAS_INDEX
        try:
            self.ow._TEAM_ALIAS_INDEX = self.ow._load_alias_index()
            for left, right in pairs:
                with self.subTest(left=left, right=right):
                    self.assertEqual(self.ow._name_score(left, right), 1.0)
        finally:
            self.ow._TEAM_ALIAS_INDEX = previous

    def test_live_nip_alias_binds_big_match_in_reverse_order(self):
        previous = self.ow._TEAM_ALIAS_INDEX
        try:
            self.ow._TEAM_ALIAS_INDEX = self.ow._load_alias_index()
            self.stream._store_matches([{
                "id": 43474970,
                "sportId": 142,
                "homeTeam": {"name": "Ninjas in Pyjamas"},
                "awayTeam": {"name": "BIG"},
                "startAt": "2026-11-19T10:00:00Z",
            }])
            slug = "cs2-big5-nip-2026-11-19"
            binding = self.stream.bind(
                slug,
                ["BIG", "NIP"],
                {"start": "2026-11-19T10:00:00Z"},
            )
            self.assertIsNotNone(binding)
            self.assertEqual(binding["match_id"], 43474970)
            self.assertEqual(binding["score"], 1.0)
            self.assertTrue(binding["swap"])
            self.assertEqual(
                self.stream.snapshot_match_diagnostics()[slug]["reason"],
                "matched",
            )
        finally:
            self.ow._TEAM_ALIAS_INDEX = previous

    def test_alias_shared_by_two_groups_is_disabled(self):
        index = self.ow._build_alias_index([
            ["Alpha Pro", "Alpha"],
            ["Alpha Academy", "Alpha"],
        ])
        self.assertIsNone(index["alpha"])

    def test_time_context_resolves_duplicate_event_only_when_clear(self):
        first = dict(self.match, startAt="2026-11-19T10:00:00Z")
        duplicate = dict(self.match, id=12346, startAt="2026-11-20T10:00:00Z")
        self.stream._store_matches([first, duplicate])
        binding = self.stream.bind(
            self.slug,
            ["Team Alpha", "Team Bravo"],
            {"start": "2026-11-19T10:30:00Z"},
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["match_id"], 12345)
        self.assertEqual(binding["start_delta_hours"], 0.5)
        diagnostic = self.stream.snapshot_match_diagnostics()[self.slug]
        self.assertEqual(diagnostic["reason"], "matched_time_context")

    def test_unique_near_start_time_corroborates_tennis_name_match(self):
        first = {
            "id": 33301, "sportId": 33,
            "homeTeam": {"name": "Бранко Джурич"},
            "awayTeam": {"name": "Марко Топо"},
            "startAt": "2026-11-19T10:00:00Z",
        }
        distant_duplicate = dict(first, id=33302, startAt="2026-11-20T10:00:00Z")
        self.stream._store_matches([first, distant_duplicate])
        slug = "atp-djuric1-topo1-2026-11-19"
        binding = self.stream.bind(
            slug,
            ["Branko Djuric", "Marko Topo"],
            {"start": "2026-11-19T10:30:00Z"},
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding["match_id"], 33301)
        self.assertGreaterEqual(binding["score"], self.ow.TIME_CORROBORATED_MIN_SCORE)
        self.assertLess(binding["score"], 0.65)
        self.assertEqual(
            self.stream.snapshot_match_diagnostics()[slug]["reason"],
            "matched_time_corroborated",
        )

    def test_time_corroboration_rejects_multiple_close_tennis_events(self):
        first = {
            "id": 33401, "sportId": 33,
            "homeTeam": {"name": "Бранко Джурич"},
            "awayTeam": {"name": "Марко Топо"},
            "startAt": "2026-11-19T10:00:00Z",
        }
        second = dict(first, id=33402, startAt="2026-11-19T10:40:00Z")
        self.stream._store_matches([first, second])
        slug = "atp-djuric1-topo1-2026-11-19"
        self.assertIsNone(self.stream.bind(
            slug,
            ["Branko Djuric", "Marko Topo"],
            {"start": "2026-11-19T10:30:00Z"},
        ))
        self.assertEqual(
            self.stream.snapshot_match_diagnostics()[slug]["reason"],
            "ambiguous_time_corroboration",
        )

    def test_time_corroboration_does_not_lower_esports_threshold(self):
        match = {
            "id": 33501, "sportId": 142,
            "homeTeam": {"name": "Бранко Джурич"},
            "awayTeam": {"name": "Марко Топо"},
            "startAt": "2026-11-19T10:00:00Z",
        }
        self.stream._store_matches([match])
        slug = "cs2-djuric-topo-2026-11-19"
        self.assertIsNone(self.stream.bind(
            slug,
            ["Branko Djuric", "Marko Topo"],
            {"start": "2026-11-19T10:30:00Z"},
        ))
        self.assertEqual(
            self.stream.snapshot_match_diagnostics()[slug]["reason"],
            "below_threshold",
        )

    def test_below_threshold_diagnostic_contains_top_candidate(self):
        self.assertIsNone(self.stream.bind(self.slug, ["Completely Different", "Unknown Club"]))
        diagnostic = self.stream.snapshot_match_diagnostics()[self.slug]
        self.assertEqual(diagnostic["reason"], "below_threshold")
        self.assertEqual(diagnostic["top_candidates"][0]["match_id"], 12345)
        self.assertLess(diagnostic["top_candidates"][0]["score"], diagnostic["threshold"])

    def test_catalog_refresh_prunes_removed_matches_per_successful_sport(self):
        other_sport = dict(self.match, id=22345, sportId=47)
        replacement = dict(self.match, id=12346)
        self.stream._store_matches([self.match, other_sport])
        self.stream._store_matches([replacement], replace_sport_ids={142})
        self.assertNotIn("12345", self.stream.matches)
        self.assertIn("12346", self.stream.matches)
        self.assertIn("22345", self.stream.matches)

    def test_catalog_refresh_preserves_odds_for_retained_match(self):
        self.stream.bind(self.slug, ["Team Alpha", "Team Bravo"])
        self.stream._connected.set()
        self.stream._on_match_odds({
            "matchId": 12345,
            "isBaseOddsGroups": True,
            "oddsGroups": [{
                "id": "map3-winner", "name": "Map 3. Winner", "isBase": True,
                "oddsList": [
                    {"id": 11, "name": "Team Alpha", "cf": 1.91, "status": 1},
                    {"id": 12, "name": "Team Bravo", "cf": 2.07, "status": 1},
                ],
            }],
        }, snapshot=True)
        refreshed = dict(self.match, name="Updated catalog metadata")
        self.stream._store_matches([refreshed], replace_sport_ids={142})
        result = self.stream.live_bc(self.slug)
        self.assertEqual(result["Team Alpha"]["o"], 1.91)
        self.assertEqual(result["Team Bravo"]["o"], 2.07)

    def test_other_market_types_are_not_mistaken_for_winner(self):
        self.assertEqual(self.ow._group_market_kind("Map 3. Winner"), ("map_winner", 3))
        self.assertEqual(self.ow._group_market_kind("Winner"), ("winner", None))
        self.assertEqual(self.ow._group_market_kind("Map 3. Total"), ("unsupported", None))
        self.assertEqual(self.ow._group_market_kind("Map handicap"), ("unsupported", None))


if __name__ == "__main__":
    unittest.main()
