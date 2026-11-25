"""
远端备份包的"有效文件"识别与滚动清理决策。

这一层不接触网络，只做纯逻辑计算，方便单元测试覆盖：
* 哪些名字才算本插件生成的备份包；
* 哪些文件要计入保留配额（体积过小的空壳文件只告警、不计数、不删除）；
* 当数量超过 ``max_keep`` 时应该删掉哪些（时间码最早的优先）。

注意：本模块不导入 mcdreforged。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

#: 包名中的时间码格式，与 ``filename_prefix`` 拼接后整体匹配。
_TIMESTAMP_FORMAT = '%Y%m%d_%H%M%S'
_TIMESTAMP_LENGTH = 15  # len('YYYYMMDD_HHMMSS')


@dataclass
class RemoteEntry:
    """远端目录里的一个条目（只保留清理需要的字段）。"""

    name: str
    size: int
    is_dir: bool = False

    @classmethod
    def from_openlist(cls, data: dict) -> 'RemoteEntry':
        return cls(
            name=str(data.get('name', '')),
            size=int(data.get('size') or 0),
            is_dir=bool(data.get('is_dir', False)),
        )


@dataclass
class PruneDecision:
    """清理决策结果。"""

    valid: List[RemoteEntry] = field(default_factory=list)
    to_delete: List[RemoteEntry] = field(default_factory=list)
    suspicious: List[RemoteEntry] = field(default_factory=list)
    ignored: List[RemoteEntry] = field(default_factory=list)

    @property
    def nothing_to_do(self) -> bool:
        return not self.to_delete


def build_name_pattern(filename_prefix: str) -> 're.Pattern[str]':
    """构造严格匹配 ``{prefix}YYYYMMDD_HHMMSS.tar`` 的正则。"""
    return re.compile('^' + re.escape(filename_prefix) + r'\d{8}_\d{6}\.tar$')


def is_valid_backup_name(name: str, filename_prefix: str) -> bool:
    """名字是否严格符合本插件的备份包命名规则。"""
    return build_name_pattern(filename_prefix).match(name) is not None


def timestamp_of(name: str, filename_prefix: str) -> Optional[str]:
    """从包名中取出 ``YYYYMMDD_HHMMSS`` 时间码；名字不合法时返回 None。"""
    if not is_valid_backup_name(name, filename_prefix):
        return None
    start = len(filename_prefix)
    return name[start:start + _TIMESTAMP_LENGTH]


def format_backup_name(filename_prefix: str, moment) -> str:
    """按配置时区下的时间生成包名：``{prefix}YYYYMMDD_HHMMSS.tar``。"""
    return '{}{}.tar'.format(filename_prefix, moment.strftime(_TIMESTAMP_FORMAT))


def human_size(num: int) -> str:
    """把字节数格式化成便于阅读的字符串，小于 1 MB 时用 KB，避免显示成 0.0 MB。"""
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return '{:.0f}B'.format(value) if unit == 'B' else '{:.1f}{}'.format(value, unit)
        value /= 1024
    return '{:.1f}TB'.format(value)


def parse_backup_time(name: str, filename_prefix: str):
    """把包名里的时间码解析成 datetime（naive）；解析失败返回 None。"""
    stamp = timestamp_of(name, filename_prefix)
    if stamp is None:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(stamp, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


def decide_prune(
    entries: Iterable[RemoteEntry],
    filename_prefix: str,
    max_keep: int,
    min_valid_size_bytes: int,
) -> PruneDecision:
    """
    根据远端列表决定要删除哪些备份包。

    规则（与需求文档一一对应）：
    * 目录、以 ``.`` 开头的隐藏项（含 exFAT 的 ``._*``）、名字不匹配正则的项一律忽略；
    * 名字匹配但体积 ``<= min_valid_size_bytes`` 的，视为异常空壳：记入 ``suspicious``
      用于告警，但**不**计入保留配额，也**不**删除；
    * ``max_keep <= 0`` 表示"永不自动清理"，此时 valid 仍会返回，但不产生删除项；
    * 有效包数量超过 ``max_keep`` 时，从时间码最早的开始删，直到剩 ``max_keep`` 个。
    """
    decision = PruneDecision()
    for entry in entries:
        if entry.is_dir:
            decision.ignored.append(entry)
            continue
        if entry.name.startswith('.'):
            # 包含 exFAT/macOS 生成的 ._xxx（AppleDouble）、.DS_Store 等
            decision.ignored.append(entry)
            continue
        if not is_valid_backup_name(entry.name, filename_prefix):
            decision.ignored.append(entry)
            continue
        if entry.size <= min_valid_size_bytes:
            decision.suspicious.append(entry)
            continue
        decision.valid.append(entry)

    # 包名里的时间码是定宽的 YYYYMMDD_HHMMSS，按名字排序即按时间排序
    decision.valid.sort(key=lambda e: e.name)
    if max_keep > 0 and len(decision.valid) > max_keep:
        surplus = len(decision.valid) - max_keep
        decision.to_delete = decision.valid[:surplus]
        decision.valid = decision.valid[surplus:]
    return decision


def sort_newest_first(entries: Sequence[RemoteEntry]) -> List[RemoteEntry]:
    return sorted(entries, key=lambda e: e.name, reverse=True)
