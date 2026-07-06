from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, read_bool, read_list


WAKE_MODE_CONTAINS = "contains"
WAKE_MODE_PREFIX = "prefix"
WAKE_MODE_SUFFIX = "suffix"


@dataclass(slots=True)
class WakeMatch:
    matched: bool = False
    word: str = ""
    mode: str = ""
    admin_only: bool = False


def _event_sender_id(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    return clean_text(getter()) if callable(getter) else ""


def _event_group_id(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    return clean_text(getter()) if callable(getter) else ""


def _event_self_id(event: Any) -> str:
    getter = getattr(event, "get_self_id", None)
    return clean_text(getter()) if callable(getter) else ""


def _is_admin(event: Any) -> bool:
    checker = getattr(event, "is_admin", None)
    return bool(checker()) if callable(checker) else False


def _event_messages(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    messages = getter() if callable(getter) else []
    return messages if isinstance(messages, list) else []


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


class WakeService:
    def __init__(self, config: Any) -> None:
        self.config = config

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

    def apply(self, event: Any) -> str:
        if not self.enabled():
            return ""
        sender_id = _event_sender_id(event)
        if sender_id and sender_id == _event_self_id(event):
            return ""
        blacklist = self.global_blacklist()
        if blacklist and any(value in blacklist for value in (clean_text(getattr(event, "unified_msg_origin", "")), sender_id, _event_group_id(event))):
            stopper = getattr(event, "stop_event", None)
            if callable(stopper):
                stopper()
            return "blocked"

        text = clean_text(getattr(event, "message_str", ""))
        has_at = self.has_at_bot(event)
        match = self.match_wake_word(text, is_admin=_is_admin(event))
        if has_at or match.matched:
            event.is_wake = True
            event.is_at_or_wake_command = True
            if match.matched and self.strip_prefix_suffix_word():
                self._strip_matched_word(event, match)
            return "wake"

        if self.disable_reply_wake() and self.has_reply_to_bot(event):
            event.is_at_or_wake_command = False
            return "reply_wake_disabled"
        return ""

    @staticmethod
    def _strip_matched_word(event: Any, match: WakeMatch) -> None:
        text = clean_text(getattr(event, "message_str", ""))
        if not text or not match.word:
            return
        if match.mode == WAKE_MODE_PREFIX and text.startswith(match.word):
            event.message_str = text[len(match.word) :].strip()
        elif match.mode == WAKE_MODE_SUFFIX and text.endswith(match.word):
            event.message_str = text[: -len(match.word)].strip()
