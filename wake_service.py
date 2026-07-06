from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, core_wake_prefixes, read_bool, read_float, read_int, read_list, section


WAKE_MODE_CONTAINS = "contains"
WAKE_MODE_PREFIX = "prefix"
WAKE_MODE_SUFFIX = "suffix"

BOT_RANGES: tuple[tuple[int, int], ...] = (
    (3328144510, 3328144510),
    (2854196301, 2854216399),
    (66600000, 66600000),
    (3889000000, 3889999999),
    (4010000000, 4019999999),
)

DEFAULT_BUILTIN_COMMANDS = [
    "llm",
    "t2i",
    "tts",
    "sid",
    "op",
    "wl",
    "dashboard_update",
    "alter_cmd",
    "provider",
    "model",
    "plugin",
    "plugin ls",
    "new",
    "switch",
    "rename",
    "del",
    "reset",
    "history",
    "persona",
    "tool ls",
    "key",
    "websearch",
]

DEFAULT_BLOCK_WORDS_PATH = Path(__file__).with_name("default_wake_block_words.json")


@dataclass(slots=True)
class WakeMatch:
    matched: bool = False
    word: str = ""
    mode: str = ""
    admin_only: bool = False


@dataclass(slots=True)
class PendingWakeRequest:
    event: Any
    chain: list[Any]
    plain: str
    created_at: float
    merged_count: int = 1
    cleanup_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class DebounceResult:
    merged: bool = False
    skip_reason: str = ""
    merged_count: int = 1


def _event_sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    return clean_text(getter()) if callable(getter) else ""


def _event_group_id(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    return clean_text(getter()) if callable(getter) else ""


def _event_self_id(event: Any) -> str:
    getter = getattr(event, "get_self_id", None)
    return clean_text(getter()) if callable(getter) else ""


def _event_platform_name(event: Any) -> str:
    getter = getattr(event, "get_platform_name", None)
    return clean_text(getter()) if callable(getter) else ""


def _is_admin(event: Any) -> bool:
    checker = getattr(event, "is_admin", None)
    return bool(checker()) if callable(checker) else False


def _event_messages(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    messages = getter() if callable(getter) else []
    return messages if isinstance(messages, list) else []


def _plain_text_from_chain(chain: list[Any]) -> str:
    return " ".join(clean_text(getattr(seg, "text", "")) for seg in chain if isinstance(seg, Comp.Plain)).strip()


def _normalize_mode(value: str) -> str:
    text = clean_text(value).lower()
    if not text:
        return ""
    if text in {WAKE_MODE_CONTAINS, "free", "any", "自由触发"} or text.startswith("自由"):
        return WAKE_MODE_CONTAINS
    if text in {WAKE_MODE_PREFIX, "前缀触发"} or text.startswith("前缀"):
        return WAKE_MODE_PREFIX
    if text in {WAKE_MODE_SUFFIX, "后缀触发"} or text.startswith("后缀"):
        return WAKE_MODE_SUFFIX
    return text


def _is_qqbot_id(value: str) -> bool:
    try:
        uid = int(value)
    except (TypeError, ValueError):
        return False
    return any(start <= uid <= end for start, end in BOT_RANGES)


def _clean_for_compare(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", clean_text(text)).lower()


class WakeService:
    def __init__(self, config: Any, context: Any | None = None) -> None:
        self.config = config
        self.context = context
        self._last_wake: dict[str, float] = {}
        self._bot_messages: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=5))
        self._pending: dict[str, PendingWakeRequest] = {}
        self._pending_lock = asyncio.Lock()
        self._commands_cache: set[str] | None = None
        self._default_block_keywords_cache: list[str] | None = None
        self._migrate_editable_block_keywords()

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "wake", "enabled", True), True)

    def at_wake_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wake", "at_wake_enabled", True), True)

    def disable_reply_wake(self) -> bool:
        return read_bool(cfg(self.config, "wake", "disable_reply_wake", True), True)

    def strip_prefix_suffix_word(self) -> bool:
        return read_bool(cfg(self.config, "wake", "strip_prefix_suffix_wake_word", False), False)

    def wake_words(self) -> list[str]:
        return read_list(cfg(self.config, "wake", "wake_words", []), [])

    def admin_wake_words(self) -> list[str]:
        return read_list(cfg(self.config, "wake", "admin_wake_words", ["宝贝", "宝宝"]), ["宝贝", "宝宝"])

    def trigger_modes(self) -> set[str]:
        raw = read_list(cfg(self.config, "wake", "trigger_modes", ["自由触发"]), ["自由触发"])
        modes = {_normalize_mode(item) for item in raw}
        modes.discard("")
        return modes or {WAKE_MODE_CONTAINS}

    def global_blacklist(self) -> set[str]:
        return set(read_list(cfg(self.config, "wake", "global_blacklist", []), []))

    def wake_prefixes(self) -> list[str]:
        return core_wake_prefixes(self.context)

    def block_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wake", "block_enabled", True), True)

    def wake_cd(self) -> float:
        return read_float(cfg(self.config, "wake", "wake_cd", 0.5), 0.5, minimum=0.0, maximum=60.0)

    def block_qqbot(self) -> bool:
        return read_bool(cfg(self.config, "wake", "block_qqbot", True), True)

    def block_reread(self) -> bool:
        return read_bool(cfg(self.config, "wake", "block_reread", True), True)

    def default_block_keywords(self) -> list[str]:
        if self._default_block_keywords_cache is not None:
            return self._default_block_keywords_cache
        try:
            raw = json.loads(DEFAULT_BLOCK_WORDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        self._default_block_keywords_cache = self._dedupe_words(raw if isinstance(raw, list) else [])
        return self._default_block_keywords_cache

    def block_keywords(self) -> list[str]:
        raw = cfg(self.config, "wake", "block_keywords", None)
        if raw is None:
            return self.default_block_keywords()
        if isinstance(raw, list):
            return self._dedupe_words(raw)
        return self._dedupe_words(read_list(raw, []))

    def _migrate_editable_block_keywords(self) -> None:
        wake_config = section(self.config, "wake")
        if not isinstance(wake_config, dict):
            return
        changed = False

        if "use_default_block_keywords" in wake_config:
            use_defaults = read_bool(wake_config.pop("use_default_block_keywords"), True)
            current = wake_config.get("block_keywords", [])
            current_words = self._dedupe_words(current if isinstance(current, list) else read_list(current, []))
            if use_defaults:
                wake_config["block_keywords"] = self._dedupe_words([*self.default_block_keywords(), *current_words])
            else:
                wake_config["block_keywords"] = current_words
            wake_config["block_keywords_initialized"] = True
            changed = True

        initialized = read_bool(wake_config.get("block_keywords_initialized"), False)
        current = wake_config.get("block_keywords", None)
        current_words = self._dedupe_words(current if isinstance(current, list) else read_list(current, []))
        if not initialized:
            wake_config["block_keywords"] = current_words or list(self.default_block_keywords())
            wake_config["block_keywords_initialized"] = True
            changed = True

        if changed:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    saver()
                except Exception:
                    pass

    @staticmethod
    def _dedupe_words(values: list[Any]) -> list[str]:
        seen: set[str] = set()
        words: list[str] = []
        for value in values:
            word = clean_text(value)
            if word and word not in seen:
                seen.add(word)
                words.append(word)
        return words

    def command_block_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wake", "command_block_enabled", True), True)

    def builtin_commands(self) -> list[str]:
        return read_list(cfg(self.config, "wake", "builtin_commands", DEFAULT_BUILTIN_COMMANDS), DEFAULT_BUILTIN_COMMANDS)

    def block_builtin_commands(self) -> bool:
        return read_bool(cfg(self.config, "wake", "block_builtin_commands", False), False)

    def block_prefix_commands(self) -> bool:
        return read_bool(cfg(self.config, "wake", "block_prefix_commands", False), False)

    def block_prefix_llm(self) -> bool:
        return read_bool(cfg(self.config, "wake", "block_prefix_llm", False), False)

    def debounce_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wake", "debounce_enabled", True), True)

    def debounce_listen_seconds(self) -> float:
        return read_float(cfg(self.config, "wake", "debounce_listen_seconds", 3.0), 3.0, minimum=0.0, maximum=60.0)

    def debounce_max_merge_count(self) -> int:
        return read_int(cfg(self.config, "wake", "debounce_max_merge_count", 3), 3, minimum=0, maximum=50)

    def debounce_message_types(self) -> set[str]:
        return set(read_list(cfg(self.config, "wake", "debounce_message_types", ["normal", "at", "reply"]), ["normal", "at", "reply"]))

    def has_at_bot(self, event: Any) -> bool:
        if not self.at_wake_enabled():
            return False
        bot_id = _event_self_id(event)
        if not bot_id:
            return False
        return any(isinstance(seg, Comp.At) and clean_text(getattr(seg, "qq", "")) == bot_id for seg in _event_messages(event))

    def has_reply_to_bot(self, event: Any) -> bool:
        bot_id = _event_self_id(event)
        if not bot_id:
            return False
        for seg in _event_messages(event):
            if isinstance(seg, Comp.Reply) and clean_text(getattr(seg, "sender_id", "")) == bot_id:
                return True
        return False

    def match_wake_word(self, text: str, *, is_admin: bool) -> WakeMatch:
        normalized_text = clean_text(text)
        if not normalized_text:
            return WakeMatch()
        candidates: list[tuple[str, bool]] = [(word, False) for word in self.wake_words()]
        if is_admin:
            candidates.extend((word, True) for word in self.admin_wake_words())
        modes = self.trigger_modes()
        for word, admin_only in candidates:
            word = clean_text(word)
            if not word:
                continue
            if WAKE_MODE_PREFIX in modes and normalized_text.startswith(word):
                return WakeMatch(True, word, WAKE_MODE_PREFIX, admin_only)
            if WAKE_MODE_SUFFIX in modes and normalized_text.endswith(word):
                return WakeMatch(True, word, WAKE_MODE_SUFFIX, admin_only)
            if WAKE_MODE_CONTAINS in modes and word in normalized_text:
                return WakeMatch(True, word, WAKE_MODE_CONTAINS, admin_only)
        return WakeMatch()

    async def apply(self, event: Any) -> str:
        if not self.enabled():
            return ""
        sender_id = _event_sender_id(event)
        if sender_id and sender_id == _event_self_id(event):
            return ""
        blacklist_result = self._apply_global_blacklist(event)
        if blacklist_result:
            return blacklist_result

        debounce = await self._try_debounce_follow_up(event)
        if debounce.merged:
            await self._activate_debounce_window(event, debounce.merged_count)
            self._mark_last_wake(event)
            return "debounce"

        block_reason = self._apply_block(event)
        if block_reason:
            return block_reason

        text = clean_text(getattr(event, "message_str", ""))
        has_at = self.has_at_bot(event)
        match = self.match_wake_word(text, is_admin=_is_admin(event))
        if has_at or match.matched:
            event.is_wake = True
            event.is_at_or_wake_command = True
            if match.matched and self.strip_prefix_suffix_word():
                self._strip_matched_word(event, match)

        command_reason = self._apply_command_block(event)
        if command_reason:
            return command_reason

        if self.disable_reply_wake() and self.has_reply_to_bot(event) and not has_at and not match.matched:
            event.is_at_or_wake_command = False
            return "reply_wake_disabled"

        if bool(getattr(event, "is_at_or_wake_command", False)):
            self._mark_last_wake(event)
            await self._activate_debounce_window(event, 1)
            return "wake"
        return ""

    async def on_decorating_result(self, event: Any) -> None:
        sender_id = _event_sender_id(event)
        if sender_id:
            await self._clear_pending(self._pending_key(event))
        result_getter = getattr(event, "get_result", None)
        result = result_getter() if callable(result_getter) else None
        if result is None:
            return
        text = ""
        plain_getter = getattr(result, "get_plain_text", None)
        if callable(plain_getter):
            text = clean_text(plain_getter())
        if text:
            self._bot_messages[self._conversation_key(event)].append(text)

    async def stop(self) -> None:
        async with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            task = pending.cleanup_task
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _apply_global_blacklist(self, event: Any) -> str:
        blacklist = self.global_blacklist()
        if not blacklist:
            return ""
        values = (clean_text(getattr(event, "unified_msg_origin", "")), _event_sender_id(event), _event_group_id(event))
        if any(value and value in blacklist for value in values):
            self._stop_event(event)
            return "global_blacklist"
        return ""

    def _apply_block(self, event: Any) -> str:
        if not self.block_enabled():
            return ""
        sender_id = _event_sender_id(event)
        text = clean_text(getattr(event, "message_str", "")) or _plain_text_from_chain(_event_messages(event))
        wake_cd = self.wake_cd()
        if wake_cd > 0 and sender_id:
            last_wake = self._last_wake.get(self._wake_key(event), 0.0)
            if last_wake and time.time() - last_wake < wake_cd:
                self._stop_event(event)
                return "wake_cd"
        if self.block_qqbot() and _event_platform_name(event) == "aiocqhttp" and _is_qqbot_id(sender_id):
            self._stop_event(event)
            return "qqbot"
        if self.block_reread() and text:
            cleaned = _clean_for_compare(text)
            if cleaned:
                for bot_msg in self._bot_messages[self._conversation_key(event)]:
                    if cleaned == _clean_for_compare(bot_msg):
                        self._stop_event(event)
                        return "reread"
        if text:
            for word in self.block_keywords():
                if word and word in text:
                    self._stop_event(event)
                    return "block_keyword"
        return ""

    def _apply_command_block(self, event: Any) -> str:
        if not self.command_block_enabled():
            return ""
        command = self._detect_command(event)
        builtin_command = self._detect_builtin_command(event)
        if self.block_builtin_commands() and builtin_command:
            self._stop_event(event)
            return "builtin_command"

        prefix_triggered = self._is_prefix_triggered(event)
        if prefix_triggered and command and self.block_prefix_commands():
            self._stop_event(event)
            return "prefix_command"
        if prefix_triggered and not command and self.block_prefix_llm():
            self._stop_event(event)
            return "prefix_llm"
        return ""

    async def _try_debounce_follow_up(self, event: Any) -> DebounceResult:
        if not self.debounce_enabled() or self.debounce_listen_seconds() <= 0 or not _event_sender_id(event):
            return DebounceResult()
        pending = await self._claim_pending(self._pending_key(event), event)
        if pending is None:
            return DebounceResult()
        current_chain = list(_event_messages(event))
        if self._contains_gif(pending.chain) or self._contains_gif(current_chain):
            return DebounceResult(skip_reason="gif")
        self._stop_previous_event(pending.event)
        self._detach_active_runner(clean_text(getattr(event, "unified_msg_origin", "")))
        merged_chain = self._merge_chain(pending.chain, current_chain)
        merged_plain = self._merge_plain(pending.plain, clean_text(getattr(event, "message_str", "")) or _plain_text_from_chain(current_chain))
        self._apply_merged_message(event, merged_chain, merged_plain)
        event.is_wake = True
        event.is_at_or_wake_command = True
        merged_count = pending.merged_count + 1
        return DebounceResult(True, merged_count=merged_count)

    async def _activate_debounce_window(self, event: Any, merged_count: int) -> None:
        if not self.debounce_enabled() or self.debounce_listen_seconds() <= 0 or not _event_sender_id(event):
            return
        chain = list(_event_messages(event))
        if self._contains_gif(chain):
            return
        if merged_count <= 1:
            message_type = self._detect_message_type(event)
            if message_type not in self.debounce_message_types():
                return
        if not self._should_continue_listening(merged_count):
            return
        key = self._pending_key(event)
        pending = PendingWakeRequest(
            event=event,
            chain=chain,
            plain=clean_text(getattr(event, "message_str", "")) or _plain_text_from_chain(chain),
            created_at=time.time(),
            merged_count=merged_count,
        )
        await self._register_pending(key, pending)

    async def _claim_pending(self, key: str, current_event: Any) -> PendingWakeRequest | None:
        now = time.time()
        async with self._pending_lock:
            pending = self._pending.get(key)
            if pending is None:
                return None
            if pending.event is current_event:
                return None
            if now - pending.created_at > self.debounce_listen_seconds():
                self._pop_pending_unlocked(key)
                return None
            if bool(getattr(pending.event, "is_stopped", lambda: False)()) or bool(getattr(pending.event, "_has_send_oper", False)):
                self._pop_pending_unlocked(key)
                return None
            return self._pop_pending_unlocked(key)

    async def _register_pending(self, key: str, pending: PendingWakeRequest) -> None:
        async with self._pending_lock:
            self._pop_pending_unlocked(key)
            pending.cleanup_task = asyncio.create_task(self._expire_pending(key, pending.event, self.debounce_listen_seconds()))
            self._pending[key] = pending

    async def _clear_pending(self, key: str) -> bool:
        async with self._pending_lock:
            return self._pop_pending_unlocked(key) is not None

    def _pop_pending_unlocked(self, key: str) -> PendingWakeRequest | None:
        pending = self._pending.pop(key, None)
        if pending and pending.cleanup_task and not pending.cleanup_task.done():
            pending.cleanup_task.cancel()
        return pending

    async def _expire_pending(self, key: str, event: Any, window: float) -> None:
        try:
            await asyncio.sleep(window)
            async with self._pending_lock:
                pending = self._pending.get(key)
                if pending and pending.event is event:
                    self._pop_pending_unlocked(key)
        except asyncio.CancelledError:
            return

    def _registered_commands(self) -> set[str]:
        if self._commands_cache is not None:
            return self._commands_cache
        commands: set[str] = set()
        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.filter.command_group import CommandGroupFilter
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception:
            self._commands_cache = commands
            return commands
        for handler in star_handlers_registry:
            for flt in getattr(handler, "event_filters", []):
                if isinstance(flt, CommandFilter):
                    commands.add(clean_text(getattr(flt, "command_name", "")))
                    break
                if isinstance(flt, CommandGroupFilter):
                    commands.add(clean_text(getattr(flt, "group_name", "")))
                    break
        commands.update(self.builtin_commands())
        commands.discard("")
        self._commands_cache = commands
        return commands

    def _detect_command(self, event: Any) -> str:
        registered_commands = self._registered_commands()
        for message in self._command_message_candidates(event):
            first_arg = message.split(None, 1)[0]
            if first_arg in registered_commands:
                return first_arg
        return ""

    def _detect_builtin_command(self, event: Any) -> str:
        for message in self._command_message_candidates(event):
            for command in sorted(self.builtin_commands(), key=len, reverse=True):
                command = clean_text(command)
                if command and (message == command or message.startswith(command + " ")):
                    return command
        return ""

    def _command_message_candidates(self, event: Any) -> list[str]:
        message = clean_text(getattr(event, "message_str", ""))
        if not message:
            return []
        candidates = [message]
        stripped = self._strip_wake_prefix(message)
        if stripped and stripped != message:
            candidates.append(stripped)
        return candidates

    def _strip_wake_prefix(self, message: str) -> str:
        for prefix in sorted(self.wake_prefixes(), key=len, reverse=True):
            prefix = clean_text(prefix)
            if prefix and message.startswith(prefix):
                return message[len(prefix) :].lstrip()
        return message

    def _is_prefix_triggered(self, event: Any) -> bool:
        first_plain = ""
        messages = _event_messages(event)
        if messages and isinstance(messages[0], Comp.Plain):
            first_plain = clean_text(getattr(messages[0], "text", ""))
        text = first_plain or clean_text(getattr(event, "message_str", ""))
        return bool(text) and any(prefix and text.startswith(prefix) for prefix in self.wake_prefixes())

    def _detect_message_type(self, event: Any) -> str:
        if self._detect_command(event):
            return "command"
        bot_id = _event_self_id(event)
        chain = _event_messages(event)
        if bot_id and any(isinstance(seg, Comp.At) and clean_text(getattr(seg, "qq", "")) == bot_id for seg in chain):
            return "at"
        if bot_id and any(isinstance(seg, Comp.Reply) and clean_text(getattr(seg, "sender_id", "")) == bot_id for seg in chain):
            return "reply"
        return "normal"

    def _should_continue_listening(self, merged_count: int) -> bool:
        max_count = self.debounce_max_merge_count()
        return max_count <= 0 or merged_count < max_count

    @staticmethod
    def _merge_plain(previous_plain: str, current_plain: str) -> str:
        parts = [part.strip() for part in (previous_plain, current_plain) if part.strip()]
        return "\n".join(parts)

    @staticmethod
    def _merge_chain(previous_chain: list[Any], current_chain: list[Any]) -> list[Any]:
        merged = list(previous_chain)
        if merged and current_chain:
            try:
                merged.append(Comp.Plain("\n", convert=False))
            except TypeError:
                merged.append(Comp.Plain("\n"))
        merged.extend(current_chain)
        return merged

    @staticmethod
    def _apply_merged_message(event: Any, chain: list[Any], plain: str) -> None:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            try:
                message_obj.message = list(chain)
                message_obj.message_str = plain
            except Exception:
                pass
        event.message_str = plain

    @staticmethod
    def _contains_gif(chain: list[Any]) -> bool:
        return any(WakeService._is_gif_image(seg) for seg in chain)

    @staticmethod
    def _is_gif_image(seg: Any) -> bool:
        if not isinstance(seg, Comp.Image):
            return False
        refs = (
            getattr(seg, "path", None),
            getattr(seg, "url", None),
            getattr(seg, "file", None),
        )
        return any(WakeService._is_gif_ref(clean_text(ref)) for ref in refs)

    @staticmethod
    def _is_gif_ref(ref: str) -> bool:
        if not ref:
            return False
        parsed_ref = ref
        lowered = ref.lower()
        if lowered.startswith(("http://", "https://")):
            parsed_ref = urlparse(ref).path
        elif lowered.startswith("file:///"):
            parsed_ref = ref[8:]
        return Path(parsed_ref).suffix.lower() == ".gif"

    @staticmethod
    def _stop_previous_event(event: Any) -> None:
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("agent_stop_requested", True)
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()

    @staticmethod
    def _detach_active_runner(umo: str) -> None:
        if not umo:
            return
        try:
            from astrbot.core.pipeline.process_stage import follow_up as process_follow_up
        except Exception:
            return
        active_runners = getattr(process_follow_up, "_ACTIVE_AGENT_RUNNERS", None)
        if isinstance(active_runners, dict):
            active_runners.pop(umo, None)

    @staticmethod
    def _stop_event(event: Any) -> None:
        event.is_at_or_wake_command = False
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()

    def _pending_key(self, event: Any) -> str:
        return f"{clean_text(getattr(event, 'unified_msg_origin', ''))}:{_event_sender_id(event)}"

    def _wake_key(self, event: Any) -> str:
        return f"{self._conversation_key(event)}:{_event_sender_id(event)}"

    @staticmethod
    def _conversation_key(event: Any) -> str:
        return _event_group_id(event) or clean_text(getattr(event, "unified_msg_origin", "")) or _event_sender_id(event)

    def _mark_last_wake(self, event: Any) -> None:
        key = self._wake_key(event)
        if key.strip(":"):
            self._last_wake[key] = time.time()

    @staticmethod
    def _strip_matched_word(event: Any, match: WakeMatch) -> None:
        text = clean_text(getattr(event, "message_str", ""))
        if not text or not match.word:
            return
        if match.mode == WAKE_MODE_PREFIX and text.startswith(match.word):
            event.message_str = text[len(match.word) :].strip()
        elif match.mode == WAKE_MODE_SUFFIX and text.endswith(match.word):
            event.message_str = text[: -len(match.word)].strip()
