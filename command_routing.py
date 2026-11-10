"""Route commands addressed by prefix, private chat, mention or reply."""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import filter
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry

from .helper_utils import clean_text, read_list
from .platform_support import is_bot_mention
from .poke_service import is_poke_synthetic_command


def _is_private_chat(event: Any) -> bool:
    getter = getattr(event, "is_private_chat", None)
    if callable(getter):
        return bool(getter())
    group_getter = getattr(event, "get_group_id", None)
    return callable(group_getter) and not group_getter()


def _message_components(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    return (
        getter()
        if callable(getter)
        else getattr(getattr(event, "message_obj", None), "message", [])
    ) or []


def _bot_id(event: Any) -> str:
    self_getter = getattr(event, "get_self_id", None)
    return clean_text(self_getter()) if callable(self_getter) else ""


def _addressed_elsewhere(event: Any) -> bool:
    if _is_private_chat(event):
        return False
    for component in _message_components(event):
        if isinstance(component, Comp.Reply):
            continue
        if isinstance(component, Comp.Plain) and not clean_text(component.text):
            continue
        return (
            isinstance(component, Comp.At)
            and clean_text(component.qq) != "all"
            and not is_bot_mention(event, component)
        )
    return False


def _directly_addressed(event: Any) -> bool:
    if _is_private_chat(event):
        return True
    self_id = _bot_id(event)
    return any(
        is_bot_mention(event, component)
        or (
            bool(self_id)
            and isinstance(component, Comp.Reply)
            and clean_text(component.sender_id) == self_id
        )
        for component in _message_components(event)
    )


def original_message_text(event: Any) -> str:
    """Read adapter text without trusting a rewritten event body."""
    message_obj = getattr(event, "message_obj", None)
    original = getattr(message_obj, "message_str", None)
    return clean_text(
        original if original is not None else getattr(event, "message_str", "")
    )


def prefixed_command_text(event: Any, prefixes: Iterable[str]) -> str | None:
    """Return a command body only when its original text has a real prefix."""
    text = original_message_text(event)
    if not text or _addressed_elsewhere(event):
        return None
    for value in prefixes:
        prefix = clean_text(value)
        if prefix and text.startswith(prefix):
            return text[len(prefix) :].lstrip()
    return None


def addressed_command_text(event: Any, prefixes: Iterable[str]) -> str | None:
    """Return original command text when it is addressed to this bot."""
    text = original_message_text(event)
    # These events are generated locally by the poke action, never by a QQ
    # message. They keep their existing command dispatch and sender identity.
    if is_poke_synthetic_command(event):
        return text
    if not text or _addressed_elsewhere(event):
        return None
    prefixed = prefixed_command_text(event, prefixes)
    if prefixed is not None:
        return prefixed
    return text if _directly_addressed(event) else None


class RequireCommandAddress(filter.CustomFilter):
    """Check command addressing before parameter and permission checks."""

    def __init__(
        self, raise_error: bool = True, *, command_names: Iterable[str] = ()
    ) -> None:
        super().__init__(raise_error=raise_error)
        self.command_names = tuple(command_names)
        self.command_filter: CommandFilter | None = None

    def filter(self, event: Any, cfg: Any) -> bool:
        prefixes = read_list(cfg.get("wake_prefix"), [])
        body = addressed_command_text(event, prefixes)
        if body is None:
            return False
        names = (
            self.command_filter.get_complete_command_names()
            if self.command_filter is not None
            else self.command_names
        )
        if names:
            normalized = " ".join(body.split())
            if not any(
                normalized == name or normalized.startswith(name + " ")
                for name in names
            ):
                return False
            # Wake only this registered command, preserving core wake settings
            # for ordinary text that happens to be in a private chat.
            event.is_at_or_wake_command = True
            event.message_str = body
        return True


def addressed_command(
    command_name: str, *, alias: set[str] | None = None, **kwargs: Any
):
    """Register an AstrBot command with its address check before its parser."""
    address_filter = partial(
        RequireCommandAddress, command_names=(command_name, *(alias or ()))
    )

    def decorator(handler: Any):
        handler = filter.custom_filter(address_filter)(handler)
        handler = filter.command(command_name, alias=alias, **kwargs)(handler)
        metadata = star_handlers_registry.get_handler_by_full_name(
            f"{handler.__module__}_{handler.__name__}"
        )
        if metadata is not None:
            guard, parser = metadata.event_filters[-2:]
            if isinstance(guard, RequireCommandAddress) and isinstance(
                parser, CommandFilter
            ):
                guard.command_filter = parser
        return handler

    return decorator
