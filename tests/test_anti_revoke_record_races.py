from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_helper_tools import anti_revoke_service as anti_revoke
from astrbot_plugin_helper_tools.anti_revoke_service import (
    AntiRevokeService,
    RevokeRecord,
    unwrap_onebot_payload,
)
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.tests.test_anti_revoke_service import (
    TINY_PNG_BASE64,
    FakeBot,
    FakeEvent,
    PreDeleteCaptureBot,
)

WAIT_SECONDS = 5


class AntiRevokeRecordRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_compat_caches()
        self.addCleanup(reset_compat_caches)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = AntiRevokeService(
            {"anti_revoke": {"enabled": True, "target_groups": ["999"]}},
            temporary.name,
        )
        self.addAsyncCleanup(self.service.stop)
        self.png_bytes = base64.b64decode(TINY_PNG_BASE64)
        self.record_path = self.service.cache_dir / "cache_123_456.json"

    @staticmethod
    def _payload(message_id: int, image_reference: str = "") -> dict:
        message = [{"type": "text", "data": {"text": "original message"}}]
        if image_reference:
            message.append({"type": "image", "data": {"file": image_reference}})
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "message_id": message_id,
            "user_id": 10001,
            "time": int(time.time()),
            "message": message,
        }

    def _persisted_index(self) -> dict[str, str]:
        return json.loads(self.record_path.read_text(encoding="utf-8"))["image_cache_files"]

    def _assert_png_snapshot(self, index: dict[str, str]) -> None:
        self.assertEqual(set(index), {"1"})
        self.assertEqual((self.service.cache_dir / index["1"]).read_bytes(), self.png_bytes)

    async def _drain(self, tasks: list[asyncio.Task]) -> None:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=WAIT_SECONDS
        )

    async def test_duplicate_capture_preserves_pre_delete_image_index(self) -> None:
        """A delayed original event must retain the preceding pre-delete snapshot."""
        bot = PreDeleteCaptureBot()
        download_started = asyncio.Event()
        release_download = asyncio.Event()
        duplicate_started = asyncio.Event()
        real_read = self.service._read_image_segment
        lookups = 0

        async def lookup(_event, message_id):
            nonlocal lookups
            lookups += 1
            if lookups == 1:
                return unwrap_onebot_payload(await bot.get_msg(message_id=int(message_id)))
            return {}

        async def read(event, segment, **kwargs):
            if not download_started.is_set():
                download_started.set()
                await release_download.wait()
            return await real_read(event, segment, **kwargs)

        async def duplicate_capture():
            duplicate_started.set()
            await self.service.handle_event(
                FakeEvent(self._payload(456, "opaque-original-image-id"), bot)
            )

        tasks = []
        with (
            patch.object(self.service, "_lookup_onebot_message", side_effect=lookup),
            patch.object(self.service, "_read_image_segment", side_effect=read) as reader,
            patch.object(
                self.service,
                "_resolve_image_with_onebot",
                new=AsyncMock(side_effect=ValueError("no alternate reference in this fixture")),
            ),
        ):
            try:
                tasks.append(asyncio.create_task(self.service._capture_before_delete(bot, "456")))
                await asyncio.wait_for(download_started.wait(), timeout=WAIT_SECONDS)
                original = self.service._records["123:456"]
                self.assertEqual(self._persisted_index(), {})
                tasks.append(asyncio.create_task(duplicate_capture()))
                await asyncio.wait_for(duplicate_started.wait(), timeout=WAIT_SECONDS)
                self.assertFalse(tasks[1].done(), "duplicate must queue behind the original capture")

                release_download.set()
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=WAIT_SECONDS)
                expected = dict(original.image_cache_files)
                self._assert_png_snapshot(expected)
                self.assertEqual(
                    (self.service._records["123:456"].image_cache_files, self._persisted_index()),
                    (expected, expected),
                    "a duplicate record must not replace completed image indexes with an empty map",
                )
                self.assertEqual(reader.await_count, 1, "reuse the saved PNG instead of reading again")
            finally:
                release_download.set()
                await self._drain(tasks)

    async def test_cancelled_writer_finishes_before_unlock_and_cannot_erase_recall_index(self) -> None:
        """Pause an already serialized JSON write, cancel its caller, then recall."""
        bot = FakeBot()
        payload = self._payload(456, f"base64://{TINY_PNG_BASE64}")
        event = FakeEvent(payload, bot)
        record = RevokeRecord(
            group_id="123",
            message_id="456",
            sender_id="10001",
            sender_name="sender",
            timestamp=payload["time"],
            message=payload["message"],
            cached_at=time.time(),
        )
        loop = asyncio.get_running_loop()
        writer_started = asyncio.Event()
        writer_finished = asyncio.Event()
        release_writer = threading.Event()
        write_completed = threading.Event()
        probe_started = asyncio.Event()
        first_write_guard = threading.Lock()
        first_write_claimed = False
        real_atomic_write = anti_revoke._write_bytes_atomic

        def atomic_write(path: Path, data: bytes) -> None:
            nonlocal first_write_claimed
            with first_write_guard:
                block_this_write = path == self.record_path and not first_write_claimed
                if block_this_write:
                    first_write_claimed = True
            if not block_this_write:
                real_atomic_write(path, data)
                return
            # The empty index is already serialized. Releasing this writer late
            # reproduces a stale replacement even if the live record changes.
            loop.call_soon_threadsafe(writer_started.set)
            try:
                if not release_writer.wait(timeout=WAIT_SECONDS * 3):
                    raise RuntimeError("test did not release the initial JSON writer")
                real_atomic_write(path, data)
            finally:
                write_completed.set()
                loop.call_soon_threadsafe(writer_finished.set)

        tasks = []
        with patch.object(anti_revoke, "_write_bytes_atomic", new=atomic_write):
            try:
                capture = asyncio.create_task(
                    self.service._cache_record_images(event, record, store_record=True)
                )
                tasks.append(capture)
                await asyncio.wait_for(writer_started.wait(), timeout=WAIT_SECONDS)
                lock = self.service._image_locks["123:456"]
                capture.cancel()

                async def probe_lock():
                    probe_started.set()
                    async with lock:
                        return write_completed.is_set()

                probe = asyncio.create_task(probe_lock())
                tasks.append(probe)
                await asyncio.wait_for(probe_started.wait(), timeout=WAIT_SECONDS)
                cancellation_waited = not capture.done()
                recall = asyncio.create_task(
                    self.service._cache_record_images(event, record, phase="recall")
                )
                tasks.append(recall)

                if probe.done() or capture.done():
                    # On the buggy path commit the new index first, then let
                    # the old empty-index writer replace it deterministically.
                    await asyncio.wait_for(asyncio.shield(probe), timeout=WAIT_SECONDS)
                    await asyncio.wait_for(asyncio.shield(recall), timeout=WAIT_SECONDS)
                release_writer.set()
                await asyncio.wait_for(writer_finished.wait(), timeout=WAIT_SECONDS)
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(capture), timeout=WAIT_SECONDS)
                lock_waited = await asyncio.wait_for(asyncio.shield(probe), timeout=WAIT_SECONDS)
                await asyncio.wait_for(asyncio.shield(recall), timeout=WAIT_SECONDS)

                expected = dict(record.image_cache_files)
                self._assert_png_snapshot(expected)
                self.assertEqual(
                    {
                        "cancelled_caller_waited_for_writer": cancellation_waited,
                        "lock_acquired_after_write_completed": lock_waited,
                        "persisted_image_index": self._persisted_index(),
                    },
                    {
                        "cancelled_caller_waited_for_writer": True,
                        "lock_acquired_after_write_completed": True,
                        "persisted_image_index": expected,
                    },
                )
            finally:
                release_writer.set()
                if first_write_claimed:
                    await asyncio.wait_for(writer_finished.wait(), timeout=WAIT_SECONDS)
                await self._drain(tasks)

    async def test_recall_after_inflight_lru_eviction_reuses_original_download(self) -> None:
        """A stale disk-loaded record must yield to the original in-flight record."""
        bot = FakeBot()
        image_url = "https://example.invalid/anti-revoke-original.png"
        download_started = asyncio.Event()
        release_download = asyncio.Event()
        stale_record_loaded = asyncio.Event()
        stale_records = []
        attempts = 0
        real_load = self.service._load_record

        async def fetch(url, **_kwargs):
            nonlocal attempts
            self.assertEqual(url, image_url)
            attempts += 1
            if attempts != 1:
                raise OSError("fixture URL is unavailable after its first download")
            download_started.set()
            await release_download.wait()
            return self.png_bytes, "image/png"

        async def load(group_id, message_id):
            loaded = await real_load(group_id, message_id)
            if (group_id, message_id) == ("123", "456") and not stale_record_loaded.is_set():
                stale_records.append(loaded)
                stale_record_loaded.set()
            return loaded

        recall_payload = {
            "post_type": "notice",
            "notice_type": "group_recall",
            "group_id": 123,
            "message_id": 456,
            "user_id": 10001,
            "operator_id": 10001,
        }
        tasks = []
        with (
            patch.object(self.service, "max_cached_messages", return_value=1),
            patch.object(anti_revoke, "fetch_bytes", side_effect=fetch) as downloader,
            patch.object(self.service, "_lookup_onebot_message", new=AsyncMock(return_value={})),
        ):
            try:
                tasks.append(asyncio.create_task(
                    self.service.handle_event(FakeEvent(self._payload(456, image_url), bot))
                ))
                await asyncio.wait_for(download_started.wait(), timeout=WAIT_SECONDS)
                original = self.service._records["123:456"]
                self.assertEqual(self._persisted_index(), {})

                await asyncio.wait_for(
                    self.service.handle_event(FakeEvent(self._payload(457), bot)),
                    timeout=WAIT_SECONDS,
                )
                self.assertNotIn("123:456", self.service._records)
                self.assertEqual(self._persisted_index(), {})

                with patch.object(self.service, "_load_record", side_effect=load):
                    tasks.append(asyncio.create_task(
                        self.service.handle_event(FakeEvent(recall_payload, bot))
                    ))
                    await asyncio.wait_for(stale_record_loaded.wait(), timeout=WAIT_SECONDS)
                    stale = stale_records[0]
                    self.assertIsNotNone(stale)
                    self.assertIsNot(stale, original, "recall must actually load a second object")
                    self.assertEqual(stale.image_cache_files, {})
                    self.assertEqual(bot.sent, [], "recall must wait for the in-flight capture")
                    self.assertFalse(tasks[1].done())
                    release_download.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=WAIT_SECONDS)

                expected = dict(original.image_cache_files)
                self._assert_png_snapshot(expected)
                self.assertEqual(stale.image_cache_files, expected)
                self.assertEqual(self._persisted_index(), expected)
                self.assertEqual(downloader.await_count, 1, "the expired URL must never be read again")
                self.assertEqual(len(bot.sent), 1)
                kind, notification = bot.sent[0]
                self.assertEqual(kind, "group")
                self.assertEqual(notification["group_id"], 999)
                images = [part for part in notification["message"] if part["type"] == "image"]
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0]["data"]["file"], f"base64://{TINY_PNG_BASE64}")
            finally:
                release_download.set()
                await self._drain(tasks)


if __name__ == "__main__":
    unittest.main()
