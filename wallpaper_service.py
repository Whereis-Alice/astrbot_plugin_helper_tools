from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .helper_utils import cfg, clean_text, fetch_bytes, parse_dynamic_command, read_bool, read_int, read_list
from .qq_features import call_onebot


DEFAULT_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
SEND_MODE_TOGETHER = "together"
SEND_MODE_CAPTION_FIRST = "caption_first"
SEND_MODE_IMAGE_ONLY = "image_only"


@dataclass(slots=True)
class WallpaperLibrary:
    name: str
    path: Path
    commands: list[str]
    caption: str
    send_mode: str
    recursive: bool


@dataclass(slots=True)
class ImageRef:
    url: str = ""
    file: str = ""
    path: str = ""
    source: str = ""


@dataclass(slots=True)
class WallpaperHandleResult:
    handled: bool
    message: str = ""
    sent: bool = False


def _safe_filename_part(value: str, default: str = "wallpaper") -> str:
    text = clean_text(value, default)
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    safe = safe.strip("._")
    return safe or default


def _normalize_send_mode(value: Any) -> str:
    text = clean_text(value, SEND_MODE_TOGETHER).lower()
    if text in {SEND_MODE_TOGETHER, "同一条消息", "合并发送", "together"}:
        return SEND_MODE_TOGETHER
    if text in {SEND_MODE_CAPTION_FIRST, "先发文案再发图", "caption_first"}:
        return SEND_MODE_CAPTION_FIRST
    if text in {SEND_MODE_IMAGE_ONLY, "只发图片", "image_only"}:
        return SEND_MODE_IMAGE_ONLY
    return SEND_MODE_TOGETHER


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _path_from_file_uri(value: str) -> Path | None:
    if not value.startswith("file://"):
        return None
    parsed = urllib.parse.urlparse(value)
    try:
        return Path(urllib.request.url2pathname(urllib.parse.unquote(parsed.path))).resolve(strict=False)
    except OSError:
        return None


def _guess_extension(*values: str, content_type: str = "") -> str:
    content_type = content_type.lower()
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    for value in values:
        suffix = Path(urllib.parse.urlparse(clean_text(value)).path).suffix.lower()
        if suffix:
            return suffix
    return ".jpg"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WallpaperService:
    def __init__(self, config: Any, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir
        self.registry_path = self.data_dir / "wallpaper_sent_images.json"

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "enabled", True), True)

    def commands_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "commands_enabled", True), True)

    def add_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "add_enabled", True), True)

    def delete_enabled(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "delete_enabled", True), True)

    def mutate_admin_only(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "mutate_admin_only", True), True)

    def stop_after_response(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "stop_event_after_response", True), True)

    def deduplicate_on_add(self) -> bool:
        return read_bool(cfg(self.config, "wallpaper", "deduplicate_on_add", True), True)

    def max_add_bytes(self) -> int:
        return read_int(
            cfg(self.config, "wallpaper", "max_add_bytes", 20 * 1024 * 1024),
            20 * 1024 * 1024,
            minimum=64 * 1024,
            maximum=200 * 1024 * 1024,
        )

    def add_commands(self) -> list[str]:
        return read_list(cfg(self.config, "wallpaper", "add_commands", ["存图", "/存图"]), ["存图", "/存图"])

    def delete_commands(self) -> list[str]:
        return read_list(cfg(self.config, "wallpaper", "delete_commands", ["删图", "/删图"]), ["删图", "/删图"])

    def allowed_extensions(self) -> set[str]:
        values = read_list(cfg(self.config, "wallpaper", "allowed_extensions", DEFAULT_IMAGE_EXTENSIONS), DEFAULT_IMAGE_EXTENSIONS)
        return {ext if ext.startswith(".") else f".{ext}" for ext in (item.lower() for item in values)}

    def libraries(self) -> list[WallpaperLibrary]:
        raw = cfg(self.config, "wallpaper", "libraries", [])
        if not isinstance(raw, list):
            return []
        libraries: list[WallpaperLibrary] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = clean_text(item.get("name") or item.get("library_name"))
            if not name:
                continue
            path = self._resolve_library_path(name, clean_text(item.get("path")))
            commands = read_list(item.get("commands"), [f"/{name}"])
            caption = clean_text(item.get("caption"), "随机给你摸一张 {library}。")
            send_mode = _normalize_send_mode(item.get("send_mode"))
            recursive = read_bool(item.get("recursive"), False)
            libraries.append(
                WallpaperLibrary(
                    name=name,
                    path=path,
                    commands=commands or [f"/{name}"],
                    caption=caption,
                    send_mode=send_mode,
                    recursive=recursive,
                )
            )
        return libraries

    def _resolve_library_path(self, name: str, raw_path: str) -> Path:
        path = Path(raw_path).expanduser() if raw_path else Path("wallpapers") / _safe_filename_part(name)
        if not path.is_absolute():
            path = self.data_dir / path
        return path.resolve(strict=False)

    async def handle_message(self, event: Any, text: str) -> WallpaperHandleResult:
        if not self.enabled() or not self.commands_enabled():
            return WallpaperHandleResult(False)

        delete_match = parse_dynamic_command(text, self.delete_commands())
        if delete_match:
            if not self.delete_enabled():
                return WallpaperHandleResult(True, "删图功能当前未启用。")
            return WallpaperHandleResult(True, await self.delete_replied_wallpaper(event))

        add_match = parse_dynamic_command(text, self.add_commands())
        if add_match:
            if not self.add_enabled():
                return WallpaperHandleResult(True, "存图功能当前未启用。")
            library_name = clean_text(add_match[1])
            return WallpaperHandleResult(True, await self.add_images_from_event(event, library_name))

        library = self.match_random_command(text)
        if library is None:
            return WallpaperHandleResult(False)
        message = await self.send_random_wallpaper(event, library)
        return WallpaperHandleResult(True, message, sent=not bool(message))

    def match_random_command(self, text: str) -> WallpaperLibrary | None:
        for library in self.libraries():
            if parse_dynamic_command(text, library.commands):
                return library
        return None

    def find_library(self, value: str) -> WallpaperLibrary | None:
        needle = clean_text(value)
        if not needle:
            return None
        normalized = needle.lstrip("/")
        for library in self.libraries():
            if needle == library.name or normalized == library.name:
                return library
            if any(needle == command or normalized == command.lstrip("/") for command in library.commands):
                return library
        return None

    def image_files(self, library: WallpaperLibrary) -> list[Path]:
        if not library.path.exists():
            return []
        pattern_iter = library.path.rglob("*") if library.recursive else library.path.glob("*")
        allowed = self.allowed_extensions()
        files = [path for path in pattern_iter if path.is_file() and path.suffix.lower() in allowed]
        return sorted(files)

    async def send_random_wallpaper(self, event: Any, library: WallpaperLibrary) -> str:
        files = self.image_files(library)
        if not files:
            return f"壁纸库「{library.name}」里还没有可用图片。"
        image_path = random.choice(files)
        caption = self.render_caption(library, image_path)
        try:
            if library.send_mode == SEND_MODE_CAPTION_FIRST and caption:
                await self._send_chain(event, [Comp.Plain(caption)], None, library)
                await self._send_chain(event, [Comp.Image.fromFileSystem(str(image_path))], image_path, library)
            else:
                chain: list[Any] = []
                if caption and library.send_mode != SEND_MODE_IMAGE_ONLY:
                    chain.append(Comp.Plain(caption))
                chain.append(Comp.Image.fromFileSystem(str(image_path)))
                await self._send_chain(event, chain, image_path, library)
        except Exception as exc:
            logger.warning("[HelperTools] send wallpaper failed: %s", exc)
            return f"发送壁纸失败: {exc}"
        return ""

    @staticmethod
    def render_caption(library: WallpaperLibrary, image_path: Path) -> str:
        caption = library.caption
        return (
            caption.replace("{library}", library.name)
            .replace("{filename}", image_path.name)
            .replace("{path}", str(image_path))
        )

    async def _send_chain(
        self,
        event: Any,
        chain: list[Any],
        image_path: Path | None,
        library: WallpaperLibrary,
    ) -> None:
        message_id = await self._send_chain_onebot(event, chain)
        if message_id and image_path is not None:
            self.record_sent_image(str(message_id), image_path, library.name)
            return
        if message_id:
            return
        sender = getattr(event, "send", None)
        if callable(sender):
            await sender(MessageChain(chain=chain))

    async def _send_chain_onebot(self, event: Any, chain: list[Any]) -> str:
        bot = getattr(event, "bot", None)
        parser = getattr(event, "_parse_onebot_json", None)
        if bot is None or not callable(parser):
            return ""
        group_id = clean_text(getattr(event, "get_group_id", lambda: "")())
        sender_id = clean_text(getattr(event, "get_sender_id", lambda: "")())
        try:
            messages = await parser(MessageChain(chain=chain))
            if not messages:
                return ""
            if group_id and group_id.isdigit():
                result = await bot.send_group_msg(group_id=int(group_id), message=messages)
            elif sender_id and sender_id.isdigit():
                result = await bot.send_private_msg(user_id=int(sender_id), message=messages)
            else:
                return ""
            setattr(event, "_has_send_oper", True)
        except Exception as exc:
            logger.debug("[HelperTools] OneBot wallpaper direct send fallback: %s", exc)
            return ""
        if isinstance(result, dict):
            return clean_text(result.get("message_id"))
        return clean_text(getattr(result, "message_id", ""))

    def load_registry(self) -> dict[str, dict[str, str]]:
        if not self.registry_path.exists():
            return {}
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_registry(self, payload: dict[str, dict[str, str]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_sent_image(self, message_id: str, image_path: Path, library_name: str) -> None:
        if not message_id:
            return
        payload = self.load_registry()
        payload[message_id] = {
            "path": str(image_path.resolve(strict=False)),
            "library": library_name,
            "sent_at": str(int(time.time())),
        }
        if len(payload) > 2000:
            items = sorted(payload.items(), key=lambda item: int(item[1].get("sent_at") or 0), reverse=True)
            payload = dict(items[:2000])
        self.save_registry(payload)

    async def add_images_from_event(self, event: Any, library_name: str) -> str:
        if not self._can_mutate(event):
            return "只有管理员可以添加壁纸。"
        library = self.find_library(library_name)
        if library is None:
            return "请提供要存入的壁纸库名称，例如：存图 卡比壁纸。"
        refs = await self.extract_image_refs(event, include_direct=True, include_reply=True)
        if not refs:
            return "没有找到可保存的图片。请随指令发送图片，或引用一张图片后发送存图指令。"
        library.path.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        skipped = 0
        errors: list[str] = []
        for ref in refs:
            try:
                data, ext = await self.read_image_ref(event, ref)
                if ext.lower() not in self.allowed_extensions():
                    ext = ".jpg"
                duplicate = self.find_duplicate(library, data) if self.deduplicate_on_add() else None
                if duplicate:
                    skipped += 1
                    continue
                digest = _sha256_bytes(data)
                filename = f"{_safe_filename_part(library.name)}_{int(time.time())}_{digest[:12]}{ext}"
                target = library.path / filename
                target.write_bytes(data)
                saved.append(target)
            except Exception as exc:
                errors.append(str(exc))
        if not saved and skipped:
            return f"图片已存在于「{library.name}」，没有重复保存。"
        if not saved:
            detail = f": {'; '.join(errors[:3])}" if errors else "。"
            return f"保存失败{detail}"
        message = f"已保存 {len(saved)} 张图片到「{library.name}」。"
        if skipped:
            message += f" 跳过 {skipped} 张重复图片。"
        if errors:
            message += f" 另有 {len(errors)} 张失败。"
        return message

    def find_duplicate(self, library: WallpaperLibrary, data: bytes) -> Path | None:
        digest = _sha256_bytes(data)
        for path in self.image_files(library):
            try:
                if path.stat().st_size == len(data) and _sha256_file(path) == digest:
                    return path
            except OSError:
                continue
        return None

    async def delete_replied_wallpaper(self, event: Any) -> str:
        if not self._can_mutate(event):
            return "只有管理员可以删除壁纸。"
        reply_ids = self.extract_reply_ids(event)
        registry = self.load_registry()
        candidates: list[Path] = []
        for reply_id in reply_ids:
            item = registry.get(reply_id)
            if isinstance(item, dict):
                candidates.append(Path(clean_text(item.get("path"))))
        if not candidates:
            candidates.extend(await self.local_paths_from_reply_images(event))
        for path in candidates:
            resolved = path.expanduser().resolve(strict=False)
            if not self.is_in_any_library(resolved):
                continue
            if not resolved.exists() or not resolved.is_file():
                self.remove_registry_path(registry, resolved)
                self.save_registry(registry)
                return "这张壁纸文件已经不存在，已清理记录。"
            try:
                resolved.unlink()
            except OSError as exc:
                return f"删除失败: {exc}"
            self.remove_registry_path(registry, resolved)
            self.save_registry(registry)
            return f"已删除壁纸文件: {resolved.name}"
        return "没有找到可删除的壁纸。请引用本插件刚发出的壁纸图片再使用删图指令。"

    @staticmethod
    def remove_registry_path(registry: dict[str, dict[str, str]], path: Path) -> None:
        target = str(path.resolve(strict=False))
        for key in [key for key, value in registry.items() if clean_text(value.get("path")) == target]:
            registry.pop(key, None)

    def _can_mutate(self, event: Any) -> bool:
        if not self.mutate_admin_only():
            return True
        checker = getattr(event, "is_admin", None)
        return bool(checker()) if callable(checker) else False

    def is_in_any_library(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        for library in self.libraries():
            root = library.path.resolve(strict=False)
            try:
                if resolved.is_relative_to(root):
                    return True
            except ValueError:
                continue
        return False

    async def extract_image_refs(self, event: Any, *, include_direct: bool, include_reply: bool) -> list[ImageRef]:
        refs: list[ImageRef] = []
        messages = getattr(event, "get_messages", lambda: [])()
        if not isinstance(messages, list):
            return refs
        if include_direct:
            refs.extend(self.image_refs_from_chain(messages, source="message"))
        if include_reply:
            refs.extend(self.reply_image_refs_from_chain(messages))
            for reply_id in self.extract_reply_ids(event):
                refs.extend(await self.image_refs_from_onebot_message(event, reply_id))
        return self._dedupe_refs(refs)

    @staticmethod
    def image_refs_from_chain(chain: list[Any], *, source: str) -> list[ImageRef]:
        refs: list[ImageRef] = []
        for seg in chain:
            if isinstance(seg, Comp.Image):
                refs.append(
                    ImageRef(
                        url=clean_text(getattr(seg, "url", "")),
                        file=clean_text(getattr(seg, "file", "")),
                        path=clean_text(getattr(seg, "path", "")),
                        source=source,
                    )
                )
        return refs

    def reply_image_refs_from_chain(self, chain: list[Any]) -> list[ImageRef]:
        refs: list[ImageRef] = []
        for seg in chain:
            if isinstance(seg, Comp.Reply) and isinstance(getattr(seg, "chain", None), list):
                refs.extend(self.image_refs_from_chain(seg.chain or [], source="reply"))
        return refs

    @staticmethod
    def extract_reply_ids(event: Any) -> list[str]:
        ids: list[str] = []
        messages = getattr(event, "get_messages", lambda: [])()
        if not isinstance(messages, list):
            return ids
        for seg in messages:
            if isinstance(seg, Comp.Reply):
                reply_id = clean_text(getattr(seg, "id", ""))
                if reply_id and reply_id not in ids:
                    ids.append(reply_id)
        return ids

    async def image_refs_from_onebot_message(self, event: Any, message_id: str) -> list[ImageRef]:
        bot = getattr(event, "bot", None)
        if bot is None or not message_id:
            return []
        try:
            payload = await call_onebot(bot, "get_msg", message_id=int(message_id))
        except Exception:
            return []
        segments = payload.get("message", []) if isinstance(payload, dict) else []
        if not isinstance(segments, list):
            return []
        refs: list[ImageRef] = []
        for seg in segments:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            data = seg.get("data", {})
            if not isinstance(data, dict):
                continue
            refs.append(
                ImageRef(
                    url=clean_text(data.get("url")),
                    file=clean_text(data.get("file")),
                    path=clean_text(data.get("path")),
                    source="onebot_reply",
                )
            )
        return refs

    async def local_paths_from_reply_images(self, event: Any) -> list[Path]:
        refs = await self.extract_image_refs(event, include_direct=False, include_reply=True)
        paths: list[Path] = []
        for ref in refs:
            for raw in (ref.path, ref.file):
                path = self.path_from_ref(raw)
                if path is not None:
                    paths.append(path)
        return paths

    @staticmethod
    def path_from_ref(value: str) -> Path | None:
        text = clean_text(value)
        if not text:
            return None
        uri_path = _path_from_file_uri(text)
        if uri_path is not None:
            return uri_path
        path = Path(text).expanduser()
        if path.exists():
            return path.resolve(strict=False)
        return None

    @staticmethod
    def _dedupe_refs(refs: list[ImageRef]) -> list[ImageRef]:
        seen: set[tuple[str, str, str]] = set()
        result: list[ImageRef] = []
        for ref in refs:
            key = (ref.url, ref.file, ref.path)
            if key in seen or not any(key):
                continue
            seen.add(key)
            result.append(ref)
        return result

    async def read_image_ref(self, event: Any, ref: ImageRef) -> tuple[bytes, str]:
        for raw in (ref.path, ref.file):
            path = self.path_from_ref(raw)
            if path is not None and path.exists() and path.is_file():
                data = path.read_bytes()
                if len(data) > self.max_add_bytes():
                    raise ValueError("图片超过最大保存大小。")
                return data, _guess_extension(str(path))
        for raw in (ref.url, ref.file):
            value = clean_text(raw)
            if value.startswith("base64://"):
                data = base64.b64decode(value[len("base64://") :])
                if len(data) > self.max_add_bytes():
                    raise ValueError("图片超过最大保存大小。")
                return data, ".jpg"
            if _is_url(value):
                data, content_type = await fetch_bytes(value, timeout_seconds=20, max_bytes=self.max_add_bytes())
                return data, _guess_extension(value, content_type=content_type)

        bot = getattr(event, "bot", None)
        if bot is not None and ref.file:
            try:
                payload = await call_onebot(bot, "get_image", file=ref.file)
            except Exception as exc:
                raise ValueError(f"无法通过 OneBot 读取图片: {exc}") from exc
            if isinstance(payload, dict):
                next_ref = ImageRef(
                    url=clean_text(payload.get("url")),
                    file=clean_text(payload.get("file")),
                    path=clean_text(payload.get("path")),
                    source="onebot_get_image",
                )
                if next_ref.url or next_ref.path or (next_ref.file and next_ref.file != ref.file):
                    return await self.read_image_ref(event, next_ref)
        raise ValueError("无法读取图片原图。")
