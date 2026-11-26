import contextlib
import io
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bk_router
import onewin_bridge as ow
import polyarb_v4


FIXTURE = Path(__file__).parent / 'fixtures' / 'onewin_group_names_20261126.json'


def quote(label, oid, **overrides):
    value = {'id': str(oid), 'name': label, 'cf': 1.9, 'status': 1}
    value.update(overrides)
    return value


class MarketSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stream = ow.OneWinStream()
        self.stream._store_matches([{'id': 123, 'sportId': 142,
            'homeTeam': {'name': 'Team Alpha'}, 'awayTeam': {'name': 'Team Bravo'}}])
        self.slug = 'cs2-alpha-bravo-2026-11-26-game3'
        self.stream.bind(self.slug, ['Team Alpha', 'Team Bravo'])
        self.stream._connected.set()
        self.names = json.loads(FIXTURE.read_text(encoding='utf-8'))['examples']

    def group(self, name, gid='1', odds=None, **fields):
        group = {'id': gid, 'name': name, 'isBase': False,
                 'oddsList': odds if odds is not None else [
                     quote('Team Alpha', 'a', cf=1.91), quote('Team Bravo', 'b', cf=2.07)]}
        group.update(fields)
        return group

    def snapshot(self, groups):
        with patch.object(ow.time, 'time', return_value=1000.0):
            self.stream._on_match_odds({'matchId': 123, 'isBaseOddsGroups': False,
                                        'oddsGroups': groups}, snapshot=True)

    def inspect(self, now=1001.0):
        with patch.object(ow.time, 'time', return_value=now), \
             patch.object(ow, 'ODDS_MAX_AGE_SECONDS', 30.0):
            pair, diagnostic = self.stream.inspect_live_bc(self.slug)
            self.assertEqual(pair, self.stream.live_bc(self.slug))
        return pair, diagnostic

    def test_live_fixture_only_canonical_map_is_selected(self):
        names = self.names[0]['groups']
        groups = [self.group(name, str(i), isBase=(i == 1)) for i, name in enumerate(names)]
        self.snapshot(groups)
        pair, diag = self.inspect()
        self.assertEqual(diag['matching_groups'], 1)
        self.assertEqual(diag['reason'], 'fresh')
        self.assertEqual(pair['Team Alpha']['o'], 1.91)
        self.assertEqual(pair['Team Bravo']['o'], 2.07)

    def test_fixture_prematch_special_markets_are_not_selected(self):
        names = self.names[1]['groups']
        self.slug = 'cs2-alpha-bravo-2026-11-26-game1'
        self.stream.bind(self.slug, ['Team Alpha', 'Team Bravo'])
        self.snapshot([self.group(name, str(i)) for i, name in enumerate(names)])
        self.assertEqual(self.inspect()[1]['reason'], 'fresh')
        self.snapshot([self.group(name, str(i)) for i, name in enumerate(names)
                       if 'без ОТ' in name or 'Досрочный' in name])
        pair, diag = self.inspect()
        self.assertFalse(pair)
        self.assertEqual(diag['reason'], 'missing_market_group')

    def test_stale_or_closed_standard_is_not_replaced_by_special(self):
        standard = self.group('Карта 3. Победитель', '1')
        special = self.group('Карта 3. Победитель (без ОТ)', '2')
        self.snapshot([standard])
        with patch.object(ow.time, 'time', return_value=1040.0):
            self.stream._on_match_odds({'matchId': 123, 'oddsGroups': [special]})
        self.assertEqual(self.inspect(1041.0)[1]['reason'], 'no_fresh_sides')
        standard['oddsList'] = [quote('Team Alpha', 'a', status=0), quote('Team Bravo', 'b', status=0)]
        self.snapshot([standard, special])
        self.assertEqual(self.inspect()[1]['reason'], 'no_fresh_sides')

    def test_nonbase_plain_market_and_empty_period_are_allowed(self):
        self.snapshot([self.group(self.names[2]['groups'][3], period=None, periodName=' ')])
        pair, diag = self.inspect()
        self.assertTrue(pair)
        self.assertEqual(diag['reason'], 'fresh')
        self.assertFalse(self.stream.stakes['123']['1']['isBase'])

    def test_unknown_period_metadata_is_rejected_and_survives_delta(self):
        for field, value in [('period', 3), ('periodName', 'Round 5'), ('period', {})]:
            with self.subTest(field=field, value=value):
                self.snapshot([self.group('Карта 3. Победитель', **{field: value})])
                self.stream._on_match_odds({'matchId': 123, 'oddsGroups': [{'id': '1'}]})
                self.assertEqual(self.inspect()[1]['reason'], 'missing_market_group')

    def test_multiple_canonical_groups_fail_with_bounded_details(self):
        self.snapshot([self.group('Карта 3. Победитель', str(i)) for i in range(7)])
        pair, diag = self.inspect()
        self.assertFalse(pair)
        self.assertEqual(diag['reason'], 'ambiguous_market_groups')
        self.assertEqual(diag['matching_groups'], 7)
        self.assertEqual(len(diag['group_examples']), 5)
        self.assertTrue(diag['group_examples_truncated'])
        self.assertEqual(diag['group_examples'][0]['outcome_count'], 2)
        self.assertIn('id', diag['group_examples'][0])
        self.assertIn('period', diag['group_examples'][0])

    def test_unidentified_or_duplicate_group_id_is_ambiguous(self):
        no_id = self.group('Карта 3. Победитель')
        no_id.pop('id')
        cases = [[no_id, dict(no_id)],
                 [self.group('Карта 3. Победитель', 'same'),
                  self.group('Карта 3. Победитель', 'same')]]
        for groups in cases:
            with self.subTest(groups=groups):
                self.snapshot(groups)
                pair, diag = self.inspect()
                self.assertFalse(pair)
                self.assertEqual(diag['reason'], 'ambiguous_market_groups')
                self.assertTrue(diag['group_examples'][0]['ambiguous_identity'])
                self.snapshot([self.group('Карта 3. Победитель', 'unique')])
                self.assertEqual(self.inspect()[1]['reason'], 'fresh')

    def test_extra_outcomes_are_rejected_regardless_of_age_or_status(self):
        extras = [quote('Ничья', 'c'), quote('Draw', 'c', status=0),
                  quote('unknown', 'c'), quote('', 'c'), quote('Team Alpha', 'c', status=0),
                  {'id': 'c', 'name': 'Draw', 'cf': 3.0}]
        for extra in extras:
            with self.subTest(extra=extra):
                base = self.group('Карта 3. Победитель')
                base['oddsList'].append(extra)
                self.snapshot([base])
                pair, diag = self.inspect()
                self.assertFalse(pair)
                self.assertEqual(diag['reason'], 'unsupported_outcomes')
        # A stale third quote is rejected even when the two team quotes were refreshed.
        self.snapshot([self.group('Карта 3. Победитель', odds=[quote('Team Alpha', 'a'),
                         quote('Team Bravo', 'b'), quote('Draw', 'c')])])
        with patch.object(ow.time, 'time', return_value=1040.0):
            self.stream._on_match_odds({'matchId': 123, 'oddsGroups': [{'id': '1',
                'oddsList': [quote('Team Alpha', 'a'), quote('Team Bravo', 'b')]}]})
        self.assertEqual(self.inspect(1041.0)[1]['reason'], 'unsupported_outcomes')

    def test_missing_unknown_label_among_two_outcomes_fails_closed(self):
        for bad in ['Draw', 'Ничья', '', 'unknown']:
            with self.subTest(label=bad):
                self.snapshot([self.group('Карта 3. Победитель', odds=[quote('Team Alpha', 'a'), quote(bad, 'b')])])
                pair, diag = self.inspect()
                self.assertFalse(pair)
                self.assertEqual(diag['reason'], 'unsupported_outcomes')

    def test_unkeyed_or_duplicate_outcome_cannot_disappear_from_snapshot(self):
        for invalid in [{'name': 'Draw', 'cf': 3.0, 'status': 1}, None, quote('Draw', 'a')]:
            with self.subTest(invalid=invalid):
                group = self.group('Карта 3. Победитель')
                group['oddsList'].append(invalid)
                self.snapshot([group])
                self.assertEqual(self.inspect()[1]['reason'], 'unsupported_outcomes')
                self.stream._on_match_odds({'matchId': 123, 'oddsGroups': [{'id': '1', 'oddsList': []}]})
                self.assertEqual(self.inspect()[1]['reason'], 'unsupported_outcomes')
                self.snapshot([self.group('Карта 3. Победитель')])
                self.assertEqual(self.inspect()[1]['reason'], 'fresh')

    def test_exact_names_and_special_variants(self):
        for name, expected in [('Winner', ('winner', None)), (' Победитель ', ('winner', None)),
                               ('Map 3. Winner', ('map_winner', 3)), ('Карта 3. Исход', ('map_winner', 3))]:
            self.assertEqual(ow._group_market_kind(name), expected)
        for name in ['Winner (early payout)', 'Map 3. Winner (no overtime)',
                     'Карта 3. Победитель (без ОТ)', 'Карта 3. Раунд 5 победитель',
                     'Карта 3. Половина 2. Победитель (без ОТ)', 'Map 0. Winner']:
            self.assertEqual(ow._group_market_kind(name), ('unsupported', None))


class RouterExamplesTests(unittest.TestCase):
    def test_examples_cover_distinct_reasons_prioritizing_ambiguity(self):
        reasons = ['missing_market_group'] * 8 + ['no_fresh_sides', 'unsupported_outcomes',
                    'ambiguous_market_groups', 'one_side_not_fresh']
        slugs = ['s' + str(i) for i in range(len(reasons))]
        mapping = dict(zip(slugs, reasons))
        stream = SimpleNamespace(lock=threading.RLock(), matches={},
            inspect_live_bc=lambda slug: ({}, {'slug': slug, 'reason': mapping[slug]}))
        adapter = SimpleNamespace(_GLOBAL=stream)
        with patch.object(bk_router, '_ow', adapter), patch.object(bk_router, '_load_onewin', return_value=adapter):
            router = bk_router.BKRouter(use_bb=False, use_onewin=True)
            router._onewin_stream = stream
            router.bindings = {slug: ('1win', {}) for slug in slugs}
            diagnostic = router.stats(slugs)['onewin_pair_diagnostics']
        self.assertEqual(len(diagnostic['examples']), 5)
        self.assertEqual(diagnostic['examples'][0]['reason'], 'ambiguous_market_groups')
        self.assertEqual(len({entry['reason'] for entry in diagnostic['examples']}), 5)
        self.assertEqual(diagnostic['reason_counts']['missing_market_group'], 8)


class ConsoleAgeTests(unittest.TestCase):
    def test_age_is_local_at_selection_never_fixed_profile_interval(self):
        m = {'slug': 'cs2-alpha-bravo-2026-11-26', 'q': 'Alpha vs Bravo'}
        r = {'stakes': [10.0, 2.0], 'roi': .03, 'payout': 12.36}
        for age, expected in [(4.7, 'возраст при отборе 4.7с'), (None, 'возраст при отборе неизвестен'),
                              (float('nan'), 'возраст при отборе неизвестен'), (-1, 'возраст при отборе неизвестен'),
                              (True, 'возраст при отборе неизвестен')]:
            with self.subTest(age=age):
                bets = [{'source': 'Polymarket', 'outcome': 'Alpha', 'o': 1.3},
                        {'source': '1win', 'outcome': 'Bravo', 'o': 5.0, 'odds_age_sec': age}]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    polyarb_v4.print_arb(m, bets, r, 1, 0, {'1win': {'update_seconds': 2}})
                text = output.getvalue()
                self.assertIn(expected, text)
                self.assertNotIn('(2s)', text)
                self.assertEqual(text.count('возраст при отборе'), 1)


if __name__ == '__main__':
    unittest.main()
