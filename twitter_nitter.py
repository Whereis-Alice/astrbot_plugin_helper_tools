from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

_STATUS_ID_RE = re.compile(r"/status/(\d+)", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class NitterParseError(ValueError):
    pass


class NitterParser:
    """Normalize Nitter HTML into the small FxTwitter-shaped records our service uses."""

    @classmethod
    def parse_post(cls, document: str, base_url: str) -> dict[str, Any]:
        soup = cls._soup(document)
        node = soup.select_one(".main-tweet") or soup.select_one(".timeline-item")
        if not isinstance(node, Tag):
            raise NitterParseError("Nitter did not return a post container")
        post = cls._parse_tweet(node, base_url)
        if not post.get("id"):
            raise NitterParseError("Nitter post did not contain a status ID")
        return post

    @classmethod
    def parse_timeline(cls, document: str, base_url: str) -> list[dict[str, Any]]:
        posts, _ = cls.parse_timeline_page(document, base_url)
        return posts

    @classmethod
    def parse_timeline_page(
        cls,
        document: str,
        base_url: str,
    ) -> tuple[list[dict[str, Any]], str]:
        soup = cls._soup(document)
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in soup.select(".timeline-item"):
            if not isinstance(node, Tag):
                continue
            try:
                post = cls._parse_tweet(node, base_url)
            except NitterParseError:
                continue
            post_id = str(post.get("id") or "")
            if not post_id or post_id in seen:
                continue
            seen.add(post_id)
            posts.append(post)
        next_href = ""
        load_more_links = soup.select(".show-more:not(.timeline-item) a[href]")
        if load_more_links:
            next_href = str(load_more_links[-1].get("href") or "").strip()
        return posts, next_href

    @classmethod
    def parse_profile(
        cls,
        document: str,
        base_url: str,
        *,
        fallback_username: str = "",
    ) -> dict[str, Any]:
        soup = cls._soup(document)
        node = soup.select_one(".profile-card")
        if not isinstance(node, Tag):
            raise NitterParseError("Nitter did not return a profile card")
        account = cls._parse_account(node, base_url, fallback_username=fallback_username)
        if not account.get("screen_name"):
            raise NitterParseError("Nitter profile did not contain a username")
        return account

    @classmethod
    def parse_account_search(cls, document: str, base_url: str) -> list[dict[str, Any]]:
        soup = cls._soup(document)
        accounts: list[dict[str, Any]] = []
        seen: set[str] = set()
        selectors = ".profile-card, .user-card, .search-user, [data-username]"
        for node in soup.select(selectors):
            if not isinstance(node, Tag):
                continue
            is_profile_result = isinstance(node.select_one(".profile-result"), Tag)
            if "timeline-item" in cls._classes(node) and not is_profile_result:
                continue
            account = cls._parse_account(node, base_url)
            username = str(account.get("screen_name") or "").casefold()
            if not username or username in seen:
                continue
            seen.add(username)
            accounts.append(account)
        return accounts

    @staticmethod
    def _soup(document: str) -> BeautifulSoup:
        return BeautifulSoup(document or "", "html.parser")

    @classmethod
    def _parse_tweet(cls, node: Tag, base_url: str) -> dict[str, Any]:
        body = node.select_one(".tweet-body")
        if not isinstance(body, Tag):
            body = node
        username = cls._handle_from_node(body) or cls._handle_from_node(node)
        status_url = cls._status_url(body, base_url) or cls._status_url(node, base_url)
        status_match = _STATUS_ID_RE.search(status_url)
        post_id = status_match.group(1) if status_match else ""
        if not username or not post_id:
            raise NitterParseError("Nitter timeline item was missing author or status ID")
        account = cls._parse_account(body, base_url, fallback_username=username)
        text = cls._text(body.select_one(".tweet-content.media-body, .tweet-content"))
        quote = body.select_one(".quote")
        quote_record: dict[str, Any] | None = None
        if isinstance(quote, Tag):
            quote_account = cls._parse_account(quote, base_url)
            quote_text = cls._text(quote.select_one(".quote-text, .tweet-content"))
            if quote_account.get("screen_name") or quote_text:
                quote_record = {"author": quote_account, "text": quote_text}
        photos, videos = cls._extract_media(body, base_url, quote)
        reposted_by = cls._parse_reposted_by(node, base_url)
        return {
            "type": "status",
            "id": post_id,
            "url": f"https://x.com/{username}/status/{post_id}",
            "text": text,
            "author": account,
            "created_at": cls._date_text(body),
            "possibly_sensitive": cls._contains_sensitive_marker(body),
            "media": {"photos": photos, "videos": videos},
            "quote": quote_record,
            "reposted_by": reposted_by,
        }

    @classmethod
    def _parse_account(
        cls,
        node: Tag,
        base_url: str,
        *,
        fallback_username: str = "",
    ) -> dict[str, Any]:
        username = cls._handle_from_node(node) or fallback_username.lstrip("@")
        name = cls._text(node.select_one(".profile-card-fullname, .fullname"))
        bio = cls._text(node.select_one(".profile-bio, .bio, .profile-result .tweet-content"))
        avatar = ""
        image = node.select_one(
            ".profile-card-avatar img, .profile-avatar img, .tweet-avatar img"
        )
        if isinstance(image, Tag):
            avatar = cls._absolute_url(base_url, image.get("src"))
        followers = cls._stat_count(node, "/followers")
        following = cls._stat_count(node, "/following")
        statuses = cls._stat_count(node, "/media")
        return {
            "type": "profile",
            "screen_name": username,
            "name": name or username,
            "description": bio,
            "url": f"https://x.com/{username}" if username else "",
            "avatar_url": avatar,
            "followers": followers,
            "following": following,
            "statuses": statuses,
            "verification": {"verified": bool(node.select_one(".icon-ok, .verified"))},
            "protected": bool(node.select_one(".protected")),
        }

    @classmethod
    def _parse_reposted_by(
        cls,
        node: Tag,
        base_url: str,
    ) -> dict[str, Any] | None:
        header = node.select_one(".retweet-header")
        if not isinstance(header, Tag):
            return None
        account = cls._parse_account(header, base_url)
        return account if account.get("screen_name") else None

    @classmethod
    def _extract_media(
        cls,
        body: Tag,
        base_url: str,
        quote: Tag | None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        photos: list[dict[str, str]] = []
        videos: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in body.select("a.still-image"):
            if not isinstance(anchor, Tag) or cls._inside_quote(anchor, quote):
                continue
            url = cls._absolute_url(base_url, anchor.get("href"))
            if not url or url in seen:
                continue
            seen.add(url)
            image = anchor.select_one("img")
            alt = image.get("alt", "") if isinstance(image, Tag) else ""
            photos.append({"url": url, "alt_text": str(alt or "")})
        for video in body.select("video[poster]"):
            if not isinstance(video, Tag) or cls._inside_quote(video, quote):
                continue
            url = cls._absolute_url(base_url, video.get("poster"))
            if not url or url in seen:
                continue
            seen.add(url)
            videos.append({"thumbnail_url": url})
        return photos, videos

    @classmethod
    def _handle_from_node(cls, node: Tag) -> str:
        value = str(node.get("data-username") or "").strip().lstrip("@")
        if _HANDLE_RE.fullmatch(value):
            return value
        username_node = node.select_one(
            ".profile-card-username, .username, a[href^='/'][href]:not(.tweet-link)"
        )
        if isinstance(username_node, Tag):
            text = cls._text(username_node).lstrip("@")
            if _HANDLE_RE.fullmatch(text):
                return text
            href = str(username_node.get("href") or "").strip("/")
            first = href.split("/", 1)[0]
            if _HANDLE_RE.fullmatch(first):
                return first
        return ""

    @classmethod
    def _status_url(cls, node: Tag, base_url: str) -> str:
        link = node.select_one(".tweet-date a[href*='/status/'], a.tweet-link[href*='/status/']")
        if isinstance(link, Tag):
            return cls._absolute_url(base_url, link.get("href"))
        return ""

    @staticmethod
    def _date_text(node: Tag) -> str:
        link = node.select_one(".tweet-date a[title], .tweet-published")
        if isinstance(link, Tag):
            return str(link.get("title") or link.get_text(" ", strip=True) or "").strip()
        return ""

    @classmethod
    def _stat_count(cls, node: Tag, suffix: str) -> int | None:
        link = node.select_one(f"a[href$='{suffix}']")
        if not isinstance(link, Tag):
            if suffix == "/media":
                value = cls._text(node.select_one(".posts .profile-stat-num"))
                return cls._parse_count(value)
            return None
        value = cls._text(link.select_one(".profile-stat-num")) or cls._text(link)
        return cls._parse_count(value)

    @staticmethod
    def _parse_count(value: str) -> int | None:
        match = re.search(r"([\d,.]+)\s*([KMB])?", value or "", re.IGNORECASE)
        if not match:
            return None
        number = match.group(1).replace(",", "")
        try:
            parsed = float(number)
        except ValueError:
            return None
        multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
            str(match.group(2) or "").upper(),
            1,
        )
        return max(0, int(parsed * multiplier))

    @classmethod
    def _contains_sensitive_marker(cls, node: Tag) -> bool:
        for item in node.select(".nsfw, .sensitive-content, .sensitive"):
            if isinstance(item, Tag):
                return True
        return any("sensitive" in class_name or "nsfw" in class_name for class_name in cls._classes(node))

    @staticmethod
    def _inside_quote(node: Tag, quote: Tag | None) -> bool:
        return quote is not None and (node is quote or quote in node.parents)

    @staticmethod
    def _classes(node: Tag) -> tuple[str, ...]:
        classes = node.get("class") or []
        return tuple(str(item).casefold() for item in classes)

    @staticmethod
    def _text(node: Tag | None) -> str:
        return node.get_text(" ", strip=True) if isinstance(node, Tag) else ""

    @staticmethod
    def _absolute_url(base_url: str, value: Any) -> str:
        url = str(value or "").strip()
        return urljoin(base_url.rstrip("/") + "/", url) if url else ""
