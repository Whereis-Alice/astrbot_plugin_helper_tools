from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_helper_tools.bilibili_frames import (
    BilibiliFrameExtractor,
    evenly_sample_timestamps,
)
from astrbot_plugin_helper_tools.bilibili_types import DownloadedMedia, VideoInfo


def make_info(duration: int = 3) -> VideoInfo:
    return VideoInfo(
        aid=1,
        bvid="BV1GJ411x7h7",
        cid=2,
        page=1,
        page_count=1,
        part_title="测试分P",
        title="测试视频",
        description="",
        owner_name="测试UP",
        owner_mid="3",
        duration=duration,
        pubdate=0,
        cover_url="",
        category="测试",
    )


class FrameSamplingTests(unittest.TestCase):
    def test_uniform_timestamps_cover_beginning_middle_and_end(self) -> None:
        self.assertEqual(evenly_sample_timestamps(100, 3), (1.0, 50.0, 99.0))
        self.assertEqual(evenly_sample_timestamps(0, 6), (0.0,))
        self.assertEqual(evenly_sample_timestamps(100, 1), (50.0,))

    def test_frame_settings_are_bounded(self) -> None:
        extractor = BilibiliFrameExtractor(
            {
                "bilibili_video": {
                    "default_model": {
                        "frame_vision": {
                            "enabled": "yes",
                            "frame_count": 999,
                            "max_frame_width": 1,
                            "jpeg_quality": 999,
                            "max_total_size_mb": 0,
                        }
                    }
                }
            }
        )

        self.assertTrue(extractor.enabled())
        self.assertEqual(extractor.frame_count(), 24)
        self.assertEqual(extractor.max_frame_width(), 320)
        self.assertEqual(extractor.jpeg_quality(), 31)
        self.assertEqual(extractor.max_total_size_mb(), 1)


class FrameExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_real_jpeg_frames_with_ffmpeg(self) -> None:
        try:
            import imageio_ffmpeg
        except ImportError:
            self.skipTest("imageio-ffmpeg is not installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "sample.mp4"
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x360:rate=5:duration=3",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(video_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await process.communicate()
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace"))

            work_dir = root / "work"
            work_dir.mkdir()
            extractor = BilibiliFrameExtractor(
                {
                    "bilibili_video": {
                        "default_model": {
                            "frame_vision": {
                                "enabled": True,
                                "frame_count": 3,
                                "max_frame_width": 320,
                                "jpeg_quality": 5,
                                "max_total_size_mb": 1,
                            }
                        }
                    }
                }
            )

            frames = await extractor.extract(
                DownloadedMedia(video_path, work_dir, "video/mp4"),
                make_info(),
            )

        self.assertEqual(len(frames), 3)
        self.assertEqual([frame.index for frame in frames], [1, 2, 3])
        self.assertTrue(all(frame.data.startswith(b"\xff\xd8") for frame in frames))
        self.assertTrue(all(len(frame.data) > 100 for frame in frames))
        self.assertTrue(all("data:image/jpeg;base64," in frame.data_url for frame in frames))
