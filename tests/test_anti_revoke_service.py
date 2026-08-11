from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_helper_tools.anti_revoke_service import (
    AntiRevokeService,
    extract_event_payload,
    is_group_recall,
    sanitize_recall_message,
)

TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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


class RejectFirstSendBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def send_group_msg(self, *, group_id: int, message: object) -> dict[str, object]:
        self.send_attempts += 1
        if self.send_attempts == 1:
            return {"status": "failed", "retcode": 100}
        return await super().send_group_msg(group_id=group_id, message=message)


class FakeEvent:
    def __init__(self, raw_message: dict[str, object], bot: FakeBot) -> None:
        self.message_obj = SimpleNamespace(raw_message=raw_message)
        self.bot = bot

    def get_group_id(self) -> str:
        return str(self.message_obj.raw_message.get("group_id") or "")


class AntiRevokeServiceTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
