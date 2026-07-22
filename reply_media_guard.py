from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, read_bool


BOT_REPLY_IMAGE_MARKER = "[图片来源说明：这是你先前发出的图，不是当前用户上传的图片。]"


@dataclass(frozen=True, slots=True)
class ReplyMediaGuardResult:
    marked_reply_count: int = 0
    marked_image_count: int = 0


class ReplyMediaGuard:
    """Label a quoted bot image without removing it from LLM input."""

    def __init__(self, config: Any) -> None:
        self.config = config

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "reply_media_guard", "enabled", True), True)

    def mark_bot_reply_images(self, event: Any) -> ReplyMediaGuardResult:
        if not self.enabled():
            return ReplyMediaGuardResult()

        bot_id = self._event_self_id(event)
        if not bot_id:
            return ReplyMediaGuardResult()

        marked_reply_count = 0
        marked_image_count = 0
        for component in self._event_messages(event):
            if not isinstance(component, Comp.Reply):
                continue
            if clean_text(getattr(component, "sender_id", "")) != bot_id:
                continue

            image_count = self._count_images(component)
            if not image_count:
                continue
            self._append_marker(component)
            marked_reply_count += 1
            marked_image_count += image_count

        return ReplyMediaGuardResult(marked_reply_count, marked_image_count)

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
    def _count_images(cls, component: Any) -> int:
        chain = getattr(component, "chain", None)
        if not isinstance(chain, list):
            return 0
        return cls._count_images_in_chain(chain)

    @classmethod
    def _count_images_in_chain(cls, chain: list[Any]) -> int:
        image_count = 0
        for component in chain:
            if isinstance(component, Comp.Image):
                image_count += 1
                continue
            image_count += cls._count_images_in_nested_component(component)
        return image_count

    @classmethod
    def _count_images_in_nested_component(cls, component: Any) -> int:
        if isinstance(component, Comp.Reply):
            return cls._count_images(component)

        content = getattr(component, "content", None)
        if isinstance(content, list):
            return cls._count_images_in_chain(content)

        nodes = getattr(component, "nodes", None)
        if not isinstance(nodes, list):
            return 0

        image_count = 0
        for node in nodes:
            node_content = getattr(node, "content", None)
            if isinstance(node_content, list):
                image_count += cls._count_images_in_chain(node_content)
        return image_count

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
