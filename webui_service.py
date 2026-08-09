"""Authenticated AstrBot plugin-page API for Helper Tools management."""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import math
import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from quart import jsonify, request, send_file

from .helper_utils import cfg, clean_text, extract_file_config_value, read_bool
from .webui_activity import WebUiActivityLog
from .webui_wallpaper import (
    WallpaperDashboardError,
    WallpaperLibraryDashboard,
    WallpaperUpload,
)
from .wallpaper_service import WallpaperService


PLUGIN_ID = "astrbot_plugin_helper_tools"
_SCHEMA_PATH = Path(__file__).with_name("_conf_schema.json")
_UPLOAD_DIRECTORY_NAME = "webui_uploads"
_MAX_UPLOAD_BYTES = 24 * 1024 * 1024
_MAX_DASHBOARD_DATA_URL_BYTES = 64 * 1024 * 1024
_MAX_WALLPAPER_UPLOAD_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_TEXT_LENGTH = 200_000
_MAX_LIST_ITEMS = 2_000
_MAX_TEMPLATE_ITEMS = 300
_MAX_ABSOLUTE_NUMBER = 1_000_000_000
_SECRET_ACTION_KEY = "__helper_tools_secret_action"
_SECRET_REPLACE = "replace"
_SECRET_CLEAR = "clear"
_SECRET_KEEP = "keep"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "cookie",
    "token",
    "secret",
    "password",
    "authorization",
)
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
_SAFE_FILE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


class DashboardValidationError(ValueError):
    """A bad dashboard request that is safe to display to its administrator."""


class HelperToolsDashboard:
    """Backend for the native AstrBot plugin page.

    The page consumes the same configuration schema as AstrBot's normal
    configuration page. This keeps future modules maintainable: a new schema
    section automatically appears here, including nested objects and template
    lists, without a parallel handwritten form.
    """

    def __init__(self, plugin: Any, *, version: str) -> None:
        self.plugin = plugin
        self.version = version
        self.data_dir = Path(plugin.data_dir)
        self.activity = WebUiActivityLog(plugin.config, self.data_dir)
        self._schema = self._load_schema()
        self._lock = asyncio.Lock()
        wallpaper = getattr(plugin, "wallpaper", None)
        if not isinstance(wallpaper, WallpaperService):
            wallpaper = WallpaperService(plugin.config, self.data_dir, getattr(plugin, "context", None))
        self.wallpaper_dashboard = WallpaperLibraryDashboard(wallpaper, self.data_dir)

    def register(self) -> None:
        routes = (
            ("state", self.get_state, ["GET"], "Get Helper Tools Dashboard state"),
            ("save_config", self.save_config, ["POST"], "Save Helper Tools configuration"),
            ("save_theme", self.save_theme, ["POST"], "Save Helper Tools Dashboard theme"),
            ("activities", self.get_activities, ["GET"], "Get Helper Tools activity records"),
            ("clear_activities", self.clear_activities, ["POST"], "Clear Helper Tools activity records"),
            ("storage", self.get_storage, ["GET"], "Get Helper Tools storage summary"),
            ("upload_file", self.upload_file, ["POST"], "Upload one Helper Tools configuration file"),
            ("clear_file", self.clear_file, ["POST"], "Clear one Helper Tools configuration file"),
            ("wallpaper_libraries", self.get_wallpaper_libraries, ["GET"], "Get wallpaper library summaries"),
            ("wallpaper_images", self.get_wallpaper_images, ["GET"], "List wallpaper library images"),
            ("wallpaper_thumbnail", self.get_wallpaper_thumbnail, ["GET"], "Get wallpaper image thumbnail"),
            ("wallpaper_thumbnail_data", self.get_wallpaper_thumbnail_data, ["GET"], "Get authenticated wallpaper thumbnail data"),
            ("wallpaper_file", self.get_wallpaper_file, ["GET"], "View wallpaper image file"),
            ("wallpaper_preview_data", self.get_wallpaper_preview_data, ["GET"], "Get authenticated wallpaper preview data"),
            ("wallpaper_download", self.download_wallpaper_file, ["GET"], "Download wallpaper image file"),
            ("wallpaper_upload", self.upload_wallpaper_images, ["POST"], "Upload wallpaper images"),
            ("wallpaper_upload_file/<library_id>", self.upload_wallpaper_file, ["POST"], "Upload one wallpaper image"),
            ("wallpaper_save_library", self.save_wallpaper_library, ["POST"], "Create or update wallpaper library"),
            ("wallpaper_delete_library", self.delete_wallpaper_library, ["POST"], "Delete wallpaper library configuration"),
            ("wallpaper_delete_image", self.delete_wallpaper_image, ["POST"], "Delete wallpaper image"),
            ("wallpaper_rename_image", self.rename_wallpaper_image, ["POST"], "Rename wallpaper image"),
        )
        for endpoint, handler, methods, description in routes:
            self.plugin.context.register_web_api(
                f"/{PLUGIN_ID}/{endpoint}",
                handler,
                methods,
                description,
            )

    async def get_state(self):
        public_config, secret_state, file_state = self._public_config()
        storage = await asyncio.to_thread(self._storage_snapshot)
        modules = self._module_summaries(public_config)
        llm_tools = self._enabled_llm_tools()
        activity_summary = self.activity.summary()
        return jsonify(
            {
                "success": True,
                "version": self.version,
                "schema": self._schema,
                "config": public_config,
                "secret_state": secret_state,
                "file_state": file_state,
                "theme": self._theme_value(),
                "modules": modules,
                "metrics": {
                    "modules_total": len(modules),
                    "modules_enabled": sum(1 for item in modules if item["enabled"]),
                    "llm_tools_enabled": len(llm_tools),
                    "storage_bytes": storage["total_bytes"],
                    "activity_today": activity_summary["today"],
                },
                "llm_tools": llm_tools,
                "runtime": self._runtime_snapshot(file_state, llm_tools=llm_tools),
                "storage": storage,
                "activity_summary": activity_summary,
                "recent_activities": self.activity.get_records(limit=8),
                "config_updated_at": self._config_modified_at(),
            }
        )

    async def save_config(self):
        payload = await self._json_body()
        incoming = payload.get("config")
        if not isinstance(incoming, dict):
            return self._error("配置内容格式不正确。", 400)

        tools_updated: list[str] = []
        async with self._lock:
            try:
                current = self._config_mapping()
                changed_modules: list[str] = []
                for module_name, schema_entry in self._schema.items():
                    if module_name not in incoming:
                        continue
                    old_value = deepcopy(current.get(module_name))
                    next_value = self._coerce_value(
                        schema_entry,
                        incoming[module_name],
                        old_value,
                        (module_name,),
                    )
                    if old_value != next_value:
                        current[module_name] = next_value
                        changed_modules.append(module_name)
                if changed_modules:
                    self._save_plugin_config()
                    tools_updated = self._refresh_registered_llm_tools(changed_modules)
            except DashboardValidationError as exc:
                return self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001 - API must not expose tracebacks
                logger.exception("[HelperTools/WebUI] configuration save failed")
                return self._error(f"保存失败：{type(exc).__name__}", 500)

        detail = "未发现有效更改。"
        if changed_modules:
            visible = ", ".join(changed_modules[:6])
            suffix = " 等" if len(changed_modules) > 6 else ""
            detail = f"已更新 {len(changed_modules)} 个模块：{visible}{suffix}。"
        self.activity.record("webui", "保存配置", detail=detail)
        public_config, secret_state, file_state = self._public_config()
        return jsonify(
            {
                "success": True,
                "message": detail,
                "config": public_config,
                "secret_state": secret_state,
                "file_state": file_state,
                "changed_modules": changed_modules,
                "tools_updated": tools_updated,
                "reload_recommended": self._reload_recommended(changed_modules),
            }
        )

    async def save_theme(self):
        payload = await self._json_body()
        theme = clean_text(payload.get("theme", "")).lower()
        if theme not in {"dark", "light"}:
            return self._error("主题只能是夜间或普通。", 400)
        async with self._lock:
            section = self._ensure_config_section("webui")
            section["dashboard_theme"] = theme
            try:
                self._save_plugin_config()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] theme save failed")
                return self._error(f"主题保存失败：{type(exc).__name__}", 500)
        self.activity.record("webui", "切换主题", detail="已切换为夜间主题。" if theme == "dark" else "已切换为普通主题。")
        return jsonify({"success": True, "theme": theme, "message": "主题已保存。"})

    async def get_activities(self):
        limit = self._int_query("limit", 100, 1, 500)
        module = clean_text(request.args.get("module", ""))
        status = clean_text(request.args.get("status", ""))
        return jsonify(
            {
                "success": True,
                "records": self.activity.get_records(
                    limit=limit,
                    module=module,
                    status=status,
                ),
                "summary": self.activity.summary(),
            }
        )

    async def clear_activities(self):
        async with self._lock:
            deleted = self.activity.clear()
        return jsonify({"success": True, "message": f"已清空 {deleted} 条本地记录。"})

    async def get_storage(self):
        public_config, _secret_state, file_state = self._public_config()
        storage = await asyncio.to_thread(self._storage_snapshot)
        return jsonify(
            {
                "success": True,
                "storage": storage,
                "runtime": self._runtime_snapshot(file_state),
                "modules": self._module_summaries(public_config),
            }
        )

    async def upload_file(self):
        payload = await self._json_body()
        raw_path = clean_text(payload.get("path", ""))
        filename = clean_text(payload.get("filename", ""))
        data_url = payload.get("data_url")
        try:
            schema_entry = self._schema_entry_for_path(raw_path)
            if schema_entry.get("type") != "file":
                raise DashboardValidationError("这个配置项不接受文件上传。")
            file_bytes = self._decode_data_url(data_url)
            if not filename:
                raise DashboardValidationError("请选择要上传的文件。")
            extension = Path(filename).suffix.lower()
            allowed_extensions = {
                str(item).lower()
                for item in schema_entry.get("file_types", [])
                if isinstance(item, str) and item.startswith(".")
            }
            if allowed_extensions and extension not in allowed_extensions:
                expected = "、".join(sorted(allowed_extensions))
                raise DashboardValidationError(f"该配置项只接受 {expected} 文件。")
            if not extension or len(extension) > 16:
                raise DashboardValidationError("文件扩展名无效。")
        except DashboardValidationError as exc:
            return self._error(str(exc), 400)

        async with self._lock:
            try:
                stored_path = await asyncio.to_thread(
                    self._write_uploaded_file,
                    raw_path,
                    filename,
                    file_bytes,
                )
                previous = self._value_at_config_path(raw_path)
                self._set_config_path(raw_path, [str(stored_path)])
                self._save_plugin_config()
                await asyncio.to_thread(self._delete_owned_uploaded_file, previous)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] file upload failed path=%s", raw_path)
                return self._error(f"文件保存失败：{type(exc).__name__}", 500)

        self.activity.record("webui", "上传配置文件", detail=f"已更新 {raw_path}。")
        return jsonify(
            {
                "success": True,
                "message": "文件已保存。部分模块需要重载插件后才会读取新文件。",
                "file_state": self._file_state(),
                "reload_recommended": True,
            }
        )

    async def clear_file(self):
        payload = await self._json_body()
        raw_path = clean_text(payload.get("path", ""))
        try:
            schema_entry = self._schema_entry_for_path(raw_path)
            if schema_entry.get("type") != "file":
                raise DashboardValidationError("这个配置项不是文件配置。")
        except DashboardValidationError as exc:
            return self._error(str(exc), 400)

        async with self._lock:
            try:
                previous = self._value_at_config_path(raw_path)
                self._set_config_path(raw_path, [])
                self._save_plugin_config()
                await asyncio.to_thread(self._delete_owned_uploaded_file, previous)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] file clear failed path=%s", raw_path)
                return self._error(f"清除文件失败：{type(exc).__name__}", 500)
        self.activity.record("webui", "清除配置文件", detail=f"已清除 {raw_path}。")
        return jsonify({"success": True, "message": "文件配置已清除。", "file_state": self._file_state()})

    async def get_wallpaper_libraries(self):
        try:
            result = await asyncio.to_thread(self.wallpaper_dashboard.list_libraries)
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001 - dashboard should not expose tracebacks
            logger.exception("[HelperTools/WebUI] wallpaper library scan failed")
            return self._error(f"读取壁纸库失败：{type(exc).__name__}", 500)
        return jsonify({"success": True, **result})

    async def get_wallpaper_images(self):
        library_id = clean_text(request.args.get("library_id", ""))
        query = clean_text(request.args.get("query", ""))
        sort = clean_text(request.args.get("sort", "newest")).lower()
        page = self._int_query("page", 1, 1, 100_000)
        page_size = self._int_query("page_size", 36, 12, 96)
        try:
            result = await asyncio.to_thread(
                self.wallpaper_dashboard.list_images,
                library_id,
                page=page,
                page_size=page_size,
                query=query,
                sort=sort,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[HelperTools/WebUI] wallpaper image listing failed")
            return self._error(f"读取壁纸图片失败：{type(exc).__name__}", 500)
        return jsonify({"success": True, **result})

    async def get_wallpaper_thumbnail(self):
        library_id = clean_text(request.args.get("library_id", ""))
        relative_path = clean_text(request.args.get("path", ""))
        try:
            thumbnail = await asyncio.to_thread(
                self.wallpaper_dashboard.make_thumbnail,
                library_id,
                relative_path,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/WebUI] wallpaper thumbnail failed: %r", exc)
            return self._error("无法生成壁纸缩略图。", 500)
        return await send_file(
            thumbnail,
            mimetype="image/jpeg",
            cache_timeout=600,
        )

    async def get_wallpaper_thumbnail_data(self):
        library_id = clean_text(request.args.get("library_id", ""))
        relative_path = clean_text(request.args.get("path", ""))
        try:
            data_url = await asyncio.to_thread(
                self.wallpaper_dashboard.make_thumbnail_data,
                library_id,
                relative_path,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/WebUI] wallpaper thumbnail data failed: %r", exc)
            return self._error("无法生成壁纸缩略图。", 500)
        return jsonify({"success": True, "data_url": data_url})

    async def get_wallpaper_file(self):
        library_id = clean_text(request.args.get("library_id", ""))
        relative_path = clean_text(request.args.get("path", ""))
        try:
            _record, path = await asyncio.to_thread(
                self.wallpaper_dashboard.resolve_image,
                library_id,
                relative_path,
                require_previewable=True,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/WebUI] wallpaper file lookup failed: %r", exc)
            return self._error("无法读取壁纸文件。", 500)
        return await send_file(
            path,
            mimetype=self.wallpaper_dashboard.image_content_type(path),
            cache_timeout=300,
            conditional=True,
        )

    async def get_wallpaper_preview_data(self):
        library_id = clean_text(request.args.get("library_id", ""))
        relative_path = clean_text(request.args.get("path", ""))
        try:
            data_url = await asyncio.to_thread(
                self.wallpaper_dashboard.make_preview_data,
                library_id,
                relative_path,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/WebUI] wallpaper preview data failed: %r", exc)
            return self._error("无法生成壁纸预览。", 500)
        return jsonify({"success": True, "data_url": data_url})

    async def download_wallpaper_file(self):
        library_id = clean_text(request.args.get("library_id", ""))
        relative_path = clean_text(request.args.get("path", ""))
        try:
            _record, path = await asyncio.to_thread(
                self.wallpaper_dashboard.resolve_image,
                library_id,
                relative_path,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HelperTools/WebUI] wallpaper download lookup failed: %r", exc)
            return self._error("无法读取壁纸文件。", 500)
        return await send_file(
            path,
            mimetype="application/octet-stream",
            as_attachment=True,
            attachment_filename=path.name,
            cache_timeout=0,
            conditional=True,
        )

    async def upload_wallpaper_images(self):
        try:
            library_id, uploads = await self._wallpaper_upload_request()
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)

        return await self._save_wallpaper_uploads(library_id, uploads)

    async def upload_wallpaper_file(self, library_id: str):
        """Receive one bridge-proxied image without relying on iframe cookies."""

        try:
            files = await self._request_part("files")
            item = files.get("file") if files is not None else None
            if item is None:
                raise WallpaperDashboardError("没有收到要上传的图片。")
            maximum = self.wallpaper_dashboard.upload_max_bytes()
            data = await self._read_uploaded_file(item, maximum + 1)
            if len(data) > maximum:
                raise WallpaperDashboardError("单张图片超过当前壁纸库允许大小。")
            upload = WallpaperUpload(
                clean_text(getattr(item, "filename", ""), "wallpaper"),
                data,
            )
        except WallpaperDashboardError as exc:
            return self._error(str(exc), 400)

        return await self._save_wallpaper_uploads(clean_text(library_id), [upload])

    async def _save_wallpaper_uploads(
        self,
        library_id: str,
        uploads: list[WallpaperUpload],
    ):

        async with self._lock:
            try:
                result = await asyncio.to_thread(
                    self.wallpaper_dashboard.upload_images,
                    library_id,
                    uploads,
                )
            except WallpaperDashboardError as exc:
                return self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] wallpaper upload failed")
                return self._error(f"上传壁纸失败：{type(exc).__name__}", 500)
        self.activity.record(
            "wallpaper",
            "控制台上传壁纸",
            status="success" if result["saved"] else "warning",
            detail=result["message"],
        )
        return jsonify({"success": True, **result})

    async def save_wallpaper_library(self):
        payload = await self._json_body()
        raw_library_id = payload.get("library_id")
        library_id = clean_text(raw_library_id) if raw_library_id is not None else None
        if library_id == "":
            library_id = None
        raw_entry = payload.get("library")
        create_directory = self._as_bool(payload.get("create_directory", True))
        async with self._lock:
            try:
                entry = self._coerce_wallpaper_library_entry(raw_entry, library_id)
                result = await asyncio.to_thread(
                    self.wallpaper_dashboard.save_library,
                    library_id,
                    entry,
                    create_directory=create_directory,
                )
                self._save_plugin_config()
            except WallpaperDashboardError as exc:
                return self._error(str(exc), 400)
            except DashboardValidationError as exc:
                return self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] wallpaper library save failed")
                return self._error(f"保存壁纸库失败：{type(exc).__name__}", 500)
        self.activity.record("wallpaper", "控制台保存壁纸库", detail=f"已保存图库“{result['name']}”。")
        return jsonify({"success": True, "library": result, "message": "壁纸库配置已保存。"})

    async def delete_wallpaper_library(self):
        payload = await self._json_body()
        delete_files = self._as_bool(payload.get("delete_files", False))
        if not self._as_bool(payload.get("confirm", False)):
            return self._error("删除壁纸库前需要确认。", 400)
        library_id = clean_text(payload.get("library_id", ""))
        confirmation_name = clean_text(payload.get("confirmation_name", ""))
        async with self._lock:
            try:
                result = await asyncio.to_thread(
                    self.wallpaper_dashboard.delete_library,
                    library_id,
                    delete_files=delete_files,
                    confirmation_name=confirmation_name,
                )
                self._save_plugin_config()
            except WallpaperDashboardError as exc:
                return self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] wallpaper library delete failed")
                return self._error(f"删除壁纸库失败：{type(exc).__name__}", 500)
        self.activity.record(
            "wallpaper",
            "控制台删除壁纸库",
            detail=("已删除图库配置及磁盘文件。" if delete_files else "已删除图库配置，图片文件未删除。"),
        )
        return jsonify({"success": True, "message": result["message"], "deleted": result})

    async def delete_wallpaper_image(self):
        payload = await self._json_body()
        if not self._as_bool(payload.get("confirm", False)):
            return self._error("删除图片前需要确认。", 400)
        library_id = clean_text(payload.get("library_id", ""))
        relative_path = clean_text(payload.get("path", ""))
        async with self._lock:
            try:
                result = await asyncio.to_thread(
                    self.wallpaper_dashboard.delete_image,
                    library_id,
                    relative_path,
                )
            except WallpaperDashboardError as exc:
                return self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] wallpaper image delete failed")
                return self._error(f"删除壁纸图片失败：{type(exc).__name__}", 500)
        self.activity.record("wallpaper", "控制台删除壁纸图片", detail="已删除一张壁纸图片。")
        return jsonify({"success": True, "deleted": result, "message": "壁纸图片已删除。"})

    async def rename_wallpaper_image(self):
        payload = await self._json_body()
        library_id = clean_text(payload.get("library_id", ""))
        relative_path = clean_text(payload.get("path", ""))
        new_name = clean_text(payload.get("new_name", ""))
        async with self._lock:
            try:
                result = await asyncio.to_thread(
                    self.wallpaper_dashboard.rename_image,
                    library_id,
                    relative_path,
                    new_name,
                )
            except WallpaperDashboardError as exc:
                return self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[HelperTools/WebUI] wallpaper image rename failed")
                return self._error(f"重命名壁纸图片失败：{type(exc).__name__}", 500)
        self.activity.record("wallpaper", "控制台重命名壁纸图片", detail="已重命名一张壁纸图片。")
        return jsonify({"success": True, "image": result, "message": "壁纸图片已重命名。"})

    def record_activity(
        self,
        module: str,
        action: str,
        *,
        status: str = "success",
        detail: str = "",
        event: Any = None,
    ) -> None:
        """Expose the privacy-safe recorder to the main plugin orchestration."""

        self.activity.record(module, action, status=status, detail=detail, event=event)

    def _load_schema(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Helper Tools configuration schema cannot be read: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("Helper Tools configuration schema root must be an object")
        return {
            str(name): value
            for name, value in raw.items()
            if isinstance(name, str) and isinstance(value, dict)
        }

    async def _json_body(self) -> dict[str, Any]:
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    async def _wallpaper_upload_request(self) -> tuple[str, list[WallpaperUpload]]:
        """Read either native multipart uploads or a small JSON fallback.

        Plugin pages use multipart data so image bytes do not need a Base64
        copy in the browser. JSON remains useful for API callers and tests.
        """

        maximum = self.wallpaper_dashboard.upload_max_bytes()
        if request.mimetype == "multipart/form-data":
            form = await self._request_part("form") or {}
            files = await self._request_part("files")
            library_id = clean_text(form.get("library_id", ""))
            incoming = (
                files.getlist("files") or files.getlist("file")
                if files is not None
                else []
            )
            if not incoming:
                raise WallpaperDashboardError("请至少选择一张图片。")
            if len(incoming) > 24:
                raise WallpaperDashboardError("一次最多上传 24 张图片。")
            uploads: list[WallpaperUpload] = []
            total_limit = min(_MAX_WALLPAPER_UPLOAD_TOTAL_BYTES, maximum * 5)
            total_bytes = 0
            for item in incoming:
                filename = clean_text(getattr(item, "filename", ""), "wallpaper")
                # Never buffer an unbounded multipart request before validation.
                remaining = total_limit - total_bytes
                data = await self._read_uploaded_file(item, min(maximum + 1, remaining + 1))
                total_bytes += len(data)
                if len(data) > maximum:
                    raise WallpaperDashboardError("单张图片超过当前壁纸库允许大小。")
                if total_bytes > total_limit:
                    raise WallpaperDashboardError("本次上传总大小超过安全限制。")
                uploads.append(WallpaperUpload(filename, data))
            return library_id, uploads

        payload = await self._json_body()
        library_id = clean_text(payload.get("library_id", ""))
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raw_files = [payload] if payload.get("data_url") else []
        if not raw_files:
            raise WallpaperDashboardError("请至少选择一张图片。")
        if len(raw_files) > 24:
            raise WallpaperDashboardError("一次最多上传 24 张图片。")
        uploads = []
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise WallpaperDashboardError("上传图片参数无效。")
            uploads.append(
                WallpaperUpload(
                    clean_text(raw.get("filename", ""), "wallpaper"),
                    self._decode_data_url(raw.get("data_url"), maximum),
                )
            )
        return library_id, uploads

    @staticmethod
    async def _request_part(name: str) -> Any:
        """Read Quart and modern AstrBot request properties through one path."""

        value = getattr(request, name, None)
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await value
        return value

    @staticmethod
    async def _read_uploaded_file(item: Any, limit: int) -> bytes:
        reader = getattr(item, "read", None)
        if not callable(reader):
            stream = getattr(item, "stream", None)
            reader = getattr(stream, "read", None)
        if not callable(reader):
            raise WallpaperDashboardError("上传图片内容无效。")
        data = reader(limit)
        if inspect.isawaitable(data):
            data = await data
        return bytes(data or b"")

    def _coerce_wallpaper_library_entry(
        self,
        incoming: Any,
        library_id: str | None,
    ) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise DashboardValidationError("壁纸库配置必须是对象。")
        wallpaper_schema = self._schema.get("wallpaper", {})
        libraries_schema = self._schema_items(wallpaper_schema).get("libraries", {})
        template = self._schema_templates(libraries_schema).get("library", {})
        fields = self._schema_items(template)
        if not fields:
            raise DashboardValidationError("找不到壁纸库配置模板。")
        current: dict[str, Any] = {}
        if library_id is not None:
            current = self.wallpaper_dashboard.get_library(library_id).row
        entry: dict[str, Any] = {"__template_key": "library"}
        path_prefix = ("wallpaper", "libraries", library_id or "new")
        for key, schema_entry in fields.items():
            value = incoming.get(key, current.get(key, self._default_value(schema_entry)))
            entry[key] = self._coerce_value(
                schema_entry,
                value,
                current.get(key),
                (*path_prefix, key),
            )
        if not clean_text(entry.get("name")):
            raise DashboardValidationError("壁纸库名称不能为空。")
        return entry

    def _config_mapping(self) -> dict[str, Any]:
        config = self.plugin.config
        if isinstance(config, dict):
            return config
        raise RuntimeError("插件配置对象不可写。")

    def _ensure_config_section(self, key: str) -> dict[str, Any]:
        config = self._config_mapping()
        section = config.get(key)
        if not isinstance(section, dict):
            section = {}
            config[key] = section
        return section

    def _save_plugin_config(self) -> None:
        saver = getattr(self.plugin.config, "save_config", None)
        if callable(saver):
            saver()

    def _public_config(self) -> tuple[dict[str, Any], dict[str, dict[str, bool]], dict[str, dict[str, Any]]]:
        config = self._config_mapping()
        secret_state: dict[str, dict[str, bool]] = {}
        file_state: dict[str, dict[str, Any]] = {}
        output: dict[str, Any] = {}
        for name, schema_entry in self._schema.items():
            output[name] = self._public_value(
                schema_entry,
                config.get(name),
                (name,),
                secret_state,
                file_state,
            )
        return output, secret_state, file_state

    def _public_value(
        self,
        schema_entry: dict[str, Any],
        current: Any,
        path: tuple[str, ...],
        secret_state: dict[str, dict[str, bool]],
        file_state: dict[str, dict[str, Any]],
    ) -> Any:
        field_type = str(schema_entry.get("type", "string"))
        path_key = ".".join(path)
        if field_type == "file":
            raw_path = extract_file_config_value(current)
            name = Path(raw_path).name if raw_path else ""
            file_state[path_key] = {"configured": bool(raw_path), "name": name[:160]}
            return []
        if self._is_secret_field(path[-1], schema_entry):
            secret_state[path_key] = {"configured": self._has_value(current)}
            return ""
        if field_type == "object":
            source = current if isinstance(current, dict) else {}
            return {
                key: self._public_value(
                    child,
                    source.get(key),
                    (*path, key),
                    secret_state,
                    file_state,
                )
                for key, child in self._schema_items(schema_entry).items()
            }
        if field_type == "template_list":
            templates = self._schema_templates(schema_entry)
            source = current if isinstance(current, list) else self._default_value(schema_entry)
            public_rows: list[dict[str, Any]] = []
            if not isinstance(source, list):
                source = []
            for index, raw_row in enumerate(source[:_MAX_TEMPLATE_ITEMS]):
                if not isinstance(raw_row, dict):
                    continue
                template_key = clean_text(raw_row.get("__template_key", ""))
                if template_key not in templates:
                    continue
                row: dict[str, Any] = {"__template_key": template_key}
                for key, child in self._schema_items(templates[template_key]).items():
                    row[key] = self._public_value(
                        child,
                        raw_row.get(key),
                        (*path, str(index), key),
                        secret_state,
                        file_state,
                    )
                public_rows.append(row)
            return public_rows
        value = current if current is not None else self._default_value(schema_entry)
        return self._json_safe(value)

    def _file_state(self) -> dict[str, dict[str, Any]]:
        _config, _secret_state, file_state = self._public_config()
        return file_state

    def _module_summaries(self, public_config: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for module_name, schema_entry in self._schema.items():
            value = public_config.get(module_name, {})
            enabled = True
            if isinstance(value, dict) and "enabled" in self._schema_items(schema_entry):
                enabled = read_bool(value.get("enabled"), True)
            description = clean_text(schema_entry.get("description", module_name))
            items.append(
                {
                    "key": module_name,
                    "title": description or module_name,
                    "enabled": enabled,
                    "field_count": self._field_count(schema_entry),
                    "llm_tools": self._module_llm_tools(module_name),
                    "commands_enabled": bool(
                        isinstance(value, dict) and value.get("commands_enabled", False)
                    ),
                }
            )
        return items

    def _runtime_snapshot(
        self,
        file_state: dict[str, dict[str, Any]],
        *,
        llm_tools: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        config = self._config_mapping()
        bilibili_credentials = getattr(getattr(self.plugin, "bilibili", None), "credentials", None)
        has_qr_credentials = False
        try:
            has_qr_credentials = bool(
                callable(getattr(bilibili_credentials, "has_credentials", None))
                and bilibili_credentials.has_credentials()
            )
        except Exception:  # noqa: BLE001 - dashboard must not fail on an unreadable credential file
            pass
        has_manual_cookie = self._has_value(cfg(config, "bilibili_video", "cookie", ""))
        cookies_file = file_state.get("bilibili_video.cookies_file", {}).get("configured", False)
        qr_service = getattr(self.plugin, "bilibili_qr_login", None)
        qr_active = bool(
            callable(getattr(qr_service, "is_active", None)) and qr_service.is_active()
        )
        history_repository = getattr(getattr(self.plugin, "chat_history", None), "repository", None)
        history_path = getattr(history_repository, "path", None)
        try:
            history_available = bool(history_path) and Path(history_path).is_file()
        except (OSError, TypeError, ValueError):
            history_available = False
        wallpaper_libraries = cfg(config, "wallpaper", "libraries", [])
        auto_change = cfg(config, "qq_avatar", "auto_change", {})
        auto_change = auto_change if isinstance(auto_change, dict) else {}
        avatar_rotation_enabled = read_bool(auto_change.get("enabled", False), False)
        avatar_rotation = getattr(self.plugin, "avatar_rotation", None)
        avatar_job_id = clean_text(getattr(avatar_rotation, "_cron_job_id", ""))
        active_tools = llm_tools if llm_tools is not None else self._enabled_llm_tools()
        registered_tools = getattr(self.plugin, "_registered_llm_tools", None)
        registered_count = (
            len(registered_tools)
            if isinstance(registered_tools, (list, tuple, set))
            else 0
        )
        return [
            {
                "key": "llm_tools",
                "label": "LLM 工具运行状态",
                "state": "ready" if active_tools else "disabled",
                "value": (
                    f"当前可调用 {len(active_tools)} 项"
                    + (f"，已注册 {registered_count} 项" if registered_count else "")
                ),
                "detail": "这里按当前已注册工具实例的 active 状态显示；保存相关模块配置后会立即同步。",
            },
            {
                "key": "bilibili_credentials",
                "label": "B 站登录凭据",
                "state": "ready" if (has_qr_credentials or has_manual_cookie or cookies_file) else "empty",
                "value": "已配置" if (has_qr_credentials or has_manual_cookie or cookies_file) else "未配置",
                "detail": "扫码凭据、Cookie 文本和 cookies.txt 只显示状态，不会回传内容。",
            },
            {
                "key": "bilibili_qr",
                "label": "B 站扫码任务",
                "state": "active" if qr_active else "idle",
                "value": "正在等待扫码" if qr_active else "当前没有进行中的扫码",
                "detail": "扫码二维码仍需从 QQ 管理员命令发起。",
            },
            {
                "key": "twitter_source",
                "label": "X / Twitter 数据源",
                "state": "ready" if read_bool(cfg(config, "twitter", "enabled", False), False) else "disabled",
                "value": clean_text(cfg(config, "twitter", "data_source", "自动"), "自动"),
                "detail": "仅显示当前选择的数据源，不会发起网络探测。",
            },
            {
                "key": "chat_history",
                "label": "群聊历史库",
                "state": "ready" if history_available else "empty",
                "value": "本地历史库可用" if history_available else "尚未建立本地历史库",
                "detail": "聊天原文仍只保存在插件本地存储，控制台不会展示正文。",
            },
            {
                "key": "wallpaper_libraries",
                "label": "壁纸图库",
                "state": "ready" if isinstance(wallpaper_libraries, list) and wallpaper_libraries else "empty",
                "value": f"已配置 {len(wallpaper_libraries) if isinstance(wallpaper_libraries, list) else 0} 个图库",
                "detail": "图库目录和递归扫描规则可在模块配置页调整。",
            },
            {
                "key": "avatar_rotation",
                "label": "自动换头像",
                "state": (
                    "ready"
                    if avatar_rotation_enabled and avatar_job_id
                    else "warning"
                    if avatar_rotation_enabled
                    else "disabled"
                ),
                "value": (
                    "定时任务已注册"
                    if avatar_rotation_enabled and avatar_job_id
                    else "已启用，等待插件初始化注册"
                    if avatar_rotation_enabled
                    else "未启用"
                ),
                "detail": (
                    f"任务编号：{avatar_job_id}" if avatar_job_id else "更换时间和头像池目录由 QQ 头像模块管理。"
                ),
            },
        ]

    def _enabled_llm_tools(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        registered = getattr(self.plugin, "_registered_llm_tools", None)
        has_registered_tools = isinstance(registered, (list, tuple, set))
        registered_by_name = {
            clean_text(getattr(tool, "name", "")): tool
            for tool in (registered if has_registered_tools else [])
            if clean_text(getattr(tool, "name", ""))
        }
        config = self._config_mapping()
        for module, names in self._module_tool_map().items():
            for name in names:
                registered_tool = registered_by_name.get(name)
                if registered_tool is not None:
                    active = bool(getattr(registered_tool, "active", False))
                elif has_registered_tools:
                    active = False
                else:
                    active = read_bool(cfg(config, module, "enabled", True), True) and read_bool(
                        cfg(config, module, "llm_tool_enabled", True),
                        True,
                    )
                if active:
                    result.append({"module": module, "name": name})
        return result

    def _refresh_registered_llm_tools(self, changed_modules: list[str]) -> list[str]:
        """Synchronize active flags without allowing Dashboard save to fail."""

        tool_modules = set(self._module_tool_map())
        if "general" not in changed_modules and not tool_modules.intersection(changed_modules):
            return []
        refresher = getattr(self.plugin, "refresh_llm_tool_states", None)
        if not callable(refresher):
            return []
        try:
            affected = None if "general" in changed_modules else changed_modules
            result = refresher(affected)
        except Exception as exc:  # noqa: BLE001 - config persistence is more important than a UI refresh
            logger.warning("[HelperTools/WebUI] LLM tool state refresh failed: %r", exc)
            return []
        if not isinstance(result, (list, tuple, set)):
            return []
        return [clean_text(name) for name in result if clean_text(name)]

    @staticmethod
    def _module_tool_map() -> dict[str, tuple[str, ...]]:
        return {
            "qq_avatar": ("get_qq_avatar",),
            "qq_member": ("get_qq_group_member_info",),
            "qq_profile": ("get_qq_profile",),
            "bot_profile": ("set_bot_qq_profile",),
            "voice": ("send_random_voice",),
            "payqr": ("send_payment_qr",),
            "anime1": ("get_anime1_updates", "get_anime1_watch_url"),
            "steam": ("search_steam_game",),
            "bilibili_video": ("understand_bilibili_video",),
            "chat_history": ("search_current_group_chat_history",),
            "web_browser": ("browse_webpage",),
            "twitter": (
                "find_x_account",
                "get_x_post",
                "get_x_recent_posts",
                "search_x_posts",
            ),
            "poke": ("poke_qq_user",),
        }

    def _module_llm_tools(self, module: str) -> list[str]:
        return list(self._module_tool_map().get(module, ()))

    def _storage_snapshot(self) -> dict[str, Any]:
        root = self.data_dir
        if not root.exists():
            return {
                "available": False,
                "total_files": 0,
                "total_bytes": 0,
                "latest_modified_at": "",
                "truncated": False,
                "buckets": [],
            }
        buckets: dict[str, dict[str, Any]] = {}
        total_files = 0
        total_bytes = 0
        latest_mtime = 0.0
        scanned = 0
        truncated = False
        max_files = 30_000
        try:
            for current_dir, directory_names, file_names in os.walk(root, followlinks=False):
                safe_directories: list[str] = []
                for directory in directory_names:
                    candidate = Path(current_dir) / directory
                    try:
                        if not candidate.is_symlink():
                            safe_directories.append(directory)
                    except OSError:
                        continue
                directory_names[:] = safe_directories
                for file_name in file_names:
                    scanned += 1
                    if scanned > max_files:
                        truncated = True
                        break
                    path = Path(current_dir) / file_name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                        relative = path.relative_to(root)
                    except (OSError, ValueError):
                        continue
                    bucket_name = relative.parts[0] if len(relative.parts) > 1 else "根目录"
                    bucket = buckets.setdefault(
                        bucket_name,
                        {"name": bucket_name, "files": 0, "bytes": 0, "latest_modified_at": ""},
                    )
                    bucket["files"] += 1
                    bucket["bytes"] += stat.st_size
                    if stat.st_mtime > latest_mtime:
                        latest_mtime = stat.st_mtime
                    previous = bucket.get("_mtime", 0.0)
                    if stat.st_mtime > previous:
                        bucket["_mtime"] = stat.st_mtime
                    total_files += 1
                    total_bytes += stat.st_size
                if truncated:
                    break
        except OSError as exc:
            logger.warning("[HelperTools/WebUI] storage scan stopped: %r", exc)
        for bucket in buckets.values():
            mtime = float(bucket.pop("_mtime", 0.0))
            bucket["latest_modified_at"] = self._format_timestamp(mtime)
        return {
            "available": True,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "latest_modified_at": self._format_timestamp(latest_mtime),
            "truncated": truncated,
            "buckets": sorted(buckets.values(), key=lambda item: (-item["bytes"], item["name"])),
        }

    def _config_modified_at(self) -> str:
        path = getattr(self.plugin.config, "config_path", "")
        if not path:
            return ""
        try:
            return self._format_timestamp(Path(path).stat().st_mtime)
        except OSError:
            return ""

    def _theme_value(self) -> str:
        value = clean_text(cfg(self._config_mapping(), "webui", "dashboard_theme", "dark")).lower()
        return value if value in {"dark", "light"} else "dark"

    def _coerce_value(
        self,
        schema_entry: dict[str, Any],
        incoming: Any,
        current: Any,
        path: tuple[str, ...],
    ) -> Any:
        field_type = str(schema_entry.get("type", "string"))
        if field_type == "file":
            return deepcopy(current if current is not None else self._default_value(schema_entry))
        if self._is_secret_field(path[-1], schema_entry):
            return self._coerce_secret(schema_entry, incoming, current, path)
        if field_type == "object":
            if not isinstance(incoming, dict):
                raise DashboardValidationError(f"{'.'.join(path)} 必须是对象。")
            existing = current if isinstance(current, dict) else {}
            output: dict[str, Any] = {}
            for key, child in self._schema_items(schema_entry).items():
                output[key] = self._coerce_value(
                    child,
                    incoming.get(key, existing.get(key)),
                    existing.get(key),
                    (*path, key),
                )
            return output
        if field_type == "template_list":
            if not isinstance(incoming, list):
                raise DashboardValidationError(f"{'.'.join(path)} 必须是列表。")
            if len(incoming) > _MAX_TEMPLATE_ITEMS:
                raise DashboardValidationError(f"{'.'.join(path)} 最多保留 {_MAX_TEMPLATE_ITEMS} 项。")
            templates = self._schema_templates(schema_entry)
            if not templates:
                return []
            output_rows: list[dict[str, Any]] = []
            for index, raw_row in enumerate(incoming):
                if not isinstance(raw_row, dict):
                    raise DashboardValidationError(f"{'.'.join(path)} 第 {index + 1} 项必须是对象。")
                template_key = clean_text(raw_row.get("__template_key", ""))
                if template_key not in templates:
                    raise DashboardValidationError(f"{'.'.join(path)} 第 {index + 1} 项模板无效。")
                template = templates[template_key]
                row: dict[str, Any] = {"__template_key": template_key}
                for key, child in self._schema_items(template).items():
                    row[key] = self._coerce_value(
                        child,
                        raw_row.get(key),
                        None,
                        (*path, str(index), key),
                    )
                output_rows.append(row)
            return output_rows
        if field_type == "list":
            if not isinstance(incoming, list):
                raise DashboardValidationError(f"{'.'.join(path)} 必须是列表。")
            if len(incoming) > _MAX_LIST_ITEMS:
                raise DashboardValidationError(f"{'.'.join(path)} 最多保留 {_MAX_LIST_ITEMS} 项。")
            return self._coerce_list(schema_entry, incoming, path)
        if field_type == "bool":
            return self._as_bool(incoming)
        if field_type == "int":
            return self._bounded_number(schema_entry, incoming, path, integer=True)
        if field_type == "float":
            return self._bounded_number(schema_entry, incoming, path, integer=False)
        if field_type in {"string", "text"}:
            value = "" if incoming is None else str(incoming)
            if len(value) > _MAX_TEXT_LENGTH:
                raise DashboardValidationError(f"{'.'.join(path)} 内容过长。")
            if field_type == "string":
                value = value.strip()
            options = schema_entry.get("options")
            if isinstance(options, list) and options and value not in options:
                raise DashboardValidationError(f"{'.'.join(path)} 选项无效。")
            return value
        raise DashboardValidationError(f"{'.'.join(path)} 的配置类型不受控制台支持。")

    def _coerce_secret(
        self,
        schema_entry: dict[str, Any],
        incoming: Any,
        current: Any,
        path: tuple[str, ...],
    ) -> Any:
        if not isinstance(incoming, dict):
            return deepcopy(current if current is not None else self._default_value(schema_entry))
        action = clean_text(incoming.get(_SECRET_ACTION_KEY, _SECRET_KEEP)).lower()
        if action == _SECRET_KEEP:
            return deepcopy(current if current is not None else self._default_value(schema_entry))
        if action == _SECRET_CLEAR:
            return self._default_value(schema_entry)
        if action != _SECRET_REPLACE:
            raise DashboardValidationError(f"{'.'.join(path)} 的密钥操作无效。")
        value = incoming.get("value", "")
        if not isinstance(value, str):
            raise DashboardValidationError(f"{'.'.join(path)} 必须是文本。")
        if len(value) > _MAX_TEXT_LENGTH:
            raise DashboardValidationError(f"{'.'.join(path)} 内容过长。")
        return value.strip()

    def _coerce_list(
        self,
        schema_entry: dict[str, Any],
        incoming: list[Any],
        path: tuple[str, ...],
    ) -> list[Any]:
        default = self._default_value(schema_entry)
        prototype = default[0] if isinstance(default, list) and default else ""
        result: list[Any] = []
        for index, value in enumerate(incoming):
            if isinstance(prototype, bool):
                result.append(self._as_bool(value))
            elif isinstance(prototype, int) and not isinstance(prototype, bool):
                result.append(self._bounded_plain_number(value, (*path, str(index)), integer=True))
            elif isinstance(prototype, float):
                result.append(self._bounded_plain_number(value, (*path, str(index)), integer=False))
            elif isinstance(prototype, (dict, list)):
                if not isinstance(value, type(prototype)):
                    raise DashboardValidationError(f"{'.'.join(path)} 第 {index + 1} 项类型无效。")
                result.append(self._json_safe(value))
            else:
                text = clean_text(value)
                if text:
                    result.append(text[:_MAX_TEXT_LENGTH])
        return result

    def _bounded_number(
        self,
        schema_entry: dict[str, Any],
        value: Any,
        path: tuple[str, ...],
        *,
        integer: bool,
    ) -> int | float:
        parsed = self._bounded_plain_number(value, path, integer=integer)
        minimum = schema_entry.get("min")
        maximum = schema_entry.get("max")
        if isinstance(minimum, (int, float)):
            parsed = max(parsed, int(minimum) if integer else float(minimum))
        if isinstance(maximum, (int, float)):
            parsed = min(parsed, int(maximum) if integer else float(maximum))
        return parsed

    @staticmethod
    def _bounded_plain_number(value: Any, path: tuple[str, ...], *, integer: bool) -> int | float:
        try:
            parsed: int | float = int(value) if integer else float(value)
        except (TypeError, ValueError) as exc:
            kind = "整数" if integer else "数字"
            raise DashboardValidationError(f"{'.'.join(path)} 必须是{kind}。") from exc
        if (
            isinstance(parsed, float)
            and not math.isfinite(parsed)
        ) or not (-_MAX_ABSOLUTE_NUMBER <= parsed <= _MAX_ABSOLUTE_NUMBER):
            raise DashboardValidationError(f"{'.'.join(path)} 超出允许范围。")
        return parsed

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        return bool(value)

    def _schema_entry_for_path(self, raw_path: str) -> dict[str, Any]:
        parts = self._parse_schema_path(raw_path)
        entry: dict[str, Any] | None = None
        schema_map = self._schema
        for index, part in enumerate(parts):
            entry = schema_map.get(part)
            if entry is None:
                raise DashboardValidationError("找不到对应的配置项。")
            if index == len(parts) - 1:
                return entry
            if entry.get("type") != "object":
                raise DashboardValidationError("配置路径无效。")
            schema_map = self._schema_items(entry)
        raise DashboardValidationError("配置路径无效。")

    def _parse_schema_path(self, raw_path: str) -> tuple[str, ...]:
        parts = tuple(part for part in raw_path.split(".") if part)
        if not parts or len(parts) > 8 or any(not _SAFE_PATH_PART.fullmatch(part) for part in parts):
            raise DashboardValidationError("配置路径无效。")
        return parts

    def _value_at_config_path(self, raw_path: str) -> Any:
        parts = self._parse_schema_path(raw_path)
        current: Any = self._config_mapping()
        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return deepcopy(current)

    def _set_config_path(self, raw_path: str, value: Any) -> None:
        parts = self._parse_schema_path(raw_path)
        current = self._config_mapping()
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value

    @staticmethod
    def _decode_data_url(value: Any, maximum_bytes: int = _MAX_UPLOAD_BYTES) -> bytes:
        if not isinstance(value, str) or not value.startswith("data:"):
            raise DashboardValidationError("上传内容格式不正确。")
        header, separator, encoded = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise DashboardValidationError("上传内容必须是 Base64 文件。")
        maximum_bytes = min(max(int(maximum_bytes), 1), _MAX_DASHBOARD_DATA_URL_BYTES)
        if len(encoded) > ((maximum_bytes + 2) // 3) * 4 + 8:
            raise DashboardValidationError("上传文件超过允许大小。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise DashboardValidationError("上传文件无法解码。") from exc
        if not data:
            raise DashboardValidationError("上传文件为空。")
        if len(data) > maximum_bytes:
            raise DashboardValidationError("上传文件超过允许大小。")
        return data

    def _write_uploaded_file(self, raw_path: str, filename: str, data: bytes) -> Path:
        safe_path = "_".join(self._parse_schema_path(raw_path))
        extension = Path(filename).suffix.lower()
        stem = _SAFE_FILE_STEM.sub("_", Path(filename).stem).strip("._")[:80] or "upload"
        destination_dir = (self.data_dir / _UPLOAD_DIRECTORY_NAME / safe_path).resolve()
        upload_root = (self.data_dir / _UPLOAD_DIRECTORY_NAME).resolve()
        destination_dir.relative_to(upload_root)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{uuid.uuid4().hex}_{stem}{extension}"
        temporary = destination.with_suffix(f"{extension}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return destination

    def _delete_owned_uploaded_file(self, value: Any) -> None:
        raw_path = extract_file_config_value(value)
        if not raw_path:
            return
        try:
            candidate = Path(raw_path).resolve()
            upload_root = (self.data_dir / _UPLOAD_DIRECTORY_NAME).resolve()
            candidate.relative_to(upload_root)
        except (OSError, ValueError):
            return
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _schema_items(schema_entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = schema_entry.get("items")
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict)}

    @staticmethod
    def _schema_templates(schema_entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = schema_entry.get("templates")
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict)}

    @classmethod
    def _default_value(cls, schema_entry: dict[str, Any]) -> Any:
        if "default" in schema_entry:
            return deepcopy(schema_entry["default"])
        field_type = str(schema_entry.get("type", "string"))
        if field_type == "object":
            return {key: cls._default_value(value) for key, value in cls._schema_items(schema_entry).items()}
        if field_type in {"list", "template_list", "file"}:
            return []
        if field_type == "bool":
            return False
        if field_type == "int":
            return 0
        if field_type == "float":
            return 0.0
        return ""

    @staticmethod
    def _is_secret_field(key: str, schema_entry: dict[str, Any]) -> bool:
        if str(schema_entry.get("type", "")) == "file":
            return False
        lowered = key.lower().replace("-", "_")
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            return True
        description = clean_text(schema_entry.get("description", "")).lower()
        return "api key" in description or "密钥" in description or "凭据" in description

    @staticmethod
    def _has_value(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return value is not None

    @classmethod
    def _field_count(cls, schema_entry: dict[str, Any]) -> int:
        field_type = str(schema_entry.get("type", ""))
        if field_type == "object":
            return sum(cls._field_count(item) for item in cls._schema_items(schema_entry).values())
        if field_type == "template_list":
            return sum(
                cls._field_count(item)
                for template in cls._schema_templates(schema_entry).values()
                for item in cls._schema_items(template).values()
            )
        return 1

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): HelperToolsDashboard._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [HelperToolsDashboard._json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        if timestamp <= 0:
            return ""
        try:
            return datetime.fromtimestamp(timestamp, UTC).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    @staticmethod
    def _reload_recommended(changed_modules: list[str]) -> bool:
        needs_restart = {
            "bilibili_video",
            "web_browser",
            "twitter",
            "qq_avatar",
            "anime1",
            "poke",
            "anti_revoke",
        }
        return any(module in needs_restart for module in changed_modules)

    @staticmethod
    def _int_query(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(request.args.get(name, default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, minimum), maximum)

    @staticmethod
    def _error(message: str, status_code: int):
        return jsonify({"success": False, "message": message}), status_code
