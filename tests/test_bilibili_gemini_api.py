from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from astrbot_plugin_helper_tools.bilibili_gemini import GeminiVideoAnalyzer
from astrbot_plugin_helper_tools.bilibili_types import (
    BilibiliError,
    DownloadedMedia,
    VideoInfo,
)


def make_info() -> VideoInfo:
    return VideoInfo(
        aid=1,
        bvid="BV1GJ411x7h7",
        cid=2,
        page=1,
        page_count=1,
        part_title="测试分P",
        title="测试视频",
        description="测试简介",
        owner_name="测试UP",
        owner_mid="3",
        duration=5,
        pubdate=0,
        cover_url="",
        category="测试",
    )


class GeminiFileAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_file_api_upload_generate_and_cleanup(self) -> None:
        observed: dict[str, object] = {}

        async def start_upload(request: web.Request) -> web.Response:
            observed["start_key"] = request.query.get("key")
            observed["start_body"] = await request.json()
            upload_url = f"{request.scheme}://{request.host}/upload-session"
            return web.json_response(
                {},
                headers={"X-Goog-Upload-URL": upload_url},
            )

        async def upload(request: web.Request) -> web.Response:
            observed["upload_body"] = await request.read()
            observed["upload_command"] = request.headers.get("X-Goog-Upload-Command")
            return web.json_response(
                {
                    "file": {
                        "name": "files/test-file",
                        "uri": "https://files.example/test-file",
                        "mime_type": "video/mp4",
                        "state": "ACTIVE",
                    }
                }
            )

        async def generate(request: web.Request) -> web.Response:
            observed["generate_key"] = request.query.get("key")
            observed["generate_body"] = await request.json()
            return web.json_response(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": "客观视频事实"}]},
                        }
                    ]
                }
            )

        async def delete_file(request: web.Request) -> web.Response:
            observed["deleted"] = request.match_info["file_id"]
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/upload/v1beta/files", start_upload)
        app.router.add_post("/upload-session", upload)
        app.router.add_post(
            "/v1beta/models/gemini-test:generateContent",
            generate,
        )
        app.router.add_delete("/v1beta/files/{file_id}", delete_file)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        try:
            analyzer = GeminiVideoAnalyzer(
                {
                    "bilibili_video": {
                        "gemini": {
                            "api_key": "test-key",
                            "api_base": f"http://127.0.0.1:{port}",
                            "model": "gemini-test",
                            "upload_mode": "Gemini File API",
                            "delete_uploaded_file": True,
                            "timeout_seconds": 30,
                        }
                    }
                }
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "video.mp4"
                path.write_bytes(b"test-video-bytes")
                media = DownloadedMedia(path, Path(temp_dir), "video/mp4")
                async with aiohttp.ClientSession() as session:
                    result = await analyzer.analyze(make_info(), media, session)
        finally:
            await runner.cleanup()

        self.assertEqual(result, "客观视频事实")
        self.assertEqual(observed["start_key"], "test-key")
        self.assertEqual(observed["upload_body"], b"test-video-bytes")
        self.assertEqual(observed["upload_command"], "upload, finalize")
        self.assertEqual(observed["generate_key"], "test-key")
        parts = observed["generate_body"]["contents"][0]["parts"]
        self.assertIn("text", parts[0])
        self.assertEqual(
            parts[1]["file_data"]["file_uri"],
            "https://files.example/test-file",
        )
        self.assertEqual(observed["deleted"], "test-file")

    async def test_file_api_cleans_up_when_processing_fails(self) -> None:
        deleted: list[str] = []

        async def start_upload(request: web.Request) -> web.Response:
            upload_url = f"{request.scheme}://{request.host}/upload-session"
            return web.json_response(
                {},
                headers={"X-Goog-Upload-URL": upload_url},
            )

        async def upload(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "file": {
                        "name": "files/failed-file",
                        "uri": "https://files.example/failed-file",
                        "mime_type": "video/mp4",
                        "state": "FAILED",
                    }
                }
            )

        async def delete_file(request: web.Request) -> web.Response:
            deleted.append(request.match_info["file_id"])
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/upload/v1beta/files", start_upload)
        app.router.add_post("/upload-session", upload)
        app.router.add_delete("/v1beta/files/{file_id}", delete_file)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        try:
            analyzer = GeminiVideoAnalyzer(
                {
                    "bilibili_video": {
                        "gemini": {
                            "api_key": "test-key",
                            "api_base": f"http://127.0.0.1:{port}",
                            "model": "gemini-test",
                            "upload_mode": "Gemini File API",
                            "delete_uploaded_file": True,
                            "timeout_seconds": 30,
                        }
                    }
                }
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "video.mp4"
                path.write_bytes(b"test-video-bytes")
                media = DownloadedMedia(path, Path(temp_dir), "video/mp4")
                async with aiohttp.ClientSession() as session:
                    with self.assertRaises(BilibiliError):
                        await analyzer.analyze(make_info(), media, session)
        finally:
            await runner.cleanup()

        self.assertEqual(deleted, ["failed-file"])


if __name__ == "__main__":
    unittest.main()
