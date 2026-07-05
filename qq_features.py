from __future__ import annotations

import base64
import re
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .helper_utils import (
    cfg,
    clean_text,
    fetch_bytes,
    first_non_empty,
    format_timestamp,
    is_empty_value,
    json_dumps,
    read_bool,
    read_int,
    read_list,
    truncate,
)


QQ_AVATAR_TOOL_NAME = "get_qq_avatar"
QQ_GROUP_MEMBER_TOOL_NAME = "get_qq_group_member_info"
QQ_PROFILE_TOOL_NAME = "get_qq_profile"

ALLOWED_AVATAR_SIZES = ("40", "100", "140", "640")
DEFAULT_AVATAR_SIZE = "640"
QQ_ID_PATTERN = re.compile(r"^(?:qq\s*[:=]?\s*)?(\d{5,12})$", re.IGNORECASE)

ROLE_LABELS = {
    "owner": "群主",
    "admin": "管理员",
    "member": "成员",
}
SEX_LABELS = {
    "male": "男",
    "female": "女",
    "unknown": "未知",
}


def normalize_qq_id(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.strip("@ \t\r\n")
    match = QQ_ID_PATTERN.fullmatch(text)
    return match.group(1) if match else None


def normalize_avatar_size(value: Any, default: str = DEFAULT_AVATAR_SIZE) -> str:
    text = clean_text(value, default)
    return text if text in ALLOWED_AVATAR_SIZES else default


def build_qq_avatar_url(qq_id: str, size: str = DEFAULT_AVATAR_SIZE) -> str:
    return f"https://q.qlogo.cn/headimg_dl?dst_uin={qq_id}&spec={normalize_avatar_size(size)}&img_type=jpg"


def _event_sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        return clean_text(getter())
    return ""


def _event_group_id(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        return clean_text(getter())
    return ""


def _event_self_id(event: Any) -> str:
    getter = getattr(event, "get_self_id", None)
    if callable(getter):
        return clean_text(getter())
    return ""


def extract_at_ids(event: Any) -> list[str]:
    ids: list[str] = []
    messages_getter = getattr(event, "get_messages", None)
    messages = messages_getter() if callable(messages_getter) else []
    for segment in messages or []:
        qq = getattr(segment, "qq", None)
        if qq is None:
            continue
        qq_id = normalize_qq_id(qq)
        if qq_id and qq_id not in ids:
            ids.append(qq_id)
    message_text = clean_text(getattr(event, "message_str", ""))
    for token in message_text.split():
        if token.startswith("@"):
            qq_id = normalize_qq_id(token)
            if qq_id and qq_id not in ids:
                ids.append(qq_id)
    return ids


async def call_onebot(bot: Any, action: str, **params: Any) -> Any:
    method = getattr(bot, action, None)
    if callable(method):
        try:
            return await method(**params)
        except TypeError:
            if "no_cache" in params:
                fallback = dict(params)
                fallback.pop("no_cache", None)
                return await method(**fallback)
            raise
    call_action = getattr(bot, "call_action", None)
    if callable(call_action):
        try:
            return await call_action(action, **params)
        except TypeError:
            if "no_cache" in params:
                fallback = dict(params)
                fallback.pop("no_cache", None)
                return await call_action(action, **fallback)
            raise
    raise RuntimeError("当前事件没有可用的 OneBot 调用入口。")


def require_onebot(event: Any) -> Any:
    bot = getattr(event, "bot", None)
    if bot is None:
        raise RuntimeError("当前平台不支持 OneBot/AIOCQHTTP 接口。")
    return bot


def _format_role(value: Any) -> str:
    text = clean_text(value)
    return ROLE_LABELS.get(text, text)


def _format_sex(value: Any) -> str:
    text = clean_text(value)
    return SEX_LABELS.get(text, text)


def _format_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    return clean_text(value)


def _line(label: str, value: Any, *, formatter: Any = None) -> str | None:
    if is_empty_value(value):
        return None
    text = formatter(value) if formatter else clean_text(value)
    if not text:
        return None
    return f"{label}: {text}"


class QQService:
    def __init__(self, config: Any) -> None:
        self.config = config

    def avatar_default_size(self) -> str:
        return normalize_avatar_size(cfg(self.config, "qq_avatar", "default_size", DEFAULT_AVATAR_SIZE))

    def avatar_timeout(self) -> int:
        return read_int(cfg(self.config, "qq_avatar", "download_timeout_seconds", 8), 8, minimum=1, maximum=60)

    def avatar_max_bytes(self) -> int:
        return read_int(
            cfg(self.config, "qq_avatar", "max_download_bytes", 2 * 1024 * 1024),
            2 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=10 * 1024 * 1024,
        )

    def avatar_download_for_llm(self) -> bool:
        return read_bool(cfg(self.config, "qq_avatar", "download_image_for_llm", True), True)

    def profile_admin_only(self) -> bool:
        return read_bool(cfg(self.config, "qq_profile", "admin_only_other_users", False), False)

    def protected_ids(self) -> set[str]:
        return set(read_list(cfg(self.config, "qq_profile", "protected_ids", []), []))

    def include_raw_extra_fields(self) -> bool:
        return read_bool(cfg(self.config, "qq_member", "include_raw_extra_fields", True), True)

    async def get_avatar_result(
        self,
        *,
        event: Any,
        qq_id: str = "",
        size: str = "",
        return_image: bool = True,
    ) -> str | CallToolResult:
        resolved_qq_id, error = self.resolve_qq_id(event, qq_id)
        if error:
            return error
        assert resolved_qq_id is not None
        avatar_size = normalize_avatar_size(size, self.avatar_default_size())
        url = build_qq_avatar_url(resolved_qq_id, avatar_size)
        text = "\n".join(
            [
                "已获取 QQ 用户头像。",
                f"QQ 号: {resolved_qq_id}",
                f"尺寸: {avatar_size}",
                f"头像 URL: {url}",
            ]
        )
        if not return_image or not self.avatar_download_for_llm():
            return text
        try:
            data, mime_type = await fetch_bytes(
                url,
                timeout_seconds=self.avatar_timeout(),
                max_bytes=self.avatar_max_bytes(),
            )
        except Exception as exc:
            logger.warning("[HelperTools] failed to download QQ avatar: %s", exc)
            return f"{text}\n图片下载失败，已降级为 URL: {exc}"
        if not mime_type.startswith("image/"):
            mime_type = "image/jpeg"
        return CallToolResult(
            content=[
                TextContent(type="text", text=f"{text}\n图片内容已随工具结果返回。"),
                ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("ascii"),
                    mimeType=mime_type,
                ),
            ],
            isError=False,
        )

    def resolve_qq_id(self, event: Any, qq_id: str = "") -> tuple[str | None, str]:
        normalized = normalize_qq_id(qq_id)
        if normalized:
            return normalized, ""
        if clean_text(qq_id):
            return None, "QQ 号格式不正确，请提供 5 到 12 位数字。"
        at_ids = extract_at_ids(event)
        if at_ids:
            return at_ids[0], ""
        sender_id = normalize_qq_id(_event_sender_id(event))
        if sender_id:
            return sender_id, ""
        return None, "没有提供 QQ 号，也无法从当前消息识别 QQ 号。"

    def resolve_group_id(self, event: Any, group_id: str = "") -> tuple[str | None, str]:
        text = clean_text(group_id)
        if text:
            if text.isdigit():
                return text, ""
            return None, "群号格式不正确，请提供纯数字群号。"
        current_group = _event_group_id(event)
        if current_group:
            return current_group, ""
        return None, "没有提供群号，且当前会话不是 QQ 群聊。"

    def can_query_target(self, event: Any, target_id: str) -> tuple[bool, str]:
        if target_id == _event_self_id(event):
            return False, "不查询 bot 自己。"
        if target_id in self.protected_ids() and target_id != _event_sender_id(event):
            return False, "目标用户在保护名单中。"
        is_admin = getattr(event, "is_admin", lambda: False)
        if self.profile_admin_only() and callable(is_admin) and not is_admin():
            if target_id != _event_sender_id(event):
                return False, "当前配置仅允许管理员查询他人资料。"
        return True, ""

    async def fetch_group_member_info(
        self,
        *,
        event: Any,
        qq_id: str = "",
        group_id: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        target_id, error = self.resolve_qq_id(event, qq_id)
        if error:
            return None, error
        resolved_group_id, error = self.resolve_group_id(event, group_id)
        if error:
            return None, error
        assert target_id is not None and resolved_group_id is not None
        bot = require_onebot(event)
        try:
            info = await call_onebot(
                bot,
                "get_group_member_info",
                group_id=int(resolved_group_id),
                user_id=int(target_id),
                no_cache=True,
            )
        except Exception as exc:
            return None, f"获取群成员信息失败: {exc}"
        if not isinstance(info, dict):
            return None, "OneBot 返回的群成员信息不是字典。"
        info = dict(info)
        info.setdefault("group_id", resolved_group_id)
        info.setdefault("user_id", target_id)
        return info, ""

    async def fetch_stranger_info(self, *, event: Any, qq_id: str) -> dict[str, Any]:
        bot = require_onebot(event)
        info = await call_onebot(
            bot,
            "get_stranger_info",
            user_id=int(qq_id),
            no_cache=True,
        )
        return dict(info) if isinstance(info, dict) else {}

    async def fetch_group_info(self, *, event: Any, group_id: str) -> dict[str, Any]:
        bot = require_onebot(event)
        try:
            info = await call_onebot(bot, "get_group_info", group_id=int(group_id))
        except Exception:
            return {}
        return dict(info) if isinstance(info, dict) else {}

    def format_group_member_info(self, info: dict[str, Any]) -> str:
        lines: list[str] = ["QQ群成员信息"]
        group_id = clean_text(info.get("group_id"))
        if group_id:
            lines.append(f"群号: {group_id}")
        required_items = [
            _line("QQ号", info.get("user_id")),
            _line("QQ名", info.get("nickname")),
            _line("群昵称", info.get("card")),
            _line("群身份", info.get("role"), formatter=_format_role),
            _line("群等级", info.get("level")),
            _line("群专属头衔", info.get("title")),
        ]
        lines.extend(item for item in required_items if item)
        optional_items = [
            _line("性别", info.get("sex"), formatter=_format_sex),
            _line("年龄", info.get("age")),
            _line("地区", info.get("area")),
            _line("入群时间", info.get("join_time"), formatter=format_timestamp),
            _line("最后发言时间", info.get("last_sent_time"), formatter=format_timestamp),
            _line("禁言到期", info.get("shut_up_timestamp"), formatter=format_timestamp),
            _line("头衔到期", info.get("title_expire_time"), formatter=format_timestamp),
            _line("可改群名片", info.get("card_changeable"), formatter=_format_bool),
            _line("风险账号", info.get("unfriendly"), formatter=_format_bool),
            _line("机器人账号", info.get("is_robot"), formatter=_format_bool),
        ]
        optional_lines = [item for item in optional_items if item]
        if optional_lines:
            lines.append("")
            lines.append("可额外获取的信息")
            lines.extend(optional_lines)
        if self.include_raw_extra_fields():
            known = {
                "group_id",
                "user_id",
                "nickname",
                "card",
                "role",
                "level",
                "title",
                "sex",
                "age",
                "area",
                "join_time",
                "last_sent_time",
                "shut_up_timestamp",
                "title_expire_time",
                "card_changeable",
                "unfriendly",
                "is_robot",
            }
            extras = {k: v for k, v in info.items() if k not in known and not is_empty_value(v)}
            if extras:
                lines.append("")
                lines.append("其它原始字段")
                lines.append(json_dumps(extras))
        return "\n".join(lines)

    async def get_group_member_result(self, *, event: Any, qq_id: str = "", group_id: str = "") -> str:
        info, error = await self.fetch_group_member_info(event=event, qq_id=qq_id, group_id=group_id)
        if error:
            return error
        assert info is not None
        return self.format_group_member_info(info)

    async def get_profile_result(
        self,
        *,
        event: Any,
        qq_id: str = "",
        group_id: str = "",
        include_avatar: bool = True,
        return_image: bool = True,
    ) -> str | CallToolResult:
        target_id, error = self.resolve_qq_id(event, qq_id)
        if error:
            return error
        assert target_id is not None
        allowed, reason = self.can_query_target(event, target_id)
        if not allowed:
            return reason
        stranger: dict[str, Any] = {}
        member: dict[str, Any] = {}
        group_info: dict[str, Any] = {}
        try:
            stranger = await self.fetch_stranger_info(event=event, qq_id=target_id)
        except Exception as exc:
            logger.warning("[HelperTools] get_stranger_info failed: %s", exc)
        resolved_group_id, _group_error = self.resolve_group_id(event, group_id)
        if resolved_group_id:
            info, _error = await self.fetch_group_member_info(
                event=event,
                qq_id=target_id,
                group_id=resolved_group_id,
            )
            member = info or {}
            group_info = await self.fetch_group_info(event=event, group_id=resolved_group_id)
        text = self.format_profile(target_id=target_id, stranger=stranger, member=member, group_info=group_info)
        if not include_avatar:
            return text
        avatar_url = build_qq_avatar_url(target_id, self.avatar_default_size())
        text = f"{text}\n头像 URL: {avatar_url}"
        if not return_image or not self.avatar_download_for_llm():
            return text
        try:
            data, mime_type = await fetch_bytes(
                avatar_url,
                timeout_seconds=self.avatar_timeout(),
                max_bytes=self.avatar_max_bytes(),
            )
            if not mime_type.startswith("image/"):
                mime_type = "image/jpeg"
        except Exception as exc:
            logger.warning("[HelperTools] profile avatar download failed: %s", exc)
            return f"{text}\n头像下载失败，已降级为 URL: {exc}"
        return CallToolResult(
            content=[
                TextContent(type="text", text=text),
                ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType=mime_type),
            ],
            isError=False,
        )

    def format_profile(
        self,
        *,
        target_id: str,
        stranger: dict[str, Any],
        member: dict[str, Any],
        group_info: dict[str, Any],
    ) -> str:
        lines = ["QQ 用户资料", f"QQ号: {target_id}"]
        group_name = first_non_empty(group_info.get("group_name"), group_info.get("group_name_raw"))
        if group_name:
            lines.append(f"所在群: {group_name} ({member.get('group_id')})")
        profile_items = [
            _line("QQ名", first_non_empty(stranger.get("nickname"), stranger.get("nick"), member.get("nickname"))),
            _line("备注", stranger.get("remark")),
            _line("群昵称", member.get("card")),
            _line("群身份", member.get("role"), formatter=_format_role),
            _line("群等级", member.get("level")),
            _line("群专属头衔", member.get("title")),
            _line("签名", first_non_empty(stranger.get("long_nick"), stranger.get("longNick"), stranger.get("longnick"))),
            _line("性别", first_non_empty(stranger.get("sex"), member.get("sex")), formatter=_format_sex),
            _line("年龄", first_non_empty(stranger.get("age"), member.get("age"))),
            _line("地区", first_non_empty(stranger.get("area"), member.get("area"))),
            _line("入群时间", member.get("join_time"), formatter=format_timestamp),
            _line("最后发言时间", member.get("last_sent_time"), formatter=format_timestamp),
            _line("QQ等级", stranger.get("qqLevel")),
            _line("VIP等级", stranger.get("vip_level")),
            _line("邮箱", stranger.get("eMail")),
            _line("职业", stranger.get("makeFriendCareer")),
            _line("个性标签", stranger.get("labels")),
        ]
        lines.extend(item for item in profile_items if item)
        merged = {"stranger_info": stranger, "group_member_info": member}
        if read_bool(cfg(self.config, "qq_profile", "include_raw_extra_fields", False), False):
            lines.append("")
            lines.append("原始字段")
            lines.append(json_dumps(merged))
        max_chars = read_int(cfg(self.config, "qq_profile", "max_text_chars", 4000), 4000, minimum=500, maximum=20000)
        return truncate("\n".join(lines), max_chars)

    def command_avatar_chain(self, qq_id: str, size: str = "") -> list[Any]:
        avatar_size = normalize_avatar_size(size, self.avatar_default_size())
        url = build_qq_avatar_url(qq_id, avatar_size)
        return [Comp.Image.fromURL(url), Comp.Plain(f"QQ号: {qq_id}\n头像 URL: {url}")]
