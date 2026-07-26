from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace

from astrbot.core.agent.message import (
    ImageURLPart,
    Message,
    TextPart,
    dump_messages_with_checkpoints,
)
from mcp.types import CallToolResult

from astrbot_plugin_helper_tools.bilibili_service import request_has_bilibili_context
from astrbot_plugin_helper_tools.bilibili_types import (
    BILIBILI_CONTEXT_PREFIX,
    BilibiliVideoContext,
    VideoFrame,
)
from astrbot_plugin_helper_tools.main import (
    BILIBILI_TOOL_NAME,
    HelperToolsPlugin,
    _bilibili_tool_result,
    _mark_bilibili_tool_frames_temporary,
)


class FakeBilibiliService:
    def __init__(self, frames: tuple[VideoFrame, ...] = ()) -> None:
        self.calls = 0
        self.frames = frames

    def auto_parse_mode(self) -> str:
        return "follow"

    def analysis_mode(self) -> str:
        return "astrbot"

    async def context_for_event_result(self, _event) -> BilibiliVideoContext:
        self.calls += 1
        return BilibiliVideoContext(
            f"{BILIBILI_CONTEXT_PREFIX}\n测试视频事实",
            frames=self.frames,
        )


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

    async def test_marks_automatic_frames_as_temporary(self) -> None:
        frames = (VideoFrame(index=1, timestamp=1.5, data=b"\xff\xd8fake-frame"),)
        bilibili = FakeBilibiliService(frames)
        plugin = SimpleNamespace(
            config={"bilibili_video": {"enabled": True}},
            bilibili=bilibili,
            enabled=lambda: True,
        )
        request = SimpleNamespace(
            prompt="用户原消息",
            system_prompt="当前 AstrBot 人格提示词",
            extra_user_content_parts=[],
        )

        await HelperToolsPlugin.bilibili_video_context_handler(plugin, FakeEvent(), request)

        self.assertEqual(len(request.extra_user_content_parts), 2)
        self.assertIsInstance(request.extra_user_content_parts[0], TextPart)
        self.assertIsInstance(request.extra_user_content_parts[1], ImageURLPart)
        self.assertTrue(
            request.extra_user_content_parts[1].image_url.url.startswith(
                "data:image/jpeg;base64,"
            )
        )
        self.assertTrue(
            all(
                getattr(part, "_no_save", False)
                for part in request.extra_user_content_parts
            )
        )
        persisted = dump_messages_with_checkpoints(
            [Message(role="user", content=request.extra_user_content_parts)]
        )
        self.assertEqual(persisted[0]["content"], [])

    def test_tool_frames_are_visible_now_but_marked_before_history_is_saved(self) -> None:
        frame = VideoFrame(index=1, timestamp=2.0, data=b"\xff\xd8tool-frame")
        result = _bilibili_tool_result(
            BilibiliVideoContext("工具视频资料", frames=(frame,))
        )

        self.assertIsInstance(result, CallToolResult)
        self.assertIn("工具视频资料", result.content[0].text)
        self.assertEqual(
            result.content[1].data,
            base64.b64encode(frame.data).decode("ascii"),
        )

        tool_frame_message = Message(
            role="user",
            content=[
                TextPart(
                    text=(
                        f"[Image from tool '{BILIBILI_TOOL_NAME}', "
                        "path='/tmp/video-frame.jpg']"
                    )
                ),
                ImageURLPart(
                    image_url=ImageURLPart.ImageURL(
                        url=frame.data_url,
                        id="/tmp/video-frame.jpg",
                    )
                ),
            ],
        )
        ordinary_message = Message(role="user", content=[TextPart(text="保留")])
        marked = _mark_bilibili_tool_frames_temporary(
            SimpleNamespace(messages=[tool_frame_message, ordinary_message])
        )

        self.assertEqual(marked, 1)
        self.assertTrue(getattr(tool_frame_message, "_no_save", False))
        self.assertTrue(
            all(getattr(part, "_no_save", False) for part in tool_frame_message.content)
        )
        self.assertFalse(getattr(ordinary_message, "_no_save", False))


if __name__ == "__main__":
    unittest.main()
