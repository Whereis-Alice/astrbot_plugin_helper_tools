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
                "desc": "????",
                "prompt": "[QQ???]????",
                "meta": {
                    "detail_1": {
                        "title": "??????",
                        "desc": "??????",
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
        self.assertIn("????????", marker)
        self.assertIn("???????", marker)
        self.assertIn("?????????", marker)
        self.assertIn("?????????", marker)
        self.assertIn("???https://b23.tv/example", marker)
        self.assertIs(reply.chain[0], card)
        self.assertEqual(reply.id, "101")

    def test_makes_a_quoted_netease_music_card_readable(self) -> None:
        card = Comp.Json(
            data={
                "app": "com.tencent.structmsg",
                "desc": "??",
                "meta": {
                    "music": {
                        "tag": "?????",
                        "title": "????",
                        "desc": "????",
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
        self.assertIn("???????", marker)
        self.assertIn("????????", marker)
        self.assertIn("???????", marker)
        self.assertIn("??/???????", marker)

    def test_supports_native_music_and_share_segments(self) -> None:
        music = Comp.Music(
            id=456,
            title="????",
            content="???",
            url="https://music.163.com/song?id=456",
        )
        share = Comp.Share(
            title="????",
            content="????",
            url="https://example.com/share",
        )
        reply = Comp.Reply(id="103", chain=[music, share])

        result = ReplyCardReader({}).enrich(DummyEvent([reply]))

        marker = card_marker(reply)
        self.assertEqual(result.card_count, 2)
        self.assertIn("?? 1", marker)
        self.assertIn("?????", marker)
        self.assertIn("?? 2", marker)
        self.assertIn("????", marker)

    def test_does_not_duplicate_the_generated_marker(self) -> None:
        reply = Comp.Reply(
            id="104",
            chain=[
                Comp.Share(
                    title="??????",
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
                    title="??", content="??", url="https://example.com/private"
                )
            ],
        )
        disabled_reply = Comp.Reply(
            id="106",
            chain=[Comp.Share(title="????", content="", url="https://example.com")],
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
