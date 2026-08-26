from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.chat_history_service import (
    ChatHistoryError,
    ChatHistoryService,
)
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches


class FakeActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed：错误细节都在 .result 里。"""

    def __init__(self, retcode: int, message: str) -> None:
        super().__init__(f"ActionFailed(retcode={retcode})")
        self.retcode = retcode
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": message,
        }


class ProtocolHistoryBot:
    """只通过 call_action 暴露 action 的假协议端，可指定接受哪个排序参数。"""

    def __init__(
        self,
        pages: list[dict[str, object]],
        *,
        app_name: str = "LLOneBot",
        order_param: str | None = "reverseOrder",
    ) -> None:
        self.pages = list(pages)
        self.app_name = app_name
        self.order_param = order_param
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def calls_of(self, action: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == action]

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action == "get_version_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"app_name": self.app_name, "app_version": "8.1.9"},
            }
        if action != "get_group_msg_history":
            raise FakeActionFailed(1404, f"{action} API 不存在")
        allowed = {"group_id", "message_seq", "count"}
        if self.order_param:
            allowed.add(self.order_param)
        unexpected = sorted(set(params) - allowed)
        if unexpected:
            raise FakeActionFailed(1400, f"参数错误: {unexpected}")
        page = self.pages.pop(0) if self.pages else {"messages": []}
        return {"status": "ok", "retcode": 0, "data": page}


class HistoryBot:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    async def get_group_msg_history(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.pages.pop(0) if self.pages else {"data": {"messages": []}}


class HistoryEvent:
    def __init__(
        self,
        *,
        group_id: str = "10000",
        platform: str = "aiocqhttp",
        sender_id: str = "20001",
        sender_name: str = "小明",
        text: str = "",
        message_id: str = "1",
        timestamp: int | None = None,
        bot: object | None = None,
    ) -> None:
        self._group_id = group_id
        self._platform = platform
        self._sender_id = sender_id
        self._text = text
        self.bot = bot
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            message_seq=int(message_id) if message_id.isdigit() else None,
            timestamp=timestamp or int(time.time()),
            sender=SimpleNamespace(card=sender_name, nickname=sender_name),
        )

    def get_platform_name(self) -> str:
        return self._platform

    def get_group_id(self) -> str:
        return self._group_id

    def get_self_id(self) -> str:
        return "10001"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_messages(self) -> list[object]:
        return [Comp.Plain(self._text)]


class ChatHistoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_and_searches_only_the_current_group(self) -> None:
        config = {
            "chat_history": {
                "enabled": True,
                "onebot_backfill_enabled": False,
                "include_sender_qq": True,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(config, Path(directory))
            try:
                current = HistoryEvent(
                    group_id="10000",
                    text="今天讨论 AstrBot 的插件方案",
                    message_id="10",
                )
                other_group = HistoryEvent(
                    group_id="20000",
                    text="AstrBot 不该出现在当前查询",
                    message_id="11",
                )
                await service.capture_event(current)
                await service.capture_event(other_group)

                result = await service.search(current, query="AstrBot", hours=24, limit=20)

                self.assertEqual(result.total_count, 1)
                self.assertEqual(len(result.messages), 1)
                self.assertEqual(result.messages[0].group_id, "10000")
                rendered = result.render_for_model(
                    timezone=service.timezone(),
                    max_chars=4_000,
                    include_sender_qq=True,
                )
                self.assertIn("不可信用户内容", rendered)
                self.assertIn("QQ 20001", rendered)
            finally:
                service.close()

    async def test_bounds_and_sql_like_keywords_are_safe(self) -> None:
        config = {
            "chat_history": {
                "enabled": True,
                "onebot_backfill_enabled": False,
                "max_query_days": 1,
                "max_result_messages": 2,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(config, Path(directory))
            try:
                now = int(time.time())
                for index in range(4):
                    await service.capture_event(
                        HistoryEvent(
                            text=f"needle {index}",
                            message_id=str(20 + index),
                            timestamp=now - index,
                        )
                    )
                result = await service.search(
                    HistoryEvent(message_id="99"),
                    query="needle",
                    start=now - 10 * 86_400,
                    end=now,
                    limit=999,
                )
                injected = await service.search(
                    HistoryEvent(message_id="100"),
                    query="%' OR 1=1 --",
                    hours=1,
                )

                self.assertTrue(result.range_capped)
                self.assertEqual(len(result.messages), 2)
                self.assertEqual(injected.total_count, 0)
            finally:
                service.close()

    @staticmethod
    def _history_page(now: int) -> dict[str, object]:
        return {
            "messages": [
                {
                    "message_id": "77",
                    "message_seq": 77,
                    "time": now - 300,
                    "sender": {"user_id": "20002", "nickname": "小红"},
                    "message": [{"type": "text", "data": {"text": "旧记录"}}],
                }
            ]
        }

    async def test_llonebot_backfill_uses_camel_case_reverse_order(self) -> None:
        reset_compat_caches()
        now = int(time.time())
        bot = ProtocolHistoryBot([self._history_page(now)])
        config = {
            "chat_history": {
                "enabled": True,
                "max_backfill_pages": 1,
                "backfill_delay_milliseconds": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(config, Path(directory))
            try:
                event = HistoryEvent(text="现在", message_id="88", bot=bot)
                result = await service.search(event, query="旧记录", hours=1)

                self.assertEqual(result.backfill.inserted_count, 1)
                history_calls = bot.calls_of("get_group_msg_history")
                self.assertEqual(
                    history_calls[0],
                    {"group_id": 10000, "message_seq": 0, "reverseOrder": True},
                )
                self.assertEqual(result.messages[0].sender_name, "小红")
            finally:
                service.close()

    async def test_backfill_downgrades_when_order_param_is_rejected(self) -> None:
        reset_compat_caches()
        now = int(time.time())
        bot = ProtocolHistoryBot(
            [self._history_page(now)], app_name="go-cqhttp", order_param=None
        )
        config = {
            "chat_history": {
                "enabled": True,
                "max_backfill_pages": 1,
                "backfill_delay_milliseconds": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(config, Path(directory))
            try:
                event = HistoryEvent(text="现在", message_id="88", bot=bot)
                result = await service.search(event, query="旧记录", hours=1)

                self.assertEqual(result.backfill.inserted_count, 1)
                history_calls = bot.calls_of("get_group_msg_history")
                self.assertEqual(
                    [sorted(params) for params in history_calls],
                    [
                        ["group_id", "message_seq", "reverseOrder"],
                        ["group_id", "message_seq", "reverse_order"],
                        ["group_id", "message_seq"],
                    ],
                )
            finally:
                service.close()

    async def test_other_onebot_platforms_are_accepted(self) -> None:
        reset_compat_caches()
        config = {"chat_history": {"enabled": True, "onebot_backfill_enabled": False}}
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(config, Path(directory))
            try:
                settings = service.settings()
                for platform in ("aiocqhttp", "LLOneBot", "napcat", "lagrange", "go-cqhttp"):
                    with self.subTest(platform=platform):
                        scope = service._scope_for_event(
                            HistoryEvent(platform=platform), settings
                        )
                        self.assertIsNotNone(scope)
                self.assertIsNone(
                    service._scope_for_event(HistoryEvent(platform="telegram"), settings)
                )
            finally:
                service.close()

    async def test_backfill_is_bounded_and_private_or_non_qq_events_are_denied(self) -> None:
        reset_compat_caches()
        now = int(time.time())
        bot = HistoryBot(
            [
                {
                    "data": {
                        "messages": [
                            {
                                "message_id": "77",
                                "message_seq": 77,
                                "time": now - 300,
                                "sender": {"user_id": "20002", "nickname": "小红"},
                                "message": [{"type": "text", "data": {"text": "旧记录"}}],
                            }
                        ]
                    }
                }
            ]
        )
        config = {
            "chat_history": {
                "enabled": True,
                "max_backfill_pages": 2,
                "backfill_delay_milliseconds": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(config, Path(directory))
            try:
                event = HistoryEvent(text="现在", message_id="88", bot=bot)
                result = await service.search(event, query="旧记录", hours=1)

                self.assertTrue(result.backfill.attempted)
                self.assertEqual(result.backfill.inserted_count, 1)
                self.assertEqual(bot.calls[0]["group_id"], 10000)
                self.assertEqual(bot.calls[0]["message_seq"], 0)
                self.assertEqual(result.messages[0].sender_name, "小红")
                with self.assertRaises(ChatHistoryError):
                    await service.search(HistoryEvent(group_id="", text="私聊"), hours=1)
                with self.assertRaises(ChatHistoryError):
                    await service.search(
                        HistoryEvent(platform="telegram", text="非 QQ"), hours=1
                    )
            finally:
                service.close()


class SequenceExtractionTests(unittest.TestCase):
    """锁住历史消息序号提取逻辑：序号 0 是合法值，不能被当成缺失。"""

    def test_sequence_keys_order_is_stable(self) -> None:
        """键名优先级顺序是协议兼容契约的一部分，写死避免被误改。"""
        self.assertEqual(
            ChatHistoryService._SEQUENCE_KEYS,
            ("message_seq", "messageSeq", "real_seq", "realSeq", "seq"),
        )

    def test_every_supported_key_is_recognized(self) -> None:
        """五个键名单独出现时都能取到序号。"""
        for key in ChatHistoryService._SEQUENCE_KEYS:
            with self.subTest(key=key):
                self.assertEqual(ChatHistoryService._message_sequence({key: 42}), 42)

    def test_keys_follow_declared_priority(self) -> None:
        """同时存在多个键名时，按 _SEQUENCE_KEYS 顺序取第一个可用值。"""
        item = {
            "message_seq": 1,
            "messageSeq": 2,
            "real_seq": 3,
            "realSeq": 4,
            "seq": 5,
        }
        for index, key in enumerate(ChatHistoryService._SEQUENCE_KEYS, start=1):
            with self.subTest(first_key=key):
                self.assertEqual(ChatHistoryService._message_sequence(item), index)
                item.pop(key)
        self.assertIsNone(ChatHistoryService._message_sequence(item))

    def test_unusable_value_falls_through_to_next_key(self) -> None:
        """高优先级键的值不可用时，继续尝试后面的键名。"""
        self.assertEqual(
            ChatHistoryService._message_sequence(
                {"message_seq": None, "messageSeq": "", "real_seq": "12345"}
            ),
            12345,
        )

    def test_string_digits_are_converted_to_int(self) -> None:
        """部分协议端（LLOneBot / NapCat）给的是字符串序号。"""
        self.assertEqual(
            ChatHistoryService._message_sequence({"real_seq": "12345"}), 12345
        )
        for raw in ("0", 0):
            with self.subTest(raw=raw):
                value = ChatHistoryService._message_sequence({"real_seq": raw})
                self.assertEqual(value, 0)
                self.assertIsInstance(value, int)

    def test_zero_sequence_is_not_treated_as_missing(self) -> None:
        """回归用例：旧实现用 `x or y` 取值，会把 seq == 0 误判成缺失。"""
        for raw in (0, "0"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    ChatHistoryService._message_sequence({"message_seq": raw}), 0
                )
        self.assertEqual(
            ChatHistoryService._message_sequence({"message_seq": 0, "seq": 99}), 0
        )
        self.assertEqual(
            ChatHistoryService._oldest_sequence([{"message_seq": 5}, {"seq": 0}]), 0
        )

    def test_missing_and_invalid_values_return_none(self) -> None:
        """None、空串、非数字与负数序号都视为缺失。"""
        cases: list[Any] = [
            {},
            {"message_seq": None},
            {"message_seq": ""},
            {"message_seq": "   "},
            {"message_seq": "abc"},
            {"message_seq": "12.5"},
            {"message_seq": -1},
            {"other_seq": 7},
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsNone(ChatHistoryService._message_sequence(item))

    def test_non_mapping_items_return_none(self) -> None:
        """不是映射的条目直接视为缺失，不做属性回退。"""
        cases: list[Any] = [
            None,
            "12345",
            12345,
            ["message_seq", 1],
            ("message_seq", 1),
            SimpleNamespace(message_seq=1),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsNone(ChatHistoryService._message_sequence(item))

    def test_oldest_sequence_returns_minimum(self) -> None:
        """多条消息里取最小序号，与顺序无关。"""
        messages: list[Any] = [
            {"message_seq": 300},
            {"real_seq": "120"},
            {"seq": 200},
        ]
        self.assertEqual(ChatHistoryService._oldest_sequence(messages), 120)

    def test_oldest_sequence_skips_missing_items(self) -> None:
        """部分条目缺失序号时忽略它们，仍能得到最小值。"""
        messages: list[Any] = [
            "not a mapping",
            {"message_seq": None},
            {"message_seq": 88},
            {"seq": "bad"},
            {"real_seq": "77"},
        ]
        self.assertEqual(ChatHistoryService._oldest_sequence(messages), 77)

    def test_oldest_sequence_returns_none_when_unavailable(self) -> None:
        """空列表或全部缺失时返回 None，交给调用方决定兜底。"""
        self.assertIsNone(ChatHistoryService._oldest_sequence([]))
        self.assertIsNone(
            ChatHistoryService._oldest_sequence(
                [{}, {"message_seq": "abc"}, None, {"seq": -5}]
            )
        )


if __name__ == "__main__":
    unittest.main()
