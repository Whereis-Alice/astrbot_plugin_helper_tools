from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_helper_tools.bot_profile_service import BotProfileService
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches


class FakeActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed：错误细节都在 .result 里。"""

    def __init__(self, retcode: int, message: str) -> None:
        super().__init__(f"ActionFailed(retcode={retcode})")
        self.retcode = retcode
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": message,
        }


#: LLOneBot 8.1.9 实测支持的 action 及其参数白名单。
LLONEBOT_SCHEMAS: dict[str, set[str]] = {
    "get_version_info": set(),
    "get_login_info": set(),
    "set_qq_profile": {"nickname", "personal_note"},
    "set_online_status": {"status", "ext_status", "battery_status"},
    "set_qq_avatar": {"file"},
}

#: LLOneBot 的 set_online_status 三个参数全部必填。
LLONEBOT_REQUIRED: dict[str, set[str]] = {
    "set_online_status": {"status", "ext_status", "battery_status"},
}


class FakeOneBot:
    """只通过 call_action 暴露 action 的假协议端。"""

    def __init__(
        self,
        *,
        app_name: str = "LLOneBot",
        app_version: str = "8.1.9",
        nickname: str = "小助手",
        schemas: dict[str, set[str]] | None = None,
        required: dict[str, set[str]] | None = None,
    ) -> None:
        self.app_name = app_name
        self.app_version = app_version
        self.nickname = nickname
        self.schemas = LLONEBOT_SCHEMAS if schemas is None else schemas
        self.required = LLONEBOT_REQUIRED if required is None else required
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def calls_of(self, action: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == action]

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action not in self.schemas:
            raise FakeActionFailed(1404, f"{action} API 不存在")
        allowed = self.schemas[action]
        unexpected = set(params) - allowed
        if unexpected:
            raise FakeActionFailed(1400, f"参数错误: {sorted(unexpected)}")
        missing = self.required.get(action, set()) - set(params)
        if missing:
            raise FakeActionFailed(1400, f"缺少参数: {sorted(missing)}")
        if action == "get_version_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"app_name": self.app_name, "app_version": self.app_version},
            }
        if action == "get_login_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 10001, "nickname": self.nickname},
            }
        return {"status": "ok", "retcode": 0, "data": None}


class ProfileEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:10000"

    def __init__(self, bot: FakeOneBot) -> None:
        self.bot = bot

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_messages(self) -> list[object]:
        return []


class BotProfileServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data_dir = Path(self._directory.name)

    def _service(self) -> BotProfileService:
        return BotProfileService(
            {"bot_profile": {"enabled": True, "llm_tool_enabled": True}},
            SimpleNamespace(),
            self.data_dir,
        )

    async def test_llonebot_signature_falls_back_to_personal_note_with_nickname(self) -> None:
        bot = FakeOneBot()
        event = ProfileEvent(bot)

        reply = await self._service().set_signature(event, "在摸鱼")

        self.assertIn("Bot 签名已更新", reply)
        # LLOneBot 没有 set_self_longnick，兼容层直接走 set_qq_profile.personal_note。
        self.assertEqual(bot.calls_of("set_self_longnick"), [])
        self.assertEqual(
            bot.calls_of("set_qq_profile"),
            [{"personal_note": "在摸鱼", "nickname": "小助手"}],
        )

    async def test_unknown_impl_signature_downgrades_after_1404(self) -> None:
        # app_name 不可识别时不做变体过滤，必须靠 retcode=1404 逐个降级。
        bot = FakeOneBot(app_name="SomeBot", nickname="阿助")
        event = ProfileEvent(bot)

        reply = await self._service().set_signature(event, "签名文本")

        self.assertIn("Bot 签名已更新", reply)
        attempted = [name for name, _params in bot.calls]
        self.assertIn("set_self_longnick", attempted)
        self.assertEqual(attempted[-1], "set_qq_profile")
        self.assertEqual(
            bot.calls_of("set_qq_profile"),
            [{"personal_note": "签名文本", "nickname": "阿助"}],
        )

    async def test_signature_still_works_when_get_login_info_fails(self) -> None:
        schemas = dict(LLONEBOT_SCHEMAS)
        schemas.pop("get_login_info")
        bot = FakeOneBot(schemas=schemas)
        event = ProfileEvent(bot)

        reply = await self._service().set_signature(event, "无昵称")

        self.assertIn("Bot 签名已更新", reply)
        self.assertEqual(bot.calls_of("set_qq_profile"), [{"personal_note": "无昵称"}])

    async def test_nickname_uses_set_qq_profile(self) -> None:
        bot = FakeOneBot()
        event = ProfileEvent(bot)

        reply = await self._service().set_nickname(event, "新昵称")

        self.assertIn("新昵称", reply)
        self.assertEqual(bot.calls_of("set_qq_profile"), [{"nickname": "新昵称"}])

    async def test_status_sends_all_three_required_params(self) -> None:
        bot = FakeOneBot()
        event = ProfileEvent(bot)

        reply = await self._service().set_status(event, "听歌中")

        self.assertIn("听歌中", reply)
        self.assertEqual(
            bot.calls_of("set_online_status"),
            [{"status": 10, "ext_status": 1028, "battery_status": 0}],
        )

    async def test_avatar_uses_set_qq_avatar(self) -> None:
        bot = FakeOneBot()
        event = ProfileEvent(bot)
        avatar = self.data_dir / "avatar.jpg"
        avatar.write_bytes(b"jpeg")

        reply = await self._service().set_avatar(event, str(avatar))

        self.assertIn("头像已更新", reply)
        self.assertEqual(bot.calls_of("set_qq_avatar"), [{"file": str(avatar)}])

    async def test_all_candidates_missing_returns_readable_chinese_hint(self) -> None:
        bot = FakeOneBot(app_name="SomeBot", schemas={"get_version_info": set()})
        event = ProfileEvent(bot)

        reply = await self._service().set_nickname(event, "改个名")

        self.assertIn("设置 Bot 昵称失败", reply)
        self.assertIn("没有提供可用的接口", reply)
        self.assertNotIn("Traceback", reply)

    async def test_execution_failure_reports_protocol_wording(self) -> None:
        class FailingBot(FakeOneBot):
            async def call_action(self, action: str, **params: Any) -> Any:
                if action == "set_qq_avatar":
                    self.calls.append((action, dict(params)))
                    raise FakeActionFailed(1200, "设置头像失败")
                return await super().call_action(action, **params)

        bot = FailingBot()
        event = ProfileEvent(bot)

        reply = await self._service().set_avatar(event, "/tmp/not-exist.jpg")

        self.assertIn("设置 Bot 头像失败", reply)
        self.assertIn("设置头像失败", reply)
        self.assertIn("1200", reply)


if __name__ == "__main__":
    unittest.main()
