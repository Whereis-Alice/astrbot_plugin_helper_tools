from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .helper_utils import cfg, clean_text, fetch_bytes, parse_dynamic_command, read_bool, read_int, read_list


VOICE_TOOL_NAME = "send_random_voice"


class VoiceService:
    def __init__(self, config: Any, data_dir: Path) -> None:
        self.config = config
        self.cache_dir = data_dir / "voice_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "voice", "enabled", True), True)

    def tool_enabled(self) -> bool:
        return read_bool(cfg(self.config, "voice", "llm_tool_enabled", True), True)

    def commands_enabled(self) -> bool:
        return read_bool(cfg(self.config, "voice", "commands_enabled", True), True)

    def api_url(self) -> str:
        return clean_text(cfg(self.config, "voice", "api_url", "http://api.ocoa.cn/api/hjm.php?type=audio"))

    def timeout(self) -> int:
        return read_int(cfg(self.config, "voice", "timeout_seconds", 15), 15, minimum=3, maximum=120)

    def max_bytes(self) -> int:
        return read_int(cfg(self.config, "voice", "max_download_bytes", 8 * 1024 * 1024), 8 * 1024 * 1024, minimum=64 * 1024, maximum=50 * 1024 * 1024)

    def command_prefixes(self) -> list[str]:
        return read_list(cfg(self.config, "voice", "command_prefixes", ["/voice_meme", "/随机语音"]), ["/voice_meme"])

    def trigger_keywords(self) -> list[str]:
        return read_list(cfg(self.config, "voice", "trigger_keywords", ["哈基米"]), ["哈基米"])

    def auto_trigger_enabled(self) -> bool:
        return read_bool(cfg(self.config, "voice", "auto_trigger_enabled", True), True)

    def stop_after_response(self) -> bool:
        return read_bool(cfg(self.config, "voice", "stop_event_after_response", True), True)

    def cache_max_files(self) -> int:
        return read_int(cfg(self.config, "voice", "cache_max_files", 30), 30, minimum=1, maximum=500)

    def should_handle_message(self, text: str) -> bool:
        if not self.enabled():
            return False
        if self.commands_enabled() and parse_dynamic_command(text, self.command_prefixes()):
            return True
        if not self.auto_trigger_enabled():
            return False
        return any(keyword and keyword in text for keyword in self.trigger_keywords())

    async def download_voice(self) -> Path:
        url = self.api_url()
        if not url:
            raise ValueError("语音 API URL 未配置。")
        data, content_type = await fetch_bytes(
            url,
            timeout_seconds=self.timeout(),
            max_bytes=self.max_bytes(),
        )
        suffix = ".mp3"
        if "wav" in content_type:
            suffix = ".wav"
        elif "ogg" in content_type:
            suffix = ".ogg"
        elif "mpeg" in content_type or "mp3" in content_type:
            suffix = ".mp3"
        path = self.cache_dir / f"voice_{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"
        path.write_bytes(data)
        self.cleanup_cache()
        return path

    def cleanup_cache(self) -> None:
        files = sorted(
            [item for item in self.cache_dir.glob("voice_*") if item.is_file()],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for item in files[self.cache_max_files() :]:
            try:
                item.unlink()
            except OSError:
                pass

    async def build_chain(self) -> list[Any]:
        path = await self.download_voice()
        return [Comp.Record.fromFileSystem(str(path))]

    async def send_to_event(self, event: Any) -> str:
        if not self.tool_enabled():
            return "随机语音 LLM 工具当前未启用。"
        if not self.enabled():
            return "随机语音功能当前未启用。"
        try:
            chain = await self.build_chain()
            await event.send(event.chain_result(chain))
        except Exception as exc:
            logger.warning("[HelperTools] send random voice failed: %s", exc)
            return f"随机语音发送失败: {exc}"
        return "随机语音已发送。"
