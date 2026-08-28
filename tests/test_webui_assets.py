from __future__ import annotations

import re
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WEBUI_ROOT = PLUGIN_ROOT / "pages" / "helper-tools"


class DashboardAssetTests(unittest.TestCase):
    def test_destructive_actions_use_the_in_page_confirmation_dialog(self) -> None:
        script = (WEBUI_ROOT / "app.js").read_text(encoding="utf-8")
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="confirm-action-dialog"', markup)
        self.assertIn('id="confirm-action-form"', markup)
        self.assertIn("async function deleteWallpaperImage(image)", script)
        self.assertIn("await confirmAction(`删除图片", script)
        self.assertIn("await confirmAction(`删除图库", script)
        self.assertNotIn('window.confirm(`删除图片', script)
        self.assertNotIn('window.confirm(`删除图库', script)

    def test_dashboard_uses_the_top_tab_bar_shell(self) -> None:
        """v1.0.0 起控制台改成顶栏 + 横向标签栏，防止回退到旧的侧边栏结构。"""
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")
        stylesheet = (WEBUI_ROOT / "style.css").read_text(encoding="utf-8")

        self.assertIn('class="app-shell"', markup)
        self.assertIn('class="app-header"', markup)
        self.assertIn('class="tab-bar"', markup)
        self.assertIn('class="app-main"', markup)
        self.assertIn('class="app-status"', markup)
        self.assertNotIn("dashboard-shell", markup)
        self.assertNotIn("dashboard-shell", stylesheet)
        self.assertNotIn('class="sidebar', markup)

        # app.js 仍靠这些 id / data 属性驱动，改版时不能顺手删掉。
        self.assertIn('id="sidebar-status"', markup)
        self.assertIn('id="header-state"', markup)
        self.assertIn('id="page-kicker"', markup)
        for tab in ("overview", "config", "activity", "wallpaper", "storage", "about"):
            self.assertIn(f'data-tab="{tab}"', markup)
            self.assertIn(f'id="tab-{tab}" class="tab-pane', markup)

    def test_save_button_keeps_a_span_for_the_dirty_label(self) -> None:
        """setDirty() 直接改 #save-button 里第一个 span 的文本，结构必须保住。"""
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")
        script = (WEBUI_ROOT / "app.js").read_text(encoding="utf-8")

        save_button = markup.split('id="save-button"', 1)[1].split("</button>", 1)[0]
        self.assertIn("<span>", save_button)
        self.assertLess(save_button.index("<span>"), save_button.index("</span>"))
        self.assertIn('button.querySelector("span")', script)

    def test_tab_badges_and_version_labels_are_wired(self) -> None:
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")
        script = (WEBUI_ROOT / "app.js").read_text(encoding="utf-8")

        for badge in ("tab-badge-config", "tab-badge-activity", "tab-badge-wallpaper"):
            self.assertIn(f'id="{badge}"', markup)
            self.assertIn(f'"{badge}"', script)
        self.assertIn('id="brand-version"', markup)
        self.assertIn('id="footer-version"', markup)
        self.assertIn("function renderTabBadges()", script)
        self.assertIn("function renderVersionLabels()", script)
        self.assertIn("renderTabBadges();", script)
        self.assertIn("renderVersionLabels();", script)

    def test_plugin_logo_assets_exist(self) -> None:
        logo = (WEBUI_ROOT / "logo.svg").read_text(encoding="utf-8")
        mark = (WEBUI_ROOT / "logo-mark.svg").read_text(encoding="utf-8")
        markup = (WEBUI_ROOT / "index.html").read_text(encoding="utf-8")
        stylesheet = (WEBUI_ROOT / "style.css").read_text(encoding="utf-8")

        self.assertTrue(logo.lstrip().startswith("<svg"))
        self.assertTrue(mark.lstrip().startswith("<svg"))
        self.assertIn('viewBox="0 0 48 48"', mark)
        self.assertIn('viewBox="0 0 158 48"', logo)

        # v1.0.1 起标识是 45° 两用扳手，挖孔必须用 <mask>：柄和两个头相互重叠，
        # 换成 fill-rule 会在重叠处挖出空洞，别改回去。
        self.assertIn('mask="url(#ht-cut)"', mark)
        self.assertIn('mask="url(#htl-cut)"', logo)
        self.assertIn("url(#ht-grad-light)", mark)
        self.assertIn("url(#htl-grad-light)", logo)
        for svg, art in ((mark, "ht-art"), (logo, "htl-art")):
            with self.subTest(art=art):
                self.assertIn("prefers-color-scheme: light", svg)
                self.assertIn(f".ht-light .{art}", svg)
                self.assertIn(f".ht-dark .{art}", svg)

        # 旧的「圆角面板 + 工字」标识连同两个 <symbol> 已删除，防止半新半旧混用。
        for stale in ("ht-panel", "ht-edge", "ht-glyph", "ht-mark-dark", "ht-mark-light"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, mark)
                self.assertNotIn(stale, logo)
                self.assertNotIn(stale, markup)

        # Chromium 不渲染跨文件 <use href="x.svg#id">，顶栏必须内联 SVG。
        self.assertIn('class="brand-mark"', markup)
        self.assertNotIn("logo-mark.svg#", markup)
        brand = markup.split('class="brand-mark"', 1)[1].split("</span>", 1)[0]
        self.assertIn('class="ht-v-dark"', brand)
        self.assertIn('class="ht-v-light"', brand)
        self.assertIn('mask="url(#ht-cut)"', brand)
        self.assertIn("url(#ht-grad)", brand)
        self.assertIn("url(#ht-grad-light)", brand)
        # 内联 SVG 里不能出现 style 元素，否则规则会泄漏到整个控制台页面（注释里提到不算）。
        self.assertNotIn("<style", re.sub(r"<!--.*?-->", "", brand, flags=re.DOTALL))
        # 顶栏的深浅变体靠 style.css 的 body[data-theme] 规则切换。
        self.assertIn('body[data-theme="light"] .brand-mark .ht-v-dark', stylesheet)
        self.assertIn('body[data-theme="light"] .brand-mark .ht-v-light', stylesheet)

        # 控制台自己的 favicon 用紧凑版标识，相对路径交给 AstrBot 重写。
        self.assertIn('rel="icon"', markup)
        self.assertIn('href="./logo-mark.svg?v=', markup)


if __name__ == "__main__":
    unittest.main()
