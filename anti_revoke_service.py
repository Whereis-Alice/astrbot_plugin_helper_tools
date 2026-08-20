from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
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
_QQ_IMAGE_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": "https://qzone.qq.com/",
}
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


@dataclass(slots=True)
class _DeleteInterceptor:
    """Original OneBot methods replaced on one live bot instance."""

    bot: Any
    delete_msg: Any | None = None
    delete_msg_wrapper: Any | None = None
    call_action: Any | None = None
    call_action_wrapper: Any | None = None
    api: Any | None = None
    api_call_action: Any | None = None
    api_call_action_wrapper: Any | None = None


class _PreDeleteCaptureEvent:
    """Small event facade used while saving a message immediately before deletion."""

    def __init__(self, bot: Any, group_id: str = "") -> None:
        self.bot = bot
        self._group_id = group_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_messages(self) -> list[Any]:
        return []


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


def _component_type_name(component: Any) -> str:
    component_type = getattr(component, "type", None)
    name = clean_text(getattr(component_type, "name", "")) or clean_text(component_type)
    if not name:
        name = component.__class__.__name__
    return name.casefold()


def _component_to_segment(component: Any) -> dict[str, Any] | None:
    type_name = _component_type_name(component)
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
                data[key] = value
        return {"type": type_name, "data": data}
    if type_name in {"record", "video", "file", "json", "xml", "forward"}:
        data: dict[str, Any] = {}
        for key in ("file", "url", "name", "content"):
            value = getattr(component, key, None)
            if value not in (None, ""):
                data[key] = _json_safe(value)
        return {"type": type_name, "data": data}
    return None


def _event_image_components(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    try:
        value = getter() if callable(getter) else []
    except Exception:  # noqa: BLE001 - adapter events vary
        value = []
    if not isinstance(value, (list, tuple)):
        message_obj = getattr(event, "message_obj", None)
        candidate = getattr(message_obj, "message", None)
        if isinstance(candidate, (list, tuple)):
            value = candidate
    if not isinstance(value, (list, tuple)):
        for attribute in ("chain", "components"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, (list, tuple)):
                value = candidate
                break
        else:
            value = []
    return [component for component in value if _component_type_name(component) == "image"]


def _merge_image_segment_references(
    target: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    replace_existing: bool = False,
) -> dict[str, Any]:
    merged = _json_safe(target)
    if not isinstance(merged, dict):
        merged = {}
    target_data = merged.get("data")
    merged_data = dict(target_data) if isinstance(target_data, Mapping) else {}
    source_data = source.get("data")
    if isinstance(source_data, Mapping):
        for key in ("path", "file", "url"):
            value = clean_text(source_data.get(key))
            if value and (replace_existing or not clean_text(merged_data.get(key))):
                merged_data[key] = value
    merged["type"] = clean_text(merged.get("type"), "image")
    merged["data"] = merged_data
    return merged


def _merge_event_image_references(message: Any, event: Any) -> Any:
    components = _event_image_components(event)
    component_segments = [
        segment
        for component in components
        if (segment := _component_to_segment(component)) is not None
    ]
    if not component_segments:
        return message
    merged = _json_safe(message)
    raw_images = _iter_image_segments(merged)
    if not raw_images:
        if isinstance(merged, list):
            merged.extend(component_segments)
            return merged
        if isinstance(merged, Mapping):
            return [merged, *component_segments]
        text = clean_text(merged)
        return [_text_segment(text), *component_segments] if text else component_segments
    for (_key, raw_segment), component_segment in zip(
        raw_images,
        component_segments,
        strict=False,
    ):
        replacement = _merge_image_segment_references(raw_segment, component_segment)
        if isinstance(raw_segment, dict):
            raw_segment.clear()
            raw_segment.update(replacement)
    return merged


def extract_message_payload(payload: Mapping[str, Any], event: Any) -> Any:
    message = payload.get("message")
    if isinstance(message, (str, list, dict)):
        return _json_safe(message)
    getter = getattr(event, "get_messages", None)
    components = getter() if callable(getter) else []
    if not isinstance(components, (list, tuple)):
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


def _safe_image_error(error: BaseException) -> str:
    text = clean_text(error) or error.__class__.__name__
    text = re.sub(r"(?:base64://|data:image/)[^\s]+", "<embedded-image>", text)
    text = re.sub(r"https?://[^\s]+", "<http-image>", text)
    prefix = f"{error.__class__.__name__}:"
    rendered = text if text.casefold().startswith(prefix.casefold()) else f"{prefix} {text}"
    return rendered[:320]


def _image_id_candidates(data: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("file", "file_id", "id", "image"):
        reference = clean_text(data.get(key))
        if not reference or reference.casefold().startswith(
            ("http://", "https://", "file://", "base64://", "data:image/")
        ):
            continue
        if reference not in candidates:
            candidates.append(reference)
        base_name, extension = Path(reference).stem, Path(reference).suffix.casefold()
        if (
            extension in {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
            and base_name
            and base_name not in candidates
        ):
            candidates.append(base_name)
    return candidates


def _resolved_image_references(response: Any) -> list[str]:
    resolved = unwrap_onebot_payload(response)
    references: list[str] = []
    for key in ("base64", "path", "file", "url"):
        reference = clean_text(resolved.get(key))
        if not reference:
            continue
        if key == "base64" and not reference.startswith(("base64://", "data:image/")):
            reference = f"base64://{reference}"
        if reference not in references:
            references.append(reference)
    return references


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
        self._delete_interceptors: dict[int, _DeleteInterceptor] = {}
        self._pre_delete_capture_tasks: dict[str, asyncio.Task[None]] = {}

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

    def _install_delete_interceptor(self, bot: Any) -> None:
        """Save messages before plugins call OneBot's delete_msg API.

        OneBot normally emits a group-message event after an outgoing message is
        sent. A plugin can, however, send and immediately delete a message before
        that event reaches AstrBot. Intercepting deletion gives us one last
        reliable chance to read the message with get_msg while it still exists.
        """

        if bot is None or id(bot) in self._delete_interceptors:
            return

        interceptor = _DeleteInterceptor(bot=bot)
        installed = False

        original_delete = getattr(bot, "delete_msg", None)
        if callable(original_delete):

            async def delete_msg_wrapper(*args: Any, **kwargs: Any) -> Any:
                await self._capture_before_delete_from_args(bot, args, kwargs)
                value = original_delete(*args, **kwargs)
                if inspect.isawaitable(value):
                    return await value
                return value

            try:
                bot.delete_msg = delete_msg_wrapper
            except (AttributeError, TypeError):
                pass
            else:
                interceptor.delete_msg = original_delete
                interceptor.delete_msg_wrapper = delete_msg_wrapper
                installed = True

        original_call_action = getattr(bot, "call_action", None)
        if callable(original_call_action):
            call_action_wrapper = self._make_call_action_wrapper(bot, original_call_action)
            try:
                bot.call_action = call_action_wrapper
            except (AttributeError, TypeError):
                pass
            else:
                interceptor.call_action = original_call_action
                interceptor.call_action_wrapper = call_action_wrapper
                installed = True

        api = getattr(bot, "api", None)
        original_api_call_action = getattr(api, "call_action", None)
        if api is not None and api is not bot and callable(original_api_call_action):
            api_call_action_wrapper = self._make_call_action_wrapper(bot, original_api_call_action)
            try:
                api.call_action = api_call_action_wrapper
            except (AttributeError, TypeError):
                pass
            else:
                interceptor.api = api
                interceptor.api_call_action = original_api_call_action
                interceptor.api_call_action_wrapper = api_call_action_wrapper
                installed = True

        if installed:
            self._delete_interceptors[id(bot)] = interceptor
            logger.debug("[HelperTools/AntiRevoke] installed pre-delete capture hook")

    def _make_call_action_wrapper(self, bot: Any, original: Any) -> Any:
        async def call_action_wrapper(action: Any, *args: Any, **kwargs: Any) -> Any:
            if clean_text(action).casefold() in {"delete_msg", "delete_message"}:
                await self._capture_before_delete_from_args(bot, args, kwargs)
            value = original(action, *args, **kwargs)
            if inspect.isawaitable(value):
                return await value
            return value

        return call_action_wrapper

    async def _capture_before_delete_from_args(
        self,
        bot: Any,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> None:
        message_id = clean_text(kwargs.get("message_id") or kwargs.get("id"))
        if not message_id and args:
            message_id = clean_text(args[0])
        if not message_id:
            return
        try:
            await self._capture_before_delete(bot, message_id)
        except Exception as exc:  # noqa: BLE001 - never block the caller's recall API
            logger.debug(
                "[HelperTools/AntiRevoke] pre-delete capture failed message=%s error=%s",
                message_id,
                _safe_image_error(exc),
            )

    async def _capture_before_delete(self, bot: Any, message_id: str) -> None:
        if not self.enabled():
            return
        task_key = f"{id(bot)}:{message_id}"
        task = self._pre_delete_capture_tasks.get(task_key)
        if task is not None and task is not asyncio.current_task():
            await asyncio.shield(task)
            return

        async def capture() -> None:
            event = _PreDeleteCaptureEvent(bot)
            looked_up = await self._lookup_onebot_message(event, message_id)
            message = looked_up.get("message")
            group_id = _payload_value(looked_up, "group_id")
            if (
                not group_id
                or not isinstance(message, (str, list, dict))
                or not self._is_monitored(group_id)
            ):
                return
            sender = looked_up.get("sender")
            sender_map = sender if isinstance(sender, Mapping) else {}
            sender_id = _payload_value(looked_up, "user_id", "sender_id") or _payload_value(
                sender_map,
                "user_id",
            )
            if sender_id in self.ignore_senders():
                return

            record = await self._load_record(group_id, message_id)
            if record is None:
                record = RevokeRecord(
                    group_id=group_id,
                    message_id=message_id,
                    sender_id=sender_id,
                    sender_name=_payload_value(sender_map, "card", "nickname") or sender_id,
                    timestamp=read_int(looked_up.get("time"), int(time.time())),
                    message=_json_safe(message),
                    cached_at=time.time(),
                )
                if _message_size(record.message) > self.max_message_bytes():
                    return
                await self._store_record(record)

            event._group_id = group_id
            if await self._cache_record_images(event, record, phase="pre_delete"):
                await asyncio.to_thread(self._write_record, record)
            logger.debug(
                "[HelperTools/AntiRevoke] pre-delete message capture ready group=%s message=%s",
                group_id,
                message_id,
            )

        task = asyncio.create_task(capture())
        self._pre_delete_capture_tasks[task_key] = task
        try:
            await task
        finally:
            if self._pre_delete_capture_tasks.get(task_key) is task:
                self._pre_delete_capture_tasks.pop(task_key, None)

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
            headers=_QQ_IMAGE_HEADERS,
        )
        return data, _validated_image_extension(data)

    async def _read_image_component(self, component: Any) -> tuple[bytes, str]:
        errors: list[str] = []
        for key in ("path", "file", "url"):
            reference = clean_text(getattr(component, key, ""))
            if not reference:
                continue
            try:
                return await self._read_image_reference(reference, allow_local=True)
            except (OSError, ValueError) as exc:
                errors.append(_safe_image_error(exc))

        converter = getattr(component, "convert_to_base64", None)
        if callable(converter):
            try:
                converted = converter()
                if inspect.isawaitable(converted):
                    converted = await converted
                if isinstance(converted, bytes):
                    return converted, _validated_image_extension(converted)
                reference = clean_text(converted)
                if reference and not reference.startswith(("base64://", "data:image/")):
                    reference = f"base64://{reference}"
                if reference:
                    return await self._read_image_reference(reference)
            except Exception as exc:  # noqa: BLE001 - component implementations vary
                errors.append(_safe_image_error(exc))
        detail = "; ".join(errors[-3:]) or "component has no usable image reference"
        raise ValueError(detail)

    async def _resolve_image_with_onebot(
        self,
        event: Any,
        data: Mapping[str, Any],
    ) -> tuple[bytes, str]:
        bot = getattr(event, "bot", None)
        candidates = _image_id_candidates(data)
        if bot is None or not candidates:
            raise ValueError("image has no OneBot-resolvable file identifier")

        actions: list[tuple[str, dict[str, Any]]] = []
        for candidate in candidates:
            actions.extend(
                (
                    ("get_image", {"file": candidate}),
                    ("get_image", {"file_id": candidate}),
                    ("get_image", {"id": candidate}),
                    ("get_image", {"image": candidate}),
                    ("get_file", {"file_id": candidate}),
                    ("get_file", {"file": candidate}),
                )
            )
        try:
            group_id = clean_text(getattr(event, "get_group_id", lambda: "")())
        except Exception:  # noqa: BLE001 - notice events vary by adapter
            group_id = ""
        if group_id:
            group_value: int | str = int(group_id) if group_id.isdigit() else group_id
            actions.extend(
                (
                    "get_group_file_url",
                    {"group_id": group_value, "file_id": candidate},
                )
                for candidate in candidates
            )

        deadline = asyncio.get_running_loop().time() + _IMAGE_FETCH_TIMEOUT_SECONDS
        errors: list[str] = []
        seen_references: set[str] = set(candidates)
        for action, params in actions:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                errors.append("OneBot image resolution timed out")
                break
            try:
                response = await asyncio.wait_for(
                    call_onebot(bot, action, **params),
                    timeout=min(1.5, remaining),
                )
            except Exception as exc:  # noqa: BLE001 - adapters expose varying errors
                errors.append(f"{action}: {_safe_image_error(exc)}")
                continue
            for reference in _resolved_image_references(response):
                if reference in seen_references:
                    continue
                seen_references.add(reference)
                try:
                    return await self._read_image_reference(reference, allow_local=True)
                except (OSError, ValueError) as exc:
                    errors.append(f"{action} result: {_safe_image_error(exc)}")
        detail = "; ".join(errors[-4:]) or "OneBot returned no downloadable image"
        raise ValueError(detail)

    async def _read_image_segment(
        self,
        event: Any,
        segment: Mapping[str, Any],
        *,
        component: Any | None = None,
        resolve_onebot: bool = False,
    ) -> tuple[bytes, str]:
        raw_data = segment.get("data")
        data = raw_data if isinstance(raw_data, Mapping) else {}
        references = [
            (key, value)
            for key in ("path", "file", "url")
            if (value := clean_text(data.get(key)))
        ]
        seen: set[str] = set()
        errors: list[str] = []
        for key, reference in references:
            if reference in seen:
                continue
            seen.add(reference)
            try:
                return await self._read_image_reference(
                    reference,
                    allow_local=key in {"path", "file"},
                )
            except (OSError, ValueError) as exc:
                errors.append(_safe_image_error(exc))
        if component is not None:
            try:
                return await self._read_image_component(component)
            except (OSError, ValueError) as exc:
                errors.append(f"AstrBot Image: {_safe_image_error(exc)}")
        if resolve_onebot:
            try:
                return await self._resolve_image_with_onebot(event, data)
            except (OSError, ValueError) as exc:
                errors.append(f"OneBot: {_safe_image_error(exc)}")
        detail = "; ".join(errors[-4:]) or "image has no downloadable reference"
        raise ValueError(detail)

    async def _lookup_onebot_message(self, event: Any, message_id: str) -> dict[str, Any]:
        bot = getattr(event, "bot", None)
        if bot is None or not message_id:
            return {}
        lookup_values: list[int | str] = [message_id]
        if message_id.isdigit():
            lookup_values.insert(0, int(message_id))
        attempts: list[dict[str, Any]] = []
        for value in lookup_values:
            attempts.extend(({"message_id": value}, {"id": value}))

        timeout = read_int(
            cfg(self.config, "anti_revoke", "lookup_timeout_seconds", 3),
            3,
            minimum=1,
            maximum=15,
        )
        deadline = asyncio.get_running_loop().time() + timeout
        last_error: BaseException | None = None
        for params in attempts:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                response = await asyncio.wait_for(
                    call_onebot(bot, "get_msg", **params),
                    timeout=min(1.5, remaining),
                )
            except Exception as exc:  # noqa: BLE001 - adapters expose varying errors
                last_error = exc
                continue
            payload = unwrap_onebot_payload(response)
            if payload:
                return payload
        if last_error is not None:
            logger.debug(
                "[HelperTools/AntiRevoke] get_msg lookup failed message=%s error=%s",
                message_id,
                _safe_image_error(last_error),
            )
        return {}

    def _cached_image_path(self, filename_value: Any) -> Path | None:
        filename = clean_text(filename_value)
        if not filename or Path(filename).name != filename:
            return None
        path = (self.cache_dir / filename).resolve(strict=False)
        if path.parent != self.cache_dir.resolve(strict=False):
            return None
        return path

    async def _cache_record_images(
        self,
        event: Any,
        record: RevokeRecord,
        *,
        phase: str = "capture",
    ) -> bool:
        all_segments = _iter_image_segments(record.message)
        segments = all_segments[:_IMAGE_CACHE_MAX_COUNT]
        pending: list[tuple[str, Mapping[str, Any]]] = []
        for key, segment in segments:
            path = self._cached_image_path(record.image_cache_files.get(key))
            if path is not None and path.is_file():
                continue
            pending.append((key, segment))
        if not pending:
            return False

        components = _event_image_components(event)
        component_by_key: dict[str, Any] = {}
        component_index = 0
        for key, segment in segments:
            if clean_text(segment.get("type")).casefold() != "image":
                continue
            if component_index >= len(components):
                break
            component_by_key[key] = components[component_index]
            component_index += 1

        changed = False
        message_changed = False
        error_history: dict[str, list[str]] = {}

        async def cache_one(
            key: str,
            segment: Mapping[str, Any],
            *,
            component: Any | None,
            resolve_onebot: bool,
        ) -> tuple[str, str]:
            data, extension = await self._read_image_segment(
                event,
                segment,
                component=component,
                resolve_onebot=resolve_onebot,
            )
            filename = _media_filename(record.group_id, record.message_id, key, extension)
            path = self.cache_dir / filename
            await asyncio.to_thread(_write_bytes_atomic, path, data)
            return key, filename

        async def run_attempt(
            entries: list[tuple[str, Mapping[str, Any]]],
            *,
            use_components: bool,
            resolve_onebot: bool,
        ) -> list[tuple[str, Mapping[str, Any]]]:
            nonlocal changed
            results = await asyncio.gather(
                *(
                    cache_one(
                        key,
                        segment,
                        component=component_by_key.get(key) if use_components else None,
                        resolve_onebot=resolve_onebot,
                    )
                    for key, segment in entries
                ),
                return_exceptions=True,
            )
            failed: list[tuple[str, Mapping[str, Any]]] = []
            for (key, segment), result in zip(entries, results, strict=True):
                if isinstance(result, BaseException):
                    error_history.setdefault(key, []).append(_safe_image_error(result))
                    failed.append((key, segment))
                    continue
                cached_key, filename = result
                old_path = self._cached_image_path(record.image_cache_files.get(cached_key))
                if old_path is not None and old_path.name != filename:
                    old_path.unlink(missing_ok=True)
                record.image_cache_files[cached_key] = filename
                changed = True
            return failed

        failed = await run_attempt(
            pending,
            use_components=True,
            resolve_onebot=False,
        )
        if failed:
            looked_up = await self._lookup_onebot_message(event, record.message_id)
            fresh_message = looked_up.get("message")
            fresh_segments = (
                _iter_image_segments(_json_safe(fresh_message))
                if isinstance(fresh_message, (list, dict))
                else []
            )
            segment_positions = {key: index for index, (key, _segment) in enumerate(segments)}
            retry_entries: list[tuple[str, Mapping[str, Any]]] = []
            for key, segment in failed:
                position = segment_positions.get(key)
                if position is None or position >= len(fresh_segments):
                    error_history.setdefault(key, []).append("get_msg returned no matching image")
                    retry_entries.append((key, segment))
                    continue
                fresh_segment = fresh_segments[position][1]
                replacement = _merge_image_segment_references(
                    segment,
                    fresh_segment,
                    replace_existing=True,
                )
                if isinstance(segment, dict) and replacement != segment:
                    segment.clear()
                    segment.update(replacement)
                    message_changed = True
                retry_entries.append((key, segment))
            failed = await run_attempt(
                retry_entries,
                use_components=False,
                resolve_onebot=False,
            )
        if failed:
            failed = await run_attempt(
                failed,
                use_components=False,
                resolve_onebot=True,
            )

        cached_count = sum(
            1
            for key, _segment in segments
            if (path := self._cached_image_path(record.image_cache_files.get(key))) is not None
            and path.is_file()
        )
        if failed or cached_count < len(segments):
            for key, _segment in failed:
                logger.warning(
                    "[HelperTools/AntiRevoke] image snapshot failed phase=%s group=%s "
                    "message=%s segment=%s error=%s",
                    phase,
                    record.group_id,
                    record.message_id,
                    key,
                    "; ".join(error_history.get(key, [])[-4:]),
                )
            logger.warning(
                "[HelperTools/AntiRevoke] image snapshot incomplete phase=%s group=%s "
                "message=%s cached_images=%d/%d",
                phase,
                record.group_id,
                record.message_id,
                cached_count,
                len(all_segments),
            )
        elif changed:
            logger.info(
                "[HelperTools/AntiRevoke] image snapshot ready phase=%s group=%s "
                "message=%s cached_images=%d/%d",
                phase,
                record.group_id,
                record.message_id,
                cached_count,
                len(all_segments),
            )
        return changed or message_changed

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
        capture_tasks = list(self._pre_delete_capture_tasks.values())
        self._pre_delete_capture_tasks.clear()
        for capture_task in capture_tasks:
            if not capture_task.done():
                capture_task.cancel()
        if capture_tasks:
            await asyncio.gather(*capture_tasks, return_exceptions=True)
        self._restore_delete_interceptors()
        self._records.clear()

    async def handle_event(self, event: Any) -> None:
        if not self.enabled():
            return
        payload = extract_event_payload(event)
        if not payload:
            logger.debug("[HelperTools/AntiRevoke] ignored event without raw OneBot payload")
            return
        if is_group_recall(payload) or is_group_message(payload, event):
            self._install_delete_interceptor(getattr(event, "bot", None))
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
        message = _merge_event_image_references(
            extract_message_payload(payload, event),
            event,
        )
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
        # Store first: a bot can send and delete an image before its download
        # finishes, but the recall handler can still find the text/segments.
        await self._store_record(record)
        await self._cache_record_images(event, record, phase="capture")
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
        if await self._cache_record_images(event, record, phase="recall"):
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
        looked_up = await self._lookup_onebot_message(event, message_id)
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

    async def _store_record(self, record: RevokeRecord) -> None:
        """Make a record visible before any slow image snapshot work begins."""

        key = self._record_key(record.group_id, record.message_id)
        self._records[key] = record
        self._records.move_to_end(key)
        self._evict_memory_records()
        await asyncio.to_thread(self._write_record, record)

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

    def _restore_delete_interceptors(self) -> None:
        for interceptor in self._delete_interceptors.values():
            self._restore_interceptor_attribute(
                interceptor.bot,
                "delete_msg",
                interceptor.delete_msg_wrapper,
                interceptor.delete_msg,
            )
            self._restore_interceptor_attribute(
                interceptor.bot,
                "call_action",
                interceptor.call_action_wrapper,
                interceptor.call_action,
            )
            self._restore_interceptor_attribute(
                interceptor.api,
                "call_action",
                interceptor.api_call_action_wrapper,
                interceptor.api_call_action,
            )
        self._delete_interceptors.clear()

    @staticmethod
    def _restore_interceptor_attribute(
        target: Any,
        name: str,
        wrapper: Any,
        original: Any,
    ) -> None:
        if target is None or wrapper is None or original is None:
            return
        try:
            if getattr(target, name, None) is wrapper:
                setattr(target, name, original)
        except (AttributeError, TypeError):
            return

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
        expected_images = len(_iter_image_segments(record.message)) if include_original else 0
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
            if restored_images < expected_images:
                logger.warning(
                    "[HelperTools/AntiRevoke] recall notification sent with incomplete image "
                    "restore group=%s message=%s target=%s:%s restored_images=%d/%d",
                    record.group_id,
                    record.message_id,
                    target_type,
                    target_id,
                    restored_images,
                    expected_images,
                )
            logger.info(
                "[HelperTools/AntiRevoke] recall notification sent group=%s message=%s "
                "target=%s:%s restored_images=%d/%d",
                record.group_id,
                record.message_id,
                target_type,
                target_id,
                restored_images,
                expected_images,
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
