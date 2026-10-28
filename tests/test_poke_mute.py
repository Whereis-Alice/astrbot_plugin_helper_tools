from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_helper_tools.poke_service import PokeService
from astrbot_plugin_helper_tools.tests.test_poke_service import (
    FakeBot,
    FakeContext,
    FakeEvent,
    command_only_config,
)

LEGACY_FAILURE_PROMPT = (
    "{username} 戳了你一下，（{reason}）。你因为你不是管理员或者身份比这人低而无法禁言这人，"
    "请你用一句话吐槽一下，考虑上下文。"
)


class PokeMuteTests(unittest.IsolatedAsyncioTestCase):
    def service(self, **settings) -> PokeService:
        config = command_only_config()
        config["poke"]["command_reply"]["weight"] = 0
        config["poke"]["mute_reply"] = {
            "weight": 100,
            "duration_seconds": 60,
            "random_delta_seconds": 0,
            **settings,
        }
        return PokeService(config, Path("."), FakeContext())

    async def test_role_matrix_describes_exact_limit_and_only_calls_allowed_bans(self) -> None:
        cases = (
            ("admin", "admin", "双方都是管理员", False),
            ("admin", "owner", "对方是群主", False),
            ("member", "owner", "对方是群主", False),
            ("owner", "owner", "对方是群主", False),
            ("member", "admin", "对方是管理员", False),
            ("member", "member", "不是群管理员", False),
            ("admin", "member", "禁言接口已确认成功", True),
            ("owner", "admin", "禁言接口已确认成功", True),
            ("owner", "member", "禁言接口已确认成功", True),
            (" ADMIN ", " ADMIN ", "双方都是管理员", False),
        )
        for bot_role, target_role, reason, should_ban in cases:
            with self.subTest(bot_role=bot_role, target_role=target_role):
                bot = FakeBot()
                bot.bot_role, bot.target_role = bot_role, target_role
                # AstrBot's global-admin flag must not replace the QQ group role.
                event = FakeEvent.poke_notice(bot=bot)
                event.role = "admin"
                response = await self.service().handle_event(event)
                self.assertEqual(response.action, "mute_reply")
                self.assertIn(reason, response.llm_prompt)
                self.assertIn("Bot QQ=10001", response.llm_prompt)
                self.assertIn("戳你的人（QQ=20001）", response.llm_prompt)
                self.assertNotIn("身份比这人低", response.llm_prompt)
                self.assertEqual(bot.bans, [(30001, 20001, 60)] if should_ban else [])

    async def test_legacy_default_is_corrected_without_replacing_custom_config(self) -> None:
        bot = FakeBot()
        bot.target_role = "admin"
        service = self.service(failure_prompt_template=LEGACY_FAILURE_PROMPT)
        response = await service.handle_event(FakeEvent.poke_notice(bot=bot))
        self.assertIn("双方都是管理员", response.llm_prompt)
        self.assertNotIn("你因为你不是管理员或者身份比这人低", response.llm_prompt)
        self.assertEqual(service.config["poke"]["mute_reply"]["failure_prompt_template"], LEGACY_FAILURE_PROMPT)

    async def test_custom_template_receives_roles_and_facts_even_without_reason_placeholder(self) -> None:
        bot = FakeBot()
        bot.target_role = "owner"
        template = "按人设回复 {username}；你是{bot_role}，对方是{target_role}。"
        service = self.service(failure_prompt_template=template)
        response = await service.handle_event(FakeEvent.poke_notice(bot=bot))
        self.assertIn("按人设回复 Alice；你是管理员，对方是群主。", response.llm_prompt)
        self.assertIn("对方是群主，无法禁言群主", response.llm_prompt)
        self.assertEqual(service.config["poke"]["mute_reply"]["failure_prompt_template"], template)
        self.assertEqual(bot.bans, [])

    async def test_wrapped_roles_are_fresh_and_not_inferred_from_notice_sender(self) -> None:
        bot = FakeBot()
        requests = []

        async def member_info(*, group_id, user_id, no_cache):
            requests.append((group_id, user_id, no_cache))
            return {"status": "ok", "retcode": 0, "data": {"role": "admin"}}

        bot.get_group_member_info = member_info
        event = FakeEvent.poke_notice(bot=bot)
        event.message_obj.raw_message["sender"] = {"role": "owner"}
        response = await self.service().handle_event(event)
        self.assertCountEqual(requests, [(30001, 10001, True), (30001, 20001, True)])
        self.assertIn("双方都是管理员", response.llm_prompt)
        self.assertEqual(bot.bans, [])

    async def test_unknown_or_failed_roles_never_authorize_ban(self) -> None:
        for bad_info in ({}, {"role": "unexpected"}, {"status": "failed", "retcode": 1200, "data": {"role": "member"}}):
            for failed_user in (10001, 20001):
                with self.subTest(bad_info=bad_info, failed_user=failed_user):
                    bot = FakeBot()

                    async def member_info(*, group_id, user_id, no_cache, failed_id=failed_user, response=bad_info):
                        if user_id == failed_id:
                            return response
                        return {"role": "admin" if user_id == 10001 else "member"}

                    bot.get_group_member_info = member_info
                    response = await self.service().handle_event(FakeEvent.poke_notice(bot=bot))
                    self.assertIn("未能确认", response.llm_prompt)
                    self.assertIn("本次未执行禁言", response.llm_prompt)
                    self.assertEqual(bot.bans, [])

    async def test_confirmed_owner_is_still_named_when_bot_role_query_fails(self) -> None:
        bot = FakeBot()

        async def member_info(*, group_id, user_id, no_cache):
            if user_id == 10001:
                raise RuntimeError("member lookup failed")
            return {"role": "owner"}

        bot.get_group_member_info = member_info
        response = await self.service().handle_event(FakeEvent.poke_notice(bot=bot))
        self.assertIn("对方是群主", response.llm_prompt)
        self.assertIn("群身份=未能确认", response.llm_prompt)
        self.assertEqual(bot.bans, [])

    async def test_failed_ban_payload_is_not_reported_as_success_or_low_role(self) -> None:
        bot = FakeBot()
        bot.set_group_ban = AsyncMock(return_value={"status": "failed", "retcode": 1200, "message": "ban failed"})
        response = await self.service().handle_event(FakeEvent.poke_notice(bot=bot))
        bot.set_group_ban.assert_awaited_once_with(group_id=30001, user_id=20001, duration=60)
        self.assertIn("调用 QQ 禁言接口失败", response.llm_prompt)
        self.assertIn("群身份=管理员", response.llm_prompt)
        self.assertNotIn("随后被禁言", response.llm_prompt)
        self.assertNotIn("不是群管理员", response.llm_prompt)

    async def test_ban_timeout_describes_unknown_execution_without_retry(self) -> None:
        bot = FakeBot()
        bot.set_group_ban = AsyncMock(side_effect=TimeoutError())
        response = await self.service().handle_event(FakeEvent.poke_notice(bot=bot))
        self.assertIn("响应超时，无法确认是否执行成功", response.llm_prompt)
        self.assertNotIn("随后被禁言", response.llm_prompt)
        self.assertEqual(bot.set_group_ban.await_count, 1)

    async def test_role_lookup_has_bounded_timeout(self) -> None:
        bot = FakeBot()
        bot.get_group_member_info = AsyncMock(side_effect=TimeoutError())
        with patch("astrbot_plugin_helper_tools.poke_service.asyncio.wait_for", wraps=asyncio.wait_for) as bounded:
            response = await self.service().handle_event(FakeEvent.poke_notice(bot=bot))
        self.assertEqual([call.kwargs["timeout"] for call in bounded.call_args_list], [3, 3])
        self.assertIn("未能确认机器人", response.llm_prompt)
        self.assertEqual(bot.bans, [])


if __name__ == "__main__":
    unittest.main()
