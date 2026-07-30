from __future__ import annotations

import ast
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.agent.message import (
    ImageURLPart,
    Message,
    TextPart,
    dump_messages_with_checkpoints,
)
from mcp.types import ImageContent, TextContent

from astrbot_plugin_helper_tools.main import (
    HelperToolsPlugin,
    _twitter_result_for_tool,
    _twitter_tool_result,
)
from astrbot_plugin_helper_tools.twitter_service import (
    TWITTER_CONTEXT_PREFIX,
    TwitterAccount,
    TwitterContext,
    TwitterImage,
    TwitterPost,
    TwitterResult,
    TwitterService,
    request_has_twitter_context,
)

MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


class _FakeTwitterService:
    def __init__(self, images: tuple[TwitterImage, ...] = ()) -> None:
        self.calls = 0
        self.images = images

    def auto_parse_mode(self) -> str:
        return "follow"

    def auto_parse_attach_images(self) -> bool:
        return bool(self.images)

    async def context_for_event_result(self, _event, *, include_images: bool) -> TwitterContext:
        self.calls += 1
        return TwitterContext(
            f"{TWITTER_CONTEXT_PREFIX}\n[X/Twitter 公开资料]\n测试推文事实",
            images=self.images if include_images else (),
        )


class _FakeEvent:
    unified_msg_origin = "default:GroupMessage:123"

    def is_stopped(self) -> bool:
        return False


class _ActionFailedLike(Exception):
    def __init__(self, *, retcode: int | str, message: str) -> None:
        super().__init__(message)
        self.retcode = retcode
        self.message = message
        self.wording = message


class _FailingSendEvent:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.send_calls = 0

    @staticmethod
    def chain_result(chain):
        return chain

    async def send(self, _result) -> None:
        self.send_calls += 1
        raise self.error


class _PreparedImageTwitterService(TwitterService):
    async def collect_safe_images(
        self,
        _posts,
        *,
        limit: int,
        require_bytes: bool,
    ) -> tuple[tuple[TwitterImage, ...], int]:
        del limit, require_bytes
        return (
            (
                TwitterImage(
                    source_url="https://pbs.twimg.com/media/example.jpg",
                    caption="Artist (@artist)（本人发布）",
                ),
            ),
            0,
        )


class TwitterContextHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_injects_x_facts_and_images_as_current_turn_only(self) -> None:
        image = TwitterImage(
            source_url="https://pbs.twimg.com/media/example.jpg",
            data=b"\xff\xd8twitter-image",
        )
        service = _FakeTwitterService((image,))
        plugin = SimpleNamespace(
            config={"twitter": {"enabled": True}},
            twitter=service,
            enabled=lambda: True,
        )
        request = SimpleNamespace(
            prompt="用户原消息",
            system_prompt="当前 AstrBot 人格",
            extra_user_content_parts=[],
        )

        await HelperToolsPlugin.twitter_context_handler(plugin, _FakeEvent(), request)
        await HelperToolsPlugin.twitter_context_handler(plugin, _FakeEvent(), request)

        self.assertEqual(service.calls, 1)
        self.assertEqual(request.prompt, "用户原消息")
        self.assertEqual(request.system_prompt, "当前 AstrBot 人格")
        self.assertEqual(len(request.extra_user_content_parts), 2)
        self.assertIsInstance(request.extra_user_content_parts[0], TextPart)
        self.assertIsInstance(request.extra_user_content_parts[1], ImageURLPart)
        self.assertTrue(
            request.extra_user_content_parts[1].image_url.url.startswith(
                "data:image/jpeg;base64,"
            )
        )
        self.assertTrue(
            all(getattr(part, "_no_save", False) for part in request.extra_user_content_parts)
        )
        persisted = dump_messages_with_checkpoints(
            [Message(role="user", content=request.extra_user_content_parts)]
        )
        self.assertEqual(persisted[0]["content"], [])

    def test_recognizes_twitter_context_in_parts_or_prompt(self) -> None:
        in_parts = SimpleNamespace(
            prompt="",
            extra_user_content_parts=[
                {"type": "text", "text": f"{TWITTER_CONTEXT_PREFIX}\n事实"}
            ],
        )
        in_prompt = SimpleNamespace(
            prompt=f"原消息\n{TWITTER_CONTEXT_PREFIX}\n读取失败",
            extra_user_content_parts=[],
        )

        self.assertTrue(request_has_twitter_context(in_parts))
        self.assertTrue(request_has_twitter_context(in_prompt))

    def test_image_payload_is_normal_base64_data(self) -> None:
        image = TwitterImage(source_url="https://example.test/image.jpg", data=b"test")
        self.assertEqual(
            image.data_url,
            "data:image/jpeg;base64," + base64.b64encode(b"test").decode("ascii"),
        )

    def test_tool_image_is_preceded_by_its_author_source(self) -> None:
        result = _twitter_tool_result(
            TwitterContext(
                f"{TWITTER_CONTEXT_PREFIX}\n推文资料",
                images=(
                    TwitterImage(
                        source_url="https://example.test/image.jpg",
                        data=b"test",
                        caption="Artist (@artist) 转推；原作者 Other (@other)",
                    ),
                ),
            )
        )

        self.assertIsInstance(result.content[0], TextContent)
        self.assertIsInstance(result.content[1], TextContent)
        self.assertIn("原作者 Other", result.content[1].text)
        self.assertIsInstance(result.content[2], ImageContent)

    async def test_qq_send_ack_timeout_keeps_tool_result_without_retry(self) -> None:
        error = _ActionFailedLike(
            retcode="1200",
            message=(
                "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
                "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate"
            ),
        )
        event = _FailingSendEvent(error)
        with tempfile.TemporaryDirectory() as temporary:
            service = _PreparedImageTwitterService(
                {"twitter": {"download_media_before_send": False}},
                Path(temporary),
            )
            result = TwitterResult(
                title="X/Twitter 本人发布内容检索",
                query="from:artist",
                posts=(
                    TwitterPost(
                        post_id="123",
                        author=TwitterAccount(username="artist", name="Artist"),
                        text="测试原创内容",
                        url="https://x.com/artist/status/123",
                    ),
                ),
            )
            tool_result = await _twitter_result_for_tool(
                SimpleNamespace(twitter=service),
                SimpleNamespace(context=SimpleNamespace(event=event)),
                result,
                return_images=False,
                send_images=True,
                max_images=1,
            )

        self.assertIsInstance(tool_result, str)
        self.assertEqual(event.send_calls, 1)
        self.assertIn("测试原创内容", tool_result)
        self.assertIn("QQ 回执超时", tool_result)
        self.assertIn("请勿重复发送", tool_result)

    async def test_unrelated_send_action_failure_still_propagates(self) -> None:
        error = _ActionFailedLike(
            retcode=1200,
            message="sendMsg failed because permission was denied",
        )
        event = _FailingSendEvent(error)
        with tempfile.TemporaryDirectory() as temporary:
            service = _PreparedImageTwitterService(
                {"twitter": {"download_media_before_send": False}},
                Path(temporary),
            )
            with self.assertRaises(_ActionFailedLike):
                await service.send_images_to_event(
                    event,
                    TwitterResult(title="测试", query="artist"),
                    max_images=1,
                )

        self.assertEqual(event.send_calls, 1)


class TwitterCommandNamespaceTests(unittest.TestCase):
    def test_commands_use_the_helper_x_namespace(self) -> None:
        tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
        command_names: dict[str, str] = {}
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                if not isinstance(decorator.func, ast.Attribute) or decorator.func.attr != "command":
                    continue
                argument = decorator.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    command_names[node.name] = argument.value
                for keyword in decorator.keywords:
                    if keyword.arg != "alias" or not isinstance(
                        keyword.value,
                        (ast.Set, ast.List, ast.Tuple),
                    ):
                        continue
                    for alias in keyword.value.elts:
                        if isinstance(alias, ast.Constant) and isinstance(alias.value, str):
                            aliases.add(alias.value)

        expected = {
            "twitter_search_command": "helper_x_search",
            "twitter_account_command": "helper_x_account",
            "twitter_recent_command": "helper_x_recent",
            "twitter_post_command": "helper_x_post",
        }
        self.assertEqual({name: command_names[name] for name in expected}, expected)
        self.assertFalse({"x", "twitter", "推特", "X搜索"} & aliases)


if __name__ == "__main__":
    unittest.main()
