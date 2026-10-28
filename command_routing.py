"""Route manual commands using the current AstrBot wake prefixes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import filter

from .helper_utils import clean_text, read_list
from .poke_service import is_poke_synthetic_command


def _addressed_elsewhere(event: Any) -> bool:
    group_getter = getattr(event, "get_group_id", None)
    if callable(group_getter) and not group_getter():
        return False
    getter = getattr(event, "get_messages", None)
    messages = (
        getter()
        if callable(getter)
        else getattr(getattr(event, "message_obj", None), "message", [])
    )
    self_getter = getattr(event, "get_self_id", None)
    self_id = clean_text(self_getter()) if callable(self_getter) else ""
    for component in messages or []:
        if isinstance(component, Comp.Reply):
            continue
        if isinstance(component, Comp.Plain) and not clean_text(component.text):
            continue
        return isinstance(component, Comp.At) and clean_text(component.qq) not in {
            self_id,
            "all",
        }
    return False


def wake_command_text(event: Any, prefixes: Iterable[str]) -> str | None:
    """Return the original command body only after an explicit matching wake."""
    message_obj = getattr(event, "message_obj", None)
    original = getattr(message_obj, "message_str", None)
    text = clean_text(
        original if original is not None else getattr(event, "message_str", "")
    )
    # These events are generated locally by the poke action, never by a QQ
    # message. They keep their existing command dispatch and sender identity.
    if is_poke_synthetic_command(event):
        return text
    if _addressed_elsewhere(event):
        return None
    for value in prefixes:
        prefix = clean_text(value)
        if prefix and text.startswith(prefix):
            return text[len(prefix) :].lstrip()
    return None


class RequireWakePrefix(filter.CustomFilter):
    """Reject unaddressed commands before parameter and permission checks."""

    def filter(self, event: Any, cfg: Any) -> bool:
        prefixes = read_list(cfg.get("wake_prefix"), [])
        return wake_command_text(event, prefixes) is not None
