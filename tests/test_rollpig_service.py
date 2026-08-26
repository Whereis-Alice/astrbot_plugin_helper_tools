from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from astrbot_plugin_helper_tools.rollpig_service import RollPigService


class FakeEvent:
    def __init__(
        self, *, sender_id: str = "20001", mentions: list[str] | None = None
    ) -> None:
        self._sender_id = sender_id
        self._mentions = mentions or []

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return "10001"

    def get_group_id(self) -> str:
        return "30001"

    def get_messages(self) -> list[object]:
        return [SimpleNamespace(qq=target_id) for target_id in self._mentions]


class RollPigServiceTests(unittest.IsolatedAsyncioTestCase):
    def _assets(self, root: Path) -> Path:
        assets = root / "assets"
        (assets / "image").mkdir(parents=True)
        (assets / "font").mkdir()
        (assets / "pig.json").write_text(
            json.dumps(
                [
                    {
                        "id": "test-pig",
                        "name": "Test Pig",
                        "description": "Ready",
                        "analysis": "A stable daily result for testing.",
                    },
                    {
                        "id": "second-pig",
                        "name": "Second Pig",
                        "description": "Also ready",
                        "analysis": "Another valid entry.",
                    },
                ]
            ),
            encoding="utf-8",
        )
        Image.new("RGBA", (64, 32), (220, 130, 150, 255)).save(
            assets / "image" / "test-pig.png"
        )
        Image.new("RGBA", (32, 64), (130, 180, 220, 255)).save(
            assets / "image" / "second-pig.png"
        )
        return assets

    async def test_daily_selection_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RollPigService({}, root / "data", assets_dir=self._assets(root))

            first, first_error = await service.select_pig("20001", "2026-07-27")
            second, second_error = await service.select_pig("20001", "2026-07-27")

            self.assertEqual(first_error, "")
            self.assertEqual(second_error, "")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(first.pig_id, second.pig_id)

    async def test_mentioned_target_uses_message_components_not_plain_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RollPigService(
                {"rollpig": {"allow_mentioned_user": True}},
                root / "data",
                assets_dir=self._assets(root),
            )
            event = FakeEvent(mentions=["20002"])

            target_id, error = service.resolve_target(event)

            self.assertEqual(error, "")
            self.assertEqual(target_id, "20002")

    async def test_mentioned_target_is_rejected_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RollPigService({}, root / "data", assets_dir=self._assets(root))

            _, error = service.resolve_target(FakeEvent(mentions=["20002"]))

            self.assertIn("未开启", error)

    async def test_render_normalizes_float_dimensions_before_pillow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RollPigService({}, root / "data", assets_dir=self._assets(root))
            service.CARD_WIDTH = 480.0
            service.MIN_CARD_HEIGHT = 600.0
            service.MAX_CARD_HEIGHT = 1200.0
            service.CARD_PADDING = 32.0
            service.AVATAR_SIZE = 160.0
            pig, error = await service.select_pig("20001", "2026-07-27")

            self.assertEqual(error, "")
            assert pig is not None
            rendered = service.render_card(pig, "20001", "2026-07-27")

            self.assertIsNotNone(rendered)
            assert rendered is not None
            self.assertTrue(rendered.is_file())
            with Image.open(rendered) as image:
                self.assertEqual(image.width, 480)
                self.assertIsInstance(image.height, int)

    async def test_state_file_io_runs_in_a_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RollPigService({}, root / "data", assets_dir=self._assets(root))
            main_thread = threading.get_ident()
            thread_ids: list[int] = []
            original_save = service._save_today_state

            def tracking_save(state: dict[str, object]) -> None:
                thread_ids.append(threading.get_ident())
                original_save(state)

            service._save_today_state = tracking_save  # type: ignore[method-assign]
            pig, error = await service.select_pig("20001", "2026-07-27")

            self.assertEqual(error, "")
            self.assertIsNotNone(pig)
            self.assertEqual(len(thread_ids), 1)
            self.assertNotEqual(thread_ids[0], main_thread)
            self.assertTrue(service.state_path.is_file())

    async def test_font_falls_back_to_pillow_default_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RollPigService({}, root / "data", assets_dir=self._assets(root))
            warnings: list[str] = []

            service._bundled_font_paths = lambda kind: []  # type: ignore[method-assign]
            service._system_font_paths = lambda kind: iter(())  # type: ignore[method-assign]
            service._warn_font_fallback = lambda: warnings.append("warned")  # type: ignore[method-assign]

            font = service._font("bold", 32)

            self.assertIsNotNone(font)
            self.assertEqual(warnings, ["warned"])
            self.assertIsNone(service._resolve_font_path("bold"))

    async def test_bundled_font_is_discovered_from_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = self._assets(root)
            bundled = assets / "font" / "bundled-sample.ttf"
            bundled.write_bytes(b"not-a-real-font")
            service = RollPigService({}, root / "data", assets_dir=assets)

            self.assertEqual(service._resolve_font_path("regular"), bundled)
            # An unreadable bundled font must degrade to the Pillow default, not raise.
            self.assertIsNotNone(service._font("regular", 28))


if __name__ == "__main__":
    unittest.main()
