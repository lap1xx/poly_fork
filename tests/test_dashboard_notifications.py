import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DashboardNotificationTests(unittest.TestCase):
    def test_notifications_are_optional_fresh_and_deduplicated_without_browser(self):
        node = shutil.which('node')
        if not node:
            try:
                from playwright._impl._driver import compute_driver_executable
                node = compute_driver_executable()[0]
            except ImportError:
                self.skipTest('Node.js is unavailable')
        result = subprocess.run(
            [node, str(ROOT / 'tests' / 'dashboard_notifications_node.js')],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_button_is_opt_in_and_accessible(self):
        html = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="btn-desktop-notify"', html)
        self.assertIn('aria-label="Уведомления выключены"', html)
        self.assertIn('aria-pressed="false"', html)


if __name__ == '__main__':
    unittest.main()
