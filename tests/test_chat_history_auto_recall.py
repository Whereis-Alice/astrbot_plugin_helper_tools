from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.chat_history_auto_recall import (
    AUTO_RECALL_MARKER,
    prepare_auto_recall,
)
from astrbot_plugin_helper_tools.chat_history_service import ChatHistoryService
from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.tests.test_chat_history_service import HistoryEvent

NOW = datetime(2026, 11, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
RECAP = "总结一下昨天下午6:00到8:30发生的事"


class AutoRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.config = {
            "chat_history": {
                "enabled": True,
                "llm_tool_enabled": True,
                "onebot_backfill_enabled": False,
                "max_result_messages": 2,
            }
        }
        self.history = ChatHistoryService(self.config, Path(self.directory.name))
        self.event = HistoryEvent(text=RECAP, timestamp=int(NOW.timestamp()))
        self.event.stopped = False
        self.event.is_stopped = lambda: self.event.stopped
        self.plugin = SimpleNamespace(
            config=self.config,
            chat_history=self.history,
            _tool_active=lambda *_args: True,
        )
        self.clock = patch(
            "astrbot_plugin_helper_tools.chat_history_service.time.time",
            return_value=NOW.timestamp(),
        )
        self.clock.start()

    def tearDown(self) -> None:
        self.clock.stop()
        self.history.close()
        self.directory.cleanup()

    async def seed(self, count: int = 7) -> None:
        start = NOW.replace(day=23, hour=18)
        for index in range(count):
            await self.history.capture_event(
                HistoryEvent(
                    text=f"本群事件{index}",
                    message_id=str(index + 10),
                    timestamp=int((start + timedelta(minutes=index + 1)).timestamp()),
                )
            )
        await self.history.capture_event(
            HistoryEvent(
                group_id="99999",
                text="其它群隐私",
                message_id="50",
                timestamp=int(start.timestamp()),
            )
        )

    @staticmethod
    def request() -> SimpleNamespace:
        return SimpleNamespace(
            prompt="原始用户消息", system_prompt="当前人格", extra_user_content_parts=[]
        )

    async def test_reads_multiple_pages_in_time_order_without_cross_group_data(
        self,
    ) -> None:
        await self.seed()
        with patch.object(
            self.history, "_backfill", wraps=self.history._backfill
        ) as backfill:
            result = await self.history.search_for_recall(
                self.event,
                start="2026-11-23 18:00",
                end="2026-11-23 20:30",
                max_messages=20,
            )
        self.assertEqual(backfill.await_count, 1)
        self.assertEqual(
            [item.content for item in result.messages],
            [f"本群事件{i}" for i in range(7)],
        )
        self.assertEqual(result.total_count, 7)

    async def test_auto_recall_exposes_limits_and_actual_query_range(self) -> None:
        await self.seed()
        self.config["chat_history"]["auto_recall_max_messages"] = 5
        context = await prepare_auto_recall(self.history, self.event, RECAP, now=NOW)
        self.assertIn("5/7", context)
        self.assertIn("2026-11-23 18:00", context)
        self.assertIn("2026-11-23 20:30", context)
        self.assertNotIn("其它群隐私", context)
        self.assertIn("不能称为完整回顾", context)
        self.assertIn("不可信用户内容", context)

    async def test_result_is_temporary_and_keeps_user_prompt_and_persona(self) -> None:
        await self.seed()
        request = self.request()
        await HelperToolsPlugin.chat_history_auto_recall_handler(
            self.plugin, self.event, request
        )
        self.assertEqual(request.prompt, "原始用户消息")
        self.assertTrue(request.system_prompt.startswith("当前人格"))
        self.assertIn("不要仅凭对话记忆", request.system_prompt)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertTrue(getattr(request.extra_user_content_parts[0], "_no_save", False))
        self.assertIn(AUTO_RECALL_MARKER, request.extra_user_content_parts[0].text)

    async def test_wake_stop_module_tool_and_auto_switches_prevent_history_reads(
        self,
    ) -> None:
        cases = ("wake", "stopped", "module", "auto", "tool", "filtered")
        from astrbot.core.agent.tool import ToolSet

        for reason in cases:
            with self.subTest(reason=reason):
                event = HistoryEvent(text=RECAP, timestamp=int(NOW.timestamp()))
                event.is_stopped = lambda selected=reason: selected == "stopped"
                event.helper_tools_wake_block_llm_request = reason == "wake"
                request = self.request()
                plugin = SimpleNamespace(
                    config={"chat_history": {"auto_recall_enabled": reason != "auto"}},
                    chat_history=self.history,
                    _tool_active=lambda *_args, selected=reason: (
                        selected not in ("module", "tool")
                    ),
                )
                if reason == "filtered":
                    request.func_tool = ToolSet(tools=[])
                with patch(
                    "astrbot_plugin_helper_tools.main.prepare_auto_recall",
                    new_callable=AsyncMock,
                ) as prepare:
                    await HelperToolsPlugin.chat_history_auto_recall_handler(
                        plugin, event, request
                    )
                prepare.assert_not_awaited()
                self.assertEqual(request.extra_user_content_parts, [])

    async def test_synthetic_poke_and_unsupported_request_parts_are_skipped(
        self,
    ) -> None:
        for synthetic in (True, False):
            request = self.request()
            if not synthetic:
                del request.extra_user_content_parts
            with (
                patch(
                    "astrbot_plugin_helper_tools.main.is_poke_synthetic_command",
                    return_value=synthetic,
                ),
                patch(
                    "astrbot_plugin_helper_tools.main.prepare_auto_recall",
                    new_callable=AsyncMock,
                ) as prepare,
            ):
                await HelperToolsPlugin.chat_history_auto_recall_handler(
                    self.plugin, self.event, request
                )
            prepare.assert_not_awaited()
            self.assertEqual(request.prompt, "原始用户消息")

    async def test_quoted_recap_does_not_trigger_retrieval(self) -> None:
        self.event.get_messages = lambda: [
            Comp.Reply(id="123", chain=[Comp.Plain(RECAP)]),
            Comp.Plain("这是什么？"),
        ]
        with patch.object(
            self.history, "search_for_recall", wraps=self.history.search_for_recall
        ) as search:
            request = self.request()
            await HelperToolsPlugin.chat_history_auto_recall_handler(
                self.plugin, self.event, request
            )
        search.assert_not_awaited()
        self.assertEqual(request.extra_user_content_parts, [])

    async def test_one_event_is_retrieved_once_and_each_request_has_one_context(
        self,
    ) -> None:
        await self.seed()
        request = self.request()
        with patch.object(
            self.history, "search_for_recall", wraps=self.history.search_for_recall
        ) as search:
            await HelperToolsPlugin.chat_history_auto_recall_handler(
                self.plugin, self.event, request
            )
            await HelperToolsPlugin.chat_history_auto_recall_handler(
                self.plugin, self.event, request
            )
            await HelperToolsPlugin.chat_history_auto_recall_handler(
                self.plugin, self.event, self.request()
            )
        self.assertEqual(search.await_count, 1)
        self.assertEqual(len(request.extra_user_content_parts), 1)

    async def test_event_stopped_during_fetch_does_not_receive_evidence(self) -> None:
        async def stopped_fetch(*_args, **_kwargs):
            self.event.stopped = True
            return AUTO_RECALL_MARKER

        request = self.request()
        with patch(
            "astrbot_plugin_helper_tools.main.prepare_auto_recall",
            side_effect=stopped_fetch,
        ):
            await HelperToolsPlugin.chat_history_auto_recall_handler(
                self.plugin, self.event, request
            )
        self.assertEqual(request.extra_user_content_parts, [])

    async def test_disallowed_group_and_private_messages_are_not_queried(self) -> None:
        self.config["chat_history"]["blocked_group_ids"] = ["10000"]
        for group_id in ("10000", ""):
            event = HistoryEvent(group_id=group_id, text=RECAP)
            with patch.object(
                self.history, "search_for_recall", wraps=self.history.search_for_recall
            ) as search:
                result = await prepare_auto_recall(self.history, event, RECAP, now=NOW)
            self.assertEqual(result, "")
            search.assert_not_awaited()

    async def test_unexpected_read_failure_returns_no_claimed_events(self) -> None:
        request = self.request()
        with patch(
            "astrbot_plugin_helper_tools.main.prepare_auto_recall",
            side_effect=RuntimeError("secret transport detail"),
        ):
            await HelperToolsPlugin.chat_history_auto_recall_handler(
                self.plugin, self.event, request
            )
        context = request.extra_user_content_parts[0].text
        self.assertIn("读取失败", context)
        self.assertNotIn("secret", context)
