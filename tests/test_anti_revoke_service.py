from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_helper_tools.anti_revoke_service import (
    AntiRevokeService,
    extract_event_payload,
    is_group_recall,
    sanitize_recall_message,
)
from astrbot_plugin_helper_tools.onebot_compat import (
    call_onebot,
    reset_compat_caches,
)

TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed（LLOneBot 失败时抛出的异常）。"""

    def __init__(self, retcode: int, message: str) -> None:
        super().__init__(f"retcode={retcode} message={message}")
        self.retcode = retcode
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": message,
        }


LLONEBOT_VERSION_INFO = {
    "status": "ok",
    "retcode": 0,
    "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def get_group_info(self, *, group_id: int) -> dict[str, object]:
        return {"group_id": group_id, "group_name": "测试群"}

    async def get_group_member_info(
        self,
        *,
        group_id: int,
        user_id: int,
    ) -> dict[str, object]:
        return {"group_id": group_id, "user_id": user_id, "card": f"用户{user_id}"}

    async def send_group_msg(self, *, group_id: int, message: object) -> dict[str, object]:
        self.sent.append(("group", {"group_id": group_id, "message": message}))
        return {"status": "ok", "retcode": 0}


class PreDeleteCaptureBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[int] = []

    async def get_msg(self, **params: object) -> dict[str, object]:
        message_id = int(params.get("message_id") or params.get("id") or 0)
        if message_id != 456:
            return {}
        return {
            "data": {
                "message_id": message_id,
                "group_id": 123,
                "user_id": 10001,
                "time": 1700000000,
                "sender": {"user_id": 10001, "card": "机器人"},
                "message": [
                    {"type": "text", "data": {"text": "马上被撤回的图片"}},
                    {
                        "type": "image",
                        "data": {"file": f"base64://{TINY_PNG_BASE64}"},
                    },
                ],
            }
        }

    async def delete_msg(self, *, message_id: int) -> dict[str, object]:
        self.deleted.append(message_id)
        return {"status": "ok", "retcode": 0}


class RejectFirstSendBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def send_group_msg(self, *, group_id: int, message: object) -> dict[str, object]:
        self.send_attempts += 1
        if self.send_attempts == 1:
            return {"status": "failed", "retcode": 100}
        return await super().send_group_msg(group_id=group_id, message=message)


class FreshMessageBot(FakeBot):
    async def get_msg(self, **_params: object) -> dict[str, object]:
        return {
            "data": {
                "message": [
                    {
                        "type": "image",
                        "data": {"file": f"base64://{TINY_PNG_BASE64}"},
                    }
                ]
            }
        }


class AlternateImageParamBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.image_params: list[str] = []

    async def get_msg(self, **_params: object) -> dict[str, object]:
        return {}

    async def get_image(self, *, file_id: str) -> dict[str, object]:
        self.image_params.append(file_id)
        return {"data": {"base64": TINY_PNG_BASE64}}


class LLOneBotImageBot(FakeBot):
    """LLOneBot：get_image 只认 file_id，返回 file/url/file_size/file_name。"""

    def __init__(self) -> None:
        super().__init__()
        self.image_calls: list[dict[str, object]] = []

    async def get_version_info(self) -> dict[str, object]:
        return LLONEBOT_VERSION_INFO

    async def get_msg(self, **_params: object) -> dict[str, object]:
        raise FakeActionFailed(1200, "消息不存在")

    async def get_image(self, **params: object) -> dict[str, object]:
        self.image_calls.append(dict(params))
        if not params.get("file_id"):
            raise FakeActionFailed(1400, "缺少参数 file_id")
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "file": "/llonebot/cache/nonexistent-original.png",
                "url": "https://gchat.qpic.cn/llonebot-image",
                "file_size": 68,
                "file_name": "llonebot-image.png",
            },
        }

    async def get_file(self, **_params: object) -> dict[str, object]:
        raise FakeActionFailed(1404, "get_file API 不存在")


class LLOneBotGroupFileBot(FakeBot):
    """LLOneBot：get_image / get_file 都不可用，只能靠 get_group_file_url。"""

    def __init__(self) -> None:
        super().__init__()
        self.group_file_calls: list[dict[str, object]] = []

    async def get_version_info(self) -> dict[str, object]:
        return LLONEBOT_VERSION_INFO

    async def get_msg(self, **_params: object) -> dict[str, object]:
        raise FakeActionFailed(1200, "消息不存在")

    async def get_image(self, **_params: object) -> dict[str, object]:
        raise FakeActionFailed(1404, "get_image API 不存在")

    async def get_file(self, **_params: object) -> dict[str, object]:
        raise FakeActionFailed(1404, "get_file API 不存在")

    async def get_group_file_url(self, **params: object) -> dict[str, object]:
        self.group_file_calls.append(dict(params))
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"url": "https://gchat.qpic.cn/llonebot-group-file"},
        }


class ExpiredCacheBot(FakeBot):
    """LLOneBot 的 msgCacheExpire 默认 120 秒，过期后 get_msg 直接抛异常。"""

    def __init__(self) -> None:
        super().__init__()
        self.get_msg_calls = 0

    async def get_version_info(self) -> dict[str, object]:
        return LLONEBOT_VERSION_INFO

    async def get_msg(self, **_params: object) -> dict[str, object]:
        self.get_msg_calls += 1
        raise FakeActionFailed(1200, "消息不存在或已过期")


class CallActionOnlyBot:
    """只暴露 call_action 的适配器（没有 delete_msg / get_msg 属性）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[int] = []
        self.actions: list[str] = []

    async def call_action(self, action: str, **params: object) -> dict[str, object]:
        self.actions.append(action)
        if action == "get_version_info":
            return LLONEBOT_VERSION_INFO
        if action == "get_msg":
            message_id = params.get("message_id") or params.get("id")
            if str(message_id) != "456":
                raise FakeActionFailed(1200, "消息不存在")
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 456,
                    "group_id": 123,
                    "user_id": 10001,
                    "time": 1700000000,
                    "sender": {"user_id": 10001, "card": "机器人"},
                    "message": [
                        {"type": "text", "data": {"text": "马上被撤回的图片"}},
                        {
                            "type": "image",
                            "data": {"file": f"base64://{TINY_PNG_BASE64}"},
                        },
                    ],
                },
            }
        if action == "delete_msg":
            self.deleted.append(int(str(params.get("message_id"))))
            return {"status": "ok", "retcode": 0}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}}
        if action == "get_group_member_info":
            return {"status": "ok", "retcode": 0, "data": {"card": "机器人"}}
        if action in {"send_group_msg", "send_private_msg"}:
            self.sent.append((action, dict(params)))
            return {"status": "ok", "retcode": 0}
        raise FakeActionFailed(1404, f"{action} API 不存在")


class FakeImageComponent:
    type = SimpleNamespace(name="Image")

    def __init__(self, *, file: str = "", url: str = "", converted: str = "") -> None:
        self.file = file
        self.url = url
        self.path = ""
        self.converted = converted

    async def convert_to_base64(self) -> str:
        if not self.converted:
            raise ValueError("no converted image")
        return self.converted


class FakeEvent:
    def __init__(
        self,
        raw_message: dict[str, object],
        bot: FakeBot,
        components: list[object] | None = None,
    ) -> None:
        self.message_obj = SimpleNamespace(raw_message=raw_message)
        self.bot = bot
        self._components = components or []

    def get_group_id(self) -> str:
        return str(self.message_obj.raw_message.get("group_id") or "")

    def get_messages(self) -> list[object]:
        return self._components


class AntiRevokeServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 清空实现探测与变体命中缓存，避免用例之间互相串味。
        reset_compat_caches()

    def tearDown(self) -> None:
        reset_compat_caches()

    def test_sanitize_recall_message_replaces_active_reply_segments(self) -> None:
        message = [
            {"type": "reply", "data": {"id": "12345"}},
            {"type": "text", "data": {"text": "hello"}},
        ]

        sanitized = sanitize_recall_message(message)

        self.assertEqual(
            sanitized,
            [
                {"type": "text", "data": {"text": "\u3010\u5f15\u7528\u6d88\u606f\u3011"}},
                {"type": "text", "data": {"text": "hello"}},
            ],
        )
        self.assertEqual(
            sanitize_recall_message("[CQ:reply,id=12345]hello"),
            "\u3010\u5f15\u7528\u6d88\u606f\u3011hello",
        )

    def test_extracts_wrapped_payload_and_detects_recall(self) -> None:
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                raw_message={
                    "data": {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": 456,
                    }
                }
            )
        )

        payload = extract_event_payload(event)

        self.assertEqual(payload["notice_type"], "group_recall")
        self.assertTrue(is_group_recall(payload))

    async def test_caches_and_restores_original_onebot_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                        "cache_expiration_seconds": 300,
                    }
                },
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "time": 1700000000,
                "user_id": 789,
                "sender": {"user_id": 789, "card": "Alice"},
                "message": [
                    {"type": "text", "data": {"text": "会撤回吗"}},
                    {"type": "image", "data": {"file": "image-id"}},
                ],
            }
            await service.handle_event(FakeEvent(original, bot))

            recall = {
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "operator_id": 789,
            }
            await service.handle_event(FakeEvent(recall, bot))

            self.assertEqual(len(bot.sent), 1)
            sent_message = bot.sent[0][1]["message"]
            self.assertEqual(bot.sent[0][1]["group_id"], 999)
            self.assertIsInstance(sent_message, list)
            assert isinstance(sent_message, list)
            self.assertIn("撤回消息", str(sent_message[0]))
            self.assertEqual(sent_message[1:], original["message"])

    async def test_pre_delete_hook_caches_bot_message_before_qqadmin_style_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = PreDeleteCaptureBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            # Any normal group event installs the OneBot deletion interceptor.
            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 123,
                        "message_id": 1,
                        "user_id": 10001,
                        "message": "install hook",
                    },
                    bot,
                )
            )

            result = await bot.delete_msg(message_id=456)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(bot.deleted, [456])
            record = service._records["123:456"]
            self.assertEqual(record.sender_name, "机器人")
            self.assertEqual(set(record.image_cache_files), {"1"})

            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": 456,
                        "user_id": 10001,
                        "operator_id": 10001,
                    },
                    bot,
                )
            )

            self.assertEqual(len(bot.sent), 1)
            restored = bot.sent[0][1]["message"]
            self.assertIsInstance(restored, list)
            assert isinstance(restored, list)
            self.assertEqual(restored[2]["type"], "image")
            self.assertTrue(restored[2]["data"]["file"].startswith("base64://"))

    async def test_recalled_reply_does_not_send_expired_quote_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "sender": {"user_id": 789, "card": "Alice"},
                "message": [
                    {"type": "reply", "data": {"id": "111"}},
                    {"type": "face", "data": {"id": "128"}},
                ],
            }
            await service.handle_event(FakeEvent(original, bot))
            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": 456,
                        "user_id": 789,
                        "operator_id": 789,
                    },
                    bot,
                )
            )

            sent_message = bot.sent[0][1]["message"]
            self.assertIsInstance(sent_message, list)
            assert isinstance(sent_message, list)
            self.assertEqual(
                sent_message[1],
                {"type": "text", "data": {"text": "\u3010\u5f15\u7528\u6d88\u606f\u3011"}},
            )
            self.assertEqual(sent_message[2], {"type": "face", "data": {"id": "128"}})

    async def test_recalled_image_uses_snapshot_after_original_reference_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [
                    {"type": "text", "data": {"text": "图片会被撤回"}},
                    {
                        "type": "image",
                        "data": {"file": f"base64://{TINY_PNG_BASE64}"},
                    },
                ],
            }
            await service.handle_event(FakeEvent(original, bot))

            record = service._records["123:456"]
            self.assertEqual(set(record.image_cache_files), {"1"})
            cached_path = service.cache_dir / record.image_cache_files["1"]
            self.assertTrue(cached_path.is_file())
            record.message[1]["data"]["file"] = "expired-image-reference"

            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": 456,
                        "user_id": 789,
                        "operator_id": 789,
                    },
                    bot,
                )
            )

            sent_message = bot.sent[0][1]["message"]
            self.assertIsInstance(sent_message, list)
            assert isinstance(sent_message, list)
            image_segment = sent_message[2]
            self.assertEqual(image_segment["type"], "image")
            restored_ref = image_segment["data"]["file"]
            self.assertTrue(restored_ref.startswith("base64://"))
            self.assertEqual(
                base64.b64decode(restored_ref.removeprefix("base64://")),
                base64.b64decode(TINY_PNG_BASE64),
            )

    async def test_astrbot_image_component_supplies_snapshot_when_raw_file_is_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [{"type": "image", "data": {"file": "opaque-image-id"}}],
            }
            component = FakeImageComponent(converted=TINY_PNG_BASE64)

            await service.handle_event(FakeEvent(original, bot, [component]))

            record = service._records["123:456"]
            self.assertEqual(set(record.image_cache_files), {"0"})
            self.assertTrue((service.cache_dir / record.image_cache_files["0"]).is_file())

    async def test_astrbot_image_component_is_added_when_raw_payload_has_no_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [{"type": "text", "data": {"text": "图片"}}],
            }
            component = FakeImageComponent(file=f"base64://{TINY_PNG_BASE64}")

            await service.handle_event(FakeEvent(original, bot, [component]))

            record = service._records["123:456"]
            self.assertEqual(len(record.message), 2)
            self.assertEqual(record.message[1]["type"], "image")
            self.assertEqual(set(record.image_cache_files), {"1"})

    async def test_get_msg_refreshes_image_reference_after_initial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FreshMessageBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [{"type": "image", "data": {"file": "expired-image"}}],
            }

            await service.handle_event(FakeEvent(original, bot))

            record = service._records["123:456"]
            self.assertEqual(set(record.image_cache_files), {"0"})
            self.assertTrue(record.message[0]["data"]["file"].startswith("base64://"))

    async def test_get_image_compatibility_tries_file_id_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = AlternateImageParamBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [{"type": "image", "data": {"file": "opaque.png"}}],
            }

            await service.handle_event(FakeEvent(original, bot))

            record = service._records["123:456"]
            self.assertEqual(set(record.image_cache_files), {"0"})
            self.assertEqual(bot.image_params, ["opaque.png"])

    async def test_qq_image_download_uses_qzone_referer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = AntiRevokeService({}, Path(temp_dir))
            image_bytes = base64.b64decode(TINY_PNG_BASE64)
            fetch = AsyncMock(return_value=(image_bytes, "image/png"))

            with patch(
                "astrbot_plugin_helper_tools.anti_revoke_service.fetch_bytes",
                fetch,
            ):
                await service._read_image_reference("https://gchat.qpic.cn/example")

            self.assertEqual(fetch.await_args.kwargs["headers"]["Referer"], "https://qzone.qq.com/")

    async def test_failed_image_snapshot_writes_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [{"type": "image", "data": {"file": "unresolved-image"}}],
            }

            with patch(
                "astrbot_plugin_helper_tools.anti_revoke_service.logger.warning"
            ) as warning:
                await service.handle_event(FakeEvent(original, bot))

            formats = [clean_call.args[0] for clean_call in warning.call_args_list]
            self.assertTrue(any("image snapshot incomplete" in value for value in formats))

    async def test_failed_full_restore_keeps_cached_image_in_single_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = RejectFirstSendBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [
                    {"type": "text", "data": {"text": "混合消息"}},
                    {
                        "type": "image",
                        "data": {"file": f"base64://{TINY_PNG_BASE64}"},
                    },
                    {"type": "video", "data": {"file": "expired-video"}},
                ],
            }
            await service.handle_event(FakeEvent(original, bot))
            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": 456,
                        "user_id": 789,
                        "operator_id": 789,
                    },
                    bot,
                )
            )

            self.assertEqual(bot.send_attempts, 2)
            self.assertEqual(len(bot.sent), 1)
            sent_message = bot.sent[0][1]["message"]
            self.assertIsInstance(sent_message, list)
            assert isinstance(sent_message, list)
            self.assertIn("已恢复图片", sent_message[0]["data"]["text"])
            self.assertEqual(len([item for item in sent_message if item["type"] == "image"]), 1)

    async def test_custom_target_commands_are_persisted_and_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = AntiRevokeService(
                {"anti_revoke": {"target_groups": ["999"]}},
                Path(temp_dir),
            )

            self.assertIn("已添加", service.add_forward_target("123", "@456"))
            self.assertEqual(service._targets_for_group("123"), [("private", "456")])
            self.assertIn("按群设置", service.list_forward_targets())
            self.assertIn("已取消", service.remove_forward_target("123", "@456"))
            self.assertEqual(service._targets_for_group("123"), [("group", "999")])

    async def test_ignored_operator_does_not_send(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = FakeBot()
            config = {
                "anti_revoke": {
                    "enabled": True,
                    "target_groups": ["999"],
                    "ignore_operators": ["100"],
                }
            }
            service = AntiRevokeService(config, Path(temp_dir))
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": "hello",
            }
            await service.handle_event(FakeEvent(original, bot))
            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "notice",
                        "notice_type": "group_recall",
                        "group_id": 123,
                        "message_id": 456,
                        "user_id": 789,
                        "operator_id": 100,
                    },
                    bot,
                )
            )

            self.assertEqual(bot.sent, [])

    async def test_llonebot_get_image_shape_is_resolved_through_compat_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = LLOneBotImageBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [
                    {"type": "image", "data": {"file": "opaque-llonebot-id"}}
                ],
            }
            image_bytes = base64.b64decode(TINY_PNG_BASE64)
            fetch = AsyncMock(return_value=(image_bytes, "image/png"))

            with patch(
                "astrbot_plugin_helper_tools.anti_revoke_service.fetch_bytes",
                fetch,
            ):
                await service.handle_event(FakeEvent(original, bot))

            # file 参数被拒绝（1400）后兼容层自动换到 file_id。
            self.assertEqual(
                bot.image_calls,
                [{"file": "opaque-llonebot-id"}, {"file_id": "opaque-llonebot-id"}],
            )
            # LLOneBot 只返回 file/url/file_size/file_name，必须能取到 url。
            self.assertEqual(
                fetch.await_args.args[0], "https://gchat.qpic.cn/llonebot-image"
            )
            record = service._records["123:456"]
            self.assertEqual(set(record.image_cache_files), {"0"})
            self.assertTrue((service.cache_dir / record.image_cache_files["0"]).is_file())

    async def test_unsupported_actions_fall_back_to_group_file_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = LLOneBotGroupFileBot()
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True}},
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [
                    {"type": "image", "data": {"file": "group-opaque-id"}}
                ],
            }
            image_bytes = base64.b64decode(TINY_PNG_BASE64)
            fetch = AsyncMock(return_value=(image_bytes, "image/png"))

            with patch(
                "astrbot_plugin_helper_tools.anti_revoke_service.fetch_bytes",
                fetch,
            ):
                await service.handle_event(FakeEvent(original, bot))

            self.assertEqual(
                bot.group_file_calls,
                [{"group_id": 123, "file_id": "group-opaque-id"}],
            )
            self.assertEqual(
                fetch.await_args.args[0], "https://gchat.qpic.cn/llonebot-group-file"
            )
            record = service._records["123:456"]
            self.assertEqual(set(record.image_cache_files), {"0"})

    async def test_expired_message_lookup_degrades_without_user_visible_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = ExpiredCacheBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": "缓存已过期的文本",
            }
            event = FakeEvent(original, bot)

            # get_msg 抛异常（LLOneBot 查不到消息就是抛异常）时静默返回空字典。
            self.assertEqual(await service._lookup_onebot_message(event, "456"), {})
            self.assertGreaterEqual(bot.get_msg_calls, 1)

            with patch(
                "astrbot_plugin_helper_tools.anti_revoke_service.logger.error"
            ) as error:
                await service.handle_event(event)
                await service.handle_event(
                    FakeEvent(
                        {
                            "post_type": "notice",
                            "notice_type": "group_recall",
                            "group_id": 123,
                            "message_id": 456,
                            "user_id": 789,
                            "operator_id": 789,
                        },
                        bot,
                    )
                )

            error.assert_not_called()
            self.assertEqual(len(bot.sent), 1)
            self.assertIn("缓存已过期的文本", str(bot.sent[0][1]["message"]))

    async def test_delete_msg_interception_covers_compat_attribute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = PreDeleteCaptureBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 123,
                        "message_id": 1,
                        "user_id": 10001,
                        "message": "install hook",
                    },
                    bot,
                )
            )

            # call_onebot 的"属性优先"路径拿到的必须是被包装过的 delete_msg。
            result = await call_onebot(bot, "delete_msg", message_id=456)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(bot.deleted, [456])
            record = service._records["123:456"]
            self.assertEqual(record.sender_name, "机器人")
            self.assertEqual(set(record.image_cache_files), {"1"})

    async def test_delete_msg_interception_covers_call_action_only_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = CallActionOnlyBot()
            service = AntiRevokeService(
                {
                    "anti_revoke": {
                        "enabled": True,
                        "target_groups": ["999"],
                    }
                },
                Path(temp_dir),
            )
            await service.handle_event(
                FakeEvent(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 123,
                        "message_id": 1,
                        "user_id": 10001,
                        "message": "install hook",
                    },
                    bot,
                )
            )

            # 没有 delete_msg 属性时 call_onebot 会回退到 call_action，拦截同样生效。
            result = await call_onebot(bot, "delete_msg", message_id=456)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(bot.deleted, [456])
            self.assertIn("get_msg", bot.actions)
            record = service._records["123:456"]
            self.assertEqual(record.sender_name, "机器人")
            self.assertEqual(set(record.image_cache_files), {"1"})


if __name__ == "__main__":
    unittest.main()
