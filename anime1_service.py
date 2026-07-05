from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .helper_utils import DEFAULT_USER_AGENT, cfg, clean_text, fetch_json, read_bool, read_int, read_list, truncate


ANIME1_LIST_URL = "https://anime1.me/animelist.json"
ANIME1_WATCH_URL = "https://anime1.me/?cat={anime_id}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _resolve_redirect_sync(url: str, timeout_seconds: int) -> str:
    opener = urllib.request.build_opener(_NoRedirectHandler)
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        opener.open(request, timeout=timeout_seconds).close()
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            return clean_text(exc.headers.get("Location"))
        raise
    return ""


class Anime1Service:
    def __init__(self, config: Any, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir
        self.cache_path = self.data_dir / "anime1_list.json"
        self._scheduler_task: asyncio.Task | None = None
        self._last_run_keys: set[str] = set()

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "anime1", "enabled", True), True)

    def update_on_start(self) -> bool:
        return read_bool(cfg(self.config, "anime1", "update_on_start", False), False)

    def update_times(self) -> list[int]:
        raw = read_list(cfg(self.config, "anime1", "update_times", ["1"]), ["1"])
        hours: list[int] = []
        for item in raw:
            try:
                hour = int(item)
            except ValueError:
                continue
            if 0 <= hour <= 23 and hour not in hours:
                hours.append(hour)
        return hours or [1]

    def timeout(self) -> int:
        return read_int(cfg(self.config, "anime1", "timeout_seconds", 20), 20, minimum=3, maximum=120)

    def default_limit(self) -> int:
        return read_int(cfg(self.config, "anime1", "default_limit", 20), 20, minimum=1, maximum=200)

    async def start(self) -> None:
        if not self.enabled():
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.update_on_start():
            try:
                await self.update_cache()
            except Exception as exc:
                logger.warning("[HelperTools] Anime1 startup update failed: %s", exc)
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        if self._scheduler_task is None:
            return
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                now = datetime.now()
                run_key = f"{now:%Y-%m-%d}-{now.hour}"
                if now.minute == 0 and now.hour in self.update_times() and run_key not in self._last_run_keys:
                    self._last_run_keys.add(run_key)
                    await self.update_cache()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[HelperTools] Anime1 scheduler error: %s", exc)
                await asyncio.sleep(60)

    def load_cache(self) -> list[dict[str, Any]]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[HelperTools] failed to load Anime1 cache: %s", exc)
            return []
        return payload if isinstance(payload, list) else []

    def save_cache(self, entries: list[dict[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def fetch_remote_list(self) -> list[Any]:
        payload = await fetch_json(ANIME1_LIST_URL, timeout_seconds=self.timeout())
        if not isinstance(payload, list):
            raise ValueError("Anime1 返回内容不是列表。")
        return payload

    async def update_cache(self) -> int:
        remote = await self.fetch_remote_list()
        existing = {str(item.get("id")): item for item in self.load_cache() if item.get("id") is not None}
        now = datetime.now().isoformat(timespec="seconds")
        new_ids: set[str] = set()
        merged: list[dict[str, Any]] = []
        for raw in remote:
            if not isinstance(raw, list) or not raw:
                continue
            anime_id = clean_text(raw[0])
            if not anime_id:
                continue
            new_ids.add(anime_id)
            old = existing.get(anime_id, {})
            entry = {
                "id": anime_id,
                "title": clean_text(raw[1]) if len(raw) > 1 else clean_text(old.get("title")),
                "status": clean_text(raw[2]) if len(raw) > 2 else clean_text(old.get("status")),
                "year": clean_text(raw[3]) if len(raw) > 3 else clean_text(old.get("year")),
                "season": clean_text(raw[4]) if len(raw) > 4 else clean_text(old.get("season")),
                "extra": clean_text(raw[5]) if len(raw) > 5 else clean_text(old.get("extra")),
                "first_seen_at": clean_text(old.get("first_seen_at"), now),
                "last_seen_at": now,
            }
            if (
                entry["title"] != old.get("title")
                or entry["status"] != old.get("status")
                or entry["year"] != old.get("year")
                or entry["season"] != old.get("season")
                or entry["extra"] != old.get("extra")
            ):
                entry["updated_at"] = now
            else:
                entry["updated_at"] = clean_text(old.get("updated_at"), now)
            merged.append(entry)
        for old_id, old_entry in existing.items():
            if old_id not in new_ids:
                merged.append(old_entry)
        self.save_cache(merged)
        logger.info("[HelperTools] Anime1 cache updated: %s entries", len(merged))
        return len(merged)

    def filter_entries(
        self,
        entries: list[dict[str, Any]],
        *,
        time_range: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        normalized_range = clean_text(time_range).lower()
        range_aliases = {
            "年": "year",
            "year": "year",
            "月": "month",
            "month": "month",
            "周": "week",
            "week": "week",
            "日": "day",
            "天": "day",
            "day": "day",
            "today": "day",
            "": "",
            "all": "",
            "全部": "",
        }
        selected_range = range_aliases.get(normalized_range, normalized_range)
        now = datetime.now()
        if selected_range:
            filtered = []
            for item in entries:
                updated_at = clean_text(item.get("updated_at") or item.get("first_seen_at"))
                if not updated_at:
                    continue
                try:
                    updated = datetime.fromisoformat(updated_at)
                except ValueError:
                    continue
                if selected_range == "year" and updated.year == now.year:
                    filtered.append(item)
                elif selected_range == "month" and updated.year == now.year and updated.month == now.month:
                    filtered.append(item)
                elif selected_range == "week" and updated >= now - timedelta(days=7):
                    filtered.append(item)
                elif selected_range == "day" and updated.date() == now.date():
                    filtered.append(item)
            entries = filtered
        needle = clean_text(query).casefold()
        if needle:
            entries = [
                item
                for item in entries
                if needle in clean_text(item.get("title")).casefold()
                or needle == clean_text(item.get("id")).casefold()
            ]
        return entries

    async def get_updates(
        self,
        *,
        use_cache: bool = True,
        time_range: str = "",
        limit: int | None = None,
        query: str = "",
    ) -> str:
        if not self.enabled():
            return "Anime1 功能当前未启用。"
        if not use_cache:
            await self.update_cache()
        entries = self.load_cache()
        if not entries:
            return "暂无 Anime1 缓存数据，可以先执行更新或让工具 use_cache=false。"
        entries = self.filter_entries(entries, time_range=time_range, query=query)
        if not entries:
            return "没有找到符合条件的 Anime1 条目。"
        total = len(entries)
        actual_limit = self.default_limit() if limit is None or limit <= 0 else min(limit, 500)
        visible = entries[:actual_limit]
        lines = [f"Anime1 条目共 {total} 个，返回 {len(visible)} 个："]
        for item in visible:
            title = clean_text(item.get("title"), "未命名")
            status = clean_text(item.get("status"))
            season = clean_text(item.get("season"))
            year = clean_text(item.get("year"))
            extra = clean_text(item.get("extra"))
            suffix = " ".join(part for part in (status, year, season, extra) if part)
            lines.append(f"- [{item.get('id')}] {title}" + (f" - {suffix}" if suffix else ""))
        return truncate("\n".join(lines), 12000)

    async def get_watch_url(self, anime_id: Any) -> str:
        anime_id_text = clean_text(anime_id)
        if not anime_id_text.isdigit():
            return "Anime1 ID 必须是数字。"
        url = ANIME1_WATCH_URL.format(anime_id=anime_id_text)
        try:
            redirect_url = await asyncio.to_thread(_resolve_redirect_sync, url, self.timeout())
        except Exception as exc:
            logger.warning("[HelperTools] Anime1 redirect resolve failed: %s", exc)
            redirect_url = ""
        return f"Anime1 观看地址: {redirect_url or url}"
