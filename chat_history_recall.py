"""Conservative, dependency-free Chinese current-group history recall parsing.

Only direct, unquoted recap requests are considered. This module neither reads
history nor verifies the event's group, permissions, or persona; the caller owns
those checks and must not query when ``clarification`` is nonempty.

Bounds use the existing history service's local ``YYYY-MM-DD HH:MM:SS`` format,
in ``now.tzinfo`` (the caller must use the same timezone when querying). Ends are
inclusive: a past calendar day ends at 23:59:59, and today's whole-day request
ends at the event time. Explicit future/inverted ranges are never clipped or
rolled into another day/year. A month/day without a year uses ``now.year``.
Dayparts alone and single clock endpoints require clarification. ``query`` is
reserved and stays empty: nicknames and recap topics must not become filters.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation


@dataclass
class HistoryRecallIntent:
    start: str = ""
    end: str = ""
    query: str = ""
    clarification: str = ""


_CN_DIGITS = "零〇一二两三四五六七八九"
_CN_NUMBER = rf"[{_CN_DIGITS}十百]+"
_INTEGER = rf"(?:[0-9]+|{_CN_NUMBER})"
_PERIOD = "凌晨|清晨|早晨|早上|上午|中午|下午|傍晚|晚上|晚间|夜里|夜间"
_PERIOD_RE = re.compile(_PERIOD)
_ACTION_RE = re.compile(r"总结|回顾|复盘|汇总")
_TARGET_RE = re.compile(
    r"聊天(?:记录|内容|历史)|群聊|本群|当前群|这个群|我们群|咱们群"
    r"|聊了(?:些)?什么|聊过什么|聊的什么|讨论了什么|都说了什么"
    r"|发生(?:了)?的?事(?:情)?(?!故)"
)
_EXTERNAL_RE = re.compile(
    r"跨群|跨聊|私聊|私信|其他群|其它群|别的群|另一个群|所有群|各群"
    r"|多个群|隔壁群|那个群|某个群|指定群|目标群|群号|群\s*ID|group_id"
    r"|群\s*[0-9]{4,}|[0-9]{4,}\s*群"
    r"|(?:名为|叫做|群名[为是:])\s*\S+群",
    re.IGNORECASE,
)
_NONREQUEST_PREFIX_RE = re.compile(
    r"不要|不用|别再|别帮|别给|不必|无需|不需要|禁止|不想|不是让|别\s*$|(?<!能)不能"
    r"|他说|她说|有人说|原文|引用|转述|示例|例子|例如|假如|如果"
    r"|解释|翻译|分析这句|这句话|这条指令|这个指令|这条命令"
    r"|我(?:正在|在|已经|刚刚|刚|曾经)(?:做|写|进行)?\s*$"
    r"|(?:做|写|给|发)(?:的|过的)\s*$|(?:你的|他的|她的|我的)\s*$"
    r"|不\s*$|(?:^|[,，])我(?:昨天|今天|前天)?\s*$"
)
_NONREQUEST_SUFFIX_RE = re.compile(
    r"^(?:很|挺|真|太|是|的|得|过(?!去)|报告|结果|功能|指令|示例|能力|按钮|怎么|如何)"
)
_OTHER_TARGET_RE = re.compile(
    r"^(?:(?:一下|一遍|下)\s*)?(?:这篇|那篇|一篇|这份|那份|这个|这张|下面的|以下的)?"
    r"(?:文章|论文|新闻|报告|网页|作文|文档|视频|电影|小说|截图|图片|日记)"
)
_DOCUMENT_OBJECT_RE = re.compile(
    r"(?:这篇|那篇|一篇|这份|那份|这张|那张)[^,，]*?"
    r"(?:文章|论文|新闻|报告|网页|作文|文档|视频|电影|小说|截图|图片|日记)"
    r"|(?:文章|论文|新闻|报告|网页|作文|文档|视频|电影|小说|截图|图片|日记)(?:里|中|内)"
)
_DATE_RE = re.compile(
    rf"(?<![0-9])(?:(?P<iso>[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}})(?![0-9])"
    rf"|(?:(?P<year>[0-9]{{4}})年)?(?P<month>{_INTEGER})月(?P<day>{_INTEGER})[日号]"
    r"|(?P<relative>前天|昨天|昨日|今天|今日|昨晚|昨夜|昨早|昨晨|今早|今晨))"
)
_HOURS_RE = re.compile(
    rf"(?:最近|过去|近)\s*(?P<number>[0-9]+(?:\.[0-9]+)?|{_CN_NUMBER}|半)"
    r"\s*(?:个)?(?P<before_half>半)?小时(?P<half>半)?"
)
_CLOCK_RE = re.compile(
    rf"(?<![0-9])(?:(?P<period>{_PERIOD})\s*)?"
    rf"(?:(?P<colon_hour>[0-9]+):(?P<colon_minute>[0-9]+)(?::(?P<colon_second>[0-9]+))?"
    rf"|(?P<hour>{_INTEGER})(?:点|时)"
    rf"(?:(?P<fraction>半|一刻|三刻)|(?P<minute>{_INTEGER})(?:分)?)?"
    rf"(?:(?P<second>{_INTEGER})秒)?)"
)
_CONNECTOR_RE = re.compile(r"\s*(?:到|至|~|～|-|—|–)\s*")
_FUZZY_RE = re.compile(
    r"大前天|明天|后天|今晚|上周|本周|这周|下周|上个月|本月|这个月|下个月"
    r"|去年|前年|明年|前几天|那天|几天前|最近几|前一阵|之前|以前|一段时间"
    r"|刚才|刚刚|早些时候|稍早|周[一二三四五六日天末]|星期|礼拜"
    r"|左右|大概|大约|差不多|差一刻|不记得|UTC|GMT|时区",
    re.IGNORECASE,
)
_UNPARSED_TIME_RE = re.compile(
    rf"{_PERIOD}|[0-9]{{4}}[-/年]|{_INTEGER}月|{_INTEGER}\s*(?:点|时|:|到|至)"
    r"|小时|分钟|秒钟|几点|时间段|时段|半天|一整晚|整晚|整夜|通宵"
)
_CODE_OR_QUOTE_RE = re.compile(
    r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)"
    r"|<(?:pre|code)\b[^>]*>[\s\S]*?(?:</(?:pre|code)\s*>|\Z)"
    r"|(?P<ticks>`+)[\s\S]*?(?:(?P=ticks)|\Z)|“[^”]*(?:”|\Z)|‘[^’]*(?:’|\Z)"
    r'|"[^"]*(?:"|\Z)|\'[^\']*(?:\'|\Z)|「[^」]*(?:」|\Z)|『[^』]*(?:』|\Z)',
    re.IGNORECASE,
)
_FORMAT = "%Y-%m-%d %H:%M:%S"


class _NeedClarification(ValueError):
    pass


def _plain_text(text: str) -> str:
    # Newlines prevent a verb and target on either side of a quote from joining.
    text = _CODE_OR_QUOTE_RE.sub("\n", text)
    lines: list[str] = []
    in_blockquote = False
    for line in text.splitlines():
        if not line.strip():
            in_blockquote = False
        elif line.lstrip().startswith(">"):
            in_blockquote = True
        if in_blockquote or line.startswith(("    ", "\t")):
            lines.append("")
        else:
            lines.append(line)
    return "\n".join(lines)


def _has_named_group(text: str) -> bool:
    # After removing current-group names and request scaffolding, a name before
    # 群 is an explicit destination, e.g. 猫猫群, rather than generic 群聊.
    residue = re.sub(r"本群|当前群|这个群|我们群|咱们群", "", text)
    for pattern in (_DATE_RE, _HOURS_RE, _CLOCK_RE, _ACTION_RE, _FUZZY_RE, _UNPARSED_TIME_RE):
        residue = pattern.sub(" ", residue)
    residue = re.sub(rf"(?:{_INTEGER}|几)(?:个)?小时|[0-9]+(?:[/.-][0-9]+)*", " ", residue)
    residue = re.sub(r"请|帮我|给我|能不能|能否|可以|一下|一遍|的|这段|最近几?|过去|近|到|至|前|后", " ", residue)
    return bool(re.search(r"[A-Za-z0-9_\u4e00-\u9fff]+群", residue))


def _request_clause(text: str) -> str | None:
    if _EXTERNAL_RE.search(text) or _has_named_group(text):
        return None
    candidates: list[str] = []
    for clause in re.split(r"[\n。!?！？;；]", text):
        for action in _ACTION_RE.finditer(clause):
            before, after = clause[: action.start()], clause[action.end() :].lstrip()
            if _NONREQUEST_PREFIX_RE.search(before):
                continue
            if _NONREQUEST_SUFFIX_RE.search(after) or _OTHER_TARGET_RE.search(after):
                continue
            target = after
            for pattern in (_DATE_RE, _HOURS_RE, _CLOCK_RE, _PERIOD_RE):
                target = pattern.sub(" ", target)
            target = re.sub(r"^(?:一下|一遍|下)?\s*(?:的)?\s*", "", target)
            if _OTHER_TARGET_RE.search(target) or _DOCUMENT_OBJECT_RE.search(target):
                continue
            # A trailing noun such as 昨天群聊的总结 is not an imperative.
            if not after and not re.search(r"请|帮我|给我|能否|能不能", before):
                continue
            if _TARGET_RE.search(clause):
                candidates.append(clause)
                break
    # Do not choose one of several requests and silently ignore the others.
    return "\n".join(candidates) if candidates else None


def _integer(value: str) -> int:
    if value.isascii() and value.isdigit():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "十" not in value and "百" not in value:
        if len(value) != 1:
            raise ValueError("ambiguous Chinese number")
        return digits[value]
    # Validate the common positional forms, not arbitrary repeated units.
    if not re.fullmatch(
        rf"(?:[一二两三四五六七八九]百(?:零)?(?:[一二两三四五六七八九]十[{_CN_DIGITS}]?|[{_CN_DIGITS}])?"
        rf"|[一二两三四五六七八九]?十[{_CN_DIGITS}]?)",
        value,
    ):
        raise ValueError("invalid Chinese number")
    result = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        else:
            result += (current or 1) * (100 if char == "百" else 10)
            current = 0
    return result + current


def _date(match: re.Match[str], now: datetime) -> tuple[date, str]:
    relative = match.group("relative")
    if relative:
        offset = 2 if relative == "前天" else 0 if relative.startswith("今") else 1
        period = "晚上" if relative in ("昨晚", "昨夜") else "早上" if relative in (
            "昨早", "昨晨", "今早", "今晨"
        ) else ""
        return now.date() - timedelta(days=offset), period
    if match.group("iso"):
        year, month, day = map(int, match.group("iso").split("-"))
    else:
        year = int(match.group("year") or now.year)
        month, day = _integer(match.group("month")), _integer(match.group("day"))
    return date(year, month, day), ""


def _clock(match: re.Match[str], inherited_period: str) -> tuple[time, str]:
    explicit_period = match.group("period") or ""
    period = explicit_period or inherited_period
    hour = _integer(match.group("colon_hour") or match.group("hour"))
    minute = _integer(match.group("colon_minute") or match.group("minute") or "0")
    second = _integer(match.group("colon_second") or match.group("second") or "0")
    fraction = match.group("fraction")
    if fraction:
        minute = {"半": 30, "一刻": 15, "三刻": 45}[fraction]
    if hour > 23 or minute > 59 or second > 59:
        raise _NeedClarification("钟点无效，请给出有效的起止时间。")
    # An inherited afternoon qualifier applies to 8, but not an explicit 20.
    if period and hour > 12:
        if explicit_period:
            raise _NeedClarification("时段与钟点不一致，请确认起止时间。")
        period = ""
    if period:
        if period == "中午":
            if hour in (1, 2):
                hour += 12
            elif hour not in (11, 12):
                raise _NeedClarification("中午的钟点不明确，请用24小时制。")
        elif period in ("下午", "傍晚", "晚上", "晚间", "夜里", "夜间"):
            if hour == 0 or (hour == 12 and period not in ("下午", "傍晚")):
                raise _NeedClarification("午夜属于哪一天？请写明日期和起止时间。")
            hour = hour if hour == 12 else hour + 12
        elif period == "凌晨" and hour == 12:
            hour = 0
    return time(hour, minute, second), explicit_period or inherited_period


def _without_matches(text: str, matches: list[re.Match[str]]) -> str:
    for match in sorted(matches, key=lambda item: item.start(), reverse=True):
        text = text[: match.start()] + " " + text[match.end() :]
    return text


def _checked_bounds(start: datetime, end: datetime, now: datetime) -> tuple[str, str]:
    if start.timestamp() >= end.timestamp():
        raise _NeedClarification("起止时间倒置或相同，请确认日期和起止时间。")
    if start.timestamp() > now.timestamp() or end.timestamp() > now.timestamp():
        raise _NeedClarification("这段时间包含未来，请给出已经发生的起止时间。")
    return start.strftime(_FORMAT), end.strftime(_FORMAT)


def _parse_window(text: str, now: datetime) -> tuple[str, str]:
    if "\n" in text:
        raise _NeedClarification("请指定一个需要回顾的日期或时间段。")
    if _FUZZY_RE.search(text):
        raise _NeedClarification("具体是哪一天、几点到几点？")
    dates = list(_DATE_RE.finditer(text))
    clocks = list(_CLOCK_RE.finditer(text))
    hours = list(_HOURS_RE.finditer(text))
    residue = _without_matches(text, dates + clocks + hours)
    if _UNPARSED_TIME_RE.search(residue):
        raise _NeedClarification("请写明具体日期和起止时间。")
    if hours:
        if len(hours) != 1 or dates or clocks or re.search(r"前|后|到|至", residue):
            raise _NeedClarification("请确认一个明确的最近几小时范围。")
        number = hours[0].group("number")
        duration = Decimal("0.5") if number == "半" else (
            Decimal(number) if number.isascii() else Decimal(_integer(number))
        )
        half = hours[0].group("half") or hours[0].group("before_half")
        if (number == "半" and half) or (hours[0].group("half") and hours[0].group("before_half")):
            raise _NeedClarification("请确认一个明确的最近几小时范围。")
        if half:
            duration += Decimal("0.5")
        if duration <= 0:
            raise _NeedClarification("要回顾最近几小时？请输入大于零的时长。")
        seconds = duration * 3600
        if seconds != seconds.to_integral_value():
            raise _NeedClarification("请给出至少一秒且精确到秒的时长。")
        start = datetime.fromtimestamp(now.timestamp() - int(seconds), now.tzinfo)
        return _checked_bounds(start, now, now)
    if not dates:
        raise _NeedClarification("要回顾哪一天或最近几小时的群聊？")
    if len(dates) > 2 or len(clocks) not in (0, 2):
        raise _NeedClarification("请给出一个完整的起止时间段。")
    parsed_dates = [_date(match, now) for match in dates]
    if not clocks:
        if any(period for _, period in parsed_dates):
            raise _NeedClarification("具体几点到几点？")
        if len(dates) == 2 and not _CONNECTOR_RE.fullmatch(
            text[dates[0].end() : dates[1].start()]
        ):
            raise _NeedClarification("请确认一个连续的日期范围。")
        start = datetime.combine(parsed_dates[0][0], time.min, now.tzinfo)
        end_date = parsed_dates[-1][0]
        end = now if end_date == now.date() else (
            datetime.combine(end_date + timedelta(days=1), time.min, now.tzinfo)
            - timedelta(seconds=1)
        )
        return _checked_bounds(start, end, now)
    if dates[0].end() > clocks[0].start():
        raise _NeedClarification("请在钟点前写明日期。")
    if re.search(r"[+\-.0-9]\s*$", text[dates[0].end() : clocks[0].start()]):
        raise _NeedClarification("钟点格式不明确，请写明有效的起止时间。")
    if len(dates) == 2 and not (
        clocks[0].end() <= dates[1].start() < dates[1].end() <= clocks[1].start()
    ):
        raise _NeedClarification("请分别写明起点和终点的日期、钟点。")
    between = text[clocks[0].end() : clocks[1].start()]
    if len(dates) == 2:
        between = _DATE_RE.sub("", between)
    if not _CONNECTOR_RE.fullmatch(between):
        raise _NeedClarification("请用“到”连接明确的起止时间。")
    first_day, first_period = parsed_dates[0]
    last_day, last_period = parsed_dates[-1]
    if first_period and clocks[0].group("period") not in (None, first_period):
        raise _NeedClarification("日期时段与钟点不一致，请确认起止时间。")
    first_clock, inherited = _clock(clocks[0], first_period)
    last_clock, _ = _clock(clocks[1], last_period if len(dates) == 2 else inherited)
    return _checked_bounds(
        datetime.combine(first_day, first_clock, now.tzinfo),
        datetime.combine(last_day, last_clock, now.tzinfo),
        now,
    )


def parse_history_recall(text: str, *, now: datetime) -> HistoryRecallIntent | None:
    """Return None for nonrequests, empty bounds plus a question for ambiguity.

    ``now`` must be an aware event-time datetime; no system clock is consulted.
    A successful result always has both bounds, with ``start < end <= now``.
    This intentionally recognizes a limited grammar rather than guessing via an
    LLM. Quoted subjects, multiple requests, unfamiliar dates, and midnight
    ambiguity may therefore need rephrasing. The caller still validates scope.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware event-time datetime")
    if not isinstance(text, str) or not text.strip():
        return None
    text = _plain_text(unicodedata.normalize("NFKC", text))
    request = _request_clause(text)
    if request is None:
        return None
    try:
        start, end = _parse_window(request, now.replace(microsecond=0))
    except _NeedClarification as exc:
        return HistoryRecallIntent(clarification=str(exc))
    except (ValueError, OverflowError, OSError, InvalidOperation):
        return HistoryRecallIntent(clarification="日期或时间无效，请确认具体日期和起止时间。")
    return HistoryRecallIntent(start=start, end=end)
