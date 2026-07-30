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


if __name__ == "__main__":
    unittest.main()
