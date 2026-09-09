from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.webui_service import HelperToolsDashboard


ROOT = Path(__file__).resolve().parents[1]


class SplitBoundaryTests(unittest.TestCase):
    def test_helper_no_longer_exposes_bilibili_runtime_handlers(self) -> None:
        names = set(dir(HelperToolsPlugin))
        self.assertFalse(any("bilibili" in name.lower() for name in names))
        self.assertFalse(any("reply_card" in name.lower() for name in names))

    def test_legacy_schema_sections_are_invisible_but_preserved(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for name in ("bilibili_video", "bilibili_article", "reply_card_reader"):
            self.assertTrue(schema[name]["invisible"])

        config = SimpleNamespace(data_dir=ROOT, config={}, context=None)
        dashboard = HelperToolsDashboard(config, version="2.0.0")
        self.assertNotIn("bilibili_video", dashboard._schema)
        self.assertNotIn("bilibili_article", dashboard._schema)
        self.assertNotIn("reply_card_reader", dashboard._schema)


if __name__ == "__main__":
    unittest.main()
