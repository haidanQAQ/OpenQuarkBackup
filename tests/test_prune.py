"""单元测试：远端备份包的识别与滚动清理决策。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_quark_backup.prune import (  # noqa: E402
    RemoteEntry,
    decide_prune,
    format_backup_name,
    is_valid_backup_name,
    parse_backup_time,
    timestamp_of,
)

PREFIX = 'pb_backup_'


def entry(name, size=10 * 1024 * 1024, is_dir=False):
    return RemoteEntry(name=name, size=size, is_dir=is_dir)


def names(entries):
    return [e.name for e in entries]


class NamingTest(unittest.TestCase):
    def test_valid_names(self):
        self.assertTrue(is_valid_backup_name('pb_backup_20260101_020000.tar', PREFIX))
        self.assertTrue(is_valid_backup_name('pb_backup_20261231_235959.tar', PREFIX))

    def test_invalid_names(self):
        for name in [
            'pb_backup_20260101.tar',            # 缺时分秒
            'pb_backup_20260101_020000.zip',     # 后缀不对
            'other_20260101_020000.tar',         # 前缀不对
            'pb_backup_20260101_020000.tar.gz',  # 二次压缩
            '._pb_backup_20260101_020000.tar',   # exFAT AppleDouble
            'pb_backup_20260101_020000.TAR',     # 大小写敏感
        ]:
            self.assertFalse(is_valid_backup_name(name, PREFIX), name)

    def test_timestamp_and_parse(self):
        name = 'pb_backup_20260101_020000.tar'
        self.assertEqual(timestamp_of(name, PREFIX), '20260101_020000')
        parsed = parse_backup_time(name, PREFIX)
        self.assertEqual((parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute),
                         (2026, 1, 1, 2, 0))

    def test_format_name(self):
        from datetime import datetime
        name = format_backup_name(PREFIX, datetime(2026, 11, 26, 4, 5, 6))
        self.assertEqual(name, 'pb_backup_20261126_040506.tar')
        self.assertTrue(is_valid_backup_name(name, PREFIX))


class PruneDecisionTest(unittest.TestCase):
    def test_keeps_latest_when_over_limit(self):
        entries = [
            entry('pb_backup_20260101_020000.tar'),
            entry('pb_backup_20260108_020000.tar'),
            entry('pb_backup_20260115_020000.tar'),
            entry('pb_backup_20260122_020000.tar'),
            entry('pb_backup_20260129_020000.tar'),
        ]
        decision = decide_prune(entries, PREFIX, max_keep=4, min_valid_size_bytes=8192)
        self.assertEqual(names(decision.to_delete), ['pb_backup_20260101_020000.tar'])
        self.assertEqual(len(decision.valid), 4)
        self.assertEqual(len(decision.suspicious), 0)

    def test_removes_multiple_when_far_over_limit(self):
        entries = [entry('pb_backup_2026010{}_020000.tar'.format(i)) for i in range(1, 8)]
        decision = decide_prune(entries, PREFIX, max_keep=4, min_valid_size_bytes=8192)
        self.assertEqual(len(decision.to_delete), 3)
        self.assertEqual(names(decision.to_delete), sorted(names(decision.to_delete)))
        self.assertEqual(len(decision.valid), 4)

    def test_no_cleanup_when_max_keep_zero_or_negative(self):
        entries = [entry('pb_backup_2026010{}_020000.tar'.format(i)) for i in range(1, 8)]
        for max_keep in (0, -1):
            with self.subTest(max_keep=max_keep):
                decision = decide_prune(entries, PREFIX, max_keep=max_keep, min_valid_size_bytes=8192)
                self.assertEqual(decision.to_delete, [])
                self.assertEqual(len(decision.valid), 7)

    def test_ignores_unrelated_and_hidden_files(self):
        entries = [
            entry('pb_backup_20260101_020000.tar'),
            entry('pb_backup_20260108_020000.tar'),
            entry('pb_backup_20260115_020000.tar'),
            entry('pb_backup_20260122_020000.tar'),
            entry('pb_backup_20260129_020000.tar'),
            entry('notes.txt', size=100),
            entry('._pb_backup_20250101_020000.tar', size=4096),   # exFAT 隐藏文件
            entry('.DS_Store', size=32),
            entry('备份临时目录', is_dir=True),
            entry('pb_backup_20260101_020000.tar', size=100, is_dir=True),  # 同名目录
        ]
        decision = decide_prune(entries, PREFIX, max_keep=4, min_valid_size_bytes=8192)
        self.assertEqual(names(decision.to_delete), ['pb_backup_20260101_020000.tar'])
        self.assertEqual(len(decision.valid), 4)
        self.assertIn('._pb_backup_20250101_020000.tar', names(decision.ignored))
        self.assertIn('notes.txt', names(decision.ignored))

    def test_tiny_files_are_suspicious_not_counted_nor_deleted(self):
        entries = [
            entry('pb_backup_20260101_020000.tar'),
            entry('pb_backup_20260108_020000.tar'),
            entry('pb_backup_20260115_020000.tar'),
            entry('pb_backup_20260122_020000.tar'),
            entry('pb_backup_20260129_020000.tar'),
            entry('pb_backup_20250101_020000.tar', size=4096),  # 名字合法但体积异常
        ]
        decision = decide_prune(entries, PREFIX, max_keep=4, min_valid_size_bytes=8192)
        self.assertEqual(names(decision.suspicious), ['pb_backup_20250101_020000.tar'])
        self.assertEqual(names(decision.to_delete), ['pb_backup_20260101_020000.tar'])
        self.assertNotIn('pb_backup_20250101_020000.tar', names(decision.to_delete))

    def test_nothing_to_do_when_under_limit(self):
        entries = [entry('pb_backup_2026010{}_020000.tar'.format(i)) for i in range(1, 4)]
        decision = decide_prune(entries, PREFIX, max_keep=4, min_valid_size_bytes=8192)
        self.assertTrue(decision.nothing_to_do)

    def test_from_openlist_dict(self):
        parsed = RemoteEntry.from_openlist({'name': 'a.tar', 'size': '1024', 'is_dir': 0})
        self.assertEqual((parsed.name, parsed.size, parsed.is_dir), ('a.tar', 1024, False))


if __name__ == '__main__':
    unittest.main(verbosity=2)
