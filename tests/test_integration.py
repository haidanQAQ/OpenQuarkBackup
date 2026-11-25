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
from open_quark_backup.openlist_api import OpenListClient, OpenListError  # noqa: E402
from open_quark_backup import OpenQuarkBackupPlugin  # noqa: E402

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
        self.mkdir_calls = []
        self.task_info_paths = []
        self.request_count = 0
        self.fail_tasks = set()
        self.fail_next_task = False
        #: 接下来多少次业务请求返回 HTTP 200 + {"code": 401}（模拟 AList 的鉴权失效报文）
        self.json_401_remaining = 0
        #: True 时所有业务请求都返回 200 + code=401（验证不会无限递归重登）
        self.json_401_always = False
        #: 登录接口直接返回 HTTP 401 / HTTP 200 + code=401（验证登录失败不会递归）
        self.login_http_401 = False
        self.login_json_401 = False
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

            def _json_401(self):
                """按需伪造 AList 的「HTTP 200 + code 401」鉴权失效。"""
                with fake._lock:
                    if fake.json_401_always:
                        return True
                    if fake.json_401_remaining > 0:
                        fake.json_401_remaining -= 1
                        return True
                return False

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                with fake._lock:
                    fake.request_count += 1
                if path == '/api/auth/login':
                    data = json.loads(self._body() or b'{}')
                    if fake.login_http_401:
                        return self._reply(401, {'code': 401, 'message': 'login rejected'})
                    if fake.login_json_401:
                        return self._reply(200, {'code': 401, 'message': 'login rejected'})
                    if data.get('username') == 'admin' and data.get('password') == 'secret':
                        fake.login_count += 1
                        return self._reply(200, {'code': 200, 'data': {'token': TOKEN}})
                    return self._reply(200, {'code': 401, 'message': 'wrong password'})

                if not self._authorized():
                    return self._reply(401, {'code': 401, 'message': 'token is invalidated'})
                if self._json_401():
                    return self._reply(200, {'code': 401, 'message': 'token is invalidated'})

                if path == '/api/fs/mkdir':
                    target = json.loads(self._body())['path']
                    parent = os.path.dirname(target.rstrip('/')) or '/'
                    with fake._lock:
                        fake.mkdir_calls.append(target)
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
                        if target in fake.dirs:
                            return self._reply(200, {'code': 200, 'data': {
                                'name': os.path.basename(target) or '/',
                                'size': 0,
                                'is_dir': True,
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

                if path in ('/api/task/upload/info', '/api/task/info'):
                    with fake._lock:
                        fake.task_info_paths.append(path)
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

                if path == '/api/admin/task/upload/info':
                    # 只有根路由存在时才会走到这里；测试里默认让管理端路由 404，
                    # 用来证明客户端用的是 /api/task/upload/info
                    return self._reply(404, {'code': 404, 'message': 'no such endpoint'})

                return self._reply(404, {'code': 404, 'message': 'no such endpoint'})

            def do_PUT(self):
                parsed = urllib.parse.urlsplit(self.path)
                with fake._lock:
                    fake.request_count += 1
                if parsed.path != '/api/fs/put':
                    return self._reply(404, {'code': 404, 'message': 'no such endpoint'})
                if not self._authorized():
                    self._body()
                    return self._reply(401, {'code': 401, 'message': 'token is invalidated'})
                if self._json_401():
                    self._body()
                    return self._reply(200, {'code': 401, 'message': 'token is invalidated'})

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
    """冒充 MCDR 的 PluginServerInterface（只用到这几个方法）。"""

    def __init__(self, root):
        self.root = root
        self._logger = logging.getLogger('oqb.test')
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False
        #: 注册过的命令节点与帮助信息，用来验证"配置坏了也要能注册命令"
        self.commands = []
        self.help_messages = []

    @property
    def logger(self):
        return self._logger

    def register_command(self, node, *args, **kwargs):
        self.commands.append(node)

    def register_help_message(self, prefix, message, permission=0):
        self.help_messages.append((prefix, message, permission))

    def get_data_folder(self):
        path = os.path.join(self.root, 'config', 'open_quark_backup')
        os.makedirs(path, exist_ok=True)
        return path

    def get_mcdr_config(self):
        return {'working_directory': os.path.join(self.root, 'server')}

    def load_config_simple(self, file_name='config.json', target_class=Config,
                           encoding='utf-8', **_kwargs):
        """冒充 MCDR 的配置读取（只支持我们自己的 Config）。"""
        path = os.path.join(self.get_data_folder(), file_name)
        cfg = target_class() if target_class is not None else {}
        if os.path.isfile(path):
            with io.open(path, encoding=encoding) as f:
                raw = json.load(f)
            for key, value in raw.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        return cfg


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

    # ------------------------------------------------------------ 审查问题回归
    def test_task_info_uses_upload_route(self):
        """任务查询必须走 /api/task/upload/info（分组路由），不是 /api/task/info。"""
        client = OpenListClient(self.fake.url, username='admin', password='secret',
                                timeout=30.0, as_task=True)
        local = os.path.join(self.root, 'payload.bin')
        with open(local, 'wb') as f:
            f.write(os.urandom(128 * 1024))
        client.upload(local, '/夸克/Minecraft_Backup/route.bin')
        self.assertTrue(self.fake.task_info_paths)
        self.assertEqual(self.fake.task_info_paths[0], '/api/task/upload/info')
        self.assertNotIn('/api/task/info', self.fake.task_info_paths)

    def test_json_401_triggers_exactly_one_relogin(self):
        """HTTP 200 + {"code": 401} 也算鉴权失效：清 token、重登、原请求重试一次。"""
        self.fake.dirs.update({'/夸克', REMOTE_DIR})
        # 用当前有效的 token，确保触发的是 body 里的 code=401 而不是 HTTP 401
        client = OpenListClient(self.fake.url, token=TOKEN,
                                username='admin', password='secret', timeout=30.0)
        self.fake.json_401_remaining = 1
        items = client.list_dir(REMOTE_DIR, refresh=True)
        self.assertEqual(items, [])
        self.assertEqual(self.fake.login_count, 1, '应恰好重登一次')
        self.assertEqual(client._token, TOKEN)  # noqa: SLF001

    def test_json_401_never_recurses(self):
        """持续 401 时必须抛错而不是无限重登（retry_auth 防递归保护）。"""
        self.fake.dirs.update({'/夸克', REMOTE_DIR})
        client = OpenListClient(self.fake.url, token='stale-token',
                                username='admin', password='secret', timeout=30.0)
        self.fake.json_401_always = True
        before = self.fake.request_count
        with self.assertRaises(OpenListError):
            client.list_dir(REMOTE_DIR, refresh=True)
        attempts = self.fake.request_count - before
        # 1 次原请求 + 1 次重登 + 1 次重试，绝不能再多
        self.assertLessEqual(attempts, 3, '重试次数失控: {} 次请求'.format(attempts))
        self.assertEqual(self.fake.login_count, 1)

    def test_ensure_dir_skips_existing_mount_root(self):
        """挂载根 /夸克 已存在时，ensure_dir 不能再去 mkdir 它。"""
        self.fake.dirs.add('/夸克')
        client = OpenListClient(self.fake.url, username='admin', password='secret', timeout=30.0)
        client.ensure_dir(REMOTE_DIR)
        self.assertNotIn('/夸克', self.fake.mkdir_calls)
        self.assertIn(REMOTE_DIR, self.fake.mkdir_calls)
        self.assertIn(REMOTE_DIR, self.fake.dirs)

    def test_ensure_dir_is_idempotent_when_target_exists(self):
        self.fake.dirs.update({'/夸克', REMOTE_DIR})
        client = OpenListClient(self.fake.url, username='admin', password='secret', timeout=30.0)
        client.ensure_dir(REMOTE_DIR)
        self.assertEqual(self.fake.mkdir_calls, [], '目录已存在时不应调用 mkdir')

    # ------------------------------------------------------------ 二轮审计回归
    def test_ensure_dir_rejects_file_in_the_way(self):
        """远端有个同名普通文件占着目录路径时，必须明确报错而不是静默继续。"""
        # '/夸克/Minecraft_Backup' 是文件而不是目录
        self.fake.files[REMOTE_DIR] = b'not a directory'
        client = OpenListClient(self.fake.url, username='admin', password='secret', timeout=30.0)
        with self.assertRaises(OpenListError) as ctx:
            client.ensure_dir(REMOTE_DIR)
        self.assertIn('同名文件', str(ctx.exception))
        self.assertNotIn(REMOTE_DIR, self.fake.mkdir_calls)

    def test_login_http_401_raises_without_recursion(self):
        """登录接口返回 HTTP 401 时直接报错：不能再触发"重新登录"把自己递归进去。"""
        self.fake.login_http_401 = True
        client = OpenListClient(self.fake.url, username='admin', password='secret', timeout=30.0)
        before = self.fake.request_count
        with self.assertRaises(OpenListError):
            client.login()
        self.assertEqual(self.fake.request_count - before, 1, '登录失败后不应再重试')
        self.assertEqual(self.fake.login_count, 0)
        self.assertEqual(client._token, '')  # noqa: SLF001

    def test_login_json_401_raises_without_recursion(self):
        """登录接口返回 HTTP 200 + {"code": 401}（密码错）时同样不能递归重登。"""
        self.fake.login_json_401 = True
        client = OpenListClient(self.fake.url, username='admin', password='secret', timeout=30.0)
        before = self.fake.request_count
        with self.assertRaises(OpenListError):
            client.login()
        self.assertEqual(self.fake.request_count - before, 1)
        self.assertEqual(self.fake.login_count, 0)


class BrokenConfigTest(unittest.TestCase):
    """config.json 写坏时，命令仍要注册上，修好文件后一条 !!oqb reload 就能恢复。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='oqb_broken_')
        self.server = FakeServer(self.root)
        self.pb_dir, self.db_path = make_pb_files(self.root)
        self.config_path = os.path.join(self.server.get_data_folder(), 'config.json')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_load_registers_commands_even_when_config_is_broken(self):
        with io.open(self.config_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('{ this is not json ')
        plugin = OpenQuarkBackupPlugin(self.server)

        plugin.load()  # 不能抛异常

        self.assertIsNone(plugin.resolved, '配置坏了就不该有解析结果')
        self.assertIsNone(plugin.job)
        self.assertEqual(len(self.server.commands), 1, '命令必须注册，否则运维没法 reload')
        self.assertEqual(len(self.server.help_messages), 1)
        self.assertEqual(self.server.help_messages[0][0], '!!oqb')

    def test_reload_recovers_after_config_is_fixed(self):
        with io.open(self.config_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('{ broken ')
        plugin = OpenQuarkBackupPlugin(self.server)
        plugin.load()
        self.assertIsNone(plugin.resolved)

        # 运维把文件改回合法 JSON，然后直接 !!oqb reload
        payload = {
            'pb_files_dir': self.pb_dir,
            'temp_dir': os.path.join(self.root, 'tmp'),
            'cron': '0 2 * * 1',
        }
        with io.open(self.config_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(payload, f)
        try:
            plugin._reload()  # noqa: SLF001
            self.assertIsNotNone(plugin.resolved)
            self.assertIsNotNone(plugin.job)
            self.assertEqual(plugin.job.config.pb_files_dir, self.pb_dir)
        finally:
            plugin.unload()


class ReloadKeepsJobTest(unittest.TestCase):
    """``!!oqb reload`` 不能重建 BackupJob，否则运行中的互斥锁就没了。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='oqb_reload_')
        self.server = FakeServer(self.root)
        self.pb_dir, self.db_path = make_pb_files(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_resolved(self, cron='0 2 * * 1'):
        cfg = Config()
        cfg.pb_files_dir = self.pb_dir
        cfg.temp_dir = os.path.join(self.root, 'tmp')
        cfg.cron = cron
        return resolve_config(cfg, self.server)

    def test_reload_keeps_same_job_instance(self):
        plugin = OpenQuarkBackupPlugin(self.server)
        plugin._reload(initial=True)  # noqa: SLF001
        job = plugin.job
        self.assertIsNotNone(job)
        plugin._reload()  # noqa: SLF001
        self.assertIs(plugin.job, job, 'reload 不应重建 BackupJob')
        self.assertEqual(plugin.job.config.cron_expression, '0 2 * * 1')

    def test_reload_does_not_break_running_lock(self):
        job = BackupJob(self.make_resolved(), self.server.logger)
        # 直接占住互斥锁，模拟"任务正在运行"
        self.assertTrue(job._lock.acquire(blocking=False))  # noqa: SLF001
        try:
            job.update_config(self.make_resolved(cron='30 3 * * *'))
            self.assertFalse(job.start('manual'), '运行中必须拒绝并发启动')
            self.assertTrue(job.is_running())
            self.assertEqual(job.config.cron_expression, '30 3 * * *')
        finally:
            job._lock.release()  # noqa: SLF001

    def test_update_config_swaps_config_only(self):
        job = BackupJob(self.make_resolved(), self.server.logger)
        old_lock = job._lock  # noqa: SLF001
        job.update_config(self.make_resolved(cron='0 4 * * *'))
        self.assertIs(job._lock, old_lock)  # noqa: SLF001
        self.assertEqual(job.config.cron_expression, '0 4 * * *')

    def test_unload_joins_scheduler(self):
        plugin = OpenQuarkBackupPlugin(self.server)
        plugin._reload(initial=True)  # noqa: SLF001
        scheduler = plugin.scheduler
        self.assertTrue(scheduler.is_alive())
        plugin.unload()
        self.assertFalse(scheduler.is_alive(), '卸载后调度线程必须退出')
        self.assertIsNone(plugin.scheduler)


if __name__ == '__main__':
    unittest.main(verbosity=2)
