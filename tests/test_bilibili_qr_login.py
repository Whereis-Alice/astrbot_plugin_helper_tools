from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import brotli
from aiohttp import web

from astrbot_plugin_helper_tools.bilibili_qr_login import (
    BilibiliCredentialStore,
    BilibiliQrLoginApiResponseError,
    BilibiliQrLoginService,
    QrLoginOutcome,
)
from astrbot_plugin_helper_tools.bilibili_service import BilibiliVideoService


class BilibiliCredentialStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_qr_credentials_are_used_by_default_and_can_be_deprioritized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = BilibiliCredentialStore(root)
            await store.save_cookie_pairs(
                {"SESSDATA": "qr-secret", "bili_jct": "qr-csrf"}
            )

            preferred = BilibiliVideoService(
                {"bilibili_video": {"cookie": "SESSDATA=manual-secret"}},
                root,
            )
            self.assertIn("SESSDATA=qr-secret", preferred._cookie_header())
            self.assertEqual(preferred._cookie_source(), store.source_label)

            manual_first = BilibiliVideoService(
                {
                    "bilibili_video": {
                        "cookie": "SESSDATA=manual-secret",
                        "qr_login": {"prefer_saved_credentials": False},
                    }
                },
                root,
            )
            self.assertIn("SESSDATA=manual-secret", manual_first._cookie_header())
            self.assertEqual(manual_first._cookie_source(), "配置文本")

            self.assertTrue(await store.clear())
            self.assertFalse(store.has_credentials())


class BilibiliQrLoginTests(unittest.IsolatedAsyncioTestCase):
    async def _start_server(self, app: web.Application) -> tuple[web.AppRunner, int]:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    async def test_decodes_brotli_json_when_transport_does_not(self) -> None:
        payload = {"code": 0, "data": {"code": 86101, "message": "not scanned"}}
        body = brotli.compress(json.dumps(payload).encode("utf-8"))

        class ResponseContent:
            async def read(self, _limit: int) -> bytes:
                return body

        class Response:
            status = 200

            def __init__(self) -> None:
                self.headers = {
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Encoding": "br",
                }
                self.content = ResponseContent()

        parsed = await BilibiliQrLoginService._read_json_response(Response())

        self.assertEqual(parsed, payload)
        self.assertEqual(
            BilibiliQrLoginService._request_headers()["Accept-Encoding"],
            "gzip, deflate",
        )

    async def test_qr_login_saves_cookies_without_sending_existing_cookie(self) -> None:
        requests: list[tuple[str, str]] = []
        poll_count = 0

        async def generate(request: web.Request) -> web.Response:
            requests.append(("generate", request.headers.get("Cookie", "")))
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/sso/scan?auth_code=test",
                        "qrcode_key": "test-key",
                    },
                }
            )

        async def poll(request: web.Request) -> web.Response:
            nonlocal poll_count
            requests.append(("poll", request.headers.get("Cookie", "")))
            self.assertEqual(request.query.get("qrcode_key"), "test-key")
            poll_count += 1
            if poll_count == 1:
                return web.json_response(
                    {"code": 0, "data": {"code": 86101, "message": "not scanned"}}
                )
            response = web.json_response(
                {"code": 0, "data": {"code": 0, "message": "success"}}
            )
            response.headers.add("Set-Cookie", "SESSDATA=qr-secret; Path=/")
            response.headers.add("Set-Cookie", "bili_jct=qr-csrf; Path=/")
            return response

        app = web.Application()
        app.router.add_get("/generate", generate)
        app.router.add_get("/poll", poll)
        runner, port = await self._start_server(app)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = {
            "bilibili_video": {
                "cookie": "SESSDATA=old-secret",
                "qr_login": {"poll_interval_seconds": 1, "timeout_seconds": 30},
            }
        }
        credentials = BilibiliCredentialStore(root)
        service = BilibiliQrLoginService(config, root, credentials)

        try:
            with (
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_GENERATE_ENDPOINT",
                    f"http://127.0.0.1:{port}/generate",
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_POLL_ENDPOINT",
                    f"http://127.0.0.1:{port}/poll",
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.BILIBILI_COOKIE_URL",
                    f"http://127.0.0.1:{port}/",
                ),
            ):
                started = await service.start_login()
                self.assertFalse(started.reused_existing_qr)
                self.assertTrue(started.qr_image_path.is_file())
                outcome = await service.wait_for_login(started)
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(outcome.status, "success")
        self.assertIn("SESSDATA=qr-secret", credentials.cookie_header())
        self.assertIn("bili_jct=qr-csrf", credentials.cookie_header())
        self.assertTrue(requests)
        self.assertTrue(all(not cookie for _kind, cookie in requests))

    async def test_expired_qr_is_not_saved(self) -> None:
        async def generate(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/sso/scan?auth_code=test",
                        "qrcode_key": "expired-key",
                    },
                }
            )

        async def poll(_request: web.Request) -> web.Response:
            return web.json_response(
                {"code": 0, "data": {"code": 86038, "message": "expired"}}
            )

        app = web.Application()
        app.router.add_get("/generate", generate)
        app.router.add_get("/poll", poll)
        runner, port = await self._start_server(app)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        credentials = BilibiliCredentialStore(root)
        service = BilibiliQrLoginService({}, root, credentials)

        try:
            with (
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_GENERATE_ENDPOINT",
                    f"http://127.0.0.1:{port}/generate",
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_POLL_ENDPOINT",
                    f"http://127.0.0.1:{port}/poll",
                ),
            ):
                started = await service.start_login()
                outcome = await service.wait_for_login(started)
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(outcome.status, "expired")
        self.assertFalse(credentials.has_credentials())

    async def test_cancelled_login_finishes_without_deleting_existing_credentials(self) -> None:
        async def generate(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/sso/scan?auth_code=test",
                        "qrcode_key": "cancel-key",
                    },
                }
            )

        async def poll(_request: web.Request) -> web.Response:
            return web.json_response(
                {"code": 0, "data": {"code": 86101, "message": "not scanned"}}
            )

        app = web.Application()
        app.router.add_get("/generate", generate)
        app.router.add_get("/poll", poll)
        runner, port = await self._start_server(app)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        credentials = BilibiliCredentialStore(root)
        await credentials.save_cookie_pairs({"SESSDATA": "existing"})
        service = BilibiliQrLoginService(
            {"bilibili_video": {"qr_login": {"poll_interval_seconds": 15}}},
            root,
            credentials,
        )

        try:
            with (
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_GENERATE_ENDPOINT",
                    f"http://127.0.0.1:{port}/generate",
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_POLL_ENDPOINT",
                    f"http://127.0.0.1:{port}/poll",
                ),
            ):
                started = await service.start_login()
                await asyncio.sleep(0)
                reused = await service.start_login()
                self.assertTrue(reused.reused_existing_qr)
                self.assertIs(reused.task, started.task)
                self.assertTrue(await service.cancel_login_and_wait())
                outcome = await service.wait_for_login(started)
                renewed = await service.start_login()
                self.assertFalse(renewed.reused_existing_qr)
                self.assertIsNot(renewed.task, started.task)
                self.assertTrue(await service.cancel_login_and_wait())
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(outcome.status, "cancelled")
        self.assertIn("SESSDATA=existing", credentials.cookie_header())

    async def test_html_poll_response_explains_proxy_or_interception(self) -> None:
        async def generate(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/sso/scan?auth_code=test",
                        "qrcode_key": "html-key",
                    },
                }
            )

        async def poll(_request: web.Request) -> web.Response:
            return web.Response(
                text="<html><body>blocked</body></html>",
                content_type="text/html",
            )

        app = web.Application()
        app.router.add_get("/generate", generate)
        app.router.add_get("/poll", poll)
        runner, port = await self._start_server(app)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        service = BilibiliQrLoginService(
            {
                "bilibili_video": {
                    "qr_login": {"direct_retry_on_invalid_response": False},
                }
            },
            root,
            BilibiliCredentialStore(root),
        )

        try:
            with (
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_GENERATE_ENDPOINT",
                    f"http://127.0.0.1:{port}/generate",
                ),
                patch(
                    "astrbot_plugin_helper_tools.bilibili_qr_login.QR_POLL_ENDPOINT",
                    f"http://127.0.0.1:{port}/poll",
                ),
            ):
                started = await service.start_login()
                outcome = await service.wait_for_login(started)
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(outcome.status, "failed")
        self.assertIn("网页而不是 JSON", outcome.message)
        self.assertIn("代理", outcome.message)

    async def test_invalid_poll_response_retries_without_system_proxy(self) -> None:
        class ProxyFallbackService(BilibiliQrLoginService):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.poll_trust_env: list[bool] = []

            async def _poll_login_with_session(self, active, session):
                self.poll_trust_env.append(bool(session._trust_env))
                if len(self.poll_trust_env) == 1:
                    raise BilibiliQrLoginApiResponseError("unexpected proxy response")
                return QrLoginOutcome("cancelled", "test complete")

        async def generate(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/sso/scan?auth_code=test",
                        "qrcode_key": "retry-key",
                    },
                }
            )

        app = web.Application()
        app.router.add_get("/generate", generate)
        runner, port = await self._start_server(app)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        service = ProxyFallbackService({}, root, BilibiliCredentialStore(root))

        try:
            with patch(
                "astrbot_plugin_helper_tools.bilibili_qr_login.QR_GENERATE_ENDPOINT",
                f"http://127.0.0.1:{port}/generate",
            ):
                started = await service.start_login()
                outcome = await service.wait_for_login(started)
        finally:
            await service.close()
            await runner.cleanup()

        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(service.poll_trust_env, [True, False])
