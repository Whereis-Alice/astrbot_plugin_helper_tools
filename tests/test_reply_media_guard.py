from __future__ import annotations

import tempfile
import time
import unittest

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.reply_media_guard import (
    BOT_REPLY_IMAGE_MARKER,
    ReplyMediaGuard,
)


class DummyMessage:
    def __init__(self, chain: list[object], message_id: str = "") -> None:
        self.message = chain
        self.message_id = message_id


class DummyEvent:
    def __init__(
        self,
        chain: list[object],
        self_id: str = "10001",
        bot: object | None = None,
        sender_id: str | None = None,
        message_id: str = "",
    ) -> None:
        self.message_obj = DummyMessage(chain, message_id)
        self._self_id = self_id
        # 默认把事件视为 bot 自己发出的（sender == self），方便自身图片台账用例；
        # 现有引用用例不读取 get_sender_id，因此不受影响。
        self._sender_id = sender_id if sender_id is not None else self_id
        self.bot = bot

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_self_id(self) -> str:
        return self._self_id

    def get_sender_id(self) -> str:
        return self._sender_id

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


    async def test_self_ledger_marks_quote_after_cache_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            config = {"reply_media_guard": {"enabled": True}}
            guard = ReplyMediaGuard(config, data_dir)
            # bot 先发出一条含图消息（message_id=900），记入自身图片台账。
            sent = DummyEvent(
                [Comp.Image.fromURL("https://example.com/bot.png")],
                message_id="900",
            )
            self.assertEqual(guard.remember_self_authored_images(sent), 1)
            # 用户稍后引用这张图，此时 LLOneBot 缓存已过期，get_msg 会抛异常，
            # 但靠台账仍能判定为 bot 自己的图。
            bare_quote = Comp.Reply(id="900", sender_id=0, chain=[])
            quote_event = DummyEvent(
                [bare_quote], bot=ExpiredCacheBot(), sender_id="20002"
            )
            result = await guard.mark_bot_reply_images(quote_event)
            self.assertEqual(result.marked_reply_count, 1)
            self.assertEqual(result.marked_image_count, 1)
            self.assertTrue(
                any(
                    isinstance(item, Comp.Plain)
                    and item.text == BOT_REPLY_IMAGE_MARKER
                    for item in bare_quote.chain or []
                )
            )

    async def test_self_ledger_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            config = {"reply_media_guard": {"enabled": True}}
            sent = DummyEvent(
                [Comp.Image.fromURL("https://example.com/bot.png")],
                message_id="901",
            )
            ReplyMediaGuard(config, data_dir).remember_self_authored_images(sent)
            # 新实例模拟插件重启：从磁盘加载台账后仍能命中。
            reborn = ReplyMediaGuard(config, data_dir)
            bare_quote = Comp.Reply(id="901", sender_id=0, chain=[])
            quote_event = DummyEvent([bare_quote], bot=ExpiredCacheBot())
            result = await reborn.mark_bot_reply_images(quote_event)
            self.assertEqual(result.marked_reply_count, 1)
            self.assertEqual(result.marked_image_count, 1)

    async def test_confirmed_bot_quote_is_written_back_to_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            config = {"reply_media_guard": {"enabled": True}}
            # 第一次：get_msg 成功确认这是 bot 的图（message_id=902）并写回台账。
            first_quote = Comp.Reply(id="902", sender_id=0, chain=[])
            first_event = DummyEvent([first_quote], bot=LLOneBotStrictIdBot())
            first = await ReplyMediaGuard(
                config, data_dir
            ).mark_bot_reply_images(first_event)
            self.assertEqual(first.marked_reply_count, 1)
            # 第二次：换新实例 + 过期的 bot，仅靠写回的台账也能命中。
            second_quote = Comp.Reply(id="902", sender_id=0, chain=[])
            second_event = DummyEvent([second_quote], bot=ExpiredCacheBot())
            second = await ReplyMediaGuard(
                config, data_dir
            ).mark_bot_reply_images(second_event)
            self.assertEqual(second.marked_reply_count, 1)
            self.assertEqual(second.marked_image_count, 1)

    async def test_remember_ignores_non_self_events(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            guard = ReplyMediaGuard({"reply_media_guard": {"enabled": True}}, data_dir)
            user_event = DummyEvent(
                [Comp.Image.fromURL("https://example.com/user.png")],
                self_id="10001",
                sender_id="20002",
                message_id="903",
            )
            self.assertEqual(guard.remember_self_authored_images(user_event), 0)
            bare_quote = Comp.Reply(id="903", sender_id=0, chain=[])
            quote_event = DummyEvent([bare_quote], bot=ExpiredCacheBot())
            result = await guard.mark_bot_reply_images(quote_event)
            self.assertEqual(result.marked_reply_count, 0)

    async def test_self_ledger_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            config = {
                "reply_media_guard": {"enabled": True, "self_ledger_enabled": False}
            }
            guard = ReplyMediaGuard(config, data_dir)
            sent = DummyEvent(
                [Comp.Image.fromURL("https://example.com/bot.png")],
                message_id="904",
            )
            self.assertEqual(guard.remember_self_authored_images(sent), 0)
            bare_quote = Comp.Reply(id="904", sender_id=0, chain=[])
            quote_event = DummyEvent([bare_quote], bot=ExpiredCacheBot())
            result = await guard.mark_bot_reply_images(quote_event)
            self.assertEqual(result.marked_reply_count, 0)

    async def test_expired_self_ledger_entry_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            config = {"reply_media_guard": {"enabled": True}}
            guard = ReplyMediaGuard(config, data_dir)
            sent = DummyEvent(
                [Comp.Image.fromURL("https://example.com/bot.png")],
                message_id="905",
            )
            guard.remember_self_authored_images(sent)
            # 手动把记录时间推到很久以前，使其超过 TTL。
            key = guard._cache_key(sent, "905")
            guard._self_image_ledger[key] = (time.time() - 10**6, 1)
            bare_quote = Comp.Reply(id="905", sender_id=0, chain=[])
            quote_event = DummyEvent([bare_quote], bot=ExpiredCacheBot())
            result = await guard.mark_bot_reply_images(quote_event)
            self.assertEqual(result.marked_reply_count, 0)


if __name__ == "__main__":
    unittest.main()
