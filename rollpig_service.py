from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .helper_utils import (
    cfg,
    clean_text,
    extract_file_config_value,
    read_bool,
    read_int,
    read_list,
    resolve_existing_path,
)

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover - requirements.txt installs Pillow
    PILImage = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None


DEFAULT_TIMEZONE = "Asia/Shanghai"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
FONT_SUFFIXES = (".otf", ".ttf", ".ttc", ".otc")
SYSTEM_FONT_DIRS = (
    "C:/Windows/Fonts",
    "~/AppData/Local/Microsoft/Windows/Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.local/share/fonts",
    "~/.fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "~/Library/Fonts",
)
SYSTEM_FONT_FILES = {
    "bold": (
        "msyhbd.ttc",
        "NotoSansCJK-Bold.ttc",
        "NotoSansCJKsc-Bold.otf",
        "SourceHanSansSC-Bold.otf",
        "wqy-zenhei.ttc",
        "PingFang.ttc",
        "DejaVuSans-Bold.ttf",
    ),
    "regular": (
        "msyh.ttc",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJKsc-Regular.otf",
        "SourceHanSansSC-Regular.otf",
        "wqy-zenhei.ttc",
        "PingFang.ttc",
        "DejaVuSans.ttf",
    ),
}


@dataclass(frozen=True, slots=True)
class RollPig:
    pig_id: str
    name: str
    description: str
    analysis: str


def _positive_int(
    value: Any, default: int, *, minimum: int = 1, maximum: int = 10000
) -> int:
    """Normalize values before they reach Pillow, which rejects float dimensions."""

    try:
        number = round(float(value))
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(maximum, number))


def _safe_filename(value: str, default: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "-_") else "_"
        for char in value
    )
    safe = safe.strip("._")
    return safe or default


class RollPigService:
    """Daily roll-pig selection, cache management, and local card rendering."""

    CARD_WIDTH = 800
    MIN_CARD_HEIGHT = 760
    MAX_CARD_HEIGHT = 1500
    CARD_PADDING = 48
    AVATAR_SIZE = 260
    NAME_FONT_SIZE = 58
    DESCRIPTION_FONT_SIZE = 30
    ANALYSIS_FONT_SIZE = 27

    def __init__(
        self,
        config: Any,
        data_dir: Path,
        context: Any | None = None,
        assets_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.context = context
        self.assets_dir = (
            assets_dir or Path(__file__).resolve().parent / "rollpig_assets"
        )
        self.storage_dir = data_dir / "rollpig"
        self.state_path = self.storage_dir / "today.json"
        self.cards_dir = self.storage_dir / "cards"
        self._state_lock = asyncio.Lock()
        self._font_cache: dict[tuple[str, int], Any] = {}
        self._font_path_cache: dict[str, Path | None] = {}
        self._font_fallback_warned = False

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "rollpig", "enabled", True), True)

    def commands_enabled(self) -> bool:
        return read_bool(cfg(self.config, "rollpig", "commands_enabled", True), True)

    def allow_mentioned_user(self) -> bool:
        return read_bool(
            cfg(self.config, "rollpig", "allow_mentioned_user", False), False
        )

    def mention_target_in_group(self) -> bool:
        return read_bool(
            cfg(self.config, "rollpig", "mention_target_in_group", True), True
        )

    def protect_admins(self) -> bool:
        return read_bool(cfg(self.config, "rollpig", "protect_admins", True), True)

    def card_cache_days(self) -> int:
        return read_int(
            cfg(self.config, "rollpig", "card_cache_days", 7),
            7,
            minimum=1,
            maximum=30,
        )

    def timezone_name(self) -> str:
        return clean_text(
            cfg(self.config, "rollpig", "timezone", DEFAULT_TIMEZONE), DEFAULT_TIMEZONE
        )

    def today_key(self, now: datetime | None = None) -> str:
        if now is not None:
            return now.date().isoformat()
        try:
            return datetime.now(ZoneInfo(self.timezone_name())).date().isoformat()
        except ZoneInfoNotFoundError:
            logger.warning(
                "[HelperTools/RollPig] invalid timezone %r; using %s",
                self.timezone_name(),
                DEFAULT_TIMEZONE,
            )
            return datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()

    def _custom_catalog_path(self) -> Path | None:
        raw = extract_file_config_value(
            cfg(self.config, "rollpig", "custom_catalog_file", [])
        )
        path = resolve_existing_path(raw, self.storage_dir, self.assets_dir)
        return path if path and path.is_file() else None

    def _custom_image_dir(self) -> Path | None:
        raw = clean_text(cfg(self.config, "rollpig", "custom_image_dir", ""))
        path = resolve_existing_path(raw, self.storage_dir, self.assets_dir)
        return path if path and path.is_dir() else None

    def _catalog_paths(self) -> list[Path]:
        paths: list[Path] = []
        custom = self._custom_catalog_path()
        if custom:
            paths.append(custom)
        builtin = self.assets_dir / "pig.json"
        if builtin not in paths:
            paths.append(builtin)
        return paths

    def _load_catalog_file(self, path: Path) -> list[RollPig]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[HelperTools/RollPig] could not read catalog %s: %s", path, exc
            )
            return []
        if not isinstance(raw, list):
            logger.warning(
                "[HelperTools/RollPig] catalog %s must contain a JSON list", path
            )
            return []

        entries: list[RollPig] = []
        seen_ids: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            pig_id = clean_text(item.get("id"))
            name = clean_text(item.get("name"))
            if not pig_id or not name or pig_id in seen_ids:
                continue
            seen_ids.add(pig_id)
            entries.append(
                RollPig(
                    pig_id=pig_id,
                    name=name,
                    description=clean_text(
                        item.get("description"), "今天也要好好生活。"
                    ),
                    analysis=clean_text(item.get("analysis"), "今天的你也很特别。"),
                )
            )
        return entries

    def catalog(self) -> list[RollPig]:
        for path in self._catalog_paths():
            entries = self._load_catalog_file(path)
            if entries:
                return entries
        return []

    @staticmethod
    def _event_id(event: Any, getter_name: str) -> str:
        getter = getattr(event, getter_name, None)
        try:
            return clean_text(getter() if callable(getter) else "")
        except Exception:  # noqa: BLE001 - malformed platform event must not break a command
            return ""

    def _mention_ids(self, event: Any) -> list[str]:
        getter = getattr(event, "get_messages", None)
        try:
            messages = getter() if callable(getter) else []
        except Exception:  # noqa: BLE001 - optional message-chain access
            return []
        self_id = self._event_id(event, "get_self_id")
        target_ids: list[str] = []
        for segment in messages or []:
            target_id = clean_text(getattr(segment, "qq", ""))
            if target_id and target_id != self_id and target_id not in target_ids:
                target_ids.append(target_id)
        return target_ids

    def _admin_ids(self) -> set[str]:
        getter = getattr(self.context, "get_config", None)
        try:
            core_config = getter() if callable(getter) else {}
            raw_admins = (
                core_config.get("admins_id", []) if hasattr(core_config, "get") else []
            )
        except Exception:  # noqa: BLE001 - an unavailable core config only disables this optional guard
            raw_admins = []
        return set(read_list(raw_admins, []))

    def resolve_target(self, event: Any) -> tuple[str, str]:
        sender_id = self._event_id(event, "get_sender_id")
        if not sender_id:
            return "", "没有读取到当前用户的 ID，无法抽取今日小猪。"

        mentions = self._mention_ids(event)
        if not mentions:
            return sender_id, ""
        if len(mentions) > 1:
            return "", "一次只能查看一位用户的今日小猪。"
        if not self.allow_mentioned_user():
            return "", "查看被 @ 用户的今日小猪当前未开启。"

        target_id = mentions[0]
        if self.protect_admins() and target_id in self._admin_ids():
            return "", "不能查看管理员的今日小猪。"
        return target_id, ""

    @staticmethod
    def _record_pig_id(value: Any) -> str:
        if isinstance(value, dict):
            return clean_text(value.get("id") or value.get("pig_id"))
        return clean_text(value)

    def _load_today_state(self, date_key: str) -> dict[str, Any]:
        state: dict[str, Any] = {"date": date_key, "records": {}}
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if (
                isinstance(raw, dict)
                and raw.get("date") == date_key
                and isinstance(raw.get("records"), dict)
            ):
                state = {"date": date_key, "records": dict(raw["records"])}
        return state

    def _save_today_state(self, state: dict[str, Any]) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.state_path)

    async def select_pig(
        self, user_id: str, date_key: str | None = None
    ) -> tuple[RollPig | None, str]:
        user_id = clean_text(user_id)
        if not user_id:
            return None, "没有读取到目标用户的 ID。"
        pigs = self.catalog()
        if not pigs:
            return None, "今日小猪素材库不可用，请检查插件资源或自定义素材配置。"
        pigs_by_id = {pig.pig_id: pig for pig in pigs}
        resolved_date = date_key or self.today_key()

        async with self._state_lock:
            state = await asyncio.to_thread(self._load_today_state, resolved_date)
            records = state["records"]
            existing_id = self._record_pig_id(records.get(user_id))
            existing = pigs_by_id.get(existing_id)
            if existing:
                return existing, ""

            selected = random.choice(pigs)
            records[user_id] = selected.pig_id
            await asyncio.to_thread(self._save_today_state, state)
            return selected, ""

    def _image_directories(self) -> list[Path]:
        directories: list[Path] = []
        custom = self._custom_image_dir()
        if custom:
            directories.append(custom)
        builtin = self.assets_dir / "image"
        if builtin not in directories:
            directories.append(builtin)
        return directories

    def image_path(self, pig_id: str) -> Path | None:
        pig_id = clean_text(pig_id)
        if not pig_id or Path(pig_id).name != pig_id or pig_id in {".", ".."}:
            return None
        for directory in self._image_directories():
            try:
                root = directory.resolve()
            except OSError:
                continue
            for suffix in IMAGE_SUFFIXES:
                candidate = (root / f"{pig_id}{suffix}").resolve()
                if candidate.parent == root and candidate.is_file():
                    return candidate
        return None

    def _configured_font_path(self, kind: str) -> Path | None:
        raw = extract_file_config_value(
            cfg(self.config, "rollpig", f"font_{kind}_file", [])
        )
        path = resolve_existing_path(
            raw, self.storage_dir, self.assets_dir, self.assets_dir / "font"
        )
        return path if path and path.is_file() else None

    def _bundled_font_paths(self, kind: str) -> list[Path]:
        font_dir = self.assets_dir / "font"
        preferred = "荆南麦圆体.otf" if kind == "bold" else "可爱字体.ttf"
        paths = [font_dir / preferred]
        try:
            bundled = sorted(
                item
                for item in font_dir.iterdir()
                if item.is_file() and item.suffix.lower() in FONT_SUFFIXES
            )
        except OSError:
            bundled = []
        for item in bundled:
            if item not in paths:
                paths.append(item)
        return paths

    def _system_font_paths(self, kind: str) -> Iterator[Path]:
        names = SYSTEM_FONT_FILES.get(kind, SYSTEM_FONT_FILES["regular"])
        for raw_dir in SYSTEM_FONT_DIRS:
            try:
                directory = Path(raw_dir).expanduser()
                if not directory.is_dir():
                    continue
            except OSError:
                continue
            for name in names:
                direct = directory / name
                try:
                    if direct.is_file():
                        yield direct
                        continue
                    nested = next(iter(directory.rglob(name)), None)
                except OSError:
                    continue
                if nested is not None:
                    yield nested

    def _font_candidates(self, kind: str) -> Iterator[Path]:
        """Yield candidates lazily so system font trees are only scanned when needed."""

        seen: set[Path] = set()
        configured = self._configured_font_path(kind)
        sources = (
            [configured] if configured else [],
            self._bundled_font_paths(kind),
        )
        for source in sources:
            for candidate in source:
                if candidate not in seen:
                    seen.add(candidate)
                    yield candidate
        for candidate in self._system_font_paths(kind):
            if candidate not in seen:
                seen.add(candidate)
                yield candidate

    def _resolve_font_path(self, kind: str) -> Path | None:
        if kind in self._font_path_cache:
            return self._font_path_cache[kind]
        resolved: Path | None = None
        for candidate in self._font_candidates(kind):
            try:
                if candidate.is_file():
                    resolved = candidate
                    break
            except OSError:
                continue
        if resolved is None:
            self._warn_font_fallback()
        self._font_path_cache[kind] = resolved
        return resolved

    def _warn_font_fallback(self) -> None:
        if self._font_fallback_warned:
            return
        self._font_fallback_warned = True
        logger.warning(
            "[HelperTools/RollPig] no usable font file found in %s or the system font "
            "directories; falling back to the Pillow default font",
            self.assets_dir / "font",
        )

    def _default_font(self, size: int) -> Any:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10 does not accept the size keyword
            return ImageFont.load_default()

    def _font(self, kind: str, size: Any) -> Any:
        if ImageFont is None:
            return None
        normalized_size = _positive_int(size, 24, minimum=8, maximum=160)
        key = (kind, normalized_size)
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached

        font: Any = None
        path = self._resolve_font_path(kind)
        if path is not None:
            try:
                font = ImageFont.truetype(str(path), normalized_size)
            except OSError:
                self._font_path_cache[kind] = None
                self._warn_font_fallback()
                font = None
        if font is None:
            font = self._default_font(normalized_size)
        self._font_cache[key] = font
        return font

    @staticmethod
    def _measure(draw: Any, text: str, font: Any) -> tuple[int, int]:
        try:
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
            width = _positive_int(math.ceil(float(right) - float(left)), 1)
            height = _positive_int(math.ceil(float(bottom) - float(top)), 1)
            return width, height
        except (AttributeError, TypeError, ValueError, OverflowError):
            width = _positive_int(math.ceil(float(draw.textlength(text, font=font))), 1)
            return width, _positive_int(getattr(font, "size", 16), 16)

    def _fit_line(self, draw: Any, text: str, font: Any, max_width: int) -> str:
        text = clean_text(text)
        if self._measure(draw, text, font)[0] <= max_width:
            return text
        suffix = "..."
        while text and self._measure(draw, text + suffix, font)[0] > max_width:
            text = text[:-1]
        return (text + suffix) if text else suffix

    def _wrap_text(
        self, draw: Any, text: str, font: Any, max_width: int, max_lines: int
    ) -> list[str]:
        text = clean_text(text)
        if not text:
            return [""]
        lines: list[str] = []
        current = ""
        truncated = False
        for char in text:
            candidate = current + char
            if not current or self._measure(draw, candidate, font)[0] <= max_width:
                current = candidate
                continue
            lines.append(current)
            if len(lines) >= max_lines:
                truncated = True
                current = ""
                break
            current = char
        if current and len(lines) < max_lines:
            lines.append(current)
        elif current:
            truncated = True
        if truncated and lines:
            lines[-1] = (
                self._fit_line(
                    draw,
                    lines[-1],
                    font,
                    max_width - self._measure(draw, "...", font)[0],
                )
                + "..."
            )
        return lines or [""]

    @staticmethod
    def _center_x(width: int, text_width: int) -> int:
        return max(0, _positive_int((width - text_width) // 2, 0, minimum=0))

    def _card_path(self, pig: RollPig, user_id: str, date_key: str) -> Path:
        user_part = _safe_filename(user_id, "user")
        pig_part = _safe_filename(pig.pig_id, "pig")
        date_part = _safe_filename(date_key, "date")
        return self.cards_dir / f"{date_part}_{user_part}_{pig_part}.png"

    def _prune_cards(self) -> None:
        if not self.cards_dir.exists():
            return
        cutoff = datetime.now(timezone.utc).timestamp() - self.card_cache_days() * 86400
        for path in self.cards_dir.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def render_card(self, pig: RollPig, user_id: str, date_key: str) -> Path | None:
        if PILImage is None or ImageDraw is None or ImageOps is None:
            logger.warning(
                "[HelperTools/RollPig] Pillow is unavailable; using fallback output"
            )
            return None

        output_path = self._card_path(pig, user_id, date_key)
        if output_path.is_file():
            return output_path

        try:
            self.cards_dir.mkdir(parents=True, exist_ok=True)
            self._prune_cards()
            width = _positive_int(self.CARD_WIDTH, 800, minimum=400, maximum=1600)
            min_height = _positive_int(
                self.MIN_CARD_HEIGHT, 760, minimum=400, maximum=2400
            )
            max_height = _positive_int(
                self.MAX_CARD_HEIGHT, 1500, minimum=min_height, maximum=3000
            )
            padding = _positive_int(self.CARD_PADDING, 48, minimum=16, maximum=160)
            avatar_size = _positive_int(self.AVATAR_SIZE, 260, minimum=80, maximum=500)
            content_width = max(120, width - padding * 2)

            name_font = self._font("bold", self.NAME_FONT_SIZE)
            desc_font = self._font("regular", self.DESCRIPTION_FONT_SIZE)
            analysis_font = self._font("regular", self.ANALYSIS_FONT_SIZE)
            header_font = self._font("bold", 24)
            measure_canvas = PILImage.new("RGB", (width, 1), "white")
            measure_draw = ImageDraw.Draw(measure_canvas)
            name = self._fit_line(measure_draw, pig.name, name_font, content_width)
            descriptions = self._wrap_text(
                measure_draw, pig.description, desc_font, content_width, 2
            )
            analysis_lines = self._wrap_text(
                measure_draw, pig.analysis, analysis_font, content_width, 9
            )
            _, name_height = self._measure(measure_draw, name, name_font)
            _, desc_height = self._measure(measure_draw, "A", desc_font)
            _, analysis_height = self._measure(measure_draw, "A", analysis_font)
            line_step = _positive_int(
                math.ceil(analysis_height * 1.45),
                analysis_height,
                minimum=analysis_height,
            )
            content_height = (
                30
                + 18
                + avatar_size
                + 24
                + name_height
                + 18
                + len(descriptions) * desc_height
                + 26
                + len(analysis_lines) * line_step
            )
            height = min(max_height, max(min_height, content_height + padding * 2))
            canvas = PILImage.new(
                "RGBA", (int(width), int(height)), (248, 249, 250, 255)
            )
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle(
                (0, 0, int(width - 1), int(height - 1)),
                radius=28,
                fill=(255, 255, 255, 255),
                outline=(224, 226, 230, 255),
                width=2,
            )

            y = max(padding, int((height - content_height) // 2))
            header = "今日小猪"
            header_width, header_height = self._measure(draw, header, header_font)
            draw.text(
                (int(self._center_x(width, header_width)), int(y)),
                header,
                fill=(204, 91, 110),
                font=header_font,
            )
            y += header_height + 18

            avatar = self._load_avatar(pig.pig_id, avatar_size)
            avatar_x = int((width - avatar_size) // 2)
            if avatar is not None:
                canvas.alpha_composite(avatar, (avatar_x, int(y)))
            else:
                draw.ellipse(
                    (avatar_x, int(y), avatar_x + avatar_size, int(y) + avatar_size),
                    fill=(245, 213, 219, 255),
                    outline=(224, 152, 164, 255),
                    width=3,
                )
                placeholder = "?"
                placeholder_width, placeholder_height = self._measure(
                    draw, placeholder, name_font
                )
                draw.text(
                    (
                        int(self._center_x(width, placeholder_width)),
                        int(y + (avatar_size - placeholder_height) // 2),
                    ),
                    placeholder,
                    fill=(204, 91, 110),
                    font=name_font,
                )
            y += avatar_size + 24

            name_width, name_height = self._measure(draw, name, name_font)
            draw.text(
                (int(self._center_x(width, name_width)), int(y)),
                name,
                fill=(38, 42, 48),
                font=name_font,
            )
            y += name_height + 18
            for line in descriptions:
                line_width, line_height = self._measure(draw, line, desc_font)
                draw.text(
                    (int(self._center_x(width, line_width)), int(y)),
                    line,
                    fill=(96, 101, 110),
                    font=desc_font,
                )
                y += line_height
            y += 20
            for line in analysis_lines:
                line_width, _ = self._measure(draw, line, analysis_font)
                draw.text(
                    (int(self._center_x(width, line_width)), int(y)),
                    line,
                    fill=(52, 57, 65),
                    font=analysis_font,
                )
                y += line_step

            canvas.convert("RGB").save(output_path, format="PNG")
            return output_path
        except (OSError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("[HelperTools/RollPig] card rendering failed: %s", exc)
            return None

    def _load_avatar(self, pig_id: str, size: int) -> Any | None:
        path = self.image_path(pig_id)
        if path is None or PILImage is None or ImageOps is None:
            return None
        try:
            with PILImage.open(path) as source:
                image = source.convert("RGBA")
            resampling = getattr(
                getattr(PILImage, "Resampling", PILImage), "LANCZOS", 1
            )
            return ImageOps.fit(
                image, (int(size), int(size)), method=resampling, centering=(0.5, 0.5)
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "[HelperTools/RollPig] could not load avatar %s: %s", path, exc
            )
            return None

    @staticmethod
    def _pig_text(pig: RollPig) -> str:
        return f"【今日小猪】\n名称：{pig.name}\n描述：{pig.description}\n解析：{pig.analysis}"

    async def build_chain(self, event: Any) -> tuple[list[Any] | None, str]:
        target_id, error = self.resolve_target(event)
        if error:
            return None, error
        date_key = self.today_key()
        pig, error = await self.select_pig(target_id, date_key)
        if pig is None:
            return None, error

        card_path = await asyncio.to_thread(self.render_card, pig, target_id, date_key)
        sender_id = self._event_id(event, "get_sender_id")
        group_id = self._event_id(event, "get_group_id")
        intro = (
            " 这是你的今日小猪：" if target_id == sender_id else " 这是 TA 的今日小猪："
        )
        chain: list[Any] = []
        if group_id and self.mention_target_in_group():
            chain.append(Comp.At(qq=target_id))
        chain.append(Comp.Plain(intro))
        if card_path is not None:
            chain.append(Comp.Image.fromFileSystem(str(card_path)))
            return chain, ""

        original_image = self.image_path(pig.pig_id)
        if original_image is not None:
            chain.append(Comp.Image.fromFileSystem(str(original_image)))
        chain.append(Comp.Plain("\n" + self._pig_text(pig)))
        return chain, ""
