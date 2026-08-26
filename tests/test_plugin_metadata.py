"""校验插件版本号在各处保持一致，避免发布时漏改其中一处。"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


if __name__ == "__main__":
    unittest.main()
