from __future__ import annotations

import unittest
from base64 import b64decode
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from mcp.types import CallToolResult, ImageContent

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.qq_features import (
    QQ_GROUP_INFO_TOOL_NAME,
    QQ_GROUP_MEMBER_LIST_TOOL_NAME,
    QQService,
    build_qq_group_avatar_url,
)


class FakeEvent:
    def __init__(self, bot: Any, *, group_id: str = "30003") -> None:
        self.bot = bot
        self._group_id = group_id

    def get_group_id(self) -> str:
        return self._group_id

    @staticmethod
    def get_sender_id() -> str:
        return "20001"

    @staticmethod
    def get_self_id() -> str:
        return "10001"


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.members = [
            {
                "group_id": 30003,
                "user_id": 20001,
                "nickname": "Alice",
                "card": "爱丽丝",
                "role": "owner",
                "level": "88",
                "title": "群主头衔",
                "sex": "female",
                "join_time": 1700000000,
                "last_sent_time": 1700100000,
                "custom_badge": "星之卡比",
            },
            {
                "group_id": 30003,
                "user_id": 20002,
                "nickname": "Bob",
                "card": "小明",
                "role": "admin",
                "level": "42",
                "title": "管理员头衔",
                "is_robot": True,
            },
            {
                "group_id": 30003,
                "user_id": 20003,
                "nickname": "Carol",
                "role": "member",
                "level": "12",
                "unfriendly": True,
            },
        ]
        self.group_info = {
            "group_id": 30003,
            "group_name": "测试群",
            "group_memo": "群备注",
            "group_create_time": 1700000000,
            "group_level": 5,
            "member_count": 3,
            "max_member_count": 500,
            "custom_group_field": "可见扩展字段",
        }
        self.honors = {
            "current_talkative": {
                "user_id": 20001,
                "nickname": "Alice",
                "day_count": 7,
            },
            "talkative_list": [
                {"user_id": 20001, "nickname": "Alice"},
                {"user_id": 20002, "nickname": "Bob"},
            ],
        }
        self.at_all = {
            "can_at_all": True,
            "remain_at_all_count_for_group": 2,
            "remain_at_all_count_for_uin": 1,
        }

    async def get_group_member_list(self, *, group_id: int) -> list[dict[str, Any]]:
        self.calls.append(("get_group_member_list", {"group_id": group_id}))
        return self.members

    async def get_group_info(self, *, group_id: int) -> dict[str, Any]:
        self.calls.append(("get_group_info", {"group_id": group_id}))
        return {"data": self.group_info}

    async def get_group_honor_info(self, *, group_id: int, type: str) -> dict[str, Any]:
        self.calls.append(("get_group_honor_info", {"group_id": group_id, "type": type}))
        return self.honors

    async def get_group_at_all_remain(self, *, group_id: int) -> dict[str, Any]:
        self.calls.append(("get_group_at_all_remain", {"group_id": group_id}))
        return self.at_all


class QQGroupDetailsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _service(**overrides: Any) -> QQService:
        settings = {
            "include_raw_extra_fields": True,
            "member_list_default_limit": 2,
            "member_list_max_limit": 3,
            "member_list_max_text_chars": 12000,
            "group_info_max_text_chars": 12000,
            "group_info_include_member_statistics": True,
            "group_info_include_honors": True,
            "group_info_honor_member_limit": 1,
            "group_info_include_at_all_remain": True,
        }
        settings.update(overrides)
        return QQService({"qq_member": settings})

    async def test_member_list_includes_detailed_fields_and_filters(self) -> None:
        bot = FakeBot()
        service = self._service()

        result = await service.get_group_member_list_result(
            event=FakeEvent(bot),
            keyword="爱丽",
            limit=3,
        )

        self.assertIn("QQ群成员列表", result)
        self.assertIn("群名称: 测试群", result)
        self.assertIn("QQ 20001", result)
        self.assertIn("QQ昵称: Alice", result)
        self.assertIn("群昵称: 爱丽丝", result)
        self.assertIn("群身份: 群主", result)
        self.assertIn("群等级: 88", result)
        self.assertIn("群专属头衔: 群主头衔", result)
        self.assertIn("custom_badge", result)
        self.assertNotIn("QQ 20002", result)
        self.assertEqual(bot.calls[:2], [
            ("get_group_member_list", {"group_id": 30003}),
            ("get_group_info", {"group_id": 30003}),
        ])

    async def test_member_list_paginates_and_enforces_the_configured_limit(self) -> None:
        bot = FakeBot()
        service = self._service(member_list_default_limit=1, member_list_max_limit=2)

        result = await service.get_group_member_list_result(
            event=FakeEvent(bot),
            offset=1,
            limit=99,
            include_group_info=False,
        )

        self.assertIn("本次返回: 第 2-3 名", result)
        self.assertIn("QQ 20002", result)
        self.assertIn("QQ 20003", result)
        self.assertNotIn("QQ 20001", result)
        self.assertEqual(bot.calls, [("get_group_member_list", {"group_id": 30003})])

    async def test_group_info_combines_standard_fields_statistics_honors_and_quota(self) -> None:
        bot = FakeBot()
        service = self._service()

        result = await service.get_group_info_result(event=FakeEvent(bot))

        self.assertIn("QQ群详情", result)
        self.assertIn("群名称: 测试群", result)
        self.assertIn("群备注: 群备注", result)
        self.assertIn("成员数: 3", result)
        self.assertIn("custom_group_field", result)
        self.assertIn("成员统计（基于 OneBot 返回的 3 名成员）", result)
        self.assertIn("群主 1", result)
        self.assertIn("管理员 1", result)
        self.assertIn("当前龙王", result)
        self.assertIn("龙王", result)
        self.assertIn("另有 1 位未列出", result)
        self.assertIn("当前账号可 @全体成员: 是", result)
        self.assertEqual(
            [name for name, _params in bot.calls],
            [
                "get_group_info",
                "get_group_member_list",
                "get_group_honor_info",
                "get_group_at_all_remain",
            ],
        )

    async def test_group_info_skips_optional_queries_when_requested(self) -> None:
        bot = FakeBot()
        service = self._service()

        result = await service.get_group_info_result(
            event=FakeEvent(bot),
            include_member_statistics=False,
            include_honors=False,
            include_at_all_remain=False,
        )

        self.assertIn("QQ群详情", result)
        self.assertNotIn("成员统计", result)
        self.assertEqual(bot.calls, [("get_group_info", {"group_id": 30003})])

    async def test_group_info_can_return_group_avatar_to_the_model(self) -> None:
        bot = FakeBot()
        service = self._service()

        async def fake_fetch_bytes(url: str, **_kwargs: Any) -> tuple[bytes, str]:
            self.assertEqual(url, build_qq_group_avatar_url("30003"))
            return b"group-avatar", "image/png"

        with patch(
            "astrbot_plugin_helper_tools.qq_features.fetch_bytes",
            side_effect=fake_fetch_bytes,
        ):
            result = await service.get_group_info_result(
                event=FakeEvent(bot),
                include_member_statistics=False,
                include_honors=False,
                include_at_all_remain=False,
                include_avatar=True,
                return_image=True,
            )

        self.assertIsInstance(result, CallToolResult)
        text_part, image_part = result.content
        self.assertIn("群头像 URL: https://p.qlogo.cn/gh/30003/30003/100", text_part.text)
        self.assertIsInstance(image_part, ImageContent)
        self.assertEqual(b64decode(image_part.data), b"group-avatar")
        self.assertEqual(image_part.mimeType, "image/png")

    async def test_group_info_can_return_the_avatar_url_without_downloading(self) -> None:
        bot = FakeBot()
        result = await self._service().get_group_info_result(
            event=FakeEvent(bot),
            include_member_statistics=False,
            include_honors=False,
            include_at_all_remain=False,
            include_avatar=True,
            return_image=False,
        )

        self.assertIsInstance(result, str)
        self.assertIn("群头像 URL: https://p.qlogo.cn/gh/30003/30003/100", result)


class QQGroupToolRegistrationTests(unittest.TestCase):
    def test_group_list_and_detail_tools_are_registered_with_the_member_module(self) -> None:
        plugin = SimpleNamespace(
            _tool_active=lambda _module, _default=True: True,
            _web_browser_tool_active=lambda: False,
        )

        tools = HelperToolsPlugin._build_tools(plugin)

        names = {tool.name for tool in tools}
        self.assertIn(QQ_GROUP_MEMBER_LIST_TOOL_NAME, names)
        self.assertIn(QQ_GROUP_INFO_TOOL_NAME, names)
        group_info_tool = next(tool for tool in tools if tool.name == QQ_GROUP_INFO_TOOL_NAME)
        self.assertIn("include_avatar", group_info_tool.parameters["properties"])
        self.assertIn("return_image", group_info_tool.parameters["properties"])
