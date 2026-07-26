from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import astrbot.api.message_components as Comp
from aiohttp import web

from astrbot_plugin_helper_tools.bilibili_service import (
    BilibiliVideoService,
    _video_info_from_api,
    extract_event_video_reference,
    extract_video_reference,
)
from astrbot_plugin_helper_tools.bilibili_types import (
    BilibiliError,
    BilibiliVideoContext,
    VideoFrame,
    VideoInfo,
)


class DummyEvent:
    def __init__(self, chain: list[object], message_str: str = "") -> None:
        self._chain = chain
        self.message_str = message_str
        self.message_obj = type("Message", (), {"message_str": message_str})()

    def get_messages(self) -> list[object]:
        return self._chain


def make_info() -> VideoInfo:
    return VideoInfo(
        aid=123,
        bvid="BV1GJ411x7h7",
        cid=456,
        page=1,
        page_count=1,
        part_title="测试分P",
        title="测试视频",
        description="简介",
        owner_name="UP主",
        owner_mid="100",
        duration=120,
        pubdate=0,
        cover_url="",
        category="测试",
    )


class BilibiliReferenceTests(unittest.TestCase):
    def test_extracts_supported_text_forms_and_page(self) -> None:
        bv = extract_video_reference("分享：BV1GJ411x7h7?p=3 快来看")
        av = extract_video_reference(
            "https://www.bilibili.com/video/av170001?p=2&spm_id_from=333"
        )
        short = extract_video_reference("链接 https://b23.tv/AbCd12，真的很好看")
        short_ascii = extract_video_reference(
            "链接 https://b23.tv/AbCd12,and then some text"
        )

        self.assertIsNotNone(bv)
        self.assertEqual((bv.kind, bv.value, bv.part), ("bvid", "BV1GJ411x7h7", 3))
        self.assertIsNotNone(av)
        self.assertEqual((av.kind, av.value, av.part), ("aid", "170001", 2))
        self.assertIsNotNone(short)
        self.assertEqual(short.kind, "short_url")
        self.assertEqual(short.value, "https://b23.tv/AbCd12")
        self.assertEqual(short_ascii.value, "https://b23.tv/AbCd12")

    def test_rejects_lookalike_domains_and_unrelated_numbers(self) -> None:
        self.assertIsNone(
            extract_video_reference("https://evilbilibili.com/video/170001")
        )
        self.assertIsNone(extract_video_reference("今天有 170001 个人看过"))

    def test_extracts_direct_and_quoted_qq_miniapp_cards(self) -> None:
        direct = Comp.Json(
            data={
                "app": "com.tencent.miniapp_01",
                "meta": {
                    "detail_1": {
                        "title": "视频标题",
                        "qqdocurl": "https://b23.tv/Test01",
                    }
                },
            }
        )
        quoted = Comp.Reply(
            id="123",
            chain=[
                Comp.Json(
                    data=(
                        '{"meta":{"detail_1":{"jumpUrl":'
                        '"https://www.bilibili.com/video/BV1GJ411x7h7?p=2"}}}'
                    )
                )
            ],
        )

        direct_reference = extract_event_video_reference(DummyEvent([direct]))
        quoted_reference = extract_event_video_reference(DummyEvent([quoted]))

        self.assertEqual(direct_reference.kind, "short_url")
        self.assertEqual(quoted_reference.kind, "bvid")
        self.assertEqual(quoted_reference.part, 2)

    def test_builds_selected_page_metadata(self) -> None:
        data = {
            "aid": 123,
            "bvid": "BV1GJ411x7h7",
            "title": "总标题",
            "desc": "简介",
            "duration": 300,
            "pubdate": 100,
            "pic": "https://i0.hdslb.com/test.jpg",
            "tname": "科技",
            "owner": {"name": "Alice", "mid": 42},
            "stat": {"view": 10, "like": 2},
            "pages": [
                {"page": 1, "cid": 11, "part": "上", "duration": 100},
                {
                    "page": 2,
                    "cid": 22,
                    "part": "下",
                    "duration": 200,
                    "dimension": {"width": 1920, "height": 1080},
                },
            ],
        }

        info = _video_info_from_api(data, 2)

        self.assertEqual(info.cid, 22)
        self.assertEqual(info.duration, 200)
        self.assertEqual(info.part_title, "下")
        self.assertEqual(
            info.canonical_url, "https://www.bilibili.com/video/BV1GJ411x7h7?p=2"
        )
        with self.assertRaises(BilibiliError):
            _video_info_from_api(data, 3)

    def test_reads_http_only_cookies_from_netscape_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = Path(temp_dir) / "cookies.txt"
            cookie_path.write_text(
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsecret\n"
                ".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\tcsrf\n",
                encoding="utf-8",
            )
            service = BilibiliVideoService(
                {"bilibili_video": {"cookies_file": str(cookie_path)}},
                Path(temp_dir),
            )

            header = service._cookie_header()

            self.assertIn("SESSDATA=secret", header)
            self.assertIn("bili_jct=csrf", header)
            self.assertNotIn("Cookie", service._request_headers(include_cookie=False))
            self.assertIn("Cookie", service._request_headers())


class BilibiliShortUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_url_strips_tracking_and_never_sends_bilibili_cookie(self) -> None:
        observed: dict[str, str] = {}

        async def handler(request: web.Request) -> web.Response:
            observed["query"] = request.query_string
            observed["cookie"] = request.headers.get("Cookie", "")
            return web.Response(
                status=302,
                headers={
                    "Location": "https://www.bilibili.com/video/BV1GJ411x7h7?p=2"
                },
            )

        app = web.Application()
        app.router.add_route("*", "/OffDfmV", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        service = BilibiliVideoService(
            {"bilibili_video": {"cookie": "SESSDATA=secret"}},
            Path(tempfile.gettempdir()),
        )

        try:
            with (
                patch(
                    "astrbot_plugin_helper_tools.bilibili_service._is_allowed_bilibili_url",
                    return_value=True,
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_service._is_short_url_host",
                    return_value=True,
                ),
            ):
                resolved = await service._resolve_short_url(
                    f"http://127.0.0.1:{port}/OffDfmV?share_medium=android&ts=1"
                )
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(observed["query"], "")
        self.assertEqual(observed["cookie"], "")
        self.assertEqual(
            resolved,
            "https://www.bilibili.com/video/BV1GJ411x7h7?p=2",
        )

    async def test_short_url_retries_with_head_after_get_400(self) -> None:
        methods: list[str] = []

        async def handler(request: web.Request) -> web.Response:
            methods.append(request.method)
            if request.method == "GET":
                return web.Response(status=400)
            return web.Response(
                status=302,
                headers={"Location": "https://www.bilibili.com/video/BV1GJ411x7h7"},
            )

        app = web.Application()
        app.router.add_route("*", "/OffDfmV", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        service = BilibiliVideoService({}, Path(tempfile.gettempdir()))

        try:
            with (
                patch(
                    "astrbot_plugin_helper_tools.bilibili_service._is_allowed_bilibili_url",
                    return_value=True,
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_service._is_short_url_host",
                    return_value=True,
                ),
            ):
                resolved = await service._resolve_short_url(
                    f"http://127.0.0.1:{port}/OffDfmV"
                )
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(methods, ["GET", "HEAD"])
        self.assertEqual(resolved, "https://www.bilibili.com/video/BV1GJ411x7h7")


class BilibiliCookieVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_check_calls_bilibili_nav_with_the_configured_cookie(self) -> None:
        observed: dict[str, str] = {}

        async def nav(request: web.Request) -> web.Response:
            observed["cookie"] = request.headers.get("Cookie", "")
            return web.json_response({"code": 0, "data": {"isLogin": True}})

        app = web.Application()
        app.router.add_get("/x/web-interface/nav", nav)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        service = BilibiliVideoService(
            {"bilibili_video": {"cookie": "SESSDATA=secret; bili_jct=csrf"}},
            Path(tempfile.gettempdir()),
        )

        try:
            with patch(
                "astrbot_plugin_helper_tools.bilibili_service._NAV_ENDPOINT",
                f"http://127.0.0.1:{port}/x/web-interface/nav",
            ):
                await service._verify_cookie_on_start()
        finally:
            await service.close()
            await runner.cleanup()

        self.assertIn("SESSDATA=secret", observed["cookie"])
        self.assertIn("bili_jct=csrf", observed["cookie"])


class BilibiliCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_merges_concurrent_analysis_for_the_same_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = BilibiliVideoService(
                {"bilibili_video": {"cache_ttl_minutes": 10}},
                Path(temp_dir),
            )
            info = make_info()
            calls = 0

            async def resolve(_reference, *, force_refresh):
                return info

            async def analyze(_info, _mode):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.02)
                return "解析完成"

            service._resolve_info = resolve
            service._analyze_info = analyze
            reference = extract_video_reference(info.bvid)

            first, second = await asyncio.gather(
                service.analyze_reference(reference),
                service.analyze_reference(reference),
            )

            self.assertEqual((first, second), ("解析完成", "解析完成"))
            self.assertEqual(calls, 1)

    async def test_cache_never_keeps_visual_frame_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = BilibiliVideoService(
                {"bilibili_video": {"cache_ttl_minutes": 10}},
                Path(temp_dir),
            )
            info = make_info()
            frame = VideoFrame(index=1, timestamp=1.0, data=b"\xff\xd8not-cached")

            async def resolve(_reference, *, force_refresh):
                return info

            async def analyze(_info, _mode):
                return BilibiliVideoContext("解析完成", frames=(frame,))

            service._resolve_info = resolve
            service._analyze_info = analyze
            result = await service.analyze_reference_result(
                extract_video_reference(info.bvid)
            )
            cached = service._cached_context(f"astrbot:{info.cache_key}")

            self.assertEqual(result.frames, ())
            self.assertIsNotNone(cached)
            self.assertEqual(cached.frames, ())
            self.assertEqual(cached.content, "解析完成")


class BilibiliDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_extraction_uses_the_remaining_pipeline_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = BilibiliVideoService(
                {
                    "bilibili_video": {
                        "default_model": {"frame_vision": {"enabled": True}}
                    }
                },
                Path(temp_dir),
            )
            info = make_info()
            frame_extraction_cancelled = asyncio.Event()

            async def resolve(_reference, *, force_refresh):
                return info

            async def analyze(_info, _mode):
                await asyncio.sleep(0.10)
                return BilibiliVideoContext("字幕资料")

            async def extract(_info):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    frame_extraction_cancelled.set()
                    raise
                return ()

            service._resolve_info = resolve
            service._analyze_info = analyze
            service._extract_frames = extract
            service._processing_timeout_seconds = lambda: 0.16

            started = time.monotonic()
            result = await service.analyze_reference_result(
                extract_video_reference(info.bvid)
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.22)
        self.assertTrue(frame_extraction_cancelled.is_set())
        self.assertEqual(result.frames, ())
        self.assertIn("抽取画面失败", result.text)


if __name__ == "__main__":
    unittest.main()
