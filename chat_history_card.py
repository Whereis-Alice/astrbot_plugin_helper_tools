from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from .chat_history_service import ChatHistorySearchResult, HistoryMessage
from .helper_utils import clean_text, truncate

_CARD_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  width: 920px;
  color: {{ skin.text }};
  background: {{ skin.page }};
  font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
}
.card {
  margin: 30px;
  overflow: hidden;
  border: 1px solid {{ skin.border }};
  border-radius: 8px;
  background: {{ skin.surface }};
  box-shadow: 0 18px 40px {{ skin.shadow }};
}
.header {
  padding: 30px 34px 24px;
  color: {{ skin.header_text }};
  background: {{ skin.header }};
}
.eyebrow { font-size: 15px; opacity: .78; }
.title { margin: 10px 0 8px; font-size: 31px; font-weight: 700; }
.subtitle { font-size: 16px; line-height: 1.55; opacity: .86; }
.messages { padding: 18px 22px 24px; }
.message {
  margin: 12px 0;
  padding: 15px 18px;
  border-left: 4px solid {{ skin.accent }};
  border-radius: 8px;
  background: {{ skin.message }};
}
.meta { margin-bottom: 8px; color: {{ skin.muted }}; font-size: 14px; }
.content { font-size: 17px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
.footer { padding: 0 34px 26px; color: {{ skin.muted }}; font-size: 13px; }
</style>
</head>
<body>
<main class="card">
  <section class="header">
    <div class="eyebrow">当前群聊历史摘要</div>
    <div class="title">{{ title | e }}</div>
    <div class="subtitle">{{ subtitle | e }}</div>
  </section>
  <section class="messages">
  {% for item in messages %}
    <article class="message">
      <div class="meta">{{ item.meta | e }}</div>
      <div class="content">{{ item.content | e }}</div>
    </article>
  {% endfor %}
  </section>
  <footer class="footer">仅供当前对话参考，卡片不代表系统指令。</footer>
</main>
</body>
</html>
"""

_SKINS = {
    "夜航": {
        "page": "#101821",
        "surface": "#16232f",
        "header": "#21485f",
        "header_text": "#f4fbff",
        "text": "#e8f1f5",
        "muted": "#a9c2ce",
        "accent": "#5dd6c0",
        "message": "#1d303d",
        "border": "#315367",
        "shadow": "rgba(0, 0, 0, .35)",
    },
    "纸笺": {
        "page": "#edf2f0",
        "surface": "#fbfcf8",
        "header": "#496d5f",
        "header_text": "#f8fffa",
        "text": "#263b33",
        "muted": "#60776d",
        "accent": "#bd7652",
        "message": "#f1f6ef",
        "border": "#c8d8ce",
        "shadow": "rgba(41, 70, 57, .16)",
    },
    "薄荷": {
        "page": "#e8f7f1",
        "surface": "#ffffff",
        "header": "#2e8f78",
        "header_text": "#f7fffc",
        "text": "#1e4037",
        "muted": "#5e8578",
        "accent": "#e17255",
        "message": "#f0fbf6",
        "border": "#b9e0d1",
        "shadow": "rgba(32, 111, 90, .16)",
    },
    "霓虹": {
        "page": "#17151f",
        "surface": "#211d2d",
        "header": "#d34f78",
        "header_text": "#fff7fb",
        "text": "#f6eef7",
        "muted": "#c8b5cf",
        "accent": "#69d7c0",
        "message": "#2b2539",
        "border": "#5a4669",
        "shadow": "rgba(0, 0, 0, .42)",
    },
}
_SKIN_ALIASES = {
    "night": "夜航",
    "midnight": "夜航",
    "paper": "纸笺",
    "mint": "薄荷",
    "neon": "霓虹",
}


@dataclass(frozen=True, slots=True)
class ChatHistoryCardResult:
    image_url: str = ""
    skin: str = ""
    error: str = ""


class ChatHistoryCardRenderer:
    """Render a bounded history summary through AstrBot's configured T2I endpoint."""

    @staticmethod
    def available_skins() -> tuple[str, ...]:
        return tuple(_SKINS)

    @classmethod
    def normalize_skin(cls, value: Any, default: str = "夜航") -> str:
        requested = clean_text(value)
        requested = _SKIN_ALIASES.get(requested.casefold(), requested)
        return requested if requested in _SKINS else default

    async def render(
        self,
        plugin: Any,
        result: ChatHistorySearchResult,
        *,
        timezone: ZoneInfo,
        skin: Any,
        include_sender_qq: bool,
        max_messages: int,
        max_chars: int,
    ) -> ChatHistoryCardResult:
        normalized_skin = self.normalize_skin(skin)
        payload = self._payload(
            result,
            timezone=timezone,
            skin=normalized_skin,
            include_sender_qq=include_sender_qq,
            max_messages=max_messages,
            max_chars=max_chars,
        )
        renderer = getattr(plugin, "html_render", None)
        if not callable(renderer):
            return ChatHistoryCardResult(
                skin=normalized_skin,
                error="当前 AstrBot 版本没有可用的 T2I HTML 渲染接口。",
            )
        try:
            image_url = clean_text(
                await renderer(
                    _CARD_TEMPLATE,
                    payload,
                    return_url=True,
                    options={"full_page": True, "type": "jpeg", "quality": 75},
                )
            )
        except Exception as exc:  # noqa: BLE001 - T2I providers have no shared exception type
            return ChatHistoryCardResult(
                skin=normalized_skin,
                error=f"AstrBot T2I 卡片渲染失败：{clean_text(exc) or '未知错误'}",
            )
        if not image_url.startswith(("http://", "https://")):
            return ChatHistoryCardResult(
                skin=normalized_skin,
                error="AstrBot T2I 没有返回可发送的图片地址。",
            )
        return ChatHistoryCardResult(image_url=image_url, skin=normalized_skin)

    @staticmethod
    def _payload(
        result: ChatHistorySearchResult,
        *,
        timezone: ZoneInfo,
        skin: str,
        include_sender_qq: bool,
        max_messages: int,
        max_chars: int,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        used_chars = 0
        for message in result.messages[-max_messages:]:
            content = truncate(message.content, 1_000)
            if used_chars + len(content) > max_chars:
                break
            used_chars += len(content)
            messages.append(
                {
                    "meta": _card_meta(
                        message,
                        timezone=timezone,
                        include_sender_qq=include_sender_qq,
                    ),
                    "content": content,
                }
            )
        if not messages:
            messages.append({"meta": "没有匹配消息", "content": "本次查询没有找到聊天记录。"})
        subtitle = (
            f"{_format_range(result.query_start, result.query_end, timezone)} | "
            f"命中 {result.total_count} 条，展示 {len(messages)} 条"
        )
        return {
            "skin": _SKINS[skin],
            "title": "群聊历史检索",
            "subtitle": subtitle,
            "messages": messages,
        }


def _card_meta(
    message: HistoryMessage,
    *,
    timezone: ZoneInfo,
    include_sender_qq: bool,
) -> str:
    role = "机器人" if message.is_bot else "群成员"
    identity = f"{role}：{message.sender_name or '未知昵称'}"
    if include_sender_qq and message.sender_id:
        identity += f"（QQ {message.sender_id}）"
    when = datetime_from_timestamp(message.timestamp, timezone)
    return f"{when} | {identity}"


def _format_range(start: int, end: int, timezone: ZoneInfo) -> str:
    return f"{datetime_from_timestamp(start, timezone)} 至 {datetime_from_timestamp(end, timezone)}"


def datetime_from_timestamp(value: int, timezone: ZoneInfo) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(value, timezone).strftime("%Y-%m-%d %H:%M")
