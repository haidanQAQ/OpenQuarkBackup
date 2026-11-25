"""
PrimeBackup 仓库的"安全打包"：SQLite 在线快照 + 无压缩 tar + 严格过滤。

要点：
1. 数据库必须用 ``sqlite3.Connection.backup``（在线备份 API）导出一致性快照，
   绝不能直接复制运行中的 ``.db`` / ``.db-wal`` / ``.db-shm``；
2. 归档用 tar 的 store 模式（不压缩）—— PB 的 blobs/packs 本身已是压缩数据，
   再 gzip/zstd 一遍只会白白吃满凌晨的 CPU；
3. 遍历时过滤 exFAT / macOS / Windows 的隐藏与系统元数据文件，以及 PB 的临时文件。

注意：本模块不导入 mcdreforged，可脱离 MCDR 做单元测试。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

#: 需要整目录跳过的名字（PB 运行期临时目录 + 各平台的系统目录）。
SKIP_DIR_NAMES = {
    'temp',                      # PrimeBackup 自己的临时目录
    'System Volume Information',  # Windows 在外接盘上创建的系统目录
    '.Trashes',                  # macOS 废纸篓
    '.Spotlight-V100',           # macOS 索引
    '.fseventsd',                # macOS 文件事件
    '.TemporaryItems',
    '__MACOSX',
}

#: 需要跳过的具体文件名。
SKIP_FILE_NAMES = {
    'Thumbs.db',
    'desktop.ini',
    '.DS_Store',
}

#: 需要跳过的文件名后缀（PB/SQLite 的锁与预写日志）。
SKIP_FILE_SUFFIXES = (
    '.lock',
    '.db-wal',
    '.db-shm',
    '.db-journal',
    '.tmp',
    '.part',
)


class PackError(Exception):
    """打包过程中的可读错误（空间不足、数据库快照失败等）。"""


@dataclass
class Payload:
    """一次打包的清单。"""

    root: str
    db_path: str
    db_name: str
    files: List[Tuple[str, str, int]] = field(default_factory=list)  # (绝对路径, 归档内路径, 字节数)
    dirs: List[Tuple[str, str]] = field(default_factory=list)        # (绝对路径, 归档内路径)
    total_bytes: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)


def is_hidden_name(name: str) -> bool:
    """以 '.' 开头即视为隐藏项（含 exFAT 的 ``._*`` AppleDouble 文件）。"""
    return name.startswith('.')


def should_skip_dir(name: str) -> bool:
    return is_hidden_name(name) or name in SKIP_DIR_NAMES


def should_skip_file(name: str) -> bool:
    if is_hidden_name(name) or name in SKIP_FILE_NAMES:
        return True
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in SKIP_FILE_SUFFIXES)


def _relative_posix(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    return '' if rel == '.' else rel.replace(os.sep, '/')


def scan_payload(root: str, db_path: str) -> Payload:
    """
    扫描 ``root`` 下需要打包的内容。

    ``db_path`` 只用于：① 计算体积时计入；② 遍历时跳过原文件（归档里放的是快照）。
    """
    root = os.path.abspath(root)
    db_path = os.path.abspath(db_path)
    db_name = os.path.basename(db_path)
    payload = Payload(root=root, db_path=db_path, db_name=db_name)

    try:
        db_size = os.path.getsize(db_path)
    except OSError as e:
        raise PackError('找不到 PrimeBackup 数据库 {}: {}'.format(db_path, e)) from None
    payload.total_bytes += db_size

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(d for d in dirnames if not should_skip_dir(d))
        rel_dir = _relative_posix(dirpath, root)

        for dirname in dirnames:
            abs_dir = os.path.join(dirpath, dirname)
            arc = '{}/{}'.format(rel_dir, dirname) if rel_dir else dirname
            payload.dirs.append((abs_dir, arc))

        for filename in sorted(filenames):
            abs_file = os.path.join(dirpath, filename)
            if os.path.abspath(abs_file) == db_path:
                continue  # 原库文件不进归档，只放快照
            if should_skip_file(filename):
                continue
            if os.path.islink(abs_file):
                try:
                    size = 0
                except OSError:
                    size = 0
            else:
                try:
                    size = os.path.getsize(abs_file)
                except OSError:
                    continue
            arc = '{}/{}'.format(rel_dir, filename) if rel_dir else filename
            payload.files.append((abs_file, arc, size))
            payload.total_bytes += size

    return payload


def free_space_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def check_free_space(temp_dir: str, needed_bytes: int, ratio: float) -> None:
    """打包前检查暂存盘剩余空间；不足则抛 :class:`PackError`。"""
    os.makedirs(temp_dir, exist_ok=True)
    required = int(needed_bytes * max(1.0, ratio))
    free = free_space_bytes(temp_dir)
    if free < required:
        raise PackError(
            '暂存目录空间不足：需要约 {}，剩余 {}（安全系数 {}）。'
            '请清理 {} 或把 temp_dir 改到更大的磁盘。'.format(
                _human_size(required), _human_size(free), ratio, temp_dir
            )
        )


def snapshot_database(db_path: str, snapshot_path: str, timeout: float = 60.0) -> None:
    """
    用 SQLite 在线备份 API 导出一致性快照。

    与直接复制文件不同，backup API 会通过 SQLite 自己的一致性读取视图取数据，
    因此即使服务端/PB 正在写库，快照也是完整可用的（WAL 中的已提交事务同样包含在内）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(snapshot_path)), exist_ok=True)
    if os.path.exists(snapshot_path):
        os.remove(snapshot_path)

    try:
        source = sqlite3.connect(db_path, timeout=timeout)
    except sqlite3.Error as e:
        raise PackError('打开数据库失败 {}: {}'.format(db_path, e)) from None
    try:
        target = sqlite3.connect(snapshot_path)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as e:
        raise PackError('SQLite 在线快照失败 {}: {}'.format(db_path, e)) from None
    finally:
        source.close()

    if not os.path.isfile(snapshot_path) or os.path.getsize(snapshot_path) == 0:
        raise PackError('SQLite 快照为空: {}'.format(snapshot_path))


def build_tar(
    tar_path: str,
    payload: Payload,
    snapshot_path: str,
    extra_files: Sequence[Tuple[str, str]] = (),
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """
    生成无压缩 tar 包，返回写入的总字节数。

    * 快照以原库名（如 ``prime_backup.db``）放在归档根目录，恢复时可直接替换；
    * ``extra_files`` 是 (绝对路径, 归档内路径) 序列，用于附带 PB 配置文件；
    * ``progress(done_bytes, total_bytes, arcname)`` 会定期被调用，便于打日志。
    """
    os.makedirs(os.path.dirname(os.path.abspath(tar_path)), exist_ok=True)
    if os.path.exists(tar_path):
        os.remove(tar_path)

    total = payload.total_bytes
    done = 0
    last_report = 0.0
    started = time.time()

    def report(arcname: str, force: bool = False) -> None:
        nonlocal last_report
        now = time.time()
        if progress is not None and (force or now - last_report >= 5.0):
            last_report = now
            progress(done, total, arcname)

    def add_file(abs_path: str, arcname: str, size: int) -> None:
        nonlocal done
        try:
            tar.add(abs_path, arcname=arcname, recursive=False)
        except (OSError, tarfile.TarError) as e:
            raise PackError('写入归档失败 {} -> {}: {}'.format(abs_path, arcname, e)) from None
        done += size
        report(arcname)

    try:
        with tarfile.open(tar_path, mode='w', format=tarfile.PAX_FORMAT) as tar:
            # 1) 数据库快照（放在根目录，名字与原来一致）
            db_size = os.path.getsize(snapshot_path)
            tar.add(snapshot_path, arcname=payload.db_name, recursive=False)
            done += db_size
            report(payload.db_name, force=True)

            # 2) 额外文件（PB 配置文件等）
            for abs_path, arcname in extra_files:
                if not os.path.isfile(abs_path):
                    continue
                add_file(abs_path, arcname, os.path.getsize(abs_path))

            # 3) 数据目录
            for abs_dir, arc in payload.dirs:
                try:
                    tar.add(abs_dir, arcname=arc, recursive=False)
                except (OSError, tarfile.TarError):
                    # 目录可能在扫描与打包之间被删除，跳过即可
                    continue
            for abs_file, arc, size in payload.files:
                if not os.path.isfile(abs_file):
                    continue
                add_file(abs_file, arc, size)
    except tarfile.TarError as e:
        raise PackError('创建 tar 归档失败: {}'.format(e)) from None

    elapsed = time.time() - started
    if progress is not None:
        progress(done, total, '')
    if not os.path.isfile(tar_path) or os.path.getsize(tar_path) == 0:
        raise PackError('归档文件为空: {}'.format(tar_path))
    _ = elapsed
    return os.path.getsize(tar_path)


def cleanup_artifacts(temp_dir: str, tar_path: Optional[str], snapshot_path: Optional[str],
                      started_at: Optional[float] = None) -> List[str]:
    """
    清理本次任务在暂存目录里的残留：tar、快照，以及 macOS 可能连带生成的 ``._*``。

    返回被删除的路径列表（用于日志）。``started_at`` 用于只清理本次任务之后生成的文件，
    避免误删运维放在同一目录里的其他东西。
    """
    removed: List[str] = []
    for path in (tar_path, snapshot_path):
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed.append(path)
        except OSError:
            pass

    if not os.path.isdir(temp_dir):
        return removed

    threshold = (started_at - 5.0) if started_at else 0.0
    try:
        entries = os.listdir(temp_dir)
    except OSError:
        return removed
    for name in entries:
        if not name.startswith('._'):
            continue
        path = os.path.join(temp_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= threshold:
                os.remove(path)
                removed.append(path)
        except OSError:
            pass
    return removed


def _human_size(num: int) -> str:
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return '{:.1f}{}'.format(value, unit) if unit != 'B' else '{:.0f}B'.format(value)
        value /= 1024
    return '{:.1f}TB'.format(value)
