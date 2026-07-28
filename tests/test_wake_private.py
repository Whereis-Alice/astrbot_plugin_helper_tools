from __future__ import annotations

import unittest

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
        self._stopped = False

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
