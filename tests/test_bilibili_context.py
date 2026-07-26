from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot.core.agent.message import TextPart

from astrbot_plugin_helper_tools.bilibili_service import request_has_bilibili_context
from astrbot_plugin_helper_tools.bilibili_types import BILIBILI_CONTEXT_PREFIX
from astrbot_plugin_helper_tools.main import HelperToolsPlugin


class FakeBilibiliService:
    def __init__(self) -> None:
        self.calls = 0

    def auto_parse_mode(self) -> str:
        return "follow"

    def analysis_mode(self) -> str:
        return "astrbot"

    async def context_for_event(self, _event) -> str:
        self.calls += 1
        return f"{BILIBILI_CONTEXT_PREFIX}\n测试视频事实"


class FakeEvent:
    unified_msg_origin = "default:GroupMessage:123"

    def is_stopped(self) -> bool:
        return False


class BilibiliContextHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_injects_facts_without_replacing_the_current_persona(self) -> None:
        bilibili = FakeBilibiliService()
        plugin = SimpleNamespace(
            config={"bilibili_video": {"enabled": True}},
            bilibili=bilibili,
            enabled=lambda: True,
        )
        event = FakeEvent()
        request = SimpleNamespace(
            prompt="用户原消息",
            system_prompt="当前 AstrBot 人格提示词",
            extra_user_content_parts=[],
        )

        await HelperToolsPlugin.bilibili_video_context_handler(plugin, event, request)
        await HelperToolsPlugin.bilibili_video_context_handler(plugin, event, request)

        self.assertEqual(request.prompt, "用户原消息")
        self.assertEqual(request.system_prompt, "当前 AstrBot 人格提示词")
        self.assertEqual(bilibili.calls, 1)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIsInstance(request.extra_user_content_parts[0], TextPart)
        self.assertIn("测试视频事实", request.extra_user_content_parts[0].text)
        self.assertTrue(getattr(request.extra_user_content_parts[0], "_no_save", False))

    def test_recognizes_success_and_failure_context_in_parts_or_prompt(self) -> None:
        success = SimpleNamespace(
            prompt="",
            extra_user_content_parts=[
                {"type": "text", "text": f"{BILIBILI_CONTEXT_PREFIX}\n事实"}
            ],
        )
        failure = SimpleNamespace(
            prompt="原消息\n[B站视频解析失败]\n原因：测试",
            extra_user_content_parts=[],
        )

        self.assertTrue(request_has_bilibili_context(success))
        self.assertTrue(request_has_bilibili_context(failure))


if __name__ == "__main__":
    unittest.main()
