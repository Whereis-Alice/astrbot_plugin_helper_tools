from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from astrbot_plugin_helper_tools import anime1_service as anime1_module
from astrbot_plugin_helper_tools.anime1_service import Anime1Service
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches
from astrbot_plugin_helper_tools.qq_like_service import QQProfileLikeService
from astrbot_plugin_helper_tools.steam_service import SteamService
from astrbot_plugin_helper_tools.tests.test_qq_like_service import FakeBot, FakeEvent
from astrbot_plugin_helper_tools.voice_service import VoiceService

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

def _schema_entry(section: str, key: str) -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema[section]["items"][key]


def _configs(section: str, key: str) -> list[tuple[str, dict[str, Any]]]:
    """构造「用户已清空」的四种配置形态。"""

    return [
        ("empty_list", {section: {key: []}}),
        ("blank_string", {section: {key: "   "}}),
        ("missing_key", {section: {}}),
        ("missing_section", {}),
    ]


class VoiceTriggerKeywordsTests(unittest.TestCase):
    """自动触发关键词清空后必须真的不触发，而不是回落到「哈基米」。"""

    def _service(self, config: dict[str, Any], root: Path) -> VoiceService:
        return VoiceService(config, root)

    def test_cleared_config_shapes_return_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for name, config in _configs("voice", "trigger_keywords"):
                with self.subTest(shape=name):
                    service = self._service(config, Path(temp_dir))

                    self.assertEqual(service.trigger_keywords(), [])

    def test_blank_items_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                {"voice": {"trigger_keywords": ["", "  ", "哈基米"]}},
                Path(temp_dir),
            )

            self.assertEqual(service.trigger_keywords(), ["哈基米"])

    def test_cleared_keywords_do_not_trigger_any_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service({"voice": {"trigger_keywords": []}}, Path(temp_dir))

            for text in ("哈基米好听", "今天天气不错", "", "随便说一句话"):
                with self.subTest(text=text):
                    self.assertFalse(service.should_handle_message(text))

    def test_configured_keyword_still_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                {"voice": {"trigger_keywords": ["哈基米"]}},
                Path(temp_dir),
            )

            self.assertTrue(service.should_handle_message("来点哈基米"))
            self.assertFalse(service.should_handle_message("来点别的"))

    def test_commands_still_work_after_keywords_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service({"voice": {"trigger_keywords": []}}, Path(temp_dir))

            self.assertTrue(service.should_handle_message("/voice_meme"))


class QQLikeTriggerTests(unittest.IsolatedAsyncioTestCase):
    """点赞触发词/开头清空后不能回落到内置默认值。"""

    def setUp(self) -> None:
        reset_compat_caches()

    @staticmethod
    def _config(**overrides: Any) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "enabled": True,
            "likes_per_target": 10,
            "cooldown_seconds": 0,
        }
        settings.update(overrides)
        return {"qq_like": settings}

    async def test_cleared_trigger_phrases_do_not_match_self_like(self) -> None:
        for name, phrases in (("empty_list", []), ("blank_string", "   ")):
            with self.subTest(shape=name):
                bot = FakeBot(friend_ids=["20001"])
                event = FakeEvent(text="赞我", bot=bot)
                service = QQProfileLikeService(self._config(trigger_phrases=phrases))

                result = await service.handle_message(event, event.message_str)

                self.assertFalse(result.handled)
                self.assertEqual(result.reply, "")
                self.assertEqual(bot.likes, [])

    async def test_cleared_mention_prefixes_do_not_match_at_like(self) -> None:
        for name, prefixes in (("empty_list", []), ("blank_string", "   ")):
            with self.subTest(shape=name):
                bot = FakeBot(friend_ids=["20002"])
                event = FakeEvent(
                    text="赞 @20002",
                    bot=bot,
                    mentions=["20002"],
                )
                service = QQProfileLikeService(
                    self._config(mention_trigger_prefixes=prefixes)
                )

                result = await service.handle_message(event, event.message_str)

                self.assertFalse(result.handled)
                self.assertEqual(bot.likes, [])

    async def test_clearing_both_lists_disables_the_module_entirely(self) -> None:
        bot = FakeBot(friend_ids=["20001", "20002"])
        service = QQProfileLikeService(
            self._config(trigger_phrases=[], mention_trigger_prefixes=[])
        )

        for text, mentions in (("赞我", []), ("赞 @20002", ["20002"]), ("给我点赞", [])):
            with self.subTest(text=text):
                event = FakeEvent(text=text, bot=bot, mentions=mentions)

                result = await service.handle_message(event, event.message_str)

                self.assertFalse(result.handled)
        self.assertEqual(bot.likes, [])

    async def test_configured_phrases_still_match(self) -> None:
        bot = FakeBot(friend_ids=["20001"])
        event = FakeEvent(text="点个赞", bot=bot)
        service = QQProfileLikeService(self._config(trigger_phrases=["点个赞"]))

        result = await service.handle_message(event, event.message_str)

        self.assertTrue(result.handled)
        self.assertEqual(bot.likes, [(20001, 10)])

    def test_cleared_config_shapes_match_no_target(self) -> None:
        for key in ("trigger_phrases", "mention_trigger_prefixes"):
            for name, config in _configs("qq_like", key):
                with self.subTest(key=key, shape=name):
                    service = QQProfileLikeService(config)
                    bot = FakeBot(friend_ids=["20002"])
                    event = FakeEvent(text="赞 @20002", bot=bot, mentions=["20002"])

                    self.assertEqual(service._match_targets(event, "赞我"), [])
                    self.assertEqual(
                        service._match_targets(event, event.message_str), []
                    )


class Anime1UpdateTimesTests(unittest.IsolatedAsyncioTestCase):
    """清空更新时间点表示「不定时检查更新」，调度循环必须安静地空转。"""

    def _service(self, config: dict[str, Any], root: Path) -> Anime1Service:
        return Anime1Service(config, root / "data")

    def test_cleared_config_shapes_return_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for name, config in _configs("anime1", "update_times"):
                with self.subTest(shape=name):
                    service = self._service(config, Path(temp_dir))

                    self.assertEqual(service.update_times(), [])

    def test_invalid_hours_are_dropped_without_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                {"anime1": {"update_times": ["99", "abc", "-1"]}},
                Path(temp_dir),
            )

            self.assertEqual(service.update_times(), [])

    def test_configured_hours_are_parsed_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                {"anime1": {"update_times": ["1", "13", "1", "abc", "24"]}},
                Path(temp_dir),
            )

            self.assertEqual(service.update_times(), [1, 13])

    async def _run_one_scheduler_tick(self, service: Anime1Service) -> list[int]:
        calls: list[int] = []

        async def fake_update() -> int:
            calls.append(1)
            return 0

        service.update_cache = fake_update  # type: ignore[method-assign]
        # 必须是「本地时间」的 1:00 整，调度循环用的是 _local_now()。
        moment = datetime(2026, 1, 1, 1, 0).astimezone()
        with patch.object(anime1_module, "_local_now", return_value=moment):
            task = asyncio.create_task(service._scheduler_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        return calls

    async def test_scheduler_never_updates_when_times_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service({"anime1": {"update_times": []}}, Path(temp_dir))

            calls = await self._run_one_scheduler_tick(service)

            self.assertEqual(calls, [])

    async def test_scheduler_still_updates_at_a_configured_hour(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service({"anime1": {"update_times": ["1"]}}, Path(temp_dir))

            calls = await self._run_one_scheduler_tick(service)

            self.assertEqual(calls, [1])


class SteamCommandPrefixesTests(unittest.TestCase):
    """指令名是结构性必需项：清空后必须回落到内置默认值，否则指令彻底不可用。"""

    def test_cleared_config_shapes_fall_back_to_builtin_prefix(self) -> None:
        expected = {
            "empty_list": ["steam"],
            "blank_string": ["steam"],
            # 缺 key / 缺整段时 cfg() 直接返回内置默认指令名，两个都保留。
            "missing_key": ["steam", "查找"],
            "missing_section": ["steam", "查找"],
        }
        for name, config in _configs("steam", "command_prefixes"):
            with self.subTest(shape=name):
                service = SteamService(config)

                self.assertEqual(service.command_prefixes(), expected[name])

    def test_cleared_prefixes_still_match_the_builtin_command(self) -> None:
        service = SteamService({"steam": {"command_prefixes": []}})

        match = service.match_message("/steam 塞尔达")

        self.assertTrue(match.handled)
        self.assertEqual(match.query, "塞尔达")


class EmptyDefaultSchemaTests(unittest.TestCase):
    """守住 schema：可清空项默认必须是空列表，必需项要说明会回落。"""

    def test_clearable_entries_default_to_empty_list(self) -> None:
        for section, key in (
            ("voice", "trigger_keywords"),
            ("qq_like", "trigger_phrases"),
            ("qq_like", "mention_trigger_prefixes"),
            ("anime1", "update_times"),
        ):
            with self.subTest(section=section, key=key):
                entry = _schema_entry(section, key)

                self.assertEqual(entry["default"], [])
                self.assertIn("留空", entry.get("hint", ""))

    def test_steam_command_prefixes_keep_builtin_default(self) -> None:
        entry = _schema_entry("steam", "command_prefixes")

        self.assertEqual(entry["default"], ["steam", "查找"])
        self.assertIn("留空", entry.get("hint", ""))


if __name__ == "__main__":
    unittest.main()
