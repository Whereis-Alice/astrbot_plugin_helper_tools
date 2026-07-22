from __future__ import annotations

import unittest

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.reply_media_guard import (
    BOT_REPLY_IMAGE_MARKER,
    ReplyMediaGuard,
)


class DummyMessage:
    def __init__(self, chain: list[object]) -> None:
        self.message = chain


class DummyEvent:
    def __init__(self, chain: list[object], self_id: str = "10001") -> None:
        self.message_obj = DummyMessage(chain)
        self._self_id = self_id

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_self_id(self) -> str:
        return self._self_id


class ReplyMediaGuardTests(unittest.TestCase):
    def test_marks_a_bot_authored_quote_without_removing_images(self) -> None:
        direct_image = Comp.Image.fromURL("https://example.com/user.png")
        bot_reply = Comp.Reply(
            id="123",
            sender_id="10001",
            chain=[
                Comp.Plain("这是一段 bot 文字"),
                Comp.Image.fromURL("https://example.com/bot.png"),
            ],
        )
        user_reply = Comp.Reply(
            id="456",
            sender_id="20002",
            chain=[Comp.Image.fromURL("https://example.com/other-user.png")],
        )
        event = DummyEvent([direct_image, bot_reply, user_reply])

        result = ReplyMediaGuard({"reply_media_guard": {"enabled": True}}).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 1)
        self.assertEqual(result.marked_image_count, 1)
        self.assertIs(event.get_messages()[0], direct_image)
        self.assertEqual(bot_reply.id, "123")
        self.assertTrue(any(isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER for item in bot_reply.chain or []))
        self.assertTrue(any(isinstance(item, Comp.Image) for item in bot_reply.chain or []))
        self.assertEqual(user_reply.id, "456")
        self.assertTrue(any(isinstance(item, Comp.Image) for item in user_reply.chain or []))

    def test_can_be_disabled(self) -> None:
        bot_reply = Comp.Reply(
            id="123",
            sender_id="10001",
            chain=[Comp.Image.fromURL("https://example.com/bot.png")],
        )
        event = DummyEvent([bot_reply])

        result = ReplyMediaGuard({"reply_media_guard": {"enabled": False}}).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 0)
        self.assertEqual(bot_reply.id, "123")
        self.assertTrue(any(isinstance(item, Comp.Image) for item in bot_reply.chain or []))
