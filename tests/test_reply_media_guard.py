from __future__ import annotations

import unittest

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.reply_media_guard import (
    BOT_REPLY_IMAGE_MARKER,
    ReplyMediaGuard,
)


class DummyMessage:
    def __init__(self, chain: list[object]) -> None:
        self.message = chain


class DummyEvent:
    def __init__(
        self,
        chain: list[object],
        self_id: str = "10001",
        bot: object | None = None,
    ) -> None:
        self.message_obj = DummyMessage(chain)
        self._self_id = self_id
        self.bot = bot

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_self_id(self) -> str:
        return self._self_id

    def get_platform_id(self) -> str:
        return "qq-test"


class FakeBot:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0

    async def get_msg(self, *, message_id: int | str) -> dict[str, object]:
        self.calls += 1
        return self.payload


class FakeActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed（LLOneBot 失败时抛出的异常）。"""

    def __init__(self, retcode: int, message: str) -> None:
        super().__init__(f"retcode={retcode} message={message}")
        self.retcode = retcode
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": message,
        }


LLONEBOT_VERSION_INFO = {
    "status": "ok",
    "retcode": 0,
    "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
}


class ExpiredCacheBot:
    """LLOneBot 的 msgCacheExpire 默认 120 秒，过期后 get_msg 直接抛异常。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get_version_info(self) -> dict[str, object]:
        return LLONEBOT_VERSION_INFO

    async def get_msg(self, **_params: object) -> dict[str, object]:
        self.calls += 1
        raise FakeActionFailed(1200, "消息不存在或已过期")


class LLOneBotStrictIdBot:
    """LLOneBot：message_id 只接受整数，字符串会被判为参数错误。"""

    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []

    async def get_version_info(self) -> dict[str, object]:
        return LLONEBOT_VERSION_INFO

    async def get_msg(self, **params: object) -> dict[str, object]:
        self.received.append(dict(params))
        if not isinstance(params.get("message_id"), int):
            raise FakeActionFailed(1400, "message_id 参数错误")
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "sender": {"user_id": "10001"},
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "file": "bot.jpg",
                            "url": "https://example.com/bot.jpg",
                            "file_name": "bot.jpg",
                        },
                    }
                ],
            },
        }


class ReplyMediaGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 清空实现探测与变体命中缓存，避免用例之间互相串味。
        reset_compat_caches()

    def tearDown(self) -> None:
        reset_compat_caches()

    async def test_marks_a_bot_authored_quote_without_removing_images(self) -> None:
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

        result = await ReplyMediaGuard(
            {"reply_media_guard": {"enabled": True}}
        ).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 1)
        self.assertEqual(result.marked_image_count, 1)
        self.assertIs(event.get_messages()[0], direct_image)
        self.assertEqual(bot_reply.id, "123")
        self.assertTrue(any(isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER for item in bot_reply.chain or []))
        self.assertTrue(any(isinstance(item, Comp.Image) for item in bot_reply.chain or []))
        self.assertEqual(user_reply.id, "456")
        self.assertTrue(any(isinstance(item, Comp.Image) for item in user_reply.chain or []))

    async def test_can_be_disabled(self) -> None:
        bot_reply = Comp.Reply(
            id="123",
            sender_id="10001",
            chain=[Comp.Image.fromURL("https://example.com/bot.png")],
        )
        event = DummyEvent([bot_reply])

        result = await ReplyMediaGuard(
            {"reply_media_guard": {"enabled": False}}
        ).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 0)
        self.assertEqual(bot_reply.id, "123")
        self.assertTrue(any(isinstance(item, Comp.Image) for item in bot_reply.chain or []))

    async def test_looks_up_bare_quote_from_another_plugin(self) -> None:
        bot = FakeBot(
            {
                "data": {
                    "sender": {"user_id": "10001"},
                    "message": [{"type": "image", "data": {"file": "bot.jpg"}}],
                }
            }
        )
        bare_quote = Comp.Reply(id="789", sender_id=0, chain=[])
        event = DummyEvent([bare_quote], bot=bot)

        result = await ReplyMediaGuard(
            {"reply_media_guard": {"enabled": True}}
        ).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 1)
        self.assertEqual(result.marked_image_count, 1)
        self.assertEqual(bot.calls, 1)
        self.assertTrue(
            any(
                isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER
                for item in bare_quote.chain or []
            )
        )

    async def test_authoritative_lookup_overrides_inline_sender_and_result_wrapper(self) -> None:
        bot = FakeBot(
            {
                "result": {
                    "sender": {"userId": "qq:10001"},
                    "message": [{"type": "image", "data": {"file": "bot.jpg"}}],
                }
            }
        )
        # Some adapters fill the inline Reply sender from the current message
        # or leave stale metadata when another plugin sent the quoted image.
        reply = Comp.Reply(
            id="791",
            sender_id="20002",
            chain=[Comp.Image.fromURL("https://example.com/bot.png")],
        )
        event = DummyEvent([reply], bot=bot)

        result = await ReplyMediaGuard(
            {"reply_media_guard": {"enabled": True}}
        ).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 1)
        self.assertEqual(result.marked_image_count, 1)
        self.assertEqual(bot.calls, 1)
        self.assertTrue(
            any(
                isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER
                for item in reply.chain or []
            )
        )

    async def test_quote_lookup_is_cached_and_non_bot_quotes_are_not_marked(self) -> None:
        bot = FakeBot(
            {
                "sender": {"user_id": "20002"},
                "message": [{"type": "image", "data": {"file": "user.jpg"}}],
            }
        )
        bare_quote = Comp.Reply(id="790", sender_id=0, chain=[])
        event = DummyEvent([bare_quote], bot=bot)
        guard = ReplyMediaGuard({"reply_media_guard": {"enabled": True}})

        first = await guard.mark_bot_reply_images(event)
        second = await guard.mark_bot_reply_images(event)

        self.assertEqual(first.marked_reply_count, 0)
        self.assertEqual(second.marked_reply_count, 0)
        self.assertEqual(bot.calls, 1)
        self.assertFalse(
            any(
                isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER
                for item in bare_quote.chain or []
            )
        )

    async def test_expired_quote_lookup_degrades_silently(self) -> None:
        bot = ExpiredCacheBot()
        bare_quote = Comp.Reply(id="789", sender_id=0, chain=[])
        event = DummyEvent([bare_quote], bot=bot)

        result = await ReplyMediaGuard(
            {"reply_media_guard": {"enabled": True}}
        ).mark_bot_reply_images(event)

        # 消息缓存过期属于常态，不能标记、不能抛错、也不该给用户刷错误。
        self.assertEqual(result.marked_reply_count, 0)
        self.assertEqual(result.marked_image_count, 0)
        self.assertEqual(bot.calls, 1)
        self.assertFalse(
            any(
                isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER
                for item in bare_quote.chain or []
            )
        )

    async def test_llonebot_integer_message_id_is_used_first(self) -> None:
        bot = LLOneBotStrictIdBot()
        bare_quote = Comp.Reply(id="789", sender_id=0, chain=[])
        event = DummyEvent([bare_quote], bot=bot)

        result = await ReplyMediaGuard(
            {"reply_media_guard": {"enabled": True}}
        ).mark_bot_reply_images(event)

        self.assertEqual(result.marked_reply_count, 1)
        self.assertEqual(result.marked_image_count, 1)
        self.assertEqual(bot.received, [{"message_id": 789}])
        self.assertTrue(
            any(
                isinstance(item, Comp.Plain) and item.text == BOT_REPLY_IMAGE_MARKER
                for item in bare_quote.chain or []
            )
        )


if __name__ == "__main__":
    unittest.main()
