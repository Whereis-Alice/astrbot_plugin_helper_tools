from __future__ import annotations

import unittest
from types import SimpleNamespace

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import Message, TextPart

from astrbot_plugin_helper_tools.chat_history_service import (
    CHAT_HISTORY_TOOL_RESULT_MARKER,
)
from astrbot_plugin_helper_tools.main import (
    HelperToolsPlugin,
    _mark_temporary_tool_results,
)
from astrbot_plugin_helper_tools.onebot_compat import (
    is_onebot_platform,
    register_extra_platform_names,
)
from astrbot_plugin_helper_tools.reply_media_guard import (
    BOT_REPLY_IMAGE_MARKER,
    ReplyMediaGuard,
)


class ReplyBot:
    async def get_msg(self, *, message_id: int | str) -> dict[str, object]:
        return {
            "data": {
                "sender": {"user_id": "10001"},
                "message": [{"type": "image", "data": {"file": "bot.jpg"}}],
            }
        }


class ReplyEvent:
    def __init__(self) -> None:
        self.bot = ReplyBot()
        self.message_obj = SimpleNamespace(
            message=[Comp.Reply(id="321", sender_id=0, chain=[])]
        )

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_self_id(self) -> str:
        return "10001"

    def get_platform_id(self) -> str:
        return "qq-test"


class GuardEvent:
    def __init__(self) -> None:
        self.stopped = False

    def plain_result(self, text: str):
        return ("plain", text)

    def stop_event(self) -> None:
        self.stopped = True


class MainContextGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_request_guard_labels_quoted_bot_image_as_temporary(self) -> None:
        plugin = SimpleNamespace(
            enabled=lambda: True,
            reply_media_guard=ReplyMediaGuard({"reply_media_guard": {"enabled": True}}),
        )
        event = ReplyEvent()
        request = SimpleNamespace(prompt="用户的问题", extra_user_content_parts=[])

        await HelperToolsPlugin.reply_media_llm_request_context_handler(
            plugin,
            event,
            request,
        )

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertIsInstance(part, TextPart)
        self.assertEqual(part.text, BOT_REPLY_IMAGE_MARKER)
        self.assertTrue(getattr(part, "_no_save", False))
        self.assertTrue(
            any(
                isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER
                for item in event.get_messages()[0].chain
            )
        )

    async def test_late_request_guard_uses_system_prompt_for_real_provider_requests(self) -> None:
        plugin = SimpleNamespace(
            enabled=lambda: True,
            reply_media_guard=ReplyMediaGuard({"reply_media_guard": {"enabled": True}}),
        )
        event = ReplyEvent()
        request = SimpleNamespace(
            prompt="用户的问题",
            system_prompt="已有的人格提示",
            extra_user_content_parts=[],
        )

        await HelperToolsPlugin.reply_media_llm_request_context_handler(
            plugin,
            event,
            request,
        )

        self.assertIn("已有的人格提示", request.system_prompt)
        self.assertIn(BOT_REPLY_IMAGE_MARKER, request.system_prompt)
        self.assertEqual(request.extra_user_content_parts, [])

    async def test_history_tool_result_is_removed_from_future_context(self) -> None:
        message = Message(
            role="tool",
            content=[TextPart(text=f"{CHAT_HISTORY_TOOL_RESULT_MARKER}\n测试")],
        )
        run_context = SimpleNamespace(messages=[message])

        marked = _mark_temporary_tool_results(run_context)

        self.assertEqual(marked, 1)
        self.assertTrue(getattr(message.content[0], "_no_save", False))
        self.assertTrue(getattr(message, "_no_save", False))


class BotProfileCommandGuardTests(unittest.IsolatedAsyncioTestCase):
    """非 OneBot 平台上服务层仍会抛异常，指令层必须给出中文提示。"""

    @staticmethod
    def _plugin(failure: BaseException) -> SimpleNamespace:
        async def raise_failure(*_args: object, **_kwargs: object) -> str:
            raise failure

        return SimpleNamespace(
            config={"bot_profile": {"enabled": True, "commands_enabled": True}},
            enabled=lambda: True,
            bot_profile=SimpleNamespace(
                set_nickname=raise_failure,
                set_signature=raise_failure,
                set_status=raise_failure,
                set_avatar=raise_failure,
            ),
        )

    async def _run(self, handler, plugin: SimpleNamespace) -> str:
        event = GuardEvent()
        results = [result async for result in handler(plugin, event)]
        self.assertTrue(event.stopped)
        self.assertEqual(len(results), 1)
        kind, text = results[0]
        self.assertEqual(kind, "plain")
        return text

    async def test_each_bot_profile_command_returns_a_friendly_message(self) -> None:
        cases = (
            (HelperToolsPlugin.set_bot_nickname_command, "设置 Bot 昵称失败"),
            (HelperToolsPlugin.set_bot_signature_command, "设置 Bot 签名失败"),
            (HelperToolsPlugin.set_bot_status_command, "设置 Bot 在线状态失败"),
            (HelperToolsPlugin.set_bot_avatar_command, "设置 Bot 头像失败"),
        )
        failure = RuntimeError("当前平台不是 OneBot v11 协议端，无法使用该功能。")
        for handler, prefix in cases:
            with self.subTest(handler=handler.__name__):
                text = await self._run(handler, self._plugin(failure))
                self.assertTrue(text.startswith(prefix), text)
                self.assertIn("当前平台不是 OneBot v11 协议端", text)
                self.assertNotIn("Traceback", text)

    async def test_unsupported_action_failure_mentions_the_protocol_side(self) -> None:
        failure = RuntimeError("API 不存在: set_qq_profile")
        text = await self._run(
            HelperToolsPlugin.set_bot_nickname_command, self._plugin(failure)
        )
        self.assertIn("没有提供可用的接口", text)


class ExtraOneBotPlatformNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(register_extra_platform_names, None)

    @staticmethod
    def _plugin(general: dict[str, object]) -> SimpleNamespace:
        plugin = SimpleNamespace(config={"general": general})
        plugin.onebot_platform_names = lambda: HelperToolsPlugin.onebot_platform_names(
            plugin
        )
        return plugin

    def test_general_config_names_are_registered(self) -> None:
        plugin = self._plugin({"onebot_platform_names": ["My-Adapter", " napcat2 "]})

        names = HelperToolsPlugin._sync_onebot_platform_names(plugin)

        self.assertEqual(names, frozenset({"my-adapter", "napcat2"}))
        self.assertTrue(is_onebot_platform("My-Adapter"))
        self.assertTrue(is_onebot_platform("napcat2"))
        self.assertFalse(is_onebot_platform("telegram"))

    def test_empty_config_clears_previous_registration(self) -> None:
        register_extra_platform_names(["stale-adapter"])
        plugin = self._plugin({})

        names = HelperToolsPlugin._sync_onebot_platform_names(plugin)

        self.assertEqual(names, frozenset())
        self.assertFalse(is_onebot_platform("stale-adapter"))


if __name__ == "__main__":
    unittest.main()
