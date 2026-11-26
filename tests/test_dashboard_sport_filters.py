import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

import polyarb_v4

ROOT = Path(__file__).resolve().parents[1]


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'input':
            value = dict(attrs)
            if value.get('id'):
                self.inputs[value['id']] = value


class DashboardSportFiltersTests(unittest.TestCase):
    def test_valorant_is_supported_and_checkbox_is_enabled_by_default(self):
        self.assertEqual(polyarb_v4.detect_sport('valorant-alpha-bravo'), 'VALORANT')
        parser = Inputs()
        parser.feed((ROOT / 'templates' / 'index.html').read_text(encoding='utf-8'))
        checkbox = parser.inputs['f-valorant']
        self.assertEqual(checkbox['type'], 'checkbox')
        self.assertIn('checked', checkbox)

    def test_valorant_checkbox_has_change_handler(self):
        app = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertRegex(app, r"\[[^\]]*'f-valorant'[^\]]*\]\.forEach\(id => \{")
        self.assertRegex(app, r"el\.addEventListener\('change', applyFilters\)")

    def test_real_filter_with_node_no_browser(self):
        node = shutil.which('node')
        if not node:
            try:
                from playwright._impl._driver import compute_driver_executable
                node = compute_driver_executable()[0]
            except ImportError:
                self.skipTest('Node.js is unavailable')
        result = subprocess.run(
            [node, str(ROOT / 'tests' / 'dashboard_sport_filters_node.js')],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
