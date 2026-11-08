from __future__ import annotations

import asyncio
import copy
import unittest
from typing import Any
from unittest.mock import patch

from astrbot_plugin_helper_tools.qq_status import (
    USER_STATUS_ACTION,
    QQUserStatus,
    query_user_status,
    strip_profile_status_fields,
)


class StatusBot:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class QQUserStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_specific_query_decodes_wrapped_current_status(self) -> None:
        bot = StatusBot({"status": "ok", "retcode": 0, "data": {"status": 10, "ext_status": 1028}})

        result = await query_user_status(bot, "20001")

        self.assertEqual(bot.calls, [(USER_STATUS_ACTION, {"user_id": 20001})])
        text = "\n".join(result.render())
        self.assertIn("QQ在线状态: 在线", text)
        self.assertIn("QQ展示状态: 听歌中", text)
        self.assertIn("状态查询时间:", text)
        self.assertIn("仅代表协议返回", text)
        self.assertIn("不能证明", text)

    async def test_all_basic_codes_are_reported_as_protocol_observations(self) -> None:
        for code, expected in ((10, "在线"), (30, "离开"), (40, "隐身"),
                               (50, "忙碌"), (60, "Q我吧"), (70, "请勿打扰")):
            with self.subTest(code=code):
                status = await query_user_status(StatusBot({"status": code, "ext_status": 0}), "20001")
                self.assertIn(f"QQ在线状态: {expected}", status.render())

    async def test_invalid_and_bot_health_payloads_do_not_become_user_presence(self) -> None:
        for payload in (
            None, [], {}, {"online": True, "good": True},
            {"status": "ok", "retcode": 0},
            {"status": "ok", "retcode": 0, "data": None},
            {"status": True}, {"status": False}, {"status": 10.0},
            {"status": 0}, {"status": -1}, {"status": 20}, {"status": 9999},
            {"status": "在线"}, {"status": "10.0"},
        ):
            with self.subTest(payload=payload):
                status = await query_user_status(StatusBot(payload), "20001")
                self.assertIsNone(status.status)
                self.assertTrue(status.render()[0].startswith("QQ在线状态: 未知"))
                self.assertNotIn("状态来源:", "\n".join(status.render()))

    async def test_numeric_strings_are_supported_without_coercing_arbitrary_types(self) -> None:
        status = await query_user_status(StatusBot({"status": "10", "ext_status": "1028"}), "20001")
        self.assertIn("QQ展示状态: 听歌中", status.render())

    async def test_failure_payload_and_exception_have_no_profile_fallback(self) -> None:
        for payload in (
            RuntimeError("unknown action"),
            {"status": "failed", "retcode": 1404, "data": {"status": 10, "ext_status": 1028}},
            {"status": "ok", "retcode": 1200, "message": "query failed", "data": {"status": 10}},
            {"data": {"status": "failed", "retcode": 1404, "data": {"status": 10, "ext_status": 1028}}},
            {"status": "ok", "retcode": 0, "data": {"status": "failed", "retcode": 1200, "data": {"status": 10}}},
        ):
            with self.subTest(payload=payload):
                bot = StatusBot(payload)
                status = await query_user_status(bot, "20001")
                self.assertIsNone(status.status)
                self.assertEqual([name for name, _ in bot.calls], [USER_STATUS_ACTION])

    async def test_wrong_account_response_is_rejected(self) -> None:
        status = await query_user_status(StatusBot({"user_id": 20002, "status": 10}), "20001")
        self.assertIsNone(status.status)
        self.assertIn("不一致", status.render()[0])

    async def test_unknown_extension_and_incompatible_base_do_not_invent_wording(self) -> None:
        for code, ext in ((10, None), (10, 0), (10, 999999), (10, True), (40, 1028)):
            with self.subTest(code=code, ext=ext):
                status = await query_user_status(StatusBot({"status": code, "ext_status": ext}), "20001")
                self.assertTrue(status.render()[1].startswith("QQ展示状态: 未知"))

    async def test_each_query_fetches_new_status_instead_of_reusing_a_previous_user(self) -> None:
        bot = StatusBot({"status": 10, "ext_status": 1028})
        first = await query_user_status(bot, "20001")
        bot.payload = {"status": 40, "ext_status": 0}
        second = await query_user_status(bot, "20002")
        self.assertEqual((first.status, second.status), (10, 40))
        self.assertEqual([p["user_id"] for _, p in bot.calls], [20001, 20002])

    async def test_status_timeout_does_not_block_profile_and_cancels_the_pending_call(self) -> None:
        cancelled = asyncio.Event()

        class SlowBot:
            async def nc_get_user_status(self, **_params: Any) -> Any:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with patch("astrbot_plugin_helper_tools.qq_status.USER_STATUS_TIMEOUT_SECONDS", 0.01):
            result = await query_user_status(SlowBot(), "20001")
        self.assertIsNone(result.status)
        self.assertIn("超时", result.unavailable_reason)
        self.assertTrue(cancelled.is_set())

    async def test_task_cancellation_is_propagated(self) -> None:
        with self.assertRaises(asyncio.CancelledError):
            await query_user_status(StatusBot(asyncio.CancelledError()), "20001")


class RawProfileStatusTests(unittest.TestCase):
    def test_removes_stale_aliases_recursively_and_preserves_non_status_fields(self) -> None:
        payload = {
            "nickname": "Alice", "long_nick": "保留签名", "vip_status": "active",
            "custom_badge": "保留扩展", "status": 40, "extStatus": 0,
            "customStatus": {"faceId": 5, "wording": "旧的 ICU 状态"},
            "customStatusDescInfo": "旧的 ICU 状态", "online_status": "busy",
            "is_online": False, "batteryStatus": 23,
            "profile": {"custom_status_desc": "旧的天命状态", "email": "keep@example.test"},
            "userInfo": [{"presence": "away", "diyStatus": "旧的自定义状态", "qq_level": 60}],
        }
        original = copy.deepcopy(payload)

        filtered = strip_profile_status_fields(payload)

        self.assertEqual(filtered, {
            "nickname": "Alice", "long_nick": "保留签名", "vip_status": "active",
            "custom_badge": "保留扩展", "profile": {"email": "keep@example.test"},
            "userInfo": [{"qq_level": 60}],
        })
        self.assertEqual(payload, original)

    def test_unqueried_status_never_uses_a_default_online_state(self) -> None:
        self.assertIn("QQ在线状态: 未知（未单独查询用户状态）", QQUserStatus().render())
