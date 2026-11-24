from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from astrbot_plugin_helper_tools.chat_history_recall import (
    HistoryRecallIntent,
    parse_history_recall,
)

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
NOW = datetime(2026, 11, 24, 22, 15, 40, tzinfo=SHANGHAI)


class HistoryRecallTests(unittest.TestCase):
    def assert_window(
        self, text: str, start: str, end: str, *, now: datetime = NOW
    ) -> HistoryRecallIntent:
        intent = parse_history_recall(text, now=now)
        self.assertIsInstance(intent, HistoryRecallIntent, text)
        self.assertEqual(intent, HistoryRecallIntent(start=start, end=end), text)
        parsed_start = datetime.strptime(intent.start, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=now.tzinfo
        )
        parsed_end = datetime.strptime(intent.end, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=now.tzinfo
        )
        self.assertLess(parsed_start.timestamp(), parsed_end.timestamp(), text)
        self.assertLessEqual(parsed_end.timestamp(), now.timestamp(), text)
        return intent

    def assert_clarification(self, text: str, *, now: datetime = NOW) -> None:
        intent = parse_history_recall(text, now=now)
        self.assertIsInstance(intent, HistoryRecallIntent, text)
        self.assertTrue(intent.clarification, text)
        self.assertLess(len(intent.clarification), 55, text)
        self.assertRegex(intent.clarification, r"[\u4e00-\u9fff]", text)
        self.assertEqual((intent.start, intent.end, intent.query), ("", "", ""), text)

    def test_public_dataclass_contract(self) -> None:
        self.assertEqual(
            [field.name for field in fields(HistoryRecallIntent)],
            ["start", "end", "query", "clarification"],
        )
        self.assertEqual(HistoryRecallIntent(), HistoryRecallIntent("", "", "", ""))

    def test_screenshot_request_and_nickname_are_not_keyword_filters(self) -> None:
        self.assert_window(
            "@爱丽丝 五音不全 总结一下昨天下午6:00到8:30发生的事",
            "2026-11-23 18:00:00",
            "2026-11-23 20:30:00",
        )

    def test_explicit_current_group_recap_verbs(self) -> None:
        for text in (
            "请总结昨天本群聊天记录",
            "帮我回顾昨天的群聊",
            "复盘昨天聊了什么",
            "汇总昨天当前群的聊天内容",
            "能不能总结一下昨天我们群发生的事情？",
            "昨天本群聊天记录，请帮我总结一下",
            "请回顾昨天咱们群讨论了什么",
        ):
            with self.subTest(text=text):
                self.assert_window(text, "2026-11-23 00:00:00", "2026-11-23 23:59:59")

    def test_relative_calendar_days(self) -> None:
        for word, day, end in (
            ("前天", 22, "23:59:59"),
            ("昨天", 23, "23:59:59"),
            ("昨日", 23, "23:59:59"),
            ("今天", 24, "22:15:40"),
            ("今日", 24, "22:15:40"),
        ):
            with self.subTest(word=word):
                self.assert_window(
                    f"总结{word}群聊", f"2026-11-{day} 00:00:00", f"2026-11-{day} {end}"
                )

    def test_day_resolution_across_year_boundary_uses_event_date(self) -> None:
        event_time = datetime(2026, 1, 1, 0, 10, tzinfo=SHANGHAI)
        self.assert_window(
            "总结昨天群聊", "2025-12-31 00:00:00", "2025-12-31 23:59:59", now=event_time
        )
        self.assert_window(
            "回顾前天群聊", "2025-12-30 00:00:00", "2025-12-30 23:59:59", now=event_time
        )

    def test_explicit_dates_and_chinese_month_day(self) -> None:
        for value in ("2026-11-23", "2026-1-2", "11月23日", "11月23号", "十一月二十三日", "2026年11月23日"):
            with self.subTest(value=value):
                day = "2026-01-02" if value == "2026-1-2" else "2026-11-23"
                self.assert_window(f"总结{value}本群聊天记录", f"{day} 00:00:00", f"{day} 23:59:59")

    def test_explicit_calendar_date_range(self) -> None:
        self.assert_window(
            "汇总11月22日到11月23日本群聊天记录",
            "2026-11-22 00:00:00", "2026-11-23 23:59:59",
        )
        self.assert_window(
            "回顾昨天到今天的群聊", "2026-11-23 00:00:00", "2026-11-24 22:15:40"
        )

    def test_range_connectors_and_fullwidth_clock(self) -> None:
        for connector in ("到", "至", "～", "~", "-", "—", "–"):
            with self.subTest(connector=connector):
                self.assert_window(
                    f"总结昨天18:00{connector}20:30群聊",
                    "2026-11-23 18:00:00", "2026-11-23 20:30:00",
                )
        self.assert_window(
            "总结昨天１８：００～２０：３０群聊", "2026-11-23 18:00:00", "2026-11-23 20:30:00"
        )

    def test_chinese_clocks_and_period_inheritance(self) -> None:
        for clocks, start, end in (
            ("下午6点到8点半", "18:00:00", "20:30:00"),
            ("下午六点到八点半", "18:00:00", "20:30:00"),
            ("下午六点一刻到八点三刻", "18:15:00", "20:45:00"),
            ("下午六点三十分到八点四十五分", "18:30:00", "20:45:00"),
            ("上午九时到十时半", "09:00:00", "10:30:00"),
            ("早上六点到八点半", "06:00:00", "08:30:00"),
            ("凌晨十二点到两点", "00:00:00", "02:00:00"),
            ("中午十二点到下午一点", "12:00:00", "13:00:00"),
            ("十八点到二十点半", "18:00:00", "20:30:00"),
            ("下午6点到20:30", "18:00:00", "20:30:00"),
            ("上午11点到下午1点", "11:00:00", "13:00:00"),
        ):
            with self.subTest(clocks=clocks):
                self.assert_window(f"回顾昨天{clocks}本群群聊", f"2026-11-23 {start}", f"2026-11-23 {end}")

    def test_shorthand_yesterday_evening_and_morning(self) -> None:
        for clocks, start, end in (
            ("昨晚六点到八点半", "18:00:00", "20:30:00"),
            ("昨夜18:00到20:30", "18:00:00", "20:30:00"),
            ("昨早六点到八点半", "06:00:00", "08:30:00"),
            ("昨晨6:00到8:30", "06:00:00", "08:30:00"),
        ):
            with self.subTest(clocks=clocks):
                self.assert_window(f"总结{clocks}发生的事", f"2026-11-23 {start}", f"2026-11-23 {end}")

    def test_seconds_are_preserved(self) -> None:
        self.assert_window(
            "总结昨天18:00:05到20:30:09的群聊",
            "2026-11-23 18:00:05", "2026-11-23 20:30:09",
        )
        self.assert_window(
            "回顾昨天六点三十分五秒到八点四十分九秒的群聊",
            "2026-11-23 06:30:05", "2026-11-23 08:40:09",
        )

    def test_cross_midnight_requires_explicit_dates(self) -> None:
        self.assert_window(
            "总结昨天晚上11点到今天凌晨1点本群发生的事",
            "2026-11-23 23:00:00", "2026-11-24 01:00:00",
        )
        self.assert_clarification("总结昨天23点到1点群聊")
        self.assert_clarification("总结昨晚12点到1点群聊")

    def test_recent_hours_arabic_chinese_fractional_and_half(self) -> None:
        for value, minutes in (
            ("最近2小时", 120), ("过去三小时", 180), ("近两个小时", 120),
            ("最近半小时", 30), ("最近一个半小时", 90),
            ("最近一小时半", 90), ("最近2.5小时", 150), ("最近十二小时", 720),
        ):
            with self.subTest(value=value):
                self.assert_window(
                    f"总结{value}本群聊天记录",
                    (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
                    "2026-11-24 22:15:40",
                )

    def test_recent_hours_cross_date_boundary(self) -> None:
        event_time = datetime(2026, 11, 24, 1, 30, tzinfo=SHANGHAI)
        self.assert_window(
            "回顾最近三小时群聊", "2026-11-23 22:30:00", "2026-11-24 01:30:00", now=event_time
        )

    def test_caller_timezone_and_microsecond_precision(self) -> None:
        event_time = datetime(2025, 2, 3, 3, 0, 17, 999999, tzinfo=timezone.utc)
        self.assert_window(
            "回顾最近一小时群聊", "2025-02-03 02:00:17", "2025-02-03 03:00:17", now=event_time
        )
        self.assert_window(
            "总结今天群聊", "2025-02-03 00:00:00", "2025-02-03 03:00:17", now=event_time
        )

    def test_valid_leap_day(self) -> None:
        self.assert_window(
            "总结2024-02-29群聊", "2024-02-29 00:00:00", "2024-02-29 23:59:59"
        )

    def test_nonrequests_do_not_recall(self) -> None:
        for text in (
            "", "昨天真开心", "昨天下午6点到8点半真开心", "昨天群聊很热闹",
            "昨天我们聊了什么？", "请总结昨天", "总结一下昨天下午的工作",
            "请总结这篇文章", "请总结这篇文章，昨天本群也很热闹",
            "回顾这份报告中的聊天记录", "请总结这张截图里的群聊",
            "昨天本群群聊的总结很有趣", "我正在总结昨天本群聊天记录",
            "不要总结昨天本群聊天记录", "别总结昨天群聊", "不用回顾昨天群聊",
            "如果我让你总结昨天群聊，你会怎么做", "他说：总结昨天本群聊天记录",
            "解释这条指令：总结昨天本群聊天记录",
            "总结这篇新闻中昨天下午发生的事故",
            "请总结昨天这篇讲群聊的文章", "总结2026-11-23的文章里的聊天记录",
            "总结昨天下午新闻里发生的事", "我不需要总结昨天本群聊天记录",
            "不能总结昨天群聊", "不总结昨天群聊", "你做的总结不错，昨天本群很热闹",
            "我总结昨天本群聊天记录", "我昨天总结了本群聊天记录",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_history_recall(text, now=NOW), text)

    def test_cross_group_and_private_destinations_are_not_current_group_requests(self) -> None:
        for text in (
            "总结昨天其他群聊天记录", "回顾昨天私聊记录", "汇总昨天所有群聊",
            "总结昨天群号123456的聊天记录", "复盘昨天123456群聊",
            "总结昨天技术群聊天记录", "总结昨天猫猫群的聊天记录",
            "回顾昨天QQ群123456的聊天记录", "总结昨天本群和私聊聊天记录",
            "总结昨天本群聊天记录，顺便查一下隔壁群",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_history_recall(text, now=NOW), text)

    def test_quotes_and_code_are_not_executable_requests(self) -> None:
        request = "总结昨天本群聊天记录"
        for text in (
            f'"{request}"', f"'{request}'", f"“{request}”", f"‘{request}’",
            f"「{request}」", f"『{request}』", f"`{request}`",
            f"```text\n{request}\n```", f"~~~\n{request}\n~~~",
            f"```\n{request}", f"“{request}", f"> {request}",
            f"> 引用\n{request}", f"    {request}", f"\t{request}",
            f"<pre>{request}</pre>", f"<code>{request}</code>",
            f'{{"instruction": "{request}"}}', f"说明：`{request}`，这句是什么意思？",
            "总`无关代码`结昨天本群聊天记录",
            f"``{request}``", f"`\n{request}\n`", f"`\n{request}",
            f'"引用\n{request}\n"', f"````python\n{request}\n````",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_history_recall(text, now=NOW), text)

    def test_direct_request_after_separate_quoted_block(self) -> None:
        self.assert_window(
            "> 总结前天群聊\n\n请回顾昨天本群聊天记录",
            "2026-11-23 00:00:00", "2026-11-23 23:59:59",
        )

    def test_no_date_or_only_one_clock_requires_clarification(self) -> None:
        for text in (
            "总结一下群聊", "回顾本群聊天记录", "总结18:00到20:30群聊",
            "总结昨天18点群聊", "总结昨天下午群聊", "回顾昨晚发生的事",
            "总结昨早群聊", "回顾昨天上午发生的事", "汇总最近群聊",
            "总结最近几小时群聊", "回顾11月群聊",
        ):
            with self.subTest(text=text):
                self.assert_clarification(text)

    def test_fuzzy_dates_and_approximate_times_require_clarification(self) -> None:
        for when in (
            "上周", "前几天", "那天", "去年11月23日", "星期一", "周三", "刚才",
            "昨天下午6点到8点左右", "大约昨天18点到20点", "昨天18点之前",
        ):
            with self.subTest(when=when):
                self.assert_clarification(f"总结{when}本群聊天记录")

    def test_invalid_dates_do_not_fall_back_to_a_default_day(self) -> None:
        for when in ("2026-02-29", "2026-11-31", "2026-13-01", "0000-01-01", "13月2日", "11月0日", "2026/11/23"):
            with self.subTest(when=when):
                self.assert_clarification(f"总结{when}群聊")

    def test_invalid_clocks_do_not_partially_match(self) -> None:
        for clocks in (
            "25:00到26:00", "18:99到20:30", "18:00:99到20:30:00",
            "118:00到120:00", "二十五点到二十六点", "下午18点到20点",
            "下午零点到八点", "昨晚上午6点到8点", "中午六点到八点",
            "下午六点六十分到八点", "六点三十六十一秒到八点",
            "-18点到20点", "6.5点到8点", "一二点到二三点", "零十点到十一点",
        ):
            with self.subTest(clocks=clocks):
                self.assert_clarification(f"总结昨天{clocks}群聊")

    def test_future_dates_and_clocks_are_not_clipped_or_shifted(self) -> None:
        early = datetime(2026, 11, 24, 19, 0, tzinfo=SHANGHAI)
        for when in ("明天", "后天", "2026-11-25", "12月1日", "今天18:00到20:30"):
            with self.subTest(when=when):
                self.assert_clarification(f"总结{when}本群聊天记录", now=early)
        # A month/day in the future is not silently interpreted as last year.
        january = datetime(2026, 1, 1, 8, 0, tzinfo=SHANGHAI)
        self.assert_clarification("总结12月31日群聊", now=january)

    def test_inverted_and_equal_ranges_are_not_repaired(self) -> None:
        for when in (
            "昨天20:30到18:00", "昨天18:00到18:00", "今天到昨天",
            "11月23日到11月22日", "昨天18:00到8:30", "昨晚11点到1点",
        ):
            with self.subTest(when=when):
                self.assert_clarification(f"总结{when}群聊")

    def test_multiple_ranges_or_date_sources_require_clarification(self) -> None:
        for text in (
            "总结昨天和前天本群群聊", "总结昨天前天今天群聊",
            "总结昨天18点到20点和21点到22点的群聊",
            "总结昨天最近两小时群聊", "总结最近两小时到一小时前群聊",
            "总结昨天群聊；回顾前天本群聊天记录",
            "总结昨天群聊，回顾前天本群聊天记录",
            "总结昨天18点和20点群聊",
        ):
            with self.subTest(text=text):
                self.assert_clarification(text)

    def test_invalid_relative_durations_require_clarification(self) -> None:
        for value in (
            "最近0小时", "最近零小时", "最近-2小时", "最近两十十小时",
            "最近9999999999999999999999999999小时", "最近2小时30分钟",
            "最近0.0001小时", "最近半小时半",
            "最近两三小时", "最近二三小时", "最近零十小时", "最近一个半小时半",
        ):
            with self.subTest(value=value):
                self.assert_clarification(f"总结{value}本群聊天记录")

    def test_every_24_hour_clock_boundary(self) -> None:
        for hour in range(23):
            with self.subTest(hour=hour):
                self.assert_window(
                    f"回顾昨天{hour}:00到{hour + 1}:00群聊",
                    f"2026-11-23 {hour:02}:00:00", f"2026-11-23 {hour + 1:02}:00:00",
                )

    def test_now_is_keyword_only_and_must_be_aware(self) -> None:
        with self.assertRaises(TypeError):
            parse_history_recall("总结昨天群聊", NOW)
        with self.assertRaises(ValueError):
            parse_history_recall("总结昨天群聊", now=NOW.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
