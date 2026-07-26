from __future__ import annotations

import asyncio
import mimetypes
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .bilibili_types import BilibiliError, DownloadedMedia, VideoInfo
from .helper_utils import (
    cfg,
    clean_text,
    extract_file_config_value,
    read_int,
    resolve_existing_path,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class _YtDlpLogger:
    def debug(self, _message: str) -> None:
        return

    def info(self, _message: str) -> None:
        return

    def warning(self, message: str) -> None:
        logger.debug("[HelperTools/Bilibili] yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.warning("[HelperTools/Bilibili] yt-dlp: %s", message)


class BilibiliDownloader:
    """Download one Bilibili page with bounded yt-dlp jobs and cleanup."""

    def __init__(
        self,
        config: Any,
        data_dir: Path,
        *,
        cookie_header_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.temp_root = data_dir / "bilibili_video" / "temp"
        self._cookie_header_provider = cookie_header_provider
        self._reapers: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await asyncio.to_thread(self.temp_root.mkdir, parents=True, exist_ok=True)

    async def close(self) -> None:
        if self._reapers:
            await asyncio.gather(*tuple(self._reapers), return_exceptions=True)
        await asyncio.to_thread(shutil.rmtree, self.temp_root, True)

    async def download_video(self, info: VideoInfo) -> DownloadedMedia:
        return await self._run_download("video", info)

    async def download_audio(self, info: VideoInfo) -> DownloadedMedia:
        return await self._run_download("audio", info)

    async def _run_download(self, kind: str, info: VideoInfo) -> DownloadedMedia:
        cancel_event = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(self._download_sync, kind, info, cancel_event)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel_event.set()
            reaper = asyncio.create_task(self._cleanup_late_result(task))
            self._reapers.add(reaper)
            reaper.add_done_callback(self._reapers.discard)
            raise

    @staticmethod
    async def _cleanup_late_result(
        task: asyncio.Task[DownloadedMedia],
    ) -> None:
        try:
            media = await task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - cleanup task must never escape
            return
        media.cleanup()

    def _download_sync(
        self,
        kind: str,
        info: VideoInfo,
        cancel_event: threading.Event,
    ) -> DownloadedMedia:
        try:
            import yt_dlp
        except ImportError as exc:
            raise BilibiliError(
                "yt-dlp is not installed",
                user_message="缺少 yt-dlp 依赖，请在 AstrBot 插件管理页重新安装或更新本插件。",
            ) from exc

        self.temp_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="job_", dir=self.temp_root))
        max_bytes = self._max_file_bytes()
        size_exceeded = False

        def progress_hook(status: dict[str, Any]) -> None:
            nonlocal size_exceeded
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadError("helper-tools download cancelled")
            downloaded = int(status.get("downloaded_bytes") or 0)
            if downloaded > max_bytes:
                size_exceeded = True
                raise yt_dlp.utils.DownloadError("helper-tools size limit exceeded")

        try:
            cookiefile = self._cookiefile(work_dir)
            options: dict[str, Any] = {
                "outtmpl": str(work_dir / "media.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "logger": _YtDlpLogger(),
                "progress_hooks": [progress_hook],
                "socket_timeout": self._request_timeout(),
                "retries": 2,
                "fragment_retries": 2,
                "concurrent_fragment_downloads": 2,
                "max_filesize": max_bytes,
                "overwrites": True,
                "http_headers": {
                    "User-Agent": _USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                },
            }
            if cookiefile:
                options["cookiefile"] = str(cookiefile)
            ffmpeg_path = self._ffmpeg_path()
            if ffmpeg_path:
                options["ffmpeg_location"] = ffmpeg_path

            if kind == "audio":
                options.update(
                    {
                        "format": "bestaudio/best",
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": "96",
                            }
                        ],
                    }
                )
            else:
                height = self._video_height()
                options.update(
                    {
                        "format": (
                            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                            f"bestvideo[height<={height}]+bestaudio/"
                            f"best[height<={height}]/worst"
                        ),
                        "merge_output_format": "mp4",
                    }
                )

            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.extract_info(info.canonical_url, download=True)

            output = self._find_output(work_dir, kind)
            if output is None:
                raise BilibiliError(
                    "yt-dlp produced no media file",
                    user_message="B 站视频下载完成后没有找到可分析的媒体文件。",
                )
            actual_size = output.stat().st_size
            if actual_size <= 0:
                raise BilibiliError(
                    "downloaded media is empty",
                    user_message="下载到的 B 站媒体文件为空。",
                )
            if actual_size > max_bytes:
                size_exceeded = True
                raise BilibiliError(
                    "downloaded media exceeds configured size limit",
                    user_message=self._size_limit_message(),
                )
            mime_type = mimetypes.guess_type(output.name)[0] or (
                "audio/mpeg" if kind == "audio" else "video/mp4"
            )
            return DownloadedMedia(output, work_dir, mime_type)
        except BilibiliError:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            if size_exceeded or "size limit" in str(exc).lower():
                raise BilibiliError(
                    "yt-dlp size limit exceeded",
                    user_message=self._size_limit_message(),
                ) from exc
            if cancel_event.is_set():
                raise BilibiliError(
                    "yt-dlp job cancelled",
                    user_message="B 站视频处理已取消或超时。",
                ) from exc
            logger.warning(
                "[HelperTools/Bilibili] %s download failed for %s: %r",
                kind,
                info.cache_key,
                exc,
            )
            raise BilibiliError(
                f"yt-dlp {kind} download failed: {exc}",
                user_message=(
                    "B 站媒体下载失败。视频可能需要登录、地区不可用、已失效，"
                    "或当前网络无法访问 B 站。"
                ),
            ) from exc

    def _cookiefile(self, work_dir: Path) -> Path | None:
        raw_cookie = self._effective_cookie_header()
        pairs: list[tuple[str, str]] = []
        for token in raw_cookie.split(";"):
            name, separator, value = token.strip().partition("=")
            if separator and name.strip() and value.strip():
                pairs.append((name.strip(), value.strip()))
        if not pairs:
            module = cfg(self.config, "bilibili_video", "cookies_file", [])
            raw_path = extract_file_config_value(module)
            existing = resolve_existing_path(raw_path, self.temp_root.parent)
            return existing if existing and existing.is_file() else None

        target = work_dir / "cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        lines.extend(
            f".bilibili.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}"
            for name, value in pairs
        )
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def _effective_cookie_header(self) -> str:
        if self._cookie_header_provider is not None:
            return clean_text(self._cookie_header_provider())
        return clean_text(cfg(self.config, "bilibili_video", "cookie", ""))

    @staticmethod
    def _find_output(work_dir: Path, kind: str) -> Path | None:
        preferred_suffixes = (
            (".mp3", ".m4a", ".aac", ".opus", ".webm")
            if kind == "audio"
            else (".mp4", ".mkv", ".webm", ".mov")
        )
        files = [
            path
            for path in work_dir.iterdir()
            if path.is_file()
            and path.name != "cookies.txt"
            and path.suffix.lower() not in {".part", ".ytdl"}
        ]
        for suffix in preferred_suffixes:
            match = next(
                (path for path in files if path.suffix.lower() == suffix), None
            )
            if match:
                return match
        return max(files, key=lambda path: path.stat().st_size, default=None)

    @staticmethod
    def _ffmpeg_path() -> str:
        try:
            import imageio_ffmpeg

            return clean_text(imageio_ffmpeg.get_ffmpeg_exe())
        except (ImportError, OSError, RuntimeError):
            return ""

    def _request_timeout(self) -> int:
        return read_int(
            cfg(self.config, "bilibili_video", "request_timeout_seconds", 20),
            20,
            minimum=5,
            maximum=120,
        )

    def _max_file_bytes(self) -> int:
        size_mb = read_int(
            cfg(self.config, "bilibili_video", "max_file_size_mb", 80),
            80,
            minimum=5,
            maximum=2048,
        )
        return size_mb * 1024 * 1024

    def _size_limit_message(self) -> str:
        size_mb = self._max_file_bytes() // (1024 * 1024)
        return f"B 站媒体文件超过配置的 {size_mb} MB 大小限制。"

    def _video_height(self) -> int:
        quality = clean_text(
            cfg(self.config, "bilibili_video", "download_quality", "360p")
        ).lower()
        try:
            return max(144, min(1080, int(quality.rstrip("p"))))
        except ValueError:
            return 360
