from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.chat_history_service import (
    ChatHistoryError,
    ChatHistoryService,
)


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

    async def test_backfill_is_bounded_and_private_or_non_qq_events_are_denied(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
