from __future__ import annotations

import base64
import re
from collections import Counter
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
QQ_GROUP_MEMBER_LIST_TOOL_NAME = "get_qq_group_member_list"
QQ_GROUP_INFO_TOOL_NAME = "get_qq_group_info"
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

GROUP_MEMBER_KNOWN_FIELDS = {
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

GROUP_INFO_KNOWN_FIELDS = {
    "group_id",
    "group_name",
    "group_name_raw",
    "group_memo",
    "group_create_time",
    "group_level",
    "member_count",
    "max_member_count",
}

HONOR_LABELS = {
    "current_talkative": "当前龙王",
    "talkative_list": "龙王",
    "performer_list": "群聊之火",
    "legend_list": "群聊炽焰",
    "strong_newbie_list": "冒尖小春笋",
    "emotion_list": "快乐源泉",
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


def _unwrap_onebot_list(payload: Any) -> list[Any] | None:
    """Accept direct OneBot lists and common adapter response wrappers."""

    if isinstance(payload, (list, tuple)):
        return list(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("data", "members", "member_list", "list", "result"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, dict):
            nested = _unwrap_onebot_list(value)
            if nested is not None:
                return nested
    return None


def _unwrap_onebot_mapping(payload: Any) -> dict[str, Any] | None:
    """Accept direct OneBot mappings and common adapter response wrappers."""

    if not isinstance(payload, dict):
        return None
    for key in ("data", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    return dict(payload)


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

    def member_list_default_limit(self) -> int:
        return read_int(
            cfg(self.config, "qq_member", "member_list_default_limit", 80),
            80,
            minimum=1,
            maximum=300,
        )

    def member_list_max_limit(self) -> int:
        return read_int(
            cfg(self.config, "qq_member", "member_list_max_limit", 200),
            200,
            minimum=1,
            maximum=500,
        )

    def member_list_max_text_chars(self) -> int:
        return read_int(
            cfg(self.config, "qq_member", "member_list_max_text_chars", 16000),
            16000,
            minimum=2000,
            maximum=60000,
        )

    def group_info_max_text_chars(self) -> int:
        return read_int(
            cfg(self.config, "qq_member", "group_info_max_text_chars", 12000),
            12000,
            minimum=2000,
            maximum=40000,
        )

    def group_info_include_member_statistics(self) -> bool:
        return read_bool(
            cfg(self.config, "qq_member", "group_info_include_member_statistics", True),
            True,
        )

    def group_info_include_honors(self) -> bool:
        return read_bool(
            cfg(self.config, "qq_member", "group_info_include_honors", True),
            True,
        )

    def group_info_honor_member_limit(self) -> int:
        return read_int(
            cfg(self.config, "qq_member", "group_info_honor_member_limit", 5),
            5,
            minimum=1,
            maximum=20,
        )

    def group_info_include_at_all_remain(self) -> bool:
        return read_bool(
            cfg(self.config, "qq_member", "group_info_include_at_all_remain", True),
            True,
        )

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
        info = _unwrap_onebot_mapping(info)
        if info is None:
            return None, "OneBot 返回的群成员信息不是字典。"
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

    async def fetch_group_info_detail(
        self,
        *,
        event: Any,
        group_id: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        resolved_group_id, error = self.resolve_group_id(event, group_id)
        if error:
            return None, error
        assert resolved_group_id is not None
        try:
            bot = require_onebot(event)
            payload = await call_onebot(bot, "get_group_info", group_id=int(resolved_group_id))
        except Exception as exc:
            return None, f"获取群详情失败：{exc}"
        info = _unwrap_onebot_mapping(payload)
        if info is None:
            return None, "OneBot 返回的群详情不是字典。"
        info.setdefault("group_id", resolved_group_id)
        return info, ""

    async def fetch_group_info(self, *, event: Any, group_id: str) -> dict[str, Any]:
        """Compatibility wrapper used by the existing QQ profile tool."""

        info, _error = await self.fetch_group_info_detail(event=event, group_id=group_id)
        return info or {}

    async def fetch_group_member_list(
        self,
        *,
        event: Any,
        group_id: str = "",
    ) -> tuple[list[dict[str, Any]] | None, str]:
        resolved_group_id, error = self.resolve_group_id(event, group_id)
        if error:
            return None, error
        assert resolved_group_id is not None
        try:
            bot = require_onebot(event)
            payload = await call_onebot(
                bot,
                "get_group_member_list",
                group_id=int(resolved_group_id),
            )
        except Exception as exc:
            return None, f"获取群成员列表失败：{exc}"
        raw_members = _unwrap_onebot_list(payload)
        if raw_members is None:
            return None, "OneBot 返回的群成员列表不是数组。"
        members: list[dict[str, Any]] = []
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                continue
            member = dict(raw_member)
            member.setdefault("group_id", resolved_group_id)
            members.append(member)
        return members, ""

    async def fetch_group_honor_info(
        self,
        *,
        event: Any,
        group_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            bot = require_onebot(event)
            payload = await call_onebot(
                bot,
                "get_group_honor_info",
                group_id=int(group_id),
                type="all",
            )
        except Exception as exc:
            logger.debug("[HelperTools] get_group_honor_info unavailable: %s", exc)
            return None, "当前 OneBot 适配器未提供群荣誉信息。"
        info = _unwrap_onebot_mapping(payload)
        if info is None:
            return None, "当前 OneBot 适配器未提供群荣誉信息。"
        return info, ""

    async def fetch_group_at_all_remain(
        self,
        *,
        event: Any,
        group_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            bot = require_onebot(event)
            payload = await call_onebot(
                bot,
                "get_group_at_all_remain",
                group_id=int(group_id),
            )
        except Exception as exc:
            logger.debug("[HelperTools] get_group_at_all_remain unavailable: %s", exc)
            return None, "当前 OneBot 适配器未提供 @全体成员配额。"
        info = _unwrap_onebot_mapping(payload)
        if info is None:
            return None, "当前 OneBot 适配器未提供 @全体成员配额。"
        return info, ""

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
            extras = {
                k: v
                for k, v in info.items()
                if k not in GROUP_MEMBER_KNOWN_FIELDS and not is_empty_value(v)
            }
            if extras:
                lines.append("")
                lines.append("其它原始字段")
                lines.append(json_dumps(extras))
        return "\n".join(lines)

    @staticmethod
    def _member_display_name(info: dict[str, Any]) -> str:
        return first_non_empty(info.get("card"), info.get("nickname"), info.get("nick")) or "未命名成员"

    def _format_group_member_list_entry(self, index: int, info: dict[str, Any]) -> str:
        qq_id = clean_text(info.get("user_id")) or "未知"
        lines = [f"{index}. {self._member_display_name(info)}（QQ {qq_id}）"]
        details = [
            _line("QQ昵称", first_non_empty(info.get("nickname"), info.get("nick"))),
            _line("群昵称", info.get("card")),
            _line("群身份", info.get("role"), formatter=_format_role),
            _line("群等级", info.get("level")),
            _line("群专属头衔", info.get("title")),
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
        lines.extend(f"   {item}" for item in details if item)
        if self.include_raw_extra_fields():
            extras = {
                key: value
                for key, value in info.items()
                if key not in GROUP_MEMBER_KNOWN_FIELDS and not is_empty_value(value)
            }
            if extras:
                lines.append(f"   OneBot 其它字段: {truncate(json_dumps(extras), 1500)}")
        return "\n".join(lines)

    @staticmethod
    def _member_matches_keyword(info: dict[str, Any], keyword: str) -> bool:
        if not keyword:
            return True
        searchable = " ".join(
            clean_text(info.get(key))
            for key in ("user_id", "nickname", "nick", "card", "title", "role", "level")
        ).casefold()
        return keyword.casefold() in searchable

    def format_group_member_list(
        self,
        *,
        group_id: str,
        members: list[dict[str, Any]],
        group_info: dict[str, Any] | None,
        keyword: str,
        offset: int,
        limit: int,
    ) -> str:
        group_name = first_non_empty(
            (group_info or {}).get("group_name"),
            (group_info or {}).get("group_name_raw"),
        )
        matched_members = [
            member for member in members if self._member_matches_keyword(member, keyword)
        ]
        page = matched_members[offset : offset + limit]
        lines = ["QQ群成员列表", f"群号: {group_id}"]
        if group_name:
            lines.append(f"群名称: {group_name}")
        lines.append(f"OneBot 返回成员数: {len(members)}")
        if keyword:
            lines.append(f"关键词筛选: {keyword}（匹配 {len(matched_members)} 名）")
        if not matched_members:
            lines.append("没有找到符合条件的群成员。")
            return truncate("\n".join(lines), self.member_list_max_text_chars())
        if not page:
            lines.append(
                f"offset={offset} 已超过 {len(matched_members)} 名匹配成员的范围，请从 0 开始重新查询。"
            )
            return truncate("\n".join(lines), self.member_list_max_text_chars())

        start = offset + 1
        max_chars = self.member_list_max_text_chars()
        continuation_reserve = 220
        result = "\n".join(lines)
        rendered_count = 0
        for index, member in enumerate(page, start=start):
            entry = self._format_group_member_list_entry(index, member)
            candidate = f"\n\n{entry}"
            if len(result) + len(candidate) + continuation_reserve > max_chars:
                break
            result += candidate
            rendered_count += 1

        if not rendered_count and page:
            # A single adapter-specific extra field can be very large. Keep one
            # identifiable member in the response rather than returning only a header.
            fallback_limit = max(200, max_chars - len(result) - 240)
            result += f"\n\n{truncate(self._format_group_member_list_entry(start, page[0]), fallback_limit)}"
            rendered_count = 1

        end = offset + rendered_count
        summary = f"本次返回: 第 {start}-{end} 名，共 {len(matched_members)} 名匹配成员"
        if len(result) + len(summary) + 2 <= max_chars:
            result += f"\n\n{summary}"
        if end < len(matched_members):
            next_page = f"其余 {len(matched_members) - end} 名可继续使用 offset={end} 查询。"
            if len(result) + len(next_page) + 1 <= max_chars:
                result += f"\n{next_page}"
        return result

    def _format_group_member_statistics(self, members: list[dict[str, Any]]) -> list[str]:
        if not members:
            return ["成员统计: OneBot 返回 0 名成员。"]
        role_counts = Counter(
            _format_role(member.get("role")) or "成员" for member in members
        )
        role_order = ("群主", "管理员", "成员")
        role_text = "，".join(
            f"{role} {role_counts[role]}"
            for role in (*role_order, *sorted(set(role_counts) - set(role_order)))
            if role_counts[role]
        )
        title_count = sum(bool(clean_text(member.get("title"))) for member in members)
        card_count = sum(bool(clean_text(member.get("card"))) for member in members)
        robot_count = sum(read_bool(member.get("is_robot"), False) for member in members)
        unfriendly_count = sum(read_bool(member.get("unfriendly"), False) for member in members)
        lines = [
            f"成员统计（基于 OneBot 返回的 {len(members)} 名成员）",
            f"身份: {role_text or '未提供'}",
            f"设置群昵称: {card_count}，有专属头衔: {title_count}",
        ]
        if robot_count or unfriendly_count:
            lines.append(f"标记为机器人: {robot_count}，标记为风险账号: {unfriendly_count}")
        managers = [
            member
            for member in members
            if clean_text(member.get("role")) in {"owner", "admin"}
        ]
        if managers:
            rendered = [
                f"{self._member_display_name(member)}（QQ {clean_text(member.get('user_id')) or '未知'}，{_format_role(member.get('role'))}）"
                for member in managers[:20]
            ]
            suffix = "；…" if len(managers) > len(rendered) else ""
            lines.append(f"群主/管理员: {'；'.join(rendered)}{suffix}")
        return lines

    def _format_group_honors(self, info: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        entry_limit = self.group_info_honor_member_limit()
        for key, label in HONOR_LABELS.items():
            value = info.get(key)
            if isinstance(value, dict):
                entries = [value]
            elif isinstance(value, (list, tuple)):
                entries = [item for item in value if isinstance(item, dict)]
            else:
                continue
            if not entries:
                continue
            rendered: list[str] = []
            for entry in entries[:entry_limit]:
                qq_id = clean_text(entry.get("user_id")) or "未知"
                name = first_non_empty(entry.get("card"), entry.get("nickname"), entry.get("nick")) or "未命名成员"
                details = [
                    clean_text(entry.get("description")),
                    _line("持续", entry.get("day_count")),
                ]
                detail_text = "；".join(item for item in details if item)
                rendered.append(
                    f"{name}（QQ {qq_id}{f'；{detail_text}' if detail_text else ''}）"
                )
            suffix = f"；另有 {len(entries) - len(rendered)} 位未列出" if len(entries) > len(rendered) else ""
            lines.append(f"{label}: {'；'.join(rendered)}{suffix}")
        return lines

    def _format_group_at_all_remain(self, info: dict[str, Any]) -> list[str]:
        fields = [
            _line("当前账号可 @全体成员", info.get("can_at_all"), formatter=_format_bool),
            _line("本群剩余 @全体次数", info.get("remain_at_all_count_for_group")),
            _line("当前账号剩余 @全体次数", info.get("remain_at_all_count_for_uin")),
        ]
        return [item for item in fields if item]

    def format_group_info(self, info: dict[str, Any]) -> list[str]:
        group_id = clean_text(info.get("group_id"))
        lines = ["QQ群详情"]
        details = [
            _line("群号", group_id),
            _line("群名称", first_non_empty(info.get("group_name"), info.get("group_name_raw"))),
            _line("群备注", info.get("group_memo")),
            _line("建群时间", info.get("group_create_time"), formatter=format_timestamp),
            _line("群等级", info.get("group_level")),
            _line("成员数", info.get("member_count")),
            _line("最大成员数", info.get("max_member_count")),
        ]
        lines.extend(item for item in details if item)
        if self.include_raw_extra_fields():
            extras = {
                key: value
                for key, value in info.items()
                if key not in GROUP_INFO_KNOWN_FIELDS and not is_empty_value(value)
            }
            if extras:
                lines.extend(("", "OneBot 其它群字段", json_dumps(extras)))
        return lines

    async def get_group_member_list_result(
        self,
        *,
        event: Any,
        group_id: str = "",
        keyword: str = "",
        offset: Any = 0,
        limit: Any = None,
        include_group_info: bool = True,
    ) -> str:
        resolved_group_id, error = self.resolve_group_id(event, group_id)
        if error:
            return error
        assert resolved_group_id is not None
        members, error = await self.fetch_group_member_list(
            event=event,
            group_id=resolved_group_id,
        )
        if error:
            return error
        assert members is not None
        group_info: dict[str, Any] | None = None
        if include_group_info:
            group_info, _group_error = await self.fetch_group_info_detail(
                event=event,
                group_id=resolved_group_id,
            )
        default_limit = self.member_list_default_limit()
        effective_limit = read_int(
            limit,
            default_limit,
            minimum=1,
            maximum=self.member_list_max_limit(),
        )
        effective_offset = read_int(offset, 0, minimum=0, maximum=100000)
        return self.format_group_member_list(
            group_id=resolved_group_id,
            members=members,
            group_info=group_info,
            keyword=clean_text(keyword),
            offset=effective_offset,
            limit=effective_limit,
        )

    async def get_group_info_result(
        self,
        *,
        event: Any,
        group_id: str = "",
        include_member_statistics: bool = True,
        include_honors: bool = True,
        include_at_all_remain: bool = True,
    ) -> str:
        info, error = await self.fetch_group_info_detail(event=event, group_id=group_id)
        if error:
            return error
        assert info is not None
        resolved_group_id = clean_text(info.get("group_id"))
        lines = self.format_group_info(info)

        if include_member_statistics and self.group_info_include_member_statistics():
            members, member_error = await self.fetch_group_member_list(
                event=event,
                group_id=resolved_group_id,
            )
            lines.append("")
            if member_error:
                lines.append("成员统计: 获取失败，当前适配器没有返回成员列表。")
            else:
                assert members is not None
                lines.extend(self._format_group_member_statistics(members))

        if include_honors and self.group_info_include_honors():
            honor_info, honor_error = await self.fetch_group_honor_info(
                event=event,
                group_id=resolved_group_id,
            )
            lines.append("")
            if honor_error:
                lines.append(f"群荣誉: {honor_error}")
            else:
                assert honor_info is not None
                honors = self._format_group_honors(honor_info)
                lines.append("群荣誉")
                lines.extend(honors or ["当前群没有可用荣誉数据。"])

        if include_at_all_remain and self.group_info_include_at_all_remain():
            at_all_info, at_all_error = await self.fetch_group_at_all_remain(
                event=event,
                group_id=resolved_group_id,
            )
            lines.append("")
            if at_all_error:
                lines.append(f"@全体成员配额: {at_all_error}")
            else:
                assert at_all_info is not None
                values = self._format_group_at_all_remain(at_all_info)
                lines.append("@全体成员配额")
                lines.extend(values or ["适配器没有返回可用配额字段。"])

        return truncate("\n".join(lines), self.group_info_max_text_chars())

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
