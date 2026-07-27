from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .bilibili_types import BilibiliError, DownloadedMedia, VideoFrame, VideoInfo
from .helper_utils import cfg, read_bool, read_int

_FFMPEG_DURATION_RE = re.compile(
    r"Duration:\s*(?P<hours>\d+):(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)"
)


class BilibiliFrameExtractor:
    """Extract evenly distributed, size-bounded JPEG frames with ffmpeg."""

    def __init__(self, config: Any) -> None:
        self.config = config

    def enabled(self) -> bool:
        return read_bool(self._setting("enabled", False), False)

    async def extract(
        self,
        media: DownloadedMedia,
        info: VideoInfo,
    ) -> tuple[VideoFrame, ...]:
        if not self.enabled():
            return ()

        ffmpeg = self._ffmpeg_executable()
        frame_dir = media.work_dir / "frames"
        await asyncio.to_thread(frame_dir.mkdir, parents=True, exist_ok=True)

        sampling_duration = await self._sampling_duration(ffmpeg, media.file_path, info)
        max_total_bytes = self.max_total_size_mb() * 1024 * 1024
        frames: list[VideoFrame] = []
        total_size = 0
        failures: list[str] = []
        for index, timestamp in enumerate(
            evenly_sample_timestamps(sampling_duration, self.frame_count()),
            start=1,
        ):
            output_path = frame_dir / f"frame_{index:02d}.jpg"
            data: bytes | None = None
            actual_timestamp: float | None = None
            last_error: Exception | None = None
            size_limit_reached = False
            for candidate_timestamp in _frame_timestamp_attempts(timestamp):
                try:
                    await self._extract_one(
                        ffmpeg=ffmpeg,
                        input_path=media.file_path,
                        output_path=output_path,
                        timestamp=candidate_timestamp,
                    )
                    size = output_path.stat().st_size
                    if size <= 0:
                        raise BilibiliError("ffmpeg produced an empty frame")
                    if total_size + size > max_total_bytes:
                        output_path.unlink(missing_ok=True)
                        size_limit_reached = True
                        break
                    data = await asyncio.to_thread(output_path.read_bytes)
                    actual_timestamp = candidate_timestamp
                    break
                except (BilibiliError, OSError) as exc:
                    last_error = exc
                    output_path.unlink(missing_ok=True)

            if size_limit_reached:
                break
            if data is None or actual_timestamp is None:
                detail = str(last_error or "ffmpeg produced no usable frame")
                failures.append(detail)
                if frames and isinstance(last_error, FileNotFoundError):
                    logger.info(
                        "[HelperTools/Bilibili] stopped tail frame extraction for %s at %.3fs; "
                        "ffmpeg produced no output file",
                        info.cache_key,
                        timestamp,
                    )
                    break
                logger.warning(
                    "[HelperTools/Bilibili] frame %d extraction failed for %s: %s",
                    index,
                    info.cache_key,
                    detail,
                )
                continue

            total_size += len(data)
            frames.append(
                VideoFrame(index=index, timestamp=actual_timestamp, data=data)
            )

        if frames:
            return tuple(frames)
        detail = failures[-1] if failures else "all frames exceeded the size limit"
        raise BilibiliError(
            f"ffmpeg produced no usable frames: {detail}",
            user_message="没有提取到可供视觉模型识别的视频画面。",
        )

    async def _extract_one(
        self,
        *,
        ffmpeg: str,
        input_path: Path,
        output_path: Path,
        timestamp: float,
    ) -> None:
        filter_value = f"scale='min({self.max_frame_width()},iw)':-2"
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(input_path),
                "-an",
                "-frames:v",
                "1",
                "-vf",
                filter_value,
                "-q:v",
                str(self.jpeg_quality()),
                "-y",
                str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BilibiliError(
                "failed to start ffmpeg",
                user_message="无法启动 ffmpeg，无法抽取视频画面。",
            ) from exc

        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._frame_timeout_seconds(),
            )
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise
        except asyncio.TimeoutError as exc:
            await _terminate_process(process)
            raise BilibiliError(
                "ffmpeg frame extraction timed out",
                user_message="抽取视频画面超时。",
            ) from exc

        if process.returncode != 0:
            details = stderr.decode("utf-8", errors="replace")[-600:].strip()
            raise BilibiliError(f"ffmpeg failed: {details or process.returncode}")

    async def _sampling_duration(
        self,
        ffmpeg: str,
        input_path: Path,
        info: VideoInfo,
    ) -> float:
        metadata_duration = max(0.0, float(info.duration or 0))
        probed_duration = await self._probe_media_duration(ffmpeg, input_path)
        if probed_duration is None:
            return metadata_duration
        if metadata_duration and abs(probed_duration - metadata_duration) >= max(
            1.0,
            metadata_duration * 0.03,
        ):
            logger.info(
                "[HelperTools/Bilibili] using downloaded media duration %.3fs instead of "
                "metadata duration %.3fs for %s frame sampling",
                probed_duration,
                metadata_duration,
                info.cache_key,
            )
        return probed_duration

    async def _probe_media_duration(self, ffmpeg: str, input_path: Path) -> float | None:
        """Ask ffmpeg for container metadata without decoding the whole media file."""

        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-i",
                str(input_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.debug("[HelperTools/Bilibili] could not probe media duration: %r", exc)
            return None

        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=min(15, self._frame_timeout_seconds()),
            )
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise
        except asyncio.TimeoutError:
            await _terminate_process(process)
            logger.debug("[HelperTools/Bilibili] media duration probe timed out")
            return None

        return parse_ffmpeg_duration(stderr.decode("utf-8", errors="replace"))

    def frame_count(self) -> int:
        return read_int(self._setting("frame_count", 6), 6, minimum=1, maximum=24)

    def max_frame_width(self) -> int:
        return read_int(
            self._setting("max_frame_width", 960),
            960,
            minimum=320,
            maximum=1920,
        )

    def jpeg_quality(self) -> int:
        return read_int(
            self._setting("jpeg_quality", 5),
            5,
            minimum=2,
            maximum=31,
        )

    def max_total_size_mb(self) -> int:
        return read_int(
            self._setting("max_total_size_mb", 8),
            8,
            minimum=1,
            maximum=50,
        )

    def _frame_timeout_seconds(self) -> int:
        request_timeout = read_int(
            cfg(self.config, "bilibili_video", "request_timeout_seconds", 20),
            20,
            minimum=5,
            maximum=120,
        )
        return max(15, request_timeout)

    def _setting(self, key: str, default: Any) -> Any:
        default_model = cfg(self.config, "bilibili_video", "default_model", {})
        if not isinstance(default_model, dict):
            return default
        settings = default_model.get("frame_vision", {})
        if not isinstance(settings, dict):
            return default
        return settings.get(key, default)

    @staticmethod
    def _ffmpeg_executable() -> str:
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, OSError, RuntimeError) as exc:
            raise BilibiliError(
                "imageio-ffmpeg is not available",
                user_message="缺少 imageio-ffmpeg，无法抽取视频画面。",
            ) from exc
        if not executable:
            raise BilibiliError(
                "imageio-ffmpeg returned no executable",
                user_message="没有找到可用的 ffmpeg，无法抽取视频画面。",
            )
        return executable


def evenly_sample_timestamps(duration: float, count: int) -> tuple[float, ...]:
    total = max(0.0, float(duration or 0))
    frame_count = max(1, int(count or 1))
    if total <= 0:
        return (0.0,)
    if frame_count == 1:
        return (round(total / 2, 3),)

    start = min(1.0, total * 0.01)
    # Bilibili metadata may round a video slightly past its final decodable frame.
    # Keep the last sample safely before the reported end, then retry even earlier if needed.
    tail_padding = min(total / 2, max(0.5, min(1.0, total * 0.01)))
    end = max(start, total - tail_padding)
    step = (end - start) / (frame_count - 1)
    timestamps: list[float] = []
    for index in range(frame_count):
        timestamp = round(start + step * index, 3)
        if not timestamps or timestamp > timestamps[-1]:
            timestamps.append(timestamp)
    return tuple(timestamps) or (0.0,)


def parse_ffmpeg_duration(value: str) -> float | None:
    match = _FFMPEG_DURATION_RE.search(value)
    if match is None:
        return None
    try:
        duration = (
            int(match.group("hours")) * 3600
            + int(match.group("minutes")) * 60
            + float(match.group("seconds"))
        )
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _frame_timestamp_attempts(timestamp: float) -> tuple[float, ...]:
    primary = max(0.0, round(timestamp, 3))
    fallback = max(0.0, round(primary - 0.5, 3))
    if fallback < primary:
        return primary, fallback
    return (primary,)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.communicate()
    except (ProcessLookupError, asyncio.CancelledError):
        pass
