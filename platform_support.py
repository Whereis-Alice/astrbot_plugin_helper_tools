"""Small platform checks shared by routing and QQ-specific services."""

from __future__ import annotations

from typing import Any

import astrbot.api.message_components as Comp

from .helper_utils import clean_text

QQ_OFFICIAL_PLATFORMS = frozenset(
    {"qq_official", "qq_official_webhook", "qqofficial", "qqofficial_webhook"}
)


def is_qq_official_platform(name: str) -> bool:
    return clean_text(name).casefold() in QQ_OFFICIAL_PLATFORMS


def is_qq_official_event(event: Any) -> bool:
    getter = getattr(event, "get_platform_name", None)
    return is_qq_official_platform(getter() if callable(getter) else "")


def is_bot_mention(event: Any, component: Any) -> bool:
    """Recognize the official adapter's synthetic At without trusting reply IDs."""
    if not isinstance(component, Comp.At):
        return False
    target = clean_text(component.qq)
    getter = getattr(event, "get_self_id", None)
    self_id = clean_text(getter() if callable(getter) else "")
    return bool(target) and (
        bool(self_id and target == self_id)
        or (target == "qq_official" and is_qq_official_event(event))
    )


def is_official_channel(event: Any) -> bool:
    if not is_qq_official_event(event):
        return False
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    guild_id = (
        raw.get("guild_id") if isinstance(raw, dict) else getattr(raw, "guild_id", None)
    )
    return bool(guild_id)
