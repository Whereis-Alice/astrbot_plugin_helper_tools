from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import TextPart

from astrbot_plugin_helper_tools.helper_utils import format_timestamp
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
        self_id: str = "10001",
        bot: object | None = None,
    ) -> None:
        self._platform = platform
        self._sender_id = sender_id
        self._group_id = group_id
        self._self_id = self_id
        self.bot = bot
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

    def get_self_id(self) -> str:
        return self._self_id

    def get_messages(self) -> list[object]:
        return [Comp.Plain("测试"), Comp.Image.fromURL("https://example.com/a.jpg")]

    def is_stopped(self) -> bool:
        return False


class DummyOneBot:
    def __init__(self, member: dict[str, object]) -> None:
        self.member = member
        self.calls: list[dict[str, object]] = []

    async def get_group_member_info(self, **params: object) -> dict[str, object]:
        self.calls.append(params)
        return {"data": self.member}


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

    async def test_sender_group_profile_is_injected_once_and_cached(self) -> None:
        bot = DummyOneBot(
            {
                "user_id": 123456,
                "nickname": "Alice QQ",
                "card": "群里的爱丽丝",
                "role": "admin",
                "level": "42",
                "title": "星之群管",
                "title_expire_time": 4_102_444_800,
                "shut_up_timestamp": 0,
            }
        )
        service = EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_sender_qq": True,
                    "include_sender_group_profile": True,
                    "include_bot_group_identity": False,
                    "sender_group_profile_cache_seconds": 60,
                }
            }
        )
        event = DummyEvent(bot=bot)

        first_context = await service.context_for_event(event)
        second_context = await service.context_for_event(event)

        self.assertIn("身份校验：QQ=123456", first_context)
        self.assertIn("QQ昵称=Alice QQ", first_context)
        self.assertIn("群昵称=群里的爱丽丝", first_context)
        self.assertIn("群身份=管理员", first_context)
        self.assertIn("群等级=42", first_context)
        self.assertIn("群头衔=星之群管", first_context)
        self.assertIn("头衔到期=", first_context)
        self.assertEqual(first_context.count("身份校验：QQ=123456"), 1)
        self.assertEqual(first_context, second_context)
        self.assertEqual(
            bot.calls,
            [{"group_id": 10000, "user_id": 123456, "no_cache": True}],
        )

    async def test_sender_group_profile_respects_config_and_platform(self) -> None:
        disabled_bot = DummyOneBot({"role": "admin"})
        disabled_context = await EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_sender_group_profile": False,
                    "include_bot_group_identity": False,
                }
            }
        ).context_for_event(DummyEvent(bot=disabled_bot))
        self.assertNotIn("群身份=管理员", disabled_context)
        self.assertEqual(disabled_bot.calls, [])

        telegram_bot = DummyOneBot({"role": "admin"})
        telegram_context = await EnvironmentPerceptionService(
            {"perception": {"enabled": True, "include_sender_group_profile": True}}
        ).context_for_event(DummyEvent(platform="telegram", bot=telegram_bot))
        self.assertNotIn("群身份=管理员", telegram_context)
        self.assertEqual(telegram_bot.calls, [])

        fallback_context = await EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_sender_qq": True,
                    "include_sender_group_profile": True,
                    "include_bot_group_identity": False,
                }
            }
        ).context_for_event(DummyEvent(bot=object()))
        self.assertIn("当前发言者 QQ 号为 123456", fallback_context)
        self.assertNotIn("群身份=管理员", fallback_context)

    async def test_bot_group_identity_is_injected_and_cached(self) -> None:
        bot = DummyOneBot(
            {
                "role": "admin",
                "card": "爱丽丝",
                "level": "7",
                "title": "群管理小助手",
                "title_expire_time": 4_102_444_800,
            }
        )
        service = EnvironmentPerceptionService(
            {"perception": {"enabled": True, "bot_group_identity_cache_seconds": 60}}
        )
        event = DummyEvent(bot=bot)

        first_context = await service.context_for_event(event)
        second_context = await service.context_for_event(event)

        self.assertIn("你在当前群的身份：管理员", first_context)
        self.assertIn("你的群昵称：爱丽丝", first_context)
        self.assertIn("你的群等级：7", first_context)
        self.assertIn("你的群专属头衔：群管理小助手", first_context)
        self.assertIn("头衔有效至：", first_context)
        self.assertEqual(first_context, second_context)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(
            bot.calls[0],
            {"group_id": 10000, "user_id": 10001, "no_cache": True},
        )

    async def test_bot_group_identity_role_labels_and_no_title(self) -> None:
        owner = EnvironmentPerceptionService._format_bot_group_identity(
            {"role": "owner", "title": "群主"}
        )
        member = EnvironmentPerceptionService._format_bot_group_identity(
            {"role": "member"}
        )

        self.assertIn("你在当前群的身份：群主", owner)
        self.assertIn("你的群专属头衔：群主", owner)
        self.assertIn("你在当前群的身份：普通群员", member)
        self.assertIn("你当前没有群专属头衔", member)

    async def test_expired_mute_and_title_timestamps_are_not_shown_as_live(self) -> None:
        now = datetime(2026, 10, 13, 9, 47, tzinfo=timezone.utc)
        expired_mute = int(datetime(2026, 5, 22, 8, 1, 57, tzinfo=timezone.utc).timestamp())
        future_title = int(datetime(2100, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())

        bot_identity = EnvironmentPerceptionService._format_bot_group_identity(
            {
                "role": "member",
                "level": "2",
                "shut_up_timestamp": expired_mute,
            },
            now=now,
        )
        self.assertNotIn("你当前被禁言至", bot_identity)
        self.assertIn("你在当前群的身份：普通群员", bot_identity)

        sender_profile = EnvironmentPerceptionService._format_sender_group_profile(
            "123456",
            {
                "user_id": 123456,
                "role": "owner",
                "title": "过期的头衔",
                "title_expire_time": expired_mute,
                "shut_up_timestamp": expired_mute,
            },
            now=now,
        )
        self.assertNotIn("禁言到期=", sender_profile)
        self.assertNotIn("过期的头衔", sender_profile)
        self.assertIn("群头衔=无", sender_profile)
        self.assertIn("身份校验：QQ=123456", sender_profile)

        active_profile = EnvironmentPerceptionService._format_sender_group_profile(
            "123456",
            {
                "user_id": 123456,
                "role": "owner",
                "title": "在任头衔",
                "title_expire_time": future_title,
                "shut_up_timestamp": future_title,
            },
            now=now,
        )
        self.assertIn("禁言到期=", active_profile)
        self.assertIn(f"禁言到期={format_timestamp(future_title)}", active_profile)
        self.assertIn("群头衔=在任头衔", active_profile)

        permanent = EnvironmentPerceptionService._format_sender_group_profile(
            "123456",
            {"user_id": 123456, "role": "owner", "shut_up_timestamp": -1},
            now=now,
        )
        self.assertNotIn("禁言到期=", permanent)

    async def test_context_hides_expired_mute_end_to_end(self) -> None:
        expired_mute = int(
            datetime(2026, 5, 22, 8, 1, 57, tzinfo=timezone.utc).timestamp()
        )
        bot = DummyOneBot(
            {
                "user_id": 10001,
                "role": "member",
                "level": "2",
                "shut_up_timestamp": expired_mute,
            }
        )
        service = EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_bot_group_identity": True,
                    "include_sender_group_profile": True,
                    "sender_group_profile_cache_seconds": 0,
                    "bot_group_identity_cache_seconds": 0,
                }
            }
        )
        context = await service.context_for_event(
            DummyEvent(bot=bot, sender_id="123456"),
            now=datetime(2026, 10, 13, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertIn("发送时间：2026-10-13 09:47:00", context)
        self.assertNotIn("禁言", context)
        self.assertIn("你在当前群的身份：普通群员", context)

    async def test_bot_group_identity_respects_config_and_adapter_support(self) -> None:
        disabled_bot = DummyOneBot({"role": "admin"})
        disabled_service = EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_bot_group_identity": False,
                }
            }
        )
        disabled_context = await disabled_service.context_for_event(DummyEvent(bot=disabled_bot))
        self.assertNotIn("你在当前群的身份", disabled_context)
        self.assertEqual(disabled_bot.calls, [])

        telegram_bot = DummyOneBot({"role": "admin"})
        telegram_context = await EnvironmentPerceptionService(
            {"perception": {"enabled": True}}
        ).context_for_event(DummyEvent(platform="telegram", bot=telegram_bot))
        self.assertNotIn("你在当前群的身份", telegram_context)
        self.assertEqual(telegram_bot.calls, [])

        unsupported_context = await EnvironmentPerceptionService(
            {"perception": {"enabled": True}}
        ).context_for_event(DummyEvent(bot=object()))
        self.assertIn(PERCEPTION_CONTEXT_PREFIX, unsupported_context)
        self.assertNotIn("你在当前群的身份", unsupported_context)

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
