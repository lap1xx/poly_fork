import asyncio
import unittest
from unittest.mock import patch

import web_app
from pm_event_links import polymarket_event_url_for_market


CHILD_SLUG = "cs2-ttgesp-zotix-2026-11-24-game2"
PARENT_SLUG = "cs2-ttgesp-zotix-2026-11-24"
PARENT_URL = f"https://polymarket.com/event/{PARENT_SLUG}"


class PolymarketEventLinkTests(unittest.TestCase):
    def test_selects_parent_event_slug_from_nested_gamma_metadata(self):
        market = {
            "slug": CHILD_SLUG,
            "events": [{"slug": PARENT_SLUG}],
        }

        self.assertEqual(polymarket_event_url_for_market(market), PARENT_URL)

    def test_falls_back_to_market_slug_when_event_metadata_is_absent(self):
        self.assertEqual(
            polymarket_event_url_for_market({"slug": CHILD_SLUG}),
            f"https://polymarket.com/event/{CHILD_SLUG}",
        )

    def test_rejects_malformed_slugs(self):
        self.assertIsNone(polymarket_event_url_for_market({"slug": "../bad"}))
        self.assertIsNone(polymarket_event_url_for_market(None))

    def test_api_resolves_market_slug_to_parent_event(self):
        gamma_market = {
            "slug": CHILD_SLUG,
            "events": [{"slug": PARENT_SLUG}],
        }
        with patch.object(web_app, "_http_get", return_value=[gamma_market]):
            result = asyncio.run(web_app.api_pm_url(CHILD_SLUG))

        self.assertEqual(result, {"url": PARENT_URL})

    def test_api_uses_child_slug_fallback_when_gamma_has_no_parent(self):
        with patch.object(web_app, "_http_get", return_value=[{"slug": CHILD_SLUG}]):
            result = asyncio.run(web_app.api_pm_url(CHILD_SLUG))

        self.assertEqual(result, {"url": f"https://polymarket.com/event/{CHILD_SLUG}"})

    def test_api_returns_null_when_gamma_is_unavailable(self):
        with patch.object(web_app, "_http_get", side_effect=TimeoutError("offline")):
            result = asyncio.run(web_app.api_pm_url(CHILD_SLUG))

        self.assertEqual(result, {"url": None})


if __name__ == "__main__":
    unittest.main()
