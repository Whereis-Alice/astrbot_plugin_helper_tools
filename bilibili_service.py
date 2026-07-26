from __future__ import annotations

import asyncio
import html
import json
import re
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .bilibili_downloader import BilibiliDownloader
from .bilibili_frames import BilibiliFrameExtractor
from .bilibili_gemini import GeminiVideoAnalyzer
from .bilibili_qr_login import BilibiliCredentialStore
from .bilibili_transcript import BilibiliTranscriptService
from .bilibili_types import (
    BILIBILI_CONTEXT_PREFIX,
    BilibiliError,
    BilibiliVideoContext,
    VideoAnalysis,
    VideoInfo,
    VideoReference,
    read_bounded_response,
)
from .helper_utils import (
    cfg,
    clean_text,
    extract_file_config_value,
    read_bool,
    read_int,
    resolve_existing_path,
)

BILIBILI_TOOL_NAME = "understand_bilibili_video"
_EVENT_REFERENCE_ATTR = "_helper_tools_bilibili_reference"
_VIEW_ENDPOINT = "https://api.bilibili.com/x/web-interface/view"
_NAV_ENDPOINT = "https://api.bilibili.com/x/web-interface/nav"
_SHORT_HOSTS = {"b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn", "bili33.cn"}
_ALLOWED_HOST_SUFFIXES = ("bilibili.com", *_SHORT_HOSTS)
_GENERIC_URL_RE = re.compile(
    r"https?://[^\s<>\"',\]\[\}\{\)\(，。！？；：、（）【】]+",
    re.IGNORECASE,
)
_BVID_RE = re.compile(
    r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])", re.IGNORECASE
)
_AID_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{1,20})(?!\d)", re.IGNORECASE)
_TRAILING_URL_CHARS = "\"'`}>]),，。)、）！!？?；;：:"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class BilibiliCookieVerification:
    status: str
    source: str
    message: str


class BilibiliVideoService:
    """Shared Bilibili pipeline for automatic context and the LLM tool."""

    def __init__(self, config: Any, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir
        self.credentials = BilibiliCredentialStore(data_dir)
        self.downloader = BilibiliDownloader(
            config,
            data_dir,
            cookie_header_provider=self._cookie_header,
        )
        self.transcript = BilibiliTranscriptService(config, self.downloader)
        self.frames = BilibiliFrameExtractor(config)
        self.gemini = GeminiVideoAnalyzer(config)
        self._session: aiohttp.ClientSession | None = None
        self._analysis_cache: OrderedDict[
            str,
            tuple[float, BilibiliVideoContext],
        ] = OrderedDict()
        self._metadata_cache: OrderedDict[str, tuple[float, VideoInfo]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        concurrency = read_int(
            cfg(config, "bilibili_video", "max_concurrency", 1),
            1,
            minimum=1,
            maximum=4,
        )
        self._semaphore = asyncio.Semaphore(concurrency)

    async def start(self) -> None:
        await self.downloader.start()
        await self.verify_cookie()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        await self.downloader.close()
        self._analysis_cache.clear()
        self._metadata_cache.clear()
        self._locks.clear()

    def auto_parse_mode(self) -> str:
        configured = clean_text(
            cfg(
                self.config,
                "bilibili_video",
                "auto_parse_mode",
                "跟随 AstrBot（推荐）",
            )
        ).casefold()
        if "关闭" in configured or configured in {"off", "disabled", "none"}:
            return "off"
        if "主动" in configured or "直接" in configured or configured == "always":
            return "direct"
        return "follow"

    def analysis_mode(self) -> str:
        configured = clean_text(
            cfg(
                self.config,
                "bilibili_video",
                "analysis_mode",
                "AstrBot 默认模型（字幕/转写）",
            )
        ).casefold()
        return "gemini" if "gemini" in configured else "astrbot"

    def prepare_event(self, event: Any) -> VideoReference | None:
        existing = getattr(event, _EVENT_REFERENCE_ATTR, None)
        if isinstance(existing, VideoReference):
            return existing
        reference = extract_event_video_reference(event)
        if reference is not None:
            setattr(event, _EVENT_REFERENCE_ATTR, reference)
        return reference

    async def context_for_event_result(self, event: Any) -> BilibiliVideoContext:
        if self.auto_parse_mode() == "off":
            return BilibiliVideoContext("")
        reference = self.prepare_event(event)
        if reference is None:
            return BilibiliVideoContext("")
        return await self.analyze_reference_safe_result(reference)

    async def context_for_event(self, event: Any) -> str:
        return (await self.context_for_event_result(event)).text

    async def analyze_input_result(
        self,
        value: str,
        *,
        force_refresh: bool = False,
    ) -> BilibiliVideoContext:
        reference = extract_video_reference(value)
        if reference is None:
            return BilibiliVideoContext(
                "[B站视频解析失败]\n"
                "没有识别到 B 站视频。支持链接、BV 号、av 号、b23.tv 短链和包含这些内容的分享文本。"
            )
        return await self.analyze_reference_safe_result(
            reference,
            force_refresh=force_refresh,
        )

    async def analyze_input(self, value: str, *, force_refresh: bool = False) -> str:
        return (
            await self.analyze_input_result(value, force_refresh=force_refresh)
        ).text

    async def analyze_reference_safe_result(
        self,
        reference: VideoReference,
        *,
        force_refresh: bool = False,
    ) -> BilibiliVideoContext:
        try:
            return await self.analyze_reference_result(
                reference,
                force_refresh=force_refresh,
            )
        except BilibiliError as exc:
            logger.warning(
                "[HelperTools/Bilibili] analysis failed for %s: %r",
                reference.lookup_key,
                exc,
            )
            return BilibiliVideoContext(f"[B站视频解析失败]\n原因：{exc.user_message}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep plugin failures out of Agent flow
            logger.exception(
                "[HelperTools/Bilibili] unexpected analysis error for %s",
                reference.lookup_key,
            )
            return BilibiliVideoContext(
                "[B站视频解析失败]\n"
                f"原因：处理视频时发生未预期错误（{type(exc).__name__}）。"
            )

    async def analyze_reference_safe(
        self,
        reference: VideoReference,
        *,
        force_refresh: bool = False,
    ) -> str:
        return (
            await self.analyze_reference_safe_result(
                reference,
                force_refresh=force_refresh,
            )
        ).text

    async def analyze_reference_result(
        self,
        reference: VideoReference,
        *,
        force_refresh: bool = False,
    ) -> BilibiliVideoContext:
        timeout = self._processing_timeout_seconds()
        deadline = time.monotonic() + timeout
        try:
            info = await self._run_until(
                lambda: self._resolve_info(reference, force_refresh=force_refresh),
                deadline,
            )
            max_duration = read_int(
                cfg(self.config, "bilibili_video", "max_duration_seconds", 600),
                600,
                minimum=10,
                maximum=21600,
            )
            if info.duration > max_duration:
                raise BilibiliError(
                    f"video duration {info.duration}s exceeds {max_duration}s",
                    user_message=(
                        f"视频当前分 P 时长为 {info.duration} 秒，超过配置的 "
                        f"{max_duration} 秒限制。"
                    ),
                )

            mode = self.analysis_mode()
            cache_key = f"{mode}:{info.cache_key}"
            if force_refresh:
                self._analysis_cache.pop(cache_key, None)
            cached = self._cached_context(cache_key)
            if cached is not None:
                return await self._attach_optional_frames(
                    info,
                    mode,
                    cached,
                    deadline=deadline,
                )

            lock = self._locks.setdefault(cache_key, asyncio.Lock())
            await self._run_until(lock.acquire, deadline)
            try:
                cached = self._cached_context(cache_key)
                if cached is None or force_refresh:
                    base_context = await self._run_with_processing_slot(
                        lambda: self._analyze_info(info, mode),
                        deadline,
                    )
                    if isinstance(base_context, str):
                        base_context = BilibiliVideoContext(base_context)
                    # Frames are current-turn evidence only. Cache text facts, never image bytes.
                    base_context = BilibiliVideoContext(
                        content=base_context.content,
                        requires_visual_frames=base_context.requires_visual_frames,
                    )
                    self._cache_set(
                        self._analysis_cache,
                        cache_key,
                        base_context,
                    )
                else:
                    base_context = cached
            finally:
                lock.release()

            return await self._attach_optional_frames(
                info,
                mode,
                base_context,
                deadline=deadline,
            )
        except asyncio.TimeoutError as exc:
            raise BilibiliError(
                f"video processing exceeded {timeout}s",
                user_message=f"B 站视频处理超过 {timeout} 秒，任务已取消。",
            ) from exc

    async def analyze_reference(
        self,
        reference: VideoReference,
        *,
        force_refresh: bool = False,
    ) -> str:
        return (
            await self.analyze_reference_result(
                reference,
                force_refresh=force_refresh,
            )
        ).text

    async def _analyze_info(
        self,
        info: VideoInfo,
        mode: str,
    ) -> BilibiliVideoContext:
        session = await self._get_session()
        if mode == "gemini":
            media = await self.downloader.download_video(info)
            try:
                content = await self.gemini.analyze(info, media, session)
            finally:
                media.cleanup()
            analysis = VideoAnalysis(
                info=info,
                mode="Gemini 直接视频分析",
                evidence_source="Gemini 对下载后视频的画面、声音与屏幕文字分析",
                content=content,
            )
            return BilibiliVideoContext(analysis.render())

        try:
            transcript = await self.transcript.fetch(
                info,
                session,
                self._request_headers(),
            )
            content = self.transcript.render_for_model(transcript)
            if not content:
                raise BilibiliError(
                    "transcript renderer returned empty content",
                    user_message="视频字幕或转写结果为空。",
                )
            analysis = VideoAnalysis(
                info=info,
                mode="AstrBot 当前默认模型",
                evidence_source=transcript.source,
                content=content,
            )
            return BilibiliVideoContext(analysis.render())
        except BilibiliError as exc:
            if not self.frames.enabled():
                raise
            analysis = VideoAnalysis(
                info=info,
                mode="AstrBot 当前默认模型",
                evidence_source="视频抽帧视觉资料（字幕/转写不可用）",
                content=(
                    "字幕或语音转写暂时不可用。请结合后续附带的视频画面作答；"
                    "无法从画面确定的声音、对白或连续动作请说明不确定。"
                ),
            )
            logger.warning(
                "[HelperTools/Bilibili] transcript unavailable for %s; falling back to frames: %r",
                info.cache_key,
                exc,
            )
            return BilibiliVideoContext(
                analysis.render(),
                requires_visual_frames=True,
            )

    async def _attach_optional_frames(
        self,
        info: VideoInfo,
        mode: str,
        context: BilibiliVideoContext,
        *,
        deadline: float,
    ) -> BilibiliVideoContext:
        if mode != "astrbot" or not self.frames.enabled():
            return context
        try:
            frames = await self._run_with_processing_slot(
                lambda: self._extract_frames(info),
                deadline,
            )
        except asyncio.TimeoutError:
            error = BilibiliError(
                "frame extraction exceeded the processing timeout",
                user_message="视频抽帧超过整条解析的剩余时间，任务已取消。",
            )
            return self._frame_failure_context(context, error)
        except BilibiliError as exc:
            return self._frame_failure_context(context, exc)

        return BilibiliVideoContext(content=context.content, frames=frames)

    async def _extract_frames(self, info: VideoInfo):
        media = await self.downloader.download_video(info)
        try:
            return await self.frames.extract(media, info)
        finally:
            media.cleanup()

    @staticmethod
    def _frame_failure_context(
        context: BilibiliVideoContext,
        error: BilibiliError,
    ) -> BilibiliVideoContext:
        if context.requires_visual_frames:
            raise error
        return BilibiliVideoContext(
            content=context.content,
            visual_note=(
                f"抽取画面失败（{error.user_message}），本次仅依据字幕或语音转写。"
            ),
        )

    def _cached_context(self, key: str) -> BilibiliVideoContext | None:
        cached = self._cache_get(self._analysis_cache, key)
        if isinstance(cached, BilibiliVideoContext):
            return cached
        if isinstance(cached, str):
            return BilibiliVideoContext(cached)
        return None

    def _processing_timeout_seconds(self) -> int:
        return read_int(
            cfg(self.config, "bilibili_video", "processing_timeout_seconds", 360),
            360,
            minimum=30,
            maximum=3600,
        )

    @staticmethod
    def _remaining_timeout_seconds(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    async def _run_until(
        self,
        operation: Callable[[], Awaitable[_ResultT]],
        deadline: float,
    ) -> _ResultT:
        return await asyncio.wait_for(
            operation(),
            timeout=self._remaining_timeout_seconds(deadline),
        )

    async def _run_with_processing_slot(
        self,
        operation: Callable[[], Awaitable[_ResultT]],
        deadline: float,
    ) -> _ResultT:
        await self._run_until(self._semaphore.acquire, deadline)
        try:
            return await self._run_until(operation, deadline)
        finally:
            self._semaphore.release()

    async def _resolve_info(
        self,
        reference: VideoReference,
        *,
        force_refresh: bool,
    ) -> VideoInfo:
        if not force_refresh:
            cached = self._cache_get(self._metadata_cache, reference.lookup_key)
            if isinstance(cached, VideoInfo):
                return cached

        resolved = reference
        if reference.kind == "short_url":
            final_url = await self._resolve_short_url(reference.value)
            resolved = extract_video_reference(final_url) or VideoReference(
                "invalid",
                final_url,
                reference.part,
                reference.original,
            )
            if resolved.part == 1 and reference.part > 1:
                resolved = VideoReference(
                    resolved.kind,
                    resolved.value,
                    reference.part,
                    reference.original,
                )
        if resolved.kind not in {"bvid", "aid"}:
            raise BilibiliError(
                "resolved URL has no video id",
                user_message="B 站链接跳转成功，但最终地址里没有识别到 BV 号或 av 号。",
            )

        params = (
            {"bvid": resolved.value}
            if resolved.kind == "bvid"
            else {"aid": resolved.value}
        )
        session = await self._get_session()
        async with session.get(
            _VIEW_ENDPOINT,
            params=params,
            headers=self._request_headers(),
            timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
        ) as response:
            payload = await _bounded_json(response, max_bytes=8 * 1024 * 1024)
        if _safe_int(payload.get("code", -1)) != 0:
            message = clean_text(payload.get("message")) or "未知错误"
            raise BilibiliError(
                f"view API rejected video: {message}",
                user_message=f"B 站没有返回该视频的信息：{message}。",
            )
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise BilibiliError("view API returned invalid data")
        info = _video_info_from_api(data, resolved.part)
        self._cache_set(self._metadata_cache, reference.lookup_key, info)
        self._cache_set(self._metadata_cache, info.cache_key, info)
        return info

    async def _resolve_short_url(self, url: str) -> str:
        current = _clean_url(url)
        if not _is_allowed_bilibili_url(current):
            raise BilibiliError("short URL is outside the Bilibili allowlist")
        if _is_short_url_host(current):
            current = _strip_short_url_tracking(current)
        session = await self._get_session()
        for _attempt in range(6):
            headers = (
                self._short_url_headers()
                if _is_short_url_host(current)
                else self._request_headers()
            )
            async with session.get(
                current,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = clean_text(response.headers.get("Location"))
                    response.release()
                    if not location:
                        raise BilibiliError("short URL redirect has no Location header")
                    target = self._short_redirect_target(current, location)
                    target_url = _canonical_video_url(extract_video_reference(target))
                    if target_url:
                        return target_url
                    current = target
                    continue
                if response.status < 200 or response.status >= 300:
                    status = response.status
                    response.release()
                    if status == 400 and _is_short_url_host(current):
                        location = await self._short_url_head_location(session, current)
                        if location:
                            target = self._short_redirect_target(current, location)
                            target_url = _canonical_video_url(
                                extract_video_reference(target)
                            )
                            if target_url:
                                return target_url
                            current = target
                            continue
                    raise BilibiliError(
                        f"short URL returned HTTP {status}",
                        user_message=(
                            f"b23.tv 短链解析失败（HTTP {status}）。"
                            "短链可能已失效，或 B 站短链服务暂时拒绝了本次请求。"
                        ),
                    )
                try:
                    body = await read_bounded_response(response, 512 * 1024)
                except BilibiliError as exc:
                    raise BilibiliError(
                        "short URL response was unexpectedly large",
                        user_message="b23.tv 短链没有跳转到可识别的 B 站视频页面。",
                    ) from exc
                current_reference = extract_video_reference(current)
                current_url = _canonical_video_url(current_reference)
                if current_url:
                    return current_url
                page_text = body.decode("utf-8", errors="ignore")
                try:
                    short_payload = json.loads(page_text)
                except json.JSONDecodeError:
                    short_payload = {}
                if (
                    isinstance(short_payload, dict)
                    and _safe_int(short_payload.get("code")) != 0
                ):
                    raise BilibiliError(
                        f"short URL service returned {short_payload.get('code')}",
                        user_message="这个 b23.tv 短链已失效或不存在。",
                    )
                page_reference = extract_video_reference(page_text)
                page_url = _canonical_video_url(page_reference)
                if page_url:
                    return page_url
                break
        raise BilibiliError(
            "too many or unresolved short URL redirects",
            user_message="b23.tv 短链经过多次跳转后仍无法识别对应视频。",
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    async def verify_cookie(self) -> BilibiliCookieVerification:
        cookie, source = self._cookie_header_and_source()
        if not cookie:
            logger.info(
                "[HelperTools/Bilibili] no Cookie configured; public videos will be read without login"
            )
            return BilibiliCookieVerification(
                status="none",
                source="",
                message="当前没有配置 B 站 Cookie，也没有可用的扫码登录凭据。",
            )

        try:
            session = await self._get_session()
            async with session.get(
                _NAV_ENDPOINT,
                headers=self._request_headers(),
                timeout=aiohttp.ClientTimeout(total=min(10, self._request_timeout())),
            ) as response:
                payload = await _bounded_json(response, max_bytes=512 * 1024)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - verification must not block plugin startup
            logger.warning(
                "[HelperTools/Bilibili] Cookie was loaded from %s but could not be verified: %r",
                source,
                exc,
            )
            return BilibiliCookieVerification(
                status="unknown",
                source=source,
                message=(
                    f"当前使用{source}，但网络或 B 站接口暂时不可用，"
                    "暂时无法确认登录状态。"
                ),
            )

        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            _safe_int(payload.get("code", -1)) == 0
            and isinstance(data, dict)
            and bool(data.get("isLogin"))
        ):
            logger.info(
                "[HelperTools/Bilibili] Cookie verification succeeded (source=%s, logged in)",
                source,
            )
            return BilibiliCookieVerification(
                status="valid",
                source=source,
                message=f"当前使用{source}，B 站确认已登录。",
            )

        logger.warning(
            "[HelperTools/Bilibili] Cookie was loaded from %s but Bilibili reports not logged in; it may be expired or incomplete",
            source,
        )
        return BilibiliCookieVerification(
            status="invalid",
            source=source,
            message=(
                f"当前使用{source}，但 B 站未识别为登录状态；"
                "凭据可能已失效或不完整。"
            ),
        )

    async def _verify_cookie_on_start(self) -> BilibiliCookieVerification:
        """Compatibility wrapper for callers from earlier plugin releases."""

        return await self.verify_cookie()

    @staticmethod
    def _short_url_headers() -> dict[str, str]:
        """b23.tv is a redirect host, so Bilibili account cookies never belong here."""

        return {
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }

    async def _short_url_head_location(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> str:
        try:
            async with session.head(
                url,
                headers=self._short_url_headers(),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
            ) as response:
                if response.status not in {301, 302, 303, 307, 308}:
                    return ""
                return clean_text(response.headers.get("Location"))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return ""

    @staticmethod
    def _short_redirect_target(current: str, location: str) -> str:
        target = urllib.parse.urljoin(current, location)
        if not _is_allowed_bilibili_url(target):
            raise BilibiliError(
                "short URL redirected outside the Bilibili allowlist",
                user_message="b23.tv 短链跳转到了非 B 站域名，已为安全起见拒绝访问。",
            )
        return target

    def _request_headers(self, *, include_cookie: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }
        cookie = self._cookie_header() if include_cookie else ""
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _cookie_header(self) -> str:
        return self._cookie_header_and_source()[0]

    def _cookie_header_and_source(self) -> tuple[str, str]:
        saved_qr_cookie = self.credentials.cookie_header()
        configured_cookie, configured_source = self._configured_cookie_header()
        if self._prefer_saved_qr_credentials() and saved_qr_cookie:
            return saved_qr_cookie, self.credentials.source_label
        if configured_cookie:
            return configured_cookie, configured_source
        if saved_qr_cookie:
            return saved_qr_cookie, self.credentials.source_label
        return "", ""

    def _configured_cookie_header(self) -> tuple[str, str]:
        raw = clean_text(cfg(self.config, "bilibili_video", "cookie", ""))
        if raw:
            return raw, "配置文本"
        configured = extract_file_config_value(
            cfg(self.config, "bilibili_video", "cookies_file", [])
        )
        path = resolve_existing_path(configured, self.data_dir)
        if path is None or not path.is_file():
            return "", ""
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return "", ""
        pairs: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#HttpOnly_"):
                stripped = stripped.removeprefix("#HttpOnly_")
            elif not stripped or stripped.startswith("#"):
                continue
            columns = stripped.split("\t")
            if len(columns) >= 7 and columns[5] and columns[6]:
                pairs.append(f"{columns[5]}={columns[6]}")
        return "; ".join(pairs), "cookies.txt 文件"

    def _cookie_source(self) -> str:
        _cookie, source = self._cookie_header_and_source()
        return source or "未知来源"

    def _prefer_saved_qr_credentials(self) -> bool:
        qr_login = cfg(self.config, "bilibili_video", "qr_login", {})
        if not isinstance(qr_login, dict):
            return True
        return read_bool(qr_login.get("prefer_saved_credentials", True), True)

    def _request_timeout(self) -> int:
        return read_int(
            cfg(self.config, "bilibili_video", "request_timeout_seconds", 20),
            20,
            minimum=5,
            maximum=120,
        )

    def _cache_get(
        self,
        cache: OrderedDict[str, tuple[float, Any]],
        key: str,
    ) -> Any | None:
        item = cache.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return value

    def _cache_set(
        self,
        cache: OrderedDict[str, tuple[float, Any]],
        key: str,
        value: Any,
    ) -> None:
        ttl_minutes = read_int(
            cfg(self.config, "bilibili_video", "cache_ttl_minutes", 360),
            360,
            minimum=1,
            maximum=10080,
        )
        cache[key] = (time.monotonic() + ttl_minutes * 60, value)
        cache.move_to_end(key)
        max_entries = read_int(
            cfg(self.config, "bilibili_video", "max_cache_entries", 64),
            64,
            minimum=1,
            maximum=1000,
        )
        while len(cache) > max_entries:
            evicted_key, _value = cache.popitem(last=False)
            self._locks.pop(evicted_key, None)


def extract_video_reference(text: Any) -> VideoReference | None:
    normalized = html.unescape(clean_text(text).replace("\\/", "/"))
    if not normalized:
        return None

    for match in _GENERIC_URL_RE.finditer(normalized):
        url = _clean_url(match.group(0))
        if not _is_allowed_bilibili_url(url):
            continue
        part = _extract_part(url)
        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
        if host in _SHORT_HOSTS:
            return VideoReference("short_url", url, part, normalized[:2000])
        bvid = _BVID_RE.search(url)
        if bvid:
            return VideoReference(
                "bvid",
                _canonical_bvid(bvid.group(1)),
                part,
                normalized[:2000],
            )
        aid = _AID_RE.search(url)
        if aid:
            return VideoReference("aid", aid.group(1), part, normalized[:2000])

    bvid = _BVID_RE.search(normalized)
    if bvid:
        return VideoReference(
            "bvid",
            _canonical_bvid(bvid.group(1)),
            _extract_part(normalized),
            normalized[:2000],
        )
    aid = _AID_RE.search(normalized)
    if aid:
        return VideoReference(
            "aid",
            aid.group(1),
            _extract_part(normalized),
            normalized[:2000],
        )
    return None


def extract_event_video_reference(event: Any) -> VideoReference | None:
    for text in collect_event_texts(event):
        reference = extract_video_reference(text)
        if reference is not None:
            return reference
    return None


def collect_event_texts(event: Any) -> list[str]:
    collected: list[str] = []
    seen_text: set[str] = set()
    seen_objects: set[int] = set()

    def add(value: Any) -> None:
        text = clean_text(value)
        if not text or text in seen_text:
            return
        seen_text.add(text)
        collected.append(text[:200000])

    add(getattr(event, "message_str", ""))
    message_obj = getattr(event, "message_obj", None)
    add(getattr(message_obj, "message_str", ""))
    getter = getattr(event, "get_messages", None)
    messages = getter() if callable(getter) else []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8 or value is None:
            return
        if isinstance(value, str):
            add(value)
            if value.lstrip().startswith(("{", "[")):
                try:
                    walk(json.loads(value.replace("&#44;", ",")), depth + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return
        if isinstance(value, (int, float, bool, bytes)):
            return
        object_id = id(value)
        if object_id in seen_objects:
            return
        seen_objects.add(object_id)
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested, depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                walk(nested, depth + 1)
            return

        if isinstance(value, Comp.Json):
            walk(getattr(value, "data", None), depth + 1)
        for attribute in ("text", "url", "title", "content", "data"):
            nested = getattr(value, attribute, None)
            if nested is not None:
                walk(nested, depth + 1)
        for attribute in ("chain", "nodes"):
            nested = getattr(value, attribute, None)
            if nested is not None:
                walk(nested, depth + 1)

    walk(messages if isinstance(messages, list) else [])
    return collected


def request_has_bilibili_context(request: Any) -> bool:
    markers = (BILIBILI_CONTEXT_PREFIX, "[B站视频解析失败]")
    if any(marker in clean_text(getattr(request, "prompt", "")) for marker in markers):
        return True
    parts = getattr(request, "extra_user_content_parts", None)
    if not isinstance(parts, list):
        return False
    for part in parts:
        text = (
            clean_text(part.get("text"))
            if isinstance(part, dict)
            else clean_text(getattr(part, "text", ""))
        )
        if any(text.startswith(marker) for marker in markers):
            return True
    return False


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


def _video_info_from_api(data: dict[str, Any], requested_page: int) -> VideoInfo:
    pages = data.get("pages") or []
    if not isinstance(pages, list):
        pages = []
    page_count = max(1, len(pages), _safe_int(data.get("videos")))
    if requested_page < 1 or requested_page > page_count:
        raise BilibiliError(
            f"requested page {requested_page} is outside 1..{page_count}",
            user_message=f"该视频只有 {page_count} 个分 P，不能读取 P{requested_page}。",
        )
    page_data = next(
        (
            page
            for page in pages
            if isinstance(page, dict)
            and _safe_int(page.get("page")) == requested_page
        ),
        pages[requested_page - 1] if len(pages) >= requested_page else {},
    )
    if not isinstance(page_data, dict):
        page_data = {}
    if requested_page > 1 and not page_data:
        raise BilibiliError(
            "view API omitted the requested page metadata",
            user_message=f"B 站没有返回 P{requested_page} 的分 P 信息。",
        )
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    dimension = page_data.get("dimension") or data.get("dimension") or {}
    bvid = clean_text(data.get("bvid"))
    aid = _safe_int(data.get("aid"))
    cid = _safe_int(page_data.get("cid") or data.get("cid"))
    if not bvid or aid <= 0 or cid <= 0:
        raise BilibiliError(
            "view API omitted a required video identifier",
            user_message="B 站返回的视频信息缺少 BV/av/cid 标识，暂时无法分析。",
        )
    stats = {
        key: _safe_int(stat.get(key))
        for key in ("view", "like", "coin", "favorite", "share", "reply", "danmaku")
    }
    return VideoInfo(
        aid=aid,
        bvid=_canonical_bvid(bvid),
        cid=cid,
        page=requested_page,
        page_count=page_count,
        part_title=clean_text(page_data.get("part")) or clean_text(data.get("title")),
        title=clean_text(data.get("title")),
        description=clean_text(data.get("desc")),
        owner_name=clean_text(owner.get("name")),
        owner_mid=clean_text(owner.get("mid")),
        duration=_safe_int(page_data.get("duration") or data.get("duration")),
        pubdate=_safe_int(data.get("pubdate")),
        cover_url=clean_text(data.get("pic")),
        category=clean_text(data.get("tname")),
        width=_safe_int(dimension.get("width")),
        height=_safe_int(dimension.get("height")),
        stats=stats,
    )


def _extract_part(text: str) -> int:
    match = re.search(r"(?:[?&]|\b)p=(\d+)", text, re.IGNORECASE)
    if not match:
        return 1
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return 1


def _canonical_bvid(value: str) -> str:
    return f"BV{value[2:]}" if len(value) >= 2 else value


def _clean_url(value: str) -> str:
    return html.unescape(clean_text(value)).strip("<>").rstrip(_TRAILING_URL_CHARS)


def _strip_short_url_tracking(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def _is_short_url_host(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in _SHORT_HOSTS


def _canonical_video_url(reference: VideoReference | None) -> str:
    if reference is None or reference.kind not in {"bvid", "aid"}:
        return ""
    suffix = f"?p={reference.part}" if reference.part > 1 else ""
    if reference.kind == "bvid":
        return f"https://www.bilibili.com/video/{reference.value}{suffix}"
    return f"https://www.bilibili.com/video/av{reference.value}{suffix}"


def _is_allowed_bilibili_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _ALLOWED_HOST_SUFFIXES
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
