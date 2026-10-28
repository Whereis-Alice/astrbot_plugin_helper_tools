from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot_plugin_helper_tools.steam_service import SteamMessageMatch, SteamService
from astrbot_plugin_helper_tools.voice_service import VoiceService

STEAM_URL = "https://store.steampowered.com/app/730/CounterStrike/"


def _context() -> SimpleNamespace:
    return SimpleNamespace(get_config=lambda: {"wake_prefix": ["!", "小助手"]})


@pytest.fixture
def steam() -> SteamService:
    return SteamService(
        {"steam": {"auto_parse_links": False, "appid_auto_parse_mode": "off"}},
        context=_context(),
    )


@pytest.fixture
def voice(tmp_path: Path) -> VoiceService:
    return VoiceService({}, tmp_path, context=_context())


@pytest.mark.parametrize("allow_commands", [True, False])
@pytest.mark.parametrize(
    ("text", "wake_triggered", "query"),
    [
        ("!steam Portal 2", False, "Portal 2"),
        ("小助手查找 Portal 2", False, "Portal 2"),
        ("steam Portal 2", True, "Portal 2"),
        ("查找 730", True, "730"),
        ("steam", True, ""),
    ],
)
def test_steam_manual_commands_follow_caller_policy(
    steam: SteamService,
    text: str,
    wake_triggered: bool,
    query: str,
    allow_commands: bool,
) -> None:
    expected = (
        SteamMessageMatch(True, query, True) if allow_commands else SteamMessageMatch()
    )

    assert (
        steam.match_message(
            text, wake_triggered=wake_triggered, allow_commands=allow_commands
        )
        == expected
    )
    assert steam.should_handle_message(
        text, wake_triggered=wake_triggered, allow_commands=allow_commands
    ) == (expected.handled, expected.query)


def test_steam_default_calls_keep_prefixed_and_stripped_commands(
    steam: SteamService,
) -> None:
    assert steam.match_message("!steam Portal 2") == SteamMessageMatch(
        True, "Portal 2", True
    )
    assert steam.should_handle_message("steam Portal 2", wake_triggered=True) == (
        True,
        "Portal 2",
    )
    assert SteamService({}).should_handle_message("/steam Portal 2") == (
        True,
        "Portal 2",
    )


def test_steam_plain_command_still_needs_wake_triggered(steam: SteamService) -> None:
    assert (
        steam.match_message("steam Portal 2", allow_commands=True)
        == SteamMessageMatch()
    )


@pytest.mark.parametrize("setting", ["enabled", "commands_enabled"])
def test_steam_command_policy_respects_feature_switches(
    steam: SteamService, setting: str
) -> None:
    steam.config["steam"][setting] = False

    assert (
        steam.match_message("steam Portal 2", wake_triggered=True, allow_commands=True)
        == SteamMessageMatch()
    )


@pytest.mark.parametrize("stop_event", [True, False])
@pytest.mark.parametrize("text", [STEAM_URL, f"!steam {STEAM_URL}"])
def test_steam_links_remain_automatic_when_commands_denied(
    text: str, stop_event: bool
) -> None:
    service = SteamService(
        {"steam": {"auto_parse_links": True, "stop_event_after_response": stop_event}},
        context=_context(),
    )

    assert service.match_message(text, allow_commands=False) == SteamMessageMatch(
        True, "730", stop_event
    )
    assert service.should_handle_message(text, allow_commands=False) == (True, "730")


@pytest.mark.parametrize("auto_parse_links", [True, False])
def test_steam_link_switch_is_independent_of_command_switch(
    auto_parse_links: bool,
) -> None:
    service = SteamService(
        {"steam": {"commands_enabled": False, "auto_parse_links": auto_parse_links}}
    )

    assert service.should_handle_message(STEAM_URL, allow_commands=False) == (
        auto_parse_links,
        "730" if auto_parse_links else "",
    )


@pytest.mark.parametrize("allow_commands", [True, False])
@pytest.mark.parametrize(
    ("mode", "text", "wake_triggered", "handled"),
    [
        ("off", "730", False, False),
        ("off", "730", True, False),
        ("off", "!730", False, False),
        ("any", "730", False, True),
        ("any", "730", True, True),
        ("any", "!730", False, False),
        ("wake_prefix", "730", False, False),
        ("wake_prefix", "730", True, True),
        ("wake_prefix", "!730", False, True),
        ("wake_prefix", "小助手730", False, True),
    ],
)
def test_steam_appid_modes_follow_caller_policy(
    mode: str, text: str, wake_triggered: bool, handled: bool, allow_commands: bool
) -> None:
    service = SteamService(
        {"steam": {"commands_enabled": False, "appid_auto_parse_mode": mode}},
        context=_context(),
    )
    handled = handled and (allow_commands or mode == "any")
    expected = SteamMessageMatch(True, "730", True) if handled else SteamMessageMatch()

    assert (
        service.match_message(
            text, wake_triggered=wake_triggered, allow_commands=allow_commands
        )
        == expected
    )
    assert service.should_handle_message(
        text, wake_triggered=wake_triggered, allow_commands=allow_commands
    ) == (expected.handled, expected.query)


def test_steam_rejected_session_prefix_cannot_fall_back_to_default_profile() -> None:
    service = SteamService(
        {"steam": {"appid_auto_parse_mode": "wake_prefix"}},
        context=SimpleNamespace(get_config=lambda: {"wake_prefix": ["/"]}),
    )

    # The caller uses the current session's "!" prefix, while this context
    # exposes the default profile's "/" prefix. A rejected command must stay rejected.
    assert service.match_message("/778666", allow_commands=False) == SteamMessageMatch()
    assert service.should_handle_message("/778666", allow_commands=False) == (False, "")
    assert service.match_message(
        "778666", wake_triggered=True, allow_commands=True
    ) == SteamMessageMatch(True, "778666", True)
    assert service.match_message("/778666") == SteamMessageMatch(True, "778666", True)


def test_disabled_steam_does_not_parse_automatic_messages() -> None:
    service = SteamService(
        {
            "steam": {
                "enabled": False,
                "auto_parse_links": True,
                "appid_auto_parse_mode": "any",
            }
        }
    )

    for text in (STEAM_URL, "730"):
        assert service.match_message(text, allow_commands=False) == SteamMessageMatch()


@pytest.mark.parametrize("allow_commands", [True, False])
@pytest.mark.parametrize(
    ("text", "wake_triggered"),
    [
        ("!voice_meme", False),
        ("小助手随机语音", False),
        ("voice_meme", True),
        ("随机语音", True),
    ],
)
def test_voice_manual_commands_follow_caller_policy(
    voice: VoiceService, text: str, wake_triggered: bool, allow_commands: bool
) -> None:
    assert (
        voice.should_handle_message(
            text, wake_triggered=wake_triggered, allow_commands=allow_commands
        )
        is allow_commands
    )


def test_voice_default_calls_keep_prefixed_and_stripped_commands(
    voice: VoiceService, tmp_path: Path
) -> None:
    assert voice.should_handle_message("!voice_meme")
    assert voice.should_handle_message("随机语音", wake_triggered=True)
    assert VoiceService({}, tmp_path).should_handle_message("/voice_meme")


def test_voice_plain_command_still_needs_wake_triggered(voice: VoiceService) -> None:
    assert not voice.should_handle_message("voice_meme", allow_commands=True)


@pytest.mark.parametrize("setting", ["enabled", "commands_enabled"])
def test_voice_command_policy_respects_feature_switches(
    voice: VoiceService, setting: str
) -> None:
    voice.config["voice"] = {setting: False}

    assert not voice.should_handle_message(
        "随机语音", wake_triggered=True, allow_commands=True
    )


@pytest.mark.parametrize("commands_enabled", [True, False])
@pytest.mark.parametrize("auto_trigger_enabled", [True, False])
@pytest.mark.parametrize("text", ["来点哈基米", "!随机语音"])
def test_voice_configured_keywords_remain_automatic_when_commands_denied(
    tmp_path: Path, commands_enabled: bool, auto_trigger_enabled: bool, text: str
) -> None:
    service = VoiceService(
        {
            "voice": {
                "commands_enabled": commands_enabled,
                "auto_trigger_enabled": auto_trigger_enabled,
                "trigger_keywords": ["哈基米", "随机语音"],
            }
        },
        tmp_path,
        context=_context(),
    )

    assert (
        service.should_handle_message(text, allow_commands=False)
        is auto_trigger_enabled
    )


def test_voice_empty_keywords_do_not_restore_automatic_defaults(
    voice: VoiceService,
) -> None:
    voice.config["voice"] = {"trigger_keywords": []}

    for text in ("来点哈基米", "!voice_meme", "!随机语音"):
        assert not voice.should_handle_message(text, allow_commands=False)


def test_disabled_voice_does_not_match_configured_keywords(voice: VoiceService) -> None:
    voice.config["voice"] = {"enabled": False, "trigger_keywords": ["哈基米"]}

    assert not voice.should_handle_message("来点哈基米", allow_commands=False)


def test_steam_llm_tool_still_works_when_manual_commands_denied(
    steam: SteamService,
) -> None:
    steam.config["steam"]["commands_enabled"] = False
    assert not steam.match_message(
        "steam 730", wake_triggered=True, allow_commands=False
    ).handled
    details = AsyncMock(return_value={"name": "Counter-Strike", "is_free": True})

    with patch.object(steam, "fetch_app_details", details):
        result = asyncio.run(steam.query_game(query="730"))

    details.assert_awaited_once_with(730)
    assert isinstance(result, str)
    assert "Counter-Strike" in result
    assert "AppID: 730" in result


def test_voice_llm_tool_still_works_when_manual_commands_denied(
    voice: VoiceService,
) -> None:
    voice.config["voice"] = {"commands_enabled": False}
    assert not voice.should_handle_message(
        "voice_meme", wake_triggered=True, allow_commands=False
    )
    chain = [object()]
    event = SimpleNamespace(chain_result=lambda value: value, send=AsyncMock())

    with patch.object(
        voice, "build_chain", AsyncMock(return_value=chain)
    ) as build_chain:
        result = asyncio.run(voice.send_to_event(event))

    build_chain.assert_awaited_once_with()
    event.send.assert_awaited_once_with(chain)
    assert result == "随机语音已发送。"
