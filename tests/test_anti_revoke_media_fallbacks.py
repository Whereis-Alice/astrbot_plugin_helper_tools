from __future__ import annotations

import asyncio
import base64
import tempfile
import time
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from astrbot_plugin_helper_tools import anti_revoke_service, onebot_compat
from astrbot_plugin_helper_tools.anti_revoke_service import AntiRevokeService
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.tests.test_anti_revoke_service import (
    TINY_PNG_BASE64,
    FakeActionFailed,
    FakeBot,
    FakeEvent,
    FakeImageComponent,
)

UNSHARED_IMAGE_PATH = "/not-shared/a.png"
IMAGE_ID = "opaque-image-id"


class FileFallbackBot(FakeBot):
    def __init__(
        self,
        *,
        image_key: str = "file",
        file_key: str = "file_id",
        readable_image: bool = False,
    ) -> None:
        super().__init__()
        self.image_key = image_key
        self.file_key = file_key
        self.readable_image = readable_image
        self.actions: list[tuple[str, dict[str, object]]] = []

    async def get_msg(self, **_params: object) -> dict[str, object]:
        return {}

    async def get_image(self, **params: object) -> dict[str, object]:
        self.actions.append(("get_image", dict(params)))
        if params != {self.image_key: IMAGE_ID}:
            raise FakeActionFailed(1400, f"missing parameter {self.image_key}")
        data = (
            {"base64": TINY_PNG_BASE64}
            if self.readable_image
            else {"file": UNSHARED_IMAGE_PATH}
        )
        return {"status": "ok", "retcode": 0, "data": data}

    async def get_file(self, **params: object) -> dict[str, object]:
        self.actions.append(("get_file", dict(params)))
        if params != {self.file_key: IMAGE_ID}:
            raise FakeActionFailed(1400, f"missing parameter {self.file_key}")
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"base64": TINY_PNG_BASE64},
        }

    async def get_group_file_url(self, **params: object) -> dict[str, object]:
        self.actions.append(("get_group_file_url", dict(params)))
        raise FakeActionFailed(1404, "unexpected group-file fallback")


class AntiRevokeMediaFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()
        self.addCleanup(reset_compat_caches)
        temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.service = AntiRevokeService({"anti_revoke": {"enabled": True}}, temp_dir)
        self.addAsyncCleanup(self.service.stop)
        self.fetch = self.enterContext(
            patch(
                "astrbot_plugin_helper_tools.anti_revoke_service.fetch_bytes",
                new_callable=AsyncMock,
                side_effect=AssertionError("unexpected network access"),
            )
        )

    def _event(
        self,
        bot: FakeBot,
        message: list[dict[str, object]] | None = None,
        components: list[object] | None = None,
    ) -> FakeEvent:
        return FakeEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": message
                if message is not None
                else [{"type": "image", "data": {"file": IMAGE_ID}}],
            },
            bot,
            components,
        )

    def _snapshot_bytes(self, key: str) -> bytes:
        record = self.service._records["123:456"]
        self.assertIn(
            key, record.image_cache_files, "expected a successful image snapshot"
        )
        path = self.service.cache_dir / record.image_cache_files[key]
        data = path.read_bytes()
        with Image.open(BytesIO(data)) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
        return data

    async def test_unshared_get_image_path_falls_back_to_get_file_snapshot(
        self,
    ) -> None:
        bot = FileFallbackBot(image_key="file_id")

        await self.service.handle_event(self._event(bot))

        self.assertEqual(
            bot.actions,
            [
                ("get_image", {"file": IMAGE_ID}),
                ("get_image", {"file_id": IMAGE_ID}),
                ("get_file", {"file_id": IMAGE_ID}),
            ],
        )
        self.assertEqual(self._snapshot_bytes("0"), base64.b64decode(TINY_PNG_BASE64))
        self.fetch.assert_not_awaited()

    async def test_get_file_fallback_preserves_file_parameter_compatibility(
        self,
    ) -> None:
        bot = FileFallbackBot(file_key="file")

        await self.service.handle_event(self._event(bot))

        self.assertEqual(
            bot.actions,
            [
                ("get_image", {"file": IMAGE_ID}),
                ("get_file", {"file_id": IMAGE_ID}),
                ("get_file", {"file": IMAGE_ID}),
            ],
        )
        self.assertEqual(self._snapshot_bytes("0"), base64.b64decode(TINY_PNG_BASE64))
        self.fetch.assert_not_awaited()

    async def test_readable_get_image_does_not_call_get_file(self) -> None:
        bot = FileFallbackBot(readable_image=True)

        await self.service.handle_event(self._event(bot))

        self.assertEqual(bot.actions, [("get_image", {"file": IMAGE_ID})])
        self.assertEqual(self._snapshot_bytes("0"), base64.b64decode(TINY_PNG_BASE64))
        self.fetch.assert_not_awaited()

    async def _check_resolution_deadline(self, elapsed: float) -> None:
        bot = FileFallbackBot()
        clock = [100.0]
        real_read = self.service._read_image_reference

        async def read_reference(reference: str, **kwargs: object) -> tuple[bytes, str]:
            if reference == UNSHARED_IMAGE_PATH:
                clock[0] += elapsed
                raise FileNotFoundError(UNSHARED_IMAGE_PATH)
            return await real_read(reference, **kwargs)

        # Keep asyncio's own clock real while advancing only the resolver's budget.
        resolver_time = SimpleNamespace(time=time.time, monotonic=lambda: clock[0])
        with (
            patch.object(anti_revoke_service, "time", resolver_time),
            patch(
                "astrbot_plugin_helper_tools.onebot_compat.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch(
                "astrbot_plugin_helper_tools.onebot_compat.asyncio.wait_for",
                new=AsyncMock(wraps=asyncio.wait_for),
            ) as wait,
            patch.object(
                self.service, "_read_image_reference", side_effect=read_reference
            ),
        ):
            resolution = self.service._resolve_image_with_onebot(
                self._event(bot), {"file": IMAGE_ID}
            )
            if elapsed < 8:
                data, extension = await resolution
                self.assertEqual(data, base64.b64decode(TINY_PNG_BASE64))
                self.assertEqual(extension, ".png")
            else:
                with self.assertRaises(ValueError):
                    await resolution

        # Reference reads may also be bounded with wait_for; inspect only API calls.
        timeouts = [
            call.args[1] if len(call.args) > 1 else call.kwargs["timeout"]
            for call in wait.await_args_list
            if getattr(call.args[0], "cr_code", None)
            is onebot_compat.call_onebot.__code__
        ]
        if elapsed < 8:
            self.assertEqual(
                bot.actions,
                [
                    ("get_image", {"file": IMAGE_ID}),
                    ("get_file", {"file_id": IMAGE_ID}),
                ],
            )
            self.assertEqual(timeouts, [1.5, 8.0 - elapsed])
        else:
            self.assertEqual(bot.actions, [("get_image", {"file": IMAGE_ID})])
            self.assertEqual(timeouts, [1.5])
        self.fetch.assert_not_awaited()

    async def test_get_file_fallback_uses_remaining_original_deadline(self) -> None:
        await self._check_resolution_deadline(elapsed=7.0)

    async def test_exhausted_deadline_does_not_start_get_file(self) -> None:
        await self._check_resolution_deadline(elapsed=8.0)

    @staticmethod
    def _png_bytes(color: tuple[int, int, int]) -> bytes:
        output = BytesIO()
        Image.new("RGB", (2, 2), color).save(output, format="PNG")
        return output.getvalue()

    async def _check_mixed_image_snapshots(self, special_type: str) -> None:
        red_color, blue_color = (255, 0, 0), (0, 0, 255)
        red, blue = self._png_bytes(red_color), self._png_bytes(blue_color)
        self.assertNotEqual(red, blue)
        red_url = "https://gchat.qpic.cn/red.png"
        blue_url = "https://gchat.qpic.cn/blue.png"
        images = {red_url: red, blue_url: blue}

        async def fetch_image(url: str, **_kwargs: object) -> tuple[bytes, str]:
            return images[url], "image/png"

        self.fetch.side_effect = fetch_image
        event = self._event(
            FakeBot(),
            [
                {"type": special_type, "data": {"url": red_url}},
                {"type": "image", "data": {"file": "opaque-blue-image"}},
            ],
            [FakeImageComponent(file=blue_url)],
        )

        await self.service.handle_event(event)

        record = self.service._records["123:456"]
        self.assertEqual(set(record.image_cache_files), {"0", "1"})
        special_snapshot, ordinary_snapshot = (
            self._snapshot_bytes("0"),
            self._snapshot_bytes("1"),
        )
        self.assertEqual(
            special_snapshot, red, f"{special_type} must keep its own red image"
        )
        self.assertEqual(
            ordinary_snapshot, blue, "the ordinary Image component belongs to image"
        )
        self.assertNotEqual(special_snapshot, ordinary_snapshot)
        for snapshot, color in (
            (special_snapshot, red_color),
            (ordinary_snapshot, blue_color),
        ):
            with Image.open(BytesIO(snapshot)) as image:
                rgb = image.convert("RGB")
                self.assertEqual([rgb.getpixel((x, y)) for y in range(2) for x in range(2)], [color] * 4)
        self.assertCountEqual(
            [call.args[0] for call in self.fetch.await_args_list], [red_url, blue_url]
        )

    async def test_mface_does_not_consume_ordinary_image_component(self) -> None:
        await self._check_mixed_image_snapshots("mface")

    async def test_flash_does_not_consume_ordinary_image_component(self) -> None:
        await self._check_mixed_image_snapshots("flash")


if __name__ == "__main__":
    unittest.main()
