from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import astrbot.api.message_components as Comp
from astrbot.api.event import MessageEventResult
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints

from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.qq_like_service import QQProfileLikeService


class FakeBot:
    """模拟 LLOneBot：get_friend_list 无参数，send_like 只认 user_id / times。"""

    def __init__(
        self,
        *,
        failure: Exception | None = None,
        response: dict | None = None,
    ) -> None:
        self.likes: list[tuple[int, int]] = []
        self.like_attempts: list[tuple[int, int]] = []
        self.failure = failure
        self.response = response

    async def get_version_info(self):
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
        }

    async def get_friend_list(self):
        return {"status": "ok", "retcode": 0, "data": [{"user_id": "20001"}]}

    async def send_like(self, *, user_id: int, times: int):
        assert (user_id, times) == (20001, 10)
        self.like_attempts.append((user_id, times))
        if self.failure is not None:
            raise self.failure
        if self.response is not None:
            return self.response
        self.likes.append((user_id, times))
        return {"status": "ok", "retcode": 0, "data": None}


class FakeEvent:
    def __init__(
        self,
        original_text: str = "赞我",
        *,
        group_id: str = "",
        messages: list[object] | None = None,
        bot: FakeBot | None = None,
    ) -> None:
        # WakingStage may rewrite this body, but routing must use the original.
        self.message_str = "赞我"
        self.group_id = group_id
        self.unified_msg_origin = (
            f"default:GroupMessage:{group_id}"
            if group_id
            else "default:FriendMessage:20001"
        )
        self.message_obj = SimpleNamespace(
            message_str=original_text,
            message=messages if messages is not None else [Comp.Plain(original_text)],
        )
        self.bot = bot if bot is not None else FakeBot()
        self._extras: dict[str, str] = {}
        self.is_at_or_wake_command = False
        # AstrBot uses True to DISABLE its default LLM request.
        self.call_llm = False
        self.llm_flags: list[bool] = []
        self.stopped = False
        self.stop_calls = 0
        self.request_llm = Mock(
            side_effect=AssertionError("点赞必须复用默认人设链路，不能直接请求 LLM")
        )

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_platform_id(self) -> str:
        return "qq-test"

    def get_sender_id(self) -> str:
        return "20001"

    def get_self_id(self) -> str:
        return "10001"

    def get_group_id(self) -> str:
        return self.group_id

    def is_private_chat(self) -> bool:
        return not self.group_id

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_extra(self, key: str, default: str = "") -> str:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: str) -> None:
        self._extras[key] = value

    def is_stopped(self) -> bool:
        return self.stopped

    def should_call_llm(self, value: bool) -> None:
        self.llm_flags.append(value)
        self.call_llm = value

    def stop_event(self) -> None:
        self.stop_calls += 1
        self.stopped = True

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(text)


class QQProfileLikePersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()

    def make_plugin(
        self,
        prefixes: list[str] | None = None,
        *,
        like_settings: dict | None = None,
    ) -> HelperToolsPlugin:
        settings = {
            "enabled": True,
            "cooldown_seconds": 0,
            # 默认仍为空；成功用例必须显式配置，不能悄悄恢复内置触发词。
            "trigger_phrases": ["赞我"],
            "persona_reply": {"enabled": True},
        }
        settings.update(like_settings or {})
        config = {"qq_like": settings}
        plugin = object.__new__(HelperToolsPlugin)
        plugin.config = config
        plugin.context = SimpleNamespace(
            get_config=Mock(
                return_value={"wake_prefix": ["/"] if prefixes is None else prefixes}
            ),
            llm_generate=AsyncMock(
                side_effect=AssertionError("点赞处理不能直接调用 LLM")
            ),
        )
        plugin.qq_like = QQProfileLikeService(config)
        # Wrap real routing and handling: this is an integration test of both.
        plugin.qq_like.match_message = Mock(wraps=plugin.qq_like.match_message)
        plugin.qq_like.handle_message = AsyncMock(wraps=plugin.qq_like.handle_message)
        plugin.qq_like.attach_persona_context = Mock(
            wraps=plugin.qq_like.attach_persona_context
        )
        plugin.qq_like._persona_context = Mock(wraps=plugin.qq_like._persona_context)
        plugin.wake = SimpleNamespace(is_llm_request_blocked=Mock(return_value=False))
        plugin.wallpaper = SimpleNamespace(handle_message=AsyncMock(return_value=None))
        plugin.steam = SimpleNamespace(
            match_message=Mock(return_value=SimpleNamespace(handled=False))
        )
        plugin.voice = SimpleNamespace(should_handle_message=Mock(return_value=False))
        return plugin

    async def dispatch(
        self, plugin: HelperToolsPlugin, event: FakeEvent
    ) -> list[MessageEventResult]:
        original = event.message_obj.message_str
        routed = event.message_str
        messages = event.get_messages()
        segments = list(messages)
        results = [
            item
            async for item in HelperToolsPlugin.dynamic_message_handler(plugin, event)
        ]
        self.assertEqual(event.message_obj.message_str, original)
        self.assertEqual(event.message_str, routed)
        self.assertIs(event.get_messages(), messages)
        self.assertEqual(event.get_messages(), segments)
        event.request_llm.assert_not_called()
        plugin.context.llm_generate.assert_not_awaited()
        return results

    def assert_mode_call(
        self, plugin: HelperToolsPlugin, event: FakeEvent, *, persona: bool
    ) -> None:
        plugin.qq_like.handle_message.assert_awaited_once_with(
            event, "赞我", use_persona_reply=persona
        )
        self.assertTrue(
            any(
                call.args[0] is event
                for call in plugin.qq_like.match_message.call_args_list
            )
        )
        plugin.context.get_config.assert_called_with(event.unified_msg_origin)
        plugin.wallpaper.handle_message.assert_not_awaited()
        plugin.steam.match_message.assert_not_called()
        plugin.voice.should_handle_message.assert_not_called()

    async def assert_temporary_context(
        self, plugin: HelperToolsPlugin, event: FakeEvent, *, fact: str = "10 个赞请求"
    ) -> None:
        existing = TextPart(text="本轮已有内容")
        request = SimpleNamespace(extra_user_content_parts=[existing])
        await HelperToolsPlugin.qq_like_persona_context_handler(plugin, event, request)
        self.assertEqual(len(request.extra_user_content_parts), 2)
        self.assertIs(request.extra_user_content_parts[0], existing)
        part = request.extra_user_content_parts[1]
        self.assertIsInstance(part, TextPart)
        self.assertIn("QQ 名片点赞动作已经由插件完成", part.text)
        self.assertIn(fact, part.text)
        self.assertTrue(getattr(part, "_no_save", False))
        persisted = dump_messages_with_checkpoints(
            [Message(role="user", content=request.extra_user_content_parts)]
        )
        self.assertEqual(
            persisted[0]["content"], [{"type": "text", "text": existing.text}]
        )

        # The same hook cannot duplicate it, and another request cannot inherit it.
        await HelperToolsPlugin.qq_like_persona_context_handler(plugin, event, request)
        self.assertEqual(len(request.extra_user_content_parts), 2)
        next_request = SimpleNamespace(extra_user_content_parts=[])
        await HelperToolsPlugin.qq_like_persona_context_handler(
            plugin, event, next_request
        )
        self.assertEqual(next_request.extra_user_content_parts, [])
        next_event = FakeEvent("普通聊天", group_id=event.group_id)
        await HelperToolsPlugin.qq_like_persona_context_handler(
            plugin, next_event, next_request
        )
        self.assertEqual(next_request.extra_user_content_parts, [])
        self.assertEqual(plugin.qq_like.take_persona_context(event), "")

    async def assert_no_context(
        self, plugin: HelperToolsPlugin, event: FakeEvent
    ) -> None:
        plugin.qq_like.attach_persona_context.assert_not_called()
        request = SimpleNamespace(extra_user_content_parts=[])
        await HelperToolsPlugin.qq_like_persona_context_handler(plugin, event, request)
        self.assertEqual(request.extra_user_content_parts, [])
        self.assertEqual(plugin.qq_like.take_persona_context(event), "")

    async def assert_fixed_reply(
        self,
        plugin: HelperToolsPlugin,
        event: FakeEvent,
        results: list[MessageEventResult],
        *,
        reply: str = "已向 QQ 提交给你的 10 个赞请求。",
        stop: bool = True,
    ) -> None:
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].chain), 1)
        self.assertIsInstance(results[0].chain[0], Comp.Plain)
        self.assertEqual(results[0].chain[0].text, reply)
        self.assertTrue(event.call_llm)
        self.assertEqual(event.llm_flags, [True])
        self.assertEqual(event.stop_calls, int(stop))
        await self.assert_no_context(plugin, event)

    async def assert_ignored(self, plugin: HelperToolsPlugin, event: FakeEvent) -> None:
        wake_flag = event.is_at_or_wake_command
        llm_flag = event.call_llm
        self.assertEqual(await self.dispatch(plugin, event), [])
        self.assertEqual(event.bot.likes, [])
        self.assertEqual(event.bot.like_attempts, [])
        plugin.qq_like.handle_message.assert_not_awaited()
        self.assertEqual(event.is_at_or_wake_command, wake_flag)
        self.assertEqual(event.call_llm, llm_flag)
        self.assertEqual(event.llm_flags, [])
        self.assertEqual(event.stop_calls, 0)
        await self.assert_no_context(plugin, event)

    async def test_bare_private_and_plain_group_keep_persona_temporary(self) -> None:
        for prefixes in (["/"], [], ["!", "/"]):
            for group_id in ("", "123"):
                with self.subTest(prefixes=prefixes, group_id=group_id):
                    plugin = self.make_plugin(prefixes)
                    event = FakeEvent(group_id=group_id)
                    event.message_str = "唤醒阶段改写的文本"
                    self.assertEqual(await self.dispatch(plugin, event), [])
                    self.assertEqual(event.bot.likes, [(20001, 10)])
                    self.assert_mode_call(plugin, event, persona=True)
                    self.assertTrue(event.is_at_or_wake_command)
                    self.assertFalse(event.call_llm)
                    self.assertEqual(event.llm_flags, [])
                    self.assertEqual(event.stop_calls, 0)
                    await self.assert_temporary_context(plugin, event)

    async def test_bare_self_at_and_self_reply_use_persona(self) -> None:
        for prefixes in (["/"], []):
            for address in (
                Comp.At(qq="10001"),
                Comp.Reply(id="previous", sender_id="10001"),
            ):
                with self.subTest(prefixes=prefixes, address=address):
                    plugin = self.make_plugin(prefixes)
                    event = FakeEvent(
                        group_id="123", messages=[address, Comp.Plain("赞我")]
                    )
                    self.assertEqual(await self.dispatch(plugin, event), [])
                    self.assertEqual(event.bot.likes, [(20001, 10)])
                    self.assert_mode_call(plugin, event, persona=True)
                    self.assertTrue(event.is_at_or_wake_command)
                    self.assertFalse(event.call_llm)
                    self.assertEqual(event.stop_calls, 0)
                    await self.assert_temporary_context(plugin, event)

    async def test_real_prefix_always_uses_fixed_reply_and_disables_default_llm(
        self,
    ) -> None:
        for prefix in ("/", "!", "AstrBot "):
            for group_id in ("", "123"):
                for persona_enabled in (True, False):
                    for stop in (True, False):
                        with self.subTest(
                            prefix=prefix,
                            group_id=group_id,
                            persona_enabled=persona_enabled,
                            stop=stop,
                        ):
                            plugin = self.make_plugin(
                                [prefix],
                                like_settings={
                                    "persona_reply": {"enabled": persona_enabled},
                                    "stop_after_response": stop,
                                },
                            )
                            event = FakeEvent(prefix + "赞我", group_id=group_id)
                            results = await self.dispatch(plugin, event)
                            self.assertEqual(event.bot.likes, [(20001, 10)])
                            self.assert_mode_call(plugin, event, persona=False)
                            plugin.qq_like._persona_context.assert_not_called()
                            self.assertFalse(event.is_at_or_wake_command)
                            self.assertEqual(event.is_stopped(), stop)
                            await self.assert_fixed_reply(
                                plugin, event, results, stop=stop
                            )

    async def test_prefix_is_fixed_even_with_self_at_or_reply(self) -> None:
        for address in (
            Comp.At(qq="10001"),
            Comp.At(qq="all"),
            Comp.AtAll(),
            Comp.Reply(id="previous", sender_id="10001"),
            Comp.Reply(id="previous", sender_id="20002"),
            Comp.Reply(id="previous"),
        ):
            with self.subTest(address=address):
                plugin = self.make_plugin()
                event = FakeEvent(
                    "/赞我",
                    group_id="123",
                    messages=[address, Comp.Plain("/赞我")],
                )
                results = await self.dispatch(plugin, event)
                self.assertEqual(event.bot.likes, [(20001, 10)])
                self.assert_mode_call(plugin, event, persona=False)
                plugin.qq_like._persona_context.assert_not_called()
                await self.assert_fixed_reply(plugin, event, results)

    async def test_persona_switch_off_keeps_bare_likes_with_fixed_reply(self) -> None:
        for settings in ({"enabled": False}, {}):
            for group_id in ("", "123"):
                for stop in (True, False):
                    with self.subTest(settings=settings, group_id=group_id, stop=stop):
                        plugin = self.make_plugin(
                            like_settings={
                                "persona_reply": settings,
                                "stop_after_response": stop,
                            }
                        )
                        event = FakeEvent(group_id=group_id)
                        results = await self.dispatch(plugin, event)
                        self.assertEqual(event.bot.likes, [(20001, 10)])
                        self.assert_mode_call(plugin, event, persona=True)
                        plugin.qq_like._persona_context.assert_not_called()
                        self.assertFalse(event.is_at_or_wake_command)
                        await self.assert_fixed_reply(plugin, event, results, stop=stop)

    async def test_missing_persona_setting_defaults_to_fixed_reply(self) -> None:
        plugin = self.make_plugin()
        del plugin.config["qq_like"]["persona_reply"]
        event = FakeEvent(group_id="123")
        results = await self.dispatch(plugin, event)
        self.assertEqual(event.bot.likes, [(20001, 10)])
        self.assert_mode_call(plugin, event, persona=True)
        plugin.qq_like._persona_context.assert_not_called()
        await self.assert_fixed_reply(plugin, event, results)

    async def test_empty_or_missing_triggers_disable_both_modes(self) -> None:
        for missing in (False, True):
            for original in ("赞我", "/赞我"):
                for group_id in ("", "123"):
                    with self.subTest(
                        missing=missing, original=original, group_id=group_id
                    ):
                        plugin = self.make_plugin(
                            like_settings={
                                "trigger_phrases": [],
                                "mention_trigger_prefixes": [],
                            }
                        )
                        if missing:
                            del plugin.config["qq_like"]["trigger_phrases"]
                            del plugin.config["qq_like"]["mention_trigger_prefixes"]
                        await self.assert_ignored(
                            plugin, FakeEvent(original, group_id=group_id)
                        )

    async def test_module_and_global_switches_disable_both_modes(self) -> None:
        for scope in ("qq_like", "general"):
            for original in ("赞我", "/赞我"):
                with self.subTest(scope=scope, original=original):
                    plugin = self.make_plugin()
                    plugin.config.setdefault(scope, {})["enabled"] = False
                    await self.assert_ignored(
                        plugin, FakeEvent(original, group_id="123")
                    )

    async def test_private_group_permissions_and_whitelist_are_preserved(self) -> None:
        cases = [
            ("private_disabled", "", {"allow_private_messages": False}),
            ("group_disabled", "123", {"allow_group_messages": False}),
            ("outside_whitelist", "123", {"group_whitelist": ["456"]}),
        ]
        for name, group_id, settings in cases:
            for original in ("赞我", "/赞我"):
                with self.subTest(name=name, original=original):
                    plugin = self.make_plugin(like_settings=settings)
                    await self.assert_ignored(
                        plugin, FakeEvent(original, group_id=group_id)
                    )

        for original, persona in (("赞我", True), ("/赞我", False)):
            with self.subTest(allowed=True, original=original):
                plugin = self.make_plugin(
                    like_settings={
                        "allow_private_messages": False,
                        "group_whitelist": ["123"],
                    }
                )
                event = FakeEvent(original, group_id="123")
                results = await self.dispatch(plugin, event)
                self.assertEqual(event.bot.likes, [(20001, 10)])
                self.assert_mode_call(plugin, event, persona=persona)
                if persona:
                    self.assertEqual(results, [])
                    await self.assert_temporary_context(plugin, event)
                else:
                    await self.assert_fixed_reply(plugin, event, results)

    async def test_bare_other_addresses_are_rejected_despite_rewritten_wake(
        self,
    ) -> None:
        addresses = [
            Comp.At(qq="10002"),
            Comp.At(qq="all"),
            Comp.AtAll(),
            Comp.Reply(id="previous", sender_id="20002"),
            Comp.Reply(id="previous", sender_id="10002"),
            Comp.Reply(id="previous"),
            Comp.Reply(id="previous", message_str="/赞我"),
        ]
        for address in addresses:
            with self.subTest(address=address):
                plugin = self.make_plugin()
                event = FakeEvent(
                    group_id="123", messages=[address, Comp.Plain("赞我")]
                )
                event.message_str = "/赞我"
                event.is_at_or_wake_command = True
                await self.assert_ignored(plugin, event)
                plugin.wallpaper.handle_message.assert_not_awaited()
                self.assertEqual(
                    plugin.steam.match_message.call_args.kwargs,
                    {"wake_triggered": False, "allow_commands": False},
                )
                self.assertEqual(
                    plugin.voice.should_handle_message.call_args.kwargs,
                    {"wake_triggered": False, "allow_commands": False},
                )

    async def test_leading_other_bot_rejects_even_real_prefix(self) -> None:
        for leading in (
            [],
            [Comp.Reply(id="previous")],
            [Comp.Reply(id="previous"), Comp.Plain(" ")],
        ):
            with self.subTest(leading=leading):
                plugin = self.make_plugin()
                event = FakeEvent(
                    "/赞我",
                    group_id="123",
                    messages=[*leading, Comp.At(qq="10002"), Comp.Plain("/赞我")],
                )
                event.is_at_or_wake_command = True
                await self.assert_ignored(plugin, event)
                plugin.wallpaper.handle_message.assert_not_awaited()
                self.assertEqual(
                    plugin.steam.match_message.call_args.kwargs,
                    {"wake_triggered": False, "allow_commands": False},
                )

    async def test_unconfigured_prefix_and_wake_flag_do_not_match(self) -> None:
        for original, prefixes in (
            ("!赞我", ["/"]),
            ("/赞我", ["!"]),
            ("/赞我", []),
            ("!赞我", []),
        ):
            with self.subTest(original=original, prefixes=prefixes):
                plugin = self.make_plugin(prefixes)
                event = FakeEvent(original, group_id="123")
                event.message_str = "/赞我"
                event.is_at_or_wake_command = True
                await self.assert_ignored(plugin, event)
                plugin.wallpaper.handle_message.assert_not_awaited()
                self.assertEqual(
                    plugin.steam.match_message.call_args.kwargs,
                    {"wake_triggered": False, "allow_commands": False},
                )

    async def test_current_session_profile_selects_mode_without_global_fallback(
        self,
    ) -> None:
        for group_id, original, persona in (
            ("123", "!赞我", False),
            ("123", "/赞我", None),
            ("456", "/赞我", False),
            ("456", "!赞我", None),
            ("789", "/赞我", None),
            ("123", "赞我", True),
            ("456", "赞我", True),
            ("789", "赞我", True),
        ):
            with self.subTest(group_id=group_id, original=original):
                plugin = self.make_plugin()
                profiles = {
                    "default:GroupMessage:123": ["!"],
                    "default:GroupMessage:456": ["/"],
                    "default:GroupMessage:789": [],
                }
                plugin.context.get_config = Mock(
                    side_effect=lambda origin=None, active_profiles=profiles: {
                        "wake_prefix": active_profiles.get(origin, ["/"])
                    }
                )
                event = FakeEvent(original, group_id=group_id)
                if persona is None:
                    await self.assert_ignored(plugin, event)
                else:
                    results = await self.dispatch(plugin, event)
                    self.assertEqual(event.bot.likes, [(20001, 10)])
                    self.assert_mode_call(plugin, event, persona=persona)
                    if persona:
                        self.assertEqual(results, [])
                        await self.assert_temporary_context(plugin, event)
                    else:
                        await self.assert_fixed_reply(plugin, event, results)
                plugin.context.get_config.assert_called_with(event.unified_msg_origin)
                self.assertTrue(
                    all(
                        call.args == (event.unified_msg_origin,)
                        for call in plugin.context.get_config.call_args_list
                    )
                )

    async def test_original_text_selects_mode_even_when_event_text_is_rewritten(
        self,
    ) -> None:
        for original, rewritten, persona in (
            ("赞我", "/赞我", True),
            ("赞我", "!赞我", True),
            ("赞我", "", True),
            ("/赞我", "赞我", False),
            ("/赞我", "普通聊天", False),
            ("/赞我", "", False),
            ("普通聊天", "赞我", None),
            ("普通聊天", "/赞我", None),
        ):
            with self.subTest(original=original, rewritten=rewritten):
                plugin = self.make_plugin()
                event = FakeEvent(original, group_id="123")
                event.message_str = rewritten
                if persona is None:
                    await self.assert_ignored(plugin, event)
                    continue
                results = await self.dispatch(plugin, event)
                self.assertEqual(event.bot.likes, [(20001, 10)])
                self.assert_mode_call(plugin, event, persona=persona)
                if persona:
                    self.assertEqual(results, [])
                    await self.assert_temporary_context(plugin, event)
                else:
                    await self.assert_fixed_reply(plugin, event, results)

    async def test_explicit_empty_original_never_falls_back_to_rewritten_text(
        self,
    ) -> None:
        for original in ("", "   "):
            for rewritten in ("赞我", "/赞我"):
                for group_id in ("", "123"):
                    with self.subTest(
                        original=original, rewritten=rewritten, group_id=group_id
                    ):
                        plugin = self.make_plugin()
                        event = FakeEvent(original, group_id=group_id)
                        event.message_str = rewritten
                        event.is_at_or_wake_command = True
                        await self.assert_ignored(plugin, event)
                        plugin.wallpaper.handle_message.assert_not_awaited()

    async def test_other_group_commands_still_require_actual_address(self) -> None:
        for original, prefixes, addressed in (
            ("随机壁纸", ["/"], False),
            ("!随机壁纸", ["/"], False),
            ("/随机壁纸", [], False),
            ("/随机壁纸", ["/"], True),
            ("!随机壁纸", ["!"], True),
        ):
            with self.subTest(original=original, prefixes=prefixes):
                plugin = self.make_plugin(prefixes)
                event = FakeEvent(original, group_id="123")
                event.message_str = "随机壁纸"
                event.is_at_or_wake_command = True
                self.assertEqual(await self.dispatch(plugin, event), [])
                self.assertEqual(event.bot.likes, [])
                plugin.qq_like.handle_message.assert_not_awaited()
                if addressed:
                    plugin.wallpaper.handle_message.assert_awaited_once_with(
                        event, "随机壁纸"
                    )
                else:
                    plugin.wallpaper.handle_message.assert_not_awaited()
                self.assertEqual(
                    plugin.steam.match_message.call_args.kwargs,
                    {"wake_triggered": addressed, "allow_commands": addressed},
                )
                self.assertEqual(
                    plugin.voice.should_handle_message.call_args.kwargs,
                    {"wake_triggered": addressed, "allow_commands": addressed},
                )
                self.assertEqual(event.llm_flags, [])
                self.assertEqual(event.stop_calls, 0)

    async def test_persona_cannot_override_disabled_stopped_or_wake_blocked_llm(
        self,
    ) -> None:
        for guard in ("llm_disabled", "stopped", "wake_blocked"):
            with self.subTest(guard=guard):
                plugin = self.make_plugin(like_settings={"stop_after_response": False})
                event = FakeEvent(group_id="123")
                if guard == "llm_disabled":
                    event.call_llm = True
                elif guard == "stopped":
                    event.stopped = True
                else:
                    plugin.wake.is_llm_request_blocked.return_value = True
                results = await self.dispatch(plugin, event)
                self.assertEqual(event.bot.likes, [(20001, 10)])
                self.assert_mode_call(plugin, event, persona=True)
                self.assertFalse(event.is_at_or_wake_command)
                self.assertEqual(event.is_stopped(), guard == "stopped")
                await self.assert_fixed_reply(plugin, event, results, stop=False)

    async def test_cooldown_is_shared_between_bare_and_prefix_modes(self) -> None:
        for first_text, second_text in (
            ("赞我", "赞我"),
            ("/赞我", "/赞我"),
            ("赞我", "/赞我"),
            ("/赞我", "赞我"),
        ):
            with self.subTest(first=first_text, second=second_text):
                plugin = self.make_plugin(
                    like_settings={"cooldown_seconds": 60, "stop_after_response": False}
                )
                bot = FakeBot()
                first = FakeEvent(first_text, group_id="123", bot=bot)
                second = FakeEvent(second_text, group_id="123", bot=bot)
                with patch(
                    "astrbot_plugin_helper_tools.qq_like_service.time.monotonic",
                    return_value=1000.0,
                ):
                    first_results = await self.dispatch(plugin, first)
                    if first_text == "赞我":
                        self.assertEqual(first_results, [])
                        await self.assert_temporary_context(plugin, first)
                    else:
                        await self.assert_fixed_reply(
                            plugin, first, first_results, stop=False
                        )
                    plugin.qq_like.attach_persona_context.reset_mock()
                    plugin.qq_like._persona_context.reset_mock()
                    second_results = await self.dispatch(plugin, second)
                self.assertEqual(bot.likes, [(20001, 10)])
                self.assertEqual(bot.like_attempts, [(20001, 10)])
                self.assertEqual(plugin.qq_like.handle_message.await_count, 2)
                plugin.qq_like.handle_message.assert_awaited_with(
                    second, "赞我", use_persona_reply=second_text == "赞我"
                )
                plugin.qq_like._persona_context.assert_not_called()
                await self.assert_fixed_reply(
                    plugin,
                    second,
                    second_results,
                    reply="点赞请求过于频繁，请 60 秒后再试。",
                    stop=False,
                )

    async def test_api_failures_keep_actual_facts_in_selected_reply_mode(self) -> None:
        failures = [
            ("QQ 权限限制", "无法给你点赞：QQ 权限限制了这次操作。", "失败，QQ 权限"),
            (
                "今日点赞次数已达上限",
                "无法给你点赞：今日额度或对方的可收赞次数可能已达上限。",
                "失败，今日点赞额度",
            ),
            (
                "接口暂时拒绝请求",
                "QQ 暂时没有接受给你点赞的请求，请稍后再试。",
                "失败，QQ 接口暂时拒绝请求",
            ),
        ]
        for message, reply, fact in failures:
            for original in ("赞我", "/赞我"):
                for via_exception in (False, True):
                    with self.subTest(
                        message=message, original=original, exception=via_exception
                    ):
                        bot = (
                            FakeBot(failure=RuntimeError(message))
                            if via_exception
                            else FakeBot(
                                response={
                                    "status": "failed",
                                    "retcode": "1200",
                                    "wording": message,
                                }
                            )
                        )
                        plugin = self.make_plugin()
                        event = FakeEvent(original, group_id="123", bot=bot)
                        results = await self.dispatch(plugin, event)
                        self.assertEqual(bot.likes, [])
                        self.assertEqual(bot.like_attempts, [(20001, 10)])
                        persona = original == "赞我"
                        self.assert_mode_call(plugin, event, persona=persona)
                        if persona:
                            self.assertEqual(results, [])
                            self.assertTrue(event.is_at_or_wake_command)
                            self.assertFalse(event.call_llm)
                            await self.assert_temporary_context(
                                plugin, event, fact=fact
                            )
                        else:
                            plugin.qq_like._persona_context.assert_not_called()
                            await self.assert_fixed_reply(
                                plugin, event, results, reply=reply
                            )

    async def test_handle_message_context_requires_both_route_and_config_switch(
        self,
    ) -> None:
        for use_persona_reply in (True, False):
            for enabled in (True, False):
                with self.subTest(route=use_persona_reply, enabled=enabled):
                    plugin = self.make_plugin(
                        like_settings={"persona_reply": {"enabled": enabled}}
                    )
                    event = FakeEvent()
                    result = await plugin.qq_like.handle_message(
                        event, "赞我", use_persona_reply=use_persona_reply
                    )
                    self.assertTrue(result.handled)
                    self.assertEqual(event.bot.likes, [(20001, 10)])
                    self.assertEqual(
                        bool(result.persona_context), use_persona_reply and enabled
                    )
                    if use_persona_reply and enabled:
                        self.assertIn("10 个赞请求", result.persona_context)
                        plugin.qq_like._persona_context.assert_called_once()
                    else:
                        plugin.qq_like._persona_context.assert_not_called()

    async def test_handle_message_persona_keyword_retains_true_default(self) -> None:
        plugin = self.make_plugin()
        event = FakeEvent()
        result = await plugin.qq_like.handle_message(event, "赞我")
        self.assertTrue(result.handled)
        self.assertEqual(event.bot.likes, [(20001, 10)])
        self.assertIn("10 个赞请求", result.persona_context)


if __name__ == "__main__":
    unittest.main()
