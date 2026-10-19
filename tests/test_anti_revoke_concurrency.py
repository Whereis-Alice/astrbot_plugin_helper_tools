from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_helper_tools.anti_revoke_service import (
    AntiRevokeService,
    _write_bytes_atomic,
)
from astrbot_plugin_helper_tools.tests.test_anti_revoke_service import (
    TINY_PNG_BASE64,
    FakeBot,
    FakeEvent,
)


class AntiRevokeConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def _check_recall_during_capture(self, *, pause_initial_write: bool) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = AntiRevokeService(
                {"anti_revoke": {"enabled": True, "target_groups": ["999"]}},
                temp_dir,
            )
            bot = FakeBot()
            original = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "message": [{"type": "image", "data": {"file": "temporary-image"}}],
            }
            recall = {
                "post_type": "notice",
                "notice_type": "group_recall",
                "group_id": 123,
                "message_id": 456,
                "user_id": 789,
                "operator_id": 789,
            }
            paused = asyncio.Event()
            recall_loaded = asyncio.Event()
            release_download = asyncio.Event()
            release_write = threading.Event()
            loop = asyncio.get_running_loop()
            real_write = service._write_record
            real_load = service._load_record
            writes = 0

            def write(record):
                nonlocal writes
                writes += 1
                if pause_initial_write and writes == 1:
                    loop.call_soon_threadsafe(paused.set)
                    if not release_write.wait(timeout=5):
                        raise TimeoutError("test did not release initial cache write")
                real_write(record)

            async def read(*_args, **_kwargs):
                if not pause_initial_write:
                    paused.set()
                    await release_download.wait()
                return base64.b64decode(TINY_PNG_BASE64), ".png"

            async def load(*args):
                record = await real_load(*args)
                if paused.is_set():
                    recall_loaded.set()
                return record

            tasks = []
            try:
                with (
                    patch.object(service, "_write_record", side_effect=write),
                    patch.object(service, "_read_image_segment", AsyncMock(side_effect=read)) as reader,
                    patch.object(service, "_load_record", side_effect=load),
                ):
                    tasks.append(asyncio.create_task(service.handle_event(FakeEvent(original, bot))))
                    await asyncio.wait_for(paused.wait(), timeout=3)
                    tasks.append(asyncio.create_task(service.handle_event(FakeEvent(recall, bot))))
                    await asyncio.wait_for(recall_loaded.wait(), timeout=3)
                    await asyncio.sleep(0)
                    self.assertEqual(bot.sent, [], "recall must wait for the ongoing capture")
                    self.assertEqual(reader.await_count, 0 if pause_initial_write else 1)
                    release_write.set()
                    release_download.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
                    self.assertEqual(reader.await_count, 1, "do not download the same image twice")
                self.assertEqual(len(bot.sent), 1)
                sent = bot.sent[0][1]["message"]
                self.assertEqual(sent[1]["data"]["file"], f"base64://{TINY_PNG_BASE64}")
                record_file = service.cache_dir / "cache_123_456.json"
                persisted = json.loads(record_file.read_text(encoding="utf-8"))
                self.assertEqual(set(persisted["image_cache_files"]), {"0"})
                self.assertTrue((service.cache_dir / persisted["image_cache_files"]["0"]).is_file())
                self.assertEqual(len(service._image_locks), 0, "unused message locks must not accumulate")
            finally:
                release_write.set()
                release_download.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await service.stop()

    async def test_recall_waits_for_image_download_and_persisted_snapshot(self) -> None:
        await self._check_recall_during_capture(pause_initial_write=False)

    async def test_recall_during_initial_record_write_waits_for_capture(self) -> None:
        await self._check_recall_during_capture(pause_initial_write=True)

    def test_concurrent_atomic_writers_do_not_share_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "cache.json"
            barrier = threading.Barrier(2)
            replace_lock = threading.Lock()
            original_replace = Path.replace

            def replace(path, destination):
                barrier.wait(timeout=3)
                # Exercise independent temp files, not Windows' unrelated
                # refusal of simultaneous replacements of one directory entry.
                with replace_lock:
                    return original_replace(path, destination)

            payloads = [b'{"writer":1}', b'{"writer":2}']
            with patch.object(Path, "replace", replace), ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(_write_bytes_atomic, target, data) for data in payloads]
                for future in futures:
                    future.result(timeout=5)
            self.assertIn(target.read_bytes(), payloads)
            self.assertEqual(list(Path(temp_dir).iterdir()), [target])

    def test_expired_atomic_write_leftovers_are_cleaned_without_removing_fresh_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = AntiRevokeService({"anti_revoke": {"enabled": True}}, temp_dir)
            now = 1_700_000_000
            expired = [
                service.cache_dir / ".media_123_456_0.png.old.tmp",
                service.cache_dir / ".cache_123_456.json.old.tmp",
            ]
            fresh = service.cache_dir / ".media_123_789_0.png.fresh.tmp"
            for path in [*expired, fresh]:
                path.write_bytes(b"temporary data")
                timestamp = now if path == fresh else now - 301
                os.utime(path, (timestamp, timestamp))
            with patch("astrbot_plugin_helper_tools.anti_revoke_service.time.time", return_value=now):
                service._cleanup_disk()
            self.assertTrue(all(not path.exists() for path in expired))
            self.assertTrue(fresh.is_file())


if __name__ == "__main__":
    unittest.main()
