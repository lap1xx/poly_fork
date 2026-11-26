import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MarketSides(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.children = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').split())
        if not self.depth and tag == 'div' and 'market-identity-sides' in classes:
            self.depth = 1
        elif self.depth:
            if self.depth == 1:
                self.children.append((tag, classes))
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1


class DashboardMarketIdentityTests(unittest.TestCase):
    def test_only_bookmaker_and_polymarket_sides_remain(self):
        html = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        parser = MarketSides()
        parser.feed(html)
        self.assertEqual(len(parser.children), 2)
        self.assertEqual(parser.children[0], ('div', {'market-identity-side', 'bookmaker'}))
        self.assertEqual(parser.children[1], ('div', {'market-identity-side', 'polymarket'}))
        self.assertNotIn('противоположные исходы', html)
        for element_id in ('market-bk-name', 'market-bk-outcome', 'market-pm-outcome'):
            self.assertIn(f'id="{element_id}"', html)

    def test_two_equal_columns_with_mobile_stack(self):
        css = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
        self.assertIn('.market-identity-sides{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));', css)
        self.assertIn('.market-identity-sides{grid-template-columns:1fr;gap:6px}', css)
        self.assertNotIn('market-identity-vs', css)


if __name__ == '__main__':
    unittest.main()
