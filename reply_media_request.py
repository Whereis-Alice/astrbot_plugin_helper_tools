"""Attach quote provenance to images in an already permitted, prepared request.

Call after the wake guard and final image preparation, before runner.reset().
ReplyMediaRequestPipeline automatically publishes the core's ``prepared_images``
cache as ``request._helper_tools_image_ref_map``. Guard-recorded resolver return
paths bridge late HTTP downloads to that cache. Independent callers may supply
the same exact mapping (original reference -> prepared reference or {"path": ref}).
This module never downloads, changes wake state, or guesses image positions.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes, urlsplit

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import ImageURLPart, TextPart

PROVENANCE_MARKER = "[HelperTools 引用图片来源]"
UNAVAILABLE_MARKER = "[HelperTools 引用图片不可见]"
IMAGE_REF_MAP_ATTRIBUTE = "_helper_tools_image_ref_map"
_WAKE_BLOCK_KEY = "helper_tools_wake_block_llm_request"


@dataclass(frozen=True)
class _Origin:
    sender_id: str
    message_id: str
    is_bot: bool | None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _sender_id(value: Any) -> str:
    text = _text(value)
    return "" if text == "0" else text


def _image_ref(part: Any) -> str:
    if isinstance(part, ImageURLPart):
        return part.image_url.url
    if isinstance(part, dict) and part.get("type") == "image_url":
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            return _text(image_url.get("url"))
    return ""


def _component_refs(image: Any) -> list[str]:
    refs = [
        ref
        for attr in ("url", "file", "path")
        if (ref := _text(getattr(image, attr, "")))
    ]
    resolved = getattr(image, "_helper_tools_resolved_refs", ())
    if isinstance(resolved, (tuple, list)):
        refs.extend(
            ref.strip() for ref in resolved if isinstance(ref, str) and ref.strip()
        )
    return list(dict.fromkeys(refs))


def _reply_origin(reply: Any, self_id: str) -> _Origin:
    sender = _sender_id(
        getattr(reply, "_helper_tools_quote_sender_id", None)
        if hasattr(reply, "_helper_tools_quote_sender_id")
        else _sender_id(getattr(reply, "sender_id", "")) or getattr(reply, "qq", "")
    )
    message_id = _text(
        getattr(reply, "_helper_tools_quote_message_id", getattr(reply, "id", ""))
    )
    is_bot = getattr(reply, "_helper_tools_quote_is_bot", None)
    if not isinstance(is_bot, bool) or (not sender and is_bot is False):
        is_bot = sender == self_id if sender and self_id else None
    return _Origin(sender, message_id, is_bot)


def _quote_images(chain: list[Any], self_id: str) -> list[tuple[Any, _Origin]]:
    """Walk quote chains while retaining each nested author's scope.

    Args:
        chain: Incoming event components; direct images are excluded.
        self_id: Current bot identifier.

    Returns:
        Quoted images paired with their nearest quote or forward-node origin.
    """
    images: list[tuple[Any, _Origin]] = []

    def visit(parts: list[Any], origin: _Origin | None, depth: int) -> None:
        if depth > 16:
            return
        for part in parts:
            if isinstance(part, Comp.Reply):
                nested_chain = getattr(part, "chain", None)
                if isinstance(nested_chain, list):
                    visit(nested_chain, _reply_origin(part, self_id), depth + 1)
            elif isinstance(part, Comp.Image) and origin is not None:
                sender = _sender_id(
                    getattr(part, "_helper_tools_quote_sender_id", origin.sender_id)
                )
                message_id = _text(
                    getattr(part, "_helper_tools_quote_message_id", origin.message_id)
                )
                is_bot = getattr(part, "_helper_tools_quote_is_bot", None)
                if not isinstance(is_bot, bool) or (not sender and is_bot is False):
                    is_bot = sender == self_id if sender and self_id else None
                images.append((part, _Origin(sender, message_id, is_bot)))
            elif origin is not None and isinstance(part, Comp.Node):
                sender = _sender_id(getattr(part, "uin", ""))
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    visit(
                        content,
                        _Origin(
                            sender,
                            "",
                            sender == self_id if sender and self_id else None,
                        ),
                        depth + 1,
                    )
            elif origin is not None and isinstance(part, Comp.Nodes):
                visit(list(getattr(part, "nodes", []) or []), origin, depth + 1)

    visit(chain, None, 0)
    return images


def _local_identity(ref: str) -> set[str]:
    """Read exact reference/byte identities without fetching remote media.

    Args:
        ref: A local path, file URI, data URI, base64 reference, or remote URL.

    Returns:
        Exact keys suitable for matching; failed reads retain the reference key.
    """
    keys = {"ref:" + ref}
    data: bytes | None = None
    try:
        if ref.startswith("data:"):
            header, payload = ref.split(",", 1)
            data = (
                base64.b64decode(
                    b"".join(unquote_to_bytes(payload).split()), validate=True
                )
                if ";base64" in header.lower()
                else unquote_to_bytes(payload)
            )
        elif ref.startswith("base64://"):
            data = base64.b64decode("".join(ref[9:].split()), validate=True)
        else:
            parsed = urlsplit(ref)
            if parsed.scheme.lower() in {"http", "https"}:
                return keys
            if parsed.scheme.lower() == "file":
                path_text = unquote(parsed.path)
                if parsed.netloc and parsed.netloc.lower() != "localhost":
                    # Do not open UNC/network shares while identifying provenance.
                    return keys
                if os.name == "nt" and path_text.startswith("/"):
                    path_text = path_text[1:]
            elif not parsed.scheme or (len(parsed.scheme) == 1 and ref[1:2] == ":"):
                path_text = ref
            else:
                return keys
            if path_text.startswith(("\\\\", "//")):
                return keys
            path = Path(path_text).resolve(strict=False)
            keys.add("path:" + os.path.normcase(str(path)))
            if path.is_file():
                data = path.read_bytes()
    except (OSError, ValueError, binascii.Error):
        pass
    if data is not None:
        keys.add("sha256:" + hashlib.sha256(data).hexdigest())
    return keys


def _provenance_text(origins: list[_Origin], shared_with_current: bool) -> str:
    lines = [PROVENANCE_MARKER, "以下说明仅适用于紧随其后的这一张图片。"]
    for origin in origins:
        if origin.is_bot is True:
            source = "这是 bot 自己先前发送的引用图片"
            if not shared_with_current:
                source += "，不是本轮用户新上传的图片"
        elif origin.is_bot is False:
            source = "这是其它用户先前发送的引用图片，不是 bot 自己发送的图片"
        else:
            source = "引用图片来源未知，无法确认是 bot 还是其它用户发送"
        # Identifiers are data, never instructions or unbounded quote text.
        sender = origin.sender_id.replace("\n", " ").replace("\r", " ")[:128] or "未知"
        message = (
            origin.message_id.replace("\n", " ").replace("\r", " ")[:128] or "未知"
        )
        lines.append(f"引用发送者：{sender}；引用消息 ID：{message}；{source}。")
    if shared_with_current:
        lines.append("同一图片也出现在当前用户本轮新发图片中；它同时具有上述引用来源。")
    return "\n".join(lines)


async def annotate_quoted_images(event: Any, request: Any) -> int:
    """Insert adjacent, temporary provenance for exactly matched quote images.

    Args:
        event: An event already permitted by the existing wake rules. Guard-added
            image origin attributes override the enclosing Reply metadata.
        request: Final ProviderRequest before runner.reset(). Prepared aliases may
            be supplied through IMAGE_REF_MAP_ATTRIBUTE on request or event extras.

    Returns:
        Number of image blocks whose provenance was inserted or updated. A repeated
        call returns zero. Unmatched inputs are preserved without guessed labels.

    Notes:
        All image_urls are faithfully moved into extra parts when any quote matches,
        preserving visual input order. Only quote-only image/label pairs are temporary;
        images also present in the current user's direct chain retain their save flag.
        This function does not hydrate replies, send messages, or invoke providers.
    """
    stopped = getattr(event, "is_stopped", None)
    if callable(stopped) and stopped():
        return 0
    extra_getter = getattr(event, "get_extra", None)
    if callable(extra_getter) and extra_getter(_WAKE_BLOCK_KEY, False):
        return 0
    if getattr(event, _WAKE_BLOCK_KEY, False):
        return 0
    parts = getattr(request, "extra_user_content_parts", None)
    urls = getattr(request, "image_urls", None)
    if not isinstance(parts, list) or not isinstance(urls, list):
        return 0
    getter = getattr(event, "get_messages", None)
    chain = getter() if callable(getter) else None
    if not isinstance(chain, list):
        chain = getattr(getattr(event, "message_obj", None), "message", [])
    if not isinstance(chain, list):
        return 0
    self_getter = getattr(event, "get_self_id", None)
    self_id = _sender_id(self_getter() if callable(self_getter) else "")
    quoted = _quote_images(chain, self_id)
    recorded_quotes = [
        (
            _reply_origin(reply, self_id),
            getattr(reply, "_helper_tools_quote_image_count", 0),
        )
        for reply in chain
        if isinstance(reply, Comp.Reply)
        and isinstance(getattr(reply, "_helper_tools_quote_image_count", None), int)
        and getattr(reply, "_helper_tools_quote_image_count", 0) > 0
    ]
    if not quoted and not recorded_quotes:
        return 0

    identities: dict[str, set[str]] = {}

    async def keys(ref: str) -> set[str]:
        if ref not in identities:
            identities[ref] = await asyncio.to_thread(_local_identity, ref)
        return identities[ref]

    aliases: list[set[str]] = []
    mappings = [getattr(request, IMAGE_REF_MAP_ATTRIBUTE, None)]
    if callable(extra_getter):
        mappings.append(extra_getter(IMAGE_REF_MAP_ATTRIBUTE, None))
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        for original, prepared in mapping.items():
            final = prepared.get("path") if isinstance(prepared, dict) else prepared
            if (
                isinstance(original, str)
                and isinstance(final, str)
                and original
                and final
            ):
                aliases.append((await keys(original)) | (await keys(final)))

    def expand(initial: set[str]) -> set[str]:
        expanded = set(initial)
        while True:
            before = len(expanded)
            for alias in aliases:
                if expanded & alias:
                    expanded.update(alias)
            if len(expanded) == before:
                return expanded

    quote_keys: list[tuple[set[str], _Origin]] = []
    for image, origin in quoted:
        image_keys: set[str] = set()
        for ref in _component_refs(image):
            image_keys.update(await keys(ref))
        if image_keys:
            quote_keys.append((expand(image_keys), origin))
    direct_keys: set[str] = set()
    for image in chain:
        if isinstance(image, Comp.Image):
            for ref in _component_refs(image):
                direct_keys.update(expand(await keys(ref)))

    refs = [_image_ref(part) for part in parts] + list(urls)
    matches: dict[str, tuple[list[_Origin], bool]] = {}
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            continue
        image_keys = expand(await keys(ref))
        origins = list(
            dict.fromkeys(
                origin for source, origin in quote_keys if source & image_keys
            )
        )
        if origins:
            matches[ref] = (origins, bool(image_keys & direct_keys))
    visible_origins = {
        origin for origins, _shared in matches.values() for origin in origins
    }
    unavailable_parts = []
    for origin, count in dict.fromkeys(recorded_quotes):
        if origin in visible_origins:
            continue
        text = _provenance_text([origin], False).split("\n", 2)[-1]
        unavailable_parts.append(
            TextPart(
                text=(
                    f"{UNAVAILABLE_MARKER}\n{text}\n"
                    f"来源记录含 {count} 张图片；本次无法确认该引用的可见图片像素。"
                    "不得根据来源记录假装已看到图片或编造画面内容；此说明不适用于本轮其它图片。"
                )
            ).mark_as_temp()
        )
    # Replace only this module's missing-pixel notes, including after late hydration.
    clean_parts = [
        part
        for part in parts
        if not (
            isinstance(part, TextPart)
            and part.text.startswith(UNAVAILABLE_MARKER + "\n")
        )
    ]
    if not matches:
        request.extra_user_content_parts[:] = clean_parts + unavailable_parts
        return 0

    # Existing extra images precede image_urls in the real assemble_context. Move
    # the complete URL lane, not just quotes, so animation indices remain correct.
    materialized = list(clean_parts)
    for ref in urls:
        if not isinstance(ref, str) or not ref:
            # Unsupported request entries are left untouched rather than discarded.
            return 0
        materialized.append(ImageURLPart(image_url=ImageURLPart.ImageURL(url=ref)))
    output: list[Any] = []
    changed = 0
    for part in materialized:
        match = matches.get(_image_ref(part))
        if match is None:
            output.append(part)
            continue
        origins, shared = match
        text = _provenance_text(origins, shared)
        previous = output[-1] if output else None
        previous_text = (
            previous.text
            if isinstance(previous, TextPart)
            else previous.get("text", "")
            if isinstance(previous, dict)
            else ""
        )
        if isinstance(previous_text, str) and previous_text.startswith(
            PROVENANCE_MARKER + "\n"
        ):
            output.pop()
            if previous_text != text:
                changed += 1
        else:
            changed += 1
        output.append(TextPart(text=text).mark_as_temp())
        if isinstance(part, ImageURLPart):
            if not shared:
                part.mark_as_temp()
        elif isinstance(part, dict) and not shared:
            part = {**part, "_no_save": True}
        output.append(part)
    request.extra_user_content_parts[:] = output + unavailable_parts
    request.image_urls[:] = []
    return changed
