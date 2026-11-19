import importlib
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit


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

    def test_other_market_types_are_not_mistaken_for_winner(self):
        self.assertEqual(self.ow._group_market_kind("Map 3. Winner"), ("map_winner", 3))
        self.assertEqual(self.ow._group_market_kind("Winner"), ("winner", None))
        self.assertEqual(self.ow._group_market_kind("Map 3. Total"), ("unsupported", None))
        self.assertEqual(self.ow._group_market_kind("Map handicap"), ("unsupported", None))


if __name__ == "__main__":
    unittest.main()
