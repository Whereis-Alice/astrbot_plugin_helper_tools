from __future__ import annotations

import asyncio
import base64
import re
import urllib.error
import urllib.request
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


PLUGIN_ID = "astrbot_plugin_helper_tools"
PLUGIN_VERSION = "0.1.0"
PLUGIN_DESC = "辅助工具合集：为 LLM 注册可主动调用的小工具，当前支持查看 QQ 用户头像。"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_helper_tools"

QQ_AVATAR_TOOL_NAME = "get_qq_avatar"
ALLOWED_AVATAR_SIZES = ("40", "100", "140", "640")
DEFAULT_AVATAR_SIZE = "640"
DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 8
QQ_ID_PATTERN = re.compile(r"^(?:qq\s*[:=]?\s*)?(\d{5,12})$", re.IGNORECASE)

DEFAULT_QQ_AVATAR_TOOL_DESCRIPTION = (
    "获取 QQ 用户头像，并在可能时把头像图片内容返回给模型查看。"
    "当用户要求查看某个 QQ 号的头像、让你描述头像、比较头像，或需要当前发言者头像时使用。"
    "参数 qq_id 是 QQ 号；如果用户没有给 QQ 号，可以留空，工具会尝试使用当前消息发送者的 QQ 号。"
)


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def normalize_avatar_size(value: Any, default: str = DEFAULT_AVATAR_SIZE) -> str:
    size = _clean_text(value, default)
    return size if size in ALLOWED_AVATAR_SIZES else default


def normalize_qq_id(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    text = text.strip("@ \t\r\n")
    match = QQ_ID_PATTERN.fullmatch(text)
    return match.group(1) if match else None


def build_qq_avatar_url(qq_id: str, size: str = DEFAULT_AVATAR_SIZE) -> str:
    safe_size = normalize_avatar_size(size)
    return f"https://q.qlogo.cn/headimg_dl?dst_uin={qq_id}&spec={safe_size}&img_type=jpg"


def _read_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    if value is None:
        return default
    return bool(value)


def _read_int(
    value: Any,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return min(maximum, max(minimum, result))


def _download_image_sync(
    url: str,
    *,
    timeout_seconds: int,
    max_bytes: int,
) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; AstrBot HelperTools; "
                "+https://github.com/AstrBotDevs/AstrBot)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content_type = response.headers.get_content_type() or "image/jpeg"
        if not content_type.startswith("image/"):
            raise ValueError(f"unexpected content type: {content_type}")

        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"image is larger than {max_bytes} bytes")
        if not data:
            raise ValueError("empty image response")
        return data, content_type


@pydantic_dataclass
class QQAvatarTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = QQ_AVATAR_TOOL_NAME
    description: str = DEFAULT_QQ_AVATAR_TOOL_DESCRIPTION
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "qq_id": {
                    "type": "string",
                    "description": "要查看头像的 QQ 号。留空时尝试使用当前消息发送者的 QQ 号。",
                },
                "size": {
                    "type": "string",
                    "description": "头像尺寸。",
                    "enum": list(ALLOWED_AVATAR_SIZES),
                    "default": DEFAULT_AVATAR_SIZE,
                },
                "return_image": {
                    "type": "boolean",
                    "description": "是否把头像图片内容一并返回给模型查看。",
                    "default": True,
                },
            },
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> str | CallToolResult:
        if self.plugin is None:
            return "QQ 头像工具未绑定插件实例，请重载插件。"

        event = getattr(context.context, "event", None)
        return await self.plugin.handle_get_qq_avatar(
            event=event,
            qq_id=_clean_text(kwargs.get("qq_id")),
            size=_clean_text(kwargs.get("size"), DEFAULT_AVATAR_SIZE),
            return_image=_read_bool(kwargs.get("return_image"), True),
        )


@register(PLUGIN_ID, "Huli3", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class HelperToolsPlugin(Star):
    """A small collection of LLM-callable helper tools."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.context.add_llm_tools(
            QQAvatarTool(
                plugin=self,
                description=self._tool_description(),
                active=self._plugin_enabled() and self._llm_tool_enabled(),
            )
        )

    async def initialize(self) -> None:
        logger.info("[%s] initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        logger.info("[%s] terminated", PLUGIN_ID)

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _section(self, key: str) -> dict[str, Any]:
        value = self._cfg(key, {})
        return value if isinstance(value, dict) else {}

    def _plugin_enabled(self) -> bool:
        return _read_bool(self._section("general").get("enabled"), True)

    def _llm_tool_enabled(self) -> bool:
        return _read_bool(self._section("qq_avatar").get("llm_tool_enabled"), True)

    def _commands_enabled(self) -> bool:
        return _read_bool(self._section("qq_avatar").get("commands_enabled"), True)

    def _download_image_for_llm(self) -> bool:
        return _read_bool(self._section("qq_avatar").get("download_image_for_llm"), True)

    def _tool_description(self) -> str:
        return _clean_text(
            self._section("qq_avatar").get("tool_description"),
            DEFAULT_QQ_AVATAR_TOOL_DESCRIPTION,
        )

    def _default_avatar_size(self) -> str:
        return normalize_avatar_size(
            self._section("qq_avatar").get("default_size"),
            DEFAULT_AVATAR_SIZE,
        )

    def _download_timeout_seconds(self) -> int:
        return _read_int(
            self._section("qq_avatar").get("download_timeout_seconds"),
            DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
            minimum=1,
            maximum=60,
        )

    def _max_download_bytes(self) -> int:
        return _read_int(
            self._section("qq_avatar").get("max_download_bytes"),
            DEFAULT_MAX_DOWNLOAD_BYTES,
            minimum=64 * 1024,
            maximum=10 * 1024 * 1024,
        )

    def _resolve_qq_id(
        self,
        *,
        event: AstrMessageEvent | None,
        qq_id: str,
    ) -> tuple[str | None, str]:
        normalized = normalize_qq_id(qq_id)
        if normalized:
            return normalized, ""
        if qq_id:
            return None, "QQ 号格式不正确。请提供 5 到 12 位数字 QQ 号。"

        if event is None:
            return None, "没有提供 QQ 号，也无法从当前消息事件读取发送者 QQ 号。"

        sender_id = normalize_qq_id(getattr(event, "get_sender_id", lambda: "")())
        if sender_id:
            return sender_id, ""

        return None, "没有提供 QQ 号，且当前平台发送者 ID 不是可识别的 QQ 号。"

    def _format_avatar_result_text(
        self,
        *,
        qq_id: str,
        size: str,
        url: str,
        image_attached: bool,
    ) -> str:
        lines = [
            "已获取 QQ 用户头像。",
            f"QQ 号: {qq_id}",
            f"尺寸: {size}",
            f"头像 URL: {url}",
        ]
        if image_attached:
            lines.append("图片内容已随工具结果返回，请直接根据头像画面回答用户。")
        else:
            lines.append("当前仅返回头像 URL；如果模型支持读取图片 URL，可以打开该 URL 查看。")
        return "\n".join(lines)

    async def _download_avatar_image(self, url: str) -> tuple[bytes, str]:
        return await asyncio.to_thread(
            _download_image_sync,
            url,
            timeout_seconds=self._download_timeout_seconds(),
            max_bytes=self._max_download_bytes(),
        )

    async def handle_get_qq_avatar(
        self,
        *,
        event: AstrMessageEvent | None,
        qq_id: str = "",
        size: str = DEFAULT_AVATAR_SIZE,
        return_image: bool = True,
    ) -> str | CallToolResult:
        if not self._plugin_enabled():
            return "辅助工具合集插件当前未启用。"
        if not self._llm_tool_enabled():
            return "QQ 头像 LLM 工具当前未启用。"

        resolved_qq_id, error = self._resolve_qq_id(event=event, qq_id=qq_id)
        if error:
            return error
        assert resolved_qq_id is not None

        avatar_size = normalize_avatar_size(size, self._default_avatar_size())
        url = build_qq_avatar_url(resolved_qq_id, avatar_size)

        should_attach_image = return_image and self._download_image_for_llm()
        if not should_attach_image:
            return self._format_avatar_result_text(
                qq_id=resolved_qq_id,
                size=avatar_size,
                url=url,
                image_attached=False,
            )

        try:
            image_data, mime_type = await self._download_avatar_image(url)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            logger.warning("[%s] failed to download QQ avatar: %s", PLUGIN_ID, exc)
            return (
                self._format_avatar_result_text(
                    qq_id=resolved_qq_id,
                    size=avatar_size,
                    url=url,
                    image_attached=False,
                )
                + f"\n图片下载失败: {exc}"
            )

        encoded_image = base64.b64encode(image_data).decode("ascii")
        result_text = self._format_avatar_result_text(
            qq_id=resolved_qq_id,
            size=avatar_size,
            url=url,
            image_attached=True,
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=result_text),
                ImageContent(type="image", data=encoded_image, mimeType=mime_type),
            ],
            isError=False,
        )

    @filter.command("qq_avatar")
    async def qq_avatar_command(
        self,
        event: AstrMessageEvent,
        qq_id: str | None = None,
        size: str | None = None,
    ) -> None:
        """Show a QQ user's avatar."""
        if not self._plugin_enabled() or not self._commands_enabled():
            yield event.plain_result("QQ 头像命令当前未启用。")
            return

        requested_qq_id = _clean_text(qq_id)
        requested_size = _clean_text(size)
        if requested_qq_id in ALLOWED_AVATAR_SIZES and not requested_size:
            requested_size = requested_qq_id
            requested_qq_id = ""

        resolved_qq_id, error = self._resolve_qq_id(
            event=event,
            qq_id=requested_qq_id,
        )
        if error:
            yield event.plain_result(error)
            return
        assert resolved_qq_id is not None

        avatar_size = normalize_avatar_size(requested_size, self._default_avatar_size())
        url = build_qq_avatar_url(resolved_qq_id, avatar_size)
        text = self._format_avatar_result_text(
            qq_id=resolved_qq_id,
            size=avatar_size,
            url=url,
            image_attached=False,
        )
        yield event.chain_result([Comp.Image.fromURL(url), Comp.Plain(text)])

    @filter.on_llm_request(priority=-5)
    async def inject_avatar_tool_hint(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
    ) -> None:
        if not self._plugin_enabled() or not self._llm_tool_enabled():
            return
        message = _clean_text(getattr(event, "message_str", ""))
        lowered = message.lower()
        if "头像" not in message and "avatar" not in lowered:
            return
        if not any(token in lowered for token in ("qq", "q号", "uin", "avatar")) and "头像" not in message:
            return

        hint = (
            "\n\n[辅助工具提示]\n"
            f"如果用户要求查看 QQ 用户头像，请优先调用 `{QQ_AVATAR_TOOL_NAME}`。"
            "用户没给 QQ 号时可以让工具使用当前发送者 QQ 号。"
        )
        current_prompt = _clean_text(getattr(request, "system_prompt", ""))
        if QQ_AVATAR_TOOL_NAME not in current_prompt:
            request.system_prompt = f"{current_prompt}{hint}" if current_prompt else hint.strip()
