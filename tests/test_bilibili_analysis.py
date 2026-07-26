from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_helper_tools.bilibili_gemini import (
    GeminiVideoAnalyzer,
    _extract_gemini_text,
)
from astrbot_plugin_helper_tools.bilibili_transcript import (
    BilibiliTranscriptService,
    _sample_timeline,
    _secure_bcut_upload_url,
    _secure_subtitle_url,
)
from astrbot_plugin_helper_tools.bilibili_types import (
    BilibiliError,
    DownloadedMedia,
    VideoInfo,
    read_bounded_response,
)


class TranscriptSamplingTests(unittest.TestCase):
    def test_long_timeline_keeps_beginning_middle_and_end(self) -> None:
        lines = [
            f"[{index:02d}:00] 第 {index} 段 " + "内容" * 20 for index in range(30)
        ]

        sampled = _sample_timeline(lines, 900)

        self.assertLessEqual(len(sampled), 900)
        self.assertIn("第 0 段", sampled)
        self.assertIn("第 29 段", sampled)
        self.assertIn("已省略", sampled)
        self.assertTrue(any(f"第 {index} 段" in sampled for index in range(10, 20)))

    def test_bcut_upload_url_is_allowlisted_and_upgraded_to_https(self) -> None:
        secured = _secure_bcut_upload_url(
            "http://jssz-boss.biliapi.net/upload/path?signature=example"
        )

        self.assertTrue(secured.startswith("https://jssz-boss.biliapi.net/"))
        with self.assertRaises(BilibiliError):
            _secure_bcut_upload_url("http://127.0.0.1/private")

    def test_subtitle_url_accepts_known_bilibili_cdns_and_upgrades_https(self) -> None:
        secured = _secure_subtitle_url(
            "http://aisubtitle.bilivideo.com/bfs/ai_subtitle/test.json"
        )

        self.assertEqual(
            secured,
            "https://aisubtitle.bilivideo.com/bfs/ai_subtitle/test.json",
        )
        self.assertEqual(
            _secure_subtitle_url("//aisubtitle.hdslb.com/bfs/test.json"),
            "https://aisubtitle.hdslb.com/bfs/test.json",
        )
        with self.assertRaises(BilibiliError):
            _secure_subtitle_url("https://hdslb.com.example.invalid/test.json")


class OfficialSubtitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_next_trusted_subtitle_and_omits_cookie_for_cdn(self) -> None:
        class Content:
            def __init__(self, body: bytes) -> None:
                self.body = body

            async def iter_chunked(self, _size: int):
                yield self.body

        class Response:
            def __init__(self, payload: dict) -> None:
                import json

                self.status = 200
                self.headers: dict[str, str] = {}
                self.content = Content(json.dumps(payload).encode("utf-8"))

        class RequestContext:
            def __init__(self, response: Response) -> None:
                self.response = response

            async def __aenter__(self) -> Response:
                return self.response

            async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
                return False

        class Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def get(self, url: str, **kwargs):
                self.calls.append((url, kwargs))
                if "x/player/v2" in url:
                    return RequestContext(
                        Response(
                            {
                                "code": 0,
                                "data": {
                                    "subtitle": {
                                        "subtitles": [
                                            {
                                                "lan": "zh-CN",
                                                "subtitle_url": "https://example.invalid/bad.json",
                                            },
                                            {
                                                "lan": "en",
                                                "subtitle_url": (
                                                    "http://aisubtitle.bilivideo.com/"
                                                    "bfs/good.json"
                                                ),
                                            },
                                        ]
                                    }
                                },
                            }
                        )
                    )
                return RequestContext(
                    Response(
                        {
                            "body": [
                                {"from": 0, "to": 1, "content": "hello"}
                            ]
                        }
                    )
                )

        info = VideoInfo(
            aid=1,
            bvid="BV1GJ411x7h7",
            cid=2,
            page=1,
            page_count=1,
            part_title="test",
            title="test",
            description="",
            owner_name="",
            owner_mid="",
            duration=1,
            pubdate=0,
            cover_url="",
            category="",
        )
        session = Session()
        transcript = await BilibiliTranscriptService({}, object())._fetch_official_subtitle(
            info,
            session,
            {"User-Agent": "test", "Cookie": "SESSDATA=secret"},
        )

        self.assertIsNotNone(transcript)
        self.assertEqual(transcript.segments[0].text, "hello")
        self.assertEqual(
            session.calls[1][0],
            "https://aisubtitle.bilivideo.com/bfs/good.json",
        )
        self.assertNotIn("Cookie", session.calls[1][1]["headers"])


class GeminiResponseTests(unittest.TestCase):
    def test_collects_all_text_parts(self) -> None:
        payload = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "第一段"}, {"text": "第二段"}]},
                }
            ]
        }

        self.assertEqual(_extract_gemini_text(payload), "第一段\n第二段")

    def test_surfaces_blocked_or_empty_responses(self) -> None:
        with self.assertRaises(BilibiliError):
            _extract_gemini_text({"promptFeedback": {"blockReason": "SAFETY"}})
        with self.assertRaises(BilibiliError):
            _extract_gemini_text({"candidates": []})


class GeminiUploadLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_inline_mode_still_enforces_its_memory_limit(self) -> None:
        info = VideoInfo(
            aid=1,
            bvid="BV1GJ411x7h7",
            cid=2,
            page=1,
            page_count=1,
            part_title="测试",
            title="测试",
            description="",
            owner_name="",
            owner_mid="",
            duration=1,
            pubdate=0,
            cover_url="",
            category="",
        )
        analyzer = GeminiVideoAnalyzer(
            {
                "bilibili_video": {
                    "gemini": {
                        "api_key": "test-key",
                        "upload_mode": "内嵌 Base64",
                        "inline_limit_mb": 1,
                    }
                }
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(b"0" * (1024 * 1024 + 1))
            media = DownloadedMedia(path, Path(temp_dir), "video/mp4")

            with self.assertRaises(BilibiliError):
                await analyzer.analyze(info, media, None)


class BoundedResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_network_chunks_and_enforces_the_limit(self) -> None:
        class Content:
            def __init__(self, chunks):
                self.chunks = chunks

            async def iter_chunked(self, _size):
                for chunk in self.chunks:
                    yield chunk

        class Response:
            def __init__(self, chunks):
                self.headers = {}
                self.content = Content(chunks)

        self.assertEqual(
            await read_bounded_response(Response([b"abc", b"def"]), 6),
            b"abcdef",
        )
        with self.assertRaises(BilibiliError):
            await read_bounded_response(Response([b"abc", b"def"]), 5)


if __name__ == "__main__":
    unittest.main()
