from __future__ import annotations

import base64
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from PIL import Image
from quart import Quart
from werkzeug.datastructures import FileStorage

from astrbot_plugin_helper_tools.webui_activity import WebUiActivityLog
from astrbot_plugin_helper_tools.webui_service import (
    DashboardValidationError,
    HelperToolsDashboard,
)


class _Config(dict[str, Any]):
    def __init__(self, path: Path, value: dict[str, Any] | None = None) -> None:
        super().__init__(value or {})
        self.config_path = path
        self.save_calls = 0

    def save_config(self) -> None:
        self.save_calls += 1
        self.config_path.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8")


class _Context:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any, list[str], str]] = []

    def register_web_api(
        self,
        path: str,
        handler: Any,
        methods: list[str],
        description: str,
    ) -> None:
        self.routes.append((path, handler, methods, description))


class _Plugin:
    def __init__(self, data_dir: Path, config: _Config) -> None:
        self.data_dir = data_dir
        self.config = config
        self.context = _Context()
        self._registered_llm_tools = [
            SimpleNamespace(name="get_qq_avatar", active=True),
            SimpleNamespace(name="get_qq_group_member_info", active=True),
            SimpleNamespace(name="browse_webpage", active=False),
        ]
        self.refresh_calls: list[list[str] | None] = []
        self.chat_history = SimpleNamespace(
            repository=SimpleNamespace(path=str(data_dir / "chat_history.sqlite3"))
        )
        self.avatar_rotation = SimpleNamespace(_cron_job_id="avatar-job")
        self.bilibili = SimpleNamespace(credentials=None)
        self.bilibili_qr_login = SimpleNamespace(is_active=lambda: False)

    def refresh_llm_tool_states(self, changed_modules: list[str] | None) -> list[str]:
        self.refresh_calls.append(changed_modules)
        changed = set(changed_modules or ())
        changed_all = changed_modules is None
        updated: list[str] = []
        if changed_all or "qq_avatar" in changed:
            enabled = bool(self.config.get("qq_avatar", {}).get("enabled", True))
            tool = self._registered_llm_tools[0]
            if tool.active != enabled:
                tool.active = enabled
                updated.append(tool.name)
        return updated


class DashboardRegistrationTests(unittest.TestCase):
    def test_registers_all_dashboard_endpoints(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = _Plugin(root, _Config(root / "config.json"))
            dashboard = HelperToolsDashboard(plugin, version="0.10.0")

            dashboard.register()

            self.assertEqual(len(plugin.context.routes), 21)
            self.assertEqual(
                {
                    item[0].removeprefix("/astrbot_plugin_helper_tools/")
                    for item in plugin.context.routes
                },
                {
                    "state",
                    "save_config",
                    "save_theme",
                    "activities",
                    "clear_activities",
                    "storage",
                    "upload_file",
                    "clear_file",
                    "wallpaper_libraries",
                    "wallpaper_images",
                    "wallpaper_thumbnail",
                    "wallpaper_thumbnail_data",
                    "wallpaper_file",
                    "wallpaper_preview_data",
                    "wallpaper_download",
                    "wallpaper_upload",
                    "wallpaper_upload_file/<library_id>",
                    "wallpaper_save_library",
                    "wallpaper_delete_library",
                    "wallpaper_delete_image",
                    "wallpaper_rename_image",
                },
            )


class DashboardApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "plugin-data"
        self.data_dir.mkdir()
        self.config = _Config(
            self.data_dir / "config.json",
            {
                "bilibili_video": {
                    "cookie": "SESSDATA=top-secret-cookie",
                    "gemini": {"api_key": "gemini-secret-key"},
                },
                "twitter": {
                    "ai_review": {"api_key": "review-secret-key"},
                },
                "qq_avatar": {"enabled": True},
                "wallpaper": {
                    "libraries": [
                        {
                            "__template_key": "library",
                            "name": "测试图库",
                            "path": "wallpapers/test",
                            "commands": ["测试壁纸"],
                            "caption": "随机给你抽一张 {library}。",
                            "send_mode": "同一条消息",
                            "recursive": True,
                        }
                    ]
                },
            },
        )
        self.plugin = _Plugin(self.data_dir, self.config)
        self.dashboard = HelperToolsDashboard(self.plugin, version="0.10.0")
        self.app = Quart(__name__)
        self.app.add_url_rule("/state", view_func=self.dashboard.get_state, methods=["GET"])
        self.app.add_url_rule("/save", view_func=self.dashboard.save_config, methods=["POST"])
        self.app.add_url_rule("/activities", view_func=self.dashboard.get_activities, methods=["GET"])
        self.app.add_url_rule(
            "/clear-activities",
            view_func=self.dashboard.clear_activities,
            methods=["POST"],
        )
        self.app.add_url_rule("/upload", view_func=self.dashboard.upload_file, methods=["POST"])
        self.app.add_url_rule(
            "/wallpaper-libraries",
            view_func=self.dashboard.get_wallpaper_libraries,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-images",
            view_func=self.dashboard.get_wallpaper_images,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-thumbnail",
            view_func=self.dashboard.get_wallpaper_thumbnail,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-thumbnail-data",
            view_func=self.dashboard.get_wallpaper_thumbnail_data,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-file",
            view_func=self.dashboard.get_wallpaper_file,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-preview-data",
            view_func=self.dashboard.get_wallpaper_preview_data,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-download",
            view_func=self.dashboard.download_wallpaper_file,
            methods=["GET"],
        )
        self.app.add_url_rule(
            "/wallpaper-upload",
            view_func=self.dashboard.upload_wallpaper_images,
            methods=["POST"],
        )
        self.app.add_url_rule(
            "/wallpaper-upload-file/<library_id>",
            view_func=self.dashboard.upload_wallpaper_file,
            methods=["POST"],
        )
        self.app.add_url_rule(
            "/wallpaper-save-library",
            view_func=self.dashboard.save_wallpaper_library,
            methods=["POST"],
        )
        self.app.add_url_rule(
            "/wallpaper-delete-library",
            view_func=self.dashboard.delete_wallpaper_library,
            methods=["POST"],
        )
        self.app.add_url_rule(
            "/wallpaper-delete-image",
            view_func=self.dashboard.delete_wallpaper_image,
            methods=["POST"],
        )
        self.app.add_url_rule(
            "/wallpaper-rename-image",
            view_func=self.dashboard.rename_wallpaper_image,
            methods=["POST"],
        )
        self.client = self.app.test_client()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _state(self) -> dict[str, Any]:
        response = await self.client.get("/state")
        self.assertEqual(response.status_code, 200)
        return await response.get_json()

    async def _save(self, config: dict[str, Any]):
        return await self.client.post("/save", json={"config": config})

    def _png_bytes(self, *, size: tuple[int, int] = (48, 32), color: str = "#4bf0df") -> bytes:
        output = BytesIO()
        Image.new("RGB", size, color).save(output, format="PNG")
        return output.getvalue()

    def _write_wallpaper(self, name: str = "one.png") -> Path:
        directory = self.data_dir / "wallpapers" / "test"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(self._png_bytes())
        return path

    async def test_state_redacts_secrets_and_uses_registered_tool_state(self) -> None:
        state = await self._state()
        rendered = json.dumps(state, ensure_ascii=False)

        self.assertNotIn("top-secret-cookie", rendered)
        self.assertNotIn("gemini-secret-key", rendered)
        self.assertNotIn("review-secret-key", rendered)
        self.assertEqual(state["config"]["bilibili_video"]["cookie"], "")
        self.assertTrue(state["secret_state"]["bilibili_video.cookie"]["configured"])
        self.assertTrue(
            state["secret_state"]["bilibili_video.gemini.api_key"]["configured"]
        )
        self.assertIn(
            "get_qq_avatar",
            {item["name"] for item in state["llm_tools"]},
        )
        self.assertNotIn(
            "browse_webpage",
            {item["name"] for item in state["llm_tools"]},
        )

    async def test_secret_keep_and_clear_are_safe(self) -> None:
        state = await self._state()
        config = state["config"]

        response = await self._save(config)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.config["bilibili_video"]["cookie"],
            "SESSDATA=top-secret-cookie",
        )

        config["bilibili_video"]["cookie"] = {
            "__helper_tools_secret_action": "clear"
        }
        response = await self._save(config)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config["bilibili_video"]["cookie"], "")

    async def test_save_clamps_numbers_validates_options_and_refreshes_tools(self) -> None:
        state = await self._state()
        config = state["config"]
        config["perception"]["group_name_cache_seconds"] = "999999"
        config["qq_avatar"]["enabled"] = False

        response = await self._save(config)
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config["perception"]["group_name_cache_seconds"], 86400)
        refreshed_modules = self.plugin.refresh_calls[-1]
        self.assertTrue(
            refreshed_modules is None or "perception" in refreshed_modules
        )
        self.assertTrue(
            refreshed_modules is None or "qq_avatar" in refreshed_modules
        )
        self.assertEqual(body["tools_updated"], ["get_qq_avatar"])

        config = (await self._state())["config"]
        config["webui"]["dashboard_theme"] = "not-a-theme"
        response = await self._save(config)
        self.assertEqual(response.status_code, 400)
        body = await response.get_json()
        self.assertIn("选项无效", body["message"])

    async def test_template_list_rejects_unknown_template(self) -> None:
        state = await self._state()
        config = state["config"]
        config["wallpaper"]["libraries"] = [{"__template_key": "not-real"}]

        response = await self._save(config)
        body = await response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertIn("模板无效", body["message"])

    async def test_upload_is_extension_checked_and_stays_under_plugin_data(self) -> None:
        payload = base64.b64encode(b"test image").decode("ascii")
        invalid = await self.client.post(
            "/upload",
            json={
                "path": "payqr.payment_qr",
                "filename": "not-allowed.txt",
                "data_url": f"data:text/plain;base64,{payload}",
            },
        )
        self.assertEqual(invalid.status_code, 400)

        response = await self.client.post(
            "/upload",
            json={
                "path": "payqr.payment_qr",
                "filename": "payment.png",
                "data_url": f"data:image/png;base64,{payload}",
            },
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        configured_path = Path(self.config["payqr"]["payment_qr"][0]).resolve()
        upload_root = (self.data_dir / "webui_uploads").resolve()
        self.assertTrue(configured_path.is_file())
        self.assertTrue(configured_path.is_relative_to(upload_root))
        self.assertTrue(body["file_state"]["payqr.payment_qr"]["configured"])

    async def test_activity_keeps_session_type_private_and_clear_is_empty(self) -> None:
        event = SimpleNamespace(unified_msg_origin="default:GroupMessage:10001")
        self.dashboard.record_activity("steam", "完成查询", event=event)

        response = await self.client.get("/activities")
        records = (await response.get_json())["records"]
        self.assertEqual(records[0]["session_kind"], "GroupMessage")
        self.assertNotIn("session", records[0])

        response = await self.client.post("/clear-activities", json={})
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/activities")
        self.assertEqual((await response.get_json())["records"], [])

    async def test_wallpaper_library_api_browses_previews_and_blocks_path_escape(self) -> None:
        path = self._write_wallpaper()

        response = await self.client.get("/wallpaper-libraries")
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["libraries"][0]["name"], "测试图库")
        self.assertEqual(body["libraries"][0]["image_count"], 1)

        response = await self.client.get(
            "/wallpaper-images?library_id=0&page=1&page_size=36&sort=newest"
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        image = body["images"][0]
        self.assertEqual(image["relative_path"], "one.png")
        self.assertEqual((image["width"], image["height"]), (48, 32))
        self.assertTrue(image["preview_supported"])

        response = await self.client.get("/wallpaper-thumbnail?library_id=0&path=one.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertGreater(len(await response.get_data()), 0)

        response = await self.client.get("/wallpaper-thumbnail-data?library_id=0&path=one.png")
        thumbnail = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(thumbnail["data_url"].startswith("data:image/jpeg;base64,"))

        response = await self.client.get("/wallpaper-preview-data?library_id=0&path=one.png")
        preview = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(preview["data_url"].startswith("data:image/jpeg;base64,"))

        response = await self.client.get("/wallpaper-file?library_id=0&path=one.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/png")

        response = await self.client.get("/wallpaper-download?library_id=0&path=one.png")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))

        response = await self.client.get("/wallpaper-file?library_id=0&path=../config.json")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(path.is_file())

    async def test_wallpaper_upload_rename_delete_and_registry_sync(self) -> None:
        data = self._png_bytes(color="#ff5ac9")
        payload = base64.b64encode(data).decode("ascii")
        response = await self.client.post(
            "/wallpaper-upload",
            json={
                "library_id": "0",
                "files": [
                    {
                        "filename": "uploaded.png",
                        "data_url": f"data:image/png;base64,{payload}",
                    }
                ],
            },
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(body["saved"]), 1)
        uploaded = self.data_dir / "wallpapers" / "test" / "uploaded.png"
        self.assertTrue(uploaded.is_file())

        service = self.dashboard.wallpaper_dashboard.wallpaper
        service.record_sent_image("message-1", uploaded, "测试图库")
        response = await self.client.post(
            "/wallpaper-rename-image",
            json={"library_id": "0", "path": "uploaded.png", "new_name": "renamed.jpg"},
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["image"]["relative_path"], "renamed.png")
        renamed = self.data_dir / "wallpapers" / "test" / "renamed.png"
        self.assertTrue(renamed.is_file())
        self.assertEqual(service.load_registry()["message-1"]["path"], str(renamed.resolve()))

        response = await self.client.post(
            "/wallpaper-delete-image",
            json={"library_id": "0", "path": "renamed.png", "confirm": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(renamed.exists())
        self.assertNotIn("message-1", service.load_registry())

    async def test_wallpaper_bridge_upload_accepts_one_file(self) -> None:
        data = self._png_bytes(color="#8f6fff")
        response = await self.client.post(
            "/wallpaper-upload-file/0",
            files={
                "file": FileStorage(
                    stream=BytesIO(data),
                    filename="bridge-upload.png",
                    content_type="image/png",
                )
            },
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["saved"][0]["relative_path"], "bridge-upload.png")
        self.assertTrue((self.data_dir / "wallpapers" / "test" / "bridge-upload.png").is_file())

    async def test_wallpaper_file_api_rejects_symbolic_link(self) -> None:
        directory = self.data_dir / "wallpapers" / "test"
        directory.mkdir(parents=True, exist_ok=True)
        outside = self.data_dir / "outside.png"
        outside.write_bytes(self._png_bytes())
        linked = directory / "linked.png"
        try:
            linked.symlink_to(outside)
        except OSError:
            self.skipTest("当前系统不允许测试进程创建符号链接")

        response = await self.client.get("/wallpaper-file?library_id=0&path=linked.png")
        self.assertEqual(response.status_code, 400)

    async def test_wallpaper_library_config_create_edit_and_remove_keeps_files(self) -> None:
        response = await self.client.post(
            "/wallpaper-save-library",
            json={
                "library": {
                    "name": "新图库",
                    "path": "wallpapers/new",
                    "commands": ["新壁纸"],
                    "caption": "图：{filename}",
                    "send_mode": "只发图片",
                    "recursive": False,
                },
                "create_directory": True,
            },
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["library"]["id"], "1")
        self.assertTrue((self.data_dir / "wallpapers" / "new").is_dir())
        self.assertEqual(self.config.save_calls, 1)

        response = await self.client.post(
            "/wallpaper-save-library",
            json={
                "library_id": "1",
                "library": {
                    "name": "改名图库",
                    "path": "wallpapers/new",
                    "commands": ["改名壁纸"],
                    "caption": "图：{filename}",
                    "send_mode": "同一条消息",
                    "recursive": True,
                },
                "create_directory": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config["wallpaper"]["libraries"][1]["name"], "改名图库")

        response = await self.client.post(
            "/wallpaper-delete-library",
            json={"library_id": "1", "confirm": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.config["wallpaper"]["libraries"]), 1)
        self.assertTrue((self.data_dir / "wallpapers" / "new").is_dir())

    async def test_wallpaper_library_file_removal_requires_name_and_removes_registry(self) -> None:
        response = await self.client.post(
            "/wallpaper-save-library",
            json={
                "library": {
                    "name": "待物理删除图库",
                    "path": "wallpapers/purge",
                    "commands": ["删除壁纸"],
                    "caption": "图：{filename}",
                    "send_mode": "只发图片",
                    "recursive": False,
                },
                "create_directory": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        directory = self.data_dir / "wallpapers" / "purge"
        image = directory / "remove.png"
        image.write_bytes(self._png_bytes())
        service = self.dashboard.wallpaper_dashboard.wallpaper
        service.record_sent_image("purge-message", image, "待物理删除图库")

        response = await self.client.post(
            "/wallpaper-delete-library",
            json={
                "library_id": "1",
                "confirm": True,
                "delete_files": True,
                "confirmation_name": "名称不匹配",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(directory.is_dir())

        response = await self.client.post(
            "/wallpaper-delete-library",
            json={
                "library_id": "1",
                "confirm": True,
                "delete_files": True,
                "confirmation_name": "待物理删除图库",
            },
        )
        body = await response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["deleted"]["files_deleted"])
        self.assertFalse(directory.exists())
        self.assertNotIn("purge-message", service.load_registry())


class ActivityPrivacyTests(unittest.TestCase):
    def test_old_session_identifier_is_not_returned_after_setting_is_disabled(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _Config(
                root / "config.json",
                {"webui": {"activity_log_include_session_id": True}},
            )
            activity = WebUiActivityLog(config, root)
            event = SimpleNamespace(unified_msg_origin="default:FriendMessage:20002")
            activity.record("webui", "测试", event=event)
            self.assertIn("session", activity.get_records()[0])

            config["webui"]["activity_log_include_session_id"] = False
            self.assertNotIn("session", activity.get_records()[0])


class DashboardValidationTests(unittest.TestCase):
    def test_invalid_schema_path_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            dashboard = HelperToolsDashboard(_Plugin(root, _Config(root / "config.json")), version="0.10.0")
            with self.assertRaises(DashboardValidationError):
                dashboard._schema_entry_for_path("../../outside")


class ToolStateRefreshTests(unittest.TestCase):
    def test_main_plugin_updates_only_requested_registered_tools(self) -> None:
        from astrbot_plugin_helper_tools.main import HelperToolsPlugin

        plugin = SimpleNamespace(
            config={
                "general": {"enabled": True},
                "qq_avatar": {"enabled": False, "llm_tool_enabled": True},
                "qq_profile": {"enabled": True, "llm_tool_enabled": True},
                "web_browser": {"enabled": False, "llm_tool_enabled": True},
            },
            _registered_llm_tools=[
                SimpleNamespace(name="get_qq_avatar", active=True),
                SimpleNamespace(name="get_qq_profile", active=True),
                SimpleNamespace(name="browse_webpage", active=True),
            ],
        )
        plugin.enabled = lambda: True
        plugin._tool_active = HelperToolsPlugin._tool_active.__get__(plugin)
        plugin._web_browser_tool_active = HelperToolsPlugin._web_browser_tool_active.__get__(
            plugin
        )

        updated = HelperToolsPlugin.refresh_llm_tool_states(plugin, ["qq_avatar"])

        self.assertEqual(updated, ["get_qq_avatar"])
        self.assertFalse(plugin._registered_llm_tools[0].active)
        self.assertTrue(plugin._registered_llm_tools[1].active)
        self.assertTrue(plugin._registered_llm_tools[2].active)

        updated = HelperToolsPlugin.refresh_llm_tool_states(plugin)

        self.assertEqual(updated, ["browse_webpage"])
        self.assertFalse(plugin._registered_llm_tools[2].active)


if __name__ == "__main__":
    unittest.main()
