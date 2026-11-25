"""
OpenQuarkBackup —— 把 PrimeBackup 的备份仓库按 Cron 计划打包，并自动上传到
挂载在 OpenList 上的夸克网盘。

插件 id: ``open_quark_backup``

模块划分：
* :mod:`config`  配置模型、自动探测、运行时解析
* :mod:`cron`    五段式 Cron 解析与下一次触发计算（零依赖）
* :mod:`packer`  SQLite 在线快照 + 无压缩 tar + exFAT/临时文件过滤
* :mod:`prune`   远端有效备份识别与滚动清理决策
* :mod:`openlist_api`  OpenList（兼容 AList v3）客户端与流式上传
* :mod:`job`     一次完整任务的编排
* :mod:`scheduler`  Cron 调度线程

**时间安排提醒**：务必把本插件的 Cron 与 PrimeBackup 本体的自动备份时间岔开
（建议间隔 30-60 分钟）。例如 PB 每天 01:00 备份，本插件默认 ``0 2 * * 1``
（每周一 02:00），既避免同时读写 SQLite 与磁盘 IO 撞车，又能把刚打好的新存档送上云。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from mcdreforged.api.all import (
    CommandSource,
    Literal,
    PermissionLevel,
    PluginServerInterface,
    RColor,
    RText,
)

from . import config as config_module
from .config import Config, ResolvedConfig
from .cron import CronSyntaxError
from .job import BackupJob
from .openlist_api import OpenListClient, OpenListError, redact
from .prune import human_size
from .prune import RemoteEntry, decide_prune, is_valid_backup_name
from .scheduler import CronScheduler

#: 命令都要求 MCDR 管理员（admin, 权限 3）或更高。
MANAGE_LEVEL = PermissionLevel.ADMIN


class OpenQuarkBackupPlugin:
    def __init__(self, server: PluginServerInterface):
        self.server = server
        self.logger = server.logger
        self.config: Optional[Config] = None
        self.resolved: Optional[ResolvedConfig] = None
        self.job: Optional[BackupJob] = None
        self.scheduler: Optional[CronScheduler] = None
        self.enabled = True
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def load(self) -> None:
        try:
            self._reload(initial=True)
        except Exception:
            self.logger.exception('OpenQuarkBackup 初始化失败，插件将保持未启用状态')
            return
        self._register_commands()
        self.server.register_help_message('!!oqb', 'OpenQuarkBackup 备份管理', permission=MANAGE_LEVEL)

    def unload(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()
            self.scheduler = None
        if self.job is not None:
            self.job.cancel()

    # ------------------------------------------------------------------ 配置
    def _reload(self, initial: bool = False) -> None:
        raw, resolved = config_module.load_config(self.server, self.logger)
        self.config = raw
        self.resolved = resolved
        self.enabled = bool(raw.enabled)

        if resolved.cron_error:
            self.logger.error('Cron 表达式非法，已暂停定时调度（手动 !!oqb backup 仍可用）: {}'.format(
                resolved.cron_error))
        if resolved.tz_error:
            self.logger.warning(resolved.tz_error)

        self.job = BackupJob(resolved, self.logger, on_finished=self._on_job_finished)

        if self.scheduler is None:
            self.scheduler = CronScheduler(
                spec=resolved.cron_spec,
                tz=resolved.tz,
                callback=self._on_cron,
                logger=self.logger,
                enabled=self.enabled,
            )
            self.scheduler.start()
        else:
            self.scheduler.refresh(resolved.cron_spec, resolved.tz, self.enabled)

        self._log_next_fire(initial)

    def _log_next_fire(self, initial: bool) -> None:
        r = self.resolved
        if not self.enabled:
            self.logger.info('插件已禁用（enabled=false），不会自动触发备份')
            return
        if r.cron_spec is None:
            return
        now = datetime.now(r.tz)
        upcoming = r.next_fire_times(now, 1)
        if upcoming:
            self.logger.info('Cron {}（{}）下一次触发: {}'.format(
                r.cron_expression, r.timezone_name, upcoming[0].strftime('%Y-%m-%d %H:%M:%S %z')))

    # ------------------------------------------------------------------ 任务回调
    def _on_cron(self) -> None:
        if not self.enabled:
            return
        if self.job is None:
            return
        if not self.job.start('cron'):
            self.logger.warning('上一次备份仍在进行，跳过本次 Cron 触发')

    def _on_job_finished(self, summary: dict) -> None:
        # 失败原因已由 BackupJob 记录过一条完整日志，这里只补一句结论，避免重复打印
        if summary.get('last_ok'):
            self.logger.info('OpenQuarkBackup 任务完成: {}（{}）'.format(
                summary.get('last_name'), summary.get('last_size')))
        else:
            self.logger.error('OpenQuarkBackup 任务失败，可执行 !!oqb status 查看原因')

    # ------------------------------------------------------------------ 命令
    def _register_commands(self) -> None:
        root = (
            Literal('!!oqb')
            .requires(
                lambda source: source.has_permission(MANAGE_LEVEL),
                lambda source: RText('需要 MCDR 管理员（admin）或更高权限', RColor.red),
            )
            .runs(self._cmd_help)
        )
        root.then(Literal('backup').runs(self._cmd_backup))
        root.then(Literal('status').runs(self._cmd_status))
        root.then(Literal('list').runs(self._cmd_list))
        root.then(Literal('config').runs(self._cmd_config))
        root.then(Literal('reload').runs(self._cmd_reload))
        self.server.register_command(root)

    def _cmd_help(self, source: CommandSource) -> None:
        r = self.resolved
        lines = [
            RText('===== OpenQuarkBackup =====', RColor.aqua),
            RText('  !!oqb backup  立即执行一次打包 + 上传 + 滚动清理', RColor.gray),
            RText('  !!oqb status  查看运行状态与上次结果', RColor.gray),
            RText('  !!oqb list    列出远端有效备份包', RColor.gray),
            RText('  !!oqb config  查看当前生效配置（脱敏）', RColor.gray),
            RText('  !!oqb reload  热重载 config.json 并刷新 Cron', RColor.gray),
        ]
        if r is not None:
            state = '启用' if self.enabled else '已禁用'
            lines.append(RText('  状态: {} | Cron: {} | 时区: {}'.format(
                state, r.cron_expression, r.timezone_name), RColor.gray))
            if r.cron_spec is not None and self.enabled:
                upcoming = r.next_fire_times(datetime.now(r.tz), 1)
                if upcoming:
                    lines.append(RText('  下一次计划: {}'.format(
                        upcoming[0].strftime('%Y-%m-%d %H:%M:%S %z')), RColor.gray))
            if r.cron_error:
                lines.append(RText('  Cron 错误: {}'.format(r.cron_error), RColor.red))
        for line in lines:
            source.reply(line)

    def _cmd_backup(self, source: CommandSource) -> None:
        if self.job is None:
            source.reply(RText('插件未正确初始化，请查看控制台日志', RColor.red))
            return
        if not self.job.start('manual'):
            source.reply(RText('已有备份任务在运行，请稍候（!!oqb status 查看进度）', RColor.yellow))
            return
        source.reply(RText('已开始备份，完成后会在控制台输出结果', RColor.green))

    def _cmd_status(self, source: CommandSource) -> None:
        job = self.job
        r = self.resolved
        if job is None or r is None:
            source.reply(RText('插件未正确初始化', RColor.red))
            return
        summary = job.summary()
        source.reply(RText('===== 运行状态 =====', RColor.aqua))
        source.reply(RText('  当前阶段: {}'.format(summary['phase']), RColor.gray))
        if job.is_running():
            source.reply(RText('  运行中: 是（触发方式 {}）'.format(summary['trigger']), RColor.green))
        else:
            source.reply(RText('  运行中: 否', RColor.gray))
        if summary['started_at']:
            source.reply(RText('  上次开始: {}'.format(
                summary['started_at'].strftime('%Y-%m-%d %H:%M:%S %z')), RColor.gray))
        if summary['finished_at']:
            source.reply(RText('  上次结束: {}'.format(
                summary['finished_at'].strftime('%Y-%m-%d %H:%M:%S %z')), RColor.gray))
        if summary['last_name']:
            source.reply(RText('  上次文件: {}（{} 字节，耗时 {:.1f} 秒）'.format(
                summary['last_name'], summary['last_size'], summary['last_duration']), RColor.gray))
        if summary['last_ok'] is True:
            source.reply(RText('  上次结果: 成功', RColor.green))
        elif summary['last_ok'] is False:
            source.reply(RText('  上次结果: 失败 - {}'.format(summary['last_error']), RColor.red))
        else:
            source.reply(RText('  上次结果: 尚未执行', RColor.gray))
        if summary['removed']:
            source.reply(RText('  上次清理: {}'.format(', '.join(summary['removed'])), RColor.gray))
        source.reply(RText('  远端目录: {}'.format(r.remote_dir), RColor.gray))
        source.reply(RText('  保留份数: {}'.format(
            '不自动清理' if r.max_keep <= 0 else r.max_keep), RColor.gray))
        if self.enabled and r.cron_spec is not None:
            upcoming = r.next_fire_times(datetime.now(r.tz), 1)
            if upcoming:
                source.reply(RText('  下一次计划: {}'.format(
                    upcoming[0].strftime('%Y-%m-%d %H:%M:%S %z')), RColor.gray))

    def _cmd_config(self, source: CommandSource) -> None:
        r = self.resolved
        if r is None:
            source.reply(RText('插件未正确初始化', RColor.red))
            return
        source.reply(RText('===== 当前配置（已脱敏）=====', RColor.aqua))
        rows = [
            ('enabled', str(self.enabled)),
            ('pb_files_dir', r.pb_files_dir),
            ('数据库', r.db_path),
            ('include_pb_config', str(r.include_pb_config)),
            ('pb_config_path', r.pb_config_path or '(未启用)'),
            ('temp_dir', r.temp_dir),
            ('cron', r.cron_expression),
            ('timezone', '{}（{}）'.format(r.timezone_name, r.tz)),
            ('openlist_url', r.openlist_url),
            ('openlist_username', r.openlist_username or '(未配置)'),
            ('openlist_password', redact(r.openlist_password)),
            ('openlist_token', redact(r.openlist_token)),
            ('remote_dir', r.remote_dir),
            ('filename_prefix', r.filename_prefix),
            ('max_keep', '{}（{}）'.format(r.max_keep, '不自动清理' if r.max_keep <= 0 else '保留最新 N 份')),
            ('min_valid_size_bytes', str(r.min_valid_size_bytes)),
            ('as_task', str(r.as_task)),
            ('timeout_sec', str(r.timeout_sec)),
            ('retry_times', str(r.retry_times)),
            ('min_free_space_ratio', str(r.min_free_space_ratio)),
        ]
        for key, value in rows:
            source.reply(RText('  {}: {}'.format(key, value), RColor.gray))
        if r.cron_error:
            source.reply(RText('  Cron 错误: {}'.format(r.cron_error), RColor.red))
        if r.tz_error:
            source.reply(RText('  时区警告: {}'.format(r.tz_error), RColor.yellow))

    def _cmd_reload(self, source: CommandSource) -> None:
        try:
            self._reload()
        except Exception as e:
            self.logger.exception('重载配置失败')
            source.reply(RText('重载失败: {}'.format(e), RColor.red))
            return
        r = self.resolved
        message = '配置已重载'
        if r is not None and self.enabled and r.cron_spec is not None:
            upcoming = r.next_fire_times(datetime.now(r.tz), 1)
            if upcoming:
                message += '，下一次计划: {}'.format(upcoming[0].strftime('%Y-%m-%d %H:%M:%S %z'))
        if r is not None and r.cron_error:
            source.reply(RText('{}；但 Cron 非法，定时调度已暂停: {}'.format(message, r.cron_error), RColor.yellow))
            return
        source.reply(RText(message, RColor.green))

    def _cmd_list(self, source: CommandSource) -> None:
        r = self.resolved
        if r is None:
            source.reply(RText('插件未正确初始化', RColor.red))
            return

        def worker() -> None:
            try:
                client = OpenListClient(
                    base_url=r.openlist_url,
                    token=r.openlist_token,
                    username=r.openlist_username,
                    password=r.openlist_password,
                    timeout=30.0,
                    as_task=r.as_task,
                    logger=self.logger,
                )
                items = client.list_dir(r.remote_dir, refresh=True)
            except OpenListError as e:
                source.reply(RText('读取远端目录失败: {}'.format(e), RColor.red))
                return
            except Exception as e:
                self.logger.exception('列出远端备份失败')
                source.reply(RText('读取远端目录出错: {}'.format(e), RColor.red))
                return

            entries = [RemoteEntry.from_openlist(item) for item in items]
            valid = sorted(
                (e for e in entries
                 if not e.is_dir and is_valid_backup_name(e.name, r.filename_prefix)
                 and e.size > r.min_valid_size_bytes),
                key=lambda e: e.name,
                reverse=True,
            )
            if not valid:
                source.reply(RText('远端目录 {} 里没有有效备份包'.format(r.remote_dir), RColor.yellow))
            else:
                source.reply(RText('===== 远端备份（{} 份）====='.format(len(valid)), RColor.aqua))
                for entry in valid:
                    source.reply(RText('  {}  {}'.format(entry.name, human_size(entry.size)), RColor.gray))
            decision = decide_prune(entries, r.filename_prefix, r.max_keep, r.min_valid_size_bytes)
            for entry in decision.suspicious:
                source.reply(RText('  注意: {} 体积仅 {} 字节，疑似异常文件'.format(entry.name, entry.size),
                                   RColor.yellow))

        threading.Thread(target=worker, name='open_quark_backup-list', daemon=True).start()


PLUGIN: Optional[OpenQuarkBackupPlugin] = None


def on_load(server: PluginServerInterface, prev_module) -> None:
    global PLUGIN
    PLUGIN = OpenQuarkBackupPlugin(server)
    PLUGIN.load()


def on_unload(server: PluginServerInterface) -> None:
    global PLUGIN
    if PLUGIN is not None:
        PLUGIN.unload()
        PLUGIN = None
