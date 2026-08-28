from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.qq_like_service import QQProfileLikeService


class FakeBot:
    """模拟 LLOneBot：get_friend_list 无参数，send_like 只认 user_id / times。"""

    async def get_version_info(self):
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
        }

    async def get_friend_list(self):
        return {"status": "ok", "retcode": 0, "data": [{"user_id": "20001"}]}

    async def send_like(self, *, user_id: int, times: int):
        assert (user_id, times) == (20001, 10)
        return {"status": "ok", "retcode": 0, "data": None}


class FakeEvent:
    message_str = "赞我"
    unified_msg_origin = "default:GroupMessage:123"

    def __init__(self) -> None:
        self.bot = FakeBot()
        self._extras: dict[str, str] = {}
        self.is_at_or_wake_command = False
        self.call_llm = False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_platform_id(self) -> str:
        return "qq-test"

    def get_sender_id(self) -> str:
        return "20001"

    def get_self_id(self) -> str:
        return "10001"

    def get_group_id(self) -> str:
        return ""

    def get_messages(self) -> list[object]:
        return []

    def get_extra(self, key: str, default: str = "") -> str:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: str) -> None:
        self._extras[key] = value

    def is_stopped(self) -> bool:
        return False

    def should_call_llm(self, value: bool) -> None:
        self.call_llm = value

    def stop_event(self) -> None:
        raise AssertionError("persona reply must not stop the default LLM pipeline")


class QQProfileLikePersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()

    async def test_persona_mode_forces_default_pipeline_and_keeps_fact_temporary(self) -> None:
        config = {
            "qq_like": {
                "enabled": True,
                "cooldown_seconds": 0,
                # 触发词默认已改成空列表（用户能真正清空），这里显式配置。
                "trigger_phrases": ["赞我"],
                "persona_reply": {"enabled": True},
            }
        }
        like_service = QQProfileLikeService(config)
        plugin = SimpleNamespace(
            config=config,
            qq_like=like_service,
            wake=SimpleNamespace(is_llm_request_blocked=lambda _event: False),
            enabled=lambda: True,
            _message_has_wake_prefix=lambda _event: False,
            _message_without_wake_prefix=lambda _event: "",
        )
        event = FakeEvent()

        results = [
            item
            async for item in HelperToolsPlugin.dynamic_message_handler(plugin, event)
        ]

        self.assertEqual(results, [])
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.call_llm)
        request = SimpleNamespace(extra_user_content_parts=[])
        await HelperToolsPlugin.qq_like_persona_context_handler(plugin, event, request)

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertIsInstance(part, TextPart)
        self.assertIn("QQ 名片点赞动作已经由插件完成", part.text)
        self.assertTrue(getattr(part, "_no_save", False))
        persisted = dump_messages_with_checkpoints(
            [Message(role="user", content=request.extra_user_content_parts)]
        )
        self.assertEqual(persisted[0]["content"], [])
        self.assertEqual(like_service.take_persona_context(event), "")


if __name__ == "__main__":
    unittest.main()
