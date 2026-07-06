from __future__ import annotations

import base64
import html
import re
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .helper_utils import (
    RollingRateLimiter,
    cfg,
    clean_text,
    core_wake_prefixes,
    expand_wake_prefixed_commands,
    fetch_bytes,
    fetch_json,
    parse_dynamic_command,
    quote_query,
    read_bool,
    read_float,
    read_int,
    truncate,
)


STEAM_TOOL_NAME = "search_steam_game"
STEAM_APP_URL_RE = re.compile(r"https?://store\.steampowered\.com/app/(\d+)(?:/|$|\?)", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def extract_steam_appid(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.isdigit():
        return text
    match = STEAM_APP_URL_RE.search(text)
    return match.group(1) if match else ""


def strip_html(value: Any) -> str:
    text = html.unescape(clean_text(value))
    text = HTML_TAG_RE.sub("", text)
    return " ".join(text.split())


def join_names(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    names: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            text = clean_text(item.get("description") or item.get("name"))
            if text:
                names.append(text)
    return "、".join(names)


def format_price(item: dict[str, Any]) -> str:
    price = item.get("price_overview")
    if not isinstance(price, dict):
        if item.get("is_free"):
            return "免费"
        return ""
    final_formatted = clean_text(price.get("final_formatted"))
    discount = price.get("discount_percent")
    if final_formatted and discount:
        return f"{final_formatted} (-{discount}%)"
    return final_formatted


class SteamService:
    def __init__(self, config: Any, context: Any | None = None) -> None:
        self.config = config
        self.context = context
        self._limiter = RollingRateLimiter(
            window_seconds=read_int(cfg(config, "steam", "rate_window_seconds", 600), 600, minimum=1),
            max_requests=read_int(cfg(config, "steam", "rate_max_requests", 300), 300, minimum=1),
        )

    def enabled(self) -> bool:
        return read_bool(cfg(self.config, "steam", "enabled", True), True)

    def tool_enabled(self) -> bool:
        return read_bool(cfg(self.config, "steam", "llm_tool_enabled", True), True)

    def commands_enabled(self) -> bool:
        return read_bool(cfg(self.config, "steam", "commands_enabled", True), True)

    def auto_parse_links(self) -> bool:
        return read_bool(cfg(self.config, "steam", "auto_parse_links", True), True)

    def stop_after_response(self) -> bool:
        return read_bool(cfg(self.config, "steam", "stop_event_after_response", False), False)

    def timeout(self) -> float:
        return read_float(cfg(self.config, "steam", "http_timeout_seconds", 8.0), 8.0, minimum=2.0, maximum=60.0)

    def cc(self) -> str:
        return clean_text(cfg(self.config, "steam", "country_code", "CN"), "CN")

    def command_prefixes(self) -> list[str]:
        value = cfg(self.config, "steam", "command_prefixes", ["steam", "查找"])
        if isinstance(value, list):
            return [clean_text(item) for item in value if clean_text(item)] or ["steam"]
        return ["steam"]

    def command_aliases(self) -> list[str]:
        return expand_wake_prefixed_commands(self.command_prefixes(), core_wake_prefixes(self.context))

    def show_header_image(self) -> bool:
        return read_bool(cfg(self.config, "steam", "show_header_image", True), True)

    def max_description_chars(self) -> int:
        return read_int(cfg(self.config, "steam", "max_description_chars", 500), 500, minimum=80, maximum=3000)

    def max_search_results(self) -> int:
        return read_int(cfg(self.config, "steam", "max_search_results", 5), 5, minimum=1, maximum=20)

    async def _check_rate_limit(self) -> str:
        allowed, retry_after = await self._limiter.allow()
        if allowed:
            return ""
        template = clean_text(
            cfg(self.config, "steam", "rate_limited_text", "Steam 查询太频繁了，请 {seconds}s 后再试。"),
            "Steam 查询太频繁了，请 {seconds}s 后再试。",
        )
        return template.replace("{seconds}", str(int(retry_after)))

    async def fetch_app_details(self, appid: int) -> dict[str, Any]:
        last_error = ""
        for lang in ("schinese", "tchinese", "english"):
            url = (
                "https://store.steampowered.com/api/appdetails"
                f"?appids={appid}&l={lang}&cc={self.cc()}"
            )
            try:
                payload = await fetch_json(url, timeout_seconds=self.timeout())
                node = payload.get(str(appid)) if isinstance(payload, dict) else None
                if isinstance(node, dict) and node.get("success") and isinstance(node.get("data"), dict):
                    data = dict(node["data"])
                    data["_language"] = lang
                    return data
                last_error = "Steam API 返回 success=false 或 data 为空。"
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(last_error or "Steam API 查询失败。")

    async def search_store(self, query: str, max_results: int | None = None) -> list[dict[str, Any]]:
        limit = max_results or self.max_search_results()
        url = (
            "https://store.steampowered.com/api/storesearch/"
            f"?term={quote_query(query)}&l=schinese&cc={self.cc()}"
        )
        payload = await fetch_json(url, timeout_seconds=self.timeout())
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [item for item in items[:limit] if isinstance(item, dict)]

    async def resolve_query(self, query: str) -> tuple[int | None, list[dict[str, Any]], str]:
        appid = extract_steam_appid(query)
        if appid:
            return int(appid), [], ""
        if not clean_text(query):
            return None, [], "请提供 Steam AppID、商店链接或游戏关键词。"
        try:
            results = await self.search_store(query)
        except Exception as exc:
            return None, [], f"Steam 搜索失败: {exc}"
        if not results:
            return None, [], "没有搜索到 Steam 游戏。"
        first_id = results[0].get("id")
        if isinstance(first_id, int):
            return first_id, results, ""
        if clean_text(first_id).isdigit():
            return int(first_id), results, ""
        return None, results, "搜索结果里没有可用 AppID。"

    def format_search_results(self, results: list[dict[str, Any]]) -> str:
        lines = ["Steam 搜索候选："]
        for item in results:
            appid = item.get("id")
            name = clean_text(item.get("name"), "未命名")
            price = clean_text(item.get("price", {}).get("final") if isinstance(item.get("price"), dict) else "")
            lines.append(f"- {name} | AppID: {appid}" + (f" | {price}" if price else ""))
        return "\n".join(lines)

    def format_app_details(self, appid: int, data: dict[str, Any], search_results: list[dict[str, Any]] | None = None) -> str:
        lines: list[str] = []
        name = clean_text(data.get("name"), f"App {appid}")
        lines.append(f"Steam 游戏: {name}")
        lines.append(f"AppID: {appid}")
        lines.append(f"商店链接: https://store.steampowered.com/app/{appid}/")
        app_type = clean_text(data.get("type"))
        if app_type and read_bool(cfg(self.config, "steam", "show_type", True), True):
            lines.append(f"类型: {app_type}")
        price = format_price(data)
        if price:
            lines.append(f"价格: {price}")
        release = data.get("release_date")
        if isinstance(release, dict) and read_bool(cfg(self.config, "steam", "show_release_date", True), True):
            date_text = clean_text(release.get("date"))
            if date_text:
                suffix = "（未发售）" if release.get("coming_soon") else ""
                lines.append(f"发售日期: {date_text}{suffix}")
        developers = join_names(data.get("developers"))
        publishers = join_names(data.get("publishers"))
        if read_bool(cfg(self.config, "steam", "show_developers_publishers", True), True):
            if developers:
                lines.append(f"开发商: {developers}")
            if publishers:
                lines.append(f"发行商: {publishers}")
        genres = join_names(data.get("genres"))
        if genres and read_bool(cfg(self.config, "steam", "show_genres", True), True):
            lines.append(f"分类: {genres}")
        description = strip_html(data.get("short_description"))
        if description and read_bool(cfg(self.config, "steam", "show_short_description", True), True):
            lines.append(f"简介: {truncate(description, self.max_description_chars())}")
        content_descriptors = data.get("content_descriptors")
        if isinstance(content_descriptors, dict) and read_bool(cfg(self.config, "steam", "show_content_descriptors", True), True):
            notes = strip_html(content_descriptors.get("notes"))
            if notes:
                lines.append(f"内容提示: {notes}")
        header_image = clean_text(data.get("header_image"))
        if header_image:
            lines.append(f"封面图: {header_image}")
        if search_results and len(search_results) > 1:
            lines.append("")
            lines.append(self.format_search_results(search_results))
        return "\n".join(lines)

    async def query_game(
        self,
        *,
        query: str,
        return_image: bool = False,
    ) -> str | CallToolResult:
        if not self.enabled():
            return "Steam 查询功能当前未启用。"
        if not self.tool_enabled():
            return "Steam LLM 工具当前未启用。"
        limited = await self._check_rate_limit()
        if limited:
            return limited
        appid, search_results, error = await self.resolve_query(query)
        if error:
            return error
        assert appid is not None
        try:
            data = await self.fetch_app_details(appid)
        except Exception as exc:
            logger.warning("[HelperTools] Steam details failed: %s", exc)
            return f"Steam 查询失败: {exc}"
        text = self.format_app_details(appid, data, search_results)
        header_image = clean_text(data.get("header_image"))
        if not return_image or not header_image or not self.show_header_image():
            return text
        try:
            image_data, mime_type = await fetch_bytes(header_image, timeout_seconds=self.timeout())
            if not mime_type.startswith("image/"):
                mime_type = "image/jpeg"
        except Exception:
            return text
        return CallToolResult(
            content=[
                TextContent(type="text", text=text),
                ImageContent(type="image", data=base64.b64encode(image_data).decode("ascii"), mimeType=mime_type),
            ],
            isError=False,
        )

    async def build_chain_for_message(self, query: str) -> tuple[list[Any] | None, str]:
        if not self.enabled():
            return None, "Steam 查询功能当前未启用。"
        limited = await self._check_rate_limit()
        if limited:
            return None, limited
        appid, search_results, error = await self.resolve_query(query)
        if error:
            return None, error
        assert appid is not None
        data = await self.fetch_app_details(appid)
        text = self.format_app_details(appid, data, search_results)
        chain: list[Any] = []
        header_image = clean_text(data.get("header_image"))
        if header_image and self.show_header_image():
            chain.append(Comp.Image.fromURL(header_image))
        chain.append(Comp.Plain(text))
        return chain, ""

    def should_handle_message(self, text: str) -> tuple[bool, str]:
        if not self.enabled():
            return False, ""
        if self.commands_enabled() and parse_dynamic_command(text, self.command_aliases()):
            parsed = parse_dynamic_command(text, self.command_aliases())
            return True, parsed[1] if parsed else ""
        if self.auto_parse_links():
            appid = extract_steam_appid(text)
            if appid:
                return True, appid
        return False, ""
