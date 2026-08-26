from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools import wallpaper_service as wallpaper_module
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.wallpaper_service import ImageRef, WallpaperService

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fea735c8f60000000049454e44ae426082"
)


class _Config(dict[str, Any]):
    def __init__(self, value: dict[str, Any], *, save_error: bool = False) -> None:
        super().__init__(value)
        self.save_calls = 0
        self.save_error = save_error

    def save_config(self) -> None:
        self.save_calls += 1
        if self.save_error:
            raise RuntimeError("config disk failure")


class _Event:
    def __init__(self, chain: list[Any]) -> None:
        self._chain = chain
        self.sent: list[Any] = []

    def get_messages(self) -> list[Any]:
        return self._chain

    @staticmethod
    def is_admin() -> bool:
        return True

    @staticmethod
    def get_group_id() -> str:
        return ""

    @staticmethod
    def get_sender_id() -> str:
        return ""

    async def send(self, chain: Any) -> None:
        self.sent.append(chain)


class FakeActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed（LLOneBot 失败时抛出的异常）。"""

    def __init__(self, retcode: int, message: str) -> None:
        super().__init__(f"retcode={retcode} message={message}")
        self.retcode = retcode
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": message,
        }


LLONEBOT_VERSION_INFO = {
    "status": "ok",
    "retcode": 0,
    "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
}


class LLOneBotBot:
    """LLOneBot：get_image 返回 file/url/file_size/file_name，没有 filename/size。"""

    def __init__(self, *, message_payload: Any = None) -> None:
        self.image_calls: list[dict[str, Any]] = []
        self.msg_calls: list[dict[str, Any]] = []
        self._message_payload = message_payload

    async def get_version_info(self) -> dict[str, Any]:
        return LLONEBOT_VERSION_INFO

    async def get_msg(self, **params: Any) -> dict[str, Any]:
        self.msg_calls.append(dict(params))
        if self._message_payload is None:
            raise FakeActionFailed(1200, "消息不存在或已过期")
        return {"status": "ok", "retcode": 0, "data": self._message_payload}

    async def get_image(self, **params: Any) -> dict[str, Any]:
        self.image_calls.append(dict(params))
        reference = params.get("file") or params.get("file_id")
        if not reference:
            raise FakeActionFailed(1400, "缺少参数 file")
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "file": "/llonebot/cache/nonexistent-original.png",
                "url": "https://example.com/llonebot.png",
                "file_size": len(PNG_BYTES),
                "file_name": "llonebot.png",
            },
        }

    async def get_file(self, **_params: Any) -> dict[str, Any]:
        raise FakeActionFailed(1404, "get_file API 不存在")


class WallpaperAsyncIoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 清空实现探测与变体命中缓存，避免用例之间互相串味。
        reset_compat_caches()

    def tearDown(self) -> None:
        reset_compat_caches()

    def _service(self, root: Path, *, save_error: bool = False) -> WallpaperService:
        library_dir = root / "library"
        library_dir.mkdir(parents=True, exist_ok=True)
        config = _Config(
            {
                "wallpaper": {
                    "libraries": [
                        {"name": "测试图库", "path": str(library_dir), "commands": ["测试图库"]}
                    ]
                }
            },
            save_error=save_error,
        )
        return WallpaperService(config, root / "data", None)

    async def test_image_files_async_matches_sync_scan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            library = service.libraries()[0]
            (library.path / "b.png").write_bytes(PNG_BYTES)
            (library.path / "a.png").write_bytes(PNG_BYTES)
            (library.path / "note.txt").write_text("x", encoding="utf-8")

            self.assertTrue(inspect.iscoroutinefunction(service.image_files_async))
            files = await service.image_files_async(library)

            self.assertEqual(files, service.image_files(library))
            self.assertEqual([path.name for path in files], ["a.png", "b.png"])

    async def test_registry_helpers_are_awaitable_and_roundtrip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            library = service.libraries()[0]
            image = library.path / "sent.png"
            image.write_bytes(PNG_BYTES)

            for method in (
                service.load_registry_async,
                service.save_registry_async,
                service.record_sent_image_async,
                service.find_duplicate_async,
            ):
                self.assertTrue(inspect.iscoroutinefunction(method))

            self.assertEqual(await service.load_registry_async(), {})
            await service.record_sent_image_async("message-1", image, library.name)
            registry = await service.load_registry_async()

            self.assertEqual(registry["message-1"]["path"], str(image.resolve()))
            self.assertEqual(registry, json.loads(service.registry_path.read_text(encoding="utf-8")))

            await service.save_registry_async({})
            self.assertEqual(await service.load_registry_async(), {})

    async def test_add_images_writes_file_and_skips_duplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            source = root / "source.png"
            source.write_bytes(PNG_BYTES)
            event = _Event([Comp.Image.fromFileSystem(str(source))])

            first = await service.add_images_from_event(event, "测试图库")
            library = service.libraries()[0]
            saved = service.image_files(library)

            self.assertIn("已保存 1 张图片", first)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), PNG_BYTES)
            self.assertEqual(await service.find_duplicate_async(library, PNG_BYTES), saved[0])

            second = await service.add_images_from_event(event, "测试图库")

            self.assertIn("没有重复保存", second)
            self.assertEqual(len(service.image_files(library)), 1)

    async def test_delete_replied_wallpaper_removes_file_and_registry_entry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            library = service.libraries()[0]
            image = library.path / "delete-me.png"
            image.write_bytes(PNG_BYTES)
            await service.record_sent_image_async("message-9", image, library.name)
            event = _Event([Comp.Reply(id="message-9")])

            message = await service.delete_replied_wallpaper(event)

            self.assertIn("已删除壁纸文件", message)
            self.assertFalse(image.exists())
            self.assertEqual(await service.load_registry_async(), {})

    async def test_send_random_wallpaper_uses_event_send_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            library = service.libraries()[0]
            (library.path / "only.png").write_bytes(PNG_BYTES)
            event = _Event([])

            self.assertEqual(await service.send_random_wallpaper(event, library), "")
            self.assertEqual(len(event.sent), 1)

    async def test_config_save_failure_is_logged_and_not_raised(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root, save_error=True)

            with patch.object(wallpaper_module, "logger") as fake_logger:
                created = service.create_library("新图库")

            self.assertEqual(created.name, "新图库")
            self.assertEqual(service.config.save_calls, 1)
            fake_logger.warning.assert_called_once()
            self.assertTrue(fake_logger.warning.call_args.kwargs.get("exc_info"))
            self.assertIn("[HelperTools/Wallpaper]", fake_logger.warning.call_args.args[0])

    async def test_read_image_ref_uses_llonebot_get_image_keys(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            bot = LLOneBotBot()
            event = _Event([])
            event.bot = bot
            ref = ImageRef(file="opaque-llonebot-id", source="onebot_reply")

            async def fake_fetch(url: str, **_kwargs: Any) -> tuple[bytes, str]:
                self.assertEqual(url, "https://example.com/llonebot.png")
                return PNG_BYTES, "image/png"

            with patch.object(wallpaper_module, "fetch_bytes", fake_fetch):
                data, extension = await service.read_image_ref(event, ref)

            self.assertEqual(data, PNG_BYTES)
            self.assertEqual(extension, ".png")
            # LLOneBot 只认 file / file_id，id / image 变体会被兼容层跳过。
            self.assertEqual(bot.image_calls, [{"file": "opaque-llonebot-id"}])

    async def test_read_image_ref_reports_readable_error_when_all_actions_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)

            class _NoImageApiBot:
                async def get_version_info(self) -> dict[str, Any]:
                    return LLONEBOT_VERSION_INFO

                async def get_image(self, **_params: Any) -> dict[str, Any]:
                    raise FakeActionFailed(1404, "get_image API 不存在")

                async def get_file(self, **_params: Any) -> dict[str, Any]:
                    raise FakeActionFailed(1404, "get_file API 不存在")

            event = _Event([])
            event.bot = _NoImageApiBot()

            with self.assertRaises(ValueError) as ctx:
                await service.read_image_ref(
                    event, ImageRef(file="opaque-id", source="onebot_reply")
                )

            self.assertIn("无法通过 OneBot 读取图片", str(ctx.exception))

    async def test_image_refs_from_onebot_message_degrades_when_cache_expired(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            bot = LLOneBotBot()
            event = _Event([])
            event.bot = bot

            # 消息缓存过期时 LLOneBot 直接抛异常，这里必须静默返回空列表。
            self.assertEqual(
                await service.image_refs_from_onebot_message(event, "790"), []
            )
            self.assertTrue(bot.msg_calls)

    async def test_image_refs_from_onebot_message_supports_non_numeric_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            bot = LLOneBotBot(
                message_payload={
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "llonebot.png",
                                "url": "https://example.com/llonebot.png",
                            },
                        }
                    ]
                }
            )
            event = _Event([])
            event.bot = bot

            refs = await service.image_refs_from_onebot_message(event, "abc-123")

            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0].url, "https://example.com/llonebot.png")
            self.assertEqual(refs[0].file, "llonebot.png")
            self.assertEqual(bot.msg_calls, [{"message_id": "abc-123"}])


if __name__ == "__main__":
    unittest.main()
