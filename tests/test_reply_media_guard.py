from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

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

    async def test_outgoing_receipt_restores_first_quote_after_protocol_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            guard = ReplyMediaGuard({}, tmpdir)
            await guard.remember_outgoing_images("qq-test", "10001", "old-receipt", [
                {"url": "https://example.com/magpie-embedded.png"},
                {"url": "https://example.com/another-plugin.png"},
            ])
            bot = ExpiredCacheBot()
            reply = Comp.Reply(id="old-receipt", sender_id="20002", chain=[])
            event = DummyEvent([reply], bot=bot, sender_id="20002")
            restored = ReplyMediaGuard({}, tmpdir)
            result = await restored.mark_bot_reply_images(event)
            self.assertEqual(result.marked_image_count, 2)
            self.assertEqual(bot.calls, 0)
            images = [part for part in reply.chain if isinstance(part, Comp.Image)]
            self.assertEqual(len(images), 2)
            self.assertTrue(all(part._helper_tools_quote_is_bot for part in images))
            self.assertTrue(all(part._helper_tools_quote_sender_id == "10001" for part in images))
            # Provenance enrichment must not rewrite addressing/wake metadata.
            self.assertEqual(str(reply.sender_id), "20002")
            self.assertEqual(event.get_sender_id(), "20002")

    async def test_temporary_outgoing_image_is_snapshotted_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary = Path(tmpdir) / "temporary.png"
            raw = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ1sAAAAASUVORK5CYII=")
            temporary.write_bytes(raw)
            guard = ReplyMediaGuard({}, tmpdir)
            await guard.remember_outgoing_images("qq-test", "10001", "temp-receipt", [{"file": temporary.as_uri()}])
            temporary.unlink()
            reply = Comp.Reply(id="temp-receipt", chain=[])
            result = await ReplyMediaGuard({}, tmpdir).mark_bot_reply_images(DummyEvent([reply]))
            self.assertEqual(result.marked_image_count, 1)
            image = next(part for part in reply.chain if isinstance(part, Comp.Image))
            self.assertEqual(Path(image.path).read_bytes(), raw)

    async def test_base64_snapshot_does_not_dump_image_body_into_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = b"persist-only-a-reference"
            encoded = base64.b64encode(raw).decode()
            guard = ReplyMediaGuard({}, tmpdir)
            await guard.remember_outgoing_images("qq-test", "10001", "base64-receipt", [{"file": "base64://" + encoded}])
            ledger = Path(tmpdir) / "reply_media_self_images.json"
            data = json.loads(ledger.read_text())
            self.assertEqual(data["version"], 2)
            self.assertNotIn(encoded, ledger.read_text())
            self.assertEqual(len(list((Path(tmpdir) / "reply_media_images").glob("*.img"))), 1)

    async def test_receipts_are_isolated_by_adapter_and_actual_bot_id(self) -> None:
        guard = ReplyMediaGuard({})
        await guard.remember_outgoing_images("qq-test", "10001", "42", [{"url": "https://example.com/bot.png"}])
        other = Comp.Reply(id="42", chain=[])
        event = DummyEvent([other], self_id="30003")
        result = await guard.mark_bot_reply_images(event)
        self.assertEqual(result.marked_image_count, 0)
        self.assertEqual(other.chain, [])
        event = DummyEvent([Comp.Reply(id="42", chain=[])])
        event.get_platform_id = lambda: "another-adapter"
        result = await guard.mark_bot_reply_images(event)
        self.assertEqual(result.marked_image_count, 0)

    async def test_quoted_user_and_current_upload_keep_distinct_origins(self) -> None:
        uploaded = Comp.Image.fromURL("https://example.com/shared.png")
        user_image = Comp.Image.fromURL("https://example.com/shared.png")
        bot_image = Comp.Image.fromURL("https://example.com/bot.png")
        user_reply = Comp.Reply(id="user-image", sender_id="20002", chain=[user_image])
        bot_reply = Comp.Reply(id="bot-image", sender_id="10001", chain=[bot_image])
        event = DummyEvent([uploaded, user_reply, bot_reply], sender_id="20002")
        result = await ReplyMediaGuard({}).mark_bot_reply_images(event)
        self.assertEqual(result.marked_image_count, 1)
        self.assertFalse(user_image._helper_tools_quote_is_bot)
        self.assertEqual(user_image._helper_tools_quote_sender_id, "20002")
        self.assertTrue(bot_image._helper_tools_quote_is_bot)
        self.assertFalse(hasattr(uploaded, "_helper_tools_quote_sender_id"))

    async def test_get_msg_restores_actual_image_components_and_origin(self) -> None:
        bot = FakeBot({"data": {"sender": {"user_id": "10001"}, "message": [
            {"type": "text", "data": {"text": "嵌入消息的表情包"}},
            {"type": "image", "data": {"url": "https://example.com/embedded.png", "file": "opaque-file-id"}},
        ]}})
        reply = Comp.Reply(id="embedded", chain=[])
        result = await ReplyMediaGuard({}).mark_bot_reply_images(DummyEvent([reply], bot=bot))
        self.assertEqual(result.marked_image_count, 1)
        image = next(part for part in reply.chain if isinstance(part, Comp.Image))
        self.assertEqual(image.url, "https://example.com/embedded.png")
        self.assertTrue(image._helper_tools_quote_is_bot)

    async def test_disabled_receipt_ledger_performs_no_disk_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            guard = ReplyMediaGuard({"reply_media_guard": {"self_ledger_enabled": False}}, tmpdir)
            await guard.remember_outgoing_images("qq-test", "10001", "disabled", [{"url": "https://example.com/bot.png"}])
            self.assertFalse(list(Path(tmpdir).iterdir()))

    async def test_legacy_count_only_ledger_still_confirms_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "reply_media_self_images.json").write_text(json.dumps({
                "version": 1, "entries": {"qq-test:10001:legacy": {"ts": time.time(), "images": 1}},
            }))
            reply = Comp.Reply(id="legacy", chain=[])
            result = await ReplyMediaGuard({}, tmpdir).mark_bot_reply_images(DummyEvent([reply]))
            self.assertEqual(result.marked_image_count, 1)
            self.assertTrue(reply._helper_tools_quote_is_bot)
            self.assertEqual(reply._helper_tools_quote_image_count, 1)

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
