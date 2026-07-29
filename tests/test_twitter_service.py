from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_helper_tools.twitter_service import (
    TwitterAccount,
    TwitterError,
    TwitterPost,
    TwitterService,
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
