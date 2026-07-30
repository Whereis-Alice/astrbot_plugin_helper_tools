from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import TextPart

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.perception_service import (
    PERCEPTION_CONTEXT_PREFIX,
    PERCEPTION_LOG_FULL,
    PERCEPTION_LOG_OFF,
    PERCEPTION_LOG_SUMMARY,
    EnvironmentPerceptionService,
    request_has_perception_context,
)


class DummyEvent:
    def __init__(
        self,
        *,
        platform: str = "aiocqhttp",
        sender_id: str = "123456",
        group_id: str = "10000",
    ) -> None:
        self._platform = platform
        self._sender_id = sender_id
        self._group_id = group_id
        self.unified_msg_origin = f"default:GroupMessage:{group_id}"
        self.message_obj = SimpleNamespace(
            group=SimpleNamespace(group_name="测试群"),
            sender=SimpleNamespace(card="小明", nickname="小明"),
        )

    def get_platform_name(self) -> str:
        return self._platform

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_messages(self) -> list[object]:
        return [Comp.Plain("测试"), Comp.Image.fromURL("https://example.com/a.jpg")]

    def is_stopped(self) -> bool:
        return False


class PerceptionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_2026_holiday_and_future_coverage_are_truthful(self) -> None:
        service = EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_sender_qq": True,
                    "include_almanac": True,
                }
            }
        )
        event = DummyEvent()
        spring_festival = await service.context_for_event(
            event,
            now=datetime(2026, 2, 17, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        unavailable_year = await service.context_for_event(
            event,
            now=datetime(2027, 1, 1, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertIn(PERCEPTION_CONTEXT_PREFIX, spring_festival)
        self.assertIn("法定节假日（春节）", spring_festival)
        self.assertIn("上午", spring_festival)
        self.assertIn("QQ 号为 123456", spring_festival)
        self.assertIn("含图片", spring_festival)
        self.assertIn("暂未覆盖 2027 年", unavailable_year)

    async def test_sender_qq_is_only_exposed_for_qq_platforms(self) -> None:
        service = EnvironmentPerceptionService(
            {"perception": {"enabled": True, "include_sender_qq": True}}
        )
        qq_context = await service.context_for_event(DummyEvent())
        telegram_context = await service.context_for_event(
            DummyEvent(platform="telegram", sender_id="123456")
        )

        self.assertIn("QQ 号为 123456", qq_context)
        self.assertNotIn("QQ 号为 123456", telegram_context)

    async def test_main_hook_marks_perception_as_temporary(self) -> None:
        service = EnvironmentPerceptionService({"perception": {"enabled": True}})
        plugin = SimpleNamespace(enabled=lambda: True, perception=service)
        request = SimpleNamespace(prompt="", extra_user_content_parts=[])

        with patch("astrbot_plugin_helper_tools.main.logger.info") as log_info:
            await HelperToolsPlugin.perception_context_handler(
                plugin, DummyEvent(), request
            )

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertIsInstance(part, TextPart)
        self.assertTrue(getattr(part, "_no_save", False))
        self.assertTrue(request_has_perception_context(request))
        self.assertEqual(service.log_mode(), PERCEPTION_LOG_SUMMARY)
        log_info.assert_called_once()
        self.assertNotIn(PERCEPTION_CONTEXT_PREFIX, str(log_info.call_args))

    async def test_full_logging_can_be_enabled_or_logging_disabled(self) -> None:
        event = DummyEvent()
        full_service = EnvironmentPerceptionService(
            {"perception": {"enabled": True, "log_mode": PERCEPTION_LOG_FULL}}
        )
        full_plugin = SimpleNamespace(enabled=lambda: True, perception=full_service)
        full_request = SimpleNamespace(prompt="", extra_user_content_parts=[])

        with patch("astrbot_plugin_helper_tools.main.logger.info") as log_info:
            await HelperToolsPlugin.perception_context_handler(
                full_plugin, event, full_request
            )
        self.assertIn(PERCEPTION_CONTEXT_PREFIX, str(log_info.call_args))

        off_service = EnvironmentPerceptionService(
            {"perception": {"enabled": True, "log_mode": PERCEPTION_LOG_OFF}}
        )
        off_plugin = SimpleNamespace(enabled=lambda: True, perception=off_service)
        off_request = SimpleNamespace(prompt="", extra_user_content_parts=[])

        with patch("astrbot_plugin_helper_tools.main.logger.info") as log_info:
            await HelperToolsPlugin.perception_context_handler(
                off_plugin, event, off_request
            )
        log_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
