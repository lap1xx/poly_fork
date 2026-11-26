import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ChartElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}
        self.placeholder_text = ''
        self.in_placeholder = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get('id')
        if element_id in ('price-chart', 'pm-book-head', 'chart-snapshot-placeholder'):
            self.elements[element_id] = (tag, attrs)
        if element_id == 'chart-snapshot-placeholder':
            self.in_placeholder = True

    def handle_data(self, data):
        if self.in_placeholder:
            self.placeholder_text += data

    def handle_endtag(self, tag):
        if self.in_placeholder and tag == 'div':
            self.in_placeholder = False


class DashboardChartSnapshotTests(unittest.TestCase):
    def test_placeholder_is_normal_hidden_text_with_responsive_style(self):
        html = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        parser = ChartElements()
        parser.feed(html)
        self.assertEqual(parser.elements['pm-book-head'][0], 'thead')
        self.assertEqual(parser.elements['price-chart'][0], 'canvas')
        tag, attrs = parser.elements['chart-snapshot-placeholder']
        self.assertEqual(tag, 'div')
        self.assertIn('hidden', attrs)
        self.assertIn('История цен для сохранённого снимка не загружается', parser.placeholder_text)
        css = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
        self.assertIn('.price-chart[hidden]', css)
        self.assertIn('.price-chart-placeholder[hidden]', css)
        self.assertIn('.pm-book thead[hidden]', css)
        self.assertIn('.pm-book tbody td.snapshot-empty-cell', css)
        self.assertIn('overflow-wrap:anywhere', css)

    def test_real_chart_and_snapshot_logic_without_browser(self):
        node = shutil.which('node')
        if not node:
            try:
                from playwright._impl._driver import compute_driver_executable
                node = compute_driver_executable()[0]
            except ImportError:
                self.skipTest('Node.js is unavailable')
        result = subprocess.run(
            [node, str(ROOT / 'tests' / 'dashboard_chart_snapshot_node.js')],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
