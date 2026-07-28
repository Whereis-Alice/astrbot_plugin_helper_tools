from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.wake_service import WakeService


class FakeEvent:
    def __init__(
        self,
        *,
        group_id: str = "",
        sender_id: str = "10001",
        text: str = "blocked",
    ) -> None:
        self._group_id = group_id
        self._sender_id = sender_id
        self.message_str = text
        message_type = "GroupMessage" if group_id else "FriendMessage"
        self.unified_msg_origin = f"default:{message_type}:{group_id or sender_id}"
        self.is_at_or_wake_command = True
        self.is_wake = True
        self.call_llm = False
        self._stopped = False
        self._extra: dict[str, object] = {}

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id

    @staticmethod
    def get_self_id() -> str:
        return "20002"

    @staticmethod
    def get_platform_name() -> str:
        return "aiocqhttp"

    @staticmethod
    def get_messages() -> list[object]:
        return []

    @staticmethod
    def is_admin() -> bool:
        return False

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def should_call_llm(self, blocked: bool) -> None:
        self.call_llm = blocked

    def set_extra(self, key: str, value: object) -> None:
        self._extra[key] = value

    def get_extra(self, key: str, default: object = None) -> object:
        return self._extra.get(key, default)


def make_service(
    *,
    apply_to_private_messages: bool = False,
    global_blacklist: list[str] | None = None,
) -> WakeService:
    return WakeService(
        {
            "wake": {
                "enabled": True,
                "apply_to_private_messages": apply_to_private_messages,
                "global_blacklist": global_blacklist or [],
                "block_enabled": True,
                "wake_cd": 0,
                "block_qqbot": False,
                "block_reread": False,
                "block_keywords": ["blocked"],
                "block_keywords_initialized": True,
                "command_block_enabled": False,
                "debounce_enabled": False,
            }
        }
    )


class WakePrivateMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_command_names_include_group_paths_and_aliases(self) -> None:
        group = CommandGroupFilter("统计", alias={"stats"})
        command = CommandFilter(
            "发言榜里程碑",
            alias={"发言里程碑"},
            parent_command_names=group.get_complete_command_names(),
        )

        self.assertEqual(
            WakeService._complete_command_names(command),
            {
                "统计 发言榜里程碑",
                "统计 发言里程碑",
                "stats 发言榜里程碑",
                "stats 发言里程碑",
            },
        )

    async def test_private_message_bypasses_group_wake_rules_by_default(self) -> None:
        event = FakeEvent()

        result = await make_service().apply(event)

        self.assertEqual(result, "private_bypassed")
        self.assertFalse(event.is_stopped())
        self.assertTrue(event.is_at_or_wake_command)

    async def test_private_message_uses_rules_when_explicitly_enabled(self) -> None:
        event = FakeEvent()

        result = await make_service(apply_to_private_messages=True).apply(event)

        self.assertEqual(result, "block_keyword")
        self.assertTrue(event.is_stopped())

    async def test_group_message_keeps_existing_wake_rules(self) -> None:
        event = FakeEvent(group_id="30003")

        result = await make_service().apply(event)

        self.assertEqual(result, "block_keyword")
        self.assertTrue(event.is_stopped())

    async def test_global_blacklist_still_applies_in_private_chat(self) -> None:
        event = FakeEvent(sender_id="10001", text="normal text")

        result = await make_service(global_blacklist=["10001"]).apply(event)

        self.assertEqual(result, "global_blacklist")
        self.assertTrue(event.is_stopped())

    async def test_recognized_command_blocks_late_llm_after_plugin_overwrite(self) -> None:
        event = FakeEvent(group_id="30003", text="/发言里程碑")
        service = make_service()
        service.config["wake"]["command_block_enabled"] = True
        service.config["wake"]["suppress_llm_after_command"] = True

        with patch.object(
            WakeService,
            "_registered_command_names",
            return_value={"发言榜里程碑", "发言里程碑"},
        ):
            await service.apply(event)

        self.assertTrue(event.call_llm)
        self.assertTrue(service.is_llm_request_blocked(event))
        self.assertEqual(service.llm_request_block_reason(event), "recognized_command")

        # Simulate a third-party command handler that incorrectly enables the
        # framework's default LLM fallback after producing its command response.
        event.should_call_llm(False)
        plugin = SimpleNamespace(enabled=lambda: True, wake=service)
        await HelperToolsPlugin.wake_llm_request_guard(plugin, event, object())

        self.assertTrue(event.is_stopped())
