from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.permission import PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry

from astrbot_plugin_helper_tools.command_routing import (
    RequireWakePrefix,
    wake_command_text,
)
from astrbot_plugin_helper_tools.helper_utils import core_wake_prefixes
from astrbot_plugin_helper_tools.main import HelperToolsPlugin
from astrbot_plugin_helper_tools.poke_service import (
    POKE_SYNTHETIC_COMMAND_EXTRA,
    PokeNotice,
    PokeService,
    is_poke_synthetic_command,
)
from astrbot_plugin_helper_tools.wallpaper_service import WallpaperService


def make_event(
    original_text: str,
    *,
    group: bool = True,
    routed_text: str | None = None,
    messages: list[object] | None = None,
) -> AstrMessageEvent:
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.self_id = "10001"
    message.sender = MessageMember(user_id="20001", nickname="Alice")
    message.group_id = "30001" if group else ""
    message.message_id = "routing-test"
    message.message_str = original_text
    message.message = messages if messages is not None else [Comp.Plain(original_text)]
    message.raw_message = {}
    event = AstrMessageEvent(
        original_text if routed_text is None else routed_text,
        message,
        PlatformMetadata(name="aiocqhttp", description="routing test", id="qq-test"),
        "30001" if group else "20001",
    )
    # Private messages and @/reply wakes also have this flag without a prefix.
    event.is_at_or_wake_command = True
    return event


def command_handlers():
    return [
        handler
        for handler in star_handlers_registry.get_handlers_by_module_name(
            HelperToolsPlugin.__module__
        )
        if any(isinstance(item, CommandFilter) for item in handler.event_filters)
    ]


def matches_handler(handler, event, config) -> bool:
    return all(item.filter(event, config) for item in handler.event_filters)


class WakeCommandTextTests(unittest.TestCase):
    def test_private_and_group_use_original_prefix_after_core_rewrites_event(self):
        for group in (False, True):
            for routed_text in ("/qq_profile 20001", "qq_profile 20001"):
                with self.subTest(group=group, routed_text=routed_text):
                    event = make_event(
                        "/qq_profile 20001", group=group, routed_text=routed_text
                    )
                    self.assertEqual(
                        wake_command_text(event, ["/"]), "qq_profile 20001"
                    )
                    self.assertEqual(event.message_str, routed_text)
                    self.assertEqual(event.message_obj.message_str, "/qq_profile 20001")

    def test_private_and_group_reject_bare_commands_even_when_already_awake(self):
        for group in (False, True):
            for routed_text in ("qq_profile 20001", "/qq_profile 20001"):
                with self.subTest(group=group, routed_text=routed_text):
                    event = make_event(
                        "qq_profile 20001", group=group, routed_text=routed_text
                    )
                    event.is_wake = True
                    self.assertIsNone(wake_command_text(event, ["/"]))

    def test_other_bots_prefix_cannot_be_restored_by_rewritten_event(self):
        for group in (False, True):
            for routed_text in ("qq_profile", "/qq_profile"):
                with self.subTest(group=group, routed_text=routed_text):
                    event = make_event(
                        "!qq_profile", group=group, routed_text=routed_text
                    )
                    self.assertIsNone(wake_command_text(event, ["/"]))

    def test_empty_original_does_not_fall_back_to_rewritten_event(self):
        for group in (False, True):
            with self.subTest(group=group):
                event = make_event("", group=group, routed_text="/qq_profile")
                self.assertIsNone(wake_command_text(event, ["/"]))

    def test_simplified_events_fall_back_when_original_field_is_unavailable(self):
        for event in (
            SimpleNamespace(message_str="/qq_profile"),
            SimpleNamespace(message_str="/qq_profile", message_obj=SimpleNamespace()),
        ):
            with self.subTest(event=event):
                self.assertEqual(wake_command_text(event, ["/"]), "qq_profile")

    def test_prefix_order_is_preserved_and_only_one_prefix_is_removed(self):
        event = make_event("//qq_profile")
        self.assertEqual(wake_command_text(event, ["/", "//"]), "/qq_profile")
        self.assertEqual(wake_command_text(event, ["//", "/"]), "qq_profile")
        self.assertEqual(wake_command_text(event, iter(["", " ", "/"])), "/qq_profile")

    def test_empty_prefixes_never_enable_bare_commands(self):
        for prefixes in ([], [""], ["", " "]):
            for text in ("qq_profile", "/qq_profile"):
                with self.subTest(prefixes=prefixes, text=text):
                    self.assertIsNone(wake_command_text(make_event(text), prefixes))

    def test_prefix_only_is_a_match_with_an_empty_body(self):
        event = make_event("/", routed_text="")
        self.assertEqual(wake_command_text(event, ["/"]), "")
        self.assertTrue(RequireWakePrefix().filter(event, {"wake_prefix": ["/"]}))

    def test_group_leading_other_bot_is_rejected_after_reply_and_empty_plain(self):
        for leading in (
            [Comp.At(qq="10002")],
            [Comp.Reply(id="previous"), Comp.Plain(" \t"), Comp.At(qq="10002")],
        ):
            with self.subTest(leading=leading):
                event = make_event(
                    "/qq_profile",
                    routed_text="qq_profile",
                    messages=[*leading, Comp.Plain("/qq_profile")],
                )
                self.assertIsNone(wake_command_text(event, ["/"]))

    def test_group_leading_self_at_still_requires_prefix(self):
        for text, expected in (("/qq_profile", "qq_profile"), ("qq_profile", None)):
            with self.subTest(text=text):
                event = make_event(
                    text, messages=[Comp.At(qq="10001"), Comp.Plain(text)]
                )
                self.assertEqual(wake_command_text(event, ["/"]), expected)

    def test_at_after_command_is_a_target_and_does_not_block_routing(self):
        event = make_event(
            "/qq_profile",
            routed_text="qq_profile",
            messages=[Comp.Plain("/qq_profile"), Comp.At(qq="10002")],
        )
        self.assertEqual(wake_command_text(event, ["/"]), "qq_profile")

    def test_reply_with_prefixed_quoted_text_cannot_wake_bare_body(self):
        event = make_event(
            "qq_profile",
            messages=[
                Comp.Reply(id="previous", message_str="/qq_profile"),
                Comp.Plain("qq_profile"),
            ],
        )
        self.assertIsNone(wake_command_text(event, ["/"]))

    def test_reply_before_prefixed_command_is_allowed(self):
        event = make_event(
            "/qq_profile",
            routed_text="qq_profile",
            messages=[Comp.Reply(id="previous"), Comp.Plain("/qq_profile")],
        )
        self.assertEqual(wake_command_text(event, ["/"]), "qq_profile")

    def test_internal_poke_marker_allows_bare_body_with_empty_prefix_config(self):
        for group in (False, True):
            with self.subTest(group=group):
                event = make_event("qq_profile", group=group)
                event.set_extra(POKE_SYNTHETIC_COMMAND_EXTRA, {"command": "qq_profile"})
                self.assertTrue(is_poke_synthetic_command(event))
                self.assertEqual(wake_command_text(event, []), "qq_profile")
                self.assertTrue(RequireWakePrefix().filter(event, {"wake_prefix": []}))


class RequireWakePrefixTests(unittest.TestCase):
    def test_filter_uses_supplied_session_config_and_rejects_empty_or_missing_prefix(
        self,
    ):
        self.assertIsInstance(RequireWakePrefix(), filter.CustomFilter)
        event = make_event("!qq_profile", routed_text="qq_profile")
        for config, expected in (
            ({"wake_prefix": ["!"]}, True),
            ({"wake_prefix": ["/"]}, False),
            ({"wake_prefix": []}, False),
            ({}, False),
        ):
            with self.subTest(config=config):
                self.assertEqual(RequireWakePrefix().filter(event, config), expected)


class SessionWakePrefixTests(unittest.TestCase):
    def test_current_profile_is_used_by_main_helpers_and_registered_filter(self):
        group = make_event("!qq_profile", routed_text="qq_profile")
        private = make_event("#qq_profile", group=False, routed_text="qq_profile")
        profiles = {
            group.unified_msg_origin: {"wake_prefix": ["!"]},
            private.unified_msg_origin: {"wake_prefix": ["#"]},
        }
        getter = Mock(
            side_effect=lambda origin=None: (
                profiles[origin] if origin else {"wake_prefix": ["/"]}
            )
        )
        plugin = SimpleNamespace(context=SimpleNamespace(get_config=getter))
        for event, prefix in ((group, "!"), (private, "#")):
            with self.subTest(origin=event.unified_msg_origin):
                self.assertEqual(
                    core_wake_prefixes(plugin.context, event=event), [prefix]
                )
                getter.assert_called_with(event.unified_msg_origin)
                self.assertTrue(
                    HelperToolsPlugin._message_has_wake_prefix(plugin, event)
                )
                self.assertEqual(
                    HelperToolsPlugin._message_without_wake_prefix(plugin, event),
                    "qq_profile",
                )
                self.assertTrue(
                    RequireWakePrefix().filter(
                        event, profiles[event.unified_msg_origin]
                    )
                )
                event.message_obj.message_str = "/qq_profile"
                self.assertFalse(
                    HelperToolsPlugin._message_has_wake_prefix(plugin, event)
                )
                self.assertEqual(
                    HelperToolsPlugin._message_without_wake_prefix(plugin, event), ""
                )
        self.assertTrue(all(call.args for call in getter.call_args_list))

    def test_explicit_empty_profile_never_falls_back_to_global_or_default(self):
        event = make_event("/qq_profile", routed_text="qq_profile")
        for value in ([], "", ["", " "]):
            with self.subTest(value=value):
                getter = Mock(
                    side_effect=lambda origin=None, prefixes=value: {
                        "wake_prefix": prefixes if origin else ["/"]
                    }
                )
                plugin = SimpleNamespace(context=SimpleNamespace(get_config=getter))
                self.assertEqual(
                    core_wake_prefixes(plugin.context, ["!"], event=event), []
                )
                self.assertFalse(
                    HelperToolsPlugin._message_has_wake_prefix(plugin, event)
                )
                getter.assert_called_with(event.unified_msg_origin)

    def test_legacy_no_argument_config_getter_remains_supported(self):
        class LegacyContext:
            def get_config(self):
                return {"wake_prefix": ["!"]}

        event = make_event("!qq_profile", routed_text="qq_profile")
        plugin = SimpleNamespace(context=LegacyContext())
        self.assertEqual(core_wake_prefixes(plugin.context, event=event), ["!"])
        self.assertTrue(HelperToolsPlugin._message_has_wake_prefix(plugin, event))

    def test_legacy_empty_config_does_not_restore_slash(self):
        class LegacyContext:
            def get_config(self):
                return {"wake_prefix": []}

        self.assertEqual(
            core_wake_prefixes(LegacyContext(), event=make_event("/x")), []
        )

    def test_without_event_reads_global_config_and_missing_key_uses_default(self):
        getter = Mock(return_value={"wake_prefix": ["!"]})
        self.assertEqual(core_wake_prefixes(SimpleNamespace(get_config=getter)), ["!"])
        getter.assert_called_once_with()
        self.assertEqual(core_wake_prefixes(SimpleNamespace()), ["/"])
        self.assertEqual(core_wake_prefixes(SimpleNamespace(), ["#"]), ["#"])


class RegisteredCommandRoutingTests(unittest.TestCase):
    def setUp(self):
        self.handlers = command_handlers()
        self.assertTrue(
            self.handlers, "main must register commands in AstrBot's real registry"
        )

    def handler(self, name):
        return next(item for item in self.handlers if item.handler_name == name)

    def test_all_registered_commands_check_wake_before_parameters_and_permissions(self):
        for handler in self.handlers:
            with self.subTest(handler=handler.handler_name):
                self.assertIsInstance(handler.event_filters[0], RequireWakePrefix)
                for index, item in enumerate(handler.event_filters):
                    if isinstance(item, (CommandFilter, PermissionTypeFilter)):
                        self.assertGreater(index, 0)

    def test_all_bare_commands_short_circuit_before_parameter_and_permission_checks(
        self,
    ):
        with (
            patch.object(
                CommandFilter,
                "validate_and_convert_params",
                side_effect=AssertionError(
                    "a bare command reached parameter validation"
                ),
            ) as validate,
            patch.object(
                PermissionTypeFilter,
                "filter",
                side_effect=AssertionError(
                    "a bare command reached permission handling"
                ),
            ) as permission,
        ):
            for handler in self.handlers:
                command = next(
                    item
                    for item in handler.event_filters
                    if isinstance(item, CommandFilter)
                )
                for group in (False, True):
                    for name in command.get_complete_command_names():
                        with self.subTest(
                            handler=handler.handler_name, group=group, name=name
                        ):
                            # Includes helper_x_post/helper_x_recent with required arguments absent.
                            event = make_event(name, group=group)
                            self.assertFalse(
                                matches_handler(handler, event, {"wake_prefix": ["/"]})
                            )
                            self.assertIsNone(event.get_extra("parsed_params"))
            validate.assert_not_called()
            permission.assert_not_called()

    def test_registered_command_parses_prefixed_body_and_alias_after_core_rewrite(self):
        handler = self.handler("qq_profile_command")
        for name in ("qq_profile", "开盒"):
            for group in (False, True):
                with self.subTest(name=name, group=group):
                    event = make_event(
                        f"/{name} 20002", group=group, routed_text=f"{name} 20002"
                    )
                    self.assertTrue(
                        matches_handler(handler, event, {"wake_prefix": ["/"]})
                    )
                    self.assertEqual(event.get_extra("parsed_params")["qq_id"], 20002)

    def test_registered_command_rejects_other_prefix_empty_config_at_and_reply_wakes(
        self,
    ):
        handler = self.handler("qq_profile_command")
        cases = [
            (
                make_event("!qq_profile", routed_text="qq_profile"),
                {"wake_prefix": ["/"]},
            ),
            (make_event("/qq_profile", routed_text="qq_profile"), {"wake_prefix": []}),
            (
                make_event(
                    "qq_profile",
                    messages=[Comp.At(qq="10001"), Comp.Plain("qq_profile")],
                ),
                {"wake_prefix": ["/"]},
            ),
            (
                make_event(
                    "qq_profile",
                    messages=[Comp.Reply(id="previous"), Comp.Plain("qq_profile")],
                ),
                {"wake_prefix": ["/"]},
            ),
            (
                make_event(
                    "/qq_profile",
                    routed_text="qq_profile",
                    messages=[Comp.At(qq="10002"), Comp.Plain("/qq_profile")],
                ),
                {"wake_prefix": ["/"]},
            ),
        ]
        for event, config in cases:
            with self.subTest(
                text=event.message_obj.message_str,
                config=config,
                messages=event.get_messages(),
            ):
                self.assertFalse(matches_handler(handler, event, config))
                self.assertIsNone(event.get_extra("parsed_params"))

    def test_registered_command_keeps_target_at_after_body(self):
        event = make_event(
            "/qq_profile",
            routed_text="qq_profile",
            messages=[Comp.Plain("/qq_profile"), Comp.At(qq="20002")],
        )
        self.assertTrue(
            matches_handler(
                self.handler("qq_profile_command"), event, {"wake_prefix": ["/"]}
            )
        )
        self.assertEqual(
            event.get_extra("parsed_params"), {"qq_id": None, "group_id": None}
        )

    def test_actual_poke_generated_command_routes_with_empty_prefix_config(self):
        event = make_event("", group=True)
        notice = PokeNotice(
            user_id="20001", target_id="10001", self_id="10001", group_id="30001"
        )
        synthetic = PokeService._build_synthetic_command_event(
            object.__new__(PokeService), event, notice, "qq_profile"
        )
        self.assertIsNotNone(synthetic)
        self.assertTrue(is_poke_synthetic_command(synthetic))
        self.assertTrue(
            matches_handler(
                self.handler("qq_profile_command"), synthetic, {"wake_prefix": []}
            )
        )
        self.assertIsNotNone(synthetic.get_extra("parsed_params"))


class DynamicWallpaperRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_bots_only_route_wallpaper_actions_to_the_matching_prefix(self):
        cases = (
            ("存图 风景", "add_images_from_event"),
            ("删图", "delete_replied_wallpaper"),
            ("风景", "send_random_wallpaper"),
        )
        for group in (False, True):
            for body, method in cases:
                for prefix in ("爱丽丝", "小助手", "", "/"):
                    with self.subTest(group=group, body=body, prefix=prefix):
                        actions = []
                        for bot_prefix in ("爱丽丝", "小助手"):
                            # Default config differs from the active session;
                            # the other bot must ignore even an already stripped body.
                            context = SimpleNamespace(
                                get_config=lambda origin=None, active=bot_prefix: {
                                    "wake_prefix": [active] if origin else ["/"]
                                }
                            )
                            config = {
                                "wallpaper": {
                                    "stop_event_after_response": False,
                                    "libraries": [
                                        {"name": "风景", "commands": ["风景"]}
                                    ],
                                }
                            }
                            service = WallpaperService(config, Path("."), context)
                            plugin = object.__new__(HelperToolsPlugin)
                            plugin.context, plugin.config = context, config
                            plugin.qq_like = SimpleNamespace(
                                handle_message=AsyncMock(
                                    return_value=SimpleNamespace(handled=False)
                                )
                            )
                            plugin.wallpaper = service
                            plugin.steam = SimpleNamespace(
                                match_message=Mock(
                                    return_value=SimpleNamespace(handled=False)
                                )
                            )
                            plugin.voice = SimpleNamespace(
                                should_handle_message=Mock(return_value=False)
                            )
                            event = make_event(
                                prefix + body, group=group, routed_text=body
                            )
                            with patch.object(
                                service, method, AsyncMock(return_value="")
                            ) as action:
                                results = [
                                    item
                                    async for item in plugin.dynamic_message_handler(
                                        event
                                    )
                                ]
                            self.assertEqual(results, [])
                            actions.append(action.await_count)
                        self.assertEqual(
                            actions,
                            [int(prefix == "爱丽丝"), int(prefix == "小助手")],
                        )


if __name__ == "__main__":
    unittest.main()
