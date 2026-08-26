from __future__ import annotations

import asyncio
import base64
import pathlib
import tempfile
import unittest
from typing import Any

from astrbot_plugin_helper_tools import onebot_compat as compat


class FakeActionFailed(Exception):
    """Mimics aiocqhttp.exceptions.ActionFailed."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(f"ActionFailed(retcode={result.get('retcode')})")
        self.result = result

    @property
    def retcode(self) -> Any:
        return self.result.get("retcode")


def llonebot_unknown_action(action: str) -> FakeActionFailed:
    """LLOneBot answers unknown actions over reverse WS with retcode 1404."""

    return FakeActionFailed(
        {"status": "failed", "retcode": 1404, "message": f"{action} API 不存在"}
    )


class FakeBot:
    """A ``call_action``-style bot with an explicit action allow-list."""

    def __init__(
        self,
        supported: dict[str, Any] | None = None,
        *,
        version: dict[str, Any] | None = None,
        strict_params: dict[str, set[str]] | None = None,
    ) -> None:
        self.supported = dict(supported or {})
        self.strict_params = dict(strict_params or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        if version is not None:
            self.supported["get_version_info"] = version

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action not in self.supported:
            raise llonebot_unknown_action(action)
        allowed = self.strict_params.get(action)
        if allowed is not None:
            unknown = set(params) - allowed
            if unknown:
                raise FakeActionFailed(
                    {
                        "status": "failed",
                        "retcode": 1400,
                        "message": f"参数错误: {sorted(unknown)}",
                    }
                )
        result = self.supported[action]
        if callable(result):
            return result(**params)
        return result


LLONEBOT_VERSION = {
    "status": "ok",
    "retcode": 0,
    "data": {"app_name": "LLOneBot", "app_version": "8.1.9", "protocol_version": "v11"},
}


class CompatTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        compat.reset_compat_caches()

    def tearDown(self) -> None:
        compat.reset_compat_caches()


class CallOnebotTests(CompatTestCase):
    async def test_prefers_attribute_style_action(self) -> None:
        seen: list[dict[str, Any]] = []

        class AttrBot:
            async def group_poke(self, **params: Any) -> str:
                seen.append(params)
                return "attr"

            async def call_action(self, action: str, **params: Any) -> str:
                raise AssertionError("call_action must not be used here")

        result = await compat.call_onebot(AttrBot(), "group_poke", group_id=1, user_id=2)
        self.assertEqual(result, "attr")
        self.assertEqual(seen, [{"group_id": 1, "user_id": 2}])

    async def test_type_error_drops_optional_params(self) -> None:
        seen: list[dict[str, Any]] = []

        class PickyBot:
            async def get_group_info(self, *, group_id: int) -> str:
                seen.append({"group_id": group_id})
                return "ok"

        result = await compat.call_onebot(
            PickyBot(), "get_group_info", group_id=7, no_cache=True
        )
        self.assertEqual(result, "ok")
        self.assertEqual(seen, [{"group_id": 7}])

    async def test_type_error_without_optional_params_propagates(self) -> None:
        class PickyBot:
            async def get_group_info(self, *, group_id: int) -> str:
                return "ok"

        with self.assertRaises(TypeError):
            await compat.call_onebot(PickyBot(), "get_group_info", gid=7)

    async def test_missing_entrypoint_raises_runtime_error(self) -> None:
        with self.assertRaises(RuntimeError):
            await compat.call_onebot(object(), "get_msg", message_id=1)
        with self.assertRaises(RuntimeError):
            await compat.call_onebot(None, "get_msg", message_id=1)


class UnwrapPayloadTests(unittest.TestCase):
    def test_unwraps_nested_envelopes(self) -> None:
        payload = {
            "status": "ok",
            "retcode": 0,
            "data": {"status": "ok", "data": {"message_id": 42}},
        }
        self.assertEqual(compat.unwrap_payload(payload), {"message_id": 42})

    def test_unwraps_lone_data_key(self) -> None:
        # Some adapters strip status/retcode and hand back only {"data": ...}.
        self.assertEqual(compat.unwrap_payload({"data": "keep-me"}), "keep-me")

    def test_keeps_mapping_whose_data_is_a_real_field(self) -> None:
        payload = {"data": "x", "message_id": 1}
        self.assertEqual(compat.unwrap_payload(payload), payload)

    def test_non_mapping_passthrough(self) -> None:
        self.assertEqual(compat.unwrap_payload([1, 2]), [1, 2])
        self.assertIsNone(compat.unwrap_payload(None))


class ErrorClassificationTests(unittest.TestCase):
    def test_llonebot_unknown_action_is_unsupported(self) -> None:
        exc = llonebot_unknown_action("set_self_longnick")
        self.assertEqual(compat.onebot_error_info(exc)[0], 1404)
        self.assertTrue(compat.is_unsupported_action_error(exc))
        self.assertTrue(compat.is_variant_error(exc))

    def test_bad_param_retcode(self) -> None:
        exc = FakeActionFailed({"retcode": 1400, "message": "参数错误"})
        self.assertTrue(compat.is_bad_param_error(exc))
        self.assertFalse(compat.is_unsupported_action_error(exc))
        self.assertTrue(compat.is_variant_error(exc))

    def test_execution_failure_is_not_retryable(self) -> None:
        exc = FakeActionFailed({"retcode": 1200, "message": "调用失败"})
        self.assertFalse(compat.is_unsupported_action_error(exc))
        self.assertFalse(compat.is_bad_param_error(exc))
        self.assertFalse(compat.is_variant_error(exc))

    def test_wording_preferred_over_message(self) -> None:
        exc = FakeActionFailed(
            {"retcode": 1200, "message": "failed", "wording": "群不存在"}
        )
        self.assertEqual(compat.onebot_error_info(exc), (1200, "群不存在"))

    def test_retcode_parsed_from_text_when_absent(self) -> None:
        exc = RuntimeError("call failed retcode: 1404")
        self.assertEqual(compat.onebot_error_info(exc)[0], 1404)
        self.assertTrue(compat.is_unsupported_action_error(exc))

    def test_keyword_only_detection(self) -> None:
        self.assertTrue(compat.is_unsupported_action_error(RuntimeError("不支持的接口")))
        self.assertTrue(
            compat.is_unsupported_action_error(RuntimeError("Unknown action: poke"))
        )

    def test_timeout_is_never_a_variant_error(self) -> None:
        self.assertFalse(compat.is_variant_error(asyncio.TimeoutError()))
        self.assertTrue(compat.retry_including_timeout(asyncio.TimeoutError()))
        self.assertTrue(compat.retry_including_timeout(TimeoutError()))
        self.assertFalse(compat.retry_including_timeout(asyncio.CancelledError()))

    def test_type_error_counts_as_bad_param(self) -> None:
        self.assertTrue(compat.is_bad_param_error(TypeError("unexpected kwarg")))
        self.assertFalse(compat.is_unsupported_action_error(TypeError("boom")))


class ImplementationDetectionTests(CompatTestCase):
    async def test_detects_llonebot_and_caches_result(self) -> None:
        bot = FakeBot(version=LLONEBOT_VERSION)

        impl = await compat.detect_implementation(bot)
        self.assertEqual(impl.name, compat.IMPL_LLONEBOT)
        self.assertTrue(impl.is_llonebot)
        self.assertTrue(impl.is_known)
        self.assertEqual(impl.describe(), "LLOneBot 8.1.9")

        await compat.detect_implementation(bot)
        self.assertEqual([call[0] for call in bot.calls], ["get_version_info"])
        self.assertEqual(compat.cached_implementation(bot).name, compat.IMPL_LLONEBOT)

    async def test_detects_napcat(self) -> None:
        bot = FakeBot(
            version={
                "status": "ok",
                "retcode": 0,
                "data": {"app_name": "NapCat.Onebot", "app_version": "4.0.0"},
            }
        )
        impl = await compat.detect_implementation(bot)
        self.assertEqual(impl.name, compat.IMPL_NAPCAT)
        self.assertFalse(impl.is_llonebot)

    async def test_detection_failure_is_cached_as_unknown(self) -> None:
        bot = FakeBot()
        impl = await compat.detect_implementation(bot)
        self.assertFalse(impl.is_known)
        await compat.detect_implementation(bot)
        self.assertEqual(len(bot.calls), 1)

    async def test_cache_expiry_triggers_new_probe(self) -> None:
        bot = FakeBot(version=LLONEBOT_VERSION)
        await compat.detect_implementation(bot)
        compat._cache_set(
            compat._impl_cache,
            compat._impl_cache_fallback,
            bot,
            (0.0, compat.UNKNOWN_IMPLEMENTATION),
        )
        self.assertFalse(compat.cached_implementation(bot).is_known)
        impl = await compat.detect_implementation(bot)
        self.assertEqual(impl.name, compat.IMPL_LLONEBOT)
        self.assertEqual(len(bot.calls), 2)

    async def test_none_bot_is_unknown(self) -> None:
        self.assertFalse((await compat.detect_implementation(None)).is_known)
        self.assertFalse(compat.cached_implementation(None).is_known)

    def test_normalize_impl_name(self) -> None:
        self.assertEqual(compat.normalize_impl_name("LLOneBot"), compat.IMPL_LLONEBOT)
        self.assertEqual(
            compat.normalize_impl_name("NapCat.Onebot"), compat.IMPL_NAPCAT
        )
        self.assertEqual(compat.normalize_impl_name(""), compat.IMPL_UNKNOWN)
        self.assertEqual(compat.normalize_impl_name(None), compat.IMPL_UNKNOWN)


class VariantDispatchTests(CompatTestCase):
    async def test_falls_back_until_a_supported_action_answers(self) -> None:
        bot = FakeBot({"second": "ok"})
        variants = (
            compat.OneBotVariant("first", {"a": 1}),
            compat.OneBotVariant("second", {"a": 1}),
        )
        result = await compat.call_onebot_variants(bot, variants, op_key="t:fallback")
        self.assertEqual(result, "ok")
        self.assertEqual(
            [call[0] for call in bot.calls], ["get_version_info", "first", "second"]
        )

    async def test_successful_variant_index_is_cached(self) -> None:
        bot = FakeBot({"second": "ok"})
        variants = (
            compat.OneBotVariant("first", {"a": 1}),
            compat.OneBotVariant("second", {"a": 1}),
        )
        await compat.call_onebot_variants(bot, variants, op_key="t:cache")
        bot.calls.clear()
        await compat.call_onebot_variants(bot, variants, op_key="t:cache")
        self.assertEqual([call[0] for call in bot.calls], ["second"])

    async def test_non_retryable_error_is_raised_without_further_attempts(self) -> None:
        def boom(**_params: Any) -> Any:
            raise FakeActionFailed({"retcode": 1200, "message": "执行失败"})

        bot = FakeBot({"first": boom, "second": "ok"})
        variants = (
            compat.OneBotVariant("first", {}),
            compat.OneBotVariant("second", {}),
        )
        with self.assertRaises(FakeActionFailed):
            await compat.call_onebot_variants(bot, variants, op_key="t:strict")
        self.assertNotIn("second", [call[0] for call in bot.calls])

    async def test_skip_impls_are_not_attempted_on_known_impl(self) -> None:
        bot = FakeBot({"kept": "ok"}, version=LLONEBOT_VERSION)
        variants = (
            compat.OneBotVariant("skipped", {}, skip_impls=(compat.IMPL_LLONEBOT,)),
            compat.OneBotVariant("kept", {}),
        )
        result = await compat.call_onebot_variants(bot, variants, op_key="t:skip")
        self.assertEqual(result, "ok")
        self.assertNotIn("skipped", [call[0] for call in bot.calls])

    async def test_all_variants_failing_keeps_the_informative_error(self) -> None:
        """A non-"unknown action" failure must reach the caller with its retcode."""

        def bad_param(**_params: Any) -> Any:
            raise FakeActionFailed({"retcode": 1400, "message": "参数错误"})

        bot = FakeBot({"second": bad_param})
        variants = (
            compat.OneBotVariant("first", {}),
            compat.OneBotVariant("second", {}),
        )
        with self.assertRaises(FakeActionFailed) as ctx:
            await compat.call_onebot_variants(bot, variants, op_key="t:allfail")
        self.assertEqual(compat.onebot_error_info(ctx.exception)[0], 1400)

    async def test_all_actions_unknown_raises_readable_runtime_error(self) -> None:
        bot = FakeBot()
        variants = (
            compat.OneBotVariant("first", {}),
            compat.OneBotVariant("second", {}),
        )
        with self.assertRaises(RuntimeError) as ctx:
            await compat.call_onebot_variants(bot, variants, op_key="t:unknown")
        text = str(ctx.exception)
        self.assertIn("first/second", text)
        self.assertIn("t:unknown", text)
        self.assertIn("不支持", text)
        self.assertIsInstance(ctx.exception.__cause__, FakeActionFailed)
        # Downstream classifiers rely on the wrapped message staying recognizable.
        self.assertTrue(compat.is_unsupported_action_error(ctx.exception))

    async def test_empty_variants_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await compat.call_onebot_variants(FakeBot(), (), op_key="t:empty")

    async def test_deadline_skips_detection(self) -> None:
        from time import monotonic

        bot = FakeBot({"first": "ok"}, version=LLONEBOT_VERSION)
        result = await compat.call_onebot_variants(
            bot,
            (compat.OneBotVariant("first", {}),),
            op_key="t:deadline",
            deadline=monotonic() + 5.0,
        )
        self.assertEqual(result, "ok")
        self.assertEqual([call[0] for call in bot.calls], ["first"])

    async def test_exhausted_deadline_raises(self) -> None:
        from time import monotonic

        bot = FakeBot({"first": "ok"})
        with self.assertRaises(Exception):
            await compat.call_onebot_variants(
                bot,
                (compat.OneBotVariant("first", {}),),
                op_key="t:deadline2",
                deadline=monotonic() - 1.0,
            )
        self.assertEqual(bot.calls, [])


class LogicalOperationTests(CompatTestCase):
    async def test_group_poke_uses_group_poke_on_llonebot(self) -> None:
        bot = FakeBot(
            {"group_poke": {"status": "ok", "retcode": 0, "data": None}},
            version=LLONEBOT_VERSION,
            strict_params={"group_poke": {"group_id", "user_id"}},
        )
        await compat.send_poke(bot, user_id=222, group_id=111)
        action, params = bot.calls[-1]
        self.assertEqual(action, "group_poke")
        self.assertEqual(params, {"group_id": 111, "user_id": 222})

    async def test_private_poke_never_sends_target_id(self) -> None:
        bot = FakeBot({"friend_poke": None}, version=LLONEBOT_VERSION)
        await compat.send_poke(bot, user_id=222)
        action, params = bot.calls[-1]
        self.assertEqual(action, "friend_poke")
        self.assertEqual(params, {"user_id": 222})

    async def test_bare_poke_is_skipped_on_llonebot(self) -> None:
        bot = FakeBot(version=LLONEBOT_VERSION)
        with self.assertRaises(RuntimeError):
            await compat.send_poke(bot, user_id=222, group_id=111)
        attempted = [call[0] for call in bot.calls]
        self.assertNotIn("poke", attempted)
        self.assertNotIn("send_group_poke", attempted)
        self.assertEqual(attempted.count("group_poke"), 1)

    async def test_signature_falls_back_to_personal_note(self) -> None:
        bot = FakeBot(
            {"set_qq_profile": None},
            version=LLONEBOT_VERSION,
            strict_params={"set_qq_profile": {"nickname", "personal_note"}},
        )
        await compat.set_bot_signature(bot, "在摸鱼", nickname="小助手")
        action, params = bot.calls[-1]
        self.assertEqual(action, "set_qq_profile")
        self.assertEqual(params, {"personal_note": "在摸鱼", "nickname": "小助手"})
        self.assertNotIn("set_self_longnick", [call[0] for call in bot.calls])

    async def test_online_status_sends_all_required_params(self) -> None:
        bot = FakeBot(
            {"set_online_status": None},
            version=LLONEBOT_VERSION,
            strict_params={
                "set_online_status": {"status", "ext_status", "battery_status"}
            },
        )
        await compat.set_online_status(bot, status=10, ext_status=1027)
        action, params = bot.calls[-1]
        self.assertEqual(action, "set_online_status")
        self.assertEqual(
            params, {"status": 10, "ext_status": 1027, "battery_status": 0}
        )

    async def test_avatar_and_like_use_llonebot_action_names(self) -> None:
        bot = FakeBot(
            {"set_qq_avatar": None, "send_like": None}, version=LLONEBOT_VERSION
        )
        await compat.set_bot_avatar(bot, "file:///tmp/a.png")
        self.assertEqual(bot.calls[-1], ("set_qq_avatar", {"file": "file:///tmp/a.png"}))
        await compat.send_like(bot, user_id="123", times=10)
        self.assertEqual(bot.calls[-1], ("send_like", {"user_id": "123", "times": 10}))

    async def test_group_msg_history_uses_camel_case_on_llonebot(self) -> None:
        bot = FakeBot(
            {
                "get_group_msg_history": {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"messages": []},
                }
            },
            version=LLONEBOT_VERSION,
            strict_params={
                "get_group_msg_history": {
                    "group_id",
                    "message_seq",
                    "count",
                    "reverseOrder",
                }
            },
        )
        await compat.get_group_msg_history(bot, group_id=111, count=20, reverse=False)
        action, params = bot.calls[-1]
        self.assertEqual(action, "get_group_msg_history")
        self.assertEqual(
            params,
            {
                "group_id": 111,
                "message_seq": 0,
                "count": 20,
                "reverseOrder": False,
            },
        )

    async def test_group_msg_history_drops_reverse_for_older_impls(self) -> None:
        bot = FakeBot(
            {
                "get_group_msg_history": {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"messages": []},
                }
            },
            strict_params={"get_group_msg_history": {"group_id", "message_seq"}},
        )
        await compat.get_group_msg_history(bot, group_id=111, count=20)
        self.assertEqual(
            bot.calls[-1], ("get_group_msg_history", {"group_id": 111, "message_seq": 0})
        )


class MessageAndFileLookupTests(CompatTestCase):
    def test_message_lookup_variants_are_stable(self) -> None:
        variants = compat.message_lookup_variants("12345")
        self.assertEqual(len(variants), 4)
        self.assertEqual(variants[0].params, {"message_id": 12345})
        self.assertEqual(variants[1].params, {"message_id": "12345"})
        self.assertEqual(compat.IMPL_LLONEBOT, variants[2].skip_impls[0])
        # The tuple length must never change, otherwise cached indices rot.
        self.assertEqual(len(compat.message_lookup_variants(7)), 4)

    async def test_get_msg_prefers_int_message_id(self) -> None:
        bot = FakeBot(
            {"get_msg": {"status": "ok", "retcode": 0, "data": {"message_id": 12345}}},
            version=LLONEBOT_VERSION,
        )
        payload = await compat.get_msg(bot, "12345")
        self.assertEqual(compat.unwrap_payload(payload), {"message_id": 12345})
        self.assertEqual(bot.calls[-1], ("get_msg", {"message_id": 12345}))

    async def test_get_msg_raises_for_caller_to_degrade(self) -> None:
        bot = FakeBot(version=LLONEBOT_VERSION)
        with self.assertRaises(RuntimeError):
            await compat.get_msg(bot, 999)
        self.assertEqual(
            [params for action, params in bot.calls if action == "get_msg"],
            [{"message_id": 999}, {"message_id": "999"}],
        )

    async def test_get_msg_expired_message_id_surfaces_original_error(self) -> None:
        """LLOneBot drops its message cache after msgCacheExpire (120s by default)."""

        def expired(**_params: Any) -> Any:
            raise FakeActionFailed({"retcode": 1200, "message": "消息不存在"})

        bot = FakeBot({"get_msg": expired}, version=LLONEBOT_VERSION)
        with self.assertRaises(FakeActionFailed) as ctx:
            await compat.get_msg(bot, 999)
        self.assertEqual(compat.onebot_error_info(ctx.exception), (1200, "消息不存在"))
        # No pointless retries once the implementation gave a real answer.
        self.assertEqual(len([c for c in bot.calls if c[0] == "get_msg"]), 1)

    def test_file_lookup_variants_add_group_candidate(self) -> None:
        private = compat.file_lookup_variants("abc.jpg")
        group = compat.file_lookup_variants("abc.jpg", group_id="111")
        self.assertEqual(len(group), len(private) + 1)
        self.assertEqual(group[-1].action, "get_group_file_url")
        self.assertEqual(group[-1].params["group_id"], 111)

    async def test_resolve_file_uses_llonebot_get_image(self) -> None:
        bot = FakeBot(
            {
                "get_image": {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "file": "C:/cache/abc.jpg",
                        "url": "https://example.invalid/abc.jpg",
                        "file_size": "1024",
                        "file_name": "abc.jpg",
                    },
                }
            },
            version=LLONEBOT_VERSION,
            strict_params={"get_image": {"file"}},
        )
        payload = await compat.resolve_file(bot, "abc.jpg")
        self.assertEqual(bot.calls[-1], ("get_image", {"file": "abc.jpg"}))
        self.assertEqual(
            compat.extract_file_references(payload),
            ["https://example.invalid/abc.jpg", "C:/cache/abc.jpg", "abc.jpg"],
        )

    async def test_private_and_group_file_caches_are_separate(self) -> None:
        bot = FakeBot({"get_file": {"status": "ok", "retcode": 0, "data": {"url": "u"}}})
        await compat.resolve_file(bot, "a")
        await compat.resolve_file(bot, "a", group_id=1)
        bot.calls.clear()
        await compat.resolve_file(bot, "a")
        self.assertEqual([call[0] for call in bot.calls], ["get_file"])

    def test_extract_file_references_handles_gocq_shape(self) -> None:
        payload = {
            "status": "ok",
            "retcode": 0,
            "data": {"file": "/tmp/a.jpg", "size": 1, "filename": "a.jpg", "url": "u"},
        }
        self.assertEqual(
            compat.extract_file_references(payload), ["u", "/tmp/a.jpg", "a.jpg"]
        )

    def test_extract_file_references_edge_cases(self) -> None:
        self.assertEqual(compat.extract_file_references("  "), [])
        self.assertEqual(compat.extract_file_references("http://a"), ["http://a"])
        self.assertEqual(compat.extract_file_references(None), [])
        self.assertEqual(compat.extract_file_references([1, 2]), [])
        self.assertEqual(
            compat.extract_file_references({"data": {"url": "u", "file": "u"}}), ["u"]
        )


class PlatformDetectionTests(unittest.TestCase):
    def test_known_onebot_platform_names(self) -> None:
        for name in (
            "aiocqhttp",
            "AIOCQHTTP",
            "onebot",
            "llonebot",
            "LLOneBot",
            "luckylilliabot",
            "napcat",
            "lagrange",
        ):
            self.assertTrue(compat.is_onebot_platform(name), name)

    def test_non_onebot_platform_names(self) -> None:
        for name in ("telegram", "discord", "qqofficial", "wecom"):
            self.assertFalse(compat.is_onebot_platform(name), name)

    def test_empty_name_is_treated_as_compatible(self) -> None:
        self.assertTrue(compat.is_onebot_platform(""))
        self.assertTrue(compat.is_onebot_platform("   "))

    def test_non_string_is_rejected(self) -> None:
        self.assertFalse(compat.is_onebot_platform(None))
        self.assertFalse(compat.is_onebot_platform(123))

    def test_extra_names(self) -> None:
        self.assertFalse(compat.is_onebot_platform("my_bridge"))
        self.assertTrue(compat.is_onebot_platform("my_bridge", ["My_Bridge"]))

    def test_event_platform_name_and_event_helper(self) -> None:
        class Event:
            def __init__(self, name: Any) -> None:
                self._name = name

            def get_platform_name(self) -> Any:
                if isinstance(self._name, Exception):
                    raise self._name
                return self._name

        self.assertEqual(compat.event_platform_name(Event(" LLOneBot ")), "llonebot")
        self.assertEqual(compat.event_platform_name(Event(None)), "")
        self.assertEqual(compat.event_platform_name(Event(RuntimeError("x"))), "")
        self.assertEqual(compat.event_platform_name(object()), "")
        self.assertTrue(compat.is_onebot_event(Event("napcat")))
        self.assertFalse(compat.is_onebot_event(Event("telegram")))


class ExtraPlatformRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = compat.extra_platform_names()
        self.addCleanup(compat.register_extra_platform_names, self._saved)
        compat.register_extra_platform_names(())

    def test_registration_normalizes_and_takes_effect(self) -> None:
        registered = compat.register_extra_platform_names(
            ["  My_Bridge  ", "OTHER", "my_bridge"]
        )
        self.assertEqual(registered, frozenset({"my_bridge", "other"}))
        self.assertEqual(compat.extra_platform_names(), registered)
        self.assertTrue(compat.is_onebot_platform("MY_BRIDGE"))
        self.assertTrue(compat.is_onebot_platform("other"))

        class Event:
            def get_platform_name(self) -> str:
                return "My_Bridge"

        self.assertTrue(compat.is_onebot_event(Event()))

    def test_registration_replaces_previous_names(self) -> None:
        compat.register_extra_platform_names(["first"])
        self.assertTrue(compat.is_onebot_platform("first"))
        compat.register_extra_platform_names(["second"])
        self.assertFalse(compat.is_onebot_platform("first"))
        self.assertTrue(compat.is_onebot_platform("second"))

    def test_empty_and_invalid_entries_are_ignored(self) -> None:
        registered = compat.register_extra_platform_names(
            ["keep", "", "   ", None, 123, ["nested"]]  # type: ignore[list-item]
        )
        self.assertEqual(registered, frozenset({"keep"}))
        self.assertFalse(compat.is_onebot_platform("nested"))

    def test_none_clears_registry(self) -> None:
        compat.register_extra_platform_names(["gone"])
        self.assertEqual(compat.register_extra_platform_names(None), frozenset())
        self.assertEqual(compat.extra_platform_names(), frozenset())
        self.assertFalse(compat.is_onebot_platform("gone"))

    def test_returned_snapshot_is_immutable_view(self) -> None:
        registered = compat.register_extra_platform_names(["snapshot"])
        compat.register_extra_platform_names(["changed"])
        self.assertEqual(registered, frozenset({"snapshot"}))

    def test_reset_compat_caches_keeps_registered_names(self) -> None:
        compat.register_extra_platform_names(["sticky"])
        compat.reset_compat_caches()
        self.assertTrue(compat.is_onebot_platform("sticky"))

    def test_builtin_names_survive_empty_registration(self) -> None:
        compat.register_extra_platform_names(())
        self.assertTrue(compat.is_onebot_platform("aiocqhttp"))
        self.assertTrue(compat.is_onebot_platform("llonebot"))


class PayloadFailureTests(unittest.TestCase):
    """覆盖 payload_failure / is_failed_payload / is_unsupported_payload。"""

    def test_ok_payload_is_not_a_failure(self) -> None:
        """标准成功返回体不算失败，也不算未知 action。"""

        payload = {"status": "ok", "retcode": 0, "data": {"message_id": 42}}
        self.assertEqual(compat.payload_failure(payload), (None, ""))
        self.assertFalse(compat.is_failed_payload(payload))
        self.assertFalse(compat.is_unsupported_payload(payload))

    def test_async_status_is_success(self) -> None:
        """``status`` 为 async（已入队）同样视为成功。"""

        payload = {"status": "async"}
        self.assertEqual(compat.payload_failure(payload), (None, ""))
        self.assertFalse(compat.is_failed_payload(payload))
        self.assertFalse(compat.is_unsupported_payload(payload))

    def test_failed_payload_returns_retcode_and_message(self) -> None:
        """失败返回体应原样给出 retcode 与 message。"""

        payload = {"status": "failed", "retcode": 1200, "message": "执行失败"}
        self.assertEqual(compat.payload_failure(payload), (1200, "执行失败"))
        self.assertTrue(compat.is_failed_payload(payload))

    def test_execution_failure_is_not_unsupported(self) -> None:
        """retcode 1200 这类执行失败不应被误判为缺少该 action。"""

        payload = {"status": "failed", "retcode": 1200, "message": "执行失败"}
        self.assertFalse(compat.is_unsupported_payload(payload))

    def test_unknown_action_payload_is_unsupported(self) -> None:
        """LLOneBot 风格的 retcode 1404 未知 action 返回体应判为不支持。"""

        payload = {
            "status": "failed",
            "retcode": 1404,
            "message": "`set_diy_online_status` API 不存在",
        }
        retcode, message = compat.payload_failure(payload)
        self.assertEqual(retcode, 1404)
        self.assertIn("API 不存在", message)
        self.assertTrue(compat.is_failed_payload(payload))
        self.assertTrue(compat.is_unsupported_payload(payload))

    def test_unsupported_detected_by_message_without_retcode(self) -> None:
        """没有可识别 retcode 时，靠错误文本也能判出不支持。"""

        payload = {"status": "failed", "message": "unknown action"}
        self.assertTrue(compat.is_unsupported_payload(payload))

    def test_nonzero_retcode_with_ok_status_is_failure(self) -> None:
        """部分实现失败时仍写 status=ok，只能靠非 0 retcode 判断。"""

        payload = {"status": "ok", "retcode": 1400}
        self.assertEqual(compat.payload_failure(payload), (1400, "OneBot 调用失败"))
        self.assertTrue(compat.is_failed_payload(payload))

    def test_failure_without_message_falls_back(self) -> None:
        """失败但缺少任何文本字段时，message 兜底为固定提示。"""

        payload = {"status": "failed"}
        self.assertEqual(compat.payload_failure(payload), (None, "OneBot 调用失败"))
        self.assertTrue(compat.is_failed_payload(payload))

    def test_wording_takes_priority_over_message(self) -> None:
        """``wording`` 比 ``message``/``msg``/``error`` 更贴近用户，优先采用。"""

        payload = {
            "status": "failed",
            "retcode": 1200,
            "wording": "群聊禁言中",
            "message": "send_group_msg failed",
            "msg": "MSG",
            "error": "ERR",
        }
        self.assertEqual(compat.payload_failure(payload), (1200, "群聊禁言中"))

    def test_non_mapping_payloads_are_ignored(self) -> None:
        """非 Mapping 返回体（None/字符串/列表）不做任何失败推断。"""

        payloads: list[Any] = [None, "", "failed", ["status", "failed"], 1404]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(compat.payload_failure(payload), (None, ""))
                self.assertFalse(compat.is_failed_payload(payload))
                self.assertFalse(compat.is_unsupported_payload(payload))


class PayloadBot:
    """HTTP 风格的 bot：动作失败时不抛异常，原样返回 status=failed。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action not in self.responses:
            return {
                "status": "failed",
                "retcode": 1404,
                "message": f"{action} API 不存在",
            }
        return self.responses[action]


class PayloadErrorTests(CompatTestCase):
    """返回体形式的失败要能被转成异常并参与候选回退。"""

    def test_raise_for_payload_passes_through_success(self) -> None:
        payload = {"status": "ok", "retcode": 0, "data": {"message_id": 7}}
        self.assertIs(compat.raise_for_payload(payload), payload)
        self.assertIsNone(compat.raise_for_payload(None))

    def test_raise_for_payload_raises_with_retcode_and_message(self) -> None:
        payload = {"status": "failed", "retcode": 1200, "message": "执行失败"}
        with self.assertRaises(compat.OneBotPayloadError) as ctx:
            compat.raise_for_payload(payload)
        self.assertEqual(ctx.exception.retcode, 1200)
        self.assertEqual(str(ctx.exception), "执行失败")
        self.assertEqual(compat.onebot_error_info(ctx.exception), (1200, "执行失败"))

    def test_payload_error_message_falls_back(self) -> None:
        exc = compat.OneBotPayloadError(None, "")
        self.assertEqual(str(exc), "OneBot 调用失败")
        self.assertIsNone(exc.retcode)

    def test_payload_error_is_classified_like_action_failed(self) -> None:
        unknown = compat.OneBotPayloadError(1404, "send_poke API 不存在")
        self.assertTrue(compat.is_unsupported_action_error(unknown))
        self.assertTrue(compat.is_variant_error(unknown))
        bad_param = compat.OneBotPayloadError(1400, "参数错误: ['target_id']")
        self.assertTrue(compat.is_bad_param_error(bad_param))
        self.assertTrue(compat.is_variant_error(bad_param))
        runtime = compat.OneBotPayloadError(1200, "执行失败")
        self.assertFalse(compat.is_variant_error(runtime))

    async def test_variants_fall_back_on_failed_payload(self) -> None:
        bot = PayloadBot({"friend_poke": {"status": "ok", "retcode": 0, "data": None}})
        variants = (
            compat.OneBotVariant("send_poke", {"user_id": 1}),
            compat.OneBotVariant("friend_poke", {"user_id": 1}),
        )
        result = await compat.call_onebot_variants(
            bot, variants, op_key="test:payload_fallback"
        )
        self.assertEqual(result, {"status": "ok", "retcode": 0, "data": None})
        actions = [action for action, _ in bot.calls]
        self.assertIn("send_poke", actions)
        self.assertIn("friend_poke", actions)

        # 成功的候选应被记住，第二次不再重试失败的那个。
        bot.calls.clear()
        await compat.call_onebot_variants(
            bot, variants, op_key="test:payload_fallback"
        )
        self.assertEqual([action for action, _ in bot.calls], ["friend_poke"])

    async def test_unrecoverable_failed_payload_is_raised(self) -> None:
        bot = PayloadBot(
            {
                "send_poke": {
                    "status": "failed",
                    "retcode": 1200,
                    "wording": "对方开启了免打扰",
                }
            }
        )
        variants = (
            compat.OneBotVariant("send_poke", {"user_id": 1}),
            compat.OneBotVariant("friend_poke", {"user_id": 1}),
        )
        with self.assertRaises(compat.OneBotPayloadError) as ctx:
            await compat.call_onebot_variants(
                bot, variants, op_key="test:payload_hard_fail"
            )
        self.assertEqual(ctx.exception.retcode, 1200)
        self.assertEqual(str(ctx.exception), "对方开启了免打扰")
        # 不可恢复失败不应继续尝试后续候选。
        self.assertEqual(
            [a for a, _ in bot.calls if a != "get_version_info"], ["send_poke"]
        )

    async def test_all_failed_payloads_report_unsupported(self) -> None:
        bot = PayloadBot({})
        variants = (
            compat.OneBotVariant("set_self_longnick", {"longNick": "hi"}),
            compat.OneBotVariant("set_qq_profile", {"personal_note": "hi"}),
        )
        with self.assertRaises(RuntimeError) as ctx:
            await compat.call_onebot_variants(
                bot, variants, op_key="test:payload_all_unsupported"
            )
        self.assertNotIsInstance(ctx.exception, compat.OneBotPayloadError)
        self.assertIn("不支持", str(ctx.exception))

class AvatarBase64FallbackTests(CompatTestCase):
    """协议端读不到本地路径时应自动改用 base64 重试。"""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.image = pathlib.Path(self._tmp.name) / "avatar.jpg"
        self.image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        self.expected = "base64://" + base64.b64encode(
            b"\xff\xd8\xff\xe0fake-jpeg"
        ).decode("ascii")

    async def test_local_path_is_retried_as_base64(self) -> None:
        seen: list[str] = []

        def handler(**params: Any) -> Any:
            seen.append(params["file"])
            if not params["file"].startswith("base64://"):
                raise FakeActionFailed(
                    {"status": "failed", "retcode": 1200, "message": "路径不存在"}
                )
            return {"status": "ok", "retcode": 0, "data": None}

        bot = FakeBot(
            {"set_qq_avatar": handler}, version=LLONEBOT_VERSION
        )
        result = await compat.set_bot_avatar(bot, str(self.image))
        self.assertEqual(result, {"status": "ok", "retcode": 0, "data": None})
        self.assertEqual(seen, [str(self.image), self.expected])

    async def test_successful_path_call_skips_base64(self) -> None:
        seen: list[str] = []

        def handler(**params: Any) -> Any:
            seen.append(params["file"])
            return {"status": "ok", "retcode": 0, "data": None}

        bot = FakeBot({"set_qq_avatar": handler}, version=LLONEBOT_VERSION)
        await compat.set_bot_avatar(bot, str(self.image))
        self.assertEqual(seen, [str(self.image)])

    async def test_url_failure_is_not_retried(self) -> None:
        seen: list[str] = []

        def handler(**params: Any) -> Any:
            seen.append(params["file"])
            raise FakeActionFailed(
                {"status": "failed", "retcode": 1200, "message": "下载失败"}
            )

        bot = FakeBot({"set_qq_avatar": handler}, version=LLONEBOT_VERSION)
        with self.assertRaises(FakeActionFailed):
            await compat.set_bot_avatar(bot, "https://example.com/a.jpg")
        self.assertEqual(seen, ["https://example.com/a.jpg"])

    async def test_missing_local_file_reports_original_error(self) -> None:
        bot = FakeBot(
            {
                "set_qq_avatar": lambda **_p: (_ for _ in ()).throw(
                    FakeActionFailed(
                        {
                            "status": "failed",
                            "retcode": 1200,
                            "message": "路径不存在",
                        }
                    )
                )
            },
            version=LLONEBOT_VERSION,
        )
        missing = str(pathlib.Path(self._tmp.name) / "nope.jpg")
        with self.assertRaises(FakeActionFailed) as ctx:
            await compat.set_bot_avatar(bot, missing)
        self.assertEqual(ctx.exception.retcode, 1200)

    async def test_base64_reference_is_passed_through(self) -> None:
        seen: list[str] = []

        def handler(**params: Any) -> Any:
            seen.append(params["file"])
            return {"status": "ok", "retcode": 0, "data": None}

        bot = FakeBot({"set_qq_avatar": handler}, version=LLONEBOT_VERSION)
        await compat.set_bot_avatar(bot, self.expected)
        self.assertEqual(seen, [self.expected])

if __name__ == "__main__":
    unittest.main()
