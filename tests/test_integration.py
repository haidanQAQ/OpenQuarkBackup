"""
端到端测试：用一个假的 OpenList 服务端跑完「打包 → 流式上传 → 校验 → 滚动清理」。

假服务端实现了插件用到的全部接口（login / mkdir / fs put / fs get / fs list /
fs remove / task info），并按真实行为区分「任务入队」与「任务完成」两个阶段，
用来确认插件不会把"已入队"误判成"已上传"。
"""

import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_quark_backup.config import Config, resolve_config  # noqa: E402
from open_quark_backup.job import BackupJob  # noqa: E402
from open_quark_backup.openlist_api import OpenListClient  # noqa: E402

TOKEN = 'test-token'
REMOTE_DIR = '/夸克/Minecraft_Backup'


class FakeOpenList:
    """极简 OpenList：内存里存目录与文件。"""

    def __init__(self):
        self.dirs = {'/'}
        self.files = {}          # path -> bytes
        self.tasks = {}          # tid -> {'state': int, 'polls': int, 'path': str}
        self.upload_headers = []  # 每次 PUT 的原始 header，便于校验编码
        self.removed = []
        self.canceled = []
        self.deleted = []
        self.fail_tasks = set()
        self.fail_next_task = False
        self.login_count = 0
        self._lock = threading.Lock()
        self._next_task = 1

        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):  # 静默
                pass

            def _reply(self, code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get('Content-Length') or 0)
                return self.rfile.read(length) if length else b''

            def _authorized(self):
                return self.headers.get('Authorization') == TOKEN

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == '/api/auth/login':
                    data = json.loads(self._body() or b'{}')
                    if data.get('username') == 'admin' and data.get('password') == 'secret':
                        fake.login_count += 1
                        return self._reply(200, {'code': 200, 'data': {'token': TOKEN}})
                    return self._reply(200, {'code': 401, 'message': 'wrong password'})

                if not self._authorized():
                    return self._reply(401, {'code': 401, 'message': 'token is invalidated'})

                if path == '/api/fs/mkdir':
                    target = json.loads(self._body())['path']
                    parent = os.path.dirname(target.rstrip('/')) or '/'
                    with fake._lock:
                        if parent not in fake.dirs:
                            return self._reply(200, {'code': 500, 'message': 'parent not exist'})
                        fake.dirs.add(target)
                    return self._reply(200, {'code': 200})

                if path == '/api/fs/list':
                    target = json.loads(self._body())['path']
                    with fake._lock:
                        if target not in fake.dirs:
                            return self._reply(200, {'code': 500, 'message': 'object not found'})
                        prefix = target.rstrip('/') + '/'
                        content = []
                        for p, blob in sorted(fake.files.items()):
                            if p.startswith(prefix) and '/' not in p[len(prefix):]:
                                content.append({'name': os.path.basename(p), 'size': len(blob),
                                                'is_dir': False})
                    return self._reply(200, {'code': 200, 'data': {'content': content}})

                if path == '/api/fs/get':
                    target = json.loads(self._body())['path']
                    with fake._lock:
                        if target in fake.files:
                            return self._reply(200, {'code': 200, 'data': {
                                'name': os.path.basename(target),
                                'size': len(fake.files[target]),
                                'is_dir': False,
                            }})
                    return self._reply(500, {'code': 500, 'message': 'object not found'})

                if path == '/api/fs/remove':
                    data = json.loads(self._body())
                    directory = data['dir'].rstrip('/')
                    with fake._lock:
                        for name in data['names']:
                            full = directory + '/' + name
                            if fake.files.pop(full, None) is not None:
                                fake.removed.append(name)
                    return self._reply(200, {'code': 200})

                if path == '/api/task/upload/cancel':
                    tid = urllib.parse.parse_qs(parsed.query).get('tid', [''])[0]
                    with fake._lock:
                        fake.canceled.append(tid)
                        task = fake.tasks.get(tid)
                        if task is not None:
                            task['state'] = 4
                    return self._reply(200, {'code': 200})

                if path == '/api/task/upload/delete':
                    tid = urllib.parse.parse_qs(parsed.query).get('tid', [''])[0]
                    with fake._lock:
                        fake.deleted.append(tid)
                        fake.tasks.pop(tid, None)
                    return self._reply(200, {'code': 200})

                if path == '/api/task/info':
                    tid = urllib.parse.parse_qs(parsed.query).get('tid', [''])[0]
                    with fake._lock:
                        task = fake.tasks.get(tid)
                        if task is None:
                            return self._reply(200, {'code': 500, 'message': 'task not found'})
                        task['polls'] += 1
                        if tid in fake.fail_tasks:
                            return self._reply(200, {'code': 200, 'data': {
                                'id': tid, 'state': 5, 'status': 'errored',
                                'name': os.path.basename(task['path']), 'error': 'quota exceeded',
                            }})
                        # 第一次查询仍在跑，之后报告完成（模拟真实异步任务）
                        state = 1 if task['polls'] <= 1 else 2
                        status = 'running' if state == 1 else 'succeeded'
                        return self._reply(200, {'code': 200, 'data': {
                            'id': tid, 'state': state, 'status': status,
                            'name': os.path.basename(task['path']),
                            'progress': 50.0 if state == 1 else 100.0,
                        }})

                return self._reply(404, {'code': 404, 'message': 'no such endpoint'})

            def do_PUT(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != '/api/fs/put':
                    return self._reply(404, {'code': 404, 'message': 'no such endpoint'})
                if not self._authorized():
                    self._body()
                    return self._reply(401, {'code': 401, 'message': 'token is invalidated'})

                raw_target = self.headers.get('File-Path') or ''
                target = urllib.parse.unquote(raw_target)
                expected = int(self.headers.get('Content-Length') or 0)
                chunks = []
                remaining = expected
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                blob = b''.join(chunks)
                assert len(blob) == expected, 'content-length 与实际字节数不一致'

                with fake._lock:
                    fake.upload_headers.append({
                        'file_path_raw': raw_target,
                        'file_path': target,
                        'content_type': self.headers.get('Content-Type'),
                        'as_task': self.headers.get('As-Task'),
                        'x_file_size': self.headers.get('X-File-Size'),
                        'size': len(blob),
                    })
                    fake.files[target] = blob
                    if (self.headers.get('As-Task') or '').lower() == 'true':
                        tid = str(fake._next_task)
                        fake._next_task += 1
                        fake.tasks[tid] = {'state': 0, 'polls': 0, 'path': target}
                        if fake.fail_next_task:
                            fake.fail_next_task = False
                            fake.fail_tasks.add(tid)
                        return self._reply(200, {'code': 200, 'data': {'task': {'id': tid}}})

                return self._reply(200, {'code': 200, 'data': {}})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:{}'.format(self.port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def put_existing(self, name, size):
        with self._lock:
            self.files[REMOTE_DIR.rstrip('/') + '/' + name] = b'x' * size


class FakeServer:
    """冒充 MCDR 的 PluginServerInterface（只用到这两个方法）。"""

    def __init__(self, root):
        self.root = root
        self._logger = logging.getLogger('oqb.test')
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    @property
    def logger(self):
        return self._logger

    def get_data_folder(self):
        path = os.path.join(self.root, 'config', 'open_quark_backup')
        os.makedirs(path, exist_ok=True)
        return path

    def get_mcdr_config(self):
        return {'working_directory': os.path.join(self.root, 'server')}


def make_pb_files(root):
    pb = os.path.join(root, 'pb_files')
    os.makedirs(os.path.join(pb, 'blobs', 'ab'), exist_ok=True)
    os.makedirs(os.path.join(pb, 'packs'), exist_ok=True)
    os.makedirs(os.path.join(pb, 'temp'), exist_ok=True)
    db = os.path.join(pb, 'prime_backup.db')
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE backup (id INTEGER PRIMARY KEY)')
    conn.executemany('INSERT INTO backup (id) VALUES (?)', [(1,), (2,)])
    conn.commit()
    conn.close()
    with open(os.path.join(pb, 'blobs', 'ab', 'cdef.blob'), 'wb') as f:
        f.write(os.urandom(4096))
    with open(os.path.join(pb, 'packs', 'pack_1.dat'), 'wb') as f:
        f.write(os.urandom(2048))
    with open(os.path.join(pb, 'temp', 'junk.tar'), 'wb') as f:
        f.write(b'j' * 32)
    with open(os.path.join(pb, '._prime_backup.db'), 'wb') as f:
        f.write(b'A' * 4096)
    with open(os.path.join(pb, 'prime_backup.db-wal'), 'wb') as f:
        f.write(b'W' * 64)
    return pb, db


class JobEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='oqb_e2e_')
        self.fake = FakeOpenList()
        self.server = FakeServer(self.root)
        self.pb_dir, self.db_path = make_pb_files(self.root)

    def tearDown(self):
        self.fake.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def build_config(self, **overrides):
        cfg = Config()
        cfg.pb_files_dir = self.pb_dir
        cfg.temp_dir = os.path.join(self.root, 'tmp')
        cfg.openlist_url = self.fake.url
        cfg.openlist_username = 'admin'
        cfg.openlist_password = 'secret'
        cfg.remote_dir = REMOTE_DIR
        cfg.cron = '0 2 * * 1'
        cfg.timezone = 'Asia/Shanghai'
        cfg.timeout_sec = 60
        cfg.retry_times = 1
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def run_job(self, cfg):
        resolved = resolve_config(cfg, self.server)
        job = BackupJob(resolved, self.server.logger)
        self.assertTrue(job.start('test'), '任务未能启动')
        deadline = time.time() + 60
        while job.is_running() and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(job.is_running(), '任务超时未结束')
        return job

    def test_full_flow_upload_verify_prune(self):
        # 预置 3 个历史备份，max_keep=2 → 上传后应删掉最早的 2 份
        for name in ('pb_backup_20260101_020000.tar',
                     'pb_backup_20260108_020000.tar',
                     'pb_backup_20260115_020000.tar'):
            self.fake.put_existing(name, 20 * 1024)

        cfg = self.build_config(max_keep=2)
        job = self.run_job(cfg)

        summary = job.summary()
        self.assertTrue(summary['last_ok'], '任务应成功，实际错误: {}'.format(summary['last_error']))
        self.assertEqual(summary['phase'], 'idle')
        self.assertGreater(summary['last_size'], 0)

        remote = {os.path.basename(p): blob for p, blob in self.fake.files.items()}
        self.assertIn(summary['last_name'], remote)
        self.assertEqual(len(remote), 2, '应只保留最新 2 份: {}'.format(sorted(remote)))
        self.assertNotIn('pb_backup_20260101_020000.tar', remote)
        self.assertNotIn('pb_backup_20260108_020000.tar', remote)
        self.assertIn('pb_backup_20260115_020000.tar', remote)
        self.assertEqual(sorted(summary['removed']),
                         ['pb_backup_20260101_020000.tar', 'pb_backup_20260108_020000.tar'])

        # 上传内容的头信息：路径经过 URL 编码，体积与本地一致
        header = self.fake.upload_headers[-1]
        self.assertEqual(header['file_path'], REMOTE_DIR.rstrip('/') + '/' + summary['last_name'])
        self.assertNotEqual(header['file_path_raw'], header['file_path'])  # 中文确实被编码了
        self.assertEqual(header['size'], summary['last_size'])
        self.assertEqual(header['content_type'], 'application/octet-stream')
        self.assertEqual(header['as_task'], 'true')

        # 远端 tar 可以正常解析，且里面是 SQLite 快照（不是直接复制的运行中库）
        tar_bytes = remote[summary['last_name']]
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as tar:
            names = tar.getnames()
            self.assertIn('prime_backup.db', names)
            self.assertIn('blobs/ab/cdef.blob', names)
            self.assertNotIn('temp/junk.tar', names)
            self.assertFalse(any(n.startswith('._') for n in names))
            self.assertFalse(any(n.endswith(('-wal', '-shm')) for n in names))
            db_bytes = tar.extractfile('prime_backup.db').read()

        snapshot_path = os.path.join(self.root, 'from_tar.db')
        with open(snapshot_path, 'wb') as f:
            f.write(db_bytes)
        conn = sqlite3.connect(snapshot_path)
        try:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backup').fetchone()[0], 2)
        finally:
            conn.close()

        # 本地临时文件必须被清干净
        leftovers = os.listdir(cfg.temp_dir) if os.path.isdir(cfg.temp_dir) else []
        self.assertEqual([n for n in leftovers if n.endswith(('.tar', '.db'))], [])

    def test_login_is_used_when_token_missing(self):
        cfg = self.build_config(max_keep=0)
        job = self.run_job(cfg)
        self.assertTrue(job.summary()['last_ok'], job.summary()['last_error'])
        self.assertEqual(self.fake.login_count, 1)
        self.assertEqual(len(self.fake.files), 1)  # max_keep=0：不清理

    def test_task_polling_waits_for_completion(self):
        client = OpenListClient(self.fake.url, username='admin', password='secret',
                                timeout=30.0, as_task=True)
        local = os.path.join(self.root, 'payload.bin')
        with open(local, 'wb') as f:
            f.write(os.urandom(300 * 1024))
        data = client.upload(local, '/夸克/Minecraft_Backup/payload.bin')
        self.assertIn('task', data)
        tid = data['task']['id']
        self.assertGreaterEqual(self.fake.tasks[tid]['polls'], 2,
                                '必须轮询到任务真正完成，而不是看到 200 就返回')
        self.assertTrue(client.verify('/夸克/Minecraft_Backup/payload.bin', 300 * 1024))

    def test_verify_detects_size_mismatch(self):
        client = OpenListClient(self.fake.url, username='admin', password='secret', timeout=30.0)
        self.fake.files['/夸克/Minecraft_Backup/x.tar'] = b'123'
        self.assertFalse(client.verify('/夸克/Minecraft_Backup/x.tar', 999))
        self.assertFalse(client.verify('/夸克/Minecraft_Backup/missing.tar', 3))

    def test_token_401_triggers_relogin(self):
        client = OpenListClient(self.fake.url, token='stale-token',
                                username='admin', password='secret', timeout=30.0)
        for path in ('/夸克', REMOTE_DIR):
            self.fake.dirs.add(path)
        client.list_dir(REMOTE_DIR, refresh=True)  # 应自动重新登录而不是抛错
        self.assertGreaterEqual(self.fake.login_count, 1)
        self.assertEqual(client._token, TOKEN)  # noqa: SLF001 测试内部状态

    def test_max_keep_zero_keeps_everything(self):
        for name in ('pb_backup_20260101_020000.tar', 'pb_backup_20260108_020000.tar'):
            self.fake.put_existing(name, 20 * 1024)
        cfg = self.build_config(max_keep=0)
        job = self.run_job(cfg)
        self.assertTrue(job.summary()['last_ok'], job.summary()['last_error'])
        self.assertEqual(len(self.fake.files), 3)
        self.assertEqual(job.summary()['removed'], [])

    def test_failed_task_is_canceled(self):
        """上传任务失败后必须把这个任务收掉，否则它会一直挂在 OpenList 的待处理列表里。"""
        client = OpenListClient(self.fake.url, username='admin', password='secret',
                                timeout=30.0, as_task=True)
        local = os.path.join(self.root, 'payload.bin')
        with open(local, 'wb') as f:
            f.write(os.urandom(64 * 1024))
        self.fake.fail_next_task = True
        with self.assertRaises(Exception):
            client.upload(local, '/夸克/Minecraft_Backup/fail.bin')
        self.assertEqual(len(self.fake.canceled), 1)
        tid = self.fake.canceled[0]
        self.assertEqual(self.fake.tasks[tid]['state'], 4)  # canceled

    def test_missing_credentials_fails_cleanly(self):
        cfg = self.build_config()
        cfg.openlist_username = ''
        cfg.openlist_password = ''
        job = self.run_job(cfg)
        summary = job.summary()
        self.assertFalse(summary['last_ok'])
        self.assertIn('凭据', summary['last_error'])
        # 失败也要清理临时文件
        leftovers = os.listdir(cfg.temp_dir) if os.path.isdir(cfg.temp_dir) else []
        self.assertEqual([n for n in leftovers if n.endswith(('.tar', '.db'))], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
