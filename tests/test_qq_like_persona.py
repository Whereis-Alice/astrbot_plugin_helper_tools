from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.qq_like_service import QQProfileLikeService


class FakeBot:
    """模拟 LLOneBot：get_friend_list 无参数，send_like 只认 user_id / times。"""

    def __init__(self) -> None:
        self.likes: list[tuple[int, int]] = []

    async def get_version_info(self):
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
        }

    async def get_friend_list(self):
        return {"status": "ok", "retcode": 0, "data": [{"user_id": "20001"}]}

    async def send_like(self, *, user_id: int, times: int):
        assert (user_id, times) == (20001, 10)
        self.likes.append((user_id, times))
        return {"status": "ok", "retcode": 0, "data": None}


class FakeEvent:
    def __init__(
        self,
        original_text: str = "/赞我",
        *,
        group_id: str = "",
        messages: list[object] | None = None,
    ) -> None:
        # WakingStage strips the event text but preserves the adapter's original.
        self.message_str = "赞我"
        self.group_id = group_id
        self.unified_msg_origin = (
            f"default:GroupMessage:{group_id}"
            if group_id
            else "default:FriendMessage:20001"
        )
        self.message_obj = SimpleNamespace(
            message_str=original_text,
            message=messages if messages is not None else [Comp.Plain(original_text)],
        )
        self.bot = FakeBot()
        self._extras: dict[str, str] = {}
        self.is_at_or_wake_command = False
        self.call_llm = False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_platform_id(self) -> str:
        return "qq-test"

    def get_sender_id(self) -> str:
        return "20001"

    def get_self_id(self) -> str:
        return "10001"

    def get_group_id(self) -> str:
        return self.group_id

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_extra(self, key: str, default: str = "") -> str:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: str) -> None:
        self._extras[key] = value

    def is_stopped(self) -> bool:
        return False

    def should_call_llm(self, value: bool) -> None:
        self.call_llm = value

    def stop_event(self) -> None:
        raise AssertionError("persona reply must not stop the default LLM pipeline")


class QQProfileLikePersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()

    def make_plugin(self, prefixes: list[str] | None = None) -> HelperToolsPlugin:
        config = {
            "qq_like": {
                "enabled": True,
                "cooldown_seconds": 0,
                # 触发词默认已改成空列表（用户能真正清空），这里显式配置。
                "trigger_phrases": ["赞我"],
                "persona_reply": {"enabled": True},
            }
        }
        plugin = object.__new__(HelperToolsPlugin)
        plugin.config = config
        plugin.context = SimpleNamespace(
            get_config=Mock(
                return_value={"wake_prefix": ["/"] if prefixes is None else prefixes}
            )
        )
        plugin.qq_like = QQProfileLikeService(config)
        plugin.wake = SimpleNamespace(is_llm_request_blocked=lambda _event: False)
        plugin.wallpaper = SimpleNamespace(handle_message=AsyncMock(return_value=None))
        plugin.steam = SimpleNamespace(
            match_message=Mock(return_value=SimpleNamespace(handled=False))
        )
        plugin.voice = SimpleNamespace(should_handle_message=Mock(return_value=False))
        return plugin

    async def test_persona_mode_forces_default_pipeline_and_keeps_fact_temporary(
        self,
    ) -> None:
        plugin = self.make_plugin()
        like_service = plugin.qq_like
        event = FakeEvent()

        results = [
            item
            async for item in HelperToolsPlugin.dynamic_message_handler(plugin, event)
        ]

        self.assertEqual(results, [])
        self.assertEqual(event.message_str, "赞我")
        self.assertEqual(event.message_obj.message_str, "/赞我")
        self.assertEqual(event.bot.likes, [(20001, 10)])
        plugin.context.get_config.assert_called_with(event.unified_msg_origin)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)
        request = SimpleNamespace(extra_user_content_parts=[])
        await HelperToolsPlugin.qq_like_persona_context_handler(plugin, event, request)

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertIsInstance(part, TextPart)
        self.assertIn("QQ 名片点赞动作已经由插件完成", part.text)
        self.assertTrue(getattr(part, "_no_save", False))
        persisted = dump_messages_with_checkpoints(
            [Message(role="user", content=request.extra_user_content_parts)]
        )
        self.assertEqual(persisted[0]["content"], [])
        self.assertEqual(like_service.take_persona_context(event), "")

    async def test_dynamic_entry_rejects_bare_other_prefix_empty_config_and_other_bot(
        self,
    ) -> None:
        cases = [
            (FakeEvent("赞我"), ["/"]),
            (FakeEvent("赞我", group_id="123"), ["/"]),
            (FakeEvent("!赞我", group_id="123"), ["/"]),
            (FakeEvent("/赞我", group_id="123"), []),
            (
                FakeEvent(
                    "赞我",
                    group_id="123",
                    messages=[Comp.At(qq="10001"), Comp.Plain("赞我")],
                ),
                ["/"],
            ),
            (
                FakeEvent(
                    "赞我",
                    group_id="123",
                    messages=[
                        Comp.Reply(id="previous", message_str="/赞我"),
                        Comp.Plain("赞我"),
                    ],
                ),
                ["/"],
            ),
            (
                FakeEvent(
                    "/赞我",
                    group_id="123",
                    messages=[
                        Comp.Reply(id="previous"),
                        Comp.Plain(" "),
                        Comp.At(qq="10002"),
                        Comp.Plain("/赞我"),
                    ],
                ),
                ["/"],
            ),
        ]
        for event, prefixes in cases:
            with self.subTest(
                original=event.message_obj.message_str,
                group=event.group_id,
                prefixes=prefixes,
            ):
                plugin = self.make_plugin(prefixes)
                plugin.qq_like.handle_message = AsyncMock(
                    wraps=plugin.qq_like.handle_message
                )
                results = [
                    item
                    async for item in HelperToolsPlugin.dynamic_message_handler(
                        plugin, event
                    )
                ]
                self.assertEqual(results, [])
                self.assertEqual(event.bot.likes, [])
                self.assertEqual(plugin.qq_like.take_persona_context(event), "")
                plugin.qq_like.handle_message.assert_not_awaited()
                plugin.wallpaper.handle_message.assert_not_awaited()
                plugin.steam.match_message.assert_called_once_with(
                    "赞我", wake_triggered=False, allow_commands=False
                )
                plugin.voice.should_handle_message.assert_called_once_with(
                    "赞我", wake_triggered=False, allow_commands=False
                )

    async def test_dynamic_entry_uses_session_profile_prefix_for_like_persona(
        self,
    ) -> None:
        plugin = self.make_plugin()
        plugin.context.get_config = Mock(
            side_effect=lambda origin=None: {"wake_prefix": ["!"] if origin else ["/"]}
        )
        event = FakeEvent("!赞我", group_id="123")
        results = [
            item
            async for item in HelperToolsPlugin.dynamic_message_handler(plugin, event)
        ]
        self.assertEqual(results, [])
        self.assertEqual(event.bot.likes, [(20001, 10)])
        self.assertTrue(event.is_at_or_wake_command)
        plugin.context.get_config.assert_called_with(event.unified_msg_origin)


if __name__ == "__main__":
    unittest.main()
