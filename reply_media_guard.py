from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, read_bool


BOT_REPLY_IMAGE_MARKER = "[机器人此前发送的图片已忽略，不是当前用户上传的图片]"


@dataclass(frozen=True, slots=True)
class ReplyMediaGuardResult:
    protected_reply_count: int = 0
    removed_image_count: int = 0


class ReplyMediaGuard:
    """Prevent a quoted bot image from being treated as a new user attachment."""

    def __init__(self, config: Any) -> None:
        self.config = config

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "reply_media_guard", "enabled", True), True)

    def protect_bot_reply_images(self, event: Any) -> ReplyMediaGuardResult:
        if not self.enabled():
            return ReplyMediaGuardResult()

        bot_id = self._event_self_id(event)
        if not bot_id:
            return ReplyMediaGuardResult()

        protected_reply_count = 0
        removed_image_count = 0
        for component in self._event_messages(event):
            if not isinstance(component, Comp.Reply):
                continue
            if clean_text(getattr(component, "sender_id", "")) != bot_id:
                continue

            protected_reply_count += 1
            removed_count = self._remove_images(component)
            removed_image_count += removed_count
            if removed_count:
                self._append_marker(component)

            # AstrBot can otherwise use the reply ID to fetch the original image
            # again during request construction. The quoted text stays in `chain`.
            component.id = ""

        return ReplyMediaGuardResult(protected_reply_count, removed_image_count)

    @staticmethod
    def _event_self_id(event: Any) -> str:
        getter = getattr(event, "get_self_id", None)
        return clean_text(getter()) if callable(getter) else ""

    @staticmethod
    def _event_messages(event: Any) -> list[Any]:
        getter = getattr(event, "get_messages", None)
        messages = getter() if callable(getter) else []
        return messages if isinstance(messages, list) else []

    @classmethod
    def _remove_images(cls, component: Any) -> int:
        chain = getattr(component, "chain", None)
        if not isinstance(chain, list):
            return 0
        return cls._remove_images_from_chain(chain)

    @classmethod
    def _remove_images_from_chain(cls, chain: list[Any]) -> int:
        removed_count = 0
        kept: list[Any] = []
        for component in chain:
            if isinstance(component, Comp.Image):
                removed_count += 1
                continue
            removed_count += cls._remove_images_from_nested_component(component)
            kept.append(component)
        chain[:] = kept
        return removed_count

    @classmethod
    def _remove_images_from_nested_component(cls, component: Any) -> int:
        if isinstance(component, Comp.Reply):
            return cls._remove_images(component)

        content = getattr(component, "content", None)
        if isinstance(content, list):
            return cls._remove_images_from_chain(content)

        nodes = getattr(component, "nodes", None)
        if not isinstance(nodes, list):
            return 0

        removed_count = 0
        for node in nodes:
            node_content = getattr(node, "content", None)
            if isinstance(node_content, list):
                removed_count += cls._remove_images_from_chain(node_content)
        return removed_count

    @staticmethod
    def _append_marker(reply: Any) -> None:
        chain = getattr(reply, "chain", None)
        if not isinstance(chain, list):
            reply.chain = [Comp.Plain(BOT_REPLY_IMAGE_MARKER)]
            return
        if any(
            isinstance(component, Comp.Plain)
            and clean_text(getattr(component, "text", "")) == BOT_REPLY_IMAGE_MARKER
            for component in chain
        ):
            return
        chain.append(Comp.Plain(BOT_REPLY_IMAGE_MARKER))
