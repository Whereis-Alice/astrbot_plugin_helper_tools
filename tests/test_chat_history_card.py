from __future__ import annotations

import unittest
from zoneinfo import ZoneInfo

from astrbot_plugin_helper_tools.chat_history_card import ChatHistoryCardRenderer
from astrbot_plugin_helper_tools.chat_history_service import (
    BackfillStatus,
    ChatHistorySearchResult,
    HistoryMessage,
    HistoryScope,
)


def _result() -> ChatHistorySearchResult:
    scope = HistoryScope(
        key="aiocqhttp:10001:10000",
        platform="aiocqhttp",
        self_id="10001",
        group_id="10000",
    )
    message = HistoryMessage(
        scope_key=scope.key,
        group_id=scope.group_id,
        message_id="1",
        message_seq=1,
        timestamp=1_700_000_000,
        sender_id="20001",
        sender_name="<测试用户>",
        content="<script>alert('xss')</script>",
        is_bot=False,
    )
    return ChatHistorySearchResult(
        scope=scope,
        query_start=1_700_000_000,
        query_end=1_700_000_100,
        messages=(message,),
        total_count=1,
        result_limit=20,
        range_capped=False,
        backfill=BackfillStatus(),
    )


class FakePlugin:
    def __init__(self, response: str = "https://example.com/card.jpg") -> None:
        self.response = response
        self.template = ""
        self.payload: dict[str, object] = {}
        self.options: dict[str, object] = {}

    async def html_render(
        self,
        template: str,
        data: dict[str, object],
        return_url: bool = True,
        options: dict[str, object] | None = None,
    ) -> str:
        self.template = template
        self.payload = data
        self.options = options or {}
        self.assert_return_url = return_url
        return self.response


class ChatHistoryCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_selected_skin_and_escapes_message_fields(self) -> None:
        renderer = ChatHistoryCardRenderer()
        plugin = FakePlugin()

        card = await renderer.render(
            plugin,
            _result(),
            timezone=ZoneInfo("Asia/Shanghai"),
            skin="mint",
            include_sender_qq=False,
            max_messages=24,
            max_chars=8_000,
        )

        self.assertEqual(card.image_url, "https://example.com/card.jpg")
        self.assertEqual(card.skin, "薄荷")
        self.assertIn("{{ item.meta | e }}", plugin.template)
        self.assertIn("{{ item.content | e }}", plugin.template)
        self.assertEqual(
            plugin.payload["messages"][0]["content"],  # type: ignore[index]
            "<script>alert('xss')</script>",
        )
        self.assertFalse("QQ 20001" in plugin.payload["messages"][0]["meta"])  # type: ignore[index]
        self.assertEqual(plugin.options["type"], "jpeg")
        self.assertEqual(plugin.options["selector"], "#chat-history-card")
        self.assertFalse(plugin.options["full_page"])
        self.assertEqual(
            renderer.normalize_skin("不存在的皮肤", default="纸笺"),
            "纸笺",
        )

    async def test_rejects_non_http_t2i_result(self) -> None:
        renderer = ChatHistoryCardRenderer()
        card = await renderer.render(
            FakePlugin("file:///tmp/card.jpg"),
            _result(),
            timezone=ZoneInfo("Asia/Shanghai"),
            skin="霓虹",
            include_sender_qq=True,
            max_messages=24,
            max_chars=8_000,
        )

        self.assertFalse(card.image_url)
        self.assertIn("可发送", card.error)


if __name__ == "__main__":
    unittest.main()
