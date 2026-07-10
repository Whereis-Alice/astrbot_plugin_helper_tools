from __future__ import annotations

import asyncio
import json
import random
import time
from inspect import isawaitable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .helper_utils import cfg, clean_text, read_bool, read_list
from .qq_features import call_onebot


DEFAULT_AVATAR_POOL_DIR = "avatar_pool"
DEFAULT_CRON_EXPRESSION = "0 8 * * *"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]
CRON_JOB_NAME = "astrbot_plugin_helper_tools:qq_avatar:auto_change"
STATE_FILENAME = "avatar_rotation_state.json"


class AvatarRotationService:
    def __init__(self, config: Any, data_dir: Path, context: Any) -> None:
        self.config = config
        self.data_dir = data_dir
        self.context = context
        self.state_path = self.data_dir / STATE_FILENAME
        self._cron_job_id = ""
        self._lock = asyncio.Lock()

    def _config(self) -> dict[str, Any]:
        value = cfg(self.config, "qq_avatar", "auto_change", {})
        return value if isinstance(value, dict) else {}

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "qq_avatar", "enabled", True), True) and read_bool(
            self._config().get("enabled"),
            False,
        )

    def manual_command_enabled(self) -> bool:
        return read_bool(self._config().get("manual_command_enabled"), True)

    def run_on_start(self) -> bool:
        return read_bool(self._config().get("run_on_start"), False)

    def recursive(self) -> bool:
        return read_bool(self._config().get("recursive"), False)

    def avoid_repeat(self) -> bool:
        return read_bool(self._config().get("avoid_repeat"), True)

    def cron_expression(self) -> str:
        return clean_text(self._config().get("cron_expression"), DEFAULT_CRON_EXPRESSION)

    def timezone(self) -> str:
        return clean_text(self._config().get("timezone"), DEFAULT_TIMEZONE)

    def platform_id(self) -> str:
        return clean_text(self._config().get("platform_id"))

    def image_dir(self) -> Path:
        raw_path = clean_text(self._config().get("image_dir"))
        path = Path(raw_path).expanduser() if raw_path else self.data_dir / DEFAULT_AVATAR_POOL_DIR
        if not path.is_absolute():
            path = self.data_dir / path
        return path.resolve(strict=False)

    def allowed_extensions(self) -> set[str]:
        values = read_list(self._config().get("allowed_extensions"), DEFAULT_EXTENSIONS)
        result: set[str] = set()
        for item in values:
            ext = clean_text(item).lower()
            if not ext:
                continue
            result.add(ext if ext.startswith(".") else f".{ext}")
        return result or set(DEFAULT_EXTENSIONS)

    async def start(self) -> None:
        if not self.enabled():
            return
        self.image_dir().mkdir(parents=True, exist_ok=True)
        await self._register_cron_job()
        if self.run_on_start():
            asyncio.create_task(self._run_scheduled_rotation(reason="startup"))

    async def stop(self) -> None:
        cron_mgr = getattr(self.context, "cron_manager", None)
        if cron_mgr is None or not self._cron_job_id:
            self._cron_job_id = ""
            return
        try:
            await self._maybe_await(cron_mgr.delete_job(self._cron_job_id))
        except Exception as exc:
            logger.warning("[HelperTools] delete avatar rotation cron job failed: %r", exc)
        finally:
            self._cron_job_id = ""

    async def change_once(self, event: Any | None = None, *, reason: str = "manual") -> str:
        if not self.enabled():
            return "QQ 头像自动更换未启用。"
        async with self._lock:
            images = self.image_files()
            if not images:
                return f"头像池里没有可用图片：{self.image_dir()}"
            image_path = self.pick_image(images)
            bot = self._resolve_bot(event)
            if bot is None:
                return "没有找到可用的 OneBot/AIOCQHTTP bot，无法设置 QQ 头像。"
            await call_onebot(bot, "set_qq_avatar", file=str(image_path))
            self.save_state(
                {
                    "last_path": str(image_path),
                    "last_changed_at": int(time.time()),
                    "reason": reason,
                }
            )
            return f"已随机更换 bot QQ 头像：{image_path.name}"

    async def _run_scheduled_rotation(self, reason: str = "schedule") -> None:
        try:
            result = await self.change_once(reason=reason)
            logger.info("[HelperTools] avatar rotation result: %s", result)
        except Exception as exc:
            logger.error("[HelperTools] avatar rotation failed: %r", exc, exc_info=True)
            raise

    async def _register_cron_job(self) -> None:
        cron_mgr = getattr(self.context, "cron_manager", None)
        if cron_mgr is None:
            logger.warning("[HelperTools] cron_manager unavailable; avatar rotation was not scheduled")
            return
        await self._delete_existing_jobs_by_name(cron_mgr, CRON_JOB_NAME)
        try:
            job = await self._maybe_await(
                cron_mgr.add_basic_job(
                    name=CRON_JOB_NAME,
                    cron_expression=self.cron_expression(),
                    timezone=self.timezone(),
                    handler=self._run_scheduled_rotation,
                    payload={"reason": "schedule"},
                    persistent=False,
                    enabled=True,
                    description="Randomly change bot QQ avatar from a local image pool.",
                )
            )
        except Exception as exc:
            logger.error("[HelperTools] register avatar rotation cron job failed: %r", exc, exc_info=True)
            return
        self._cron_job_id = clean_text(self._job_value(job, "job_id", "id"))
        logger.info(
            "[HelperTools] avatar rotation scheduled cron=%s timezone=%s job=%s",
            self.cron_expression(),
            self.timezone(),
            self._cron_job_id,
        )

    async def _delete_existing_jobs_by_name(self, cron_mgr: Any, name: str) -> None:
        try:
            try:
                jobs = await self._maybe_await(cron_mgr.list_jobs("basic"))
            except TypeError:
                jobs = await self._maybe_await(cron_mgr.list_jobs())
        except Exception as exc:
            logger.warning("[HelperTools] list cron jobs failed before avatar rotation register: %r", exc)
            return
        for job in jobs or []:
            if self._job_value(job, "name") != name:
                continue
            job_id = clean_text(self._job_value(job, "job_id", "id"))
            if not job_id:
                continue
            try:
                await self._maybe_await(cron_mgr.delete_job(job_id))
            except Exception as exc:
                logger.warning("[HelperTools] delete stale avatar rotation cron job failed: %r", exc)

    def image_files(self) -> list[Path]:
        root = self.image_dir()
        if not root.exists() or not root.is_dir():
            return []
        iterator = root.rglob("*") if self.recursive() else root.iterdir()
        allowed = self.allowed_extensions()
        return sorted(path.resolve(strict=False) for path in iterator if path.is_file() and path.suffix.lower() in allowed)

    def pick_image(self, images: list[Path]) -> Path:
        candidates = list(images)
        state = self.load_state()
        last_path = clean_text(state.get("last_path"))
        if self.avoid_repeat() and last_path and len(candidates) > 1:
            candidates = [path for path in candidates if str(path) != last_path] or candidates
        return random.choice(candidates)

    def load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_state(self, payload: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _resolve_bot(self, event: Any | None = None) -> Any | None:
        if event is not None:
            bot = getattr(event, "bot", None)
            if bot is not None:
                return bot
        manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(manager, "platform_insts", []) if manager is not None else []
        configured_platform_id = self.platform_id()
        for platform in platforms or []:
            try:
                meta = platform.meta()
            except Exception:
                continue
            name = clean_text(getattr(meta, "name", ""))
            platform_id = clean_text(getattr(meta, "id", ""))
            if configured_platform_id:
                if configured_platform_id not in {platform_id, name}:
                    continue
            elif name != "aiocqhttp":
                continue
            bot = getattr(platform, "bot", None)
            if bot is None:
                getter = getattr(platform, "get_client", None)
                bot = getter() if callable(getter) else None
            if bot is not None:
                return bot
        return None

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if isawaitable(value):
            return await value
        return value

    @staticmethod
    def _job_value(job: Any, *names: str) -> Any:
        if isinstance(job, dict):
            for name in names:
                if name in job:
                    return job[name]
            return None
        for name in names:
            value = getattr(job, name, None)
            if value is not None:
                return value
        return None
