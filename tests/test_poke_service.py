from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.poke_service import (
    POKE_SYNTHETIC_COMMAND_EXTRA,
    PokeCooldown,
    PokeNotice,
    PokeService,
    is_poke_synthetic_command,
    mark_poke_agent_messages_temporary,
    mark_poke_persona_reply,
    mark_synthetic_command_messages_temporary,
    normalize_poke_cron_expression,
)


class FakeQueue:
    def __init__(self) -> None:
        self.items: list[object] = []

    def put_nowait(self, event: object) -> None:
        self.items.append(event)


class FakeContext:
    def __init__(self, *, ignore_self_messages: bool = False) -> None:
        self.queue = FakeQueue()
        self.ignore_self_messages = ignore_self_messages
        self.cron_manager = FakeCronManager()

    def get_event_queue(self) -> FakeQueue:
        return self.queue

    def get_config(self, _umo: str | None = None) -> dict[str, object]:
        return {
            "wake_prefix": ["/"],
            "platform_settings": {
                "ignore_bot_self_message": self.ignore_self_messages,
            },
        }


class FakeCronManager:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []

    async def list_jobs(self, _kind: str = "basic") -> list[object]:
        return []

    async def add_basic_job(self, **kwargs: object) -> dict[str, str]:
        self.add_calls.append(kwargs)
        return {"job_id": "poke-job"}

    async def delete_job(self, _job_id: str) -> None:
        return None


class FakeBot:
    def __init__(self) -> None:
        self.pokes: list[tuple[str, int, int | None]] = []
        self.bans: list[tuple[int, int, int]] = []
        self.bot_role = "admin"
        self.target_role = "member"

    async def group_poke(self, *, group_id: int, user_id: int) -> None:
        self.pokes.append(("group", user_id, group_id))

    async def friend_poke(self, *, user_id: int) -> None:
        self.pokes.append(("private", user_id, None))

    async def get_group_member_info(self, *, group_id: int, user_id: int):
        role = self.bot_role if user_id == 10001 else self.target_role
        return {"group_id": group_id, "user_id": user_id, "role": role}

    async def set_group_ban(
        self,
        *,
        group_id: int,
        user_id: int,
        duration: int,
    ) -> None:
        self.bans.append((group_id, user_id, duration))


class FakeEvent:
    def __init__(
        self,
        message_str: str,
        message_obj: object,
        platform_meta: object,
        session_id: str,
        bot: object,
    ) -> None:
        self.message_str = message_str
        self.message_obj = message_obj
        self.platform_meta = platform_meta
        self.session_id = session_id
        self.bot = bot
        self.unified_msg_origin = f"default:GroupMessage:{session_id}"
        self.role = "member"
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.call_llm = False
        self.stopped = False
        self._extras: dict[str, object] = {}
        self._result = None
        self._force_stopped = False
        self._has_send_oper = False

    @classmethod
    def poke_notice(
        cls,
        *,
        bot: FakeBot | None = None,
        user_id: int = 20001,
        target_id: int = 10001,
    ) -> FakeEvent:
        raw = {
            "time": 123,
            "self_id": 10001,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": user_id,
            "target_id": target_id,
            "group_id": 30001,
        }
        message_obj = SimpleNamespace(
            type="GroupMessage",
            self_id="10001",
            sender=SimpleNamespace(user_id=str(user_id), nickname="Alice"),
            message=[],
            message_str="",
            message_id="notice-1",
            timestamp=123,
            raw_message=raw,
        )
        return cls(
            "",
            message_obj,
            SimpleNamespace(id="default", name="aiocqhttp"),
            "30001",
            bot or FakeBot(),
        )

    def get_self_id(self) -> str:
        return str(self.message_obj.raw_message.get("self_id", ""))

    def get_sender_id(self) -> str:
        return str(self.message_obj.sender.user_id)

    def get_sender_name(self) -> str:
        return str(self.message_obj.sender.nickname)

    def get_group_id(self) -> str:
        return str(self.message_obj.raw_message.get("group_id") or "")

    def get_platform_id(self) -> str:
        return "default"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_extra(self, key: str | None = None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: object) -> None:
        self._extras[key] = value

    def should_call_llm(self, value: bool) -> None:
        self.call_llm = value

    def stop_event(self) -> None:
        self.stopped = True

    def clear_result(self) -> None:
        self._result = None

    def is_admin(self) -> bool:
        return self.role == "admin"


def command_only_config() -> dict[str, object]:
    return {
        "poke": {
            "enabled": True,
            "user_cooldown_seconds": 0,
            "group_cooldown_seconds": 0,
            "antipoke": {"weight": 0},
            "llm_reply": {"weight": 0},
            "qq_face": {"weight": 0},
            "image_reply": {"weight": 0},
            "voice_reply": {"weight": 0},
            "mute_reply": {"weight": 0},
            "command_reply": {"weight": 100, "commands": ["//怒撕"]},
            "outgoing": {"interval_seconds": 0, "max_times": 2},
        }
    }


class PokeServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_uses_safe_defaults(self) -> None:
        schema_path = Path(__file__).parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        poke = schema["poke"]["items"]

        self.assertFalse(poke["enabled"]["default"])
        self.assertEqual(poke["mute_reply"]["items"]["weight"]["default"], 0)
        self.assertFalse(poke["scheduler"]["items"]["enabled"]["default"])
        self.assertEqual(poke["scheduler"]["items"]["targets"]["default"], [])
        self.assertIn(
            "怒撕",
            poke["command_reply"]["items"]["commands"]["default"],
        )

    def test_notice_parser_rejects_non_poke_payloads(self) -> None:
        event = FakeEvent.poke_notice()
        notice = PokeNotice.from_event(event)

        self.assertEqual(notice.user_id, "20001")
        self.assertEqual(notice.target_id, "10001")
        event.message_obj.raw_message["sub_type"] = "honor"
        self.assertIsNone(PokeNotice.from_event(event))

    def test_user_and_group_cooldowns_are_independent(self) -> None:
        now = [100.0]
        cooldown = PokeCooldown(clock=lambda: now[0])

        self.assertTrue(
            cooldown.allow("30001", "20001", user_seconds=10, group_seconds=2)
        )
        now[0] += 1
        self.assertFalse(
            cooldown.allow("30001", "20002", user_seconds=10, group_seconds=2)
        )
        now[0] += 2
        self.assertTrue(
            cooldown.allow("30001", "20002", user_seconds=10, group_seconds=2)
        )
        self.assertTrue(
            cooldown.allow("30002", "20001", user_seconds=10, group_seconds=2)
        )
        self.assertFalse(
            cooldown.allow("30001", "20001", user_seconds=10, group_seconds=2)
        )

    async def test_random_command_is_bot_authored_and_keeps_original_event_unchanged(
        self,
    ) -> None:
        context = FakeContext()
        service = PokeService(command_only_config(), Path("."), context)
        event = FakeEvent.poke_notice()

        response = await service.handle_event(event)

        self.assertEqual(response.action, "command_reply")
        self.assertTrue(event.stopped)
        self.assertEqual(event.get_sender_id(), "20001")
        self.assertEqual(event.message_obj.raw_message["post_type"], "notice")
        self.assertEqual(len(context.queue.items), 1)

        synthetic = context.queue.items[0]
        self.assertTrue(is_poke_synthetic_command(synthetic))
        self.assertEqual(synthetic.get_sender_id(), "10001")
        self.assertEqual(synthetic.message_str, "怒撕")
        self.assertTrue(synthetic.call_llm)
        self.assertEqual(synthetic.message_obj.raw_message["post_type"], "message")
        self.assertEqual(synthetic.message_obj.raw_message["user_id"], 10001)
        self.assertIsInstance(synthetic.get_messages()[0], Comp.Plain)
        self.assertEqual(synthetic.get_messages()[0].text, "怒撕")
        self.assertIsInstance(synthetic.get_messages()[1], Comp.At)
        self.assertEqual(str(synthetic.get_messages()[1].qq), "20001")

    async def test_self_message_ignore_mode_restores_author_before_other_handlers(
        self,
    ) -> None:
        context = FakeContext(ignore_self_messages=True)
        service = PokeService(command_only_config(), Path("."), context)
        event = FakeEvent.poke_notice()
        await service.handle_event(event)
        synthetic = context.queue.items[0]

        self.assertEqual(synthetic.get_sender_id(), "20001")
        await HelperToolsPlugin.poke_synthetic_command_identity_guard(
            SimpleNamespace(),
            synthetic,
        )

        self.assertEqual(synthetic.get_sender_id(), "10001")
        self.assertEqual(synthetic.message_obj.raw_message["user_id"], 10001)
        self.assertTrue(synthetic.call_llm)

    async def test_chat_history_capture_skips_synthetic_commands(self) -> None:
        context = FakeContext()
        service = PokeService(command_only_config(), Path("."), context)
        await service.handle_event(FakeEvent.poke_notice())
        synthetic = context.queue.items[0]
        calls: list[object] = []
        history = SimpleNamespace(
            enabled=lambda: True,
            capture_event=lambda event: calls.append(event),
        )
        plugin = SimpleNamespace(enabled=lambda: True, chat_history=history)

        await HelperToolsPlugin.chat_history_capture_handler(plugin, synthetic)

        self.assertEqual(calls, [])

    async def test_explicit_agent_receives_current_turn_command_attribution(self) -> None:
        context = FakeContext()
        service = PokeService(command_only_config(), Path("."), context)
        await service.handle_event(FakeEvent.poke_notice())
        synthetic = context.queue.items[0]
        request = SimpleNamespace(prompt="怒撕", extra_user_content_parts=[])

        await HelperToolsPlugin.poke_synthetic_command_llm_context_handler(
            SimpleNamespace(),
            synthetic,
            request,
        )

        self.assertEqual(len(request.extra_user_content_parts), 1)
        part = request.extra_user_content_parts[0]
        self.assertIsInstance(part, TextPart)
        self.assertIn("不是群成员发送", part.text)
        self.assertIn("20001", part.text)
        self.assertTrue(getattr(part, "_no_save", False))

    def test_synthetic_agent_turn_is_not_persisted(self) -> None:
        event = FakeEvent.poke_notice()
        event.set_extra(
            POKE_SYNTHETIC_COMMAND_EXTRA,
            {"self_id": "10001", "source_user_id": "20001"},
        )
        previous = Message(role="assistant", content=[TextPart(text="earlier")])
        command = Message(role="user", content=[TextPart(text="怒撕")])
        response = Message(role="assistant", content=[TextPart(text="done")])
        run_context = SimpleNamespace(messages=[previous, command, response])

        marked = mark_synthetic_command_messages_temporary(event, run_context)

        self.assertEqual(marked, 2)
        persisted = dump_messages_with_checkpoints(run_context.messages)
        self.assertEqual(persisted[0]["content"][0]["text"], "earlier")
        self.assertEqual(persisted[1]["content"], [])
        self.assertEqual(persisted[2]["content"], [])

    def test_persona_reply_prompt_and_answer_are_not_persisted(self) -> None:
        event = FakeEvent.poke_notice()
        mark_poke_persona_reply(event)
        prompt = Message(role="user", content=[TextPart(text="internal poke prompt")])
        answer = Message(role="assistant", content=[TextPart(text="persona reply")])
        run_context = SimpleNamespace(messages=[prompt, answer])

        marked = mark_poke_agent_messages_temporary(event, run_context)

        self.assertEqual(marked, 2)
        persisted = dump_messages_with_checkpoints(run_context.messages)
        self.assertEqual(persisted[0]["content"], [])
        self.assertEqual(persisted[1]["content"], [])

    async def test_tool_rejects_self_and_caps_requested_times(self) -> None:
        bot = FakeBot()
        event = FakeEvent.poke_notice(bot=bot)
        service = PokeService(command_only_config(), Path("."), FakeContext())

        self.assertIn("不能让机器人戳自己", await service.poke_from_tool(event, "10001"))
        result = await service.poke_from_tool(event, "20001", 99)

        self.assertIn("2 次", result)
        self.assertEqual(len(bot.pokes), 2)

    async def test_direct_command_caps_target_count_and_reports_omissions(self) -> None:
        bot = FakeBot()
        event = FakeEvent.poke_notice(bot=bot)
        event.message_str = "戳 20001 20002 20003"
        config = command_only_config()
        config["poke"]["outgoing"]["max_direct_targets"] = 2
        config["poke"]["outgoing"]["max_times"] = 1
        service = PokeService(config, Path("."), FakeContext())

        result = await service.handle_command(event)

        self.assertEqual(len(bot.pokes), 2)
        self.assertIn("另有 1 位", result)

    async def test_disabled_command_handler_yields_to_other_plugins(self) -> None:
        plugin = SimpleNamespace(
            enabled=lambda: True,
            poke=SimpleNamespace(
                enabled=lambda: False,
                commands_enabled=lambda: True,
            ),
        )
        event = FakeEvent.poke_notice()

        results = [
            item async for item in HelperToolsPlugin.poke_command(plugin, event)
        ]

        self.assertEqual(results, [])
        self.assertFalse(event.stopped)

    async def test_mute_checks_bot_permission_before_calling_ban(self) -> None:
        bot = FakeBot()
        bot.bot_role = "member"
        event = FakeEvent.poke_notice(bot=bot)
        service = PokeService(
            {
                "poke": {
                    "enabled": True,
                    "mute_reply": {
                        "duration_seconds": 60,
                        "random_delta_seconds": 0,
                    },
                }
            },
            Path("."),
            FakeContext(),
        )
        notice = PokeNotice.from_event(event)
        assert notice is not None

        success, reason, _duration = await service._mute_poker(event, notice)

        self.assertFalse(success)
        self.assertIn("不是群管理员", reason)
        self.assertEqual(bot.bans, [])

    async def test_scheduler_does_not_register_without_targets(self) -> None:
        context = FakeContext()
        service = PokeService(
            {"poke": {"enabled": True, "scheduler": {"enabled": True, "targets": []}}},
            Path("."),
            context,
        )

        await service.start()

        self.assertEqual(context.cron_manager.add_calls, [])

    def test_cron_normalization_accepts_astrbot_five_field_format(self) -> None:
        self.assertEqual(normalize_poke_cron_expression("30 22 * * *"), "30 22 * * *")
        self.assertEqual(normalize_poke_cron_expression("30 22 * *"), "30 22 * * *")
        self.assertEqual(normalize_poke_cron_expression("0 30 22 * * *"), "30 22 * * *")


if __name__ == "__main__":
    unittest.main()
