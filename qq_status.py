"""Query user presence separately from profile fields that may retain old text."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import time
from typing import Any

from astrbot.api import logger

from .helper_utils import format_timestamp
from .onebot_compat import call_onebot, raise_for_payload, unwrap_payload

USER_STATUS_ACTION = "nc_get_user_status"
USER_STATUS_TIMEOUT_SECONDS = 4.0
USER_STATUS_RULE = (
    "状态仅以本次专用接口查询结果为准；未知必须回答无法确认。"
    "资料卡残留的自定义状态、签名和最后发言时间不能当成当前状态，"
    "QQ 展示状态也不能证明用户正在进行某项现实活动。"
)
MEMBER_LIST_STATUS_NOTE = (
    "成员在线/展示状态: 未查询，不能从成员列表或最后发言时间推断；"
    "需要时请针对该 QQ 调用 get_qq_profile 或 get_qq_group_member_info。"
)

# Verified in NapCat's SetOnlineStatus action table. Unknown codes stay unknown.
# https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/extends/SetOnlineStatus.ts
ONLINE_STATUS_LABELS = {
    10: "在线",
    30: "离开",
    40: "隐身",
    50: "忙碌",
    60: "Q我吧",
    70: "请勿打扰",
}
EXT_STATUS_LABELS = {
    1028: "听歌中",
}


def _status_code(value: Any) -> int | None:
    # bool is an int subclass; neither booleans nor floats are status codes.
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d{1,10}", value.strip()):
        return int(value.strip())
    return None


@dataclass(frozen=True)
class QQUserStatus:
    status: int | None = None
    ext_status: int | None = None
    checked_at: float | None = None
    unavailable_reason: str = "未单独查询用户状态"

    def render(self) -> list[str]:
        base = ONLINE_STATUS_LABELS.get(self.status)
        if base is None:
            return [
                f"QQ在线状态: 未知（{self.unavailable_reason}）",
                "QQ展示状态: 未知",
                USER_STATUS_RULE,
            ]
        # Custom wording is deliberately never read from the profile. QQ can
        # retain it after the user has selected a different status.
        extended = EXT_STATUS_LABELS.get(self.ext_status) if self.status == 10 else None
        lines = [
            f"QQ在线状态: {base}",
            f"QQ展示状态: {extended or '未知（接口未提供可确认的当前展示文字）'}",
            f"状态来源: {USER_STATUS_ACTION} 本次查询；仅代表协议返回。",
        ]
        if self.checked_at is not None:
            lines.append(f"状态查询时间: {format_timestamp(self.checked_at)}")
        lines.append(USER_STATUS_RULE)
        return lines


async def query_user_status(bot: Any, user_id: str) -> QQUserStatus:
    """Use only a target-specific read action, with no profile/status fallback.

    NapCat and SnowLuma implement this action. Other adapters can report an
    unsupported action; that must not turn cached profile data into presence.
    OneBot's standard ``get_status`` describes the bot and is never used here.
    """

    try:
        payload = await asyncio.wait_for(
            call_onebot(bot, USER_STATUS_ACTION, user_id=int(user_id)),
            timeout=USER_STATUS_TIMEOUT_SECONDS,
        )
        info = payload
        for _ in range(4):
            # HTTP/proxy adapters can nest wrappers. A failed inner wrapper
            # must be rejected before its data can be interpreted as presence.
            raise_for_payload(info)
            unwrapped = unwrap_payload(info, depth=1)
            if unwrapped is info:
                break
            info = unwrapped
        raise_for_payload(info)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return QQUserStatus(unavailable_reason="用户状态查询超时")
    except Exception as exc:  # noqa: BLE001 - adapter errors have no common base class
        logger.debug("[HelperTools] user status query unavailable: %s", exc)
        return QQUserStatus(unavailable_reason="当前协议未提供可用的用户状态查询")

    if not isinstance(info, Mapping):
        return QQUserStatus(unavailable_reason="用户状态接口未返回有效数据")
    returned_id = info.get("user_id")
    if returned_id is not None and str(returned_id) != str(user_id):
        return QQUserStatus(unavailable_reason="状态接口返回的 QQ 号与查询目标不一致")
    status = _status_code(info.get("status"))
    if status not in ONLINE_STATUS_LABELS:
        return QQUserStatus(unavailable_reason="用户状态接口未返回可识别的状态码")
    return QQUserStatus(
        status=status,
        ext_status=_status_code(info.get("ext_status")),
        checked_at=time(),
        unavailable_reason="",
    )


_PROFILE_STATUS_FIELDS = frozenset(
    {
        "status", "statusname", "statusdesc", "statustext", "statusinfo",
        "extstatus", "extstatusname", "extstatustext", "extstatusdesc",
        "online", "isonline", "presence", "batterystatus",
    }
)


def strip_profile_status_fields(value: Any) -> Any:
    """Remove unverified presence from optional raw fields, without mutating input.

    Apply recursively: adapters can nest the same fields under profile/userInfo
    or return camelCase/snake_case aliases. Ordinary extra fields stay available.
    """

    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[_\-\s]", "", str(key)).casefold()
            if normalized in _PROFILE_STATUS_FIELDS or normalized.startswith(
                ("customstatus", "diystatus", "onlinestatus")
            ):
                continue
            result[key] = strip_profile_status_fields(item)
        return result
    if isinstance(value, (list, tuple)):
        return [strip_profile_status_fields(item) for item in value]
    return value
