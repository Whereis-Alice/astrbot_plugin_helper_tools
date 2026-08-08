from __future__ import annotations

import unittest
from types import SimpleNamespace

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import Message, TextPart

from astrbot_plugin_helper_tools.main import (
    HelperToolsPlugin,
    _mark_temporary_tool_results,
)
from astrbot_plugin_helper_tools.wake_service import (
    EMPTY_WAKE_PROMPT_MARKER,
    WakeService,
)


class FakeEvent:
    def __init__(self, *, text: str, chain: list[object]) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(message=chain, message_str=text)
        self.unified_msg_origin = "default:GroupMessage:30003"
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.call_llm = False
        self._stopped = False
        self._extras: dict[str, object] = {}

    @staticmethod
    def get_sender_id() -> str:
        return "10001"

    @staticmethod
    def get_group_id() -> str:
        return "30003"

    @staticmethod
    def get_self_id() -> str:
        return "20002"

    @staticmethod
    def get_platform_name() -> str:
        return "aiocqhttp"

    @staticmethod
    def is_admin() -> bool:
        return False

    def get_messages(self) -> list[object]:
        return self.message_obj.message

    def get_extra(self, key: str, default: object = None) -> object:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: object) -> None:
        self._extras[key] = value

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


def make_service(
    *,
    context: object | None = None,
    **wake_overrides: object,
) -> WakeService:
    wake = {
        "enabled": True,
        "block_enabled": True,
        "wake_cd": 0,
        "block_qqbot": False,
        "block_reread": False,
        "block_keywords": [],
        "block_keywords_initialized": True,
        "command_block_enabled": False,
        "debounce_enabled": False,
        "at_wake_enabled": True,
        "empty_wake_response_enabled": True,
    }
    wake.update(wake_overrides)
    return WakeService({"wake": wake}, context=context)


class EmptyWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pure_bot_mention_gets_temporary_prompt(self) -> None:
        service = make_service(empty_wake_prompt="自定义无文字唤醒提示")
        event = FakeEvent(text="", chain=[Comp.At(qq="20002")])

        result = await service.apply(event)
        injected = service.inject_empty_wake_prompt(event)

        self.assertEqual(result, "wake")
        self.assertTrue(injected)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertIn(EMPTY_WAKE_PROMPT_MARKER, event.message_str)
        self.assertIn("自定义无文字唤醒提示", event.message_str)
        self.assertEqual(event.message_obj.message_str, event.message_str)

    async def test_pure_wake_word_works_for_every_matching_mode(self) -> None:
        for mode in ("自由触发", "前缀触发", "后缀触发"):
            with self.subTest(mode=mode):
                service = make_service(
                    wake_words=["爱丽丝"],
                    trigger_modes=[mode],
                    strip_prefix_suffix_wake_word=True,
                )
                event = FakeEvent(
                    text="爱丽丝",
                    chain=[Comp.Plain("爱丽丝")],
                )

                result = await service.apply(event)
                injected = service.inject_empty_wake_prompt(event)

                self.assertEqual(result, "wake")
                self.assertTrue(injected)
                self.assertIn(EMPTY_WAKE_PROMPT_MARKER, event.message_str)

    async def test_pure_astrbot_wake_prefix_gets_the_same_prompt(self) -> None:
        context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["爱丽丝"]})
        service = make_service(context=context)
        event = FakeEvent(
            text="",
            chain=[Comp.Plain("爱丽丝")],
        )
        event.is_at_or_wake_command = True
        event.is_wake = True

        result = await service.apply(event)
        injected = service.inject_empty_wake_prompt(event)

        self.assertEqual(result, "wake")
        self.assertTrue(injected)
        self.assertIn(EMPTY_WAKE_PROMPT_MARKER, event.message_str)

    async def test_disabled_empty_wake_response_stops_pure_mention(self) -> None:
        service = make_service(empty_wake_response_enabled=False)
        event = FakeEvent(text="", chain=[Comp.At(qq="20002")])

        result = await service.apply(event)

        self.assertEqual(result, "empty_wake_disabled")
        self.assertTrue(event.is_stopped())
        self.assertFalse(event.is_at_or_wake_command)

    async def test_private_pure_mention_uses_only_the_empty_wake_compatibility(self) -> None:
        service = make_service()
        event = FakeEvent(text="", chain=[Comp.At(qq="20002")])
        event.get_group_id = lambda: ""  # type: ignore[method-assign]
        event.unified_msg_origin = "default:FriendMessage:10001"

        result = await service.apply(event)
        injected = service.inject_empty_wake_prompt(event)

        self.assertEqual(result, "private_bypassed")
        self.assertTrue(injected)
        self.assertIn(EMPTY_WAKE_PROMPT_MARKER, event.message_str)

    async def test_late_handler_injects_after_other_message_handlers(self) -> None:
        service = make_service()
        event = FakeEvent(text="", chain=[Comp.At(qq="20002")])
        await service.apply(event)
        plugin = SimpleNamespace(enabled=lambda: True, wake=service)

        await HelperToolsPlugin.empty_wake_prompt_handler(plugin, event)

        self.assertIn(EMPTY_WAKE_PROMPT_MARKER, event.message_str)

    def test_empty_wake_prompt_is_not_saved_to_future_history(self) -> None:
        message = Message(
            role="user",
            content=[TextPart(text=f"{EMPTY_WAKE_PROMPT_MARKER}\n临时提示")],
        )
        run_context = SimpleNamespace(messages=[message])

        marked = _mark_temporary_tool_results(run_context)

        self.assertEqual(marked, 1)
        self.assertTrue(message._no_save)
        self.assertTrue(message.content[0]._no_save)


if __name__ == "__main__":
    unittest.main()
