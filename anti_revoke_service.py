from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from astrbot.api import logger
from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from .helper_utils import (
    cfg,
    clean_text,
    fetch_bytes,
    json_dumps,
    read_bool,
    read_int,
    read_list,
)
from .qq_features import call_onebot

_IMAGE_SEGMENT_TYPES = {"image", "flash", "mface"}
_IMAGE_CACHE_MAX_BYTES = 12 * 1024 * 1024
_IMAGE_CACHE_MAX_COUNT = 20
_IMAGE_FETCH_TIMEOUT_SECONDS = 8
_IMAGE_FORMAT_EXTENSIONS = {
    "BMP": ".bmp",
    "GIF": ".gif",
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


@dataclass(slots=True)
class RevokeRecord:
    group_id: str
    message_id: str
    sender_id: str
    sender_name: str
    timestamp: int
    message: Any
    cached_at: float
    image_cache_files: dict[str, str] = field(default_factory=dict)


def unwrap_onebot_payload(value: Any) -> dict[str, Any]:
    """Read direct OneBot payloads and common API response wrappers."""

    if isinstance(value, Mapping):
        payload = dict(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return unwrap_onebot_payload(parsed)
    else:
        return {}

    for _ in range(3):
        nested = payload.get("data")
        if not isinstance(nested, Mapping):
            nested = payload.get("result")
        if not isinstance(nested, Mapping):
            break
        if any(
            key in payload
            for key in (
                "post_type",
                "message_type",
                "notice_type",
                "message_id",
                "group_id",
            )
        ):
            break
        payload = dict(nested)
    return payload


def extract_event_payload(event: Any) -> dict[str, Any]:
    """Extract a raw event from the variants used by AstrBot/OneBot adapters."""

    message_obj = getattr(event, "message_obj", None)
    candidates = (
        getattr(message_obj, "raw_message", None),
        getattr(event, "raw_message", None),
        getattr(event, "raw_event", None),
    )
    for candidate in candidates:
        payload = unwrap_onebot_payload(candidate)
        if payload:
            return payload
    return {}


def is_group_recall(payload: Mapping[str, Any]) -> bool:
    return (
        clean_text(payload.get("post_type")).casefold() == "notice"
        and clean_text(payload.get("notice_type")).casefold() == "group_recall"
    )


def is_group_message(payload: Mapping[str, Any], event: Any) -> bool:
    if clean_text(payload.get("post_type")).casefold() in {"notice", "request", "meta_event"}:
        return False
    message_type = clean_text(payload.get("message_type")).casefold()
    if message_type in {"group", "group_message"}:
        return True
    if payload.get("group_id") is None:
        return False
    getter = getattr(event, "get_message_type", None)
    event_type = clean_text(getter() if callable(getter) else "").casefold()
    return not event_type or "group" in event_type


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return clean_text(value)


def _event_id(payload: Mapping[str, Any], event: Any, key: str) -> str:
    value = clean_text(payload.get(key))
    if value:
        return value
    message_obj = getattr(event, "message_obj", None)
    return clean_text(getattr(message_obj, key, ""))


def _component_to_segment(component: Any) -> dict[str, Any] | None:
    type_name = clean_text(getattr(getattr(component, "type", None), "name", "")).casefold()
    if type_name in {"plain", "text"}:
        return {"type": "text", "data": {"text": clean_text(getattr(component, "text", ""))}}
    if type_name == "at":
        return {"type": "at", "data": {"qq": clean_text(getattr(component, "qq", ""))}}
    if type_name == "face":
        return {"type": "face", "data": {"id": clean_text(getattr(component, "id", ""))}}
    if type_name in {"image", "flash"}:
        data: dict[str, Any] = {}
        for key in ("file", "url", "path"):
            value = clean_text(getattr(component, key, ""))
            if value:
                data[key if key != "path" else "file"] = value
        return {"type": type_name, "data": data}
    if type_name in {"record", "video", "file", "json", "xml", "forward"}:
        data: dict[str, Any] = {}
        for key in ("file", "url", "name", "content"):
            value = getattr(component, key, None)
            if value not in (None, ""):
                data[key] = _json_safe(value)
        return {"type": type_name, "data": data}
    return None


def extract_message_payload(payload: Mapping[str, Any], event: Any) -> Any:
    message = payload.get("message")
    if isinstance(message, (str, list, dict)):
        return _json_safe(message)
    getter = getattr(event, "get_messages", None)
    components = getter() if callable(getter) else []
    if not isinstance(components, list):
        return ""
    segments = [
        segment
        for component in components
        if (segment := _component_to_segment(component)) is not None
    ]
    return segments


def _payload_value(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = clean_text(payload.get(key))
        if value:
            return value
    return ""


def _cache_filename(group_id: str, message_id: str) -> str:
    safe_group = re.sub(r"[^0-9A-Za-z_.-]", "_", group_id) or "unknown"
    safe_message = re.sub(r"[^0-9A-Za-z_.-]", "_", message_id) or "unknown"
    return f"cache_{safe_group}_{safe_message}.json"


def _media_file_prefix(group_id: str, message_id: str) -> str:
    cache_name = Path(_cache_filename(group_id, message_id)).stem
    return f"media_{cache_name.removeprefix('cache_')}_"


def _media_filename(group_id: str, message_id: str, key: str, extension: str) -> str:
    safe_key = re.sub(r"[^0-9A-Za-z_.-]", "_", key) or "image"
    return f"{_media_file_prefix(group_id, message_id)}{safe_key}{extension}"


def _iter_image_segments(message: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(message, list):
        return [
            (str(index), segment)
            for index, segment in enumerate(message)
            if isinstance(segment, Mapping)
            and clean_text(segment.get("type")).casefold() in _IMAGE_SEGMENT_TYPES
        ]
    if (
        isinstance(message, Mapping)
        and clean_text(message.get("type")).casefold() in _IMAGE_SEGMENT_TYPES
    ):
        return [("root", message)]
    return []


def _segment_for_key(message: Any, key: str) -> dict[str, Any] | None:
    if key == "root":
        return message if isinstance(message, dict) else None
    if not isinstance(message, list) or not key.isdigit():
        return None
    index = int(key)
    if index < 0 or index >= len(message):
        return None
    segment = message[index]
    return segment if isinstance(segment, dict) else None


def _decode_image_data_ref(value: str, max_bytes: int) -> bytes | None:
    text = clean_text(value)
    if text.startswith("base64://"):
        payload = text[len("base64://") :]
    elif text.startswith("data:image/") and ";base64," in text:
        payload = text.split(";base64,", 1)[1]
    else:
        return None
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > max_bytes:
        return None
    return data


def _path_from_media_ref(value: str) -> Path | None:
    text = clean_text(value)
    if not text:
        return None
    if text.casefold().startswith("file://"):
        parsed = urlparse(text)
        raw_path = url2pathname(unquote(parsed.path))
        if parsed.netloc and not raw_path.startswith("/"):
            raw_path = f"//{parsed.netloc}/{raw_path}"
        path = Path(raw_path)
    elif "://" not in text:
        path = Path(text).expanduser()
    else:
        return None
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    return resolved if resolved.exists() and resolved.is_file() else None


def _read_limited_file(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"image is larger than {max_bytes} bytes")
    return data


def _validated_image_extension(data: bytes) -> str:
    if not data or len(data) > _IMAGE_CACHE_MAX_BYTES:
        raise ValueError("invalid anti-revoke image size")
    try:
        with PILImage.open(BytesIO(data)) as image:
            format_name = clean_text(image.format).upper()
            image.verify()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError("anti-revoke media is not a supported image") from exc
    extension = _IMAGE_FORMAT_EXTENSIONS.get(format_name)
    if not extension:
        raise ValueError(f"unsupported anti-revoke image format: {format_name or 'unknown'}")
    return extension


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _materialize_cached_images(
    message: Any,
    image_cache_files: Mapping[str, str],
    cache_dir: Path,
) -> tuple[Any, int]:
    prepared = _json_safe(message)
    restored = 0
    resolved_cache_dir = cache_dir.resolve(strict=False)
    for key, filename_value in image_cache_files.items():
        filename = clean_text(filename_value)
        if not filename or Path(filename).name != filename:
            continue
        path = (cache_dir / filename).resolve(strict=False)
        if path.parent != resolved_cache_dir or not path.is_file():
            continue
        try:
            data = _read_limited_file(path, _IMAGE_CACHE_MAX_BYTES)
            _validated_image_extension(data)
        except (OSError, ValueError):
            continue
        segment = _segment_for_key(prepared, clean_text(key))
        if segment is None:
            continue
        segment_type = clean_text(segment.get("type")).casefold()
        if segment_type not in _IMAGE_SEGMENT_TYPES:
            continue
        current_data = segment.get("data")
        image_data = dict(current_data) if isinstance(current_data, Mapping) else {}
        image_data["file"] = f"base64://{base64.b64encode(data).decode('ascii')}"
        image_data.pop("url", None)
        image_data.pop("path", None)
        segment["type"] = "image"
        segment["data"] = image_data
        restored += 1
    return prepared, restored


def _cached_image_segments(message: Any) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for _key, segment in _iter_image_segments(message):
        data = segment.get("data")
        data_map = data if isinstance(data, Mapping) else {}
        if clean_text(data_map.get("file")).startswith("base64://"):
            segments.append(_json_safe(segment))
    return segments


def _message_size(message: Any) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _message_to_text(message: Any) -> str:
    if isinstance(message, str):
        return re.sub(r"\[CQ:([a-z_]+)[^\]]*\]", lambda match: f"[{match.group(1)}]", message).strip()
    if isinstance(message, list):
        parts = [_message_to_text(item) for item in message]
        return "".join(part for part in parts if part)
    if not isinstance(message, Mapping):
        return clean_text(message)
    segment_type = clean_text(message.get("type")).casefold()
    data = message.get("data")
    if not isinstance(data, Mapping):
        data = message
    if segment_type in {"text", "plain"}:
        return clean_text(data.get("text"))
    labels = {
        "image": "[图片]",
        "flash": "[闪照]",
        "mface": "[表情图片]",
        "record": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "json": "[小程序]",
        "xml": "[卡片]",
        "forward": "[合并转发]",
        "at": "[@用户]",
        "face": "[表情]",
    }
    return labels.get(segment_type, f"[{segment_type or '未知消息'}]")


def _text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


_STATIC_REPLY_LABEL = "\u3010\u5f15\u7528\u6d88\u606f\u3011"


def _static_reply_text(segment: Mapping[str, Any]) -> str:
    data = segment.get("data")
    data_map = data if isinstance(data, Mapping) else {}
    preview = clean_text(data_map.get("text"))
    return f"{_STATIC_REPLY_LABEL} {preview}" if preview else _STATIC_REPLY_LABEL


def sanitize_recall_message(message: Any) -> Any:
    """Replace active reply segments so a recalled message cannot point to an expired ID."""

    if isinstance(message, str):
        return re.sub(r"\[CQ:reply(?:,[^\]]*)?\]", _STATIC_REPLY_LABEL, message)
    if isinstance(message, list):
        sanitized: list[Any] = []
        for segment in message:
            if isinstance(segment, Mapping) and clean_text(segment.get("type")).casefold() == "reply":
                sanitized.append(_text_segment(_static_reply_text(segment)))
            else:
                sanitized.append(segment)
        return sanitized
    if isinstance(message, Mapping) and clean_text(message.get("type")).casefold() == "reply":
        return _text_segment(_static_reply_text(message))
    return message


def _message_with_header(header: str, message: Any) -> Any:
    """Prepend recall metadata while keeping the result as one QQ message."""

    prefix = f"{header}\n\n"
    if isinstance(message, str):
        return f"{prefix}{message}" if message else header
    if isinstance(message, list):
        return [_text_segment(prefix), *message] if message else header
    if isinstance(message, Mapping):
        return [_text_segment(prefix), dict(message)]
    text = clean_text(message)
    return f"{prefix}{text}" if text else header


class AntiRevokeService:
    """Cache and restore QQ group messages without depending on old event fields."""

    def __init__(self, config: Any, data_dir: str | Path) -> None:
        self.config = config
        self.cache_dir = Path(data_dir) / "anti_revoke" / "cache"
        self.forward_targets_path = Path(data_dir) / "anti_revoke" / "forward_targets.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._records: OrderedDict[str, RevokeRecord] = OrderedDict()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._forward_targets = self._load_forward_targets()

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "anti_revoke", "enabled", False), False)

    def commands_enabled(self) -> bool:
        return read_bool(
            cfg(self.config, "anti_revoke", "commands_enabled", True),
            True,
        )

    def monitor_groups(self) -> list[str]:
        return read_list(cfg(self.config, "anti_revoke", "monitor_groups", []))

    def target_receivers(self) -> list[str]:
        return read_list(cfg(self.config, "anti_revoke", "target_receivers", []))

    def target_groups(self) -> list[str]:
        return read_list(cfg(self.config, "anti_revoke", "target_groups", []))

    def ignore_senders(self) -> set[str]:
        return set(read_list(cfg(self.config, "anti_revoke", "ignore_senders", [])))

    def ignore_operators(self) -> set[str]:
        return set(read_list(cfg(self.config, "anti_revoke", "ignore_operators", [])))

    def cache_expiration_seconds(self) -> int:
        return read_int(
            cfg(self.config, "anti_revoke", "cache_expiration_seconds", 300),
            300,
            minimum=30,
            maximum=86_400,
        )

    def max_cached_messages(self) -> int:
        return read_int(
            cfg(self.config, "anti_revoke", "max_cached_messages", 2000),
            2000,
            minimum=100,
            maximum=20_000,
        )

    def max_message_bytes(self) -> int:
        return read_int(
            cfg(self.config, "anti_revoke", "max_message_bytes", 2 * 1024 * 1024),
            2 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=16 * 1024 * 1024,
        )

    def send_original_message(self) -> bool:
        return read_bool(
            cfg(self.config, "anti_revoke", "send_original_message", True),
            True,
        )

    def include_recall_header(self) -> bool:
        return read_bool(
            cfg(self.config, "anti_revoke", "include_recall_header", True),
            True,
        )

    async def _read_image_reference(
        self,
        value: Any,
        *,
        allow_local: bool = False,
    ) -> tuple[bytes, str]:
        reference = clean_text(value)
        if not reference:
            raise ValueError("empty image reference")
        embedded = _decode_image_data_ref(reference, _IMAGE_CACHE_MAX_BYTES)
        if embedded is not None:
            return embedded, _validated_image_extension(embedded)
        local_path = _path_from_media_ref(reference) if allow_local else None
        if local_path is not None:
            data = await asyncio.to_thread(
                _read_limited_file,
                local_path,
                _IMAGE_CACHE_MAX_BYTES,
            )
            return data, _validated_image_extension(data)
        if not reference.casefold().startswith(("http://", "https://")):
            raise ValueError("unresolved image reference")
        data, _content_type = await fetch_bytes(
            reference,
            timeout_seconds=_IMAGE_FETCH_TIMEOUT_SECONDS,
            max_bytes=_IMAGE_CACHE_MAX_BYTES,
        )
        return data, _validated_image_extension(data)

    async def _read_image_segment(
        self,
        event: Any,
        segment: Mapping[str, Any],
    ) -> tuple[bytes, str]:
        raw_data = segment.get("data")
        data = raw_data if isinstance(raw_data, Mapping) else {}
        references = [
            value
            for key in ("path", "file", "url")
            if (value := clean_text(data.get(key)))
        ]
        seen: set[str] = set()
        for reference in references:
            if reference in seen:
                continue
            seen.add(reference)
            try:
                return await self._read_image_reference(reference)
            except (OSError, ValueError):
                continue

        bot = getattr(event, "bot", None)
        file_ref = clean_text(data.get("file"))
        if bot is None or not file_ref:
            raise ValueError("image has no downloadable reference")
        response = await asyncio.wait_for(
            call_onebot(bot, "get_image", file=file_ref),
            timeout=_IMAGE_FETCH_TIMEOUT_SECONDS,
        )
        resolved = unwrap_onebot_payload(response)
        resolved_refs = [
            value
            for key in ("base64", "path", "file", "url")
            if (value := clean_text(resolved.get(key))) and value != file_ref
        ]
        for reference in resolved_refs:
            if reference in seen:
                continue
            seen.add(reference)
            if not reference.startswith(("base64://", "data:image/")) and clean_text(
                resolved.get("base64")
            ) == reference:
                reference = f"base64://{reference}"
            try:
                return await self._read_image_reference(reference, allow_local=True)
            except (OSError, ValueError):
                continue
        raise ValueError("OneBot could not resolve the image")

    def _cached_image_path(self, filename_value: Any) -> Path | None:
        filename = clean_text(filename_value)
        if not filename or Path(filename).name != filename:
            return None
        path = (self.cache_dir / filename).resolve(strict=False)
        if path.parent != self.cache_dir.resolve(strict=False):
            return None
        return path

    async def _cache_record_images(self, event: Any, record: RevokeRecord) -> bool:
        segments = _iter_image_segments(record.message)[:_IMAGE_CACHE_MAX_COUNT]
        pending: list[tuple[str, Mapping[str, Any]]] = []
        for key, segment in segments:
            path = self._cached_image_path(record.image_cache_files.get(key))
            if path is not None and path.is_file():
                continue
            pending.append((key, segment))
        if not pending:
            return False

        async def cache_one(
            key: str,
            segment: Mapping[str, Any],
        ) -> tuple[str, str]:
            data, extension = await self._read_image_segment(event, segment)
            filename = _media_filename(record.group_id, record.message_id, key, extension)
            path = self.cache_dir / filename
            await asyncio.to_thread(_write_bytes_atomic, path, data)
            return key, filename

        results = await asyncio.gather(
            *(cache_one(key, segment) for key, segment in pending),
            return_exceptions=True,
        )
        changed = False
        for (key, _segment), result in zip(pending, results, strict=True):
            if isinstance(result, BaseException):
                logger.debug(
                    "[HelperTools/AntiRevoke] image snapshot failed group=%s message=%s "
                    "segment=%s error=%r",
                    record.group_id,
                    record.message_id,
                    key,
                    result,
                )
                continue
            cached_key, filename = result
            old_path = self._cached_image_path(record.image_cache_files.get(cached_key))
            if old_path is not None and old_path.name != filename:
                old_path.unlink(missing_ok=True)
            record.image_cache_files[cached_key] = filename
            changed = True
        if changed:
            logger.debug(
                "[HelperTools/AntiRevoke] snapshotted %d image(s) group=%s message=%s",
                len(record.image_cache_files),
                record.group_id,
                record.message_id,
            )
        return changed

    async def start(self) -> None:
        if not self.enabled() or self._cleanup_task is not None:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        await asyncio.to_thread(self._cleanup_disk)

    async def stop(self) -> None:
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._records.clear()

    async def handle_event(self, event: Any) -> None:
        if not self.enabled():
            return
        payload = extract_event_payload(event)
        if not payload:
            logger.debug("[HelperTools/AntiRevoke] ignored event without raw OneBot payload")
            return
        if is_group_recall(payload):
            await self._handle_recall(event, payload)
        elif is_group_message(payload, event):
            await self._cache_message(event, payload)

    async def _cache_message(self, event: Any, payload: Mapping[str, Any]) -> None:
        group_id = _payload_value(payload, "group_id") or clean_text(
            getattr(event, "get_group_id", lambda: "")()
        )
        message_id = _event_id(payload, event, "message_id")
        sender = payload.get("sender")
        sender_map = sender if isinstance(sender, Mapping) else {}
        sender_id = _payload_value(payload, "user_id", "sender_id") or clean_text(
            sender_map.get("user_id")
        )
        if not group_id or not message_id or sender_id in self.ignore_senders():
            return
        message = extract_message_payload(payload, event)
        if _message_size(message) > self.max_message_bytes():
            logger.warning(
                "[HelperTools/AntiRevoke] skipped oversized message cache group=%s message=%s bytes>%s",
                group_id,
                message_id,
                self.max_message_bytes(),
            )
            return
        record = RevokeRecord(
            group_id=group_id,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=_payload_value(sender_map, "card", "nickname") or sender_id,
            timestamp=read_int(payload.get("time"), int(time.time())),
            message=message,
            cached_at=time.time(),
        )
        await self._cache_record_images(event, record)
        key = self._record_key(group_id, message_id)
        self._records[key] = record
        self._records.move_to_end(key)
        self._evict_memory_records()
        await asyncio.to_thread(self._write_record, record)

    async def _handle_recall(self, event: Any, payload: Mapping[str, Any]) -> None:
        group_id = _payload_value(payload, "group_id")
        message_id = _payload_value(payload, "message_id")
        operator_id = _payload_value(payload, "operator_id")
        sender_id = _payload_value(payload, "user_id", "sender_id")
        if not group_id or not message_id or not self._is_monitored(group_id):
            return
        if operator_id in self.ignore_operators():
            logger.info(
                "[HelperTools/AntiRevoke] ignored recall group=%s message=%s operator=%s",
                group_id,
                message_id,
                operator_id,
            )
            return
        record = await self._load_record(group_id, message_id)
        if record is None:
            record = await self._try_get_message(event, group_id, message_id, sender_id)
        if record is None:
            logger.warning(
                "[HelperTools/AntiRevoke] recall cache miss group=%s message=%s; "
                "the message may have arrived before the module was enabled or exceeded cache limits",
                group_id,
                message_id,
            )
            return
        if record.sender_id in self.ignore_senders():
            return
        targets = self._targets_for_group(group_id)
        if not targets:
            logger.warning(
                "[HelperTools/AntiRevoke] recall captured but no notification target is configured group=%s message=%s",
                group_id,
                message_id,
            )
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning("[HelperTools/AntiRevoke] recall has no OneBot bot handle group=%s", group_id)
            return
        if await self._cache_record_images(event, record):
            await asyncio.to_thread(self._write_record, record)
        group_name, sender_name, operator_name = await self._resolve_names(
            bot, group_id, record.sender_id or sender_id, operator_id
        )
        header = self._build_header(
            group_name,
            group_id,
            sender_name,
            record.sender_id or sender_id,
            operator_name,
            operator_id,
            record.timestamp,
        )
        original_message, restored_images = await asyncio.to_thread(
            _materialize_cached_images,
            sanitize_recall_message(record.message),
            record.image_cache_files,
            self.cache_dir,
        )
        for target_type, target_id in targets:
            await self._send_to_target(
                bot,
                target_type,
                target_id,
                record,
                header,
                original_message,
                restored_images,
            )

    async def _try_get_message(
        self,
        event: Any,
        group_id: str,
        message_id: str,
        sender_id: str,
    ) -> RevokeRecord | None:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        try:
            lookup_id: int | str = int(message_id) if message_id.isdigit() else message_id
            response = await asyncio.wait_for(
                call_onebot(bot, "get_msg", message_id=lookup_id),
                timeout=read_int(
                    cfg(self.config, "anti_revoke", "lookup_timeout_seconds", 3),
                    3,
                    minimum=1,
                    maximum=15,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - adapters expose implementation-specific errors
            logger.debug(
                "[HelperTools/AntiRevoke] get_msg fallback failed group=%s message=%s error=%r",
                group_id,
                message_id,
                exc,
            )
            return None
        looked_up = unwrap_onebot_payload(response)
        message = looked_up.get("message")
        if not isinstance(message, (str, list, dict)):
            return None
        sender = looked_up.get("sender")
        sender_map = sender if isinstance(sender, Mapping) else {}
        record = RevokeRecord(
            group_id=group_id,
            message_id=message_id,
            sender_id=_payload_value(looked_up, "user_id", "sender_id") or sender_id,
            sender_name=_payload_value(sender_map, "card", "nickname"),
            timestamp=read_int(looked_up.get("time"), int(time.time())),
            message=_json_safe(message),
            cached_at=time.time(),
        )
        if _message_size(record.message) > self.max_message_bytes():
            return None
        return record

    async def _load_record(self, group_id: str, message_id: str) -> RevokeRecord | None:
        key = self._record_key(group_id, message_id)
        record = self._records.get(key)
        if record is not None:
            if time.time() - record.cached_at <= self.cache_expiration_seconds():
                self._records.move_to_end(key)
                return record
            self._records.pop(key, None)
        path = self.cache_dir / _cache_filename(group_id, message_id)
        if not path.exists():
            return None
        try:
            data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
            if time.time() - float(data.get("cached_at", 0)) > self.cache_expiration_seconds():
                self._delete_record_cache(group_id, message_id)
                return None
            cached_images = data.get("image_cache_files")
            image_cache_files = (
                {
                    clean_text(key): clean_text(value)
                    for key, value in cached_images.items()
                    if clean_text(key) and clean_text(value)
                }
                if isinstance(cached_images, Mapping)
                else {}
            )
            record = RevokeRecord(
                group_id=clean_text(data.get("group_id")),
                message_id=clean_text(data.get("message_id")),
                sender_id=clean_text(data.get("sender_id")),
                sender_name=clean_text(data.get("sender_name")),
                timestamp=read_int(data.get("timestamp"), int(time.time())),
                message=data.get("message", ""),
                cached_at=float(data.get("cached_at", time.time())),
                image_cache_files=image_cache_files,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("[HelperTools/AntiRevoke] invalid cache file %s: %r", path, exc)
            return None
        if record.group_id != group_id or record.message_id != message_id:
            return None
        self._records[key] = record
        self._records.move_to_end(key)
        self._evict_memory_records()
        return record

    def _write_record(self, record: RevokeRecord) -> None:
        path = self.cache_dir / _cache_filename(record.group_id, record.message_id)
        payload = {
            "group_id": record.group_id,
            "message_id": record.message_id,
            "sender_id": record.sender_id,
            "sender_name": record.sender_name,
            "timestamp": record.timestamp,
            "message": record.message,
            "cached_at": record.cached_at,
            "image_cache_files": record.image_cache_files,
        }
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json_dumps(payload), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            logger.warning("[HelperTools/AntiRevoke] write cache failed path=%s error=%r", path, exc)
            temporary.unlink(missing_ok=True)

    def _evict_memory_records(self) -> None:
        while len(self._records) > self.max_cached_messages():
            _key, record = self._records.popitem(last=False)
            self._delete_record_cache(record.group_id, record.message_id)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(min(max(self.cache_expiration_seconds() // 2, 10), 300))
            self._remove_expired_memory()
            await asyncio.to_thread(self._cleanup_disk)

    def _remove_expired_memory(self) -> None:
        now = time.time()
        for key, record in list(self._records.items()):
            if now - record.cached_at > self.cache_expiration_seconds():
                self._records.pop(key, None)
                self._delete_record_cache(record.group_id, record.message_id)

    def _cleanup_disk(self) -> None:
        cutoff = time.time() - self.cache_expiration_seconds()
        for path in self.cache_dir.glob("cache_*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        for path in self.cache_dir.glob("media_*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _delete_record_cache(self, group_id: str, message_id: str) -> None:
        (self.cache_dir / _cache_filename(group_id, message_id)).unlink(missing_ok=True)
        prefix = _media_file_prefix(group_id, message_id)
        for path in self.cache_dir.glob(f"{prefix}*"):
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _is_monitored(self, group_id: str) -> bool:
        groups = self.monitor_groups()
        return not groups or group_id in groups

    @staticmethod
    def _record_key(group_id: str, message_id: str) -> str:
        return f"{group_id}:{message_id}"

    def _targets_for_group(self, group_id: str) -> list[tuple[str, str]]:
        custom = self._forward_targets.get(group_id)
        if custom:
            return self._parse_targets(custom)
        targets = [("private", value.lstrip("@")) for value in self.target_receivers()]
        targets.extend(("group", value.lstrip("#")) for value in self.target_groups())
        return [(kind, target) for kind, target in targets if target.isdigit()]

    @staticmethod
    def _parse_targets(values: list[str]) -> list[tuple[str, str]]:
        targets: list[tuple[str, str]] = []
        for value in values:
            text = clean_text(value)
            if len(text) > 1 and text[0] in "@#" and text[1:].isdigit():
                targets.append(("private" if text[0] == "@" else "group", text[1:]))
        return targets

    def add_forward_target(self, group_id: str, target: str) -> str:
        group_id = clean_text(group_id)
        target = clean_text(target)
        if not group_id or not group_id.isdigit():
            return "失败：群号必须是纯数字。"
        if len(target) < 2 or target[0] not in "@#" or not target[1:].isdigit():
            return "失败：目标格式应为 @QQ号（私聊）或 #群号（群聊）。"
        values = self._forward_targets.setdefault(group_id, [])
        if target not in values:
            values.append(target)
            self._save_forward_targets()
            return f"已添加撤回转发目标：群 {group_id} -> {target}。"
        return f"该撤回转发目标已存在：群 {group_id} -> {target}。"

    def remove_forward_target(self, group_id: str, target: str = "") -> str:
        group_id = clean_text(group_id)
        values = self._forward_targets.get(group_id)
        if not values:
            return f"群 {group_id} 没有单独的撤回转发配置。"
        target = clean_text(target)
        if target:
            if target not in values:
                return f"群 {group_id} 的配置中没有目标 {target}。"
            values.remove(target)
        else:
            values.clear()
        if not values:
            self._forward_targets.pop(group_id, None)
        self._save_forward_targets()
        return f"已取消群 {group_id} 的撤回转发目标。" if not target else f"已取消撤回转发目标：群 {group_id} -> {target}。"

    def list_forward_targets(self) -> str:
        if not self._forward_targets:
            return "当前没有按群单独设置撤回转发目标。"
        lines = ["按群设置的撤回转发目标："]
        lines.extend(f"群 {group_id} -> {', '.join(values)}" for group_id, values in self._forward_targets.items())
        return "\n".join(lines)

    def _load_forward_targets(self) -> dict[str, list[str]]:
        try:
            payload = json.loads(self.forward_targets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping):
            return {}
        return {
            clean_text(group_id): read_list(values)
            for group_id, values in payload.items()
            if clean_text(group_id) and read_list(values)
        }

    def _save_forward_targets(self) -> None:
        try:
            self.forward_targets_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.forward_targets_path.with_suffix(".tmp")
            temporary.write_text(json_dumps(self._forward_targets), encoding="utf-8")
            temporary.replace(self.forward_targets_path)
        except OSError as exc:
            logger.warning("[HelperTools/AntiRevoke] save forward targets failed: %r", exc)

    async def _resolve_names(
        self,
        bot: Any,
        group_id: str,
        sender_id: str,
        operator_id: str,
    ) -> tuple[str, str, str]:
        group_name = group_id
        sender_name = sender_id or "未知用户"
        operator_name = operator_id or ""
        try:
            info = await call_onebot(bot, "get_group_info", group_id=int(group_id))
            info = unwrap_onebot_payload(info)
            group_name = clean_text(info.get("group_name"), group_name)
        except Exception as exc:  # noqa: BLE001 - name lookup is best effort
            logger.debug(
                "[HelperTools/AntiRevoke] group name lookup failed group=%s error=%r",
                group_id,
                exc,
            )
        for user_id, fallback in ((sender_id, sender_name), (operator_id, operator_name)):
            if not user_id:
                continue
            try:
                info = await call_onebot(
                    bot,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                )
                info = unwrap_onebot_payload(info)
                name = clean_text(info.get("card")) or clean_text(info.get("nickname")) or fallback
            except Exception:  # noqa: BLE001 - name lookup is best effort
                name = fallback
            if user_id == sender_id:
                sender_name = name
            if user_id == operator_id:
                operator_name = name
        return group_name, sender_name, operator_name

    def _build_header(
        self,
        group_name: str,
        group_id: str,
        sender_name: str,
        sender_id: str,
        operator_name: str,
        operator_id: str,
        timestamp: int,
    ) -> str:
        if operator_id and operator_id != sender_id:
            operator = f"{operator_name or operator_id} ({operator_id})"
            operator_line = f"\n撤回操作者：{operator}"
        else:
            operator_line = ""
        moment = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        return (
            "【撤回消息】\n"
            f"群聊：{group_name} ({group_id})\n"
            f"发送者：{sender_name or sender_id or '未知用户'} ({sender_id or '未知'})"
            f"{operator_line}\n"
            f"时间：{moment}"
        )

    async def _send_to_target(
        self,
        bot: Any,
        target_type: str,
        target_id: str,
        record: RevokeRecord,
        header: str,
        original_message: Any,
        restored_images: int,
    ) -> None:
        action = "send_private_msg" if target_type == "private" else "send_group_msg"
        params = {"user_id" if target_type == "private" else "group_id": int(target_id)}
        include_header = self.include_recall_header()
        include_original = self.send_original_message()
        try:
            if include_header and include_original:
                message = _message_with_header(header, original_message)
            elif include_header:
                message = header
            elif include_original:
                message = original_message
            else:
                message = ""
            if message:
                await self._call_send(bot, action, params, message)
            logger.info(
                "[HelperTools/AntiRevoke] restored recall successfully group=%s message=%s "
                "target=%s:%s cached_images=%d",
                record.group_id,
                record.message_id,
                target_type,
                target_id,
                restored_images,
            )
        except Exception as exc:  # noqa: BLE001 - OneBot adapters vary by message type
            fallback = _message_to_text(sanitize_recall_message(record.message)) or "[原消息没有可转换的文字内容]"
            cached_images = _cached_image_segments(original_message) if include_original else []
            media_fallback_exc: BaseException | None = None
            if cached_images:
                fallback_text = f"原消息部分内容无法原样重发，已恢复图片。\n文字内容：{fallback}"
                if include_header:
                    fallback_text = f"{header}\n{fallback_text}"
                media_fallback = [_text_segment(fallback_text), *cached_images]
                try:
                    await self._call_send(bot, action, params, media_fallback)
                except Exception as image_exc:  # noqa: BLE001 - continue to text-only fallback
                    media_fallback_exc = image_exc
                else:
                    logger.warning(
                        "[HelperTools/AntiRevoke] original restore failed but image fallback succeeded "
                        "group=%s message=%s target=%s:%s cached_images=%d error=%r",
                        record.group_id,
                        record.message_id,
                        target_type,
                        target_id,
                        len(cached_images),
                        exc,
                    )
                    return

            fallback_parts: list[str] = []
            if include_header:
                fallback_parts.append(header)
            if include_original:
                fallback_parts.append(f"原消息重发失败，文字内容兜底：{fallback}")
            text_fallback = "\n".join(fallback_parts) or "撤回消息恢复失败。"
            try:
                await self._call_send(
                    bot,
                    action,
                    params,
                    text_fallback,
                )
            except Exception as fallback_exc:  # noqa: BLE001 - report both failures
                logger.error(
                    "[HelperTools/AntiRevoke] restore failed group=%s message=%s target=%s:%s "
                    "error=%r image_fallback_error=%r fallback_error=%r",
                    record.group_id,
                    record.message_id,
                    target_type,
                    target_id,
                    exc,
                    media_fallback_exc,
                    fallback_exc,
                )
            else:
                logger.warning(
                    "[HelperTools/AntiRevoke] original restore failed but text fallback succeeded "
                    "group=%s message=%s target=%s:%s error=%r",
                    record.group_id,
                    record.message_id,
                    target_type,
                    target_id,
                    exc,
                )

    @staticmethod
    async def _call_send(bot: Any, action: str, params: dict[str, Any], message: Any) -> None:
        result = await call_onebot(bot, action, message=message, **params)
        if not isinstance(result, Mapping):
            return
        status = clean_text(result.get("status")).casefold()
        retcode = result.get("retcode")
        if status in {"failed", "error"} or retcode not in (None, 0, "0"):
            raise RuntimeError(f"OneBot {action} returned status={status or 'unknown'} retcode={retcode}")
