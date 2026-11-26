import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ActionElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') in ('btn-open-bb', 'btn-buy-pm'):
            self.elements[attrs['id']] = (tag, attrs)


class DashboardEventLinksTests(unittest.TestCase):
    def test_native_links_are_initially_disabled_and_safe(self):
        html = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        parser = ActionElements()
        parser.feed(html)
        self.assertEqual(len(parser.elements), 2)
        for tag, attrs in parser.elements.values():
            self.assertEqual(tag, 'a')
            self.assertNotIn('href', attrs)
            self.assertEqual(attrs['target'], '_blank')
            self.assertEqual(set(attrs['rel'].split()), {'noopener', 'noreferrer'})
            self.assertEqual(attrs['aria-disabled'], 'true')
            self.assertEqual(attrs['tabindex'], '-1')

        app = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('openBookmakerEvent', app)
        self.assertNotIn('openPMUrl', app)
        self.assertNotRegex(app, r"addEventListener\(\s*['\"]auxclick")
        self.assertNotRegex(app, r"(?:openBB|buyPM)\.addEventListener\(")

    def test_readable_card_css_and_disabled_link_style(self):
        css = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
        readable = css.split('/* Читаемая карточка:', 1)[1]
        for rule in ('.kpi-val{font-size:20px}', '.pm-book{font-size:13px',
                     '.hm-outcome{font-size:12px', '.hm-leg input{font-size:14px}',
                     '.hm-leg-return b{font-size:14px', '.hm-info>div{font-size:12px',
                     '.event-action[aria-disabled="true"]', '.event-action:focus-visible'):
            self.assertIn(rule, readable)

    def test_real_link_logic_in_node_without_browser(self):
        node = shutil.which('node')
        if not node:
            try:
                from playwright._impl._driver import compute_driver_executable
                node = compute_driver_executable()[0]
            except ImportError:
                self.skipTest('Node.js is unavailable')
        result = subprocess.run(
            [node, str(ROOT / 'tests' / 'dashboard_event_links_node.js')],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
