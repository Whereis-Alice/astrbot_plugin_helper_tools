from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .helper_utils import cfg, clean_text, json_dumps, read_bool, read_int, read_list
from .qq_features import call_onebot


@dataclass(slots=True)
class RevokeRecord:
    group_id: str
    message_id: str
    sender_id: str
    sender_name: str
    timestamp: int
    message: Any
    cached_at: float


def unwrap_onebot_payload(value: Any) -> dict[str, Any]:
    """Read both direct OneBot payloads and API-style ``data`` wrappers."""

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

    for _ in range(2):
        nested = payload.get("data")
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
        for target_type, target_id in targets:
            await self._send_to_target(
                bot,
                target_type,
                target_id,
                record,
                header,
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
                path.unlink(missing_ok=True)
                return None
            record = RevokeRecord(
                group_id=clean_text(data.get("group_id")),
                message_id=clean_text(data.get("message_id")),
                sender_id=clean_text(data.get("sender_id")),
                sender_name=clean_text(data.get("sender_name")),
                timestamp=read_int(data.get("timestamp"), int(time.time())),
                message=data.get("message", ""),
                cached_at=float(data.get("cached_at", time.time())),
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
            (self.cache_dir / _cache_filename(record.group_id, record.message_id)).unlink(
                missing_ok=True
            )

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

    def _cleanup_disk(self) -> None:
        cutoff = time.time() - self.cache_expiration_seconds()
        for path in self.cache_dir.glob("cache_*.json"):
            try:
                if path.stat().st_mtime < cutoff:
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
    ) -> None:
        action = "send_private_msg" if target_type == "private" else "send_group_msg"
        params = {"user_id" if target_type == "private" else "group_id": int(target_id)}
        try:
            if self.include_recall_header():
                await self._call_send(bot, action, params, header)
            if self.send_original_message():
                await self._call_send(bot, action, params, record.message)
            logger.info(
                "[HelperTools/AntiRevoke] restored recall successfully group=%s message=%s target=%s:%s",
                record.group_id,
                record.message_id,
                target_type,
                target_id,
            )
        except Exception as exc:  # noqa: BLE001 - OneBot adapters vary by message type
            fallback = _message_to_text(record.message) or "[原消息没有可转换的文字内容]"
            try:
                await self._call_send(
                    bot,
                    action,
                    params,
                    f"{header}\n原消息重发失败，文字内容兜底：{fallback}",
                )
            except Exception as fallback_exc:  # noqa: BLE001 - report both failures
                logger.error(
                    "[HelperTools/AntiRevoke] restore failed group=%s message=%s target=%s:%s "
                    "error=%r fallback_error=%r",
                    record.group_id,
                    record.message_id,
                    target_type,
                    target_id,
                    exc,
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
