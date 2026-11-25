"""
五段式 Cron 表达式解析与"下一次触发时间"计算。

设计目标：
* 零第三方依赖，只用标准库；
* 支持 `*`、`*/n`、`a-b`、`a-b/n`、`a,b`（以及它们的组合）；
* 周字段同时接受 0 和 7 表示周日（与 Vixie cron 一致）；
* 日字段与周字段同时被限制时，按 Vixie cron 的语义取"或"（满足其一即可）；
* 用标准库 zoneinfo 计算指定时区（如 Asia/Shanghai）下的触发时刻，
  因此跨夏令时/时区切换也按运维配置的本地时间解释。

注意：本模块不导入 mcdreforged，方便脱离 MCDR 做单元测试。

**时间安排提醒**：本插件按这个 cron 触发「打包 + 上传」，而打包对象正是 PrimeBackup
的仓库目录。请务必让它与 PrimeBackup 本体的自动备份时间岔开（建议间隔 30-60 分钟），
否则两边同时读写 SQLite 与磁盘 IO 会互相拖慢，甚至让 PB 报数据库忙。例如 PB 每天
01:00 备份存档，本插件用默认的 ``0 2 * * 1``（每周一 02:00）把仓库送上云。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone as _timezone
from typing import Iterable, List, Optional, Sequence, Set, Tuple


class CronSyntaxError(ValueError):
    """Cron 表达式语法错误。错误信息里带字段名与非法片段，便于运维定位。"""


#: 各字段的 (名称, 最小值, 最大值)。周字段允许 0-7，7 归一化为 0。
_FIELD_SPECS: Sequence[Tuple[str, int, int]] = (
    ('minute', 0, 59),
    ('hour', 0, 23),
    ('day-of-month', 1, 31),
    ('month', 1, 12),
    ('day-of-week', 0, 7),
)

#: 计算下一次触发时最多向后搜索多少天（覆盖闰年 2 月 29 日这类稀疏表达式）。
_MAX_SEARCH_DAYS = 366 * 8


def _parse_int(text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise CronSyntaxError('字段 {} 中出现非法数字: {!r}'.format(field, text)) from None


def _parse_field(raw: str, field: str, lo: int, hi: int) -> Tuple[Set[int], bool]:
    """
    解析单个字段。

    返回 (取值集合, 是否被限制)。仅当字段恰为 ``*`` 时视为"未限制"，
    其余写法（包括 ``*/2``）都算限制，用于日/周字段的"或"语义判定。
    """
    raw = raw.strip()
    if raw == '':
        raise CronSyntaxError('字段 {} 为空'.format(field))

    values: Set[int] = set()

    def add_range(start: int, end: int, step: int) -> None:
        if start < lo or start > hi:
            raise CronSyntaxError('字段 {} 的起始值 {} 超出范围 {}-{}'.format(field, start, lo, hi))
        if end < lo or end > hi:
            raise CronSyntaxError('字段 {} 的结束值 {} 超出范围 {}-{}'.format(field, end, lo, hi))
        if start > end:
            raise CronSyntaxError('字段 {} 的范围 {}-{} 起点大于终点'.format(field, start, end))
        for value in range(start, end + 1, step):
            values.add(0 if (hi == 7 and value == 7) else value)

    for part in raw.split(','):
        part = part.strip()
        if part == '':
            raise CronSyntaxError('字段 {} 中出现空的列表项（多余的逗号？）'.format(field))

        if '/' in part:
            base, _, step_text = part.partition('/')
            step = _parse_int(step_text, field)
            if step <= 0:
                raise CronSyntaxError('字段 {} 的步长必须为正整数: {!r}'.format(field, part))
        else:
            base, step = part, 1

        if base == '*':
            add_range(lo, hi, step)
        elif '-' in base:
            start_text, _, end_text = base.partition('-')
            add_range(_parse_int(start_text, field), _parse_int(end_text, field), step)
        else:
            value = _parse_int(base, field)
            if step != 1:
                # ``5/10`` 视为 "从 5 开始到最大值、步长 10"，与常见实现一致
                add_range(value, hi, step)
            else:
                if value < lo or value > hi:
                    raise CronSyntaxError('字段 {} 的取值 {} 超出范围 {}-{}'.format(field, value, lo, hi))
                values.add(0 if (hi == 7 and value == 7) else value)

    if not values:
        raise CronSyntaxError('字段 {} 没有解析出任何取值: {!r}'.format(field, raw))
    return values, raw != '*'


class CronSpec:
    """解析后的 Cron 计划。"""

    __slots__ = ('expression', 'minutes', 'hours', 'days', 'months', 'weekdays',
                 'dom_restricted', 'dow_restricted')

    def __init__(self, expression: str, minutes: Set[int], hours: Set[int], days: Set[int],
                 months: Set[int], weekdays: Set[int], dom_restricted: bool, dow_restricted: bool):
        self.expression = expression
        self.minutes = minutes
        self.hours = hours
        self.days = days
        self.months = months
        self.weekdays = weekdays
        self.dom_restricted = dom_restricted
        self.dow_restricted = dow_restricted

    # ------------------------------------------------------------------ 解析
    @classmethod
    def parse(cls, expression: str) -> 'CronSpec':
        if not isinstance(expression, str):
            raise CronSyntaxError('Cron 表达式必须是字符串，当前为 {}'.format(type(expression).__name__))
        fields = expression.split()
        if len(fields) != 5:
            raise CronSyntaxError(
                'Cron 表达式必须是 5 段（分 时 日 月 周），当前 {} 段: {!r}'.format(len(fields), expression)
            )
        parsed = [
            _parse_field(fields[i], spec[0], spec[1], spec[2]) for i, spec in enumerate(_FIELD_SPECS)
        ]
        (minutes, _), (hours, _), (days, dom_restricted), (months, _), (weekdays, dow_restricted) = parsed
        return cls(expression, minutes, hours, days, months, weekdays, dom_restricted, dow_restricted)

    # ------------------------------------------------------------------ 判定
    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        # isoweekday(): 周一=1 ... 周日=7；Cron 的 0 表示周日
        dow_ok = (day.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    # -------------------------------------------------------------- 下一次
    def next_fire(self, after: datetime, max_days: int = _MAX_SEARCH_DAYS) -> Optional[datetime]:
        """
        返回严格晚于 ``after`` 的下一个触发时刻（带时区）。

        ``after`` 必须带时区信息；结果使用 ``after`` 的时区。
        若在 ``max_days`` 天内都不会触发（例如 ``0 0 30 2 *``），返回 None。
        """
        if after.tzinfo is None:
            raise ValueError('next_fire() 需要带时区信息的 datetime')
        tz = after.tzinfo

        # 从"下一分钟"开始找，保证严格大于 after
        start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)

        day = start.date()
        for _ in range(max_days):
            if self.matches_day(day):
                same_day = day == start.date()
                for hour in hours:
                    if same_day and hour < start.hour:
                        continue
                    for minute in minutes:
                        if same_day and hour == start.hour and minute < start.minute:
                            continue
                        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                        # 跳过夏令时"被跳过"的本地时间：转到 UTC 再转回来应当一致
                        back = candidate.astimezone(_timezone.utc).astimezone(tz)
                        if (back.year, back.month, back.day, back.hour, back.minute) != (
                            candidate.year, candidate.month, candidate.day, candidate.hour, candidate.minute
                        ):
                            continue
                        if candidate > after:
                            return candidate
            day += timedelta(days=1)
        return None

    # ---------------------------------------------------------------- 描述
    def describe(self) -> str:
        return '{}（分 时 日 月 周）'.format(self.expression)


def describe_spec(expression: str) -> str:
    """给日志/命令用的友好描述；解析失败时返回原始表达式。"""
    try:
        return CronSpec.parse(expression).describe()
    except CronSyntaxError:
        return expression


def upcoming_fires(spec: CronSpec, after: datetime, count: int = 3) -> List[datetime]:
    """列出接下来 count 次触发时间，便于命令里展示。"""
    result: List[datetime] = []
    cursor = after
    for _ in range(count):
        nxt = spec.next_fire(cursor)
        if nxt is None:
            break
        result.append(nxt)
        cursor = nxt
    return result
