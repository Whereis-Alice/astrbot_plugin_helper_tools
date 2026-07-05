from __future__ import annotations

from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .helper_utils import cfg, clean_text, fetch_bytes, read_bool
from .qq_features import call_onebot, require_onebot


BOT_PROFILE_TOOL_NAME = "set_bot_qq_profile"

STATUS_MAPPING: dict[str, tuple[int, int]] = {
    "在线": (10, 0),
    "Q我吧": (10, 0),
    "离开": (30, 0),
    "忙碌": (50, 0),
    "请勿打扰": (70, 0),
    "隐身": (40, 0),
    "听歌中": (10, 1028),
    "春日限定": (10, 2037),
    "一起元梦": (10, 2025),
    "求星搭子": (10, 2026),
    "被掏空": (10, 2014),
    "今日天气": (10, 1030),
    "我crush了": (10, 2019),
    "爱你": (10, 2006),
    "好运锦鲤": (10, 1071),
    "元气满满": (10, 1058),
    "宝宝认证": (10, 1070),
    "一言难尽": (10, 1063),
    "emo中": (10, 1401),
    "我太难了": (10, 1062),
    "想静静": (10, 1061),
    "去旅行": (10, 2015),
    "信号弱": (10, 1011),
    "学习中": (10, 1018),
    "搬砖中": (10, 2023),
    "睡觉中": (10, 1016),
    "熬夜中": (10, 1032),
    "追剧中": (10, 1021),
    "我的电量": (10, 1000),
}


class BotProfileService:
    def __init__(self, config: Any, context: Any, data_dir: Path) -> None:
        self.config = config
        self.context = context
        self.avatar_dir = data_dir / "persona_avatars"
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        self._current_nickname = ""

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "bot_profile", "enabled", True), True)

    def tool_enabled(self) -> bool:
        return read_bool(cfg(self.config, "bot_profile", "llm_tool_enabled", False), False)

    def sync_name_enabled(self) -> bool:
        return read_bool(cfg(self.config, "bot_profile", "sync_name_with_persona", False), False)

    async def current_persona_id(self, event: Any) -> str:
        umo = clean_text(getattr(event, "unified_msg_origin", ""))
        manager = getattr(self.context, "conversation_manager", None)
        if manager and umo:
            try:
                conversation_id = await manager.get_curr_conversation_id(umo)
                if conversation_id:
                    conversation = await manager.get_conversation(
                        unified_msg_origin=umo,
                        conversation_id=conversation_id,
                        create_if_not_exists=True,
                    )
                    persona_id = clean_text(getattr(conversation, "persona_id", ""))
                    if persona_id and persona_id != "[%None]":
                        return persona_id
            except Exception:
                pass
        persona_manager = getattr(self.context, "persona_manager", None)
        selected = getattr(persona_manager, "selected_default_persona_v3", None)
        if isinstance(selected, dict):
            persona_id = clean_text(selected.get("name"))
            if persona_id and persona_id != "[%None]":
                return persona_id
        return ""

    def personas(self) -> list[dict[str, Any]]:
        provider_manager = getattr(self.context, "provider_manager", None)
        personas = getattr(provider_manager, "personas", [])
        return personas if isinstance(personas, list) else []

    async def set_nickname(self, event: Any, nickname: str) -> str:
        if not self.enabled():
            return "Bot QQ 资料功能当前未启用。"
        nickname = clean_text(nickname) or await self.current_persona_id(event)
        if not nickname:
            return "没有提供新昵称，也没有识别到当前人格名。"
        bot = require_onebot(event)
        await call_onebot(bot, "set_qq_profile", nickname=nickname)
        self._current_nickname = nickname
        return f"Bot 昵称已更新为: {nickname}"

    async def set_signature(self, event: Any, signature: str) -> str:
        if not self.enabled():
            return "Bot QQ 资料功能当前未启用。"
        signature = clean_text(signature)
        if not signature:
            return "没有提供新签名。"
        bot = require_onebot(event)
        await call_onebot(bot, "set_self_longnick", longNick=signature)
        return f"Bot 签名已更新: {signature}"

    async def set_status(self, event: Any, status_name: str) -> str:
        if not self.enabled():
            return "Bot QQ 资料功能当前未启用。"
        status_name = clean_text(status_name)
        if not status_name:
            return "没有提供状态名。"
        params = STATUS_MAPPING.get(status_name)
        if not params:
            return "暂不支持该状态。可用状态: " + "、".join(STATUS_MAPPING.keys())
        bot = require_onebot(event)
        await call_onebot(
            bot,
            "set_online_status",
            status=params[0],
            ext_status=params[1],
            battery_status=0,
        )
        return f"Bot 在线状态已更新为: {status_name}"

    def extract_image_url(self, event: Any) -> str:
        messages_getter = getattr(event, "get_messages", None)
        messages = messages_getter() if callable(messages_getter) else []
        for segment in messages or []:
            if isinstance(segment, Comp.Image):
                return clean_text(segment.url or segment.file)
            if isinstance(segment, Comp.Reply) and segment.chain:
                for reply_segment in segment.chain:
                    if isinstance(reply_segment, Comp.Image):
                        return clean_text(reply_segment.url or reply_segment.file)
        return ""

    async def set_avatar(self, event: Any, image_url: str = "") -> str:
        if not self.enabled():
            return "Bot QQ 资料功能当前未启用。"
        image_ref = clean_text(image_url) or self.extract_image_url(event)
        if not image_ref:
            return "请提供图片 URL，或引用/发送一张图片。"
        bot = require_onebot(event)
        await call_onebot(bot, "set_qq_avatar", file=image_ref)
        persona_id = await self.current_persona_id(event)
        if persona_id and image_ref.startswith(("http://", "https://")):
            try:
                data, _mime = await fetch_bytes(image_ref, timeout_seconds=20, max_bytes=10 * 1024 * 1024)
                (self.avatar_dir / f"{persona_id}.jpg").write_bytes(data)
            except Exception as exc:
                logger.warning("[HelperTools] failed to cache persona avatar: %s", exc)
        return "Bot 头像已更新。"

    async def switch_persona(self, event: Any, persona_id: str = "") -> str:
        persona_id = clean_text(persona_id) or await self.current_persona_id(event)
        if not persona_id:
            return "没有提供人格名，也没有识别到当前人格。"
        target = None
        for persona in self.personas():
            if clean_text(persona.get("name")) == persona_id:
                target = persona
                break
        if target is None:
            return f"人格不存在: {persona_id}"
        manager = getattr(self.context, "conversation_manager", None)
        if not manager:
            return "当前 AstrBot 没有可用的 conversation_manager。"
        await manager.update_conversation_persona_id(event.unified_msg_origin, persona_id)
        message = f"已切换人格: {persona_id}"
        if self.sync_name_enabled():
            sync_result = await self.sync_with_persona(event, persona_id)
            message += f"\n{sync_result}"
        return message

    async def sync_with_persona(self, event: Any, persona_id: str = "") -> str:
        persona_id = clean_text(persona_id) or await self.current_persona_id(event)
        if not persona_id:
            return "没有可同步的人格名。"
        result = await self.set_nickname(event, persona_id)
        avatar_path = self.avatar_dir / f"{persona_id}.jpg"
        if avatar_path.exists():
            try:
                await self.set_avatar(event, str(avatar_path))
                result += "\n已同步该人格缓存头像。"
            except Exception as exc:
                result += f"\n头像同步失败: {exc}"
        return result

    def list_personas(self) -> str:
        personas = self.personas()
        if not personas:
            return "没有读取到人格列表。"
        lines = ["人格列表："]
        for persona in personas:
            name = clean_text(persona.get("name"), "未命名")
            prompt = clean_text(persona.get("prompt"))
            lines.append(f"\n[{name}]")
            if prompt:
                lines.append(prompt)
        return "\n".join(lines)

    async def handle_tool(
        self,
        *,
        event: Any,
        action: str,
        value: str = "",
    ) -> str:
        if not self.tool_enabled():
            return "Bot QQ 资料 LLM 工具当前未启用。"
        is_admin = getattr(event, "is_admin", lambda: False)
        if not callable(is_admin) or not is_admin():
            return "只有管理员会话可以让 LLM 修改 bot QQ 资料。"
        action = clean_text(action).lower()
        if action in {"nickname", "set_nickname", "昵称"}:
            return await self.set_nickname(event, value)
        if action in {"signature", "longnick", "set_signature", "签名"}:
            return await self.set_signature(event, value)
        if action in {"status", "set_status", "状态"}:
            return await self.set_status(event, value)
        if action in {"avatar", "set_avatar", "头像"}:
            return await self.set_avatar(event, value)
        if action in {"sync_persona", "sync", "同步人格"}:
            return await self.sync_with_persona(event, value)
        return "不支持的 action。可用: nickname, signature, status, avatar, sync_persona。"
