"""
Cron 调度线程。

只做一件事：在配置时区下计算"下一次触发时刻"，到点调用回调（回调内部再开
后台线程执行真正的备份，避免调度线程被长任务占住）。

设计取舍：
* 等待时最多睡 30 秒就醒来重新核对一次，这样系统时间被改动、或时区数据变化后
  不会错过/重复触发；
* ``!!oqb reload`` 通过 ``Event`` 打断等待并重新计算，不重建线程；
* 插件卸载时 ``stop()`` 能立刻唤醒并退出，不会留下"幽灵定时器"。

**时间安排提醒**：这里的 cron 决定什么时候打包 PrimeBackup 的仓库，请与 PrimeBackup
本体的自动备份时间岔开（建议间隔 30-60 分钟），避免同时读写 SQLite 与磁盘 IO。
"""

from __future__ import annotations

import threading
from datetime import datetime, tzinfo
from typing import Callable, Optional

from .cron import CronSpec

#: 单次最长睡眠时间（秒）。
_MAX_SLEEP = 30.0


class CronScheduler(threading.Thread):
    def __init__(self, spec: Optional[CronSpec], tz: tzinfo, callback: Callable[[], None],
                 logger, enabled: bool = True):
        super().__init__(name='open_quark_backup-cron', daemon=True)
        self._spec = spec
        self._tz = tz
        self._callback = callback
        self._logger = logger
        self._enabled = enabled
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self.next_fire: Optional[datetime] = None

    # ------------------------------------------------------------------ 控制
    def refresh(self, spec: Optional[CronSpec], tz: tzinfo, enabled: bool) -> None:
        """热重载后更新计划；线程会在下次循环重新计算。"""
        self._spec = spec
        self._tz = tz
        self._enabled = enabled
        self.next_fire = None
        self._wake.set()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        while not self._stopped.is_set():
            # 每轮开头清掉唤醒标志：重载请求只需要"立刻重算一次"，
            # 不清理会导致事件常驻、_sleep 立即返回，形成忙等。
            self._wake.clear()

            spec = self._spec
            if spec is None or not self._enabled:
                self.next_fire = None
                self._sleep(_MAX_SLEEP)
                continue

            now = datetime.now(self._tz)
            try:
                next_fire = spec.next_fire(now)
            except Exception:
                self._logger.exception('计算下次触发时间失败，暂停调度')
                next_fire = None

            if next_fire is None:
                self.next_fire = None
                self._logger.warning('Cron 表达式在未来 8 年内都不会触发（{}），暂停调度'.format(
                    spec.expression))
                self._sleep(3600.0)
                continue

            self.next_fire = next_fire
            delay = (next_fire - now).total_seconds()

            if delay > 0:
                self._sleep(min(delay, _MAX_SLEEP))
                if self._stopped.is_set():
                    break
                if self._wake.is_set():
                    # reload：丢弃本次计算，下一轮用新配置重算
                    self.next_fire = None
                    continue
                if datetime.now(self._tz) < next_fire:
                    continue  # 还没到点，继续等

            # 到了这一行说明"该触发了"：但 delay<=0 的路径不会经过上面的等待段，
            # 期间可能刚好有 refresh(enabled=False) 或 stop() 插进来。触发前再确认一次，
            # 否则会出现"已经停用/已经卸载了还跑了一次备份"。
            if self._stopped.is_set() or self._wake.is_set() or not self._enabled:
                continue

            if self._stopped.is_set():
                break
            self.next_fire = None
            self._logger.info('Cron 触发: {}'.format(
                next_fire.strftime('%Y-%m-%d %H:%M:%S %z')))
            try:
                self._callback()
            except Exception:
                self._logger.exception('Cron 回调执行出错')

    def _sleep(self, timeout: float) -> None:
        self._wake.wait(timeout)
