from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from .bilibili_downloader import BilibiliDownloader
from .bilibili_types import (
    BilibiliError,
    TranscriptResult,
    TranscriptSegment,
    VideoInfo,
    format_duration,
    read_bounded_response,
)
from .helper_utils import cfg, clean_text, read_bool, read_int, read_list

_PLAYER_ENDPOINT = "https://api.bilibili.com/x/player/v2"
_BCUT_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
_BCUT_HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}
_SUBTITLE_HOST_SUFFIXES = ("bilibili.com", "hdslb.com", "bilivideo.com")
_BCUT_UPLOAD_HOST_SUFFIXES = ("biliapi.net", "bilivideo.com", "hdslb.com")


class BilibiliTranscriptService:
    """Obtain timestamped speech text without asking a second chat model."""

    def __init__(self, config: Any, downloader: BilibiliDownloader) -> None:
        self.config = config
        self.downloader = downloader

    async def fetch(
        self,
        info: VideoInfo,
        session: aiohttp.ClientSession,
        request_headers: dict[str, str],
    ) -> TranscriptResult:
        prefer_subtitle = read_bool(self._setting("prefer_subtitles", True), True)
        subtitle_error: Exception | None = None
        if prefer_subtitle:
            try:
                subtitle = await self._fetch_official_subtitle(
                    info,
                    session,
                    request_headers,
                )
                if subtitle and subtitle.has_content:
                    return subtitle
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                BilibiliError,
                TypeError,
                ValueError,
            ) as exc:
                subtitle_error = exc
                logger.warning(
                    "[HelperTools/Bilibili] subtitle lookup failed for %s: %r",
                    info.cache_key,
                    exc,
                )

        if not read_bool(self._setting("bcut_fallback_enabled", True), True):
            detail = "官方字幕不可用，且必剪语音转写回退已关闭。"
            if subtitle_error:
                detail = "官方字幕读取失败，且必剪语音转写回退已关闭。"
            raise BilibiliError("no transcript source enabled", user_message=detail)

        media = await self.downloader.download_audio(info)
        try:
            transcript = await self._transcribe_with_bcut(media.file_path, session)
        finally:
            media.cleanup()
        if not transcript.has_content:
            raise BilibiliError(
                "BCut returned an empty transcript",
                user_message="该视频没有可用字幕，必剪语音转写也没有识别出有效内容。",
            )
        return transcript

    def render_for_model(self, transcript: TranscriptResult) -> str:
        max_chars = read_int(
            self._setting("max_transcript_chars", 30000),
            30000,
            minimum=1000,
            maximum=200000,
        )
        lines = [
            f"[{format_duration(segment.start)}] {self._clean_segment(segment.text)}"
            for segment in transcript.segments
            if self._clean_segment(segment.text)
        ]
        return _sample_timeline(lines, max_chars)

    async def _fetch_official_subtitle(
        self,
        info: VideoInfo,
        session: aiohttp.ClientSession,
        request_headers: dict[str, str],
    ) -> TranscriptResult | None:
        timeout = self._request_timeout()
        async with session.get(
            _PLAYER_ENDPOINT,
            params={"bvid": info.bvid, "cid": str(info.cid)},
            headers=request_headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            payload = await _bounded_json(response, max_bytes=4 * 1024 * 1024)
        if _safe_int(payload.get("code", -1)) != 0:
            raise BilibiliError(
                f"player API rejected subtitle request: {payload.get('message', '')}"
            )

        player_data = payload.get("data") or {}
        if not isinstance(player_data, dict):
            raise BilibiliError("player API returned invalid data")
        subtitle_root = player_data.get("subtitle") or {}
        if not isinstance(subtitle_root, dict):
            return None
        raw_candidates = subtitle_root.get("subtitles") or []
        candidates = (
            [item for item in raw_candidates if isinstance(item, dict)]
            if isinstance(raw_candidates, list)
            else []
        )
        if not candidates:
            return None
        candidate: dict[str, Any] | None = None
        subtitle_url = ""
        rejected_origins: list[str] = []
        for possible_candidate in self._ordered_subtitles(candidates):
            raw_url = clean_text(possible_candidate.get("subtitle_url"))
            if not raw_url:
                continue
            try:
                subtitle_url = _secure_subtitle_url(raw_url)
            except BilibiliError:
                rejected_origins.append(_subtitle_url_origin(raw_url))
                continue
            candidate = possible_candidate
            break
        if candidate is None:
            if rejected_origins:
                origins = ", ".join(sorted(set(rejected_origins))[:3])
                raise BilibiliError(
                    "subtitle API returned no trusted Bilibili CDN URL "
                    f"({origins})"
                )
            return None

        # Subtitle links are signed CDN URLs. Login cookies are only needed for
        # the player API and must not be forwarded to the CDN request.
        subtitle_headers = {
            name: value
            for name, value in request_headers.items()
            if name.casefold() != "cookie"
        }

        async with session.get(
            subtitle_url,
            headers=subtitle_headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            subtitle_payload = await _bounded_json(response, max_bytes=8 * 1024 * 1024)
        body = subtitle_payload.get("body") or []
        segments: list[TranscriptSegment] = []
        for item in body if isinstance(body, list) else []:
            if not isinstance(item, dict):
                continue
            text = self._clean_segment(item.get("content"))
            if not text:
                continue
            try:
                start = float(item.get("from", 0) or 0)
                end = float(item.get("to", start) or start)
            except (TypeError, ValueError):
                start = end = 0.0
            segments.append(TranscriptSegment(start, end, text))
        if not segments:
            return None

        language = clean_text(candidate.get("lan_doc")) or clean_text(
            candidate.get("lan")
        )
        ai_type = _safe_int(candidate.get("ai_type"))
        source = f"B 站{'AI ' if ai_type else '官方'}字幕（{language or '未知语言'}）"
        return TranscriptResult(source, language, tuple(segments))

    def _ordered_subtitles(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preferred = read_list(
            self._setting(
                "subtitle_languages",
                ["zh-CN", "zh-Hans", "zh-Hant", "zh-HK", "ai-zh", "en-US", "en"],
            ),
            ["zh-CN", "zh-Hans", "zh-Hant", "zh-HK", "ai-zh", "en-US", "en"],
        )
        ranks = {language.casefold(): index for index, language in enumerate(preferred)}

        def score(candidate: dict[str, Any]) -> tuple[int, int]:
            language = clean_text(candidate.get("lan")).casefold()
            language_doc = clean_text(candidate.get("lan_doc")).casefold()
            rank = ranks.get(language, len(ranks) + 2)
            if rank == len(ranks) + 2:
                rank = next(
                    (
                        index
                        for key, index in ranks.items()
                        if key and (key in language or key in language_doc)
                    ),
                    rank,
                )
            return rank, _safe_int(candidate.get("ai_type"))

        return sorted(candidates, key=score)

    async def _transcribe_with_bcut(
        self,
        audio_path: Path,
        session: aiohttp.ClientSession,
    ) -> TranscriptResult:
        try:
            size = audio_path.stat().st_size
        except OSError as exc:
            raise BilibiliError(
                "cannot read downloaded audio",
                user_message="下载后的音频文件无法读取。",
            ) from exc
        if size <= 0:
            raise BilibiliError(
                "downloaded audio is empty", user_message="下载后的音频为空。"
            )

        extension = audio_path.suffix.lower().lstrip(".") or "mp3"
        create = await self._bcut_post(
            session,
            "/resource/create",
            {
                "type": 2,
                "name": f"audio.{extension}",
                "size": size,
                "ResourceFileType": extension,
                "model_id": "8",
            },
        )
        upload_urls = create.get("upload_urls") or []
        per_size = int(create.get("per_size", 0) or 0)
        if not isinstance(upload_urls, list) or not upload_urls or per_size <= 0:
            raise BilibiliError(
                "BCut did not provide upload chunks",
                user_message="必剪语音转写服务没有返回有效的上传地址。",
            )

        etags: list[str] = []
        for index, upload_url in enumerate(upload_urls):
            upload_url = _secure_bcut_upload_url(clean_text(upload_url))
            start = index * per_size
            chunk = await asyncio.to_thread(_read_chunk, audio_path, start, per_size)
            async with session.put(
                upload_url,
                data=chunk,
                headers={"Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=max(30, self._request_timeout())),
            ) as response:
                if response.status < 200 or response.status >= 300:
                    response.release()
                    raise BilibiliError(
                        f"BCut upload returned HTTP {response.status}",
                        user_message="上传音频到必剪语音转写服务时失败。",
                    )
                etags.append(clean_text(response.headers.get("ETag")).strip('"'))

        committed = await self._bcut_post(
            session,
            "/resource/create/complete",
            {
                "InBossKey": create.get("in_boss_key"),
                "ResourceId": create.get("resource_id"),
                "Etags": ",".join(etags),
                "UploadId": create.get("upload_id"),
                "model_id": "8",
            },
        )
        task = await self._bcut_post(
            session,
            "/task",
            {"resource": committed.get("download_url"), "model_id": "8"},
        )
        task_id = clean_text(task.get("task_id"))
        if not task_id:
            raise BilibiliError("BCut did not return a task id")

        result_data = await self._wait_for_bcut(session, task_id)
        result = result_data.get("result") or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError as exc:
                raise BilibiliError("BCut returned invalid transcript JSON") from exc
        if not isinstance(result, dict):
            raise BilibiliError("BCut returned an invalid transcript payload")

        segments: list[TranscriptSegment] = []
        for utterance in result.get("utterances") or []:
            if not isinstance(utterance, dict):
                continue
            text = self._clean_segment(utterance.get("transcript"))
            if not text:
                continue
            try:
                start = float(utterance.get("start_time", 0) or 0) / 1000
                end = float(utterance.get("end_time", 0) or 0) / 1000
            except (TypeError, ValueError):
                start = end = 0.0
            segments.append(TranscriptSegment(start, end, text))
        return TranscriptResult(
            source="必剪语音转写（仅依据视频声音）",
            language=clean_text(result.get("language")) or "zh",
            segments=tuple(segments),
        )

    async def _wait_for_bcut(
        self,
        session: aiohttp.ClientSession,
        task_id: str,
    ) -> dict[str, Any]:
        timeout_seconds = read_int(
            self._setting("bcut_timeout_seconds", 240),
            240,
            minimum=30,
            maximum=1800,
        )
        deadline = time.monotonic() + timeout_seconds
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            async with session.get(
                f"{_BCUT_BASE}/task/result",
                params={"model_id": "7", "task_id": task_id},
                headers=_BCUT_HEADERS,
                timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
            ) as response:
                payload = await _bounded_json(response, max_bytes=4 * 1024 * 1024)
            if _safe_int(payload.get("code", -1)) != 0:
                raise BilibiliError(
                    f"BCut task query failed: {payload.get('message', '')}",
                    user_message="必剪语音转写任务查询失败。",
                )
            data = payload.get("data") or {}
            state = _safe_int(data.get("state"))
            if state == 4:
                return data
            if state == 3:
                raise BilibiliError(
                    "BCut task failed",
                    user_message="必剪语音转写任务执行失败。",
                )
            if attempts % 30 == 0:
                logger.info(
                    "[HelperTools/Bilibili] waiting for BCut transcript (%d seconds)",
                    attempts,
                )
            await asyncio.sleep(1)
        raise BilibiliError(
            "BCut task timed out",
            user_message=f"必剪语音转写超过 {timeout_seconds} 秒仍未完成。",
        )

    async def _bcut_post(
        self,
        session: aiohttp.ClientSession,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        async with session.post(
            f"{_BCUT_BASE}{path}",
            json=body,
            headers=_BCUT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
        ) as response:
            payload = await _bounded_json(response, max_bytes=4 * 1024 * 1024)
        if _safe_int(payload.get("code", -1)) != 0:
            raise BilibiliError(
                f"BCut request failed: {payload.get('message', '')}",
                user_message="必剪语音转写服务拒绝了请求。",
            )
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise BilibiliError("BCut returned invalid data")
        return data

    def _setting(self, key: str, default: Any) -> Any:
        settings = cfg(self.config, "bilibili_video", "default_model", {})
        if not isinstance(settings, dict):
            return default
        return settings.get(key, default)

    def _request_timeout(self) -> int:
        return read_int(
            cfg(self.config, "bilibili_video", "request_timeout_seconds", 20),
            20,
            minimum=5,
            maximum=120,
        )

    @staticmethod
    def _clean_segment(value: Any) -> str:
        return re.sub(r"\s+", " ", clean_text(value)).strip()


async def _bounded_json(
    response: aiohttp.ClientResponse,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    body = await read_bounded_response(response, max_bytes)
    if response.status < 200 or response.status >= 300:
        raise BilibiliError(f"Bilibili API returned HTTP {response.status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BilibiliError("Bilibili API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BilibiliError("Bilibili API returned a non-object response")
    return payload


def _secure_subtitle_url(url: str) -> str:
    url = clean_text(url)
    if url.startswith("//"):
        url = f"https:{url}"
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise BilibiliError("subtitle API returned an invalid URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _SUBTITLE_HOST_SUFFIXES
    ):
        raise BilibiliError(
            "subtitle API returned URL outside the Bilibili CDN allowlist "
            f"(scheme={parsed.scheme or 'none'}, host={host or 'none'})"
        )
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return parsed.geturl()


def _subtitle_url_origin(url: str) -> str:
    url = clean_text(url)
    if url.startswith("//"):
        url = f"https:{url}"
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "invalid"
    scheme = parsed.scheme or "none"
    host = (parsed.hostname or "").lower().rstrip(".") or "none"
    return f"{scheme}://{host}"


def _secure_bcut_upload_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise BilibiliError("BCut returned an invalid upload URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _BCUT_UPLOAD_HOST_SUFFIXES
    ):
        raise BilibiliError("BCut returned an upload URL outside the allowlist")
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return parsed.geturl()


def _read_chunk(path: Path, start: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(size)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _sample_timeline(lines: list[str], max_chars: int) -> str:
    if not lines:
        return ""
    complete = "\n".join(lines)
    if len(complete) <= max_chars:
        return complete

    start_budget = int(max_chars * 0.4)
    middle_budget = int(max_chars * 0.25)
    end_budget = max_chars - start_budget - middle_budget - 120

    selected: set[int] = set()
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) + 1 > start_budget:
            break
        selected.add(index)
        used += len(line) + 1

    used = 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if used + len(line) + 1 > end_budget:
            break
        selected.add(index)
        used += len(line) + 1

    middle_indexes = [index for index in range(len(lines)) if index not in selected]
    if middle_indexes and middle_budget > 0:
        used = 0
        center = len(lines) // 2
        ordered_middle = sorted(
            middle_indexes,
            key=lambda index: (abs(index - center), index),
        )
        for index in ordered_middle:
            line = lines[index]
            if used + len(line) + 1 > middle_budget:
                continue
            selected.add(index)
            used += len(line) + 1

    output: list[str] = []
    previous = -1
    for index in sorted(selected):
        if previous >= 0 and index > previous + 1:
            output.append("[中间部分因上下文长度限制已省略]")
        output.append(lines[index])
        previous = index
    rendered = "\n".join(output)
    return rendered[:max_chars]
