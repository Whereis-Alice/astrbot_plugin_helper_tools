"""校验插件版本号在各处保持一致，避免发布时漏改其中一处。"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI_ROOT = ROOT / "pages" / "helper-tools"


def _main_version() -> str:
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(r'^PLUGIN_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "main.py 缺少 PLUGIN_VERSION 定义"
    return match.group(1)


def _metadata_version() -> str:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*v?([^\s#]+)", text, re.MULTILINE)
    assert match is not None, "metadata.yaml 缺少 version 字段"
    return match.group(1)


def _changelog_version() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^##\s*\[v?([^\]]+)\]", text, re.MULTILINE)
    assert match is not None, "CHANGELOG.md 缺少版本小节"
    return match.group(1)


class PluginVersionConsistencyTests(unittest.TestCase):
    def test_main_matches_metadata(self) -> None:
        self.assertEqual(_main_version(), _metadata_version())

    def test_changelog_head_matches_metadata(self) -> None:
        self.assertEqual(_changelog_version(), _metadata_version())

    def test_version_looks_like_semver(self) -> None:
        self.assertRegex(_metadata_version(), r"^\d+\.\d+\.\d+$")

    def test_dashboard_cachebusters_match_metadata(self) -> None:
        """控制台静态资源的 ?v= 直接用插件版本号；漏改会让浏览器继续吃旧缓存。"""
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")
        version = _metadata_version()

        for asset in ("style.css", "app.js", "logo-mark.svg"):
            self.assertIn(f"{asset}?v={version}", markup)
        self.assertEqual(set(re.findall(r"\?v=([^\"'&]+)", markup)), {version})


class PluginCardIconTests(unittest.TestCase):
    """AstrBot 插件管理页的卡片图标只认插件根目录下的 logo.png。

    文件名在 StarManager.logo_fname 里硬编码，既不能改名也不吃 SVG；
    缺了这个文件，卡片就只会显示控制台自带的通用占位图标。
    """

    def test_root_logo_png_exists(self) -> None:
        logo = ROOT / "logo.png"
        self.assertTrue(logo.is_file(), "插件根目录缺少 logo.png，插件卡片会掉回占位图标")

    def test_root_logo_png_is_square_bitmap(self) -> None:
        """卡片以 64x64（窄屏 52x52）+ object-fit: cover 呈现，必须是正方形位图。"""
        data = (ROOT / "logo.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", "logo.png 不是真正的 PNG")
        self.assertEqual(data[12:16], b"IHDR")

        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        self.assertEqual(width, height, "卡片会按正方形裁切，非正方形会被裁掉边缘")
        self.assertGreaterEqual(width, 128, "位图过小，在 64px 的两倍屏下会发虚")

    def test_root_logo_svg_is_the_png_source(self) -> None:
        markup = (ROOT / "logo.svg").read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 512 512"', markup)
        self.assertIn('mask="url(#hta-cut)"', markup)

    def test_card_icon_geometry_matches_dashboard_logo(self) -> None:
        """卡片图标与控制台图标必须同几何，避免改了一处另一处悄悄跑偏。"""
        tile = (ROOT / "logo.svg").read_text(encoding="utf-8")
        mark = (WEBUI_ROOT / "logo-mark.svg").read_text(encoding="utf-8")

        shapes = re.findall(r'<(?:circle|path)\s[^>]*?(?:d|cx)="[^"]+"[^>]*/>', mark)
        self.assertGreaterEqual(len(shapes), 5, "未能从 logo-mark.svg 解析出图形")

        for shape in shapes:
            geometry = re.search(r'\sd="([^"]+)"', shape)
            if geometry is None:
                continue
            if geometry.group(1).startswith("M0 0h48v48"):
                continue  # 承载渐变的整幅底板，瓦片里换成了 512 尺寸
            self.assertIn(geometry.group(1), tile, "卡片图标与控制台图标几何不一致")


if __name__ == "__main__":
    unittest.main()
