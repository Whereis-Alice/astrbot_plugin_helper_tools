from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from astrbot_plugin_helper_tools import wake_service as wake_module
from astrbot_plugin_helper_tools.wake_service import WakeService

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


class _Config(dict[str, Any]):
    def __init__(self, value: dict[str, Any], *, save_error: bool = False) -> None:
        super().__init__(value)
        self.save_calls = 0
        self.save_error = save_error

    def save_config(self) -> None:
        self.save_calls += 1
        if self.save_error:
            raise RuntimeError("config disk failure")


class WakeConfigSaveTests(unittest.TestCase):
    def test_block_keyword_migration_saves_config(self) -> None:
        config = _Config({"wake": {"use_default_block_keywords": False, "block_keywords": ["abc", "abc"]}})

        service = WakeService(config, None)

        self.assertEqual(config["wake"]["block_keywords"], ["abc"])
        self.assertTrue(config["wake"]["block_keywords_initialized"])
        self.assertEqual(config.save_calls, 1)
        self.assertEqual(service.block_keywords(), ["abc"])

    def test_save_failure_is_logged_and_not_raised(self) -> None:
        config = _Config(
            {"wake": {"use_default_block_keywords": False, "block_keywords": ["abc"]}},
            save_error=True,
        )

        with patch.object(wake_module, "logger") as fake_logger:
            WakeService(config, None)

        self.assertEqual(config.save_calls, 1)
        fake_logger.warning.assert_called_once()
        self.assertTrue(fake_logger.warning.call_args.kwargs.get("exc_info"))
        self.assertIn("[HelperTools/Wake]", fake_logger.warning.call_args.args[0])


class AdminWakeWordsTests(unittest.TestCase):
    """管理员专属唤醒词默认必须为空，避免管理员随口说话就唤醒 LLM。"""

    def test_empty_list_stays_empty(self) -> None:
        service = WakeService(_Config({"wake": {"admin_wake_words": []}}), None)

        self.assertEqual(service.admin_wake_words(), [])

    def test_blank_string_stays_empty(self) -> None:
        service = WakeService(_Config({"wake": {"admin_wake_words": "   "}}), None)

        self.assertEqual(service.admin_wake_words(), [])

    def test_missing_key_stays_empty(self) -> None:
        service = WakeService(_Config({"wake": {}}), None)

        self.assertEqual(service.admin_wake_words(), [])

    def test_missing_section_stays_empty(self) -> None:
        service = WakeService(_Config({}), None)

        self.assertEqual(service.admin_wake_words(), [])

    def test_blank_items_are_dropped(self) -> None:
        service = WakeService(_Config({"wake": {"admin_wake_words": ["", "  ", "喂"]}}), None)

        self.assertEqual(service.admin_wake_words(), ["喂"])

    def test_admin_message_does_not_match_when_cleared(self) -> None:
        service = WakeService(_Config({"wake": {"admin_wake_words": []}}), None)

        match = service.match_wake_word("宝宝今天晚饭吃什么", is_admin=True)

        self.assertFalse(match.matched)
        self.assertEqual(match.word, "")
        self.assertFalse(match.admin_only)

    def test_admin_message_still_matches_configured_word(self) -> None:
        config = _Config({"wake": {"admin_wake_words": ["宝宝"]}})
        service = WakeService(config, None)

        admin_match = service.match_wake_word("宝宝今天晚饭吃什么", is_admin=True)
        member_match = service.match_wake_word("宝宝今天晚饭吃什么", is_admin=False)

        self.assertTrue(admin_match.matched)
        self.assertEqual(admin_match.word, "宝宝")
        self.assertTrue(admin_match.admin_only)
        self.assertFalse(member_match.matched)


class AdminWakeWordsSchemaTests(unittest.TestCase):
    """守住 schema 默认值，避免以后又把「宝贝」「宝宝」加回去。"""

    def _entry(self) -> dict[str, Any]:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        return schema["wake"]["items"]["admin_wake_words"]

    def test_schema_default_is_empty_list(self) -> None:
        entry = self._entry()

        self.assertEqual(entry["default"], [])

    def test_schema_hint_explains_empty_default(self) -> None:
        hint = self._entry().get("hint", "")

        self.assertIn("留空", hint)


if __name__ == "__main__":
    unittest.main()
