from __future__ import annotations

import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WEBUI_ROOT = PLUGIN_ROOT / "pages" / "helper-tools"


class DashboardAssetTests(unittest.TestCase):
    def test_destructive_actions_use_the_in_page_confirmation_dialog(self) -> None:
        script = (WEBUI_ROOT / "app.js").read_text(encoding="utf-8")
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="confirm-action-dialog"', markup)
        self.assertIn('id="confirm-action-form"', markup)
        self.assertIn('style.css?v=0.10.14', markup)
        self.assertIn('app.js?v=0.10.14', markup)
        self.assertIn("async function deleteWallpaperImage(image)", script)
        self.assertIn("await confirmAction(`删除图片", script)
        self.assertIn("await confirmAction(`删除图库", script)
        self.assertNotIn('window.confirm(`删除图片', script)
        self.assertNotIn('window.confirm(`删除图库', script)


if __name__ == "__main__":
    unittest.main()
