"""Privacy-conscious activity records used by the Helper Tools Dashboard."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .helper_utils import cfg, clean_text, read_bool


_ACTIVITY_FILE_NAME = "webui_activity.json"
_MAX_DETAIL_LENGTH = 240
_MAX_RECORDS_HARD_LIMIT = 5_000
_SESSION_KIND_RE = re.compile(r":(GroupMessage|FriendMessage|PrivateMessage|\w+Message):")


class WebUiActivityLog:
    """A small local audit trail for high-signal plugin operations.

    It deliberately records neither message content nor secrets. The optional
    session identifier is off by default because group and user IDs can be
    sensitive in an administrator-facing activity view as well.
    """

    def __init__(self, config: Any, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir
        self.path = data_dir / _ACTIVITY_FILE_NAME
        self._records: list[dict[str, str]] | None = None
        self._lock = threading.RLock()

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "webui", "activity_log_enabled", True), True)

    def include_session_id(self) -> bool:
        return read_bool(
            cfg(self.config, "webui", "activity_log_include_session_id", False),
            False,
        )

    def record(
        self,
        module: str,
        action: str,
        *,
        status: str = "success",
        detail: str = "",
        event: Any = None,
    ) -> None:
        """Append one non-sensitive record without affecting the main feature."""

        if not self.enabled():
            return
        try:
            normalized_module = self._safe_label(module, fallback="unknown")
            normalized_action = self._safe_label(action, fallback="operation")
            normalized_status = self._safe_label(status, fallback="success")
            record = {
                "at": datetime.now(UTC).isoformat(),
                "module": normalized_module,
                "action": normalized_action,
                "status": normalized_status,
                "detail": self._safe_detail(detail),
                "session_kind": self._event_session_kind(event),
            }
            if self.include_session_id():
                session = clean_text(getattr(event, "unified_msg_origin", ""))
                if session:
                    record["session"] = session[:180]
            with self._lock:
                records = self._load_records_locked()
                records.append(record)
                self._prune_records_locked(records)
                self._save_records_locked(records)
        except Exception as exc:  # noqa: BLE001 - activity logging is strictly best effort
            logger.debug("[HelperTools/WebUI] activity record skipped: %r", exc)

    def get_records(
        self,
        *,
        limit: int = 100,
        module: str = "",
        status: str = "",
    ) -> list[dict[str, str]]:
        """Return newest first records after applying safe dashboard filters."""

        safe_limit = max(1, min(int(limit), 500))
        wanted_module = clean_text(module)
        wanted_status = clean_text(status)
        include_session_id = self.include_session_id()
        with self._lock:
            records = self._load_records_locked()
            self._prune_records_locked(records)
            selected: list[dict[str, str]] = []
            for item in reversed(records):
                if wanted_module and item.get("module") != wanted_module:
                    continue
                if wanted_status and item.get("status") != wanted_status:
                    continue
                public_item = dict(item)
                if not include_session_id:
                    public_item.pop("session", None)
                selected.append(public_item)
            self._save_records_locked(records)
        return selected[:safe_limit]

    def summary(self) -> dict[str, Any]:
        """Return compact dashboard metrics without exposing individual records."""

        now = datetime.now(UTC)
        today_cutoff = now - timedelta(days=1)
        week_cutoff = now - timedelta(days=7)
        with self._lock:
            records = self._load_records_locked()
            self._prune_records_locked(records)
            self._save_records_locked(records)

        today = 0
        recent_week = 0
        failures = 0
        modules: set[str] = set()
        for item in records:
            at = self._parse_time(item.get("at", ""))
            if at is not None and at >= today_cutoff:
                today += 1
            if at is not None and at >= week_cutoff:
                recent_week += 1
            if item.get("status") in {"failed", "error", "warning"}:
                failures += 1
            if item.get("module"):
                modules.add(item["module"])
        return {
            "enabled": self.enabled(),
            "total": len(records),
            "today": today,
            "recent_week": recent_week,
            "failure_count": failures,
            "module_count": len(modules),
        }

    def clear(self) -> int:
        """Erase the local dashboard audit trail and return the deleted count."""

        with self._lock:
            records = self._load_records_locked()
            deleted = len(records)
            records.clear()
            self._save_records_locked(records)
        return deleted

    def _load_records_locked(self) -> list[dict[str, str]]:
        if self._records is not None:
            return self._records
        records: list[dict[str, str]] = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    at = clean_text(item.get("at", ""))
                    module = self._safe_label(item.get("module", ""), fallback="unknown")
                    action = self._safe_label(item.get("action", ""), fallback="operation")
                    status = self._safe_label(item.get("status", ""), fallback="success")
                    if not at:
                        continue
                    record = {
                        "at": at[:64],
                        "module": module,
                        "action": action,
                        "status": status,
                        "detail": self._safe_detail(item.get("detail", "")),
                        "session_kind": self._safe_label(
                            item.get("session_kind", ""),
                            fallback="",
                        ),
                    }
                    if self.include_session_id() and clean_text(item.get("session", "")):
                        record["session"] = clean_text(item["session"])[:180]
                    records.append(record)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("[HelperTools/WebUI] could not read activity records: %r", exc)
        self._records = records[-_MAX_RECORDS_HARD_LIMIT:]
        return self._records

    def _save_records_locked(self, records: list[dict[str, str]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = json.dumps(
            records[-_MAX_RECORDS_HARD_LIMIT:],
            ensure_ascii=False,
            indent=2,
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune_records_locked(self, records: list[dict[str, str]]) -> None:
        try:
            retention_days = int(cfg(self.config, "webui", "activity_log_retention_days", 14))
        except (TypeError, ValueError):
            retention_days = 14
        retention_days = max(1, min(retention_days, 365))
        try:
            maximum = int(cfg(self.config, "webui", "activity_log_max_records", 500))
        except (TypeError, ValueError):
            maximum = 500
        maximum = max(20, min(maximum, _MAX_RECORDS_HARD_LIMIT))
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        records[:] = [
            item
            for item in records
            if (parsed := self._parse_time(item.get("at", ""))) is not None and parsed >= cutoff
        ][-maximum:]

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _safe_label(value: Any, *, fallback: str) -> str:
        text = clean_text(value)
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
        text = " ".join(text.split())
        return text[:80] or fallback

    @staticmethod
    def _safe_detail(value: Any) -> str:
        text = clean_text(value)
        # Never preserve accidental credentials in a generic status message.
        text = re.sub(
            r"(?i)(cookie|token|secret|api[_ -]?key|authorization)\s*[=:]\s*[^\s,;]+",
            r"\1=[redacted]",
            text,
        )
        text = " ".join(text.split())
        return text[:_MAX_DETAIL_LENGTH]

    @staticmethod
    def _event_session_kind(event: Any) -> str:
        if event is None:
            return ""
        raw = clean_text(getattr(event, "unified_msg_origin", ""))
        matched = _SESSION_KIND_RE.search(raw)
        if matched:
            return matched.group(1)
        get_group_id = getattr(event, "get_group_id", None)
        try:
            group_id = clean_text(get_group_id()) if callable(get_group_id) else ""
        except Exception:  # noqa: BLE001 - activity recording must not affect message handling
            group_id = ""
        return "GroupMessage" if group_id else "Message"
