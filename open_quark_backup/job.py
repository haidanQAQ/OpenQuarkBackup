"""
一次完整备份任务的编排：打包 → 上传 → 校验 → 滚动清理。

所有耗时操作都在后台线程里执行，MCDR 主线程只负责接收命令与打日志。
同一时刻只允许一个任务运行（定时与手动共用同一把互斥锁）。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from . import packer, prune
from .config import ResolvedConfig
from .openlist_api import OpenListClient, OpenListError

#: 任务阶段，用于 ``!!oqb status`` 展示。
PHASE_IDLE = 'idle'
PHASE_PACKING = 'packing'
PHASE_UPLOADING = 'uploading'
PHASE_VERIFYING = 'verifying'
PHASE_PRUNING = 'pruning'


class BackupJob:
    def __init__(self, config: ResolvedConfig, logger, on_finished: Optional[Callable[[dict], None]] = None):
        self.config = config
        self.logger = logger
        self.on_finished = on_finished

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.phase = PHASE_IDLE
        self.trigger = ''
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.last_ok: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.last_name: Optional[str] = None
        self.last_size = 0
        self.last_uploaded = 0
        self.last_removed: List[str] = []
        self.last_duration = 0.0

    # ------------------------------------------------------------------ 查询
    def is_running(self) -> bool:
        return self._lock.locked()

    def cancel(self) -> None:
        self._cancel.set()

    def summary(self) -> dict:
        return {
            'phase': self.phase,
            'trigger': self.trigger,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'last_ok': self.last_ok,
            'last_error': self.last_error,
            'last_name': self.last_name,
            'last_size': self.last_size,
            'last_duration': self.last_duration,
            'removed': list(self.last_removed),
        }

    # ------------------------------------------------------------------ 启动
    def start(self, trigger: str) -> bool:
        """非阻塞启动任务；已有任务在跑时返回 False。"""
        if not self._lock.acquire(blocking=False):
            return False
        self._cancel.clear()
        self.trigger = trigger
        self.phase = PHASE_PACKING
        self.started_at = self._now()
        self.finished_at = None
        self.last_error = None
        self._thread = threading.Thread(
            target=self._run_guarded,
            name='open_quark_backup-worker',
            daemon=True,
        )
        self._thread.start()
        return True

    def _run_guarded(self) -> None:
        started = time.time()
        try:
            self._run()
        except Exception as e:
            self.last_ok = False
            self.last_error = str(e)
            self.logger.error('备份任务失败: {}'.format(e))
        finally:
            self.last_duration = time.time() - started
            self.finished_at = self._now()
            self.phase = PHASE_IDLE
            self._lock.release()
            summary = self.summary()
            if self.on_finished is not None:
                try:
                    self.on_finished(summary)
                except Exception:
                    self.logger.exception('任务完成回调出错')

    # ------------------------------------------------------------------ 主流程
    def _run(self) -> None:
        cfg = self.config
        now = datetime.now(cfg.tz)
        name = prune.format_backup_name(cfg.filename_prefix, now)
        remote_path = cfg.remote_dir.rstrip('/') + '/' + name
        self.last_name = name
        self.last_removed = []

        self.logger.info('开始备份（触发方式: {}），目标 {}'.format(self.trigger, remote_path))

        # 1) 基本检查
        if not os.path.isdir(cfg.pb_files_dir):
            raise RuntimeError('PrimeBackup 仓库目录不存在: {}（请检查 pb_files_dir）'.format(cfg.pb_files_dir))
        if not os.path.isfile(cfg.db_path):
            raise RuntimeError('找不到 PrimeBackup 数据库: {}（请检查 pb_files_dir）'.format(cfg.db_path))
        os.makedirs(cfg.temp_dir, exist_ok=True)

        # 2) 扫描与空间检查
        payload = packer.scan_payload(cfg.pb_files_dir, cfg.db_path)
        if not payload.files and payload.total_bytes <= 0:
            raise RuntimeError('仓库目录里没有可打包的内容: {}'.format(cfg.pb_files_dir))
        self.logger.info('待打包 {} 个文件，约 {}（数据库 {}）'.format(
            payload.file_count, _human(payload.total_bytes), cfg.db_name))
        packer.check_free_space(cfg.temp_dir, payload.total_bytes, cfg.min_free_space_ratio)

        tar_path = os.path.join(cfg.temp_dir, name)
        snapshot_path = os.path.join(cfg.temp_dir, '._snapshot_{}.db'.format(name))
        started_ts = time.time()
        uploaded_size = 0
        try:
            # 3) 数据库快照 + 无压缩 tar
            self.phase = PHASE_PACKING
            self.logger.info('导出 SQLite 一致性快照 …')
            packer.snapshot_database(cfg.db_path, snapshot_path)
            self._check_cancel()

            extra = []
            if cfg.include_pb_config and cfg.pb_config_path:
                if os.path.isfile(cfg.pb_config_path):
                    extra.append((cfg.pb_config_path, 'pb_config/config.json'))
                else:
                    self.logger.warning('include_pb_config=true 但找不到配置文件: {}'.format(cfg.pb_config_path))

            self.logger.info('生成无压缩 tar 归档 …')
            uploaded_size = packer.build_tar(
                tar_path, payload, snapshot_path, extra_files=extra,
                progress=self._on_pack_progress,
            )
            self.last_size = uploaded_size
            self.logger.info('归档完成: {}（{}）'.format(name, _human(uploaded_size)))
            self._check_cancel()

            # 4) 上传
            self.phase = PHASE_UPLOADING
            client = self._client()
            client.ensure_dir(cfg.remote_dir)
            self._upload_with_retry(client, tar_path, remote_path, uploaded_size)

            # 5) 校验
            self.phase = PHASE_VERIFYING
            self.logger.info('校验远端文件 …')
            if not client.verify(remote_path, uploaded_size):
                raise RuntimeError('远端校验失败：{} 不存在或大小不一致（本地 {} 字节）'.format(
                    remote_path, uploaded_size))

            # 6) 滚动清理
            self.phase = PHASE_PRUNING
            self._prune(client, remote_path)

            self.last_ok = True
            self.logger.info('备份成功: {}（{}，耗时 {:.1f}s）'.format(
                name, _human(uploaded_size), time.time() - started_ts))
        finally:
            removed = packer.cleanup_artifacts(cfg.temp_dir, tar_path, snapshot_path, started_ts)
            if removed:
                self.logger.debug('已清理本地临时文件: {}'.format(', '.join(os.path.basename(p) for p in removed)))

    # ------------------------------------------------------------------ 子步骤
    def _client(self) -> OpenListClient:
        cfg = self.config
        if not cfg.openlist_token and not (cfg.openlist_username and cfg.openlist_password):
            raise RuntimeError('OpenList 凭据缺失：请填写 openlist_token 或 openlist_username/password')
        return OpenListClient(
            base_url=cfg.openlist_url,
            token=cfg.openlist_token,
            username=cfg.openlist_username,
            password=cfg.openlist_password,
            timeout=float(cfg.timeout_sec),
            as_task=cfg.as_task,
            logger=self.logger,
        )

    def _upload_with_retry(self, client: OpenListClient, tar_path: str, remote_path: str, size: int) -> None:
        cfg = self.config
        attempts = cfg.retry_times + 1
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            self._check_cancel()
            try:
                self.logger.info('上传中（第 {}/{} 次尝试，{}）…'.format(attempt, attempts, _human(size)))
                progress_state = {'last': 0.0, 'last_bytes': 0}

                def on_progress(sent: int, total: int) -> None:
                    now = time.time()
                    if now - progress_state['last'] >= 10.0 or sent == total:
                        progress_state['last'] = now
                        percent = (sent * 100.0 / total) if total else 0.0
                        self.logger.info('上传进度 {:.1f}%（{} / {}）'.format(
                            percent, _human(sent), _human(total)))
                        progress_state['last_bytes'] = sent

                client.upload(tar_path, remote_path, progress=on_progress,
                              cancel_check=self._cancel.is_set)
                return
            except OpenListError as e:
                last_error = e
                self.logger.warning('上传失败（第 {} 次）: {}'.format(attempt, e))
                # 上传已被客户端取消，这里再把任务记录删掉：OpenList 的「待处理」列表包含
                # Errored / WaitingRetry 状态，不清理的话每重试一次就多一条僵尸任务。
                task_id = getattr(client, 'last_task_id', None)
                if task_id:
                    client.delete_task(task_id)
                    client.last_task_id = None
                if attempt < attempts:
                    time.sleep(min(30.0, 5.0 * attempt))
        raise RuntimeError('上传重试 {} 次后仍失败: {}'.format(attempts, last_error))

    def _prune(self, client: OpenListClient, just_uploaded: str) -> None:
        cfg = self.config
        if cfg.max_keep <= 0:
            self.logger.info('max_keep={} <= 0，跳过自动清理（永久保留所有备份）'.format(cfg.max_keep))
            return

        try:
            items = client.list_dir(cfg.remote_dir, refresh=True)
        except OpenListError as e:
            self.logger.warning('读取远端目录失败，跳过本次清理: {}'.format(e))
            return

        entries = [prune.RemoteEntry.from_openlist(item) for item in items]
        decision = prune.decide_prune(
            entries,
            filename_prefix=cfg.filename_prefix,
            max_keep=cfg.max_keep,
            min_valid_size_bytes=cfg.min_valid_size_bytes,
        )
        for entry in decision.suspicious:
            self.logger.warning('发现异常备份包（体积 {} 字节，未计入保留配额）: {}'.format(
                entry.size, entry.name))

        if not decision.to_delete:
            self.logger.info('远端现有 {} 份有效备份，未超过 max_keep={}，无需清理'.format(
                len(decision.valid), cfg.max_keep))
            return

        for entry in decision.to_delete:
            if entry.name == os.path.basename(just_uploaded):
                continue  # 绝不删除刚上传的那一份
            try:
                client.delete_file(cfg.remote_dir.rstrip('/') + '/' + entry.name)
                self.last_removed.append(entry.name)
                self.logger.info('已删除旧备份: {}'.format(entry.name))
            except OpenListError as e:
                self.logger.warning('删除旧备份失败 {}: {}'.format(entry.name, e))

    # ------------------------------------------------------------------ 杂项
    def _on_pack_progress(self, done: int, total: int, arcname: str) -> None:
        if total <= 0:
            return
        percent = done * 100.0 / total
        self.logger.info('打包进度 {:.1f}%（{}/{}）{}'.format(
            percent, _human(done), _human(total), ('，当前: ' + arcname) if arcname else ''))

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise RuntimeError('任务已取消')

    def _now(self) -> datetime:
        return datetime.now(self.config.tz)


def _human(num: int) -> str:
    """兼容旧调用点，统一走 prune.human_size。"""
    return prune.human_size(num)
