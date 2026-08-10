from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, read_bool, read_int
from .qq_features import call_onebot

BOT_REPLY_IMAGE_MARKER = (
    "[图片来源说明：这是你先前发出，或者自带插件自动发出的图，"
    "不是当前用户上传的图片。]"
)


@dataclass(frozen=True, slots=True)
class ReplyMediaGuardResult:
    marked_reply_count: int = 0
    marked_image_count: int = 0


@dataclass(frozen=True, slots=True)
class _QuotedMessageInfo:
    sender_id: str = ""
    image_count: int = 0


class ReplyMediaGuard:
    """Label quoted self-authored images without removing them from LLM input."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._lookup_cache: dict[str, tuple[float, _QuotedMessageInfo]] = {}

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "reply_media_guard", "enabled", True), True)

    async def mark_bot_reply_images(self, event: Any) -> ReplyMediaGuardResult:
        if not self.enabled():
            return ReplyMediaGuardResult()

        bot_id = self._event_self_id(event)
        if not bot_id:
            return ReplyMediaGuardResult()

        marked_reply_count = 0
        marked_image_count = 0
        remaining_lookups = self._max_onebot_lookups()
        for component in self._event_messages(event):
            if not isinstance(component, Comp.Reply):
                continue

            inline = _QuotedMessageInfo(
                sender_id=self._reply_sender_id(component),
                image_count=self._count_images(component),
            )
            quoted = inline
            # Some OneBot adapters cannot hydrate quoted messages emitted by other
            # plugins. Querying get_msg restores both the sender and image segments.
            if (
                self._onebot_lookup_enabled()
                and remaining_lookups > 0
                and self._should_lookup(component)
            ):
                remaining_lookups -= 1
                looked_up = await self._lookup_quoted_message(event, component)
                if looked_up is not None:
                    quoted = _QuotedMessageInfo(
                        sender_id=looked_up.sender_id or inline.sender_id,
                        image_count=max(inline.image_count, looked_up.image_count),
                    )

            if quoted.sender_id != bot_id or not quoted.image_count:
                continue
            self._append_marker(component)
            marked_reply_count += 1
            marked_image_count += quoted.image_count

        return ReplyMediaGuardResult(marked_reply_count, marked_image_count)

    def _onebot_lookup_enabled(self) -> bool:
        return read_bool(
            cfg(self.config, "reply_media_guard", "onebot_lookup_enabled", True),
            True,
        )

    def _max_onebot_lookups(self) -> int:
        return read_int(
            cfg(
                self.config,
                "reply_media_guard",
                "max_onebot_lookups_per_message",
                3,
            ),
            3,
            minimum=0,
            maximum=20,
        )

    def _lookup_timeout_seconds(self) -> int:
        return read_int(
            cfg(
                self.config,
                "reply_media_guard",
                "onebot_lookup_timeout_seconds",
                3,
            ),
            3,
            minimum=1,
            maximum=15,
        )

    def _cache_seconds(self) -> int:
        return read_int(
            cfg(self.config, "reply_media_guard", "lookup_cache_seconds", 3600),
            3600,
            minimum=0,
            maximum=86_400,
        )

    def _cache_limit(self) -> int:
        return read_int(
            cfg(self.config, "reply_media_guard", "lookup_cache_limit", 2048),
            2048,
            minimum=32,
            maximum=20_000,
        )

    @staticmethod
    def _should_lookup(reply: Any) -> bool:
        """Use the protocol message as the source of truth when an ID exists."""

        message_id = clean_text(getattr(reply, "id", ""))
        return bool(message_id)

    async def _lookup_quoted_message(
        self,
        event: Any,
        reply: Any,
    ) -> _QuotedMessageInfo | None:
        message_id = clean_text(getattr(reply, "id", ""))
        bot = getattr(event, "bot", None)
        if not message_id or bot is None:
            return None

        cache_key = self._cache_key(event, message_id)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            lookup_id: int | str = int(message_id) if message_id.isdigit() else message_id
            response = await asyncio.wait_for(
                call_onebot(bot, "get_msg", message_id=lookup_id),
                timeout=self._lookup_timeout_seconds(),
            )
        except Exception as exc:  # noqa: BLE001 - OneBot adapters expose implementation-specific errors
            logger.debug(
                "[HelperTools/ReplyMedia] get_msg failed for quoted message %s: %s",
                message_id,
                exc,
            )
            return None

        payload = self._unwrap_onebot_payload(response)
        if not isinstance(payload, dict):
            return None
        info = _QuotedMessageInfo(
            sender_id=self._payload_sender_id(payload),
            image_count=self._payload_image_count(payload),
        )
        self._cache_put(cache_key, info)
        return info

    @staticmethod
    def _unwrap_onebot_payload(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if any(
            key in value
            for key in ("message", "raw_message", "sender", "sender_id", "user_id")
        ):
            return value
        for key in ("data", "result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                return ReplyMediaGuard._unwrap_onebot_payload(nested)
        return value

    @staticmethod
    def _payload_sender_id(payload: dict[str, Any]) -> str:
        sender = payload.get("sender")
        if isinstance(sender, dict):
            for key in ("user_id", "userId", "id", "qq"):
                value = clean_text(sender.get(key))
                if value:
                    return ReplyMediaGuard._normalize_sender_id(value)
        for key in ("sender_id", "user_id", "userId", "qq"):
            value = clean_text(payload.get(key))
            if value:
                return ReplyMediaGuard._normalize_sender_id(value)
        return ""

    @classmethod
    def _payload_image_count(cls, payload: dict[str, Any]) -> int:
        message = payload.get("message")
        if isinstance(message, list):
            return cls._count_onebot_segments(message)
        raw_message = clean_text(payload.get("raw_message"))
        return raw_message.casefold().count("[cq:image")

    @classmethod
    def _count_onebot_segments(cls, value: Any) -> int:
        if isinstance(value, list):
            return sum(cls._count_onebot_segments(item) for item in value)
        if not isinstance(value, dict):
            return 0
        segment_type = clean_text(value.get("type")).casefold()
        image_count = 1 if segment_type in {"image", "flash"} else 0
        for key in ("message", "content", "messages"):
            nested = value.get(key)
            if isinstance(nested, list):
                image_count += cls._count_onebot_segments(nested)
        data = value.get("data")
        if isinstance(data, dict):
            for key in ("content", "message", "messages"):
                nested = data.get(key)
                if isinstance(nested, list):
                    image_count += cls._count_onebot_segments(nested)
        return image_count

    def _cache_key(self, event: Any, message_id: str) -> str:
        platform_getter = getattr(event, "get_platform_id", None)
        platform = clean_text(platform_getter() if callable(platform_getter) else "")
        if not platform:
            platform_getter = getattr(event, "get_platform_name", None)
            platform = clean_text(platform_getter() if callable(platform_getter) else "")
        return f"{platform}:{self._event_self_id(event)}:{message_id}"

    def _cache_get(self, key: str) -> _QuotedMessageInfo | None:
        entry = self._lookup_cache.get(key)
        if entry is None:
            return None
        expires_at, info = entry
        if expires_at <= time.monotonic():
            self._lookup_cache.pop(key, None)
            return None
        return info

    def _cache_put(self, key: str, info: _QuotedMessageInfo) -> None:
        ttl = self._cache_seconds()
        if ttl <= 0:
            return
        self._lookup_cache[key] = (time.monotonic() + ttl, info)
        overflow = len(self._lookup_cache) - self._cache_limit()
        if overflow <= 0:
            return
        oldest = sorted(self._lookup_cache.items(), key=lambda item: item[1][0])
        for stale_key, _entry in oldest[:overflow]:
            self._lookup_cache.pop(stale_key, None)

    @staticmethod
    def _event_self_id(event: Any) -> str:
        getter = getattr(event, "get_self_id", None)
        return ReplyMediaGuard._normalize_sender_id(
            getter() if callable(getter) else ""
        )

    @staticmethod
    def _reply_sender_id(reply: Any) -> str:
        sender_id = clean_text(getattr(reply, "sender_id", ""))
        if sender_id and sender_id != "0":
            return ReplyMediaGuard._normalize_sender_id(sender_id)
        legacy_sender_id = clean_text(getattr(reply, "qq", ""))
        return (
            ReplyMediaGuard._normalize_sender_id(legacy_sender_id)
            if legacy_sender_id != "0"
            else ""
        )

    @staticmethod
    def _normalize_sender_id(value: Any) -> str:
        """Normalize numeric QQ IDs and common adapter-scoped ID forms."""

        text = clean_text(value)
        if not text or text == "0":
            return ""
        if text.isdigit():
            return text
        numeric_tail = re.search(r"(?<!\d)(\d{1,20})$", text)
        return numeric_tail.group(1) if numeric_tail else text

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
