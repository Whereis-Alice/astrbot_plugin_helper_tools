from __future__ import annotations

import unittest

import astrbot.api.message_components as Comp

from astrbot_plugin_helper_tools.reply_card_reader import (
    CARD_SUMMARY_PREFIX,
    ReplyCardReader,
)


class DummyEvent:
    def __init__(self, chain: list[object]) -> None:
        self._chain = chain

    def get_messages(self) -> list[object]:
        return self._chain


def card_marker(reply: Comp.Reply) -> str:
    for component in reply.chain or []:
        if isinstance(component, Comp.Plain) and component.text.startswith(
            CARD_SUMMARY_PREFIX
        ):
            return component.text
    return ""


class ReplyCardReaderTests(unittest.TestCase):
    def test_makes_a_quoted_bilibili_miniapp_readable(self) -> None:
        card = Comp.Json(
            data={
                "app": "com.tencent.miniapp_01",
                "desc": "哔哩哔哩",
                "prompt": "[QQ小程序]哔哩哔哩",
                "meta": {
                    "detail_1": {
                        "title": "测试视频标题",
                        "desc": "测试视频简介",
                        "qqdocurl": "https://b23.tv/example",
                    }
                },
            }
        )
        reply = Comp.Reply(id="101", sender_id="20002", chain=[card])

        result = ReplyCardReader({"reply_card_reader": {"enabled": True}}).enrich(
            DummyEvent([reply])
        )

        marker = card_marker(reply)
        self.assertEqual(result.enriched_reply_count, 1)
        self.assertEqual(result.card_count, 1)
        self.assertIn("类型：小程序卡片", marker)
        self.assertIn("来源：哔哩哔哩", marker)
        self.assertIn("标题：测试视频标题", marker)
        self.assertIn("描述：测试视频简介", marker)
        self.assertIn("链接：https://b23.tv/example", marker)
        self.assertIs(reply.chain[0], card)
        self.assertEqual(reply.id, "101")

    def test_makes_a_quoted_netease_music_card_readable(self) -> None:
        card = Comp.Json(
            data={
                "app": "com.tencent.structmsg",
                "desc": "音乐",
                "meta": {
                    "music": {
                        "tag": "网易云音乐",
                        "title": "测试歌曲",
                        "desc": "测试歌手",
                        "jumpUrl": "https://music.163.com/song?id=123",
                    }
                },
                "view": "music",
            }
        )
        reply = Comp.Reply(id="102", chain=[card])

        result = ReplyCardReader({}).enrich(DummyEvent([reply]))

        marker = card_marker(reply)
        self.assertEqual(result.card_count, 1)
        self.assertIn("类型：音乐卡片", marker)
        self.assertIn("来源：网易云音乐", marker)
        self.assertIn("标题：测试歌曲", marker)
        self.assertIn("作者/歌手：测试歌手", marker)

    def test_supports_native_music_and_share_segments(self) -> None:
        music = Comp.Music(
            id=456,
            title="原生音乐",
            content="歌手名",
            url="https://music.163.com/song?id=456",
        )
        share = Comp.Share(
            title="普通分享",
            content="分享说明",
            url="https://example.com/share",
        )
        reply = Comp.Reply(id="103", chain=[music, share])

        result = ReplyCardReader({}).enrich(DummyEvent([reply]))

        marker = card_marker(reply)
        self.assertEqual(result.card_count, 2)
        self.assertIn("卡片 1", marker)
        self.assertIn("网易云音乐", marker)
        self.assertIn("卡片 2", marker)
        self.assertIn("普通分享", marker)

    def test_does_not_duplicate_the_generated_marker(self) -> None:
        reply = Comp.Reply(
            id="104",
            chain=[
                Comp.Share(
                    title="只应出现一次",
                    content="",
                    url="https://example.com",
                )
            ],
        )
        reader = ReplyCardReader({})

        first = reader.enrich(DummyEvent([reply]))
        second = reader.enrich(DummyEvent([reply]))

        markers = [
            item
            for item in reply.chain or []
            if isinstance(item, Comp.Plain)
            and item.text.startswith(CARD_SUMMARY_PREFIX)
        ]
        self.assertEqual(first.card_count, 1)
        self.assertEqual(second.card_count, 0)
        self.assertEqual(len(markers), 1)

    def test_can_hide_urls_or_disable_the_reader(self) -> None:
        hidden_url_reply = Comp.Reply(
            id="105",
            chain=[
                Comp.Share(
                    title="分享", content="说明", url="https://example.com/private"
                )
            ],
        )
        disabled_reply = Comp.Reply(
            id="106",
            chain=[Comp.Share(title="不会补全", content="", url="https://example.com")],
        )

        hidden_result = ReplyCardReader(
            {"reply_card_reader": {"include_urls": False}}
        ).enrich(DummyEvent([hidden_url_reply]))
        disabled_result = ReplyCardReader(
            {"reply_card_reader": {"enabled": False}}
        ).enrich(DummyEvent([disabled_reply]))

        self.assertEqual(hidden_result.card_count, 1)
        self.assertNotIn("https://", card_marker(hidden_url_reply))
        self.assertEqual(disabled_result.card_count, 0)
        self.assertEqual(card_marker(disabled_reply), "")

    def test_leaves_multimsg_json_to_astrbot_core(self) -> None:
        reply = Comp.Reply(
            id="107",
            chain=[
                Comp.Json(data={"app": "com.tencent.multimsg", "meta": {"detail": {}}})
            ],
        )

        result = ReplyCardReader({}).enrich(DummyEvent([reply]))

        self.assertEqual(result.card_count, 0)
        self.assertEqual(card_marker(reply), "")


if __name__ == "__main__":
    unittest.main()
