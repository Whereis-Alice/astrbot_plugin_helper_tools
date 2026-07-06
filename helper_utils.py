from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; AstrBot HelperTools; "
    "+https://github.com/Whereis-Alice/astrbot_plugin_helper_tools)"
)


def clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def section(config: Any, key: str) -> dict[str, Any]:
    if hasattr(config, "get"):
        value = config.get(key, {})
        return value if isinstance(value, dict) else {}
    return {}


def cfg(config: Any, section_name: str, key: str, default: Any = None) -> Any:
    return section(config, section_name).get(key, default)


def read_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled", "启用", "是"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled", "禁用", "否"}:
            return False
    if value is None:
        return default
    return bool(value)


def read_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def read_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def read_list(value: Any, default: list[str] | None = None) -> list[str]:
    fallback = default or []
    if isinstance(value, list):
        items = [clean_text(item) for item in value]
        return [item for item in items if item] or fallback
    if isinstance(value, str):
        normalized = value.replace("，", ",").replace("；", ";")
        items: list[str] = []
        for chunk in normalized.split(";"):
            items.extend(part.strip() for part in chunk.split(","))
        return [item for item in items if item] or fallback
    return fallback


def core_wake_prefixes(context: Any, default: list[str] | None = None) -> list[str]:
    fallback = default or ["/"]
    core_config: dict[str, Any] = {}
    getter = getattr(context, "get_config", None)
    if callable(getter):
        try:
            value = getter() or {}
            if isinstance(value, dict):
                core_config = value
        except Exception:
            core_config = {}
    return read_list(core_config.get("wake_prefix"), fallback) or fallback


def expand_wake_prefixed_commands(
    commands: Iterable[str],
    wake_prefixes: Iterable[str],
    *,
    include_plain: bool = False,
) -> list[str]:
    prefixes = [clean_text(prefix) for prefix in wake_prefixes if clean_text(prefix)]
    expanded: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = clean_text(value)
        if value and value not in seen:
            seen.add(value)
            expanded.append(value)

    for command in commands:
        command = clean_text(command)
        if not command:
            continue
        matched_prefix = next((prefix for prefix in prefixes if command.startswith(prefix)), "")
        if matched_prefix:
            body = command[len(matched_prefix) :].lstrip()
            add(command)
            if include_plain and body:
                add(body)
        elif command.startswith("/"):
            body = command[1:].lstrip()
            add(command)
            if include_plain and body:
                add(body)
        else:
            body = command
            if include_plain:
                add(command)
        if body:
            for prefix in prefixes:
                add(f"{prefix}{body}")
                if include_plain:
                    add(f"{prefix}{prefix}{body}")
    return expanded


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if value is False:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip() in {"-", "0", "None", "null"}
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def format_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return ""


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def parse_dynamic_command(text: str, prefixes: Iterable[str]) -> tuple[str, str] | None:
    stripped = clean_text(text)
    if not stripped:
        return None
    for prefix in sorted((clean_text(item) for item in prefixes), key=len, reverse=True):
        if not prefix:
            continue
        if stripped == prefix:
            return prefix, ""
        if stripped.startswith(prefix):
            rest = stripped[len(prefix) :]
            if not rest or rest[0].isspace():
                return prefix, rest.strip()
    return None


def extract_file_config_value(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        for item in value:
            found = extract_file_config_value(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("path", "file", "url", "name"):
            found = extract_file_config_value(value.get(key))
            if found:
                return found
    return ""


def resolve_existing_path(raw_path: str, *extra_roots: Path) -> Path | None:
    text = clean_text(raw_path)
    if not text:
        return None
    path = Path(text).expanduser()
    candidates = [path]
    if not path.is_absolute():
        candidates.append(Path.cwd() / path)
        for root in extra_roots:
            candidates.append(root / path)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if resolved.exists():
            return resolved
    return None


def fetch_bytes_sync(
    url: str,
    *,
    timeout_seconds: float = 10.0,
    max_bytes: int = 10 * 1024 * 1024,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content_type = response.headers.get_content_type() or ""
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"response is larger than {max_bytes} bytes")
        return data, content_type


async def fetch_bytes(
    url: str,
    *,
    timeout_seconds: float = 10.0,
    max_bytes: int = 10 * 1024 * 1024,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    return await asyncio.to_thread(
        fetch_bytes_sync,
        url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        headers=headers,
    )


async def fetch_json(
    url: str,
    *,
    timeout_seconds: float = 10.0,
    max_bytes: int = 10 * 1024 * 1024,
    headers: dict[str, str] | None = None,
) -> Any:
    data, _content_type = await fetch_bytes(
        url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        headers=headers,
    )
    return json.loads(data.decode("utf-8"))


def quote_query(value: str) -> str:
    return urllib.parse.quote(clean_text(value), safe="")


class RollingRateLimiter:
    def __init__(self, *, window_seconds: int = 600, max_requests: int = 300) -> None:
        self.window_seconds = max(1, int(window_seconds))
        self.max_requests = max(1, int(max_requests))
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def allow(self) -> tuple[bool, float]:
        now = time.time()
        cutoff = now - self.window_seconds
        async with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) < self.max_requests:
                self._timestamps.append(now)
                return True, 0.0
            retry_after = max(0.0, self._timestamps[0] + self.window_seconds - now)
            return False, retry_after
