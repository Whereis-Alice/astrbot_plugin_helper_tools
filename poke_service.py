from __future__ import annotations

import asyncio
import copy
import random
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .helper_utils import (
    cfg,
    clean_text,
    core_wake_prefixes,
    read_bool,
    read_float,
    read_int,
    resolve_existing_path,
)
from .qq_features import call_onebot

POKE_TOOL_NAME = "poke_qq_user"
POKE_SYNTHETIC_COMMAND_EXTRA = "helper_tools_poke_synthetic_command"
POKE_PERSONA_REPLY_EXTRA = "helper_tools_poke_persona_reply"
POKE_CRON_JOB_NAME = "astrbot_plugin_helper_tools:poke:scheduled"
DEFAULT_POKE_CRON = "30 22 * * *"
DEFAULT_POKE_TIMEZONE = "Asia/Shanghai"

_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_EXTENSIONS = {
    ".aac",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".silk",
    ".wav",
}
_DEFAULT_COMMAND_POOL = (
    "盒",
    "怒撕",
    "咖波撕",
    "拍",
    "摸",
    "捏",
    "啃",
    "踩",
    "锤",
    "上香",
    "小丑",
)
_DEFAULT_FACE_IDS = (
    1,
    14,
    23,
    46,
    97,
    98,
    265,
    266,
    267,
    312,
)
_DEFAULT_KEYWORDS = ("笨蛋", "人机", "机器人", "bot")
_DEFAULT_LLM_PROMPT = (
    "{username}（QQ {user_id}）刚刚戳了你一下。请结合当前对话和既有人格，"
    "用一句自然的话回应；可以表现轻微不满或玩笑，不要解释系统行为。"
)
_ACTION_DEFAULT_WEIGHTS = {
    "antipoke": 10,
    "llm_reply": 10,
    "qq_face": 10,
    "image_reply": 10,
    "voice_reply": 0,
    "mute_reply": 0,
    "command_reply": 10,
}


@dataclass(frozen=True, slots=True)
class PokeNotice:
    self_id: str
    user_id: str
    target_id: str
    group_id: str
    timestamp: int = 0

    @classmethod
    def from_event(cls, event: Any) -> PokeNotice | None:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return None
        if clean_text(raw.get("post_type")).lower() != "notice":
            return None
        if clean_text(raw.get("notice_type")).lower() != "notify":
            return None
        if clean_text(raw.get("sub_type")).lower() != "poke":
            return None

        self_id = _numeric_id(raw.get("self_id") or _event_value(event, "get_self_id"))
        user_id = _numeric_id(raw.get("user_id"))
        target_id = _numeric_id(raw.get("target_id"))
        group_id = _numeric_id(raw.get("group_id"))
        if not self_id or not user_id or not target_id:
            return None
        return cls(
            self_id=self_id,
            user_id=user_id,
            target_id=target_id,
            group_id=group_id,
            timestamp=read_int(raw.get("time"), 0, minimum=0),
        )

    @property
    def is_self_poked(self) -> bool:
        return self.target_id == self.self_id

    @property
    def is_self_sent(self) -> bool:
        return self.user_id == self.self_id


@dataclass(frozen=True, slots=True)
class PokeResponse:
    handled: bool = False
    action: str = ""
    chain: tuple[Any, ...] = ()
    llm_prompt: str = ""


@dataclass(frozen=True, slots=True)
class PokeSendResult:
    attempted: int
    succeeded: int
    failed: int

    @property
    def ok(self) -> bool:
        return self.attempted > 0 and self.failed == 0


@dataclass(frozen=True, slots=True)
class PokeCommandRequest:
    target_ids: tuple[str, ...] = ()
    times: int = 1
    error: str = ""
    omitted_targets: int = 0


class PokeCooldown:
    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._users: dict[tuple[str, str], float] = {}
        self._groups: dict[str, float] = {}

    def allow(
        self,
        group_id: str,
        user_id: str,
        *,
        user_seconds: float,
        group_seconds: float,
    ) -> bool:
        now = float(self._clock())
        key = (group_id, user_id)
        user_last = self._users.get(key)
        if user_seconds > 0 and user_last is not None and now - user_last < user_seconds:
            return False
        group_last = self._groups.get(group_id)
        if (
            group_id
            and group_seconds > 0
            and group_last is not None
            and now - group_last < group_seconds
        ):
            return False

        self._users[key] = now
        if group_id and group_seconds > 0:
            self._groups[group_id] = now
        if len(self._users) > 4096:
            self._prune(now, max(user_seconds, group_seconds, 60.0))
        return True

    def _prune(self, now: float, retention_seconds: float) -> None:
        cutoff = now - max(60.0, retention_seconds * 2)
        self._users = {key: value for key, value in self._users.items() if value >= cutoff}
        self._groups = {
            key: value for key, value in self._groups.items() if value >= cutoff
        }


class PokeService:
    """Bounded OneBot poke interactions and bot-authored command dispatch."""

    def __init__(
        self,
        config: Any,
        data_dir: Path,
        context: Any,
        *,
        rng: Any = random,
    ) -> None:
        self.config = config
        self.data_dir = data_dir
        self.context = context
        self.rng = rng
        self.cooldown = PokeCooldown()
        self._last_bot: Any | None = None
        self._media_cache: dict[str, tuple[float, tuple[Any, ...], tuple[Path, ...]]] = {}
        self._cron_job_id = ""

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "poke", "enabled", False), False)

    def listen_enabled(self) -> bool:
        return read_bool(cfg(self.config, "poke", "listen_enabled", True), True)

    def tool_enabled(self) -> bool:
        return read_bool(cfg(self.config, "poke", "llm_tool_enabled", True), True)

    def commands_enabled(self) -> bool:
        return read_bool(cfg(self.config, "poke", "commands_enabled", True), True)

    def remember_bot(self, event: Any) -> None:
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._last_bot = bot

    def is_synthetic_command(self, event: Any) -> bool:
        return is_poke_synthetic_command(event)

    async def handle_event(self, event: Any) -> PokeResponse:
        self.remember_bot(event)
        if not self.enabled() or self.is_synthetic_command(event):
            return PokeResponse()

        notice = PokeNotice.from_event(event)
        if notice is not None:
            if not self.listen_enabled() or notice.is_self_sent:
                return PokeResponse()
            return await self._handle_notice(event, notice)

        await self._handle_keyword_poke(event)
        return PokeResponse()

    async def _handle_notice(self, event: Any, notice: PokeNotice) -> PokeResponse:
        if not notice.is_self_poked:
            follow_probability = read_float(
                cfg(self.config, "poke", "follow_probability", 0.0),
                0.0,
                minimum=0.0,
                maximum=1.0,
            )
            if follow_probability <= 0 or self.rng.random() >= follow_probability:
                return PokeResponse()
            if not self._cooldown_allows(notice):
                return PokeResponse(handled=True, action="cooldown")
            await self.send_pokes(event, [notice.target_id], times=1)
            return PokeResponse(handled=True, action="follow")

        if not self._cooldown_allows(notice):
            return PokeResponse(handled=True, action="cooldown")

        action = self.choose_action(notice)
        if not action:
            logger.warning("[HelperTools/Poke] no usable response action has a positive weight")
            return PokeResponse(handled=True, action="none")
        logger.info(
            "[HelperTools/Poke] selected action=%s group=%s user=%s",
            action,
            notice.group_id or "private",
            notice.user_id,
        )

        if action == "antipoke":
            section = self._section(action)
            maximum = read_int(section.get("max_times"), 3, minimum=1, maximum=10)
            times = self.rng.randint(1, maximum)
            await self.send_pokes(event, [notice.user_id], times=times)
            return PokeResponse(handled=True, action=action)

        if action == "llm_reply":
            prompt = self._render_llm_prompt(
                event,
                notice,
                clean_text(self._section(action).get("prompt_template"), _DEFAULT_LLM_PROMPT),
            )
            return PokeResponse(handled=True, action=action, llm_prompt=prompt)

        if action == "qq_face":
            section = self._section(action)
            face_id = self.rng.choice(self._face_ids())
            maximum = read_int(section.get("max_count"), 3, minimum=1, maximum=8)
            count = self.rng.randint(1, maximum)
            return PokeResponse(
                handled=True,
                action=action,
                chain=tuple(Comp.Face(id=face_id) for _ in range(count)),
            )

        if action == "image_reply":
            path = self.rng.choice(self._media_files(action, _IMAGE_EXTENSIONS))
            return PokeResponse(
                handled=True,
                action=action,
                chain=(Comp.Image.fromFileSystem(str(path)),),
            )

        if action == "voice_reply":
            path = self.rng.choice(self._media_files(action, _AUDIO_EXTENSIONS))
            return PokeResponse(
                handled=True,
                action=action,
                chain=(Comp.Record.fromFileSystem(str(path)),),
            )

        if action == "mute_reply":
            success, reason, duration = await self._mute_poker(event, notice)
            section = self._section(action)
            key = "success_prompt_template" if success else "failure_prompt_template"
            default = (
                "{username} 戳了你一下，随后被禁言 {duration} 秒。请按当前人格用一句话自然回应。"
                if success
                else "{username} 戳了你一下，但禁言没有成功（{reason}）。请按当前人格用一句话自然回应。"
            )
            prompt = self._render_llm_prompt(
                event,
                notice,
                clean_text(section.get(key), default),
                reason=reason,
                duration=str(duration),
            )
            return PokeResponse(handled=True, action=action, llm_prompt=prompt)

        if action == "command_reply":
            command = self.rng.choice(self.command_pool())
            dispatched = await self.dispatch_synthetic_command(event, notice, command)
            return PokeResponse(
                handled=True,
                action=action if dispatched else "command_failed",
            )

        return PokeResponse(handled=True, action=action)

    def choose_action(self, notice: PokeNotice) -> str:
        actions: list[str] = []
        weights: list[int] = []
        for action, default_weight in _ACTION_DEFAULT_WEIGHTS.items():
            weight = read_int(
                self._section(action).get("weight"),
                default_weight,
                minimum=0,
                maximum=100,
            )
            if weight <= 0 or not self._action_is_usable(action, notice):
                continue
            actions.append(action)
            weights.append(weight)
        if not actions:
            return ""
        return self.rng.choices(actions, weights=weights, k=1)[0]

    def _action_is_usable(self, action: str, notice: PokeNotice) -> bool:
        if action == "llm_reply":
            return bool(
                clean_text(
                    self._section(action).get("prompt_template"),
                    _DEFAULT_LLM_PROMPT,
                )
            )
        if action == "qq_face":
            return bool(self._face_ids())
        if action == "image_reply":
            return bool(self._media_files(action, _IMAGE_EXTENSIONS))
        if action == "voice_reply":
            return bool(self._media_files(action, _AUDIO_EXTENSIONS))
        if action == "mute_reply":
            return bool(notice.group_id)
        if action == "command_reply":
            # Aiocqhttp routes private replies through get_sender_id(). Once the
            # synthetic author is the bot, a private command could reply to itself.
            return bool(notice.group_id and self.command_pool())
        return True

    def _cooldown_allows(self, notice: PokeNotice) -> bool:
        return self.cooldown.allow(
            notice.group_id,
            notice.user_id,
            user_seconds=read_float(
                cfg(self.config, "poke", "user_cooldown_seconds", 10),
                10.0,
                minimum=0.0,
                maximum=3600.0,
            ),
            group_seconds=read_float(
                cfg(self.config, "poke", "group_cooldown_seconds", 2),
                2.0,
                minimum=0.0,
                maximum=3600.0,
            ),
        )

    async def _handle_keyword_poke(self, event: Any) -> bool:
        outgoing = self._section("outgoing")
        if not read_bool(outgoing.get("keyword_enabled"), False):
            return False
        sender_id = _numeric_id(_event_value(event, "get_sender_id"))
        self_id = _numeric_id(_event_value(event, "get_self_id"))
        if not sender_id or sender_id == self_id:
            return False
        if read_bool(outgoing.get("keyword_requires_wake"), True) and not bool(
            getattr(event, "is_at_or_wake_command", False)
        ):
            return False
        text = clean_text(getattr(event, "message_str", ""))
        keywords = _string_list(outgoing.get("keywords"), _DEFAULT_KEYWORDS)
        if not text or not any(keyword in text for keyword in keywords):
            return False
        result = await self.send_pokes(event, [sender_id], times=1)
        if result.attempted:
            logger.info("[HelperTools/Poke] keyword-triggered poke user=%s", sender_id)
        return result.succeeded > 0

    async def handle_command(self, event: Any) -> str:
        if not self.enabled() or not self.commands_enabled():
            return "戳一戳命令当前未启用。"
        request = await self._parse_command_request(event)
        if request.error:
            return request.error
        result = await self.send_pokes(event, request.target_ids, times=request.times)
        if result.attempted == 0:
            return "没有找到可以戳的目标。"
        omitted_note = (
            f"；另有 {request.omitted_targets} 位超过人数上限未处理"
            if request.omitted_targets
            else ""
        )
        if result.failed == 0:
            return (
                f"已戳 {len(request.target_ids)} 位用户，每位 {request.times} 次"
                f"{omitted_note}。"
            )
        return (
            f"戳一戳完成：成功 {result.succeeded} 次，失败 {result.failed} 次"
            f"{omitted_note}。"
        )

    async def _parse_command_request(self, event: Any) -> PokeCommandRequest:
        text = clean_text(getattr(event, "message_str", ""))
        self_id = _numeric_id(_event_value(event, "get_self_id"))
        sender_id = _numeric_id(_event_value(event, "get_sender_id"))
        outgoing = self._section("outgoing")
        max_times = read_int(outgoing.get("max_times"), 5, minimum=1, maximum=20)
        times_tokens = [int(item) for item in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", text)]
        times = max(1, min(max_times, times_tokens[-1] if times_tokens else 1))

        if "全体成员" in text:
            is_admin = getattr(event, "is_admin", None)
            if not callable(is_admin) or not is_admin():
                return PokeCommandRequest(error="只有管理员可以使用“戳全体成员”。")
            group_id = _numeric_id(_event_value(event, "get_group_id"))
            if not group_id:
                return PokeCommandRequest(error="“戳全体成员”只能在 QQ 群中使用。")
            bot = getattr(event, "bot", None)
            if bot is None:
                return PokeCommandRequest(error="当前 OneBot 适配器不支持读取群成员列表。")
            try:
                members = await call_onebot(
                    bot,
                    "get_group_member_list",
                    group_id=int(group_id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[HelperTools/Poke] group member lookup failed: %r", exc)
                return PokeCommandRequest(error="读取群成员列表失败，未执行全员戳一戳。")
            members = _unwrap_onebot_list(members, "members")
            target_ids = _dedupe_ids(
                _numeric_id(item.get("user_id"))
                for item in members or []
                if isinstance(item, dict)
            )
            target_ids = tuple(item for item in target_ids if item != self_id)
            maximum = read_int(
                outgoing.get("max_group_targets"),
                200,
                minimum=1,
                maximum=500,
            )
            omitted_targets = max(0, len(target_ids) - maximum)
            if len(target_ids) > maximum:
                target_ids = tuple(self.rng.sample(list(target_ids), maximum))
            return PokeCommandRequest(
                target_ids=target_ids,
                times=times,
                omitted_targets=omitted_targets,
            )

        targets = list(self._mention_ids(event))
        if "戳我" in text or re.search(r"(?:^|\s)我(?:\s|$)", text):
            targets.append(sender_id)
        targets.extend(re.findall(r"(?<!\d)(\d{5,12})(?!\d)", text))
        target_ids = tuple(
            item for item in _dedupe_ids(targets) if item and item != self_id
        )
        if not target_ids:
            return PokeCommandRequest(
                error="请指定目标，例如“/戳 @某人 2”或“/戳我”。"
            )
        maximum = read_int(
            outgoing.get("max_direct_targets"),
            5,
            minimum=1,
            maximum=50,
        )
        return PokeCommandRequest(
            target_ids=target_ids[:maximum],
            times=times,
            omitted_targets=max(0, len(target_ids) - maximum),
        )

    async def poke_from_tool(self, event: Any, user_id: Any, times: Any = 1) -> str:
        if not self.enabled() or not self.tool_enabled():
            return "戳一戳 LLM 工具当前未启用。"
        target_id = _numeric_id(user_id)
        if not target_id:
            return "戳一戳失败：user_id 必须是纯数字 QQ 号。"
        self_id = _numeric_id(_event_value(event, "get_self_id"))
        if target_id == self_id:
            return "戳一戳失败：不能让机器人戳自己。"
        maximum = read_int(
            self._section("outgoing").get("max_times"),
            5,
            minimum=1,
            maximum=20,
        )
        actual_times = read_int(times, 1, minimum=1, maximum=maximum)
        result = await self.send_pokes(event, [target_id], times=actual_times)
        if result.failed == 0 and result.succeeded:
            return f"已戳用户 {target_id} {actual_times} 次。"
        if result.succeeded:
            return (
                f"戳用户 {target_id} 部分成功：成功 {result.succeeded} 次，"
                f"失败 {result.failed} 次。"
            )
        return f"戳一戳失败：QQ 接口未成功戳用户 {target_id}。"

    async def send_pokes(
        self,
        event: Any,
        target_ids: Any,
        *,
        times: int,
    ) -> PokeSendResult:
        bot = getattr(event, "bot", None)
        group_id = _numeric_id(_event_value(event, "get_group_id"))
        return await self._send_with_bot(bot, target_ids, group_id=group_id, times=times)

    async def _send_with_bot(
        self,
        bot: Any,
        target_ids: Any,
        *,
        group_id: str,
        times: int,
    ) -> PokeSendResult:
        targets = _dedupe_ids(target_ids)
        actual_times = read_int(times, 1, minimum=1, maximum=20)
        attempted = len(targets) * actual_times
        if bot is None or not targets:
            return PokeSendResult(attempted=attempted, succeeded=0, failed=attempted)
        interval = read_float(
            self._section("outgoing").get("interval_seconds"),
            0.5,
            minimum=0.0,
            maximum=10.0,
        )
        succeeded = 0
        failed = 0
        operation_index = 0
        for target_id in targets:
            for _ in range(actual_times):
                operation_index += 1
                try:
                    if group_id:
                        await call_onebot(
                            bot,
                            "group_poke",
                            group_id=int(group_id),
                            user_id=int(target_id),
                        )
                    else:
                        await call_onebot(
                            bot,
                            "friend_poke",
                            user_id=int(target_id),
                        )
                    succeeded += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.warning(
                        "[HelperTools/Poke] poke failed group=%s user=%s: %r",
                        group_id or "private",
                        target_id,
                        exc,
                    )
                if interval > 0 and operation_index < attempted:
                    await asyncio.sleep(interval)
        return PokeSendResult(attempted=attempted, succeeded=succeeded, failed=failed)

    async def dispatch_synthetic_command(
        self,
        event: Any,
        notice: PokeNotice,
        command: Any,
    ) -> bool:
        normalized = self._normalize_command(command)
        if not normalized:
            logger.warning(
                "[HelperTools/Poke] command_reply failed command=%r target=%s reason=empty_command",
                command,
                notice.user_id,
            )
            return False
        session = clean_text(getattr(event, "unified_msg_origin", ""))
        logger.info(
            "[HelperTools/Poke] command_reply selected command=%r target=%s session=%s",
            normalized,
            notice.user_id,
            session,
        )
        try:
            synthetic = self._build_synthetic_command_event(event, notice, normalized)
        except Exception as exc:  # noqa: BLE001 - event implementations vary by adapter
            logger.warning(
                "[HelperTools/Poke] command_reply failed command=%r target=%s "
                "reason=build_synthetic_event error=%r",
                normalized,
                notice.user_id,
                exc,
            )
            return False
        if synthetic is None:
            logger.warning(
                "[HelperTools/Poke] command_reply failed command=%r target=%s "
                "reason=build_synthetic_event returned_none",
                normalized,
                notice.user_id,
            )
            return False
        queue_getter = getattr(self.context, "get_event_queue", None)
        queue = queue_getter() if callable(queue_getter) else None
        put_nowait = getattr(queue, "put_nowait", None)
        if not callable(put_nowait):
            logger.warning(
                "[HelperTools/Poke] command_reply failed command=%r target=%s "
                "reason=event_queue_unavailable session=%s",
                normalized,
                notice.user_id,
                session,
            )
            return False
        try:
            put_nowait(synthetic)
        except Exception as exc:  # noqa: BLE001 - queue implementations vary
            logger.warning(
                "[HelperTools/Poke] command_reply failed command=%r target=%s "
                "reason=event_queue_rejected error=%r session=%s",
                normalized,
                notice.user_id,
                exc,
                session,
            )
            return False
        event.stop_event()
        logger.info(
            "[HelperTools/Poke] command_reply success command=%r target=%s session=%s",
            normalized,
            notice.user_id,
            session,
        )
        return True

    def _build_synthetic_command_event(
        self,
        event: Any,
        notice: PokeNotice,
        command: str,
    ) -> Any | None:
        message_obj = copy.copy(getattr(event, "message_obj", None))
        if message_obj is None:
            return None
        defer_bot_author = self._core_ignores_self_messages(event)
        transport_sender_id = notice.user_id if defer_bot_author else notice.self_id
        sender = copy.copy(getattr(message_obj, "sender", None))
        if sender is None:
            sender = SimpleNamespace(
                user_id=transport_sender_id,
                nickname=transport_sender_id,
            )
        else:
            sender.user_id = transport_sender_id
            if not defer_bot_author:
                sender.nickname = notice.self_id
        message_obj.sender = sender
        chain: list[Any] = [Comp.Plain(command)]
        if notice.group_id:
            chain.append(Comp.At(qq=notice.user_id))
        message_obj.message = chain
        message_obj.message_str = command
        message_obj.message_id = f"helper-poke-{uuid.uuid4().hex}"
        message_obj.timestamp = int(time.time())
        raw_segments = [{"type": "text", "data": {"text": command}}]
        if notice.group_id:
            raw_segments.append({"type": "at", "data": {"qq": notice.user_id}})
        message_obj.raw_message = {
            "time": message_obj.timestamp,
            "self_id": _onebot_id(notice.self_id),
            "post_type": "message",
            "message_type": "group" if notice.group_id else "private",
            "sub_type": "normal" if notice.group_id else "friend",
            "message_id": message_obj.message_id,
            "user_id": _onebot_id(transport_sender_id),
            "group_id": _onebot_id(notice.group_id) if notice.group_id else None,
            "message": raw_segments,
            "raw_message": command,
            "sender": {
                "user_id": _onebot_id(transport_sender_id),
                "nickname": clean_text(getattr(sender, "nickname", ""), transport_sender_id),
            },
        }
        try:
            synthetic = event.__class__(
                command,
                message_obj,
                event.platform_meta,
                event.session_id,
                getattr(event, "bot", None),
            )
        except TypeError:
            synthetic = copy.copy(event)
            synthetic.message_str = command
            synthetic.message_obj = message_obj
            synthetic.clear_result()
            synthetic._force_stopped = False
            synthetic._has_send_oper = False
            synthetic._extras = {}
        synthetic.role = "member"
        synthetic.is_wake = True
        synthetic.is_at_or_wake_command = True
        # AstrBot's historical name is inverted: True disables only the default LLM path.
        synthetic.should_call_llm(True)
        synthetic.set_extra(
            POKE_SYNTHETIC_COMMAND_EXTRA,
            {
                "command": command,
                "source_user_id": notice.user_id,
                "source_group_id": notice.group_id,
                "self_id": notice.self_id,
                "created_at": int(time.time()),
                "deferred_bot_author": defer_bot_author,
            },
        )
        if not defer_bot_author:
            materialize_poke_synthetic_command_author(synthetic)
        else:
            logger.info(
                "[HelperTools/Poke] core ignores self-authored events; command author will be "
                "restored before plugin handlers run"
            )
        return synthetic

    def _core_ignores_self_messages(self, event: Any) -> bool:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return False
        try:
            core_config = getter(clean_text(getattr(event, "unified_msg_origin", "")))
        except TypeError:
            core_config = getter()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[HelperTools/Poke] core config lookup failed: %r", exc)
            return False
        if not hasattr(core_config, "get"):
            return False
        platform_settings = core_config.get("platform_settings", {})
        if not isinstance(platform_settings, dict):
            return False
        return read_bool(platform_settings.get("ignore_bot_self_message"), False)

    async def _mute_poker(
        self,
        event: Any,
        notice: PokeNotice,
    ) -> tuple[bool, str, int]:
        if not notice.group_id:
            return False, "私聊不能禁言", 0
        section = self._section("mute_reply")
        base = read_int(section.get("duration_seconds"), 60, minimum=1, maximum=86400)
        delta = read_int(section.get("random_delta_seconds"), 30, minimum=0, maximum=3600)
        duration = max(1, min(86400, base + self.rng.randint(-delta, delta)))
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, "当前 OneBot 适配器缺少禁言接口", duration
        try:
            bot_info = await call_onebot(
                bot,
                "get_group_member_info",
                group_id=int(notice.group_id),
                user_id=int(notice.self_id),
            )
            target_info = await call_onebot(
                bot,
                "get_group_member_info",
                group_id=int(notice.group_id),
                user_id=int(notice.user_id),
            )
            bot_info = _unwrap_onebot_mapping(bot_info)
            target_info = _unwrap_onebot_mapping(target_info)
            bot_role = clean_text((bot_info or {}).get("role"), "member")
            target_role = clean_text((target_info or {}).get("role"), "member")
            if bot_role not in {"admin", "owner"}:
                return False, "机器人不是群管理员", duration
            if target_role == "owner":
                return False, "目标用户是群主", duration
            if target_role == "admin" and bot_role != "owner":
                return False, "机器人无权禁言管理员", duration
            await call_onebot(
                bot,
                "set_group_ban",
                group_id=int(notice.group_id),
                user_id=int(notice.user_id),
                duration=duration,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/Poke] mute action failed: %r", exc)
            return False, "调用 QQ 禁言接口失败", duration
        return True, "", duration

    def _render_llm_prompt(
        self,
        event: Any,
        notice: PokeNotice,
        template: str,
        **values: str,
    ) -> str:
        username = clean_text(_event_value(event, "get_sender_name"), notice.user_id)
        replacements = {
            "username": username,
            "user_id": notice.user_id,
            "group_id": notice.group_id or "私聊",
            "reason": values.get("reason", ""),
            "duration": values.get("duration", "0"),
        }
        rendered = template
        for key, value in replacements.items():
            rendered = rendered.replace("{" + key + "}", value)
        return clean_text(rendered)

    async def conversation_for_event(self, event: Any) -> Any | None:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        umo = clean_text(getattr(event, "unified_msg_origin", ""))
        if not umo:
            return None
        try:
            conversation_id = await manager.get_curr_conversation_id(umo)
            if not conversation_id:
                conversation_id = await manager.new_conversation(
                    umo,
                    _event_value(event, "get_platform_id"),
                )
            return await manager.get_conversation(umo, conversation_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/Poke] conversation lookup failed: %r", exc)
            return None

    def command_pool(self) -> tuple[str, ...]:
        values = _string_list(
            self._section("command_reply").get("commands"),
            _DEFAULT_COMMAND_POOL,
        )
        commands: list[str] = []
        seen: set[str] = set()
        for value in values:
            command = self._normalize_command(value)
            if command and command not in seen:
                seen.add(command)
                commands.append(command)
        return tuple(commands)

    def _normalize_command(self, value: Any) -> str:
        command = " ".join(clean_text(value).split())
        prefixes = sorted(
            {*core_wake_prefixes(self.context), "/"},
            key=len,
            reverse=True,
        )
        changed = True
        while command and changed:
            changed = False
            for prefix in prefixes:
                if prefix and command.startswith(prefix):
                    command = command[len(prefix) :].lstrip()
                    changed = True
                    break
        return command[:200]

    def _face_ids(self) -> tuple[int, ...]:
        raw = self._section("qq_face").get("face_ids")
        values = list(_DEFAULT_FACE_IDS) if raw is None else raw
        if not isinstance(values, list | tuple):
            values = [values]
        result: list[int] = []
        for value in values:
            face_id = read_int(value, -1, minimum=-1, maximum=100000)
            if face_id >= 0 and face_id not in result:
                result.append(face_id)
        return tuple(result)

    def _media_files(self, section_name: str, extensions: set[str]) -> tuple[Path, ...]:
        section = self._section(section_name)
        file_values = _string_list(section.get("files"), ())
        directory_values = _string_list(section.get("directories"), ())
        recursive = read_bool(section.get("recursive"), True)
        signature: tuple[Any, ...] = (
            tuple(file_values),
            tuple(directory_values),
            recursive,
            tuple(sorted(extensions)),
        )
        cached = self._media_cache.get(section_name)
        now = time.monotonic()
        if cached and cached[1] == signature and now - cached[0] < 30:
            return cached[2]

        paths: list[Path] = []
        seen: set[str] = set()

        def add(path: Path) -> None:
            if not path.is_file() or path.suffix.lower() not in extensions:
                return
            resolved = path.resolve(strict=False)
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                paths.append(resolved)

        for value in file_values:
            path = resolve_existing_path(value, self.data_dir)
            if path is not None:
                add(path)
        for value in directory_values:
            root = resolve_existing_path(value, self.data_dir)
            if root is None:
                continue
            if root.is_file():
                add(root)
                continue
            if not root.is_dir():
                continue
            iterator = root.rglob("*") if recursive else root.iterdir()
            for path in iterator:
                add(path)
        result = tuple(sorted(paths))
        self._media_cache[section_name] = (now, signature, result)
        return result

    def _mention_ids(self, event: Any) -> tuple[str, ...]:
        getter = getattr(event, "get_messages", None)
        messages = getter() if callable(getter) else getattr(
            getattr(event, "message_obj", None),
            "message",
            [],
        )
        return _dedupe_ids(
            _numeric_id(getattr(component, "qq", ""))
            for component in messages or []
            if isinstance(component, Comp.At)
        )

    def _section(self, name: str) -> dict[str, Any]:
        value = cfg(self.config, "poke", name, {})
        return value if isinstance(value, dict) else {}

    async def start(self) -> None:
        if not self.enabled():
            return
        scheduler = self._section("scheduler")
        if not read_bool(scheduler.get("enabled"), False):
            return
        if not self._schedule_targets():
            logger.warning("[HelperTools/Poke] scheduled poke is enabled but target list is empty")
            return
        await self._register_cron_job()

    async def stop(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None or not self._cron_job_id:
            self._cron_job_id = ""
            return
        try:
            await self._maybe_await(manager.delete_job(self._cron_job_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/Poke] failed to delete scheduled poke job: %r", exc)
        finally:
            self._cron_job_id = ""

    async def _register_cron_job(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            logger.warning("[HelperTools/Poke] cron_manager unavailable; schedule was not registered")
            return
        await self._delete_existing_cron_jobs(manager)
        scheduler = self._section("scheduler")
        expression = normalize_poke_cron_expression(scheduler.get("cron_expression"))
        timezone = clean_text(scheduler.get("timezone"), DEFAULT_POKE_TIMEZONE)
        try:
            job = await self._maybe_await(
                manager.add_basic_job(
                    name=POKE_CRON_JOB_NAME,
                    cron_expression=expression,
                    timezone=timezone,
                    handler=self._run_scheduled_pokes,
                    payload={"reason": "schedule"},
                    persistent=False,
                    enabled=True,
                    description="Send bounded scheduled QQ pokes.",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[HelperTools/Poke] failed to register scheduled poke: %r", exc)
            await self._delete_existing_cron_jobs(manager)
            return
        self._cron_job_id = clean_text(self._job_value(job, "job_id", "id"))
        logger.info(
            "[HelperTools/Poke] scheduled poke cron=%s timezone=%s job=%s",
            expression,
            timezone,
            self._cron_job_id,
        )

    async def _run_scheduled_pokes(self, reason: str = "schedule") -> None:
        bot = self._last_bot or self._resolve_bot()
        if bot is None:
            logger.warning("[HelperTools/Poke] scheduled poke skipped: no OneBot client")
            return
        times = read_int(
            self._section("scheduler").get("times"),
            1,
            minimum=1,
            maximum=20,
        )
        for group_id, user_id in self._schedule_targets():
            result = await self._send_with_bot(
                bot,
                [user_id],
                group_id=group_id,
                times=times,
            )
            logger.info(
                "[HelperTools/Poke] scheduled result reason=%s group=%s user=%s success=%d failed=%d",
                reason,
                group_id,
                user_id,
                result.succeeded,
                result.failed,
            )

    def _schedule_targets(self) -> tuple[tuple[str, str], ...]:
        values = _string_list(self._section("scheduler").get("targets"), ())
        result: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            group_raw, separator, user_raw = value.partition(":")
            if not separator:
                continue
            group_id = _numeric_id(group_raw)
            user_id = _numeric_id(user_raw)
            target = (group_id, user_id)
            if group_id and user_id and target not in seen:
                seen.add(target)
                result.append(target)
        return tuple(result)

    async def _delete_existing_cron_jobs(self, manager: Any) -> None:
        try:
            try:
                jobs = await self._maybe_await(manager.list_jobs("basic"))
            except TypeError:
                jobs = await self._maybe_await(manager.list_jobs())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/Poke] failed to list existing cron jobs: %r", exc)
            return
        for job in jobs or []:
            if self._job_value(job, "name") != POKE_CRON_JOB_NAME:
                continue
            job_id = clean_text(self._job_value(job, "job_id", "id"))
            if not job_id:
                continue
            try:
                await self._maybe_await(manager.delete_job(job_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[HelperTools/Poke] failed to delete stale cron job: %r", exc)

    def _resolve_bot(self) -> Any | None:
        manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(manager, "platform_insts", []) if manager is not None else []
        configured_id = clean_text(self._section("scheduler").get("platform_id"))
        for platform in platforms or []:
            try:
                metadata = platform.meta()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[HelperTools/Poke] platform metadata lookup failed: %r", exc)
                continue
            name = clean_text(getattr(metadata, "name", ""))
            platform_id = clean_text(getattr(metadata, "id", ""))
            if configured_id:
                if configured_id not in {name, platform_id}:
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
        return await value if isawaitable(value) else value

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


def is_poke_synthetic_command(event: Any) -> bool:
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        return bool(getter(POKE_SYNTHETIC_COMMAND_EXTRA, None))
    extras = getattr(event, "_extras", None)
    return isinstance(extras, dict) and bool(extras.get(POKE_SYNTHETIC_COMMAND_EXTRA))


def mark_poke_persona_reply(event: Any) -> None:
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        setter(POKE_PERSONA_REPLY_EXTRA, True)
        return
    extras = getattr(event, "_extras", None)
    if isinstance(extras, dict):
        extras[POKE_PERSONA_REPLY_EXTRA] = True


def is_poke_persona_reply(event: Any) -> bool:
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        return bool(getter(POKE_PERSONA_REPLY_EXTRA, None))
    extras = getattr(event, "_extras", None)
    return isinstance(extras, dict) and bool(extras.get(POKE_PERSONA_REPLY_EXTRA))


def materialize_poke_synthetic_command_author(event: Any) -> bool:
    """Set a queued synthetic command's visible author to the current bot account."""

    getter = getattr(event, "get_extra", None)
    metadata = (
        getter(POKE_SYNTHETIC_COMMAND_EXTRA, None)
        if callable(getter)
        else getattr(event, "_extras", {}).get(POKE_SYNTHETIC_COMMAND_EXTRA)
    )
    if not isinstance(metadata, dict):
        return False
    self_id = _numeric_id(metadata.get("self_id"))
    if not self_id:
        return False
    message_obj = getattr(event, "message_obj", None)
    if message_obj is None:
        return False
    sender = getattr(message_obj, "sender", None)
    if sender is None:
        sender = SimpleNamespace(user_id=self_id, nickname=self_id)
        message_obj.sender = sender
    else:
        sender.user_id = self_id
        sender.nickname = self_id
    raw = getattr(message_obj, "raw_message", None)
    if isinstance(raw, dict):
        raw["user_id"] = _onebot_id(self_id)
        raw_sender = raw.get("sender")
        if not isinstance(raw_sender, dict):
            raw_sender = {}
            raw["sender"] = raw_sender
        raw_sender["user_id"] = _onebot_id(self_id)
        raw_sender["nickname"] = self_id
    should_call_llm = getattr(event, "should_call_llm", None)
    if callable(should_call_llm):
        should_call_llm(True)
    return True


def mark_synthetic_command_messages_temporary(event: Any, run_context: Any) -> int:
    if not is_poke_synthetic_command(event):
        return 0
    return _mark_latest_agent_turn_temporary(run_context)


def mark_poke_agent_messages_temporary(event: Any, run_context: Any) -> int:
    if not (is_poke_synthetic_command(event) or is_poke_persona_reply(event)):
        return 0
    return _mark_latest_agent_turn_temporary(run_context)


def _mark_latest_agent_turn_temporary(run_context: Any) -> int:
    messages = getattr(run_context, "messages", None)
    if not isinstance(messages, list):
        return 0
    latest_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if getattr(messages[index], "role", "") == "user"
        ),
        -1,
    )
    if latest_user_index < 0:
        return 0
    marked = 0
    for message in messages[latest_user_index:]:
        message._no_save = True
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for part in content:
                mark_as_temp = getattr(part, "mark_as_temp", None)
                if callable(mark_as_temp):
                    mark_as_temp()
                else:
                    with suppress(AttributeError, TypeError, ValueError):
                        part._no_save = True
        marked += 1
    return marked


def normalize_poke_cron_expression(value: Any) -> str:
    expression = " ".join(clean_text(value, DEFAULT_POKE_CRON).split())
    parts = expression.split()
    if len(parts) == 5:
        return expression
    if len(parts) == 4:
        normalized = f"{expression} *"
        logger.warning(
            "[HelperTools/Poke] cron has 4 fields, normalized %r to %r",
            expression,
            normalized,
        )
        return normalized
    if len(parts) == 6 and parts[0] == "0":
        normalized = " ".join(parts[1:])
        logger.warning(
            "[HelperTools/Poke] cron has 6 fields, normalized %r to %r",
            expression,
            normalized,
        )
        return normalized
    logger.warning(
        "[HelperTools/Poke] invalid cron %r, using %r",
        expression,
        DEFAULT_POKE_CRON,
    )
    return DEFAULT_POKE_CRON


def _event_value(event: Any, method_name: str) -> Any:
    method = getattr(event, method_name, None)
    if not callable(method):
        return ""
    try:
        return method()
    except Exception:  # noqa: BLE001
        return ""


def _numeric_id(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return text if text.isdigit() and int(text) > 0 else ""


def _onebot_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _dedupe_ids(values: Any) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = _numeric_id(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _string_list(value: Any, default: Any) -> tuple[str, ...]:
    if value is None:
        values = default
    elif isinstance(value, list | tuple | set):
        values = value
    elif isinstance(value, str):
        values = [value]
    else:
        return ()
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("path") or item.get("file") or item.get("name") or ""
        text = clean_text(item)
        if text:
            result.append(text)
    return tuple(result)


def _unwrap_onebot_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    if isinstance(data, dict) and "role" not in value:
        return data
    return value


def _unwrap_onebot_list(value: Any, key: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    direct = value.get(key)
    if isinstance(direct, list):
        return direct
    data = value.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data[key]
    return []
