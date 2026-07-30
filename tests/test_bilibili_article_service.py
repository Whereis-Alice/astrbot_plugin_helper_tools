from __future__ import annotations

import base64
import io
import json
import unittest
from types import SimpleNamespace

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import ImageURLPart, TextPart
from PIL import Image

from astrbot_plugin_helper_tools.bilibili_article_service import (
    ARTICLE_RESOLVED_ATTR,
    BILIBILI_ARTICLE_CONTEXT_PREFIX,
    BilibiliArticleContext,
    BilibiliArticleDocument,
    BilibiliArticleError,
    BilibiliArticleService,
    extract_article_reference,
    request_has_bilibili_article_context,
)
from astrbot_plugin_helper_tools.reply_card_reader import ReplyCardReader


class DummyEvent:
    def __init__(self, chain: list[object] | None = None, message_str: str = "") -> None:
        self._chain = chain or []
        self.message_str = message_str
        self.message_obj = SimpleNamespace(message_str=message_str)

    def get_messages(self) -> list[object]:
        return self._chain


class FakeVideoService:
    def _request_headers(self) -> dict[str, str]:
        return {"User-Agent": "test"}


def make_service(config: dict[str, object] | None = None) -> BilibiliArticleService:
    return BilibiliArticleService(
        {"bilibili_article": config or {}},
        FakeVideoService(),
        ReplyCardReader({}),
    )


class BilibiliArticleReferenceTests(unittest.TestCase):
    def test_extracts_article_id_from_url_and_bare_path(self) -> None:
        direct = extract_article_reference("https://www.bilibili.com/read/cv123456")
        bare = extract_article_reference("请查看 /read/cv123456 的专栏")

        self.assertIsNotNone(direct)
        self.assertEqual(direct.article_id, "123456")
        self.assertEqual(bare.url, "https://www.bilibili.com/read/cv123456")

    def test_rejects_lookalike_domain(self) -> None:
        self.assertIsNone(
            extract_article_reference("https://evilbilibili.com/read/cv123456")
        )

    def test_reads_article_url_from_quoted_json_card(self) -> None:
        card = Comp.Json(
            data={
                "app": "com.tencent.miniapp_01",
                "desc": "哔哩哔哩专栏",
                "meta": {
                    "detail_1": {
                        "title": "专栏标题",
                        "qqdocurl": "https://www.bilibili.com/read/cv123456",
                        "preview": "https://i0.hdslb.com/bfs/article/cover.jpg",
                    }
                },
            }
        )
        event = DummyEvent([Comp.Reply(id="1", chain=[card])])
        service = make_service()

        reference = service.prepare_event(event)

        self.assertIsNotNone(reference)
        self.assertEqual(reference.article_id, "123456")
        self.assertEqual(
            reference.fallback_cover_url,
            "https://i0.hdslb.com/bfs/article/cover.jpg",
        )


class BilibiliArticleParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_official_api_json(self) -> None:
        service = make_service()
        reference = extract_article_reference("https://www.bilibili.com/read/cv123456")

        async def request_bytes(url: str, **_kwargs: object) -> tuple[bytes, str, str]:
            self.assertEqual(url, "https://api.bilibili.com/x/article/viewinfo")
            return (
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "title": "接口标题",
                            "author_name": "作者 Alice",
                            "summary": "文章摘要",
                            "content": "<p>正文第一段</p><img alt='插图'>",
                            "banner_url": "https://i0.hdslb.com/cover.png",
                        },
                    }
                ).encode(),
                "application/json",
                reference.url,
            )

        service._request_bytes = request_bytes
        document = await service._fetch_api_document(reference)

        self.assertEqual(document.title, "接口标题")
        self.assertEqual(document.author, "作者 Alice")
        self.assertIn("正文第一段", document.content)
        self.assertIn("[文章图片：插图]", document.content)
        self.assertEqual(document.cover_url, "https://i0.hdslb.com/cover.png")

    async def test_falls_back_to_article_html(self) -> None:
        service = make_service()
        reference = extract_article_reference("https://www.bilibili.com/read/cv123456")

        async def request_bytes(_url: str, **_kwargs: object) -> tuple[bytes, str, str]:
            return (
                """<html><head>
                <meta property='og:title' content='网页标题'>
                <meta property='og:description' content='网页摘要'>
                <meta property='og:image' content='https://i0.hdslb.com/cover.jpg'>
                </head><body><div class='article-content'><p>网页正文</p></div></body></html>""".encode(),
                "text/html",
                reference.url,
            )

        service._request_bytes = request_bytes
        document = await service._fetch_page_document(reference)

        self.assertEqual(document.title, "网页标题")
        self.assertEqual(document.summary, "网页摘要")
        self.assertEqual(document.content, "网页正文")
        self.assertEqual(document.cover_url, "https://i0.hdslb.com/cover.jpg")

    async def test_short_video_link_is_not_reported_as_article(self) -> None:
        service = make_service()
        event = DummyEvent(message_str="https://b23.tv/video-link")

        async def request_bytes(_url: str, **_kwargs: object) -> tuple[bytes, str, str]:
            return b"", "text/html", "https://www.bilibili.com/video/BV123"

        service._request_bytes = request_bytes
        context = await service.context_for_event_result(event)

        self.assertEqual(context.text, "")
        self.assertFalse(getattr(event, ARTICLE_RESOLVED_ATTR))

    async def test_main_video_handler_skips_a_resolved_article(self) -> None:
        from astrbot_plugin_helper_tools.main import HelperToolsPlugin

        class FakeBilibili:
            def __init__(self) -> None:
                self.calls = 0

            def auto_parse_mode(self) -> str:
                return "follow"

            async def context_for_event_result(self, _event: object) -> None:
                self.calls += 1

        bilibili = FakeBilibili()
        plugin = SimpleNamespace(
            config={"bilibili_video": {"enabled": True}},
            bilibili=bilibili,
            enabled=lambda: True,
        )
        event = SimpleNamespace(**{ARTICLE_RESOLVED_ATTR: True})
        request = SimpleNamespace(prompt="", extra_user_content_parts=[])

        await HelperToolsPlugin.bilibili_video_context_handler(plugin, event, request)

        self.assertEqual(bilibili.calls, 0)

    async def test_cover_is_validated_and_converted_to_data_url(self) -> None:
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format="PNG")
        image_bytes = output.getvalue()
        service = make_service()

        async def request_bytes(_url: str, **_kwargs: object) -> tuple[bytes, str, str]:
            return image_bytes, "image/png", "https://i0.hdslb.com/cover.png"

        service._request_bytes = request_bytes
        data_url = await service._fetch_cover_data_url(
            "https://i0.hdslb.com/cover.png"
        )

        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(
            base64.b64decode(data_url.split(",", 1)[1]),
            image_bytes,
        )

    async def test_response_size_limit_is_enforced(self) -> None:
        service = make_service({"max_response_bytes": 65536})

        class FakeRequest:
            async def __aenter__(self) -> SimpleNamespace:
                return SimpleNamespace(
                    headers={"Content-Length": "65537"},
                    status=200,
                    content=None,
                    url="https://www.bilibili.com/read/cv123456",
                )

            async def __aexit__(self, *_args: object) -> None:
                return None

        class FakeSession:
            def get(self, *_args: object, **_kwargs: object) -> FakeRequest:
                return FakeRequest()

        async def get_session() -> FakeSession:
            return FakeSession()

        service._get_session = get_session
        with self.assertRaises(BilibiliArticleError):
            await service._request_bytes(
                "https://www.bilibili.com/read/cv123456",
                max_bytes=service.max_response_bytes(),
            )

    async def test_configurable_article_limit_keeps_head_and_tail(self) -> None:
        service = make_service({"max_article_chars": 1000})
        document = BilibiliArticleDocument(
            url="https://www.bilibili.com/read/cv123456",
            title="标题",
            author="作者",
            summary="摘要",
            content="开头" + "中间" * 800 + "结尾",
        )

        rendered = service._render_document(document, cover_attached=False)

        self.assertIn("开头", rendered)
        self.assertIn("结尾", rendered)
        self.assertIn("正文中间内容已按长度限制省略", rendered)
        body = rendered.split("专栏正文（可能已按配置截断）：", 1)[1].split(
            "回答边界：", 1
        )[0]
        self.assertLessEqual(len(body), 1050)


class BilibiliArticleContextHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_handler_injects_temporary_text_and_cover(self) -> None:
        cover = "data:image/png;base64,AAAA"

        class FakeArticleService:
            async def context_for_event_result(self, _event: object) -> BilibiliArticleContext:
                return BilibiliArticleContext(
                    f"{BILIBILI_ARTICLE_CONTEXT_PREFIX}\n正文资料",
                    cover_data_url=cover,
                )

        from astrbot_plugin_helper_tools.main import HelperToolsPlugin

        plugin = SimpleNamespace(
            config={"bilibili_article": {"enabled": True}},
            bilibili_article=FakeArticleService(),
            enabled=lambda: True,
        )
        event = SimpleNamespace(is_stopped=lambda: False)
        request = SimpleNamespace(prompt="原消息", extra_user_content_parts=[])

        await HelperToolsPlugin.bilibili_article_context_handler(plugin, event, request)

        self.assertEqual(len(request.extra_user_content_parts), 2)
        self.assertIsInstance(request.extra_user_content_parts[0], TextPart)
        self.assertIsInstance(request.extra_user_content_parts[1], ImageURLPart)
        self.assertTrue(
            all(
                getattr(part, "_no_save", False)
                for part in request.extra_user_content_parts
            )
        )
        self.assertTrue(request_has_bilibili_article_context(request))

    def test_request_marker_is_detected_in_prompt_or_parts(self) -> None:
        self.assertTrue(
            request_has_bilibili_article_context(
                SimpleNamespace(
                    prompt=f"{BILIBILI_ARTICLE_CONTEXT_PREFIX}\n资料",
                    extra_user_content_parts=[],
                )
            )
        )
        self.assertTrue(
            request_has_bilibili_article_context(
                SimpleNamespace(
                    prompt="",
                    extra_user_content_parts=[
                        {"type": "text", "text": "[B站专栏解析失败]\n原因：测试"}
                    ],
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
