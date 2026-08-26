from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from astrbot_plugin_helper_tools import wake_service as wake_module
from astrbot_plugin_helper_tools.wake_service import WakeService


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


if __name__ == "__main__":
    unittest.main()
