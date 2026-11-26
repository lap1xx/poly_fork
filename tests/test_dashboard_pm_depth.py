import json
import random
import shutil
import subprocess
import unittest
from pathlib import Path

from pm_depth_quote import quote_pm_asks

ROOT = Path(__file__).resolve().parents[1]


class DashboardPMDepthTests(unittest.TestCase):
    def test_client_math_freshness_and_fallback_without_browser(self):
        node = shutil.which('node')
        if not node:
            try:
                from playwright._impl._driver import compute_driver_executable
                node = compute_driver_executable()[0]
            except ImportError:
                self.skipTest('Node.js is unavailable')
        rng = random.Random(261126)
        schedule = {'enabled': True, 'rate': .05, 'exponent': 1.0, 'source': 'clob_market_info'}
        fixtures = []
        for _ in range(120):
            asks = [[p, rng.uniform(.1, 80)] for p in
                    sorted(rng.sample([x / 100 for x in range(1, 99)], 6))]
            fixtures.append({'asks': asks, 'budget': rng.uniform(1, 150), 'schedule': schedule})
        result = subprocess.run(
            [node, str(ROOT / 'tests' / 'dashboard_pm_depth_node.js')],
            input=json.dumps(fixtures), cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = json.loads(result.stdout)
        self.assertEqual(len(outputs), len(fixtures))
        for fixture, actual in zip(fixtures, outputs):
            expected = quote_pm_asks(fixture['asks'], fixture['budget'], .05, 1)
            self.assertEqual(actual['full'], expected['full'])
            for key in ('shares', 'fee', 'total_cost', 'avg_price', 'odds_eff',
                        'unfilled_usd', 'available_cash_usd'):
                self.assertAlmostEqual(actual[key], expected[key], places=8, msg=key)


if __name__ == '__main__':
    unittest.main()
