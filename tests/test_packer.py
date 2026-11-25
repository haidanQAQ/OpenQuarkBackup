"""单元测试：SQLite 热快照 + 无压缩 tar 打包 + exFAT/临时文件过滤。"""

import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_quark_backup import packer  # noqa: E402


def write(path, data=b'x' * 16):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


class PackerTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='oqb_packer_')
        self.pb = os.path.join(self.root, 'pb_files')
        self.db = os.path.join(self.pb, 'prime_backup.db')
        os.makedirs(self.pb, exist_ok=True)

        # 真实的 SQLite 库，带一张表和数据
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE backup (id INTEGER PRIMARY KEY, comment TEXT)')
        conn.executemany('INSERT INTO backup (id, comment) VALUES (?, ?)',
                         [(1, 'first'), (2, 'second'), (3, 'third')])
        conn.commit()
        conn.close()

        # 正常数据
        write(os.path.join(self.pb, 'blobs', 'aa', '001.blob'), b'a' * 100)
        write(os.path.join(self.pb, 'blobs', 'bb', '002.blob'), b'b' * 200)
        write(os.path.join(self.pb, 'packs', 'pack_1.dat'), b'p' * 300)

        # 必须被过滤的内容
        write(os.path.join(self.pb, 'temp', 'leftover.tar'), b't' * 10)
        write(os.path.join(self.pb, '._prime_backup.db'), b'A' * 4096)
        write(os.path.join(self.pb, '.DS_Store'), b'D' * 32)
        write(os.path.join(self.pb, 'Thumbs.db'), b'T' * 32)
        write(os.path.join(self.pb, 'blobs', 'aa', '._001.blob'), b'A' * 4096)
        write(os.path.join(self.pb, 'blobs', 'aa', 'write.lock'), b'L' * 8)
        write(os.path.join(self.pb, 'prime_backup.db-wal'), b'W' * 64)
        write(os.path.join(self.pb, 'prime_backup.db-shm'), b'S' * 64)
        write(os.path.join(self.pb, 'System Volume Information', 'IndexerVolumeGuid'), b'V' * 8)

        self.temp_dir = os.path.join(self.root, 'tmp')
        os.makedirs(self.temp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- 扫描
    def test_scan_filters_hidden_and_temp(self):
        payload = packer.scan_payload(self.pb, self.db)
        arcs = {arc for _, arc, _ in payload.files}
        self.assertEqual(arcs, {
            'blobs/aa/001.blob',
            'blobs/bb/002.blob',
            'packs/pack_1.dat',
        })
        # 原数据库文件不直接进归档（归档里放的是快照）
        self.assertNotIn('prime_backup.db', arcs)
        for unwanted in ('temp/leftover.tar', '._prime_backup.db', '.DS_Store', 'Thumbs.db',
                         'blobs/aa/._001.blob', 'blobs/aa/write.lock',
                         'prime_backup.db-wal', 'prime_backup.db-shm'):
            self.assertNotIn(unwanted, arcs)
        # 体积 = 数据文件 + 数据库文件
        expected = 100 + 200 + 300 + os.path.getsize(self.db)
        self.assertEqual(payload.total_bytes, expected)

    def test_skip_rules(self):
        self.assertTrue(packer.should_skip_file('._x'))
        self.assertTrue(packer.should_skip_file('.DS_Store'))
        self.assertTrue(packer.should_skip_file('a.lock'))
        self.assertTrue(packer.should_skip_file('prime_backup.db-wal'))
        self.assertFalse(packer.should_skip_file('001.blob'))
        self.assertTrue(packer.should_skip_dir('temp'))
        self.assertTrue(packer.should_skip_dir('__MACOSX'))
        self.assertFalse(packer.should_skip_dir('blobs'))

    # ---------------------------------------------------------------- 快照
    def test_snapshot_is_consistent_copy(self):
        snapshot = os.path.join(self.temp_dir, 'snap.db')
        packer.snapshot_database(self.db, snapshot)
        self.assertTrue(os.path.isfile(snapshot))
        conn = sqlite3.connect(snapshot)
        try:
            rows = conn.execute('SELECT COUNT(*) FROM backup').fetchone()[0]
            self.assertEqual(rows, 3)
            # 快照必须能独立通过完整性检查
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            conn.close()

    def test_snapshot_survives_concurrent_writer(self):
        """模拟服务端边写库边快照：快照里必须包含快照开始前已提交的数据。"""
        snapshot = os.path.join(self.temp_dir, 'snap2.db')
        writer = sqlite3.connect(self.db)
        try:
            writer.execute("INSERT INTO backup (id, comment) VALUES (4, 'fourth')")
            writer.commit()
            packer.snapshot_database(self.db, snapshot)
            writer.execute("INSERT INTO backup (id, comment) VALUES (5, 'fifth')")
            writer.commit()
        finally:
            writer.close()

        conn = sqlite3.connect(snapshot)
        try:
            rows = conn.execute('SELECT id FROM backup ORDER BY id').fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4])

    # ---------------------------------------------------------------- 打包
    def test_build_tar_store_mode(self):
        payload = packer.scan_payload(self.pb, self.db)
        snapshot = os.path.join(self.temp_dir, 'snap.db')
        packer.snapshot_database(self.db, snapshot)
        config = os.path.join(self.root, 'pb_config.json')
        write(config, b'{"storage_root": "./pb_files"}')
        tar_path = os.path.join(self.temp_dir, 'pb_backup_20260101_020000.tar')

        size = packer.build_tar(tar_path, payload, snapshot,
                                extra_files=[(config, 'pb_config/config.json')])
        self.assertTrue(os.path.isfile(tar_path))
        self.assertEqual(size, os.path.getsize(tar_path))

        with tarfile.open(tar_path, 'r:') as tar:
            names = set(tar.getnames())
            # 必须是未压缩的纯 tar（store）
            self.assertFalse(tar_path.endswith(('.gz', '.bz2', '.xz', '.zst')))
            self.assertIn('prime_backup.db', names)
            self.assertIn('blobs/aa/001.blob', names)
            self.assertIn('blobs/bb/002.blob', names)
            self.assertIn('packs/pack_1.dat', names)
            self.assertIn('pb_config/config.json', names)
            self.assertNotIn('temp/leftover.tar', names)
            self.assertNotIn('._prime_backup.db', names)
            self.assertNotIn('.DS_Store', names)
            self.assertNotIn('prime_backup.db-wal', names)
            self.assertFalse(any(n.startswith('temp/') for n in names))
            self.assertFalse(any('/.' in n for n in names))

            # 从归档里抽出的数据库必须可用
            extracted = tar.extractfile('prime_backup.db')
            self.assertIsNotNone(extracted)
            data = extracted.read()
            self.assertGreater(len(data), 0)
        extracted_path = os.path.join(self.temp_dir, 'extracted.db')
        with open(extracted_path, 'wb') as f:
            f.write(data)
        conn = sqlite3.connect(extracted_path)
        try:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backup').fetchone()[0], 3)
        finally:
            conn.close()

    # ---------------------------------------------------------------- 清理
    def test_cleanup_removes_artifacts_and_appledouble(self):
        tar_path = os.path.join(self.temp_dir, 'pb_backup_20260101_020000.tar')
        snapshot = os.path.join(self.temp_dir, 'snap.db')
        write(tar_path, b't' * 10)
        write(snapshot, b's' * 10)
        write(os.path.join(self.temp_dir, '._pb_backup_20260101_020000.tar'), b'A' * 4096)
        keep = os.path.join(self.temp_dir, 'keep.txt')
        write(keep, b'k')

        removed = packer.cleanup_artifacts(self.temp_dir, tar_path, snapshot, started_at=0)
        self.assertFalse(os.path.exists(tar_path))
        self.assertFalse(os.path.exists(snapshot))
        self.assertFalse(os.path.exists(os.path.join(self.temp_dir, '._pb_backup_20260101_020000.tar')))
        self.assertTrue(os.path.exists(keep))  # 不碰无关文件
        self.assertGreaterEqual(len(removed), 3)

    def test_free_space_check(self):
        # 正常需求不报错
        packer.check_free_space(self.temp_dir, 1024, 1.15)
        # 需求远超磁盘剩余空间时必须报错
        with self.assertRaises(packer.PackError):
            packer.check_free_space(self.temp_dir, 1 << 60, 1.15)

    # ---------------------------------------------------------------- 取消
    def test_build_tar_cancel_aborts(self):
        """取消信号必须在写归档的过程中生效，而不是等整个 tar 写完。"""
        payload = packer.scan_payload(self.pb, self.db)
        snapshot = os.path.join(self.temp_dir, 'snap.db')
        packer.snapshot_database(self.db, snapshot)
        tar_path = os.path.join(self.temp_dir, 'pb_backup_20260101_020000.tar')

        calls = {'n': 0}

        def cancel_check():
            calls['n'] += 1
            return calls['n'] >= 3   # 前两次放行，之后取消

        with self.assertRaises(packer.PackError) as ctx:
            packer.build_tar(tar_path, payload, snapshot, cancel_check=cancel_check)
        self.assertIn('任务已取消', str(ctx.exception))
        # 确实是在循环里被拦下的：至少做过一次检查，而且已经写了几个条目
        self.assertGreaterEqual(calls['n'], 3)

    def test_build_tar_without_cancel_completes(self):
        """不传 cancel_check 时行为不变（回归保护）。"""
        payload = packer.scan_payload(self.pb, self.db)
        snapshot = os.path.join(self.temp_dir, 'snap.db')
        packer.snapshot_database(self.db, snapshot)
        tar_path = os.path.join(self.temp_dir, 'pb_backup_20260101_020000.tar')
        size = packer.build_tar(tar_path, payload, snapshot)
        self.assertGreater(size, 0)
        self.assertTrue(os.path.isfile(tar_path))


if __name__ == '__main__':
    unittest.main(verbosity=2)
