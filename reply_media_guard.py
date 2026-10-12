from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, read_bool, read_float, read_int
from .onebot_compat import get_msg as onebot_get_msg

BOT_REPLY_IMAGE_MARKER = (
    "[图片来源说明：这是你先前发出，或者自带插件自动发出的图，"
    "不是当前用户上传的图片。]"
)

_SELF_IMAGE_LEDGER_FILENAME = "reply_media_self_images.json"
_SELF_IMAGE_LEDGER_VERSION = 1


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

    def __init__(self, config: Any, data_dir: Any | None = None) -> None:
        self.config = config
        self._lookup_cache: dict[str, tuple[float, _QuotedMessageInfo]] = {}
        self._ledger_path: Path | None = (
            Path(data_dir) / _SELF_IMAGE_LEDGER_FILENAME if data_dir else None
        )
        # message_id -> (recorded_epoch, image_count) for images this bot sent.
        self._self_image_ledger: dict[str, tuple[float, int]] = {}
        self._load_self_image_ledger()

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

            message_id = clean_text(getattr(component, "id", ""))
            inline = _QuotedMessageInfo(
                sender_id=self._reply_sender_id(component),
                image_count=self._count_images(component),
            )
            quoted = inline
            # 本地台账优先：命中即判定为“我发出的图”，不受 LLOneBot 约 120 秒缓存
            # 限制，也无需再查 get_msg。台账在 QQ 上主要由下方“确认后写回”建立，
            # 在会回流自身消息的平台上也会由 remember_self_authored_images 建立。
            ledger_images = self._ledger_image_count(event, message_id)
            if ledger_images is not None:
                quoted = _QuotedMessageInfo(
                    sender_id=bot_id,
                    image_count=max(inline.image_count, ledger_images, 1),
                )
            # Some OneBot adapters cannot hydrate quoted messages emitted by other
            # plugins. Querying get_msg restores both the sender and image segments.
            elif (
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
            # 确认是 bot 自己的图后写回台账，使缓存过期后再次引用仍可命中。
            self._remember_quoted_bot_image(event, message_id, quoted.image_count)
            self._append_marker(component)
            marked_reply_count += 1
            marked_image_count += quoted.image_count

        return ReplyMediaGuardResult(marked_reply_count, marked_image_count)

    def remember_self_authored_images(self, event: Any) -> int:
        """在“会把 bot 自身消息作为事件回流”的适配器上，记录 bot 刚发出的含图消息。

        平台差异：aiocqhttp（QQ / LLOneBot / NapCat / Lagrange）只订阅 on_message
        group/private，不投递 bot 自身的 message_sent 事件，发送 API 的返回值
        （含 message_id）也被 AstrBot 丢弃，因此这条路径在 QQ 上通常不会触发——
        QQ 场景的台账主要由 mark_bot_reply_images 的“确认后写回”建立。要让首次引用
        旧图也稳定命中，请调大 LLOneBot 的 msgCacheExpire。本方法保留给确实会回流
        自身消息的平台。
        """

        if not self.enabled() or not self._self_ledger_enabled():
            return 0
        bot_id = self._event_self_id(event)
        if not bot_id or self._event_sender_id(event) != bot_id:
            return 0
        message_id = self._event_message_id(event)
        if not message_id:
            return 0
        image_count = self._count_images_in_chain(self._event_messages(event))
        if image_count <= 0:
            return 0
        self._ledger_put(self._cache_key(event, message_id), image_count)
        return image_count

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

    def _self_ledger_enabled(self) -> bool:
        return read_bool(
            cfg(self.config, "reply_media_guard", "self_ledger_enabled", True),
            True,
        )

    def _self_ledger_ttl_seconds(self) -> int:
        return read_int(
            cfg(
                self.config,
                "reply_media_guard",
                "self_ledger_ttl_seconds",
                259_200,
            ),
            259_200,
            minimum=60,
            maximum=2_592_000,
        )

    def _self_ledger_limit(self) -> int:
        return read_int(
            cfg(self.config, "reply_media_guard", "self_ledger_limit", 4096),
            4096,
            minimum=64,
            maximum=50_000,
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

        # 键名与 int/str 取值差异交给兼容层；LLOneBot 的消息缓存默认 120 秒，
        # 查不到旧消息是常态，这里静默降级，不向用户报错。
        timeout = float(self._lookup_timeout_seconds())
        try:
            response = await onebot_get_msg(
                bot,
                message_id,
                timeout=timeout,
                deadline=time.monotonic() + timeout,
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

    def _ledger_image_count(self, event: Any, message_id: str) -> int | None:
        """返回某条 bot 自发消息记录的图片数；无记录或已过期则返回 None。"""

        if not message_id or not self._self_ledger_enabled():
            return None
        key = self._cache_key(event, message_id)
        entry = self._self_image_ledger.get(key)
        if entry is None:
            return None
        recorded_at, image_count = entry
        if recorded_at + self._self_ledger_ttl_seconds() <= time.time():
            self._self_image_ledger.pop(key, None)
            return None
        return image_count

    def _remember_quoted_bot_image(
        self,
        event: Any,
        message_id: str,
        image_count: int,
    ) -> None:
        if not message_id or not self._self_ledger_enabled():
            return
        self._ledger_put(self._cache_key(event, message_id), image_count)

    def _ledger_put(self, key: str, image_count: int) -> None:
        if not key:
            return
        self._self_image_ledger[key] = (time.time(), max(int(image_count), 1))
        overflow = len(self._self_image_ledger) - self._self_ledger_limit()
        if overflow > 0:
            oldest = sorted(
                self._self_image_ledger.items(),
                key=lambda item: item[1][0],
            )
            for stale_key, _entry in oldest[:overflow]:
                self._self_image_ledger.pop(stale_key, None)
        self._save_self_image_ledger()

    def _load_self_image_ledger(self) -> None:
        path = self._ledger_path
        if path is None:
            return
        try:
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 台账损坏时静默重建，不影响主流程
            logger.debug(
                "[HelperTools/ReplyMedia] load self-image ledger failed: %s", exc
            )
            return
        if not isinstance(raw, dict):
            return
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return
        ttl = self._self_ledger_ttl_seconds()
        now = time.time()
        loaded: dict[str, tuple[float, int]] = {}
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            recorded_at = read_float(value.get("ts"), 0.0)
            image_count = read_int(value.get("images"), 0)
            if recorded_at <= 0.0 or image_count <= 0:
                continue
            if recorded_at + ttl <= now:
                continue
            loaded[key] = (recorded_at, image_count)
        self._self_image_ledger = loaded

    def _save_self_image_ledger(self) -> None:
        path = self._ledger_path
        if path is None:
            return
        ttl = self._self_ledger_ttl_seconds()
        now = time.time()
        entries = {
            key: {"ts": recorded_at, "images": image_count}
            for key, (recorded_at, image_count) in self._self_image_ledger.items()
            if recorded_at + ttl > now
        }
        payload = {"version": _SELF_IMAGE_LEDGER_VERSION, "entries": entries}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响内存台账与主流程
            logger.debug(
                "[HelperTools/ReplyMedia] save self-image ledger failed: %s", exc
            )

    @staticmethod
    def _event_self_id(event: Any) -> str:
        getter = getattr(event, "get_self_id", None)
        return ReplyMediaGuard._normalize_sender_id(
            getter() if callable(getter) else ""
        )

    @staticmethod
    def _event_sender_id(event: Any) -> str:
        getter = getattr(event, "get_sender_id", None)
        return ReplyMediaGuard._normalize_sender_id(
            getter() if callable(getter) else ""
        )

    @staticmethod
    def _event_message_id(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        candidates = (
            getattr(message_obj, "message_id", ""),
            getattr(message_obj, "id", ""),
            getattr(getattr(message_obj, "raw_message", None), "message_id", ""),
        )
        for candidate in candidates:
            message_id = clean_text(candidate)
            if message_id:
                return message_id
        return ""

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
