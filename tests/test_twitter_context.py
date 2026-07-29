from __future__ import annotations

import ast
import base64
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.agent.message import (
    ImageURLPart,
    Message,
    TextPart,
    dump_messages_with_checkpoints,
)

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.twitter_service import (
    TWITTER_CONTEXT_PREFIX,
    TwitterContext,
    TwitterImage,
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
