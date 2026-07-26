from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from .bilibili_types import (
    BilibiliError,
    DownloadedMedia,
    VideoInfo,
    format_duration,
    read_bounded_response,
)
from .helper_utils import cfg, clean_text, read_bool, read_float, read_int

_DEFAULT_PROMPT = """请完整观看这个视频并输出客观、可供另一个对话模型使用的中文事实报告。
至少覆盖：主题与结论、按时间顺序的重要场景、人物或主体、对白/旁白、屏幕文字、声音与情绪、梗或反转，以及无法确认的细节。
不要扮演聊天机器人，不要直接回应发送视频的用户，不要加入当前会话人格。没有看清或听清的内容请明确标为不确定，不得编造。"""


class GeminiVideoAnalyzer:
    """Upload a bounded local video to the Gemini REST API and return facts."""

    def __init__(self, config: Any) -> None:
        self.config = config

    async def analyze(
        self,
        info: VideoInfo,
        media: DownloadedMedia,
        session: aiohttp.ClientSession,
    ) -> str:
        api_key = clean_text(self._setting("api_key", "")) or clean_text(
            os.environ.get("GEMINI_API_KEY")
        )
        if not api_key:
            raise BilibiliError(
                "Gemini API key is not configured",
                user_message=(
                    "当前选择了 Gemini 视频分析，但没有配置 Gemini API Key。"
                    "请填写 bilibili_video.gemini.api_key。"
                ),
            )
        api_base = clean_text(
            self._setting("api_base", "https://generativelanguage.googleapis.com")
        ).rstrip("/")
        parsed_base = urllib.parse.urlparse(api_base)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
            raise BilibiliError(
                "invalid Gemini API base URL",
                user_message="Gemini API 地址不是有效的 HTTP/HTTPS 地址。",
            )
        model = clean_text(self._setting("model", "gemini-2.5-flash"))
        model = model.removeprefix("models/").strip("/")
        if not model:
            raise BilibiliError(
                "Gemini model is empty", user_message="Gemini 模型名不能为空。"
            )

        file_size = media.file_path.stat().st_size
        upload_mode = self._upload_mode(file_size)
        if upload_mode == "inline" and file_size > self._inline_limit_bytes():
            limit_mb = self._inline_limit_bytes() // (1024 * 1024)
            raise BilibiliError(
                "video exceeds the configured inline upload limit",
                user_message=(
                    f"视频超过内嵌 Base64 的 {limit_mb} MB 上限。"
                    "请改用“自动选择”或“Gemini File API”。"
                ),
            )
        prompt = self._build_prompt(info)
        uploaded_name = ""
        try:
            if upload_mode == "inline":
                encoded = await asyncio.to_thread(_base64_file, media.file_path)
                media_part: dict[str, Any] = {
                    "inline_data": {
                        "mime_type": media.mime_type,
                        "data": encoded,
                    }
                }
            else:
                uploaded_name, uploaded_uri, uploaded_mime = await self._upload_file(
                    session,
                    api_base,
                    api_key,
                    media,
                    info,
                )
                media_part = {
                    "file_data": {
                        "mime_type": uploaded_mime or media.mime_type,
                        "file_uri": uploaded_uri,
                    }
                }

            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}, media_part],
                    }
                ],
                "generationConfig": {
                    "temperature": read_float(
                        self._setting("temperature", 0.2),
                        0.2,
                        minimum=0.0,
                        maximum=2.0,
                    ),
                    "maxOutputTokens": read_int(
                        self._setting("max_output_tokens", 4096),
                        4096,
                        minimum=256,
                        maximum=65536,
                    ),
                },
            }
            endpoint = (
                f"{api_base}/v1beta/models/"
                f"{urllib.parse.quote(model, safe='-_.')}:generateContent"
            )
            response = await self._json_request(
                session,
                "POST",
                endpoint,
                params={"key": api_key},
                json_body=payload,
                timeout=self._timeout(),
                max_bytes=16 * 1024 * 1024,
                stage="Gemini 视频分析",
            )
            result = _extract_gemini_text(response)
            max_chars = read_int(
                self._setting("max_analysis_chars", 24000),
                24000,
                minimum=1000,
                maximum=100000,
            )
            return result[:max_chars]
        finally:
            if uploaded_name and read_bool(
                self._setting("delete_uploaded_file", True),
                True,
            ):
                await self._delete_file(session, api_base, api_key, uploaded_name)

    async def _upload_file(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        api_key: str,
        media: DownloadedMedia,
        info: VideoInfo,
    ) -> tuple[str, str, str]:
        size = media.file_path.stat().st_size
        start_headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": media.mime_type,
            "Content-Type": "application/json",
        }
        endpoint = f"{api_base}/upload/v1beta/files"
        async with session.post(
            endpoint,
            params={"key": api_key},
            headers=start_headers,
            json={"file": {"display_name": f"{info.bvid}_p{info.page}"}},
            timeout=aiohttp.ClientTimeout(total=min(60, self._timeout())),
        ) as response:
            body = await read_bounded_response(response, 1024 * 1024)
            if response.status < 200 or response.status >= 300:
                raise _gemini_http_error("初始化视频上传", response.status, body)
            upload_url = clean_text(response.headers.get("X-Goog-Upload-URL"))
        if not upload_url:
            raise BilibiliError(
                "Gemini did not return a resumable upload URL",
                user_message="Gemini File API 没有返回视频上传地址。",
            )

        upload_headers = {
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        }
        with media.file_path.open("rb") as stream:
            async with session.post(
                upload_url,
                headers=upload_headers,
                data=stream,
                timeout=aiohttp.ClientTimeout(total=self._timeout()),
            ) as response:
                body = await read_bounded_response(response, 4 * 1024 * 1024)
                if response.status < 200 or response.status >= 300:
                    raise _gemini_http_error("上传视频", response.status, body)
        payload = _decode_json(body, "Gemini 上传响应")
        file_info = payload.get("file") or {}
        if not isinstance(file_info, dict):
            raise BilibiliError(
                "Gemini upload response has invalid file metadata",
                user_message="Gemini 上传响应中的文件信息格式无效。",
            )
        name = clean_text(file_info.get("name"))
        uri = clean_text(file_info.get("uri"))
        mime_type = clean_text(file_info.get("mime_type")) or media.mime_type
        state = _state_name(file_info.get("state"))
        if not name or not uri:
            raise BilibiliError(
                "Gemini upload response is missing file identity",
                user_message="Gemini 已接收视频，但没有返回可供分析的文件标识。",
            )

        try:
            deadline = time.monotonic() + self._timeout()
            while state in {"", "PROCESSING", "STATE_UNSPECIFIED"}:
                if time.monotonic() >= deadline:
                    raise BilibiliError(
                        "Gemini file processing timed out",
                        user_message="Gemini 处理已上传视频时超时。",
                    )
                await asyncio.sleep(2)
                file_payload = await self._json_request(
                    session,
                    "GET",
                    f"{api_base}/v1beta/{name}",
                    params={"key": api_key},
                    timeout=min(30, self._timeout()),
                    max_bytes=4 * 1024 * 1024,
                    stage="查询 Gemini 文件状态",
                )
                state = _state_name(file_payload.get("state"))
                uri = clean_text(file_payload.get("uri")) or uri
                mime_type = clean_text(file_payload.get("mime_type")) or mime_type
            if state != "ACTIVE":
                raise BilibiliError(
                    f"Gemini file entered state {state}",
                    user_message=(
                        "Gemini 无法处理这个视频，文件状态为 "
                        f"{state or '未知'}。"
                    ),
                )
        except (Exception, asyncio.CancelledError):
            if read_bool(self._setting("delete_uploaded_file", True), True):
                await self._delete_file(session, api_base, api_key, name)
            raise
        return name, uri, mime_type

    async def _delete_file(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        api_key: str,
        name: str,
    ) -> None:
        try:
            async with session.delete(
                f"{api_base}/v1beta/{name}",
                params={"key": api_key},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                response.release()
                if response.status < 200 or response.status >= 300:
                    logger.warning(
                        "[HelperTools/Bilibili] Gemini file cleanup returned HTTP %s",
                        response.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(
                "[HelperTools/Bilibili] Gemini file cleanup failed: %r",
                exc,
            )

    async def _json_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        params: dict[str, str],
        timeout: int,
        max_bytes: int,
        stage: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            try:
                body = await read_bounded_response(response, max_bytes)
            except BilibiliError as exc:
                raise BilibiliError(
                    f"{stage} response exceeded {max_bytes} bytes",
                    user_message=f"{stage}返回的数据异常过大。",
                ) from exc
            if response.status < 200 or response.status >= 300:
                raise _gemini_http_error(stage, response.status, body)
        return _decode_json(body, stage)

    def _build_prompt(self, info: VideoInfo) -> str:
        configured = clean_text(self._setting("analysis_prompt", _DEFAULT_PROMPT))
        prompt = configured or _DEFAULT_PROMPT
        metadata = (
            f"B站标题：{info.title}\n"
            f"UP主：{info.owner_name}\n"
            f"分P：P{info.page}/{info.page_count} {info.part_title}\n"
            f"时长：{format_duration(info.duration)}\n"
            f"简介：{info.description[:1000]}"
        )
        return f"{metadata}\n\n{prompt}"

    def _upload_mode(self, file_size: int) -> str:
        configured = clean_text(self._setting("upload_mode", "自动选择")).casefold()
        if "base64" in configured or "内嵌" in configured:
            return "inline"
        if "file" in configured or "文件" in configured:
            return "file"
        return "inline" if file_size <= self._inline_limit_bytes() else "file"

    def _inline_limit_bytes(self) -> int:
        inline_limit = read_int(
            self._setting("inline_limit_mb", 15),
            15,
            minimum=1,
            maximum=50,
        )
        return inline_limit * 1024 * 1024

    def _timeout(self) -> int:
        return read_int(
            self._setting("timeout_seconds", 240),
            240,
            minimum=30,
            maximum=1800,
        )

    def _setting(self, key: str, default: Any) -> Any:
        settings = cfg(self.config, "bilibili_video", "gemini", {})
        if not isinstance(settings, dict):
            return default
        return settings.get(key, default)


def _base64_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _decode_json(body: bytes, stage: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BilibiliError(
            f"{stage} returned invalid JSON",
            user_message=f"{stage}返回了无法解析的数据。",
        ) from exc
    if not isinstance(payload, dict):
        raise BilibiliError(f"{stage} returned a non-object response")
    return payload


def _gemini_http_error(stage: str, status: int, body: bytes) -> BilibiliError:
    message = ""
    try:
        payload = json.loads(body[: 1024 * 1024].decode("utf-8"))
        message = clean_text((payload.get("error") or {}).get("message"))
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        message = ""
    if status in {401, 403}:
        hint = "请检查 Gemini API Key、模型权限和 API 地址。"
    elif status == 429:
        hint = "Gemini 请求额度或频率已达到限制，请稍后重试。"
    elif status == 413:
        hint = "Gemini 拒绝了过大的视频文件，请调低大小限制或下载画质。"
    elif status >= 500:
        hint = "Gemini 服务暂时不可用，请稍后重试。"
    else:
        hint = message[:300] or "请检查 Gemini 配置和视频格式。"
    return BilibiliError(
        f"{stage} returned HTTP {status}: {message}",
        user_message=f"{stage}失败（HTTP {status}）。{hint}",
    )


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    feedback = payload.get("promptFeedback") or {}
    if not isinstance(feedback, dict):
        feedback = {}
    block_reason = clean_text(feedback.get("blockReason"))
    if block_reason:
        raise BilibiliError(
            f"Gemini blocked the video: {block_reason}",
            user_message=f"Gemini 因安全策略拒绝分析这个视频（{block_reason}）。",
        )
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise BilibiliError(
            "Gemini returned no candidates",
            user_message="Gemini 没有返回视频分析结果。",
        )
    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    finish_reason = clean_text(candidate.get("finishReason"))
    if finish_reason and finish_reason not in {"STOP", "MAX_TOKENS"}:
        raise BilibiliError(
            f"Gemini stopped with {finish_reason}",
            user_message=f"Gemini 视频分析异常结束（{finish_reason}）。",
        )
    content = candidate.get("content") or {}
    parts = (content.get("parts") or []) if isinstance(content, dict) else []
    texts = [
        clean_text(part.get("text"))
        for part in parts
        if isinstance(part, dict) and clean_text(part.get("text"))
    ]
    result = "\n".join(texts).strip()
    if not result:
        raise BilibiliError(
            "Gemini returned empty text",
            user_message="Gemini 返回了空的视频分析结果。",
        )
    return result


def _state_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    return clean_text(value).upper()
