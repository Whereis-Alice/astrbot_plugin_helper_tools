"""Safe local wallpaper-library operations for the Helper Tools dashboard.

The normal wallpaper service deliberately accepts directories outside the
plugin data directory, because a deployment may keep its image collection on
another mounted disk.  The dashboard therefore never accepts an absolute file
path from its caller.  Every operation starts from a configured library index
and a validated POSIX-relative path below that library root.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

from .helper_utils import clean_text, read_bool
from .wallpaper_service import WallpaperLibrary, WallpaperService


_SAFE_PREVIEW_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_FORMAT_EXTENSIONS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
    "GIF": {".gif"},
}
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_SAFE_LIBRARY_ID = re.compile(r"^[0-9]{1,5}$")
_INVALID_FILENAME_CHARACTERS = frozenset('\\/:*?"<>|')
_MAX_LIBRARY_SCAN_ENTRIES = 30_000
_MAX_WEBUI_UPLOAD_BYTES = 64 * 1024 * 1024
_MAX_WEBUI_UPLOAD_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_UPLOAD_FILES = 24
_MAX_IMAGE_PIXELS = 64_000_000
_THUMBNAIL_SIZE = 560
_PREVIEW_SIZE = 1_920
_MAX_THUMBNAIL_BYTES = 900 * 1024
_MAX_PREVIEW_BYTES = 4 * 1024 * 1024


class WallpaperDashboardError(ValueError):
    """A safe-to-display validation error for wallpaper dashboard requests."""


@dataclass(slots=True)
class ManagedWallpaperLibrary:
    """One configured library and its stable-in-the-current-config list index."""

    index: int
    row: dict[str, Any]
    library: WallpaperLibrary
    source_path: Path

    @property
    def identifier(self) -> str:
        return str(self.index)


@dataclass(slots=True)
class WallpaperScan:
    files: list[Path]
    state: str
    detail: str = ""
    truncated: bool = False


@dataclass(slots=True)
class WallpaperUpload:
    filename: str
    data: bytes


class WallpaperLibraryDashboard:
    """Filesystem-safe wallpaper-library backend used by the native WebUI."""

    def __init__(self, wallpaper: WallpaperService, data_dir: Path) -> None:
        self.wallpaper = wallpaper
        self.data_dir = Path(data_dir)

    def list_libraries(self) -> dict[str, Any]:
        """Return configured library summaries without exposing image contents."""

        summaries: list[dict[str, Any]] = []
        for record in self._configured_libraries():
            scan = self._scan(record)
            files = scan.files
            total_bytes = 0
            latest_mtime = 0.0
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                total_bytes += stat.st_size
                latest_mtime = max(latest_mtime, stat.st_mtime)
            root = record.library.path
            writable = False
            if scan.state == "ready":
                try:
                    writable = os.access(root, os.W_OK)
                except OSError:
                    writable = False
            summaries.append(
                {
                    "id": record.identifier,
                    "name": record.library.name,
                    "configured_path": clean_text(record.row.get("path")),
                    "resolved_path": str(root),
                    "commands": list(record.library.commands),
                    "caption": record.library.caption,
                    "send_mode": clean_text(record.row.get("send_mode"), "同一条消息"),
                    "recursive": record.library.recursive,
                    "image_count": len(files),
                    "total_bytes": total_bytes,
                    "latest_modified_at": self._format_timestamp(latest_mtime),
                    "state": scan.state,
                    "detail": scan.detail,
                    "scan_truncated": scan.truncated,
                    "writable": writable,
                }
            )
        return {
            "libraries": summaries,
            "safe_preview_extensions": sorted(_SAFE_PREVIEW_EXTENSIONS),
            "upload_max_bytes": self.upload_max_bytes(),
            "upload_file_limit": _MAX_UPLOAD_FILES,
        }

    def list_images(
        self,
        library_id: str,
        *,
        page: int,
        page_size: int,
        query: str = "",
        sort: str = "newest",
    ) -> dict[str, Any]:
        record = self.get_library(library_id)
        scan = self._scan(record)
        if scan.state not in {"ready", "missing"}:
            raise WallpaperDashboardError(scan.detail or "壁纸库目录不可用。")

        normalized_query = clean_text(query).casefold()[:160]
        entries: list[tuple[Path, os.stat_result, str]] = []
        for path in scan.files:
            try:
                stat = path.stat()
                relative = path.relative_to(record.library.path).as_posix()
            except (OSError, ValueError):
                continue
            if normalized_query and normalized_query not in relative.casefold():
                continue
            entries.append((path, stat, relative))

        if sort == "name":
            entries.sort(key=lambda item: (item[2].casefold(), -item[1].st_mtime))
        elif sort == "size":
            entries.sort(key=lambda item: (-item[1].st_size, item[2].casefold()))
        else:
            entries.sort(key=lambda item: (-item[1].st_mtime, item[2].casefold()))
            sort = "newest"

        total = len(entries)
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(max(page, 1), page_count)
        start = (page - 1) * page_size
        selected = entries[start : start + page_size]
        images = [self._image_metadata(record, path, stat, relative) for path, stat, relative in selected]
        return {
            "library": self._library_brief(record, scan),
            "images": images,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "page_count": page_count,
                "scan_truncated": scan.truncated,
            },
            "query": normalized_query,
            "sort": sort,
        }

    def get_library(self, library_id: str | int) -> ManagedWallpaperLibrary:
        identifier = clean_text(library_id)
        if not _SAFE_LIBRARY_ID.fullmatch(identifier):
            raise WallpaperDashboardError("壁纸库标识无效。")
        index = int(identifier)
        for record in self._configured_libraries():
            if record.index == index:
                return record
        raise WallpaperDashboardError("找不到指定的壁纸库，可能已被删除或配置已变更。")

    def resolve_image(
        self,
        library_id: str | int,
        relative_path: str,
        *,
        require_previewable: bool = False,
    ) -> tuple[ManagedWallpaperLibrary, Path]:
        record = self.get_library(library_id)
        root = self._available_root(record)
        relative = self._parse_relative_path(relative_path)
        candidate = root.joinpath(*relative.parts)
        self._assert_no_symlink_path(root, relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError as exc:
            raise WallpaperDashboardError("图片文件不存在或已被移动。") from exc
        except (OSError, ValueError) as exc:
            raise WallpaperDashboardError("图片路径不在当前壁纸库内。") from exc
        try:
            if resolved.is_symlink() or not resolved.is_file():
                raise WallpaperDashboardError("目标不是可管理的普通图片文件。")
        except OSError as exc:
            raise WallpaperDashboardError("无法读取图片文件状态。") from exc
        suffix = resolved.suffix.lower()
        if suffix not in self.wallpaper.allowed_extensions():
            raise WallpaperDashboardError("该文件扩展名不在当前壁纸库允许范围内。")
        if require_previewable and suffix not in _SAFE_PREVIEW_EXTENSIONS:
            raise WallpaperDashboardError("该格式不能在控制台内预览，可直接下载原文件。")
        return record, resolved

    def make_thumbnail(self, library_id: str, relative_path: str) -> BytesIO:
        return self._render_preview_jpeg(
            library_id,
            relative_path,
            maximum_dimension=_THUMBNAIL_SIZE,
            maximum_bytes=_MAX_THUMBNAIL_BYTES,
            failure_message="该图片无法生成安全缩略图。",
        )

    def make_thumbnail_data(self, library_id: str, relative_path: str) -> str:
        """Render a small JPEG data URL for an authenticated plugin page."""

        return self._jpeg_data_url(self.make_thumbnail(library_id, relative_path))

    def make_preview_data(self, library_id: str, relative_path: str) -> str:
        """Render a bounded preview without exposing a direct image URL."""

        preview = self._render_preview_jpeg(
            library_id,
            relative_path,
            maximum_dimension=_PREVIEW_SIZE,
            maximum_bytes=_MAX_PREVIEW_BYTES,
            failure_message="该图片无法生成安全预览。",
        )
        return self._jpeg_data_url(preview)

    def image_content_type(self, path: Path) -> str:
        return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")

    def upload_images(
        self,
        library_id: str,
        uploads: Iterable[WallpaperUpload],
    ) -> dict[str, Any]:
        record = self.get_library(library_id)
        root = self._ensure_root(record)
        pending = list(uploads)
        if not pending:
            raise WallpaperDashboardError("请至少选择一张图片。")
        if len(pending) > _MAX_UPLOAD_FILES:
            raise WallpaperDashboardError(f"一次最多上传 {_MAX_UPLOAD_FILES} 张图片。")

        maximum = self.upload_max_bytes()
        total_limit = min(_MAX_WEBUI_UPLOAD_TOTAL_BYTES, maximum * 5)
        total_bytes = 0
        saved: list[dict[str, Any]] = []
        skipped: list[str] = []
        errors: list[dict[str, str]] = []
        for upload in pending:
            filename = self._clean_filename(upload.filename, fallback="wallpaper")
            data = upload.data
            total_bytes += len(data)
            if total_bytes > total_limit:
                errors.append({"filename": filename, "message": "本次上传总大小超过安全限制。"})
                break
            try:
                suffix = Path(filename).suffix.lower()
                if suffix not in _SAFE_PREVIEW_EXTENSIONS or suffix not in self.wallpaper.allowed_extensions():
                    raise WallpaperDashboardError("仅支持 JPG、PNG、WebP 或 GIF 图片。")
                if not data:
                    raise WallpaperDashboardError("图片文件为空。")
                if len(data) > maximum:
                    raise WallpaperDashboardError(f"单张图片不能超过 {maximum // (1024 * 1024)} MB。")
                format_name, _width, _height, _frames = self._validate_image_bytes(data)
                if suffix not in _FORMAT_EXTENSIONS.get(format_name, set()):
                    raise WallpaperDashboardError("文件扩展名与实际图片格式不一致。")
                duplicate = self._find_duplicate(record, data) if self.wallpaper.deduplicate_on_add() else None
                if duplicate is not None:
                    skipped.append(filename)
                    continue
                destination = self._next_available_path(root, filename)
                self._atomic_write(destination, data)
                saved.append(
                    {
                        "name": destination.name,
                        "relative_path": destination.relative_to(root).as_posix(),
                        "bytes": len(data),
                    }
                )
            except WallpaperDashboardError as exc:
                errors.append({"filename": filename, "message": str(exc)})
            except OSError as exc:
                errors.append({"filename": filename, "message": f"文件写入失败：{exc}"})
        return {
            "saved": saved,
            "skipped": skipped,
            "errors": errors,
            "message": self._upload_message(saved, skipped, errors),
        }

    def delete_image(self, library_id: str, relative_path: str) -> dict[str, str]:
        _record, path = self.resolve_image(library_id, relative_path)
        try:
            path.unlink()
        except OSError as exc:
            raise WallpaperDashboardError(f"删除图片失败：{exc}") from exc
        self._remove_registry_path(path)
        return {"name": path.name, "relative_path": relative_path}

    def rename_image(self, library_id: str, relative_path: str, new_name: str) -> dict[str, str]:
        record, source = self.resolve_image(library_id, relative_path)
        suffix = source.suffix.lower()
        requested_stem = Path(self._clean_filename(new_name, fallback=source.stem)).stem
        if not requested_stem or requested_stem in {".", ".."}:
            raise WallpaperDashboardError("请输入有效的新文件名。")
        target = source.with_name(f"{requested_stem}{suffix}")
        try:
            target.relative_to(record.library.path)
        except ValueError as exc:
            raise WallpaperDashboardError("新文件名无效。") from exc
        if target == source:
            return {
                "name": source.name,
                "relative_path": source.relative_to(record.library.path).as_posix(),
            }
        if target.exists():
            raise WallpaperDashboardError("同名文件已经存在。")
        try:
            source.rename(target)
        except OSError as exc:
            raise WallpaperDashboardError(f"重命名图片失败：{exc}") from exc
        self._replace_registry_path(source, target)
        return {
            "name": target.name,
            "relative_path": target.relative_to(record.library.path).as_posix(),
        }

    def save_library(
        self,
        library_id: str | None,
        entry: dict[str, Any],
        *,
        create_directory: bool,
    ) -> dict[str, Any]:
        name = clean_text(entry.get("name"))
        if not name:
            raise WallpaperDashboardError("壁纸库名称不能为空。")
        if len(name) > 100:
            raise WallpaperDashboardError("壁纸库名称不能超过 100 个字符。")
        if "\x00" in clean_text(entry.get("path")):
            raise WallpaperDashboardError("壁纸库路径不能包含空字符。")
        entries = self._libraries_config()
        current_index: int | None = None
        if library_id is not None and clean_text(library_id) != "":
            current_index = self.get_library(library_id).index
        for index, raw in enumerate(entries):
            if index == current_index or not isinstance(raw, dict):
                continue
            existing_name = clean_text(raw.get("name") or raw.get("library_name"))
            if existing_name and existing_name.casefold() == name.casefold():
                raise WallpaperDashboardError("已经有同名壁纸库，请换一个名称。")

        normalized = dict(entry)
        normalized["__template_key"] = "library"
        source_path = self._unresolved_library_path(name, clean_text(normalized.get("path")))
        if source_path.is_symlink():
            raise WallpaperDashboardError("壁纸库目录不能是符号链接，请配置真实目录。")
        root = source_path.resolve(strict=False)
        if root.exists() and not root.is_dir():
            raise WallpaperDashboardError("配置的壁纸库路径是一个文件，不是目录。")
        if create_directory and not root.exists():
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WallpaperDashboardError(f"无法创建壁纸库目录：{exc}") from exc

        old_record: ManagedWallpaperLibrary | None = None
        if current_index is None:
            entries.append(normalized)
            current_index = len(entries) - 1
        else:
            old_record = self.get_library(current_index)
            entries[current_index] = normalized
        record = self.get_library(current_index)
        if old_record is not None and old_record.library.name != record.library.name:
            self._replace_registry_library_name(old_record.library.path, record.library.name)
        scan = self._scan(record)
        return self._library_brief(record, scan)

    def delete_library(
        self,
        library_id: str,
        *,
        delete_files: bool = False,
        confirmation_name: str = "",
    ) -> dict[str, Any]:
        record = self.get_library(library_id)
        root = record.library.path
        files_deleted = False
        if delete_files:
            root = self._library_root(record)
            if clean_text(confirmation_name) != record.library.name:
                raise WallpaperDashboardError("请输入完全一致的图库名称后再删除磁盘文件。")
            if root.exists():
                self._assert_library_root_can_be_deleted(record, root)
                try:
                    shutil.rmtree(root)
                except OSError as exc:
                    raise WallpaperDashboardError(f"删除图库目录失败：{exc}") from exc
                self._remove_registry_under_root(root)
                files_deleted = True
        entries = self._libraries_config()
        entries.pop(record.index)
        if delete_files:
            message = (
                "已删除壁纸库配置和磁盘中的图库目录。"
                if files_deleted
                else "图库目录原本不存在，已删除壁纸库配置。"
            )
        else:
            message = "已删除壁纸库配置，磁盘中的图片文件保持不变。"
        return {
            "name": record.library.name,
            "resolved_path": str(root),
            "files_deleted": files_deleted,
            "message": message,
        }

    def library_entry(self, library_id: str) -> dict[str, Any]:
        record = self.get_library(library_id)
        return {
            "id": record.identifier,
            "name": record.library.name,
            "path": clean_text(record.row.get("path")),
            "commands": list(record.library.commands),
            "caption": record.library.caption,
            "send_mode": clean_text(record.row.get("send_mode"), "同一条消息"),
            "recursive": record.library.recursive,
        }

    def upload_max_bytes(self) -> int:
        return min(max(self.wallpaper.max_add_bytes(), 64 * 1024), _MAX_WEBUI_UPLOAD_BYTES)

    def _configured_libraries(self) -> list[ManagedWallpaperLibrary]:
        records: list[ManagedWallpaperLibrary] = []
        for index, raw in enumerate(self._libraries_config()):
            if not isinstance(raw, dict):
                continue
            name = clean_text(raw.get("name") or raw.get("library_name"))
            if not name:
                continue
            raw_path = clean_text(raw.get("path"))
            source_path = self._unresolved_library_path(name, raw_path)
            library = WallpaperLibrary(
                name=name,
                path=source_path.resolve(strict=False),
                commands=self._string_list(raw.get("commands"), [name]),
                caption=clean_text(raw.get("caption"), "随机给你抽一张 {library}。"),
                send_mode=clean_text(raw.get("send_mode"), "together"),
                recursive=read_bool(raw.get("recursive"), False),
            )
            records.append(
                ManagedWallpaperLibrary(
                    index=index,
                    row=raw,
                    library=library,
                    source_path=source_path,
                )
            )
        return records

    def _libraries_config(self) -> list[dict[str, Any]]:
        config = self.wallpaper.config
        if not isinstance(config, dict):
            raise WallpaperDashboardError("当前插件配置不可写，无法管理壁纸库。")
        wallpaper = config.get("wallpaper")
        if not isinstance(wallpaper, dict):
            wallpaper = {}
            config["wallpaper"] = wallpaper
        libraries = wallpaper.get("libraries")
        if not isinstance(libraries, list):
            libraries = []
            wallpaper["libraries"] = libraries
        return libraries

    def _unresolved_library_path(self, name: str, raw_path: str) -> Path:
        source = Path(raw_path).expanduser() if raw_path else Path("wallpapers") / self._safe_library_name(name)
        if not source.is_absolute():
            source = self.data_dir / source
        return source

    def _scan(self, record: ManagedWallpaperLibrary) -> WallpaperScan:
        try:
            root = self._library_root(record)
        except WallpaperDashboardError as exc:
            return WallpaperScan([], "unsafe", str(exc))
        if not root.exists():
            return WallpaperScan([], "missing", "目录尚未创建，可通过上传图片或编辑图库时创建。")
        if not root.is_dir():
            return WallpaperScan([], "not_directory", "配置路径不是目录。")

        files: list[Path] = []
        scanned = 0
        stack = [root]
        allowed = self.wallpaper.allowed_extensions()
        try:
            while stack:
                current = stack.pop()
                with os.scandir(current) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > _MAX_LIBRARY_SCAN_ENTRIES:
                            return WallpaperScan(
                                files,
                                "ready",
                                f"为避免影响 Bot 运行，最多扫描 {_MAX_LIBRARY_SCAN_ENTRIES} 个目录项。",
                                truncated=True,
                            )
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if record.library.recursive:
                                    stack.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            path = Path(entry.path)
                            if path.suffix.lower() in allowed:
                                files.append(path)
                        except OSError:
                            continue
        except OSError as exc:
            return WallpaperScan(files, "ready", f"部分目录无法扫描：{exc}")
        return WallpaperScan(files, "ready")

    def _available_root(self, record: ManagedWallpaperLibrary) -> Path:
        root = self._library_root(record)
        if not root.exists():
            raise WallpaperDashboardError("该壁纸库目录尚未创建。")
        if not root.is_dir():
            raise WallpaperDashboardError("配置的壁纸库路径不是目录。")
        return root

    def _ensure_root(self, record: ManagedWallpaperLibrary) -> Path:
        root = self._library_root(record)
        if root.exists() and not root.is_dir():
            raise WallpaperDashboardError("配置的壁纸库路径不是目录。")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WallpaperDashboardError(f"无法创建壁纸库目录：{exc}") from exc
        return root

    def _assert_library_root_can_be_deleted(
        self,
        record: ManagedWallpaperLibrary,
        root: Path,
    ) -> None:
        """Reject high-risk or overlapping roots before a recursive deletion."""

        try:
            if root.is_symlink():
                raise WallpaperDashboardError("壁纸库目录是符号链接，不能递归删除。")
            if not root.is_dir():
                raise WallpaperDashboardError("配置的壁纸库路径不是目录。")
            if root.parent == root:
                raise WallpaperDashboardError("不能删除文件系统根目录。")
            data_root = self.data_dir.resolve(strict=False)
            wallpaper_root = (data_root / "wallpapers").resolve(strict=False)
        except WallpaperDashboardError:
            raise
        except OSError as exc:
            raise WallpaperDashboardError("无法安全验证壁纸库目录。") from exc

        if root in {data_root, wallpaper_root}:
            raise WallpaperDashboardError("不能删除插件数据目录或 wallpapers 总目录。")

        for other in self._configured_libraries():
            if other.index == record.index:
                continue
            other_root = self._library_root(other)
            if self._paths_overlap(root, other_root):
                raise WallpaperDashboardError(
                    "该图库目录与另一条壁纸库配置重叠，不能删除磁盘文件。"
                )

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        try:
            return first == second or first.is_relative_to(second) or second.is_relative_to(first)
        except ValueError:
            return False

    def _library_root(self, record: ManagedWallpaperLibrary) -> Path:
        if record.source_path.is_symlink():
            raise WallpaperDashboardError("壁纸库目录是符号链接，控制台拒绝管理该目录。")
        try:
            root = record.source_path.resolve(strict=False)
        except OSError as exc:
            raise WallpaperDashboardError("壁纸库路径无法解析。") from exc
        return root

    def _assert_no_symlink_path(self, root: Path, parts: tuple[str, ...]) -> None:
        current = root
        for part in parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise WallpaperDashboardError("图片路径包含符号链接，控制台拒绝访问。")
            except OSError as exc:
                raise WallpaperDashboardError("图片路径无法安全验证。") from exc

    def _parse_relative_path(self, value: str) -> PurePosixPath:
        raw = clean_text(value)
        if not raw or "\\" in raw or "\x00" in raw:
            raise WallpaperDashboardError("图片相对路径无效。")
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise WallpaperDashboardError("图片相对路径无效。")
        if any(":" in part for part in path.parts):
            raise WallpaperDashboardError("图片相对路径无效。")
        return path

    def _image_metadata(
        self,
        record: ManagedWallpaperLibrary,
        path: Path,
        stat: os.stat_result,
        relative: str,
    ) -> dict[str, Any]:
        width: int | None = None
        height: int | None = None
        format_name = ""
        frames = 0
        readable = False
        error = ""
        if path.suffix.lower() in _SAFE_PREVIEW_EXTENSIONS:
            try:
                with self._open_image(path) as image:
                    width, height = image.size
                    self._assert_image_size((width, height))
                    format_name = clean_text(image.format, path.suffix.lstrip(".").upper())
                    frames = int(getattr(image, "n_frames", 1) or 1)
                    readable = True
            except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as exc:
                error = "图片格式损坏或超出预览安全限制。"
        return {
            "name": path.name,
            "relative_path": relative,
            "bytes": stat.st_size,
            "modified_at": self._format_timestamp(stat.st_mtime),
            "width": width,
            "height": height,
            "format": format_name or path.suffix.lstrip(".").upper(),
            "frames": frames,
            "animated": frames > 1,
            "preview_supported": path.suffix.lower() in _SAFE_PREVIEW_EXTENSIONS and readable,
            "readable": readable,
            "error": error,
            "library_id": record.identifier,
        }

    def _library_brief(self, record: ManagedWallpaperLibrary, scan: WallpaperScan) -> dict[str, Any]:
        return {
            "id": record.identifier,
            "name": record.library.name,
            "resolved_path": str(record.library.path),
            "state": scan.state,
            "detail": scan.detail,
            "image_count": len(scan.files),
            "scan_truncated": scan.truncated,
            "recursive": record.library.recursive,
        }

    def _validate_image_bytes(self, data: bytes) -> tuple[str, int, int, int]:
        try:
            with self._open_image(BytesIO(data)) as image:
                width, height = image.size
                self._assert_image_size((width, height))
                format_name = clean_text(image.format).upper()
                frames = int(getattr(image, "n_frames", 1) or 1)
                image.verify()
            if format_name not in _FORMAT_EXTENSIONS:
                raise WallpaperDashboardError("图片格式不受控制台支持。")
            return format_name, width, height, frames
        except WallpaperDashboardError:
            raise
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as exc:
            raise WallpaperDashboardError("文件不是可读取的图片，或图片已损坏。") from exc

    def _render_preview_jpeg(
        self,
        library_id: str,
        relative_path: str,
        *,
        maximum_dimension: int,
        maximum_bytes: int,
        failure_message: str,
    ) -> BytesIO:
        _record, path = self.resolve_image(
            library_id,
            relative_path,
            require_previewable=True,
        )
        try:
            with self._open_image(path) as source:
                source.seek(0)
                image = ImageOps.exif_transpose(source)
                self._assert_image_size(image.size)
                image = self._prepare_preview_image(image)
                return self._encode_bounded_jpeg(
                    image,
                    maximum_dimension=maximum_dimension,
                    maximum_bytes=maximum_bytes,
                )
        except WallpaperDashboardError:
            raise
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as exc:
            raise WallpaperDashboardError(failure_message) from exc

    @staticmethod
    def _prepare_preview_image(image: Image.Image) -> Image.Image:
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "#111827")
            background.paste(image, mask=image.getchannel("A"))
            return background
        return image.convert("RGB")

    def _encode_bounded_jpeg(
        self,
        image: Image.Image,
        *,
        maximum_dimension: int,
        maximum_bytes: int,
    ) -> BytesIO:
        dimension = maximum_dimension
        while dimension >= 160:
            candidate = image.copy()
            candidate.thumbnail((dimension, dimension), self._resampling_filter())
            for quality in (86, 78, 70, 62):
                output = BytesIO()
                candidate.save(output, format="JPEG", quality=quality, optimize=True)
                if output.tell() <= maximum_bytes:
                    output.seek(0)
                    return output
            dimension = int(dimension * 0.72)
        raise WallpaperDashboardError("图片内容过大，无法生成受限预览。")

    @staticmethod
    def _jpeg_data_url(image: BytesIO) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode("ascii")

    @staticmethod
    @contextmanager
    def _open_image(source: Path | BytesIO):
        """Open an image while treating Pillow's decompression warning as fatal."""

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                yield image

    @staticmethod
    def _assert_image_size(size: tuple[int, int]) -> None:
        width, height = size
        if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
            raise WallpaperDashboardError("图片像素过大，不能由控制台安全处理。")

    def _find_duplicate(self, record: ManagedWallpaperLibrary, data: bytes) -> Path | None:
        digest = hashlib.sha256(data).digest()
        scan = self._scan(record)
        for path in scan.files:
            try:
                if path.stat().st_size != len(data):
                    continue
                if self._file_digest(path) == digest:
                    return path
            except OSError:
                continue
        return None

    @staticmethod
    def _file_digest(path: Path) -> bytes:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.digest()

    def _next_available_path(self, root: Path, filename: str) -> Path:
        original = Path(filename)
        stem = original.stem or "wallpaper"
        suffix = original.suffix.lower()
        for index in range(1, 10_000):
            candidate_name = f"{stem}{suffix}" if index == 1 else f"{stem}_{index}{suffix}"
            candidate = root / candidate_name
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise WallpaperDashboardError("无法生成不重复的图片文件名。")

    @staticmethod
    def _atomic_write(destination: Path, data: bytes) -> None:
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_registry_path(self, path: Path) -> None:
        registry = self.wallpaper.load_registry()
        self.wallpaper.remove_registry_path(registry, path)
        self.wallpaper.save_registry(registry)

    def _replace_registry_path(self, source: Path, target: Path) -> None:
        registry = self.wallpaper.load_registry()
        old_path = str(source.resolve(strict=False))
        new_path = str(target.resolve(strict=False))
        changed = False
        for value in registry.values():
            if isinstance(value, dict) and clean_text(value.get("path")) == old_path:
                value["path"] = new_path
                changed = True
        if changed:
            self.wallpaper.save_registry(registry)

    def _replace_registry_library_name(self, root: Path, name: str) -> None:
        registry = self.wallpaper.load_registry()
        changed = False
        resolved_root = root.resolve(strict=False)
        for value in registry.values():
            if not isinstance(value, dict):
                continue
            try:
                Path(clean_text(value.get("path"))).resolve(strict=False).relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            value["library"] = name
            changed = True
        if changed:
            self.wallpaper.save_registry(registry)

    def _remove_registry_under_root(self, root: Path) -> None:
        registry = self.wallpaper.load_registry()
        changed = False
        for key, value in list(registry.items()):
            if not isinstance(value, dict):
                continue
            try:
                recorded_path = Path(clean_text(value.get("path"))).resolve(strict=False)
                recorded_path.relative_to(root)
            except (OSError, ValueError):
                continue
            registry.pop(key, None)
            changed = True
        if changed:
            self.wallpaper.save_registry(registry)

    @staticmethod
    def _clean_filename(value: str, *, fallback: str) -> str:
        raw = clean_text(value).replace("\\", "/").rsplit("/", 1)[-1]
        if not raw:
            raw = fallback
        suffix = Path(raw).suffix.lower()
        stem = Path(raw).stem
        clean_stem = "".join(
            "_" if character in _INVALID_FILENAME_CHARACTERS or ord(character) < 32 else character
            for character in stem
        ).strip(" .")[:120]
        if not clean_stem:
            clean_stem = fallback
        if len(suffix) > 12 or any(character in _INVALID_FILENAME_CHARACTERS for character in suffix):
            suffix = ""
        return f"{clean_stem}{suffix}"

    @staticmethod
    def _safe_library_name(value: str) -> str:
        cleaned = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
        return cleaned.strip("._") or "wallpaper"

    @staticmethod
    def _string_list(value: Any, default: list[str]) -> list[str]:
        if not isinstance(value, list):
            return list(default)
        result = [clean_text(item) for item in value]
        result = [item for item in result if item]
        return result or list(default)

    @staticmethod
    def _resampling_filter():
        return getattr(Image, "Resampling", Image).LANCZOS

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        if timestamp <= 0:
            return ""
        try:
            return datetime.fromtimestamp(timestamp, UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    @staticmethod
    def _upload_message(saved: list[Any], skipped: list[str], errors: list[Any]) -> str:
        parts: list[str] = []
        if saved:
            parts.append(f"已添加 {len(saved)} 张图片")
        if skipped:
            parts.append(f"跳过 {len(skipped)} 张重复图片")
        if errors:
            parts.append(f"{len(errors)} 张图片未能添加")
        return "，".join(parts) + "。" if parts else "没有可添加的图片。"
