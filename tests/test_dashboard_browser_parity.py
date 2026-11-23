"""Real-browser parity coverage for dashboard dual-read cards.

The test derives an Opportunity v1 envelope from the captured quote replay using
production normalization, then feeds the canonical UI snapshot shape to the
production page over a controlled WebSocket shim. Backend transport/validation
is covered separately by tests/test_event_pipeline.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import unittest

from opportunity import normalize_scanner_snapshot

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - CI installs the dev dependency
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "tests" / "fixtures" / "quote_quality_replay.json"


def make_ui_snapshot_pair(action_link_mode: str = "v1") -> dict:
    """Build a realistic legacy+v1 pair from the checked-in captured replay."""
    captured = json.loads(REPLAY.read_text(encoding="utf-8"))
    pm = captured["polymarket"]["books"][0]
    bookmaker = next(
        quote for quote in captured["onewin"]["quotes"]
        if quote["outcome"] != pm["outcome"]
    )
    now = time.time()
    slug = captured["market_slug"]
    pm_event_url = f"https://polymarket.com/event/{slug}"
    bookmaker_event_url = "https://f1w-7707.com/betting/match/esport/replay-match"
    legacy = {
        "slug": slug,
        "q": captured["question"],
        "sport": "CS2",
        "market_type": "MATCH",
        "phase": "prematch",
        "currency": "USDT",
        "roi": 0.031,
        "bank": 40.0,
        "profit": 1.24,
        "ts": now,
        "bk_source": "1win",
        "bk_odds_age_sec": 30.01,
        "source_age_sec": 30.01,
        "source_state": "stale",
        "age_sec": 3,
        "pm_event_url": pm_event_url,
        "bk_event_url": bookmaker_event_url,
        "bets": [
            {
                "source": "Polymarket",
                "outcome": pm["outcome"],
                "token": pm["token_id"],
                "o": 1.15,
                "pm_best_ask": pm["asks"][0][0],
                "pm_avg_ask": pm["asks"][0][0],
                "pm_book_age_sec": 7.2,
                "c": 0.0,
                "s": 0.0,
                "pm_fee_enabled": True,
                "pm_fee_rate": 0.05,
                "pm_fee_exponent": 1.0,
                "pm_fee_source": "clob_market_info",
            },
            {
                "source": "1win",
                "outcome": bookmaker["outcome"],
                "o": bookmaker["odds"],
                "odds_age_sec": 30.01,
                "c": 0.0,
                "s": 0.0,
            },
        ],
        "stakes": [22.5, 17.5],
        "is_streamer": True,
    }
    transport = {
        "type": "scanner.snapshot",
        "schema_version": 1,
        "scanner_id": "polyarb_v4",
        "session_id": "browser-parity-replay",
        "seq": 1,
        "sent_at": now,
        "arbs": {slug: legacy},
    }
    normalized = normalize_scanner_snapshot(transport, now_epoch=now + 0.001)
    if normalized["rejected"] or len(normalized["opportunities"]) != 1:
        raise AssertionError(f"Replay did not normalize: {normalized['rejected']}")
    if normalized["opportunities"][0]["status"] != "stale":
        raise AssertionError("Replay fixture must exercise stale dual-read presentation")
    ui_legacy = dict(legacy)
    if action_link_mode == "v1":
        ui_legacy.pop("pm_event_url")
        ui_legacy.pop("bk_event_url")
    elif action_link_mode == "legacy-fallback":
        for leg in normalized["opportunities"][0]["legs"]:
            leg.pop("event_url", None)
    else:
        raise ValueError(f"Unknown action link mode: {action_link_mode}")
    return {
        "type": "snapshot",
        "arbs": [ui_legacy],
        "opportunities_v1": normalized,
        "scan_status": {"state": "running", "ts": now},
        "config": {"bank": 40.0},
    }


@unittest.skipUnless(sync_playwright is not None, "Playwright dev dependency is not installed")
class DashboardBrowserParityTests(unittest.TestCase):
    def test_card_reads_v1_and_detail_remains_legacy(self):
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "web_app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            import urllib.request
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    self.fail("uvicorn exited before the dashboard became ready")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("dashboard server did not become ready")

            snapshots = {
                mode: make_ui_snapshot_pair(mode)
                for mode in ("v1", "legacy-fallback")
            }
            snapshots_json = json.dumps(snapshots, ensure_ascii=False)
            shim = f"""
              (() => {{
                const snapshots = {snapshots_json};
                const actionMode = new URL(location.href).searchParams.get('action_mode') || 'v1';
                const message = snapshots[actionMode];
                window.__openedUrls = [];
                window.open = (url) => {{ window.__openedUrls.push(String(url)); return {{ closed: false }}; }};
                class ReplayWebSocket {{
                  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
                  constructor(url) {{
                    this.url = url; this.readyState = 0;
                    setTimeout(() => {{
                      this.readyState = 1;
                      if (this.onopen) this.onopen({{target: this}});
                      if (String(url).endsWith('/ws')) setTimeout(() => {{
                        if (this.onmessage) this.onmessage({{data: JSON.stringify(message), target: this}});
                      }}, 20);
                    }}, 0);
                  }}
                  send() {{}}
                  close() {{ this.readyState = 3; if (this.onclose) this.onclose({{target: this}}); }}
                }}
                window.WebSocket = ReplayWebSocket;
              }})();
            """
            with sync_playwright() as p:
                launch_options = {"headless": True, "args": ["--no-sandbox"]}
                if chromium:
                    launch_options["executable_path"] = chromium
                browser = p.chromium.launch(**launch_options)
                try:
                    for action_mode, snapshot in snapshots.items():
                        with self.subTest(action_link_mode=action_mode):
                            page = browser.new_page()
                            page.add_init_script(shim)
                            page.goto(
                                f"http://127.0.0.1:{port}/?action_mode={action_mode}",
                                wait_until="domcontentloaded",
                            )
                            card = page.locator(".arb[data-arb-slug]").first
                            card.wait_for(state="visible", timeout=10000)
                            expected = snapshot["opportunities_v1"]["opportunities"][0]
                            self.assertEqual(card.get_attribute("data-arb-slug"), expected["market"]["slug"])
                            self.assertEqual(card.locator(".arb-title").inner_text(), expected["market"]["question"])
                            self.assertEqual(card.locator(".arb-roi").inner_text(), "+3.10%")
                            warning_tags = card.locator(".arb-tags .pm-age").all_inner_texts()
                            self.assertEqual(sum("УСТАРЕЛИ" in tag or "КОТИРОВКИ УСТАРЕЛИ" in tag for tag in warning_tags), 1)

                            # List/card dual-read remains unchanged; detail and
                            # calculator still render the legacy-backed record.
                            card.click()
                            detail_roi = page.locator("#kpi-roi")
                            detail_roi.wait_for(state="visible", timeout=5000)
                            self.assertEqual(detail_roi.inner_text(), "+3.10%")
                            self.assertEqual(page.locator("#market-pm-outcome").inner_text(), expected["legs"][0]["outcome"])
                            self.assertEqual(page.locator("#market-bk-outcome").inner_text(), expected["legs"][1]["outcome"])

                            expected_urls = {
                                leg["source"]: leg.get("event_url")
                                for leg in expected["legs"]
                            }
                            legacy_row = snapshot["arbs"][0]
                            for source, legacy_key in (
                                ("Polymarket", "pm_event_url"),
                                ("1win", "bk_event_url"),
                            ):
                                expected_urls[source] = (
                                    expected_urls[source] or legacy_row.get(legacy_key)
                                )
                            self.assertEqual(
                                expected_urls["Polymarket"],
                                "https://polymarket.com/event/" + expected["market"]["slug"],
                            )
                            self.assertEqual(
                                expected_urls["1win"],
                                "https://f1w-7707.com/betting/match/esport/replay-match",
                            )

                            # v1 legs are preferred; absent per-leg URLs must
                            # still fall back to the matching legacy root field.
                            page.locator("#btn-buy-pm").click()
                            page.locator("#btn-open-bb").click()
                            self.assertEqual(page.evaluate("window.__openedUrls"), [
                                expected_urls["Polymarket"], expected_urls["1win"]
                            ])
                            page.close()
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
