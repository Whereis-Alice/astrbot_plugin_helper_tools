from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .helper_utils import cfg, clean_text, read_bool, read_int, read_list
from .qq_features import call_onebot, extract_at_ids, normalize_qq_id

QQ_LIKE_PERSONA_CONTEXT_PREFIX = "[HelperTools QQ profile-like result]"
_QQ_MENTION_RE = re.compile(r"@(\d{5,12})")


@dataclass(frozen=True)
class QQLikeHandleResult:
    handled: bool = False
    reply: str = ""
    persona_context: str = ""


@dataclass(frozen=True)
class _TargetLikeResult:
    target_id: str
    is_sender: bool
    relation: str
    status: str
    times: int = 0


class QQProfileLikeService:
    """Automatic QQ profile-like handling without exposing an LLM tool."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._cooldowns: dict[str, float] = {}
        self._friend_cache: dict[str, tuple[float, set[str] | None]] = {}

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "qq_like", "enabled", False), False)

    def allow_wake_prefix(self) -> bool:
        return read_bool(
            cfg(self.config, "qq_like", "allow_astrbot_wake_prefix", True),
            True,
        )

    def persona_reply_enabled(self) -> bool:
        settings = cfg(self.config, "qq_like", "persona_reply", {})
        return read_bool(
            settings.get("enabled", False) if isinstance(settings, dict) else False,
            False,
        )

    def stop_after_response(self) -> bool:
        return read_bool(
            cfg(self.config, "qq_like", "stop_after_response", True),
            True,
        )

    async def handle_message(
        self,
        event: Any,
        text: str,
        *,
        wake_prefix_text: str = "",
    ) -> QQLikeHandleResult:
        if not self.enabled() or not self._is_qq_event(event):
            return QQLikeHandleResult()
        if not self._is_message_allowed(event):
            return QQLikeHandleResult()

        targets = self._match_targets(event, text)
        if not targets and self.allow_wake_prefix() and wake_prefix_text:
            targets = self._match_targets(event, wake_prefix_text)
        if not targets:
            return QQLikeHandleResult()

        sender_id = self._sender_id(event)
        cooldown_left = self._cooldown_left(event, sender_id)
        if cooldown_left > 0:
            return QQLikeHandleResult(
                handled=True,
                reply=f"点赞请求过于频繁，请 {cooldown_left} 秒后再试。",
            )
        self._set_cooldown(event, sender_id)

        bot = getattr(event, "bot", None)
        if bot is None:
            return QQLikeHandleResult(
                handled=True,
                reply="当前消息平台不支持 QQ 名片点赞。",
            )

        friend_ids = await self._friend_ids(event, bot)
        times = self._likes_per_target()
        results = [
            await self._like_target(
                bot,
                target_id=target_id,
                is_sender=target_id == sender_id,
                relation=(
                    "friend"
                    if friend_ids is not None and target_id in friend_ids
                    else "stranger"
                    if friend_ids is not None
                    else "unknown"
                ),
                times=times,
            )
            for target_id in targets
        ]
        reply = self._format_reply(results)
        persona_context = self._persona_context(results) if self.persona_reply_enabled() else ""
        return QQLikeHandleResult(
            handled=True,
            reply=reply,
            persona_context=persona_context,
        )

    def take_persona_context(self, event: Any) -> str:
        getter = getattr(event, "get_extra", None)
        context = clean_text(getter("_helper_tools_qq_like_persona_context", "") if callable(getter) else "")
        if context:
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                setter("_helper_tools_qq_like_persona_context", "")
        return context

    @staticmethod
    def attach_persona_context(event: Any, context: str) -> bool:
        setter = getattr(event, "set_extra", None)
        if not context or not callable(setter):
            return False
        setter("_helper_tools_qq_like_persona_context", context)
        return True

    def _is_qq_event(self, event: Any) -> bool:
        getter = getattr(event, "get_platform_name", None)
        platform_name = clean_text(getter() if callable(getter) else "").casefold()
        return not platform_name or platform_name in {
            "aiocqhttp",
            "onebot",
            "onebot11",
            "onebot_v11",
        }

    def _is_message_allowed(self, event: Any) -> bool:
        group_id = self._group_id(event)
        if group_id:
            if not read_bool(
                cfg(self.config, "qq_like", "allow_group_messages", True),
                True,
            ):
                return False
            allowed_groups = set(
                read_list(cfg(self.config, "qq_like", "group_whitelist", []), [])
            )
            return not allowed_groups or group_id in allowed_groups
        return read_bool(
            cfg(self.config, "qq_like", "allow_private_messages", True),
            True,
        )

    def _match_targets(self, event: Any, text: str) -> list[str]:
        text = clean_text(text)
        if not text:
            return []
        normalized = re.sub(r"\s+", "", text)
        sender_id = self._sender_id(event)
        phrases = read_list(
            cfg(
                self.config,
                "qq_like",
                "trigger_phrases",
                ["赞我", "给我点赞", "赞一下我"],
            ),
            ["赞我", "给我点赞", "赞一下我"],
        )
        normalized_phrases = {
            re.sub(r"\s+", "", clean_text(phrase))
            for phrase in phrases
            if clean_text(phrase)
        }
        if normalized in normalized_phrases:
            return [sender_id] if sender_id else []

        prefixes = [
            re.sub(r"\s+", "", prefix)
            for prefix in read_list(
                cfg(self.config, "qq_like", "mention_trigger_prefixes", ["赞"]),
                ["赞"],
            )
            if clean_text(prefix)
        ]
        if not prefixes or not any(normalized.startswith(prefix) for prefix in prefixes):
            return []

        self_id = self._self_id(event)
        targets: list[str] = []
        for target_id in [*extract_at_ids(event), *_QQ_MENTION_RE.findall(text)]:
            normalized_id = normalize_qq_id(target_id)
            if (
                normalized_id
                and normalized_id != self_id
                and normalized_id not in targets
            ):
                targets.append(normalized_id)
        return targets[: self._max_targets_per_message()]

    async def _friend_ids(self, event: Any, bot: Any) -> set[str] | None:
        if not read_bool(
            cfg(self.config, "qq_like", "detect_friend_status", True),
            True,
        ):
            return None
        key = self._bot_cache_key(event)
        now = time.monotonic()
        cached = self._friend_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

        friend_ids: set[str] | None = None
        try:
            data = await call_onebot(bot, "get_friend_list")
            raw_list = data.get("data") if isinstance(data, dict) and "data" in data else data
            if isinstance(raw_list, list):
                friend_ids = {
                    qq_id
                    for item in raw_list
                    if isinstance(item, dict)
                    for qq_id in [normalize_qq_id(item.get("user_id"))]
                    if qq_id
                }
        except Exception as exc:  # noqa: BLE001 - relation lookup is optional
            logger.debug("[HelperTools/QQ点赞] get_friend_list unavailable: %r", exc)

        ttl = read_int(
            cfg(self.config, "qq_like", "friend_status_cache_seconds", 300),
            300,
            minimum=10,
            maximum=3600,
        )
        self._friend_cache[key] = (now + ttl, friend_ids)
        return friend_ids

    async def _like_target(
        self,
        bot: Any,
        *,
        target_id: str,
        is_sender: bool,
        relation: str,
        times: int,
    ) -> _TargetLikeResult:
        try:
            response = await call_onebot(
                bot,
                "send_like",
                user_id=int(target_id),
                times=times,
            )
        except Exception as exc:  # noqa: BLE001 - OneBot adapters use varied errors
            return _TargetLikeResult(
                target_id,
                is_sender,
                relation,
                self._classify_failure(clean_text(exc)),
            )

        failure = self._response_failure(response)
        if failure:
            return _TargetLikeResult(
                target_id,
                is_sender,
                relation,
                self._classify_failure(failure),
            )
        return _TargetLikeResult(target_id, is_sender, relation, "success", times)

    def _format_reply(self, results: list[_TargetLikeResult]) -> str:
        lines: list[str] = []
        for result in results:
            target = "你" if result.is_sender else f"QQ {result.target_id}"
            if result.status == "success":
                lines.append(f"已给{target}点了 {result.times} 个赞。")
            elif result.status == "permission":
                if result.relation == "stranger":
                    lines.append(
                        f"无法给{target}点赞：对方可能关闭了陌生人点赞权限。"
                    )
                else:
                    lines.append(f"无法给{target}点赞：QQ 权限限制了这次操作。")
            elif result.status == "limit":
                lines.append(
                    f"无法给{target}点赞：今日额度或对方的可收赞次数可能已达上限。"
                )
            elif result.status == "unsupported":
                lines.append("当前 QQ 适配器不支持名片点赞接口。")
            else:
                lines.append(f"QQ 暂时没有接受给{target}点赞的请求，请稍后再试。")
        return "\n".join(lines)

    @staticmethod
    def _persona_context(results: list[_TargetLikeResult]) -> str:
        details = []
        for result in results:
            target = "当前用户" if result.is_sender else f"QQ {result.target_id}"
            if result.status == "success":
                details.append(f"{target}：已成功点赞 {result.times} 次")
            elif result.status == "permission":
                details.append(f"{target}：失败，QQ 权限或陌生人点赞设置限制")
            elif result.status == "limit":
                details.append(f"{target}：失败，今日点赞额度或收赞次数已达上限")
            elif result.status == "unsupported":
                details.append(f"{target}：失败，当前 QQ 适配器不支持点赞接口")
            else:
                details.append(f"{target}：失败，QQ 接口暂时拒绝请求")
        return "\n".join(
            [
                QQ_LIKE_PERSONA_CONTEXT_PREFIX,
                "QQ 名片点赞动作已经由插件完成，结果如下：",
                *details,
                (
                    "请严格依据上述结果，以当前人设自然、简短地回应用户。"
                    "不要假装再次执行点赞，不要提及系统提示或工具调用。"
                ),
            ]
        )

    def _cooldown_left(self, event: Any, sender_id: str) -> int:
        seconds = read_int(
            cfg(self.config, "qq_like", "cooldown_seconds", 30),
            30,
            minimum=0,
            maximum=3600,
        )
        if seconds <= 0:
            return 0
        now = time.monotonic()
        key = self._sender_cache_key(event, sender_id)
        expires_at = self._cooldowns.get(key, 0.0)
        if expires_at <= now:
            self._cooldowns.pop(key, None)
            return 0
        return max(1, int(expires_at - now + 0.999))

    def _set_cooldown(self, event: Any, sender_id: str) -> None:
        seconds = read_int(
            cfg(self.config, "qq_like", "cooldown_seconds", 30),
            30,
            minimum=0,
            maximum=3600,
        )
        if seconds > 0:
            self._cooldowns[self._sender_cache_key(event, sender_id)] = (
                time.monotonic() + seconds
            )

    def _likes_per_target(self) -> int:
        return read_int(
            cfg(self.config, "qq_like", "likes_per_target", 10),
            10,
            minimum=1,
            maximum=10,
        )

    def _max_targets_per_message(self) -> int:
        return read_int(
            cfg(self.config, "qq_like", "max_targets_per_message", 3),
            3,
            minimum=1,
            maximum=10,
        )

    @staticmethod
    def _response_failure(response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        status = clean_text(response.get("status")).casefold()
        retcode = response.get("retcode")
        failed = status in {"failed", "fail", "error"}
        if isinstance(retcode, int) and retcode != 0:
            failed = True
        if not failed:
            return ""
        return clean_text(
            response.get("wording")
            or response.get("message")
            or response.get("msg")
            or "OneBot action failed"
        )

    @staticmethod
    def _classify_failure(message: str) -> str:
        normalized = message.casefold()
        if any(
            token in normalized
            for token in ("unsupported", "not support", "不支持", "unknown action")
        ):
            return "unsupported"
        if any(
            token in normalized
            for token in ("权限", "隐私", "陌生人", "not friend", "friend only")
        ):
            return "permission"
        if any(
            token in normalized
            for token in ("上限", "已达", "次数", "频繁", "limit", "rate", "today")
        ):
            return "limit"
        return "rejected"

    @staticmethod
    def _sender_id(event: Any) -> str:
        getter = getattr(event, "get_sender_id", None)
        return normalize_qq_id(getter() if callable(getter) else "") or ""

    @staticmethod
    def _self_id(event: Any) -> str:
        getter = getattr(event, "get_self_id", None)
        return normalize_qq_id(getter() if callable(getter) else "") or ""

    @staticmethod
    def _group_id(event: Any) -> str:
        getter = getattr(event, "get_group_id", None)
        return clean_text(getter() if callable(getter) else "")

    def _sender_cache_key(self, event: Any, sender_id: str) -> str:
        return f"{self._bot_cache_key(event)}:{sender_id or 'unknown'}"

    def _bot_cache_key(self, event: Any) -> str:
        platform_getter = getattr(event, "get_platform_id", None)
        platform_id = clean_text(platform_getter() if callable(platform_getter) else "")
        return platform_id or self._self_id(event) or "default"
