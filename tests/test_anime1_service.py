from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from astrbot_plugin_helper_tools.anime1_service import Anime1Service


class Anime1ServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, root: Path) -> Anime1Service:
        return Anime1Service({}, root / "data")

    async def test_cache_roundtrip_is_awaitable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            self.assertTrue(inspect.iscoroutinefunction(service.load_cache))
            self.assertTrue(inspect.iscoroutinefunction(service.save_cache))
            self.assertEqual(await service.load_cache(), [])

            await service.save_cache([{"id": "1", "title": "Demo"}])

            self.assertEqual(await service.load_cache(), [{"id": "1", "title": "Demo"}])

    async def test_update_cache_writes_timezone_aware_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))

            async def fake_remote() -> list[object]:
                return [["42", "Some Anime", "连载中", "2026", "夏"]]

            service.fetch_remote_list = fake_remote  # type: ignore[method-assign]
            count = await service.update_cache()

            self.assertEqual(count, 1)
            entries = json.loads(service.cache_path.read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(entries[0]["updated_at"])
            self.assertIsNotNone(updated.tzinfo)

    async def test_filter_entries_accepts_naive_and_aware_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            now = datetime.now().astimezone()
            entries = [
                {"id": "1", "title": "Legacy naive", "updated_at": now.replace(tzinfo=None).isoformat(timespec="seconds")},
                {"id": "2", "title": "New aware", "updated_at": now.isoformat(timespec="seconds")},
                {"id": "3", "title": "Old entry", "updated_at": (now - timedelta(days=30)).isoformat(timespec="seconds")},
            ]

            recent = service.filter_entries(entries, time_range="week")

            self.assertEqual([item["id"] for item in recent], ["1", "2"])

    async def test_get_updates_reads_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            await service.save_cache([{"id": "7", "title": "Cached Anime", "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}])

            result = await service.get_updates()

            self.assertIn("Cached Anime", result)


if __name__ == "__main__":
    unittest.main()
