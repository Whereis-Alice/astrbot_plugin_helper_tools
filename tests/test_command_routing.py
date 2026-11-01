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
    RequireCommandAddress,
    addressed_command_text,
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
    self_id: str = "10001",
    awake: bool = True,
) -> AstrMessageEvent:
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.self_id = self_id
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
    # Exercise both misleading wake flags and core private-message waking disabled.
    event.is_at_or_wake_command = awake
    return event


def direct_events(original_text, *, routed_text=None, awake=False):
    yield (
        "private",
        make_event(original_text, group=False, routed_text=routed_text, awake=awake),
    )
    for label, leading in (
        ("self_at", [Comp.At(qq="10001")]),
        (
            "self_at_after_reply_and_whitespace",
            [Comp.Reply(id="previous"), Comp.Plain(" \t"), Comp.At(qq=10001)],
        ),
        ("self_reply", [Comp.Reply(id="previous", sender_id="10001")]),
        ("numeric_self_reply", [Comp.Reply(id="previous", sender_id=10001)]),
    ):
        yield (
            label,
            make_event(
                original_text,
                routed_text=routed_text,
                messages=[*leading, Comp.Plain(original_text)],
                awake=awake,
            ),
        )


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


class AddressedCommandTextTests(unittest.TestCase):
    def test_private_and_group_use_original_prefix_after_core_rewrites_event(self):
        for group in (False, True):
            for routed_text in ("/qq_profile 20001", "qq_profile 20001"):
                with self.subTest(group=group, routed_text=routed_text):
                    event = make_event(
                        "/qq_profile 20001", group=group, routed_text=routed_text
                    )
                    self.assertEqual(
                        addressed_command_text(event, ["/"]), "qq_profile 20001"
                    )
                    self.assertEqual(event.message_str, routed_text)
                    self.assertEqual(event.message_obj.message_str, "/qq_profile 20001")

    def test_group_rejects_bare_commands_even_when_already_awake(self):
        for routed_text in ("qq_profile 20001", "/qq_profile 20001"):
            with self.subTest(routed_text=routed_text):
                event = make_event("qq_profile 20001", routed_text=routed_text)
                event.is_wake = True
                self.assertIsNone(addressed_command_text(event, ["/"]))

    def test_direct_addresses_use_original_bare_body_without_relying_on_wake_flags(
        self,
    ):
        for routed_text in ("qq_profile 20001", "/qq_profile 20002", "", "删图"):
            for awake in (False, True):
                for label, event in direct_events(
                    "qq_profile 20001", routed_text=routed_text, awake=awake
                ):
                    with self.subTest(
                        address=label, routed_text=routed_text, awake=awake
                    ):
                        event.is_wake = awake
                        self.assertEqual(
                            addressed_command_text(event, ["/"]), "qq_profile 20001"
                        )
                        self.assertEqual(event.message_str, routed_text)
                        self.assertEqual(
                            event.message_obj.message_str, "qq_profile 20001"
                        )

    def test_other_bots_prefix_cannot_be_restored_by_rewritten_event(self):
        for group in (False, True):
            for routed_text in ("qq_profile", "/qq_profile"):
                with self.subTest(group=group, routed_text=routed_text):
                    event = make_event(
                        "!qq_profile", group=group, routed_text=routed_text
                    )
                    self.assertEqual(
                        addressed_command_text(event, ["/"]),
                        None if group else "!qq_profile",
                    )

    def test_empty_original_does_not_fall_back_to_rewritten_event(self):
        for group in (False, True):
            with self.subTest(group=group):
                event = make_event("", group=group, routed_text="/qq_profile")
                self.assertIn(addressed_command_text(event, ["/"]), (None, ""))

        for label, event in direct_events("", routed_text="/qq_profile"):
            with self.subTest(address=label):
                # Even component text must not replace an explicitly empty original.
                event.message_obj.message.append(Comp.Plain("/qq_profile"))
                self.assertIn(addressed_command_text(event, ["/"]), (None, ""))

    def test_simplified_events_fall_back_when_original_field_is_unavailable(self):
        for event in (
            SimpleNamespace(message_str="/qq_profile"),
            SimpleNamespace(message_str="/qq_profile", message_obj=SimpleNamespace()),
        ):
            with self.subTest(event=event):
                self.assertEqual(addressed_command_text(event, ["/"]), "qq_profile")

    def test_prefix_order_is_preserved_and_only_one_prefix_is_removed(self):
        events = [
            ("group_prefix", make_event("//qq_profile")),
            *direct_events("//qq_profile"),
        ]
        for label, event in events:
            with self.subTest(address=label):
                self.assertEqual(
                    addressed_command_text(event, ["/", "//"]), "/qq_profile"
                )
                self.assertEqual(
                    addressed_command_text(event, ["//", "/"]), "qq_profile"
                )
                self.assertEqual(
                    addressed_command_text(event, iter(["", " ", "/"])), "/qq_profile"
                )

    def test_empty_prefixes_never_enable_unaddressed_group_commands(self):
        for prefixes in ([], [""], ["", " "]):
            for text in ("qq_profile", "/qq_profile"):
                with self.subTest(prefixes=prefixes, text=text):
                    self.assertIsNone(
                        addressed_command_text(make_event(text), prefixes)
                    )

    def test_empty_prefixes_keep_direct_addresses_and_do_not_strip_unconfigured_prefix(
        self,
    ):
        for prefixes in ([], [""], ["", " "]):
            for text in ("qq_profile", "/qq_profile"):
                for label, event in direct_events(text):
                    with self.subTest(prefixes=prefixes, text=text, address=label):
                        self.assertEqual(addressed_command_text(event, prefixes), text)

    def test_prefix_only_is_a_match_with_an_empty_body(self):
        event = make_event("/", routed_text="")
        self.assertEqual(addressed_command_text(event, ["/"]), "")
        self.assertTrue(RequireCommandAddress().filter(event, {"wake_prefix": ["/"]}))

    def test_group_leading_other_bot_is_rejected_after_reply_and_empty_plain(self):
        for sender_id in (None, "10001", "10002", "20001"):
            for text in ("qq_profile", "/qq_profile"):
                for leading in (
                    [Comp.At(qq="10002")],
                    [
                        Comp.Reply(id="previous", sender_id=sender_id),
                        Comp.Plain(" \t"),
                        Comp.At(qq=10002),
                        Comp.At(qq="10001"),
                    ],
                ):
                    with self.subTest(sender_id=sender_id, text=text, leading=leading):
                        event = make_event(
                            text,
                            routed_text="qq_profile",
                            messages=[*leading, Comp.Plain(text)],
                        )
                        self.assertIsNone(addressed_command_text(event, ["/"]))

    def test_group_leading_self_at_allows_bare_or_prefixed_body(self):
        for text in ("/qq_profile", "qq_profile"):
            with self.subTest(text=text):
                event = make_event(
                    text, messages=[Comp.At(qq="10001"), Comp.Plain(text)]
                )
                self.assertEqual(addressed_command_text(event, ["/"]), "qq_profile")

    def test_at_all_does_not_exempt_the_group_prefix(self):
        for at in (Comp.At(qq="all"), Comp.AtAll()):
            for text, expected in (("qq_profile", None), ("/qq_profile", "qq_profile")):
                with self.subTest(at=at, text=text):
                    event = make_event(text, messages=[at, Comp.Plain(text)])
                    self.assertEqual(addressed_command_text(event, ["/"]), expected)

    def test_reply_author_must_match_the_current_bot(self):
        for sender_id in ("10001", 10001, "10002", "20001", None, "", 0):
            for self_id in ("10001", "10002"):
                with self.subTest(sender_id=sender_id, self_id=self_id):
                    event = make_event(
                        "qq_profile",
                        self_id=self_id,
                        messages=[
                            Comp.Reply(
                                id="previous",
                                sender_id=sender_id,
                                message_str="/qq_profile",
                            ),
                            Comp.Plain("qq_profile"),
                        ],
                    )
                    self.assertEqual(
                        addressed_command_text(event, ["/"]),
                        "qq_profile" if str(sender_id) == self_id else None,
                    )

    def test_at_after_command_is_a_target_and_does_not_block_routing(self):
        event = make_event(
            "/qq_profile",
            routed_text="qq_profile",
            messages=[Comp.Plain("/qq_profile"), Comp.At(qq="10002")],
        )
        self.assertEqual(addressed_command_text(event, ["/"]), "qq_profile")

        event.message_obj.message_str = "qq_profile"
        self.assertIsNone(addressed_command_text(event, ["/"]))

    def test_reply_with_prefixed_quoted_text_cannot_wake_bare_body(self):
        event = make_event(
            "qq_profile",
            messages=[
                Comp.Reply(id="previous", message_str="/qq_profile"),
                Comp.Plain("qq_profile"),
            ],
        )
        self.assertIsNone(addressed_command_text(event, ["/"]))

    def test_reply_before_prefixed_command_is_allowed(self):
        for sender_id in ("10001", "10002", "20001", None):
            with self.subTest(sender_id=sender_id):
                event = make_event(
                    "/qq_profile",
                    routed_text="qq_profile",
                    messages=[
                        Comp.Reply(id="previous", sender_id=sender_id),
                        Comp.Plain("/qq_profile"),
                    ],
                )
                self.assertEqual(addressed_command_text(event, ["/"]), "qq_profile")

    def test_internal_poke_marker_allows_bare_body_with_empty_prefix_config(self):
        for group in (False, True):
            with self.subTest(group=group):
                event = make_event("qq_profile", group=group)
                event.set_extra(POKE_SYNTHETIC_COMMAND_EXTRA, {"command": "qq_profile"})
                self.assertTrue(is_poke_synthetic_command(event))
                self.assertEqual(addressed_command_text(event, []), "qq_profile")
                self.assertTrue(
                    RequireCommandAddress().filter(event, {"wake_prefix": []})
                )


class RequireCommandAddressTests(unittest.TestCase):
    def test_filter_uses_supplied_session_config_and_rejects_empty_or_missing_prefix(
        self,
    ):
        self.assertIsInstance(RequireCommandAddress(), filter.CustomFilter)
        for config, expected in (
            ({"wake_prefix": ["!"]}, True),
            ({"wake_prefix": ["/"]}, False),
            ({"wake_prefix": []}, False),
            ({}, False),
        ):
            with self.subTest(config=config):
                event = make_event("!qq_profile", routed_text="qq_profile", awake=False)
                self.assertEqual(
                    RequireCommandAddress().filter(event, config), expected
                )
                self.assertFalse(event.is_at_or_wake_command)
                self.assertEqual(event.message_str, "qq_profile")
                self.assertEqual(event.message_obj.message_str, "!qq_profile")

    def test_unbound_filter_checks_direct_address_without_changing_text_or_wake_flag(
        self,
    ):
        for config in ({"wake_prefix": ["/"]}, {"wake_prefix": []}, {}):
            for label, event in direct_events("qq_profile 20001", routed_text="删图"):
                with self.subTest(config=config, address=label):
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertTrue(RequireCommandAddress().filter(event, config))
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertEqual(event.message_str, "删图")
                    self.assertEqual(event.message_obj.message_str, "qq_profile 20001")

    def test_bound_filter_normalizes_matching_original_command_and_sets_wake_flag(self):
        address_filter = RequireCommandAddress(command_names=("qq_profile",))
        for config in ({"wake_prefix": ["/"]}, {"wake_prefix": []}, {}):
            for label, event in direct_events("qq_profile 20001", routed_text="删图"):
                with self.subTest(config=config, address=label):
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertTrue(address_filter.filter(event, config))
                    self.assertTrue(event.is_at_or_wake_command)
                    self.assertEqual(event.message_str, "qq_profile 20001")
                    self.assertEqual(event.message_obj.message_str, "qq_profile 20001")

    def test_bound_filter_does_not_wake_or_normalize_unmatched_direct_text(self):
        address_filter = RequireCommandAddress(command_names=("qq_profile",))
        for original in ("普通私聊文本", "qq_profile_extra 20001", "qq_avatar 20001"):
            for label, event in direct_events(original, routed_text="qq_profile 20001"):
                with self.subTest(original=original, address=label):
                    self.assertFalse(
                        address_filter.filter(event, {"wake_prefix": ["/"]})
                    )
                    self.assertFalse(event.is_at_or_wake_command)
                    self.assertEqual(event.message_str, "qq_profile 20001")
                    self.assertEqual(event.message_obj.message_str, original)


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
                    HelperToolsPlugin._message_has_command_address(plugin, event)
                )
                self.assertEqual(
                    HelperToolsPlugin._addressed_command_body(plugin, event),
                    "qq_profile",
                )
                self.assertTrue(
                    RequireCommandAddress().filter(
                        event, profiles[event.unified_msg_origin]
                    )
                )
                event.message_obj.message_str = "/qq_profile"
                is_private = event is private
                self.assertEqual(
                    HelperToolsPlugin._message_has_command_address(plugin, event),
                    is_private,
                )
                self.assertEqual(
                    HelperToolsPlugin._addressed_command_body(plugin, event),
                    "/qq_profile" if is_private else "",
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
                    HelperToolsPlugin._message_has_command_address(plugin, event)
                )
                getter.assert_called_with(event.unified_msg_origin)

    def test_explicit_empty_profile_keeps_private_at_and_own_reply_addresses(self):
        for value in ([], "", ["", " "]):
            for label, event in direct_events("qq_profile", routed_text="/qq_profile"):
                with self.subTest(value=value, address=label):
                    getter = Mock(
                        side_effect=lambda origin=None, prefixes=value: {
                            "wake_prefix": prefixes if origin else ["/"]
                        }
                    )
                    plugin = SimpleNamespace(context=SimpleNamespace(get_config=getter))
                    self.assertEqual(
                        core_wake_prefixes(plugin.context, event=event), []
                    )
                    self.assertTrue(
                        HelperToolsPlugin._message_has_command_address(plugin, event)
                    )
                    self.assertEqual(
                        HelperToolsPlugin._addressed_command_body(plugin, event),
                        "qq_profile",
                    )
                    getter.assert_called_with(event.unified_msg_origin)

    def test_legacy_no_argument_config_getter_remains_supported(self):
        class LegacyContext:
            def get_config(self):
                return {"wake_prefix": ["!"]}

        event = make_event("!qq_profile", routed_text="qq_profile")
        plugin = SimpleNamespace(context=LegacyContext())
        self.assertEqual(core_wake_prefixes(plugin.context, event=event), ["!"])
        self.assertTrue(HelperToolsPlugin._message_has_command_address(plugin, event))

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
        self.assertEqual(len(self.handlers), 25)

    def handler(self, name):
        return next(item for item in self.handlers if item.handler_name == name)

    def test_all_registered_commands_check_address_before_parameters_and_permissions(
        self,
    ):
        for handler in self.handlers:
            with self.subTest(handler=handler.handler_name):
                self.assertIsInstance(handler.event_filters[0], RequireCommandAddress)
                for index, item in enumerate(handler.event_filters):
                    if isinstance(item, (CommandFilter, PermissionTypeFilter)):
                        self.assertGreater(index, 0)

    def test_all_bare_group_commands_short_circuit_before_parameter_and_permission_checks(
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
                for name in command.get_complete_command_names():
                    with self.subTest(handler=handler.handler_name, name=name):
                        # Includes helper_x_post/helper_x_recent with required arguments absent.
                        event = make_event(name)
                        self.assertFalse(
                            matches_handler(handler, event, {"wake_prefix": ["/"]})
                        )
                        self.assertIsNone(event.get_extra("parsed_params"))
            validate.assert_not_called()
            permission.assert_not_called()

    def test_all_registered_address_filters_normalize_their_own_command_and_aliases(
        self,
    ):
        for handler in self.handlers:
            command = next(
                item
                for item in handler.event_filters
                if isinstance(item, CommandFilter)
            )
            for name in command.get_complete_command_names():
                with self.subTest(handler=handler.handler_name, name=name):
                    original = f"/{name}  unchanged arguments"
                    event = make_event(
                        original,
                        group=False,
                        routed_text="ordinary rewritten text",
                        awake=False,
                    )
                    self.assertTrue(
                        handler.event_filters[0].filter(event, {"wake_prefix": ["/"]})
                    )
                    self.assertTrue(event.is_at_or_wake_command)
                    self.assertEqual(event.message_str, f"{name}  unchanged arguments")
                    self.assertEqual(event.message_obj.message_str, original)

    def test_registered_address_filter_tracks_core_command_name_and_alias_changes(self):
        handler = self.handler("qq_profile_command")
        parser = next(
            item for item in handler.event_filters if isinstance(item, CommandFilter)
        )
        with (
            patch.object(parser, "command_name", "个人档案"),
            patch.object(parser, "alias", {"查询档案"}),
            patch.object(parser, "_cmpl_cmd_names", None),
        ):
            for name in ("个人档案", "查询档案"):
                for label, event in direct_events(
                    f"{name} 20002", routed_text="", awake=False
                ):
                    with self.subTest(name=name, address=label):
                        self.assertTrue(
                            matches_handler(handler, event, {"wake_prefix": []})
                        )
                        self.assertEqual(
                            event.get_extra("parsed_params")["qq_id"], 20002
                        )
            event = make_event("qq_profile 20002", group=False, awake=False)
            self.assertFalse(matches_handler(handler, event, {"wake_prefix": []}))
            self.assertFalse(event.is_at_or_wake_command)

    def test_unmatched_private_text_never_becomes_awake_or_reaches_command_validation(
        self,
    ):
        with (
            patch.object(
                CommandFilter,
                "validate_and_convert_params",
                side_effect=AssertionError(
                    "ordinary private text reached parameter validation"
                ),
            ) as validate,
            patch.object(
                PermissionTypeFilter,
                "filter",
                side_effect=AssertionError(
                    "ordinary private text reached permission handling"
                ),
            ) as permission,
        ):
            for config in ({"wake_prefix": ["/"]}, {"wake_prefix": []}, {}):
                for original in (
                    "你好，今天怎么样？",
                    "/你好，今天怎么样？",
                    "qq_profile_extra 20002",
                    "开盒看看 20002",
                    "请聊聊 qq_profile",
                ):
                    for routed_text in (original, "qq_profile 20002"):
                        with self.subTest(
                            config=config, original=original, routed_text=routed_text
                        ):
                            event = make_event(
                                original,
                                group=False,
                                routed_text=routed_text,
                                awake=False,
                            )
                            # Reuse the event across every registered handler to catch wake leakage.
                            for handler in self.handlers:
                                with self.subTest(handler=handler.handler_name):
                                    self.assertFalse(
                                        matches_handler(handler, event, config)
                                    )
                                    self.assertFalse(event.is_at_or_wake_command)
                                    self.assertEqual(event.message_str, routed_text)
                                    self.assertEqual(
                                        event.message_obj.message_str, original
                                    )
                                    self.assertIsNone(event.get_extra("parsed_params"))
            validate.assert_not_called()
            permission.assert_not_called()

    def test_registered_command_parses_prefixed_body_and_alias_after_core_rewrite(self):
        handler = self.handler("qq_profile_command")
        for name in ("qq_profile", "开盒"):
            for group in (False, True):
                for routed_text in (f"/{name} 20002", f"{name} 20002", f"{name} 20003"):
                    with self.subTest(name=name, group=group, routed_text=routed_text):
                        event = make_event(
                            f"/{name} 20002",
                            group=group,
                            routed_text=routed_text,
                            awake=False,
                        )
                        self.assertTrue(
                            matches_handler(handler, event, {"wake_prefix": ["/"]})
                        )
                        self.assertTrue(event.is_at_or_wake_command)
                        self.assertEqual(event.message_str, f"{name} 20002")
                        self.assertEqual(
                            event.message_obj.message_str, f"/{name} 20002"
                        )
                        self.assertEqual(
                            event.get_extra("parsed_params")["qq_id"], 20002
                        )

    def test_registered_command_accepts_direct_bare_addresses_when_core_wake_is_disabled(
        self,
    ):
        handler = self.handler("qq_profile_command")
        for name in ("qq_profile", "开盒"):
            for config in ({"wake_prefix": ["/"]}, {"wake_prefix": []}, {}):
                for label, event in direct_events(
                    f"{name} 20002", routed_text=f"{name} 20003"
                ):
                    with self.subTest(name=name, config=config, address=label):
                        self.assertFalse(event.is_at_or_wake_command)
                        self.assertTrue(matches_handler(handler, event, config))
                        self.assertTrue(event.is_at_or_wake_command)
                        self.assertEqual(event.message_str, f"{name} 20002")
                        self.assertEqual(
                            event.get_extra("parsed_params")["qq_id"], 20002
                        )

    def test_registered_command_rejects_other_prefix_empty_group_config_and_foreign_addresses(
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
                    messages=[Comp.At(qq="all"), Comp.Plain("qq_profile")],
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
        cases.append(
            (
                make_event("!qq_profile", group=False, routed_text="qq_profile"),
                {"wake_prefix": ["/"]},
            )
        )
        for sender_id in ("10002", "20001", None):
            cases.append(
                (
                    make_event(
                        "qq_profile",
                        messages=[
                            Comp.Reply(
                                id="previous",
                                sender_id=sender_id,
                                message_str="/qq_profile",
                            ),
                            Comp.Plain("qq_profile"),
                        ],
                    ),
                    {"wake_prefix": ["/"]},
                )
            )
        cases.append(
            (
                make_event(
                    "/qq_profile",
                    routed_text="qq_profile",
                    messages=[
                        Comp.Reply(id="previous", sender_id="10001"),
                        Comp.Plain(" \t"),
                        Comp.At(qq="10002"),
                        Comp.Plain("/qq_profile"),
                    ],
                ),
                {"wake_prefix": ["/"]},
            )
        )
        for _label, event in direct_events("", routed_text="qq_profile"):
            cases.append((event, {"wake_prefix": ["/"]}))
        for event, config in cases:
            with self.subTest(
                text=event.message_obj.message_str,
                config=config,
                messages=event.get_messages(),
            ):
                self.assertFalse(matches_handler(handler, event, config))
                self.assertIsNone(event.get_extra("parsed_params"))

    def test_registered_command_checks_reply_and_at_ownership_for_each_bot(self):
        handler = self.handler("qq_profile_command")
        for addressed_id in ("10001", "10002", "20001", None):
            for kind in ("at", "reply", "reply_then_at"):
                if kind != "reply" and addressed_id is None:
                    continue
                routed = []
                for self_id in ("10001", "10002"):
                    with self.subTest(
                        addressed_id=addressed_id, kind=kind, self_id=self_id
                    ):
                        leading = (
                            [Comp.Reply(id="previous", sender_id=addressed_id)]
                            if kind == "reply"
                            else [Comp.At(qq=addressed_id)]
                        )
                        if kind == "reply_then_at":
                            leading = [
                                Comp.Reply(id="previous", sender_id="10001"),
                                Comp.Plain(" \t"),
                                *leading,
                            ]
                        event = make_event(
                            "qq_profile 20002",
                            self_id=self_id,
                            messages=[*leading, Comp.Plain("qq_profile 20002")],
                            awake=False,
                        )
                        result = matches_handler(handler, event, {"wake_prefix": ["/"]})
                        routed.append(result)
                        if result:
                            self.assertEqual(
                                event.get_extra("parsed_params")["qq_id"], 20002
                            )
                        else:
                            self.assertIsNone(event.get_extra("parsed_params"))
                self.assertEqual(
                    routed, [addressed_id == "10001", addressed_id == "10002"]
                )

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
    ACTIONS = (
        ("存图 风景", "add_images_from_event"),
        ("删图", "delete_replied_wallpaper"),
        ("风景", "send_random_wallpaper"),
    )

    async def route_actions(
        self,
        original_text,
        method,
        *,
        group=True,
        messages=None,
        routed_text=None,
        prefixes=None,
        awake=True,
    ):
        actions = []
        for bot_id, bot_prefix in (("10001", "爱丽丝"), ("10002", "小助手")):
            active_prefixes = [bot_prefix] if prefixes is None else prefixes
            # The global profile differs from each bot's active session profile.
            context = SimpleNamespace(
                get_config=lambda origin=None, active=active_prefixes: {
                    "wake_prefix": active if origin else ["/"]
                }
            )
            config = {
                "wallpaper": {
                    "stop_event_after_response": False,
                    "libraries": [{"name": "风景", "commands": ["风景"]}],
                }
            }
            service = WallpaperService(config, Path("."), context)
            plugin = object.__new__(HelperToolsPlugin)
            plugin.context, plugin.config = context, config
            plugin.qq_like = SimpleNamespace(
                handle_message=AsyncMock(return_value=SimpleNamespace(handled=False))
            )
            plugin.wallpaper = service
            plugin.steam = SimpleNamespace(
                match_message=Mock(return_value=SimpleNamespace(handled=False))
            )
            plugin.voice = SimpleNamespace(
                should_handle_message=Mock(return_value=False)
            )
            event = make_event(
                original_text,
                group=group,
                routed_text=routed_text,
                messages=messages,
                self_id=bot_id,
                awake=awake,
            )
            # Keep WallpaperService.handle_message real and isolate its file/send actions.
            with patch.multiple(
                service,
                **{name: AsyncMock(return_value="") for _body, name in self.ACTIONS},
            ):
                results = [item async for item in plugin.dynamic_message_handler(event)]
                self.assertEqual(results, [])
                action = getattr(service, method)
                actions.append(action.await_count)
                for _body, other in self.ACTIONS:
                    if other != method:
                        getattr(service, other).assert_not_awaited()
                if action.await_count:
                    if method == "add_images_from_event":
                        action.assert_awaited_once_with(event, "风景")
                    elif method == "delete_replied_wallpaper":
                        action.assert_awaited_once_with(event)
                    else:
                        self.assertEqual(action.await_count, 1)
                        self.assertIs(action.await_args.args[0], event)
                        self.assertEqual(action.await_args.args[1].name, "风景")
        return actions

    async def test_two_bots_use_session_prefixes_and_private_bare_words_execute(self):
        for group in (False, True):
            for body, method in self.ACTIONS:
                for prefix in (
                    ("爱丽丝", "小助手", "", "/") if group else ("爱丽丝", "小助手", "")
                ):
                    with self.subTest(group=group, body=body, prefix=prefix):
                        actions = await self.route_actions(
                            prefix + body,
                            method,
                            group=group,
                            routed_text=body,
                            awake=group,
                        )
                        self.assertEqual(
                            actions,
                            [1, 1]
                            if not group and not prefix
                            else [int(prefix == "爱丽丝"), int(prefix == "小助手")],
                        )

    async def test_group_wallpaper_at_and_reply_ownership_routes_only_the_addressed_bot(
        self,
    ):
        cases = (
            ("first_at", [Comp.At(qq="10001")], [1, 0]),
            ("second_at", [Comp.At(qq=10002)], [0, 1]),
            ("foreign_at", [Comp.At(qq="10003")], [0, 0]),
            ("at_all", [Comp.At(qq="all")], [0, 0]),
            ("first_reply", [Comp.Reply(id="previous", sender_id="10001")], [1, 0]),
            ("second_reply", [Comp.Reply(id="previous", sender_id=10002)], [0, 1]),
            ("human_reply", [Comp.Reply(id="previous", sender_id="20001")], [0, 0]),
            (
                "unknown_reply",
                [Comp.Reply(id="previous", message_str="爱丽丝风景")],
                [0, 0],
            ),
            (
                "reply_then_other_at",
                [
                    Comp.Reply(id="previous", sender_id="10001"),
                    Comp.Plain(" \t"),
                    Comp.At(qq="10002"),
                ],
                [0, 1],
            ),
            (
                "reply_then_foreign_at",
                [
                    Comp.Reply(id="previous", sender_id="10001"),
                    Comp.Plain(" \t"),
                    Comp.At(qq="10003"),
                ],
                [0, 0],
            ),
        )
        for body, method in self.ACTIONS:
            for label, leading, expected in cases:
                with self.subTest(body=body, address=label):
                    self.assertEqual(
                        await self.route_actions(
                            body,
                            method,
                            messages=[*leading, Comp.Plain(body)],
                            awake=False,
                        ),
                        expected,
                    )

    async def test_group_leading_foreign_at_overrides_matching_wallpaper_prefix(self):
        for body, method in self.ACTIONS:
            for prefix in ("爱丽丝", "小助手"):
                for leading in (
                    [Comp.At(qq="10003")],
                    [
                        Comp.Reply(id="previous", sender_id="10001"),
                        Comp.Plain(" \t"),
                        Comp.At(qq="10003"),
                    ],
                ):
                    with self.subTest(body=body, prefix=prefix, leading=leading):
                        self.assertEqual(
                            await self.route_actions(
                                prefix + body,
                                method,
                                messages=[*leading, Comp.Plain(prefix + body)],
                                routed_text=body,
                            ),
                            [0, 0],
                        )

    async def test_empty_session_prefixes_keep_direct_wallpaper_addresses(self):
        for prefixes in ([], [""], ["", " "]):
            for body, method in self.ACTIONS:
                for label, event in direct_events(body):
                    with self.subTest(prefixes=prefixes, body=body, address=label):
                        self.assertEqual(
                            await self.route_actions(
                                body,
                                method,
                                group=label != "private",
                                messages=event.get_messages(),
                                prefixes=prefixes,
                                awake=False,
                            ),
                            [1, 1] if label == "private" else [1, 0],
                        )
                with self.subTest(prefixes=prefixes, body=body, address="bare_group"):
                    self.assertEqual(
                        await self.route_actions(body, method, prefixes=prefixes),
                        [0, 0],
                    )

    async def test_direct_wallpaper_routes_original_body_after_event_rewrite(self):
        for body, method in self.ACTIONS:
            for routed_text in (
                "",
                "rewritten unrelated text",
                "删图" if body != "删图" else "风景",
            ):
                for label, event in direct_events(body):
                    with self.subTest(
                        body=body, routed_text=routed_text, address=label
                    ):
                        self.assertEqual(
                            await self.route_actions(
                                body,
                                method,
                                group=label != "private",
                                messages=event.get_messages(),
                                routed_text=routed_text,
                                awake=False,
                            ),
                            [1, 1] if label == "private" else [1, 0],
                        )

    async def test_empty_or_unrelated_original_cannot_become_wallpaper_command(self):
        for body, method in self.ACTIONS:
            for original in ("", "unrelated original text"):
                for label, event in direct_events(original):
                    with self.subTest(body=body, original=original, address=label):
                        self.assertEqual(
                            await self.route_actions(
                                original,
                                method,
                                group=label != "private",
                                messages=event.get_messages(),
                                routed_text=body,
                            ),
                            [0, 0],
                        )


if __name__ == "__main__":
    unittest.main()
