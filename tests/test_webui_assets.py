from __future__ import annotations

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
        self.assertIn('style.css?v=0.12.0', markup)
        self.assertIn('app.js?v=0.12.0', markup)
        self.assertIn("async function deleteWallpaperImage(image)", script)
        self.assertIn("await confirmAction(`删除图片", script)
        self.assertIn("await confirmAction(`删除图库", script)
        self.assertNotIn('window.confirm(`删除图片', script)
        self.assertNotIn('window.confirm(`删除图库', script)

    def test_dashboard_uses_the_top_tab_bar_shell(self) -> None:
        """v0.12.0 起控制台改成顶栏 + 横向标签栏，防止回退到旧的侧边栏结构。"""
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

        self.assertTrue(logo.lstrip().startswith("<svg"))
        self.assertTrue(mark.lstrip().startswith("<svg"))
        self.assertIn('viewBox="0 0 48 48"', mark)
        # Chromium 不渲染跨文件 <use href="x.svg#id">，顶栏必须内联 SVG。
        self.assertIn('class="brand-mark"', markup)
        self.assertIn('class="ht-v-dark"', markup)
        self.assertIn('class="ht-v-light"', markup)
        self.assertNotIn("logo-mark.svg#", markup)


if __name__ == "__main__":
    unittest.main()
