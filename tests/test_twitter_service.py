from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from astrbot_plugin_helper_tools.twitter_service import (
    TwitterAccount,
    TwitterError,
    TwitterPost,
    TwitterService,
    _resolve_nitter_page_request,
)

POST_HTML = """
<div class="main-tweet">
  <div class="tweet-body" data-username="artist">
    <a class="fullname">Example Artist</a>
    <a class="username" href="/artist">@artist</a>
    <div class="tweet-content">A new illustration</div>
    <div class="tweet-date"><a href="/artist/status/1234567890">date</a></div>
    <a class="still-image" href="/pic/media%2Fexample.jpg"><img alt="illustration" /></a>
  </div>
</div>
"""


def _fx_post_payload() -> dict:
    return {
        "status": {
            "id": "1234567890",
            "text": "A public update",
            "url": "https://x.com/artist/status/1234567890",
            "author": {
                "screen_name": "artist",
                "name": "Example Artist",
                "description": "Illustrator",
            },
            "media": {
                "photos": [
                    {
                        "url": "https://pbs.twimg.com/media/example.jpg?format=jpg",
                        "alt_text": "safe example",
                    }
                ]
            },
        }
    }


class _FallbackTwitterService(TwitterService):
    def __init__(self, config, data_dir: Path) -> None:
        super().__init__(config, data_dir)
        self.nitter_requests = 0
        self.fx_requests = 0

    async def _request_nitter_html(self, *_args, **_kwargs) -> str:
        self.nitter_requests += 1
        raise TwitterError("Nitter unavailable", user_message="Nitter 暂时不可用。")

    async def _request_json(self, *_args, **_kwargs) -> dict:
        self.fx_requests += 1
        return _fx_post_payload()


class _NitterOnlyService(TwitterService):
    async def _request_nitter_html(self, *_args, **_kwargs) -> str:
        return POST_HTML

    async def _request_json(self, *_args, **_kwargs) -> dict:
        raise AssertionError("Nitter-only mode should not request FxTwitter")


class _TimelineService(TwitterService):
    async def _request_json(self, *_args, **_kwargs) -> dict:
        return {
            "results": [
                {
                    "id": "1001",
                    "text": "my new work",
                    "author": {"screen_name": "artist", "name": "Artist"},
                    "media": {"photos": []},
                },
                {
                    "id": "1002",
                    "text": "explicit repost",
                    "author": {"screen_name": "other", "name": "Other"},
                    "reposted_by": {"screen_name": "artist", "name": "Artist"},
                    "media": {"photos": []},
                },
                {
                    "id": "1003",
                    "text": "repost with missing upstream marker",
                    "author": {"screen_name": "another", "name": "Another"},
                    "media": {"photos": []},
                },
            ]
        }


def _nitter_timeline_item(
    post_id: str,
    author: str,
    *,
    reposted_by: str = "",
    sensitive: bool = False,
) -> str:
    repost_header = (
        f'<div class="retweet-header" data-username="{reposted_by}">'
        f'<a class="username" href="/{reposted_by}">@{reposted_by}</a></div>'
        if reposted_by
        else ""
    )
    sensitive_marker = '<div class="sensitive-content"></div>' if sensitive else ""
    return f"""
    <div class="timeline-item">
      {repost_header}
      <div class="tweet-body" data-username="{author}">
        <a class="fullname">{author}</a>
        <a class="username" href="/{author}">@{author}</a>
        <div class="tweet-content">post {post_id}</div>
        {sensitive_marker}
        <div class="tweet-date"><a href="/{author}/status/{post_id}">date</a></div>
      </div>
    </div>
    """


class _PaginatedNitterTimelineService(TwitterService):
    def __init__(self, config, data_dir: Path) -> None:
        super().__init__(config, data_dir)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def _request_nitter_html(self, path: str, *, params=None, **_kwargs) -> str:
        page_params = dict(params or {})
        self.calls.append((path, page_params))
        if not page_params.get("cursor"):
            return "".join(
                (
                    _nitter_timeline_item("2001", "other1", reposted_by="artist"),
                    _nitter_timeline_item("2002", "other2", reposted_by="artist"),
                    _nitter_timeline_item("2003", "artist", sensitive=True),
                    '<div class="show-more"><a href="?cursor=older">Load more</a></div>',
                )
            )
        return "".join(
            (
                _nitter_timeline_item("1002", "artist"),
                _nitter_timeline_item("1001", "artist"),
            )
        )


class _PaginatedFxSearchService(TwitterService):
    def __init__(self, config, data_dir: Path) -> None:
        super().__init__(config, data_dir)
        self.calls: list[dict[str, str]] = []

    async def _request_json(self, _path: str, *, params=None, **_kwargs) -> dict:
        page_params = dict(params or {})
        self.calls.append(page_params)
        if not page_params.get("cursor"):
            return {
                "results": [
                    {
                        "id": "3001",
                        "text": "repost one",
                        "author": {"screen_name": "other1", "name": "Other 1"},
                        "media": {"photos": []},
                    },
                    {
                        "id": "3002",
                        "text": "repost two",
                        "author": {"screen_name": "other2", "name": "Other 2"},
                        "media": {"photos": []},
                    },
                ],
                "cursor": {"bottom": "older-search"},
            }
        return {
            "results": [
                {
                    "id": "2002",
                    "text": "original two",
                    "author": {"screen_name": "artist", "name": "Artist"},
                    "media": {"photos": []},
                },
                {
                    "id": "2001",
                    "text": "original one",
                    "author": {"screen_name": "artist", "name": "Artist"},
                    "media": {"photos": []},
                },
            ],
            "cursor": {"bottom": None},
        }


class _ChunkedContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def read(self, _size: int) -> bytes:
        return self.chunks[0]

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class _ImageResponse:
    def __init__(self, url: str, chunks: list[bytes]) -> None:
        self.status = 200
        self.url = url
        self.headers = {"Content-Type": "image/jpeg"}
        self.content = _ChunkedContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None


class _ImageSession:
    def __init__(self, url: str, chunks: list[bytes]) -> None:
        self.closed = False
        self.url = url
        self.chunks = chunks
        self.calls = 0

    def get(self, *_args, **_kwargs) -> _ImageResponse:
        self.calls += 1
        return _ImageResponse(self.url, self.chunks)


class TwitterServiceTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, **twitter) -> dict:
        return {"twitter": twitter}

    async def test_auto_source_falls_back_from_nitter_to_fxtwitter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _FallbackTwitterService(
                self._config(
                    data_source="自动（优先 Nitter，失败回退 FxTwitter）",
                    nitter_base_url="http://127.0.0.1:8585",
                ),
                Path(temporary),
            )
            result = await service.get_post("https://x.com/artist/status/1234567890")

        self.assertEqual(service.nitter_requests, 1)
        self.assertEqual(service.fx_requests, 1)
        self.assertEqual(result.posts[0].author.username, "artist")

    async def test_nitter_only_can_read_a_post_and_its_proxy_url_is_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _NitterOnlyService(
                self._config(
                    data_source="仅 Nitter",
                    nitter_base_url="http://127.0.0.1:8585",
                ),
                Path(temporary),
            )
            result = await service.get_post("http://127.0.0.1:8585/artist/status/1234567890")

            self.assertEqual(result.posts[0].text, "A new illustration")
            self.assertTrue(
                service._is_allowed_media_url(
                    "http://127.0.0.1:8585/pic/media%2Fexample.jpg"
                )
            )
            self.assertFalse(
                service._is_allowed_media_url(
                    "http://127.0.0.1:8586/pic/media%2Fexample.jpg"
                )
            )

    def test_settings_supports_empty_nitter_and_keeps_auto_mode(self) -> None:
        service = TwitterService(self._config(), Path(tempfile.gettempdir()))
        settings = service.settings()

        self.assertEqual(settings.provider, "auto")
        self.assertEqual(settings.nitter_base_url, "")
        self.assertEqual(settings.nitter_timeout_seconds, 8)
        self.assertFalse(settings.include_reposts)
        self.assertEqual(settings.filtered_result_max_pages, 6)
        self.assertEqual(settings.filtered_result_max_candidates, 120)

    def test_nitter_pagination_rejects_external_or_changed_search_links(self) -> None:
        base = "http://127.0.0.1:8585"
        current = {"f": "tweets", "q": "from:artist"}

        self.assertEqual(
            _resolve_nitter_page_request(
                base,
                "search",
                current,
                "?f=tweets&q=from%3Aartist&cursor=older",
            ),
            ("search", {"f": "tweets", "q": "from:artist", "cursor": "older"}),
        )
        self.assertIsNone(
            _resolve_nitter_page_request(
                base,
                "search",
                current,
                "https://example.com/search?f=tweets&q=from%3Aartist&cursor=older",
            )
        )
        self.assertIsNone(
            _resolve_nitter_page_request(
                base,
                "search",
                current,
                "?f=tweets&q=from%3Aother&cursor=older",
            )
        )

    async def test_account_timeline_excludes_reposts_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _TimelineService(
                self._config(data_source="仅 FxTwitter"),
                Path(temporary),
            )
            originals = await service.get_recent_posts("artist", limit=8)
            with_reposts = await service.get_recent_posts(
                "artist",
                limit=8,
                include_reposts=True,
            )

        self.assertEqual([post.post_id for post in originals.posts], ["1001"])
        self.assertEqual(originals.excluded_repost_count, 2)
        self.assertEqual(len(with_reposts.posts), 3)
        self.assertTrue(with_reposts.posts[1].is_repost)
        self.assertTrue(with_reposts.posts[2].is_repost)
        self.assertEqual(with_reposts.posts[2].reposted_by.username, "artist")
        self.assertIn("原作者：Other @other", with_reposts.posts[1].render())

    async def test_from_search_also_excludes_reposts_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _TimelineService(
                self._config(data_source="仅 FxTwitter"),
                Path(temporary),
            )
            result = await service.search_posts("from:artist", limit=8)

        self.assertEqual([post.post_id for post in result.posts], ["1001"])
        self.assertEqual(result.excluded_repost_count, 2)

    async def test_nitter_timeline_backfills_after_repost_and_r18_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _PaginatedNitterTimelineService(
                self._config(
                    data_source="仅 Nitter",
                    nitter_base_url="http://127.0.0.1:8585",
                    filtered_result_max_pages=4,
                    filtered_result_max_candidates=20,
                ),
                Path(temporary),
            )
            result = await service.get_recent_posts("artist", limit=2)

        self.assertEqual([post.post_id for post in result.posts], ["1002", "1001"])
        self.assertEqual(result.excluded_repost_count, 2)
        self.assertEqual(result.filtered_count, 1)
        self.assertEqual(result.total_count, 5)
        self.assertEqual(
            service.calls,
            [("artist", {}), ("artist", {"cursor": "older"})],
        )

    async def test_fxtwitter_search_uses_bottom_cursor_until_originals_are_full(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _PaginatedFxSearchService(
                self._config(
                    data_source="仅 FxTwitter",
                    filtered_result_max_pages=4,
                    filtered_result_max_candidates=20,
                ),
                Path(temporary),
            )
            result = await service.search_posts("from:artist", limit=2)

        self.assertEqual([post.post_id for post in result.posts], ["2002", "2001"])
        self.assertEqual(result.excluded_repost_count, 2)
        self.assertEqual(len(service.calls), 2)
        self.assertNotIn("cursor", service.calls[0])
        self.assertEqual(service.calls[1]["cursor"], "older-search")

    async def test_image_download_reads_all_chunks_and_validates_the_file(self) -> None:
        buffer = BytesIO()
        Image.new("RGB", (32, 24), (30, 90, 160)).save(buffer, format="JPEG")
        payload = buffer.getvalue()
        url = "http://127.0.0.1:8585/pic/complete.jpg"
        session = _ImageSession(url, [payload[:80], payload[80:]])
        with tempfile.TemporaryDirectory() as temporary:
            service = TwitterService(
                self._config(
                    nitter_base_url="http://127.0.0.1:8585",
                    request_retry_count=0,
                ),
                Path(temporary),
            )
            service._session = session
            downloaded, mime_type = await service._download_image(url)

        self.assertEqual(downloaded, payload)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertEqual(session.calls, 1)

    async def test_truncated_image_is_retried_then_rejected(self) -> None:
        buffer = BytesIO()
        Image.new("RGB", (64, 64), (200, 20, 80)).save(buffer, format="JPEG")
        truncated = buffer.getvalue()[:120]
        url = "http://127.0.0.1:8585/pic/truncated.jpg"
        session = _ImageSession(url, [truncated])
        with tempfile.TemporaryDirectory() as temporary:
            service = TwitterService(
                self._config(
                    nitter_base_url="http://127.0.0.1:8585",
                    request_retry_count=1,
                ),
                Path(temporary),
            )
            service._session = session
            with self.assertRaises(TwitterError):
                await service._download_image(url)

        self.assertEqual(session.calls, 2)

    def test_strict_r18_filter_checks_sensitive_flag_and_editable_keywords(self) -> None:
        service = TwitterService(
            self._config(r18_filter={"mode": "严格过滤（推荐）", "keywords": ["adult"]}),
            Path(tempfile.gettempdir()),
        )
        author = TwitterAccount(username="artist", name="Artist")
        posts = (
            TwitterPost("1", author, "adult illustration", "https://x.com/artist/status/1"),
            TwitterPost(
                "2",
                author,
                "ordinary post",
                "https://x.com/artist/status/2",
                sensitive=True,
            ),
            TwitterPost("3", author, "safe post", "https://x.com/artist/status/3"),
        )

        safe, filtered = service._filter_posts(posts)

        self.assertEqual([item.post_id for item in safe], ["3"])
        self.assertEqual(filtered, 2)


if __name__ == "__main__":
    unittest.main()
