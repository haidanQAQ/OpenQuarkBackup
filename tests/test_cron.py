"""单元测试：五段式 Cron 解析与下一次触发时间计算。"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_quark_backup.cron import CronSpec, CronSyntaxError, upcoming_fires  # noqa: E402

TZ = None


def setUpModule():
    global TZ
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('Asia/Shanghai')


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class CronParseTest(unittest.TestCase):
    def test_default_expression(self):
        spec = CronSpec.parse('0 2 * * 1')
        self.assertEqual(spec.minutes, {0})
        self.assertEqual(spec.hours, {2})
        self.assertTrue(spec.dow_restricted)
        self.assertFalse(spec.dom_restricted)

    def test_step_expression(self):
        spec = CronSpec.parse('*/15 * * * *')
        self.assertEqual(spec.minutes, {0, 15, 30, 45})

    def test_range_and_range_step(self):
        spec = CronSpec.parse('0 9-18 * * *')
        self.assertEqual(spec.hours, set(range(9, 19)))
        spec = CronSpec.parse('0 9-18/3 * * *')
        self.assertEqual(spec.hours, {9, 12, 15, 18})

    def test_list(self):
        spec = CronSpec.parse('0 0 1,15,31 * *')
        self.assertEqual(spec.days, {1, 15, 31})

    def test_combined_list_and_step(self):
        spec = CronSpec.parse('*/6,30 * 1-10 * *')
        self.assertEqual(spec.minutes, set(range(0, 60, 6)) | {30})
        self.assertEqual(spec.days, set(range(1, 11)))

    def test_sunday_zero_and_seven(self):
        self.assertEqual(CronSpec.parse('0 3 * * 0').weekdays, {0})
        self.assertEqual(CronSpec.parse('0 3 * * 7').weekdays, {0})
        self.assertEqual(CronSpec.parse('0 3 * * 0,7').weekdays, {0})

    def test_invalid_expressions(self):
        cases = [
            '',                 # 空
            '* * * *',          # 4 段
            '* * * * * *',      # 6 段
            '60 * * * *',       # 分钟越界
            '* 24 * * *',       # 小时越界
            '0 0 32 * *',       # 日越界
            '0 0 * 13 *',       # 月越界
            '0 0 * * 8',        # 周越界
            '*/0 * * * *',      # 步长为 0
            '0 5-1 * * *',      # 范围反向
            'a * * * *',        # 非数字
            '0 0 1,,2 * *',     # 多余逗号
        ]
        for expression in cases:
            with self.subTest(expression=expression):
                with self.assertRaises(CronSyntaxError):
                    CronSpec.parse(expression)

    def test_error_message_mentions_field(self):
        with self.assertRaises(CronSyntaxError) as ctx:
            CronSpec.parse('0 0 99 * *')
        self.assertIn('day-of-month', str(ctx.exception))


class CronNextFireTest(unittest.TestCase):
    def test_weekly_monday(self):
        spec = CronSpec.parse('0 2 * * 1')
        # 2026-11-25 是周三
        start = at(2026, 11, 25, 3, 0)
        nxt = spec.next_fire(start)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.isoweekday(), 1)
        self.assertEqual((nxt.hour, nxt.minute), (2, 0))
        self.assertGreater(nxt, start)
        self.assertLessEqual(nxt - start, timedelta(days=7))

    def test_every_fifteen_minutes(self):
        spec = CronSpec.parse('*/15 * * * *')
        self.assertEqual(spec.next_fire(at(2026, 11, 25, 10, 7)), at(2026, 11, 25, 10, 15))
        self.assertEqual(spec.next_fire(at(2026, 11, 25, 10, 15)), at(2026, 11, 25, 10, 30))

    def test_daily_range_start(self):
        spec = CronSpec.parse('0 9-18 * * *')
        self.assertEqual(spec.next_fire(at(2026, 11, 25, 8, 30)), at(2026, 11, 25, 9, 0))
        self.assertEqual(spec.next_fire(at(2026, 11, 25, 18, 30)), at(2026, 11, 26, 9, 0))

    def test_cross_month(self):
        spec = CronSpec.parse('0 0 1 * *')
        self.assertEqual(spec.next_fire(at(2026, 11, 25, 10, 0)), at(2026, 12, 1, 0, 0))

    def test_cross_year(self):
        spec = CronSpec.parse('0 0 1 1 *')
        self.assertEqual(spec.next_fire(at(2026, 12, 31, 23, 0)), at(2027, 1, 1, 0, 0))

    def test_leap_day(self):
        spec = CronSpec.parse('0 0 29 2 *')
        self.assertEqual(spec.next_fire(at(2027, 3, 1, 0, 0)), at(2028, 2, 29, 0, 0))

    def test_unreachable_expression_returns_none(self):
        spec = CronSpec.parse('0 0 30 2 *')  # 2 月没有 30 号
        self.assertIsNone(spec.next_fire(at(2026, 1, 1, 0, 0)))

    def test_dom_dow_or_semantics(self):
        spec = CronSpec.parse('0 3 13 * 5')  # 13 号或周五
        # 2026-11-13 是周五，两者同时命中
        self.assertEqual(spec.next_fire(at(2026, 11, 12, 23, 0)), at(2026, 11, 13, 3, 0))
        # 2026-11-20 是周五但不是 13 号：只靠周字段也要能触发
        self.assertEqual(spec.next_fire(at(2026, 11, 14, 0, 0)), at(2026, 11, 20, 3, 0))

    def test_upcoming_fires(self):
        spec = CronSpec.parse('0 2 * * 1')
        fires = upcoming_fires(spec, at(2026, 11, 25, 0, 0), 3)
        self.assertEqual(len(fires), 3)
        self.assertEqual([d.isoweekday() for d in fires], [1, 1, 1])
        self.assertEqual(fires, sorted(fires))

    def test_returns_aware_datetime(self):
        spec = CronSpec.parse('0 2 * * *')
        nxt = spec.next_fire(at(2026, 11, 25, 0, 0))
        self.assertIsNotNone(nxt)
        self.assertIsNotNone(nxt.tzinfo)


if __name__ == '__main__':
    unittest.main(verbosity=2)
