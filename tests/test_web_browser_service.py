from __future__ import annotations

import unittest

from astrbot_plugin_helper_tools.web_browser_service import (
    WEB_BROWSER_RESULT_MARKER,
    WebBrowserError,
    WebBrowserSafetyPolicy,
    WebBrowserService,
    WebPageResult,
    _clean_page_text,
    _domain_matches,
    _non_public_ip_reason,
)


class _FakeResponse:
    status = 200


class _FakeLocator:
    @property
    def first(self) -> _FakeLocator:
        return self

    async def get_attribute(self, _name: str) -> str:
        return "A short page description"

    async def inner_text(self) -> str:
        return "A readable page body"


class _FakePage:
    url = "https://example.test/final"

    def __init__(self) -> None:
        self.route_handler = None
        self.navigation_timeout = 0
        self.default_timeout = 0

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    async def route(self, _pattern: str, handler) -> None:
        self.route_handler = handler

    async def goto(self, _url: str, **_kwargs) -> _FakeResponse:
        return _FakeResponse()

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def title(self) -> str:
        return "Example title"

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator()

    async def screenshot(self, **_kwargs) -> bytes:
        return b"jpeg-bytes"


class _FakeBrowserContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> _FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, browser_context: _FakeBrowserContext) -> None:
        self.browser_context = browser_context

    async def new_context(self, **_kwargs) -> _FakeBrowserContext:
        return self.browser_context


class WebBrowserSafetyPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_missing_or_unsafe_url_schemes(self) -> None:
        policy = WebBrowserSafetyPolicy()
        for value in ("", "example.com", "file:///etc/passwd", "javascript:alert(1)"):
            with self.subTest(value=value), self.assertRaises(WebBrowserError):
                await policy.validate(value)

    async def test_rejects_localhost_before_dns_lookup(self) -> None:
        policy = WebBrowserSafetyPolicy()
        with self.assertRaises(WebBrowserError) as context:
            await policy.validate("http://localhost:8080/admin")
        self.assertIn("本机或内网", context.exception.user_message)

    async def test_rejects_private_ip_literals(self) -> None:
        policy = WebBrowserSafetyPolicy()
        for value in ("http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/"):
            with self.subTest(value=value), self.assertRaises(WebBrowserError):
                await policy.validate(value)

    async def test_rejects_zero_port_before_browser_launch(self) -> None:
        policy = WebBrowserSafetyPolicy(allow_private_network=True)
        with self.assertRaises(WebBrowserError) as context:
            await policy.validate("https://example.test:0/")
        self.assertIn("端口", context.exception.user_message)

    async def test_domain_allow_and_block_rules_precede_network_access(self) -> None:
        policy = WebBrowserSafetyPolicy(
            allowed_domains=("example.com",),
            blocked_domains=("blocked.example.com",),
        )
        with self.assertRaises(WebBrowserError) as blocked:
            await policy.validate("https://blocked.example.com/")
        self.assertIn("黑名单", blocked.exception.user_message)
        with self.assertRaises(WebBrowserError) as missing:
            await policy.validate("https://not-allowed.invalid/")
        self.assertIn("白名单", missing.exception.user_message)

    async def test_browse_uses_fresh_context_and_returns_page_evidence(self) -> None:
        service = WebBrowserService(
            {
                "web_browser": {
                    "enabled": True,
                    "allow_private_network": True,
                    "extra_wait_ms": 0,
                }
            }
        )
        page = _FakePage()
        browser_context = _FakeBrowserContext(page)
        browser = _FakeBrowser(browser_context)

        async def ensure_browser(_settings):
            return browser

        service._ensure_browser = ensure_browser  # type: ignore[method-assign]
        result = await service.browse("https://example.test/path")

        self.assertEqual(result.status, 200)
        self.assertEqual(result.final_url, "https://example.test/final")
        self.assertEqual(result.screenshot, b"jpeg-bytes")
        self.assertIn("readable page body", result.text)
        self.assertTrue(browser_context.closed)
        self.assertEqual(page.navigation_timeout, 25000)
        self.assertIsNotNone(page.route_handler)


class WebBrowserFormattingTests(unittest.TestCase):
    def test_domain_matching_supports_subdomains_and_wildcards(self) -> None:
        self.assertTrue(_domain_matches("docs.example.com", "example.com"))
        self.assertTrue(_domain_matches("docs.example.com", "*.example.com"))
        self.assertFalse(_domain_matches("example.com", "*.example.com"))
        self.assertFalse(_domain_matches("notexample.com", "example.com"))

    def test_non_public_address_detection(self) -> None:
        import ipaddress

        self.assertTrue(_non_public_ip_reason(ipaddress.ip_address("127.0.0.1")))
        self.assertTrue(_non_public_ip_reason(ipaddress.ip_address("100.64.0.1")))
        self.assertEqual(_non_public_ip_reason(ipaddress.ip_address("8.8.8.8")), "")

    def test_page_text_keeps_head_and_tail_when_truncated(self) -> None:
        text = _clean_page_text("head " + ("x" * 100) + " tail", 40)
        self.assertIn("head", text)
        self.assertIn("tail", text)
        self.assertIn("中间内容已截断", text)

    def test_web_result_is_marked_as_temporary_and_untrusted(self) -> None:
        rendered = WebPageResult(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            title="Example",
            description="",
            status=200,
            text="网页正文",
        ).render()
        self.assertIn(WEB_BROWSER_RESULT_MARKER, rendered)
        self.assertIn("不可信资料", rendered)

    def test_module_default_is_disabled(self) -> None:
        self.assertFalse(WebBrowserService({}).enabled())
