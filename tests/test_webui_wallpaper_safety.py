from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from PIL import Image

from astrbot_plugin_helper_tools.wallpaper_service import WallpaperService
from astrbot_plugin_helper_tools.webui_wallpaper import (
    WallpaperDashboardError,
    WallpaperLibraryDashboard,
    WallpaperUpload,
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fea735c8f60000000049454e44ae426082"
)


def _valid_png_bytes() -> bytes:
    """上传接口会用 PIL 打开并 verify()，所以这里必须给一张真实可解析的 PNG。"""
    buffer = BytesIO()
    Image.new("RGB", (8, 6), "#4bf0df").save(buffer, format="PNG")
    return buffer.getvalue()


VALID_PNG_BYTES = _valid_png_bytes()


class _Config(dict[str, Any]):
    def __init__(self, value: dict[str, Any]) -> None:
        super().__init__(value)
        self.save_calls = 0

    def save_config(self) -> None:
        self.save_calls += 1


class _DashboardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "plugin-data"
        self.data_dir.mkdir()

    def _dashboard(
        self,
        libraries: list[dict[str, Any]],
        *,
        wallpaper: dict[str, Any] | None = None,
    ) -> WallpaperLibraryDashboard:
        section: dict[str, Any] = {"libraries": libraries}
        section.update(wallpaper or {})
        config = _Config({"wallpaper": section})
        service = WallpaperService(config, self.data_dir, None)
        return WallpaperLibraryDashboard(service, self.data_dir)


class LibraryPurgeBlastRadiusTests(_DashboardCase):
    """「删除图库磁盘文件」以前是 shutil.rmtree，会连非图片文件一起删掉。"""

    def test_purge_deletes_indexed_images_and_keeps_unrelated_files(self) -> None:
        library_dir = self.root / "mixed"
        (library_dir / "sub").mkdir(parents=True)
        (library_dir / "one.png").write_bytes(PNG_BYTES)
        (library_dir / "sub" / "two.png").write_bytes(PNG_BYTES)
        notes = library_dir / "notes.txt"
        notes.write_text("不该被图库删除的文档", encoding="utf-8")
        report = library_dir / "sub" / "report.pdf"
        report.write_bytes(b"%PDF-1.4 fake")
        dashboard = self._dashboard(
            [{"name": "混放图库", "path": str(library_dir), "recursive": True}]
        )

        result = dashboard.delete_library(
            "0",
            delete_files=True,
            confirmation_name="混放图库",
        )

        self.assertTrue(result["files_deleted"])
        self.assertEqual(result["images_deleted"], 2)
        self.assertEqual(result["images_failed"], 0)
        self.assertEqual(result["kept_files"], 2)
        self.assertFalse(result["directory_removed"])
        self.assertIn("保留 2 个非图片文件", result["message"])
        self.assertFalse((library_dir / "one.png").exists())
        self.assertFalse((library_dir / "sub" / "two.png").exists())
        self.assertTrue(notes.is_file())
        self.assertEqual(notes.read_text(encoding="utf-8"), "不该被图库删除的文档")
        self.assertTrue(report.is_file())
        self.assertEqual(dashboard.configuration_entries(), [])

    def test_purge_removes_the_directory_when_only_images_were_stored(self) -> None:
        library_dir = self.root / "clean"
        (library_dir / "nested").mkdir(parents=True)
        (library_dir / "nested" / "a.png").write_bytes(PNG_BYTES)
        (library_dir / "b.png").write_bytes(PNG_BYTES)
        dashboard = self._dashboard(
            [{"name": "纯图图库", "path": str(library_dir), "recursive": True}]
        )

        result = dashboard.delete_library(
            "0",
            delete_files=True,
            confirmation_name="纯图图库",
        )

        self.assertEqual(result["images_deleted"], 2)
        self.assertEqual(result["kept_files"], 0)
        self.assertTrue(result["directory_removed"])
        self.assertFalse(library_dir.exists())

    def test_non_recursive_purge_keeps_images_it_never_indexed(self) -> None:
        library_dir = self.root / "flat"
        (library_dir / "sub").mkdir(parents=True)
        (library_dir / "top.png").write_bytes(PNG_BYTES)
        deep = library_dir / "sub" / "deep.png"
        deep.write_bytes(PNG_BYTES)
        dashboard = self._dashboard(
            [{"name": "非递归图库", "path": str(library_dir), "recursive": False}]
        )

        result = dashboard.delete_library(
            "0",
            delete_files=True,
            confirmation_name="非递归图库",
        )

        self.assertEqual(result["images_deleted"], 1)
        self.assertEqual(result["kept_files"], 1)
        self.assertFalse(result["directory_removed"])
        self.assertTrue(deep.is_file())

    def test_purge_refuses_the_user_home_directory(self) -> None:
        fake_home = self.root / "home"
        fake_home.mkdir()
        photo = fake_home / "photo.png"
        photo.write_bytes(PNG_BYTES)
        dashboard = self._dashboard(
            [{"name": "主目录图库", "path": str(fake_home), "recursive": True}]
        )
        resolved_home = fake_home.resolve()

        with (
            patch.object(
                WallpaperLibraryDashboard,
                "_home_directory",
                staticmethod(lambda: resolved_home),
            ),
            self.assertRaises(WallpaperDashboardError) as caught,
        ):
                dashboard.delete_library(
                    "0",
                    delete_files=True,
                    confirmation_name="主目录图库",
                )

        self.assertIn("主目录", str(caught.exception))
        self.assertTrue(photo.is_file())
        self.assertEqual(len(dashboard.configuration_entries()), 1)

    def test_purge_still_refuses_data_dir_and_wallpapers_root(self) -> None:
        (self.data_dir / "wallpapers").mkdir()
        for raw_path in (str(self.data_dir), str(self.data_dir / "wallpapers")):
            with self.subTest(path=raw_path):
                dashboard = self._dashboard(
                    [{"name": "危险图库", "path": raw_path, "recursive": True}]
                )
                with self.assertRaises(WallpaperDashboardError):
                    dashboard.delete_library(
                        "0",
                        delete_files=True,
                        confirmation_name="危险图库",
                    )
                self.assertTrue(Path(raw_path).is_dir())

    def test_purge_still_refuses_a_root_overlapping_another_library(self) -> None:
        outer = self.root / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (inner / "keep.png").write_bytes(PNG_BYTES)
        dashboard = self._dashboard(
            [
                {"name": "外层图库", "path": str(outer), "recursive": True},
                {"name": "内层图库", "path": str(inner), "recursive": True},
            ]
        )

        with self.assertRaises(WallpaperDashboardError):
            dashboard.delete_library(
                "0",
                delete_files=True,
                confirmation_name="外层图库",
            )

        self.assertTrue((inner / "keep.png").is_file())

    def test_configuration_only_delete_keeps_every_file(self) -> None:
        library_dir = self.root / "config-only"
        library_dir.mkdir()
        image = library_dir / "one.png"
        image.write_bytes(PNG_BYTES)
        dashboard = self._dashboard(
            [{"name": "只删配置", "path": str(library_dir), "recursive": True}]
        )

        result = dashboard.delete_library("0", delete_files=False)

        self.assertFalse(result["files_deleted"])
        self.assertEqual(result["images_deleted"], 0)
        self.assertTrue(image.is_file())


class ImagePathTraversalTests(_DashboardCase):
    """图片相对路径必须解析后仍落在图库根目录内。"""

    def setUp(self) -> None:
        super().setUp()
        self.library_dir = self.root / "library"
        self.library_dir.mkdir()
        self.inside = self.library_dir / "one.png"
        self.inside.write_bytes(PNG_BYTES)
        self.outside = self.root / "evil.png"
        self.outside.write_bytes(PNG_BYTES)
        self.secret = self.root / "secret.txt"
        self.secret.write_text("凭据", encoding="utf-8")
        self.dashboard = self._dashboard(
            [{"name": "图库", "path": str(self.library_dir), "recursive": True}]
        )

    ESCAPES = (
        "../evil.png",
        "../../evil.png",
        "sub/../../evil.png",
        "../secret.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "\\\\host\\share\\x.png",
        "a:b.png",
        "one.png::$DATA",
        "NUL.png",
        "CON",
        "one.png\x00.txt",
        "",
        ".",
        "..",
    )

    def test_resolve_image_rejects_paths_outside_the_library(self) -> None:
        for candidate in self.ESCAPES:
            with self.subTest(path=candidate), self.assertRaises(WallpaperDashboardError):
                self.dashboard.resolve_image("0", candidate)

    def test_delete_image_rejects_paths_outside_the_library(self) -> None:
        for candidate in self.ESCAPES:
            with self.subTest(path=candidate), self.assertRaises(WallpaperDashboardError):
                self.dashboard.delete_image("0", candidate)
        self.assertTrue(self.outside.is_file())
        self.assertTrue(self.secret.is_file())
        self.assertTrue(self.inside.is_file())

    def test_rename_image_rejects_source_paths_outside_the_library(self) -> None:
        for candidate in self.ESCAPES:
            with self.subTest(path=candidate), self.assertRaises(WallpaperDashboardError):
                self.dashboard.rename_image("0", candidate, "renamed")
        self.assertTrue(self.outside.is_file())

    def test_resolve_image_accepts_a_normal_relative_path(self) -> None:
        _record, resolved = self.dashboard.resolve_image("0", "one.png")

        self.assertEqual(resolved, self.inside.resolve())

    def test_invalid_library_identifiers_are_rejected(self) -> None:
        for library_id in ("../0", "0/../0", "-1", "1e1", "abc", "999999", ""):
            with self.subTest(library_id=library_id), self.assertRaises(WallpaperDashboardError):
                self.dashboard.get_library(library_id)

    def test_rename_keeps_the_image_inside_the_library(self) -> None:
        for index, requested in enumerate(
            ("../escaped", "..\\escaped", "/tmp/escaped", "C:/Windows/escaped", "sub/escaped", "NUL"),
        ):
            with self.subTest(new_name=requested):
                source = self.library_dir / f"movable_{index}.png"
                source.write_bytes(PNG_BYTES)
                unique = requested if requested == "NUL" else f"{requested}_{index}"

                try:
                    result = self.dashboard.rename_image("0", source.name, unique)
                except WallpaperDashboardError:
                    # 「NUL」这类保留设备名会撞上「同名文件已存在」而被拒绝，源文件保持原样也算安全。
                    self.assertTrue(source.is_file())
                    continue

                target = self.library_dir / result["relative_path"]
                self.assertTrue(target.is_file())
                self.assertEqual(target.parent, self.library_dir)
                self.assertNotIn("/", result["relative_path"])
                self.assertFalse((self.root / result["name"]).exists())


class UploadFilenameTests(_DashboardCase):
    def test_upload_normalizes_filenames_and_never_escapes_the_library(self) -> None:
        library_dir = self.root / "uploads"
        dashboard = self._dashboard(
            [{"name": "上传图库", "path": str(library_dir), "recursive": True}],
            wallpaper={"deduplicate_on_add": False},
        )
        uploads = [
            WallpaperUpload(filename="../../evil.png", data=VALID_PNG_BYTES),
            WallpaperUpload(filename="C:\\Windows\\system32\\evil.png", data=VALID_PNG_BYTES),
            WallpaperUpload(filename="\\\\host\\share\\evil.png", data=VALID_PNG_BYTES),
            WallpaperUpload(filename="trailing. .png", data=VALID_PNG_BYTES),
            WallpaperUpload(filename="NUL.png", data=VALID_PNG_BYTES),
        ]

        result = dashboard.upload_images("0", uploads)

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["saved"]), len(uploads))
        for saved in result["saved"]:
            with self.subTest(name=saved["name"]):
                self.assertNotIn("/", saved["relative_path"])
                self.assertNotIn("\\", saved["relative_path"])
                self.assertNotIn("..", saved["relative_path"])
                stored = library_dir / saved["relative_path"]
                self.assertEqual(stored.parent, library_dir)
                self.assertTrue(stored.is_file())
                self.assertEqual(stored.read_bytes(), VALID_PNG_BYTES)
        self.assertFalse((self.root / "evil.png").exists())
        self.assertFalse((self.root.parent / "evil.png").exists())
        self.assertEqual(len(list(library_dir.iterdir())), len(uploads))

    def test_upload_rejects_non_image_payloads_and_wrong_extensions(self) -> None:
        library_dir = self.root / "guarded"
        dashboard = self._dashboard(
            [{"name": "校验图库", "path": str(library_dir), "recursive": True}],
            wallpaper={"deduplicate_on_add": False},
        )
        uploads = [
            WallpaperUpload(filename="payload.php", data=VALID_PNG_BYTES),
            WallpaperUpload(filename="fake.png", data=b"<?php echo 1; ?>"),
            WallpaperUpload(filename="mismatch.gif", data=VALID_PNG_BYTES),
        ]

        result = dashboard.upload_images("0", uploads)

        self.assertEqual(result["saved"], [])
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(list(library_dir.iterdir()), [])


class RegistryAtomicWriteTests(unittest.TestCase):
    """已发送图片索引写坏会让撤回删图失效，所以必须先写临时文件再替换。"""

    def test_interrupted_registry_write_keeps_the_previous_file_intact(self) -> None:
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            service = WallpaperService(_Config({}), data_dir, None)
            first = {"one": {"path": "a.png", "library": "图库", "sent_at": "1"}}
            service.save_registry(first)
            original = service.registry_path.read_text(encoding="utf-8")
            real_write_text = Path.write_text

            def _truncated_write(target: Path, data: str, *args: Any, **kwargs: Any) -> int:
                real_write_text(target, data[: max(len(data) // 2, 1)], *args, **kwargs)
                raise OSError("模拟磁盘写入中断")

            with patch.object(Path, "write_text", _truncated_write), self.assertRaises(OSError):
                service.save_registry(
                    {"two": {"path": "b.png", "library": "图库", "sent_at": "2"}}
                )

            self.assertEqual(service.registry_path.read_text(encoding="utf-8"), original)
            self.assertEqual(service.load_registry(), first)
            self.assertEqual(list(data_dir.glob("*.tmp")), [])

    def test_registry_round_trip_still_works_after_the_atomic_rewrite(self) -> None:
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            service = WallpaperService(_Config({}), data_dir, None)
            payload = {"one": {"path": "a.png", "library": "图库", "sent_at": "1"}}

            service.save_registry(payload)

            self.assertEqual(service.load_registry(), payload)
            self.assertEqual(list(data_dir.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
