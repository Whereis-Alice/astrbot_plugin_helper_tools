from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from .anime1_service import Anime1Service
from .bot_profile_service import BOT_PROFILE_TOOL_NAME, BotProfileService
from .helper_utils import cfg, clean_text, read_bool
from .payqr_service import PAYQR_TOOL_NAME, PayQRService
from .qq_features import (
    ALLOWED_AVATAR_SIZES,
    DEFAULT_AVATAR_SIZE,
    QQ_AVATAR_TOOL_NAME,
    QQ_GROUP_MEMBER_TOOL_NAME,
    QQ_PROFILE_TOOL_NAME,
    QQService,
    build_qq_avatar_url,
    normalize_avatar_size,
)
from .steam_service import STEAM_TOOL_NAME, SteamService
from .voice_service import VOICE_TOOL_NAME, VoiceService
from .wake_service import WakeService
from .wallpaper_service import WallpaperService


PLUGIN_ID = "astrbot_plugin_helper_tools"
PLUGIN_VERSION = "0.4.5"
PLUGIN_DESC = "辅助工具合集：为 AstrBot 注册 QQ、Anime1、收款码、随机语音、Steam、唤醒增强、壁纸图库等工具。"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_helper_tools"

ToolResult = str | CallToolResult


def _tool_event(context: ContextWrapper[AstrAgentContext]) -> Any:
    return getattr(context.context, "event", None)


def _missing_event() -> str:
    return "当前工具需要在一次消息会话中调用，但没有读取到事件上下文。"


def _bool_arg(value: Any, default: bool) -> bool:
    return read_bool(value, default)


def _module_enabled(config: Any, module: str, default: bool = True) -> bool:
    return read_bool(cfg(config, module, "enabled", default), default)


def _module_commands_enabled(config: Any, module: str, default: bool = True) -> bool:
    return _module_enabled(config, module, default) and read_bool(
        cfg(config, module, "commands_enabled", default),
        default,
    )


@pydantic_dataclass
class QQAvatarTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = QQ_AVATAR_TOOL_NAME
    description: str = "获取 QQ 用户头像；可在模型支持图片输入时把头像图片内容一并返回。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "qq_id": {
                    "type": "string",
                    "description": "目标 QQ 号；留空时尝试使用当前消息发送者或被 @ 用户。",
                },
                "size": {
                    "type": "string",
                    "description": "头像尺寸。",
                    "enum": list(ALLOWED_AVATAR_SIZES),
                    "default": DEFAULT_AVATAR_SIZE,
                },
                "return_image": {
                    "type": "boolean",
                    "description": "是否返回图片内容给模型查看。",
                    "default": True,
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> ToolResult:
        if self.plugin is None:
            return "QQ 头像工具未绑定插件实例。"
        return await self.plugin.qq.get_avatar_result(
            event=_tool_event(context),
            qq_id=clean_text(kwargs.get("qq_id")),
            size=clean_text(kwargs.get("size")),
            return_image=_bool_arg(kwargs.get("return_image"), True),
        )


@pydantic_dataclass
class QQGroupMemberTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = QQ_GROUP_MEMBER_TOOL_NAME
    description: str = "获取 QQ 群成员信息，包括 QQ号、QQ名、群昵称、群身份、群等级、群专属头衔，以及 OneBot 可提供的其它字段。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "qq_id": {
                    "type": "string",
                    "description": "目标 QQ 号；留空时尝试使用当前消息发送者或被 @ 用户。",
                },
                "group_id": {
                    "type": "string",
                    "description": "群号；留空时使用当前群聊。",
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "QQ群成员信息工具未绑定插件实例。"
        return await self.plugin.qq.get_group_member_result(
            event=_tool_event(context),
            qq_id=clean_text(kwargs.get("qq_id")),
            group_id=clean_text(kwargs.get("group_id")),
        )


@pydantic_dataclass
class QQProfileTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = QQ_PROFILE_TOOL_NAME
    description: str = "查询 QQ 用户资料和当前群资料，整合头像、QQ名、签名、群名片、群身份、等级等公开/OneBot 可用信息。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "qq_id": {
                    "type": "string",
                    "description": "目标 QQ 号；留空时尝试使用当前消息发送者或被 @ 用户。",
                },
                "group_id": {
                    "type": "string",
                    "description": "群号；留空时使用当前群聊。",
                },
                "include_avatar": {
                    "type": "boolean",
                    "description": "是否附带头像 URL 或图片内容。",
                    "default": True,
                },
                "return_image": {
                    "type": "boolean",
                    "description": "是否返回头像图片内容给模型查看。",
                    "default": True,
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> ToolResult:
        if self.plugin is None:
            return "QQ 资料工具未绑定插件实例。"
        return await self.plugin.qq.get_profile_result(
            event=_tool_event(context),
            qq_id=clean_text(kwargs.get("qq_id")),
            group_id=clean_text(kwargs.get("group_id")),
            include_avatar=_bool_arg(kwargs.get("include_avatar"), True),
            return_image=_bool_arg(kwargs.get("return_image"), True),
        )


@pydantic_dataclass
class PaymentQRTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = PAYQR_TOOL_NAME
    description: str = "当对话涉及没钱、打钱、转账、赞助、请客、收款等场景时，发送已配置的收款码图片。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "收款码工具未绑定插件实例。"
        event = _tool_event(context)
        if event is None:
            return _missing_event()
        return await self.plugin.payqr.send_to_event(event)


@pydantic_dataclass
class Anime1UpdatesTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "get_anime1_updates"
    description: str = "获取 Anime1 番剧剧集更新列表，支持缓存、时间范围、关键词和数量限制。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "use_cache": {
                    "type": "boolean",
                    "description": "是否优先使用本地缓存；false 会立即刷新远端列表。",
                    "default": True,
                },
                "time_range": {
                    "type": "string",
                    "description": "时间范围：年、月、周、日、全部，也可留空。",
                    "default": "",
                },
                "query": {
                    "type": "string",
                    "description": "按番剧标题或 Anime1 ID 过滤。",
                },
                "limit": {
                    "type": "number",
                    "description": "返回数量限制；小于等于 0 时使用配置默认值。",
                    "default": 20,
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "Anime1 工具未绑定插件实例。"
        limit = kwargs.get("limit")
        try:
            parsed_limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            parsed_limit = None
        return await self.plugin.anime1.get_updates(
            use_cache=_bool_arg(kwargs.get("use_cache"), True),
            time_range=clean_text(kwargs.get("time_range")),
            query=clean_text(kwargs.get("query")),
            limit=parsed_limit,
        )


@pydantic_dataclass
class Anime1WatchURLTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "get_anime1_watch_url"
    description: str = "根据 Anime1 条目 ID 获取观看地址。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "anime_id": {
                    "type": "string",
                    "description": "Anime1 条目 ID。",
                },
            },
            "required": ["anime_id"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "Anime1 观看地址工具未绑定插件实例。"
        return await self.plugin.anime1.get_watch_url(kwargs.get("anime_id"))


@pydantic_dataclass
class RandomVoiceTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = VOICE_TOOL_NAME
    description: str = "发送一条配置好的随机语音；默认可用于哈基米语音，也可在配置中换成其它随机语音 API。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "触发发送的简短原因，可留空。",
                },
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "随机语音工具未绑定插件实例。"
        event = _tool_event(context)
        if event is None:
            return _missing_event()
        return await self.plugin.voice.send_to_event(event)


@pydantic_dataclass
class SteamSearchTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = STEAM_TOOL_NAME
    description: str = "查询 Steam 游戏信息，支持 AppID、商店链接或关键词搜索，可返回封面图给模型查看。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Steam AppID、商店链接或游戏关键词。",
                },
                "return_image": {
                    "type": "boolean",
                    "description": "是否返回 Steam 封面图片内容给模型查看。",
                    "default": False,
                },
            },
            "required": ["query"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> ToolResult:
        if self.plugin is None:
            return "Steam 工具未绑定插件实例。"
        return await self.plugin.steam.query_game(
            query=clean_text(kwargs.get("query")),
            return_image=_bool_arg(kwargs.get("return_image"), False),
        )


@pydantic_dataclass
class BotQQProfileTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = BOT_PROFILE_TOOL_NAME
    description: str = "管理员会话可用：修改 bot 的 QQ 昵称、签名、状态、头像，或同步当前人格。默认关闭，需要在配置中显式启用。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作：nickname、signature、status、avatar、sync_persona。",
                },
                "value": {
                    "type": "string",
                    "description": "操作值，如昵称、签名、状态名、头像 URL 或人格名。",
                },
            },
            "required": ["action"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "Bot QQ 资料工具未绑定插件实例。"
        event = _tool_event(context)
        if event is None:
            return _missing_event()
        return await self.plugin.bot_profile.handle_tool(
            event=event,
            action=clean_text(kwargs.get("action")),
            value=clean_text(kwargs.get("value")),
        )


@register(PLUGIN_ID, "Huli3", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class HelperToolsPlugin(Star):
    """LLM-callable helper tools for AstrBot."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_ID)

        self.qq = QQService(self.config)
        self.anime1 = Anime1Service(self.config, self.data_dir)
        self.payqr = PayQRService(self.config, self.data_dir)
        self.voice = VoiceService(self.config, self.data_dir, self.context)
        self.steam = SteamService(self.config, self.context)
        self.bot_profile = BotProfileService(self.config, self.context, self.data_dir)
        self.wake = WakeService(self.config, self.context)
        self.wallpaper = WallpaperService(self.config, self.data_dir, self.context)

        self.context.add_llm_tools(*self._build_tools())

    async def initialize(self) -> None:
        await self.anime1.start()
        logger.info("[%s] initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        await self.anime1.stop()
        await self.wake.stop()
        logger.info("[%s] terminated", PLUGIN_ID)

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "general", "enabled", True), True)

    def _tool_active(self, module: str, default: bool = True) -> bool:
        return self.enabled() and _module_enabled(self.config, module, default) and read_bool(
            cfg(self.config, module, "llm_tool_enabled", default),
            default,
        )

    def _build_tools(self) -> list[FunctionTool[AstrAgentContext]]:
        return [
            QQAvatarTool(plugin=self, active=self._tool_active("qq_avatar")),
            QQGroupMemberTool(plugin=self, active=self._tool_active("qq_member")),
            QQProfileTool(plugin=self, active=self._tool_active("qq_profile")),
            PaymentQRTool(plugin=self, active=self._tool_active("payqr")),
            Anime1UpdatesTool(plugin=self, active=self._tool_active("anime1")),
            Anime1WatchURLTool(plugin=self, active=self._tool_active("anime1")),
            RandomVoiceTool(plugin=self, active=self._tool_active("voice")),
            SteamSearchTool(plugin=self, active=self._tool_active("steam")),
            BotQQProfileTool(plugin=self, active=self._tool_active("bot_profile", False)),
        ]

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99998)
    async def wake_enhance_handler(self, event: AstrMessageEvent):
        if not self.enabled():
            return
        await self.wake.apply(event)

    @filter.on_decorating_result(priority=20)
    async def wake_after_result(self, event: AstrMessageEvent):
        if not self.enabled():
            return
        await self.wake.on_decorating_result(event)

    @filter.command("qq_avatar", alias={"qq头像", "头像"})
    async def qq_avatar_command(
        self,
        event: AstrMessageEvent,
        qq_id: str | None = None,
        size: str | None = None,
    ):
        if not self.enabled() or not _module_commands_enabled(self.config, "qq_avatar"):
            yield event.plain_result("QQ 头像命令当前未启用。")
            return
        requested_qq_id = clean_text(qq_id)
        requested_size = clean_text(size)
        if requested_qq_id in ALLOWED_AVATAR_SIZES and not requested_size:
            requested_size = requested_qq_id
            requested_qq_id = ""
        resolved_qq_id, error = self.qq.resolve_qq_id(event, requested_qq_id)
        if error:
            yield event.plain_result(error)
            return
        assert resolved_qq_id is not None
        avatar_size = normalize_avatar_size(requested_size, self.qq.avatar_default_size())
        yield event.chain_result(self.qq.command_avatar_chain(resolved_qq_id, avatar_size))

    @filter.command("qq_member", alias={"群成员信息", "qq成员"})
    async def qq_member_command(
        self,
        event: AstrMessageEvent,
        qq_id: str | None = None,
        group_id: str | None = None,
    ):
        if not self.enabled() or not _module_commands_enabled(self.config, "qq_member"):
            yield event.plain_result("QQ群成员信息命令当前未启用。")
            return
        result = await self.qq.get_group_member_result(
            event=event,
            qq_id=clean_text(qq_id),
            group_id=clean_text(group_id),
        )
        yield event.plain_result(result)

    @filter.command("qq_profile", alias={"qq资料", "box", "盒", "开盒"})
    async def qq_profile_command(
        self,
        event: AstrMessageEvent,
        qq_id: str | None = None,
        group_id: str | None = None,
    ):
        if not self.enabled() or not _module_commands_enabled(self.config, "qq_profile"):
            yield event.plain_result("QQ 资料命令当前未启用。")
            return
        resolved_qq_id, error = self.qq.resolve_qq_id(event, clean_text(qq_id))
        if error:
            yield event.plain_result(error)
            return
        assert resolved_qq_id is not None
        result = await self.qq.get_profile_result(
            event=event,
            qq_id=resolved_qq_id,
            group_id=clean_text(group_id),
            include_avatar=False,
            return_image=False,
        )
        if not isinstance(result, str):
            yield event.plain_result("QQ 资料结果格式异常。")
            return
        chain: list[Any] = []
        if read_bool(cfg(self.config, "qq_profile", "send_avatar_in_command", True), True):
            chain.append(Comp.Image.fromURL(build_qq_avatar_url(resolved_qq_id, self.qq.avatar_default_size())))
        chain.append(Comp.Plain(result))
        yield event.chain_result(chain)

    @filter.command("payqr", alias={"收款码", "打钱"})
    async def payqr_command(self, event: AstrMessageEvent):
        if not self.enabled() or not _module_commands_enabled(self.config, "payqr"):
            yield event.plain_result("收款码命令当前未启用。")
            return
        chain, error = self.payqr.build_chain()
        if error:
            yield event.plain_result(error)
            return
        assert chain is not None
        yield event.chain_result(chain)

    @filter.command("anime1_update", alias={"anime_update", "更新anime1"})
    async def anime1_update_command(self, event: AstrMessageEvent):
        if not self.enabled() or not _module_commands_enabled(self.config, "anime1"):
            yield event.plain_result("Anime1 命令当前未启用。")
            return
        try:
            count = await self.anime1.update_cache()
        except Exception as exc:
            yield event.plain_result(f"Anime1 更新失败: {exc}")
            return
        yield event.plain_result(f"Anime1 缓存已更新，共 {count} 条。")

    @filter.command("anime1", alias={"番剧更新"})
    async def anime1_command(
        self,
        event: AstrMessageEvent,
        arg1: str | None = None,
        arg2: str | None = None,
        arg3: str | None = None,
    ):
        if not self.enabled() or not _module_commands_enabled(self.config, "anime1"):
            yield event.plain_result("Anime1 命令当前未启用。")
            return
        query, time_range, limit = self._parse_anime_args(arg1, arg2, arg3)
        result = await self.anime1.get_updates(
            use_cache=True,
            query=query,
            time_range=time_range,
            limit=limit,
        )
        yield event.plain_result(result)

    @filter.command("anime1_url", alias={"番剧链接"})
    async def anime1_url_command(self, event: AstrMessageEvent, anime_id: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "anime1"):
            yield event.plain_result("Anime1 命令当前未启用。")
            return
        yield event.plain_result(await self.anime1.get_watch_url(anime_id))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置头像")
    async def set_bot_avatar_command(self, event: AstrMessageEvent, image_url: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(await self.bot_profile.set_avatar(event, clean_text(image_url)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置昵称")
    async def set_bot_nickname_command(self, event: AstrMessageEvent, nickname: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(await self.bot_profile.set_nickname(event, clean_text(nickname)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置签名")
    async def set_bot_signature_command(self, event: AstrMessageEvent, signature: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(await self.bot_profile.set_signature(event, clean_text(signature)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置状态")
    async def set_bot_status_command(self, event: AstrMessageEvent, status_name: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(await self.bot_profile.set_status(event, clean_text(status_name)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换人格")
    async def switch_persona_command(self, event: AstrMessageEvent, persona_id: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(await self.bot_profile.switch_persona(event, clean_text(persona_id)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同步人格")
    async def sync_persona_command(self, event: AstrMessageEvent, persona_id: str | None = None):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(await self.bot_profile.sync_with_persona(event, clean_text(persona_id)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("人格列表", alias={"查看人格列表"})
    async def list_persona_command(self, event: AstrMessageEvent):
        if not self.enabled() or not _module_commands_enabled(self.config, "bot_profile"):
            yield event.plain_result("Bot QQ 资料命令当前未启用。")
            return
        yield event.plain_result(self.bot_profile.list_personas())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def dynamic_message_handler(self, event: AstrMessageEvent):
        if not self.enabled():
            return
        text = clean_text(getattr(event, "message_str", ""))
        if not text:
            return

        wallpaper_result = await self.wallpaper.handle_message(event, text)
        if wallpaper_result.handled:
            if wallpaper_result.message:
                yield event.plain_result(wallpaper_result.message)
            if self.wallpaper.stop_after_response():
                event.stop_event()
            return

        steam_handled, steam_query = self.steam.should_handle_message(text)
        if steam_handled:
            try:
                chain, error = await self.steam.build_chain_for_message(steam_query)
            except Exception as exc:
                yield event.plain_result(f"Steam 查询失败: {exc}")
                return
            if error:
                yield event.plain_result(error)
                return
            assert chain is not None
            yield event.chain_result(chain)
            if self.steam.stop_after_response():
                event.stop_event()
            return

        if self.voice.should_handle_message(text):
            try:
                chain = await self.voice.build_chain()
            except Exception as exc:
                yield event.plain_result(f"随机语音发送失败: {exc}")
                return
            yield event.chain_result(chain)
            if self.voice.stop_after_response():
                event.stop_event()

    def _parse_anime_args(self, *args: str | None) -> tuple[str, str, int | None]:
        query_parts: list[str] = []
        time_range = ""
        limit: int | None = None
        range_tokens = {"年", "月", "周", "日", "天", "day", "week", "month", "year", "today", "全部", "all"}
        for raw in args:
            item = clean_text(raw)
            if not item:
                continue
            lowered = item.lower()
            if not time_range and (item in range_tokens or lowered in range_tokens):
                time_range = item
                continue
            if limit is None:
                try:
                    limit = int(item)
                    continue
                except ValueError:
                    pass
            query_parts.append(item)
        return " ".join(query_parts), time_range, limit
