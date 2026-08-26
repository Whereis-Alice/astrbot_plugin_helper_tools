from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.qq_like_service import (
    QQ_LIKE_PERSONA_CONTEXT_PREFIX,
    QQProfileLikeService,
)


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


class LLOneBotBot:
    """只通过 call_action 暴露 action、并按 LLOneBot 的 schema 校验参数。"""

    def __init__(
        self,
        *,
        friend_ids: list[str] | None = None,
        send_like_error: Exception | None = None,
        supports_send_like: bool = True,
    ) -> None:
        self.friend_ids = friend_ids or []
        self.send_like_error = send_like_error
        self.supports_send_like = supports_send_like
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.likes: list[tuple[Any, Any]] = []

    def calls_of(self, action: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == action]

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action == "get_version_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
            }
        if action == "get_friend_list":
            # LLOneBot 的 get_friend_list 没有任何参数。
            if params:
                raise FakeActionFailed(1400, f"参数错误: {sorted(params)}")
            return {
                "status": "ok",
                "retcode": 0,
                "data": [{"user_id": user_id} for user_id in self.friend_ids],
            }
        if action == "send_like":
            if not self.supports_send_like:
                raise FakeActionFailed(1404, "send_like API 不存在")
            if set(params) - {"user_id", "times"}:
                raise FakeActionFailed(1400, f"参数错误: {sorted(params)}")
            if self.send_like_error is not None:
                raise self.send_like_error
            self.likes.append((params.get("user_id"), params.get("times")))
            return {"status": "ok", "retcode": 0, "data": None}
        raise FakeActionFailed(1404, f"{action} API 不存在")


class FakeBot:
    def __init__(
        self,
        *,
        friend_ids: list[str] | None = None,
        failure: Exception | None = None,
        response: object | None = None,
    ) -> None:
        self.friend_ids = friend_ids or []
        self.failure = failure
        self.response = {} if response is None else response
        self.likes: list[tuple[int, int]] = []
        self.friend_list_calls = 0

    async def get_friend_list(self):
        self.friend_list_calls += 1
        return [{"user_id": user_id} for user_id in self.friend_ids]

    async def send_like(self, *, user_id: int, times: int):
        if self.failure is not None:
            raise self.failure
        self.likes.append((user_id, times))
        return self.response


class FakeEvent:
    def __init__(
        self,
        *,
        text: str,
        bot: FakeBot,
        sender_id: str = "20001",
        self_id: str = "10001",
        group_id: str = "",
        mentions: list[str] | None = None,
    ) -> None:
        self.message_str = text
        self.bot = bot
        self._sender_id = sender_id
        self._self_id = self_id
        self._group_id = group_id
        self._mentions = mentions or []
        self._extras: dict[str, str] = {}

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_platform_id(self) -> str:
        return "qq-test"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_messages(self) -> list[object]:
        return [SimpleNamespace(qq=qq_id) for qq_id in self._mentions]

    def set_extra(self, key: str, value: str) -> None:
        self._extras[key] = value

    def get_extra(self, key: str, default: str = "") -> str:
        return self._extras.get(key, default)


class QQProfileLikeServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()

    @staticmethod
    def _config(**overrides):
        settings = {
            "enabled": True,
            "likes_per_target": 10,
            "cooldown_seconds": 0,
        }
        settings.update(overrides)
        return {"qq_like": settings}

    async def test_self_trigger_sends_one_bounded_like(self) -> None:
        bot = FakeBot(friend_ids=["20001"])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertTrue(result.handled)
        self.assertEqual(bot.likes, [(20001, 10)])
        self.assertIn("已向 QQ 提交给你的 10 个赞请求", result.reply)
        self.assertEqual(bot.friend_list_calls, 1)

    async def test_stranger_success_is_not_reported_as_delivered(self) -> None:
        bot = FakeBot(friend_ids=[])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertTrue(result.handled)
        self.assertEqual(bot.likes, [(20001, 10)])
        self.assertIn("无法核验是否到账", result.reply)
        self.assertNotIn("已给你点了", result.reply)

    async def test_mention_trigger_limits_targets_and_skips_the_bot(self) -> None:
        bot = FakeBot(friend_ids=["20002", "20003"])
        event = FakeEvent(
            text="赞 @20002 @10001 @20003",
            bot=bot,
            mentions=["20002", "10001", "20003"],
        )
        service = QQProfileLikeService(self._config(max_targets_per_message=1))

        result = await service.handle_message(event, event.message_str)

        self.assertTrue(result.handled)
        self.assertEqual(bot.likes, [(20002, 10)])
        self.assertIn("QQ 20002", result.reply)

    async def test_stranger_permission_failure_has_actionable_reply(self) -> None:
        bot = FakeBot(friend_ids=[], failure=RuntimeError("权限不足"))
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertTrue(result.handled)
        self.assertIn("陌生人", result.reply)
        self.assertIn("权限", result.reply)

    async def test_cooldown_blocks_a_second_request_without_another_api_call(self) -> None:
        bot = FakeBot(friend_ids=["20001"])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config(cooldown_seconds=60))

        first = await service.handle_message(event, event.message_str)
        second = await service.handle_message(event, event.message_str)

        self.assertTrue(first.handled)
        self.assertTrue(second.handled)
        self.assertEqual(bot.likes, [(20001, 10)])
        self.assertIn("秒后再试", second.reply)

    async def test_string_retcode_failure_is_not_treated_as_success(self) -> None:
        bot = FakeBot(
            friend_ids=["20001"],
            response={
                "status": "ok",
                "retcode": "1200",
                "wording": "点赞失败 今日同一好友点赞数已达上限",
            },
        )
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertIn("已达上限", result.reply)
        self.assertNotIn("已向 QQ 提交", result.reply)

    async def test_persona_context_is_temporary_and_contains_only_result_facts(self) -> None:
        bot = FakeBot(friend_ids=["20001"])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(
            self._config(persona_reply={"enabled": True})
        )

        result = await service.handle_message(event, event.message_str)
        attached = service.attach_persona_context(event, result.persona_context)
        consumed = service.take_persona_context(event)

        self.assertTrue(attached)
        self.assertIn(QQ_LIKE_PERSONA_CONTEXT_PREFIX, consumed)
        self.assertIn("OneBot 已接受 10 个赞请求", consumed)
        self.assertEqual(service.take_persona_context(event), "")

    async def test_disabled_module_does_not_handle_messages(self) -> None:
        bot = FakeBot(friend_ids=["20001"])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService({"qq_like": {"enabled": False}})

        result = await service.handle_message(event, event.message_str)

        self.assertFalse(result.handled)
        self.assertEqual(bot.likes, [])

    async def test_other_onebot_platform_names_are_accepted(self) -> None:
        service = QQProfileLikeService(self._config())
        for platform in ("aiocqhttp", "LLOneBot", "napcat", "lagrange", "go-cqhttp", ""):
            with self.subTest(platform=platform):
                event = SimpleNamespace(get_platform_name=lambda name=platform: name)
                self.assertTrue(service._is_qq_event(event))
        event = SimpleNamespace(get_platform_name=lambda: "telegram")
        self.assertFalse(service._is_qq_event(event))

    async def test_llonebot_friend_list_is_queried_without_no_cache(self) -> None:
        bot = LLOneBotBot(friend_ids=["20001"])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertIn("已向 QQ 提交给你的 10 个赞请求", result.reply)
        self.assertEqual(bot.calls_of("get_friend_list"), [{}])
        self.assertEqual(bot.likes, [(20001, 10)])

    async def test_llonebot_missing_send_like_is_classified_as_unsupported(self) -> None:
        bot = LLOneBotBot(friend_ids=["20001"], supports_send_like=False)
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertTrue(result.handled)
        self.assertIn("不支持名片点赞接口", result.reply)

    async def test_action_failed_wording_is_used_for_classification(self) -> None:
        # ActionFailed 的 str() 只有 retcode，真实原因在 .result.wording 里。
        bot = LLOneBotBot(
            friend_ids=["20001"],
            send_like_error=FakeActionFailed(1200, "点赞失败 今日同一好友点赞数已达上限"),
        )
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService(self._config())

        result = await service.handle_message(event, event.message_str)

        self.assertIn("已达上限", result.reply)
        self.assertNotIn("已向 QQ 提交", result.reply)

    async def test_classify_failure_handles_llonebot_missing_api_message(self) -> None:
        self.assertEqual(
            QQProfileLikeService._classify_failure("send_like API 不存在"),
            "unsupported",
        )
        self.assertEqual(
            QQProfileLikeService._classify_failure(
                "当前 OneBot 实现不支持 send_like（user:send_like）：send_like API 不存在"
            ),
            "unsupported",
        )
        self.assertEqual(
            QQProfileLikeService._classify_failure(
                "ActionFailed(retcode=1404)",
                FakeActionFailed(1404, "send_like API 不存在"),
            ),
            "unsupported",
        )


if __name__ == "__main__":
    unittest.main()
