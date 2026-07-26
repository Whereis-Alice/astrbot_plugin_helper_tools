from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import astrbot.api.message_components as Comp

from .helper_utils import cfg, clean_text, read_bool, read_int, truncate

CARD_SUMMARY_PREFIX = "[???????????????????"
_CARD_SUMMARY_SUFFIX = "]"
_MAX_COMPONENT_DEPTH = 8
_MAX_JSON_DEPTH = 8
_GENERIC_SOURCE_NAMES = {"??", "??", "??", "???", "??"}


@dataclass(frozen=True, slots=True)
class QuotedCardSummary:
    kind: str
    source: str = ""
    title: str = ""
    author: str = ""
    description: str = ""
    url: str = ""
    identifier: str = ""

    def render(self, *, include_urls: bool) -> str:
        fields = [
            ("??", self.kind),
            ("??", self.source),
            ("??", self.title),
            ("??/??", self.author),
            ("??", self.description),
            ("??", self.identifier),
        ]
        if include_urls:
            fields.append(("??", self.url))

        rendered: list[str] = []
        seen_values: set[str] = set()
        for label, value in fields:
            value = _normalize_value(value)
            if not value:
                continue
            dedupe_key = value.casefold()
            if dedupe_key in seen_values:
                continue
            seen_values.add(dedupe_key)
            rendered.append(f"{label}?{truncate(value, 500)}")
        return "?".join(rendered)


@dataclass(frozen=True, slots=True)
class ReplyCardReaderResult:
    enriched_reply_count: int = 0
    card_count: int = 0


class ReplyCardReader:
    """Make quoted structured cards readable to AstrBot's quoted-text parser."""

    def __init__(self, config: Any) -> None:
        self.config = config

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "reply_card_reader", "enabled", True), True)

    def enrich(self, event: Any) -> ReplyCardReaderResult:
        if not self.enabled():
            return ReplyCardReaderResult()

        enriched_reply_count = 0
        card_count = 0
        for component in self._event_messages(event):
            if not isinstance(component, Comp.Reply):
                continue
            cards = self._collect_reply_cards(component)
            if not cards:
                continue
            marker = self._build_marker(cards)
            if not self._append_marker(component, marker):
                continue
            enriched_reply_count += 1
            card_count += len(cards)

        return ReplyCardReaderResult(enriched_reply_count, card_count)

    def _build_marker(self, cards: list[QuotedCardSummary]) -> str:
        include_urls = read_bool(
            cfg(self.config, "reply_card_reader", "include_urls", True),
            True,
        )
        rendered_cards = [card.render(include_urls=include_urls) for card in cards]
        rendered_cards = [item for item in rendered_cards if item]
        if len(rendered_cards) == 1:
            body = rendered_cards[0]
        else:
            body = " | ".join(
                f"?? {index}?{item}"
                for index, item in enumerate(rendered_cards, start=1)
            )

        max_chars = read_int(
            cfg(self.config, "reply_card_reader", "max_summary_chars", 1200),
            1200,
            minimum=200,
            maximum=5000,
        )
        body_limit = max(
            1, max_chars - len(CARD_SUMMARY_PREFIX) - len(_CARD_SUMMARY_SUFFIX)
        )
        return (
            f"{CARD_SUMMARY_PREFIX}{truncate(body, body_limit)}{_CARD_SUMMARY_SUFFIX}"
        )

    @staticmethod
    def _event_messages(event: Any) -> list[Any]:
        getter = getattr(event, "get_messages", None)
        messages = getter() if callable(getter) else []
        return messages if isinstance(messages, list) else []

    @classmethod
    def _collect_reply_cards(cls, reply: Any) -> list[QuotedCardSummary]:
        chain = getattr(reply, "chain", None)
        if not isinstance(chain, list):
            return []

        cards: list[QuotedCardSummary] = []
        seen_cards: set[QuotedCardSummary] = set()
        for component in cls._walk_components(chain):
            card = cls._card_from_component(component)
            if card is None or card in seen_cards:
                continue
            seen_cards.add(card)
            cards.append(card)
        return cards

    @classmethod
    def _walk_components(
        cls,
        chain: list[Any],
        *,
        depth: int = 0,
        seen: set[int] | None = None,
    ) -> Iterator[Any]:
        if depth > _MAX_COMPONENT_DEPTH:
            return
        if seen is None:
            seen = set()

        for component in chain:
            component_id = id(component)
            if component_id in seen:
                continue
            seen.add(component_id)
            yield component

            nested_chain = getattr(component, "chain", None)
            if isinstance(nested_chain, list):
                yield from cls._walk_components(
                    nested_chain,
                    depth=depth + 1,
                    seen=seen,
                )

            content = getattr(component, "content", None)
            if isinstance(content, list):
                yield from cls._walk_components(
                    content,
                    depth=depth + 1,
                    seen=seen,
                )

            nodes = getattr(component, "nodes", None)
            if isinstance(nodes, list):
                for node in nodes:
                    node_content = getattr(node, "content", None)
                    if isinstance(node_content, list):
                        yield from cls._walk_components(
                            node_content,
                            depth=depth + 1,
                            seen=seen,
                        )

    @classmethod
    def _card_from_component(cls, component: Any) -> QuotedCardSummary | None:
        if isinstance(component, Comp.Json):
            return cls._json_card(component)
        if isinstance(component, Comp.Music):
            return cls._music_card(component)
        if isinstance(component, Comp.Share):
            return QuotedCardSummary(
                kind="????",
                title=_normalize_value(getattr(component, "title", "")),
                description=_normalize_value(getattr(component, "content", "")),
                url=_normalize_value(getattr(component, "url", "")),
            )
        if isinstance(component, Comp.Location):
            lat = _normalize_value(getattr(component, "lat", ""))
            lon = _normalize_value(getattr(component, "lon", ""))
            return QuotedCardSummary(
                kind="????",
                title=_normalize_value(getattr(component, "title", "")),
                description=_normalize_value(getattr(component, "content", "")),
                identifier=", ".join(item for item in (lat, lon) if item),
            )
        if isinstance(component, Comp.Contact):
            return QuotedCardSummary(
                kind="???????",
                identifier=_normalize_value(getattr(component, "id", "")),
            )
        return None

    @classmethod
    def _json_card(cls, component: Any) -> QuotedCardSummary | None:
        data = cls._normalize_json_data(getattr(component, "data", None))
        if not data:
            return None

        app = _normalize_value(data.get("app"))
        if app.casefold() == "com.tencent.multimsg":
            # AstrBot already has dedicated extraction for merged-forward JSON.
            return None

        mappings = cls._prioritized_mappings(data)
        prompt = _normalize_value(data.get("prompt"))
        root_description = _normalize_value(data.get("desc"))
        source = cls._pick_value(
            mappings,
            ("tag", "source_name", "sourceName", "app_name", "appName", "platform"),
        )
        if not source:
            source = root_description
        if source in _GENERIC_SOURCE_NAMES:
            source = ""

        kind = cls._json_card_kind(data, mappings, prompt, root_description)
        title = cls._pick_value(mappings, ("title", "name", "headline"))
        if not title:
            title = cls._strip_prompt_label(prompt)

        description = cls._pick_value(
            mappings,
            ("desc", "description", "summary", "content", "subtitle", "subTitle"),
        )
        author = cls._pick_value(
            mappings,
            (
                "author",
                "author_name",
                "authorName",
                "nickname",
                "nick",
                "uname",
                "userName",
            ),
        )
        if kind == "????" and not author and description:
            author, description = description, ""

        url = cls._pick_value(
            mappings,
            (
                "jumpUrl",
                "jump_url",
                "qqdocurl",
                "targetUrl",
                "target_url",
                "shareUrl",
                "share_url",
                "url",
                "sourceUrl",
                "source_url",
            ),
        )
        technical_app = (
            "" if app in {"com.tencent.structmsg", "com.tencent.miniapp_01"} else app
        )
        if not source:
            source = technical_app

        return QuotedCardSummary(
            kind=kind,
            source=source,
            title=title,
            author=author,
            description=description,
            url=url,
        )

    @staticmethod
    def _music_card(component: Any) -> QuotedCardSummary:
        url = _normalize_value(getattr(component, "url", ""))
        source = ""
        lowered_url = url.casefold()
        if "music.163.com" in lowered_url or "y.music.163.com" in lowered_url:
            source = "?????"
        elif "y.qq.com" in lowered_url or "c.y.qq.com" in lowered_url:
            source = "QQ??"

        return QuotedCardSummary(
            kind="????",
            source=source,
            title=_normalize_value(getattr(component, "title", "")),
            author=_normalize_value(getattr(component, "content", "")),
            url=url,
            identifier=_normalize_value(getattr(component, "id", "")),
        )

    @staticmethod
    def _normalize_json_data(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            nested = value.get("data")
            if len(value) == 1 and isinstance(nested, str):
                return ReplyCardReader._normalize_json_data(nested)
            return value
        if not isinstance(value, str):
            return {}
        try:
            decoded = json.loads(value.replace("&#44;", ","))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _prioritized_mappings(
        cls,
        data: dict[str, Any],
    ) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
        mappings = list(cls._walk_json_mappings(data))
        return sorted(
            mappings,
            key=lambda item: (
                0 if "meta" in {part.casefold() for part in item[0]} else 1,
                1 if not item[0] else 0,
                len(item[0]),
            ),
        )

    @classmethod
    def _walk_json_mappings(
        cls,
        value: Any,
        *,
        path: tuple[str, ...] = (),
        depth: int = 0,
    ) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
        if depth > _MAX_JSON_DEPTH:
            return
        if isinstance(value, dict):
            yield path, value
            for key, nested in value.items():
                yield from cls._walk_json_mappings(
                    nested,
                    path=(*path, str(key)),
                    depth=depth + 1,
                )
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                yield from cls._walk_json_mappings(
                    nested,
                    path=(*path, str(index)),
                    depth=depth + 1,
                )

    @staticmethod
    def _pick_value(
        mappings: list[tuple[tuple[str, ...], dict[str, Any]]],
        aliases: tuple[str, ...],
    ) -> str:
        normalized_aliases = [_normalize_key(alias) for alias in aliases]
        for alias in normalized_aliases:
            for _path, mapping in mappings:
                for key, value in mapping.items():
                    if _normalize_key(key) != alias:
                        continue
                    text = _normalize_value(value)
                    if text:
                        return text
        return ""

    @staticmethod
    def _json_card_kind(
        data: dict[str, Any],
        mappings: list[tuple[tuple[str, ...], dict[str, Any]]],
        prompt: str,
        root_description: str,
    ) -> str:
        path_text = " ".join(part for path, _mapping in mappings for part in path)
        hints = " ".join(
            (
                _normalize_value(data.get("app")),
                _normalize_value(data.get("view")),
                prompt,
                root_description,
                path_text,
            )
        ).casefold()
        if "music" in hints or "??" in hints:
            return "????"
        if "miniapp" in hints or "???" in hints or "detail_1" in hints:
            return "?????"
        return "????"

    @staticmethod
    def _strip_prompt_label(prompt: str) -> str:
        return re.sub(r"^(?:\[[^\]]{1,24}\]|?[^?]{1,24}?)\s*", "", prompt).strip()

    @staticmethod
    def _append_marker(reply: Any, marker: str) -> bool:
        chain = getattr(reply, "chain", None)
        if not isinstance(chain, list):
            return False
        if any(
            isinstance(component, Comp.Plain)
            and clean_text(getattr(component, "text", "")).startswith(
                CARD_SUMMARY_PREFIX
            )
            for component in chain
        ):
            return False
        chain.append(Comp.Plain(marker))
        return True


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_text(value).casefold())


def _normalize_value(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    text = html.unescape(clean_text(value).replace("&#44;", ","))
    return re.sub(r"\s+", " ", text).strip()
