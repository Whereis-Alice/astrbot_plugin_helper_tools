from __future__ import annotations

import unittest

from astrbot_plugin_helper_tools.twitter_nitter import NitterParser

POST_HTML = """
<div class="main-tweet">
  <div class="tweet-body" data-username="artist">
    <a class="fullname">Example Artist</a>
    <a class="username" href="/artist">@artist</a>
    <div class="tweet-content">A new illustration</div>
    <div class="tweet-date"><a href="/artist/status/1234567890" title="Jul 29, 2026">date</a></div>
    <a class="still-image" href="/pic/media%2Fexample.jpg"><img alt="illustration" /></a>
  </div>
</div>
"""

PROFILE_HTML = """
<div class="profile-card" data-username="artist">
  <a class="profile-card-avatar"><img src="/pic/avatar.jpg" /></a>
  <a class="profile-card-fullname">Example Artist</a>
  <a class="profile-card-username" href="/artist">@artist</a>
  <div class="profile-bio">Illustrator and streamer</div>
  <a href="/artist/followers"><span class="profile-stat-num">1.2K</span></a>
  <a href="/artist/following"><span class="profile-stat-num">34</span></a>
  <li class="posts"><span class="profile-stat-num">567</span></li>
</div>
"""

USER_SEARCH_HTML = """
<div class="timeline-item" data-username="artist">
  <a class="tweet-link" href="/artist"></a>
  <div class="tweet-body profile-result">
    <div class="tweet-header">
      <a class="tweet-avatar" href="/artist"><img src="/pic/avatar.jpg" /></a>
      <div class="tweet-name-row">
        <div class="fullname-and-username">
          <a class="fullname" href="/artist">Example Artist</a>
        </div>
        <a class="username" href="/artist">@artist</a>
      </div>
    </div>
    <div class="tweet-content media-body">Illustrator and streamer</div>
  </div>
</div>
"""

RETWEET_HTML = """
<div class="timeline-item">
  <div class="retweet-header"><a href="/artist">Example Artist retweeted</a></div>
  <div class="tweet-body" data-username="other_artist">
    <a class="fullname">Other Artist</a>
    <a class="username" href="/other_artist">@other_artist</a>
    <div class="tweet-content">Someone else's illustration</div>
    <div class="tweet-date"><a href="/other_artist/status/9876543210">date</a></div>
    <a class="still-image" href="/pic/media%2Fother.jpg"><img alt="other work" /></a>
  </div>
</div>
"""


class NitterParserTests(unittest.TestCase):
    def test_parses_a_post_and_nitter_media_proxy_url(self) -> None:
        post = NitterParser.parse_post(POST_HTML, "http://127.0.0.1:8585")

        self.assertEqual(post["id"], "1234567890")
        self.assertEqual(post["author"]["screen_name"], "artist")
        self.assertEqual(post["text"], "A new illustration")
        self.assertEqual(post["media"]["photos"][0]["url"], "http://127.0.0.1:8585/pic/media%2Fexample.jpg")

    def test_parses_profile_and_account_search(self) -> None:
        profile = NitterParser.parse_profile(
            PROFILE_HTML,
            "http://127.0.0.1:8585",
            fallback_username="artist",
        )
        accounts = NitterParser.parse_account_search(PROFILE_HTML, "http://127.0.0.1:8585")

        self.assertEqual(profile["screen_name"], "artist")
        self.assertEqual(profile["followers"], 1200)
        self.assertEqual(profile["statuses"], 567)
        self.assertEqual(profile["avatar_url"], "http://127.0.0.1:8585/pic/avatar.jpg")
        self.assertEqual([item["screen_name"] for item in accounts], ["artist"])

    def test_parses_real_nitter_user_search_timeline_items(self) -> None:
        accounts = NitterParser.parse_account_search(
            USER_SEARCH_HTML,
            "http://127.0.0.1:8585",
        )

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["screen_name"], "artist")
        self.assertEqual(accounts[0]["description"], "Illustrator and streamer")

    def test_parses_timeline_items(self) -> None:
        posts = NitterParser.parse_timeline(
            POST_HTML.replace("main-tweet", "timeline-item"),
            "http://127.0.0.1:8585",
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["author"]["screen_name"], "artist")

    def test_marks_nitter_retweets_and_keeps_the_original_author(self) -> None:
        posts = NitterParser.parse_timeline(
            RETWEET_HTML,
            "http://127.0.0.1:8585",
        )

        self.assertEqual(posts[0]["author"]["screen_name"], "other_artist")
        self.assertEqual(posts[0]["reposted_by"]["screen_name"], "artist")
        self.assertIn("other.jpg", posts[0]["media"]["photos"][0]["url"])


if __name__ == "__main__":
    unittest.main()
