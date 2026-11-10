from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import astrbot.api.message_components as Comp
from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

from astrbot_plugin_helper_tools.avatar_rotation_service import AvatarRotationService
from astrbot_plugin_helper_tools.chat_history_card import ChatHistoryCardRenderer
from astrbot_plugin_helper_tools.chat_history_service import (
    ChatHistoryError,
    ChatHistoryService,
)
from astrbot_plugin_helper_tools.command_routing import addressed_command_text
from astrbot_plugin_helper_tools.main import (
    BotQQProfileTool,
    HelperToolsPlugin,
    PokeQQUserTool,
    QQAvatarTool,
    QQGroupInfoTool,
    QQGroupMemberListTool,
    QQGroupMemberTool,
    QQProfileTool,
    SendQQProfileLikeTool,
    SteamSearchTool,
)
from astrbot_plugin_helper_tools.perception_service import EnvironmentPerceptionService
from astrbot_plugin_helper_tools.platform_support import is_bot_mention
from astrbot_plugin_helper_tools.poke_service import PokeService
from astrbot_plugin_helper_tools.qq_features import QQService, require_onebot
from astrbot_plugin_helper_tools.reply_media_guard import ReplyMediaGuard
from astrbot_plugin_helper_tools.rollpig_service import RollPigService
from astrbot_plugin_helper_tools.wake_service import WakeService
from astrbot_plugin_helper_tools.wallpaper_service import ImageRef, WallpaperService

OFFICIAL_NAMES = ("qq_official", "qq_official_webhook")


def make_event(
    platform="qq_official",
    *,
    instance="official-a",
    sender="USER_openid",
    self_id="10001",
    group="30001",
    text="测试",
    chain=None,
    guild="",
):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.self_id = self_id
    message.sender = MessageMember(user_id=sender, nickname="测试用户")
    message.group_id = group
    message.message_id = "message-1"
    message.timestamp = int(time.time())
    message.message_str = text
    message.message = chain if chain is not None else [Comp.Plain(text)]
    message.raw_message = {"guild_id": guild} if guild else {}
    event = AstrMessageEvent(
        text,
        message,
        PlatformMetadata(name=platform, description="test", id=instance),
        group or sender,
    )
    event.bot = SimpleNamespace(call_action=AsyncMock())
    event.is_at_or_wake_command = False
    return event


class OfficialRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_at_routes_and_wakes_on_both_transports(self):
        for platform in OFFICIAL_NAMES:
            with self.subTest(platform=platform):
                event = make_event(
                    platform,
                    text="风景",
                    chain=[Comp.At(qq="qq_official"), Comp.Plain("风景")],
                )
                self.assertEqual(addressed_command_text(event, ["/"]), "风景")
                wake = WakeService({"wake": {"wake_cd": 0, "debounce_enabled": False}})
                try:
                    self.assertTrue(wake.has_at_bot(event))
                    self.assertEqual(wake._detect_message_type(event), "at")
                    self.assertEqual(await wake.apply(event), "wake")
                    self.assertTrue(event.is_at_or_wake_command)
                    event.message_obj.message = [Comp.At(qq="qq_official")]
                    self.assertTrue(wake._is_only_bot_mention(event))
                finally:
                    await wake.stop()

    async def test_official_at_obeys_disabled_waking_and_blacklist(self):
        for options in (
            {"at_wake_enabled": False},
            {"global_blacklist": ["USER_openid"]},
        ):
            event = make_event(chain=[Comp.At(qq="qq_official"), Comp.Plain("你好")])
            wake = WakeService(
                {"wake": {**options, "wake_cd": 0, "debounce_enabled": False}}
            )
            try:
                await wake.apply(event)
                self.assertFalse(event.is_at_or_wake_command)
            finally:
                await wake.stop()

    async def test_addressing_does_not_expand_bare_group_or_onebot_permissions(self):
        for platform in (*OFFICIAL_NAMES, "aiocqhttp"):
            self.assertIsNone(
                addressed_command_text(make_event(platform, text="风景"), ["/"])
            )
            self.assertEqual(
                addressed_command_text(make_event(platform, text="/风景"), ["/"]),
                "风景",
            )
        other = make_event("aiocqhttp", text="/风景", chain=[Comp.At(qq="qq_official")])
        self.assertFalse(is_bot_mention(other, other.get_messages()[0]))
        self.assertIsNone(addressed_command_text(other, ["/"]))
        reply = make_event(
            text="风景", chain=[Comp.Reply(id="quote", sender_id="qq_official")]
        )
        self.assertIsNone(addressed_command_text(reply, ["/"]))
        self.assertEqual(
            addressed_command_text(make_event(group="", text="风景"), []), "风景"
        )

    async def test_rollpig_ignores_synthetic_at_and_all(self):
        event = make_event(
            chain=[
                Comp.At(qq="qq_official"),
                Comp.At(qq="all"),
                Comp.At(qq="OTHER_openid"),
            ]
        )
        service = RollPigService({}, Path("."))
        self.assertEqual(service._mention_ids(event), ["OTHER_openid"])


class OfficialIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_official_ids_are_not_qq_profiles(self):
        for platform in OFFICIAL_NAMES:
            event = make_event(platform, sender="2123456789", self_id="1234567890")
            service = EnvironmentPerceptionService(
                {
                    "perception": {
                        "enabled": True,
                        "include_sender_qq": True,
                        "include_sender_group_profile": True,
                        "include_bot_group_identity": True,
                    }
                }
            )
            with patch(
                "astrbot_plugin_helper_tools.perception_service.call_onebot",
                new_callable=AsyncMock,
            ) as action:
                text = await service.context_for_event(event)
                self.assertIn("用户ID（OpenID/频道用户ID，非QQ号）=2123456789", text)
                self.assertNotIn("群等级", text)
                self.assertEqual(service._current_bot_qq(event), "")
                self.assertEqual(
                    await service._group_member_info(
                        event, group_id="30001", user_id="2123456789"
                    ),
                    {},
                )
                action.assert_not_awaited()

    async def test_official_identity_respects_config_and_channel_scene(self):
        service = EnvironmentPerceptionService(
            {
                "perception": {
                    "enabled": True,
                    "include_sender_qq": False,
                    "include_sender_group_profile": False,
                    "include_platform": True,
                }
            }
        )
        text = await service.context_for_event(
            make_event("qq_official_webhook", guild="guild-1")
        )
        self.assertIn("QQ 官方机器人，频道", text)
        self.assertNotIn("USER_openid", text)

    async def test_qq_resolution_requires_explicit_real_qq(self):
        service = QQService({})
        event = make_event(sender="2123456789", chain=[Comp.At(qq="2234567890")])
        self.assertIsNone(service.resolve_qq_id(event)[0])
        self.assertIsNone(service.resolve_group_id(event)[0])
        self.assertEqual(service.resolve_qq_id(event, "2123456789"), ("2123456789", ""))
        with self.assertRaisesRegex(RuntimeError, "QQ 官方机器人"):
            require_onebot(event)
        onebot_event = make_event("aiocqhttp")
        self.assertIs(require_onebot(onebot_event), onebot_event.bot)

    async def test_tool_filter_is_local_and_preserves_other_plugins(self):
        plugin = object.__new__(HelperToolsPlugin)
        qq, generic = QQAvatarTool(plugin=plugin), SteamSearchTool(plugin=plugin)
        unrelated = FunctionTool(
            name=qq.name, description="another plugin", parameters={}
        )
        plugin._registered_llm_tools = [qq, generic]
        original = ToolSet(tools=[qq, generic, unrelated])
        for platform in OFFICIAL_NAMES:
            request = SimpleNamespace(func_tool=original)
            await plugin.official_platform_tools_handler(make_event(platform), request)
            self.assertEqual(request.func_tool.tools, [generic, unrelated])
            self.assertIsNot(request.func_tool, original)
            self.assertEqual(original.tools, [qq, generic, unrelated])
            self.assertTrue(qq.active)
        request = SimpleNamespace(func_tool=original)
        await plugin.official_platform_tools_handler(make_event("aiocqhttp"), request)
        self.assertIs(request.func_tool, original)

    async def test_permission_wrapped_tools_are_filtered_without_unwrapping_survivors(
        self,
    ):
        plugin = object.__new__(HelperToolsPlugin)
        blocked = [
            cls(plugin=plugin)
            for cls in (
                QQAvatarTool,
                QQGroupMemberTool,
                QQGroupMemberListTool,
                QQGroupInfoTool,
                QQProfileTool,
                PokeQQUserTool,
                SendQQProfileLikeTool,
                BotQQProfileTool,
            )
        ]
        generic = SteamSearchTool(plugin=plugin)
        unrelated = FunctionTool(
            name=blocked[0].name, description="other plugin", parameters={}
        )
        plugin._registered_llm_tools = [*blocked, generic]
        wrapped = []
        # Match AstrBot's transparent permission proxy without requiring a
        # private class that is absent from older supported core versions.
        for tool in (*blocked, generic, unrelated):
            proxy = FunctionTool(
                name=tool.name, description=tool.description, parameters=tool.parameters
            )
            proxy._wrapped = tool
            wrapped.append(proxy)
        original = ToolSet(tools=wrapped)
        request = SimpleNamespace(func_tool=original)
        await plugin.official_platform_tools_handler(make_event(), request)
        self.assertEqual(request.func_tool.tools, wrapped[-2:])
        self.assertEqual(original.tools, wrapped)
        self.assertTrue(all(tool.active for tool in blocked))

    async def test_official_anti_revoke_does_not_start_onebot_capture(self):
        plugin = object.__new__(HelperToolsPlugin)
        plugin.config = {}
        plugin.anti_revoke = SimpleNamespace(
            enabled=lambda: True, handle_event=AsyncMock()
        )
        await plugin.anti_revoke_event_handler(make_event())
        plugin.anti_revoke.handle_event.assert_not_awaited()


class OfficialHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_history_opaque_filters_stable_scope_and_instance_isolation(
        self,
    ):
        for platform in OFFICIAL_NAMES:
            with tempfile.TemporaryDirectory() as directory:
                service = ChatHistoryService(
                    {"chat_history": {"enabled": True}}, Path(directory)
                )
                try:
                    event = make_event(
                        platform, self_id="qq_official", text="可检索的记录"
                    )
                    await service.capture_event(event)
                    event.message_obj.self_id = "1234567890"
                    with patch(
                        "astrbot_plugin_helper_tools.chat_history_service.get_group_msg_history",
                        new_callable=AsyncMock,
                    ) as backfill:
                        result = await service.search(event, sender_qqs=["USER_openid"])
                        backfill.assert_not_awaited()
                    self.assertEqual(result.total_count, 1)
                    self.assertFalse(result.backfill.attempted)
                    text = result.render_for_model(
                        timezone=ZoneInfo("Asia/Shanghai"),
                        max_chars=8000,
                        include_sender_qq=True,
                    )
                    self.assertIn("用户ID（OpenID/频道用户ID） USER_openid", text)
                    self.assertIn("不能据此推断全群", text)
                    payload = ChatHistoryCardRenderer._payload(
                        result,
                        timezone=ZoneInfo("Asia/Shanghai"),
                        skin="纸笺",
                        include_sender_qq=True,
                        max_messages=10,
                        max_chars=5000,
                    )
                    self.assertIn("用户ID", payload["messages"][0]["meta"])
                    self.assertEqual(
                        (
                            await service.search(event, sender_qqs=["OTHER_openid"])
                        ).total_count,
                        0,
                    )
                    other = make_event(platform, instance="official-b")
                    self.assertEqual((await service.search(other)).total_count, 0)
                    channel = make_event(platform, guild="guild-1")
                    self.assertEqual((await service.search(channel)).total_count, 0)
                    with self.assertRaises(ChatHistoryError):
                        await service.search(
                            event, sender_qqs=["USER_openid", "invalid id!"]
                        )
                finally:
                    service.close()

    async def test_missing_instance_never_falls_back_to_shared_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChatHistoryService(
                {"chat_history": {"enabled": True}}, Path(directory)
            )
            try:
                event = make_event(instance="")
                with self.assertRaises(ChatHistoryError):
                    await service.search(event)
            finally:
                service.close()


class OfficialMediaAndActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_quote_stays_unknown_without_onebot_lookup(self):
        for platform in OFFICIAL_NAMES:
            image = Comp.Image.fromURL("https://example.com/image.jpg")
            reply = Comp.Reply(id="quote", chain=[image])
            event = make_event(platform, chain=[reply, Comp.Plain("这是谁发的")])
            with patch(
                "astrbot_plugin_helper_tools.reply_media_guard.onebot_get_msg",
                new_callable=AsyncMock,
            ) as lookup:
                result = await ReplyMediaGuard({}).mark_bot_reply_images(event)
                self.assertEqual(result.marked_image_count, 0)
                self.assertIsNone(image._helper_tools_quote_is_bot)
                lookup.assert_not_awaited()

    async def test_known_quote_sender_is_preserved(self):
        image = Comp.Image.fromURL("https://example.com/image.jpg")
        reply = Comp.Reply(id="quote", chain=[image], sender_id="10001")
        result = await ReplyMediaGuard({}).mark_bot_reply_images(
            make_event(chain=[reply])
        )
        self.assertEqual(result.marked_image_count, 1)
        self.assertTrue(image._helper_tools_quote_is_bot)

    async def test_wallpaper_standard_send_and_missing_quote_fail_without_onebot(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WallpaperService({}, Path(directory), SimpleNamespace())
            event = make_event()
            event.send = AsyncMock()
            chain = [Comp.Image.fromURL("https://example.com/image.jpg")]
            await service._send_chain(event, chain, None, SimpleNamespace(name="test"))
            self.assertEqual(event.send.await_args.args[0].chain, chain)
            with patch(
                "astrbot_plugin_helper_tools.wallpaper_service.onebot_get_msg",
                new_callable=AsyncMock,
            ) as lookup:
                self.assertEqual(
                    await service.image_refs_from_onebot_message(event, "quote"), []
                )
                lookup.assert_not_awaited()
            with patch(
                "astrbot_plugin_helper_tools.wallpaper_service.resolve_file",
                new_callable=AsyncMock,
            ) as resolve:
                with self.assertRaises(ValueError):
                    await service.read_image_ref(
                        event, ImageRef(file="missing-image-id")
                    )
                resolve.assert_not_awaited()

    async def test_official_client_never_becomes_poke_or_avatar_client(self):
        official_bot, onebot = object(), object()
        platforms = [
            SimpleNamespace(
                meta=lambda: SimpleNamespace(name="qq_official", id="official-a"),
                bot=official_bot,
            ),
            SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp", id="qq-onebot"),
                bot=onebot,
            ),
        ]
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=platforms)
        )
        with tempfile.TemporaryDirectory() as directory:
            poke = PokeService(
                {"poke": {"enabled": True, "scheduler": {"platform_id": "official-a"}}},
                Path(directory),
                context,
            )
            event = make_event()
            poke.remember_bot(event)
            self.assertIsNone(poke._last_bot)
            self.assertIsNone(poke._resolve_bot())
            poke._last_bot = onebot
            with patch.object(poke, "_send_with_bot", new_callable=AsyncMock) as send:
                await poke._run_scheduled_pokes()
                send.assert_not_awaited()
            self.assertIn("不提供", await poke.handle_command(event))
            self.assertIn("不提供", await poke.poke_from_tool(event, "2123456789"))
            self.assertEqual(
                (await poke.send_pokes(event, ["2123456789"], times=1)).attempted, 0
            )
            avatar = AvatarRotationService(
                {"qq_avatar": {"auto_change": {"platform_id": "official-a"}}},
                Path(directory),
                context,
            )
            self.assertIsNone(avatar._resolve_bot())
            avatar.config = {}
            self.assertIsNone(avatar._resolve_bot(event))
            onebot_event = make_event("aiocqhttp")
            self.assertIs(avatar._resolve_bot(onebot_event), onebot_event.bot)


if __name__ == "__main__":
    unittest.main()
