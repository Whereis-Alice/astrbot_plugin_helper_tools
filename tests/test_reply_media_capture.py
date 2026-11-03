from __future__ import annotations

import asyncio
import functools
import gc
import json
import unittest
import weakref
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from aiocqhttp import CQHttp
from aiocqhttp.message import Message

from astrbot_plugin_helper_tools.reply_media_capture import ReplyMediaCapture

IMAGE = {"type": "image", "data": {"file": "base64://image", "sub_type": 1}}
FLASH = {"type": "flash", "data": {"file": "https://example.invalid/flash.png"}}
TEXT = {"type": "text", "data": {"text": "Message text must never reach the sink."}}


class FakeApi:
    def __init__(self, response: Any = None, *, http: bool = False) -> None:
        self.response = {"message_id": -77} if response is None else response
        self.login_response: Any = {"status": "ok", "data": {"user_id": 10001}}
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.handler: Any = None
        if http:
            self._http_api = SimpleNamespace(_api_root="https://example.invalid/")

    async def call_action(self, action: Any, **params: Any) -> Any:
        self.calls.append((action, params))
        value = self.login_response if action == "get_login_info" else self.response
        if self.handler is not None:
            value = self.handler(action, params)
            if asyncio.iscoroutine(value):
                value = await value
        if isinstance(value, BaseException):
            raise value
        return value


class FakeBot:
    """Model CQHttp.api plus both explicit and fabricated action methods."""

    def __init__(self, api: Any = None, *, keys: list[str] | None = None) -> None:
        self._api = api or FakeApi()
        self.client_self_id = "4eebbc9c-2c16-420c-8aaf-028312942753"
        if keys is not None:
            self._wsr_api_clients = dict.fromkeys(keys, object())

    @property
    def api(self) -> Any:
        return self._api

    def __getattr__(self, action: str) -> Any:
        return functools.partial(self.api.call_action, action)

    async def call_action(self, action: str, **params: Any) -> Any:
        return await self.api.call_action(action, **params)

    async def send(self, event: dict[str, Any], message: Any) -> Any:
        return await self.send_msg(**event, message=message)


class FakePlatform:
    def __init__(
        self, bot: Any, platform_id: str = "qq-main", name: str = "aiocqhttp"
    ) -> None:
        self.bot = bot
        self.metadata = SimpleNamespace(id=platform_id, name=name)
        self.client_self_id = "platform-instance-uuid"

    def meta(self) -> Any:
        return self.metadata

    def get_client(self) -> Any:
        return self.bot

    async def send_by_session(self, message: Any) -> Any:
        return await self.bot.send_group_msg(group_id=20001, message=message)


class FakeEvent:
    def __init__(
        self,
        bot: Any,
        self_id: str = "10001",
        platform_id: str = "qq-main",
        name: str = "aiocqhttp",
    ) -> None:
        self.bot = bot
        self.platform_meta = SimpleNamespace(id=platform_id, name=name)
        self.message_obj = SimpleNamespace(self_id=self_id)

    def get_platform_id(self) -> str:
        return self.platform_meta.id

    def get_platform_name(self) -> str:
        return self.platform_meta.name

    def get_self_id(self) -> str:
        return self.message_obj.self_id


class ReplyMediaCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.records: list[tuple[str, str, str, list[dict[str, Any]]]] = []
        self.captures: list[ReplyMediaCapture] = []

    async def asyncTearDown(self) -> None:
        for capture in reversed(self.captures):
            await capture.stop()

    async def sink(
        self,
        platform_id: str,
        self_id: str,
        message_id: str,
        images: list[dict[str, Any]],
    ) -> None:
        self.records.append((platform_id, self_id, message_id, images))

    async def capture(
        self,
        *platforms: FakePlatform,
        callback: Any = None,
        enabled: Any = None,
        manager: Any = None,
    ) -> ReplyMediaCapture:
        if manager is None:
            manager = SimpleNamespace(get_insts=lambda: list(platforms))
        capture = ReplyMediaCapture(
            SimpleNamespace(platform_manager=manager),
            callback if callback is not None else self.sink,
            enabled if enabled is not None else lambda: True,
        )
        self.captures.append(capture)
        await capture.start()
        return capture

    async def test_backend_envelopes_and_direct_api_calls(self) -> None:
        responses = {
            "NapCat": {"status": "ok", "retcode": 0, "data": {"message_id": -123}},
            "SnowLuma": {"status": "ok", "retcode": 0, "data": {"message_id": -123}},
            "LLBot": {"message_id": -123},
            "result-wrapper": {"result": {"message_id": -123}},
            "nested": {
                "result": {"data": {"status": "ok", "data": {"message_id": -123}}}
            },
            "json": json.dumps(
                {"status": "ok", "retcode": 0, "data": {"message_id": -123}}
            ),
        }
        for backend, response in responses.items():
            with self.subTest(backend=backend):
                api = FakeApi(response)
                bot = FakeBot(api, keys=["10001"])
                await self.capture(FakePlatform(bot))
                returned = await bot.api.call_action(
                    "send_group_msg", group_id=20001, message=[TEXT, IMAGE]
                )
                self.assertIs(returned, response)
                self.assertEqual(
                    self.records[-1], ("qq-main", "10001", "-123", [IMAGE["data"]])
                )
                self.assertEqual(len(api.calls), 1)

    async def test_real_cqhttp_api_property_and_all_send_entrypoints(self) -> None:
        # Bypass construction so this test cannot create a server or connection.
        bot = CQHttp.__new__(CQHttp)
        api = FakeApi()
        bot._api = api
        bot._wsr_api_clients = {"10001": object()}
        bot._before_sending_funcs = []
        await self.capture(FakePlatform(bot))
        self.assertIs(bot.api, api)
        for call in (
            bot.api.call_action("send_msg", message=[IMAGE]),
            bot.call_action("send_private_msg", message=[IMAGE], user_id=20001),
            bot.send_group_msg(message=Message([TEXT, IMAGE]), group_id=20001),
            bot.send({"message_type": "group", "group_id": 20001}, [IMAGE]),
        ):
            self.assertIs(await call, api.response)
        self.assertEqual(len(api.calls), 4)
        self.assertEqual(len(self.records), 4)

    async def test_magpie_bot_send_and_event_send_fallback_share_api(self) -> None:
        api = FakeApi()
        bot = FakeBot(api, keys=["10001"])
        await self.capture(FakePlatform(bot))
        await bot.send({"message_type": "group", "group_id": 20001}, [IMAGE])
        await bot.call_action("send_private_msg", user_id=20002, message=[IMAGE])
        await bot.send_group_msg(group_id=20001, message=Message([TEXT, IMAGE]))
        self.assertEqual(
            [action for action, _ in api.calls],
            ["send_msg", "send_private_msg", "send_group_msg"],
        )
        self.assertEqual(len(self.records), 3)
        self.assertTrue(all(record[3] == [IMAGE["data"]] for record in self.records))

    async def test_active_session_send_before_first_event(self) -> None:
        platform = FakePlatform(FakeBot(keys=["10001"]))
        await self.capture(platform)
        await platform.send_by_session([TEXT, IMAGE, FLASH])
        self.assertEqual(
            self.records, [("qq-main", "10001", "-77", [IMAGE["data"], FLASH["data"]])]
        )

    async def test_cq_strings_decode_entities_without_plain_text(self) -> None:
        bot = FakeBot(keys=["10001"])
        await self.capture(FakePlatform(bot))
        message = "private body [CQ:image,file=https://example.invalid/a&#44;b.png,summary=a&amp;b&#91;x&#93;] [CQ:flash,file=base64://123]"
        await bot.api.call_action("send_msg", message=message, user_id=20001)
        self.assertEqual(
            self.records[0][3],
            [
                {"file": "https://example.invalid/a,b.png", "summary": "a&b[x]"},
                {"file": "base64://123"},
            ],
        )
        self.assertNotIn("private body", repr(self.records))

    async def test_all_inline_forward_actions_recurse_nodes(self) -> None:
        message = [
            {
                "type": "node",
                "data": {
                    "user_id": 999,
                    "nickname": "another sender",
                    "content": [TEXT, IMAGE],
                },
            },
            {
                "type": "forward",
                "data": {
                    "nodes": [
                        {
                            "type": "node",
                            "data": {"content": "[CQ:flash,file=flash.png]"},
                        }
                    ]
                },
            },
            {"type": "forward", "data": {"id": "unresolved-reference"}},
            {"type": "mface", "data": {"url": "excluded.png"}},
        ]
        bot = FakeBot(keys=["10001"])
        await self.capture(FakePlatform(bot))
        for action in (
            "send_group_forward_msg",
            "send_private_forward_msg",
            "send_forward_msg",
        ):
            with self.subTest(action=action):
                await bot.api.call_action(action, messages=message, group_id=20001)
                self.assertEqual(
                    self.records[-1][3], [IMAGE["data"], {"file": "flash.png"}]
                )
                self.assertEqual(self.records[-1][1], "10001")

    async def test_input_and_return_objects_are_preserved_when_sink_mutates_images(
        self,
    ) -> None:
        request = [
            {"type": "image", "data": {"file": "a.png", "meta": {"subType": 1}}},
            TEXT,
        ]
        expected = json.loads(json.dumps(request))
        response = {"message_id": 9}

        async def mutate(*args: Any) -> None:
            args[3][0]["meta"]["subType"] = 999

        api = FakeApi(response)
        bot = FakeBot(api, keys=["10001"])
        await self.capture(FakePlatform(bot), callback=mutate)
        returned = await bot.api.call_action("send_group_msg", message=request)
        self.assertIs(returned, response)
        self.assertIs(api.calls[0][1]["message"], request)
        self.assertEqual(request, expected)

    async def test_failed_queued_and_missing_receipts_are_never_captured_or_retried(
        self,
    ) -> None:
        responses = [
            {"status": "failed", "retcode": 1200, "data": {"message_id": 1}},
            {"status": "ok", "retcode": 200, "data": {"message_id": 1}},
            {"status": "async", "retcode": 1, "data": {"message_id": 1}},
            {"status": "queued", "data": {"message_id": 1}},
            {"data": {"status": "error", "message_id": 1}},
            {"success": False, "message_id": 1},
            {"status": "ok", "data": {}},
            {"message_id": 0},
            {"message_id": False},
            {"id": 1},
            {"message_id": []},
        ]
        for response in responses:
            with self.subTest(response=response):
                api = FakeApi(response)
                bot = FakeBot(api, keys=["10001"])
                await self.capture(FakePlatform(bot))
                returned = await bot.api.call_action("send_msg", message=[IMAGE])
                self.assertIs(returned, response)
                self.assertEqual(len(api.calls), 1)
        self.assertEqual(self.records, [])

    async def test_original_send_exception_and_cancellation_are_preserved(self) -> None:
        for error in (
            RuntimeError("original failure"),
            asyncio.CancelledError("cancelled send"),
        ):
            with self.subTest(error=type(error)):
                api = FakeApi(error)
                bot = FakeBot(api, keys=["10001"])
                await self.capture(FakePlatform(bot))
                with self.assertRaises(type(error)) as raised:
                    await bot.api.call_action("send_msg", message=[IMAGE])
                self.assertIs(raised.exception, error)
                self.assertEqual(len(api.calls), 1)
        self.assertEqual(self.records, [])

    async def test_callback_exception_or_cancellation_does_not_change_success(
        self,
    ) -> None:
        for error in (
            RuntimeError("sink failure"),
            asyncio.CancelledError("sink cancellation"),
        ):
            with self.subTest(error=type(error)):

                async def fail(*args: Any, sink_error: BaseException = error) -> None:
                    raise sink_error

                api = FakeApi()
                bot = FakeBot(api, keys=["10001"])
                await self.capture(FakePlatform(bot), callback=fail)
                self.assertIs(
                    await bot.api.call_action("send_msg", message=[IMAGE]), api.response
                )
                self.assertEqual(len(api.calls), 1)

    async def test_plain_reads_and_async_action_variants_are_excluded(self) -> None:
        bot = FakeBot(keys=["10001"])
        await self.capture(FakePlatform(bot))
        for action, message in (
            ("send_msg", [TEXT]),
            ("get_msg", [IMAGE]),
            ("send_msg_async", [IMAGE]),
            ("send_msg_rate_limited", [IMAGE]),
        ):
            await bot.api.call_action(action, message=message)
        self.assertEqual(self.records, [])

    async def test_multiple_accounts_require_explicit_self_id_even_after_observation(
        self,
    ) -> None:
        bot = FakeBot(keys=["10001", "10002"])
        capture = await self.capture(FakePlatform(bot))
        capture.observe_event(FakeEvent(bot, "10001"))
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(self.records, [])
        await bot.api.call_action("send_msg", self_id="10002", message=[IMAGE])
        self.assertEqual(self.records[-1][1], "10002")

    async def test_kwargs_self_id_has_priority_and_target_user_is_not_sender(
        self,
    ) -> None:
        bot = FakeBot(keys=["10001"])
        await self.capture(FakePlatform(bot))
        await bot.api.call_action(
            "send_private_msg", self_id=10002, user_id=99999, message=[IMAGE]
        )
        self.assertEqual(self.records[0][1], "10002")

    async def test_registry_on_underlying_ws_api_is_used(self) -> None:
        api = FakeApi()
        api._wsr_api = SimpleNamespace(_api_clients={"10003": object()})
        bot = FakeBot(api)
        await self.capture(FakePlatform(bot))
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(self.records[0][1], "10003")

    async def test_observed_identity_only_used_for_single_account_and_not_empty_ws(
        self,
    ) -> None:
        bot = FakeBot()
        capture = await self.capture(FakePlatform(bot))
        capture.observe_event(FakeEvent(bot, "10001"))
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)
        capture.observe_event(FakeEvent(bot, "10002"))
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)
        other = FakeBot(keys=[])
        capture.observe_event(FakeEvent(other, "10001"))
        await other.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)

    async def test_unknown_identity_and_platform_uuid_are_safely_skipped(self) -> None:
        bot = FakeBot()
        await self.capture(FakePlatform(bot))
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(self.records, [])
        self.assertEqual([action for action, _ in bot.api.calls], ["send_msg"])

    async def test_http_login_is_read_once_with_no_incoming_event(self) -> None:
        api = FakeApi(http=True)
        bot = FakeBot(api)
        capture = await self.capture(FakePlatform(bot))
        await capture.start()
        await bot.api.call_action("send_msg", message=[IMAGE])
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual([action for action, _ in api.calls].count("get_login_info"), 1)
        self.assertEqual([record[1] for record in self.records], ["10001", "10001"])

    async def test_http_login_timeout_or_invalid_identity_is_not_retried(self) -> None:
        for response in (
            {"user_id": "random-uuid"},
            {"status": "failed", "data": {"user_id": 10001}},
            "timeout",
        ):
            with self.subTest(response=response):
                api = FakeApi(http=True)
                api.login_response = response
                if response == "timeout":

                    async def wait_login(
                        action: str, params: Any, receipt: Any = api.response
                    ) -> Any:
                        if action == "get_login_info":
                            await asyncio.sleep(10)
                        return receipt

                    api.handler = wait_login
                bot = FakeBot(api)
                with patch(
                    "astrbot_plugin_helper_tools.reply_media_capture._LOGIN_TIMEOUT_SECONDS",
                    0.01,
                ):
                    await self.capture(FakePlatform(bot))
                await bot.api.call_action("send_msg", message=[IMAGE])
                await bot.api.call_action("send_msg", message=[IMAGE])
                self.assertEqual(
                    [action for action, _ in api.calls].count("get_login_info"), 1
                )
        self.assertEqual(self.records, [])

    async def test_http_concurrent_sends_share_login_probe(self) -> None:
        api = FakeApi(http=True)
        bot = FakeBot(api)
        enabled = [False]
        await self.capture(FakePlatform(bot), enabled=lambda: enabled[0])
        enabled[0] = True
        await asyncio.gather(
            *(bot.api.call_action("send_msg", message=[IMAGE]) for _ in range(8))
        )
        self.assertEqual([action for action, _ in api.calls].count("get_login_info"), 1)
        self.assertEqual(len(self.records), 8)

    async def test_two_platforms_are_kept_separate_and_shared_api_is_ambiguous(
        self,
    ) -> None:
        one, two = FakeBot(keys=["10001"]), FakeBot(keys=["10002"])
        await self.capture(FakePlatform(one, "qq-one"), FakePlatform(two, "qq-two"))
        await asyncio.gather(
            one.api.call_action("send_msg", message=[IMAGE]),
            two.api.call_action("send_msg", message=[IMAGE]),
        )
        self.assertEqual(
            {record[:2] for record in self.records},
            {("qq-one", "10001"), ("qq-two", "10002")},
        )
        shared = FakeApi()
        await self.capture(
            FakePlatform(FakeBot(shared), "shared-one"),
            FakePlatform(FakeBot(shared), "shared-two"),
        )
        await shared.call_action("send_msg", self_id="10001", message=[IMAGE])
        self.assertEqual(len(self.records), 2)

    async def test_identity_is_snapshotted_before_concurrent_send_completes(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        api = FakeApi()

        async def handler(action: str, params: Any) -> Any:
            if params["number"] == 1:
                entered.set()
                await release.wait()
            return {"message_id": params["number"]}

        api.handler = handler
        bot = FakeBot(api, keys=["10001"])
        await self.capture(FakePlatform(bot))
        first = asyncio.create_task(
            bot.api.call_action("send_msg", message=[IMAGE], number=1)
        )
        await entered.wait()
        bot._wsr_api_clients = {"10002": object()}
        await bot.api.call_action("send_msg", message=[FLASH], number=2)
        release.set()
        await first
        self.assertEqual(
            {record[2]: record[1] for record in self.records},
            {"1": "10001", "2": "10002"},
        )

    async def test_empty_start_then_platform_loaded_rescan_covers_first_active_send(
        self,
    ) -> None:
        platforms: list[FakePlatform] = []
        manager = SimpleNamespace(get_insts=lambda: list(platforms))
        capture = await self.capture(manager=manager)
        bot = FakeBot(keys=["10001"])
        platforms.append(FakePlatform(bot))
        # main.py invokes this from on_platform_loaded, before any quote/event.
        await capture.start()
        await platforms[0].send_by_session([IMAGE])
        self.assertEqual(len(self.records), 1)

    async def test_platform_insts_fallback_and_unknown_names(self) -> None:
        bot = FakeBot(keys=["10001"])
        ignored = FakeBot(keys=["10002"])

        def not_ready() -> Any:
            raise RuntimeError("getter unavailable")

        manager = SimpleNamespace(
            get_insts=not_ready,
            platform_insts=[FakePlatform(bot), FakePlatform(ignored, name="telegram")],
        )
        await self.capture(manager=manager)
        await bot.api.call_action("send_msg", message=[IMAGE])
        await ignored.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)

    async def test_observe_event_installs_new_bot_and_replaced_api_without_stacking(
        self,
    ) -> None:
        capture = await self.capture()
        bot = FakeBot(keys=["10001"])
        event = FakeEvent(bot)
        capture.observe_event(event)
        wrapper = bot.api.call_action
        capture.observe_event(event)
        self.assertIs(bot.api.call_action, wrapper)
        await bot.api.call_action("send_msg", message=[IMAGE])
        bot._api = FakeApi({"message_id": 90})
        capture.observe_event(event)
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual([record[2] for record in self.records], ["-77", "90"])

    async def test_fallback_wraps_actual_bot_call_action_without_fake_api(self) -> None:
        class BareBot(FakeApi):
            def __getattr__(self, action: str) -> Any:
                return functools.partial(self.call_action, action)

        bot = BareBot()
        capture = await self.capture(FakePlatform(bot))
        capture.observe_event(FakeEvent(bot))
        await bot.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)
        await capture.stop()
        self.assertNotIn("call_action", vars(bot))

    async def test_enabled_is_dynamic_and_errors_do_not_change_sends(self) -> None:
        enabled = [False]
        bot = FakeBot(keys=["10001"])
        await self.capture(FakePlatform(bot), enabled=lambda: enabled[0])
        await bot.api.call_action("send_msg", message=[IMAGE])
        enabled[0] = True
        await bot.api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)
        other = FakeBot(keys=["10002"])

        def invalid() -> bool:
            raise RuntimeError("switch unavailable")

        await self.capture(FakePlatform(other), enabled=invalid)
        self.assertIs(
            await other.api.call_action("send_msg", message=[IMAGE]), other.api.response
        )
        self.assertEqual(len(self.records), 1)

    async def test_stop_restores_top_wrapper_and_cached_wrapper_becomes_inert(
        self,
    ) -> None:
        bot = FakeBot(keys=["10001"])
        original = bot.api.call_action
        capture = await self.capture(FakePlatform(bot))
        wrapper = bot.api.call_action
        await capture.start()
        self.assertIs(bot.api.call_action, wrapper)
        await capture.stop()
        self.assertEqual(bot.api.call_action, original)
        self.assertNotIn("call_action", vars(bot.api))
        await wrapper("send_msg", message=[IMAGE])
        self.assertEqual(self.records, [])

    async def test_reload_preserves_outer_and_inner_wrapper_chains(self) -> None:
        api = FakeApi()
        original = api.call_action
        seen: list[str] = []

        async def inner(*args: Any, **kwargs: Any) -> Any:
            seen.append("anti_revoke")
            return await original(*args, **kwargs)

        api.call_action = inner
        bot = FakeBot(api, keys=["10001"])
        old = await self.capture(FakePlatform(bot))
        old_wrapper = api.call_action

        async def outer(*args: Any, **kwargs: Any) -> Any:
            seen.append("other_plugin")
            return await old_wrapper(*args, **kwargs)

        api.call_action = outer
        await old.stop()
        self.assertIs(api.call_action, outer)
        fresh = await self.capture(FakePlatform(bot))
        await api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)
        self.assertEqual(seen, ["other_plugin", "anti_revoke"])
        await fresh.stop()
        self.assertIs(api.call_action, outer)
        await api.call_action("send_msg", message=[IMAGE])
        self.assertEqual(len(self.records), 1)

    async def test_inert_outer_chain_does_not_retain_unloaded_service(self) -> None:
        class Service:
            async def callback(self, *args: Any) -> None:
                return None

        service = Service()
        service_ref = weakref.ref(service)
        bot = FakeBot(keys=["10001"])
        capture = await self.capture(FakePlatform(bot), callback=service.callback)
        wrapper = bot.api.call_action

        async def outer(*args: Any, **kwargs: Any) -> Any:
            return await wrapper(*args, **kwargs)

        bot.api.call_action = outer
        await capture.stop()
        del service
        gc.collect()
        self.assertIsNone(service_ref())
        await bot.api.call_action("send_msg", message=[IMAGE])

    async def test_stop_during_send_preserves_result_without_late_callback(
        self,
    ) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        api = FakeApi()

        async def handler(action: Any, params: Any) -> Any:
            entered.set()
            await release.wait()
            return api.response

        api.handler = handler
        bot = FakeBot(api, keys=["10001"])
        capture = await self.capture(FakePlatform(bot))
        send = asyncio.create_task(bot.api.call_action("send_msg", message=[IMAGE]))
        await entered.wait()
        await capture.stop()
        release.set()
        self.assertIs(await send, api.response)
        self.assertEqual(self.records, [])

    async def test_stop_cancels_sink_in_progress_without_changing_success(self) -> None:
        entered = asyncio.Event()

        async def blocked(*args: Any) -> None:
            entered.set()
            await asyncio.Event().wait()

        bot = FakeBot(keys=["10001"])
        capture = await self.capture(FakePlatform(bot), callback=blocked)
        send = asyncio.create_task(bot.api.call_action("send_msg", message=[IMAGE]))
        await entered.wait()
        await capture.stop()
        self.assertIs(await send, bot.api.response)


if __name__ == "__main__":
    unittest.main()
