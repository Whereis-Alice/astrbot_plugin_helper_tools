from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_helper_tools.qq_like_service import (
    QQ_LIKE_PERSONA_CONTEXT_PREFIX,
    QQProfileLikeService,
)


class FakeBot:
    def __init__(self, *, friend_ids: list[str] | None = None, failure: Exception | None = None) -> None:
        self.friend_ids = friend_ids or []
        self.failure = failure
        self.likes: list[tuple[int, int]] = []
        self.friend_list_calls = 0

    async def get_friend_list(self):
        self.friend_list_calls += 1
        return [{"user_id": user_id} for user_id in self.friend_ids]

    async def send_like(self, *, user_id: int, times: int):
        if self.failure is not None:
            raise self.failure
        self.likes.append((user_id, times))
        return {}


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
        self.assertIn("已给你点了 10 个赞", result.reply)
        self.assertEqual(bot.friend_list_calls, 1)

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
        self.assertIn("已成功点赞 10 次", consumed)
        self.assertEqual(service.take_persona_context(event), "")

    async def test_disabled_module_does_not_handle_messages(self) -> None:
        bot = FakeBot(friend_ids=["20001"])
        event = FakeEvent(text="赞我", bot=bot)
        service = QQProfileLikeService({"qq_like": {"enabled": False}})

        result = await service.handle_message(event, event.message_str)

        self.assertFalse(result.handled)
        self.assertEqual(bot.likes, [])


if __name__ == "__main__":
    unittest.main()
