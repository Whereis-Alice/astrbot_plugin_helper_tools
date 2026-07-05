from __future__ import annotations

from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, extract_file_config_value, read_bool, resolve_existing_path


PAYQR_TOOL_NAME = "send_payment_qr"


class PayQRService:
    def __init__(self, config: Any, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "payqr", "enabled", True), True)

    def tool_enabled(self) -> bool:
        return read_bool(cfg(self.config, "payqr", "llm_tool_enabled", True), True)

    def caption(self) -> str:
        return clean_text(cfg(self.config, "payqr", "caption", "给我打钱！"), "给我打钱！")

    def qr_path(self) -> Path | None:
        raw = extract_file_config_value(cfg(self.config, "payqr", "payment_qr", []))
        return resolve_existing_path(
            raw,
            self.data_dir,
            self.data_dir / "files" / "payment_qr",
            self.data_dir / "files" / "payqr.payment_qr",
            self.data_dir / "files" / "payqr" / "payment_qr",
        )

    def build_chain(self) -> tuple[list[Any] | None, str]:
        if not self.enabled():
            return None, "收款码功能当前未启用。"
        path = self.qr_path()
        if path is None:
            return None, "还没有配置可用的收款码图片。"
        chain: list[Any] = []
        caption = self.caption()
        if caption:
            chain.append(Comp.Plain(caption))
        chain.append(Comp.Image.fromFileSystem(str(path)))
        return chain, ""

    async def send_to_event(self, event: Any) -> str:
        if not self.tool_enabled():
            return "收款码 LLM 工具当前未启用。"
        chain, error = self.build_chain()
        if error:
            return error
        assert chain is not None
        await event.send(event.chain_result(chain))
        return "收款码已发送。"
