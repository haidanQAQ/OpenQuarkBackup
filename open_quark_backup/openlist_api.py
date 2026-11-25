"""
OpenList REST API 客户端（兼容 AList v3 协议）。

兼容性说明：本插件面向 OpenList 开发。AList 与 OpenList 的 v3 接口基本一致，
理论上直接把地址和账号密码/Token 填进来就能用，但未针对各版本 AList 做逐一强适配；
如个别 AList 环境不可用，请以 OpenList 为准。

实现要点：
* 上传走 ``PUT /api/fs/put`` 的原始流式接口，用 http.client 分块发送，
  不会把整个备份包读进内存（8GB 机器上传数 GB 文件也不会炸）；
* ``as_task=true`` 时接口只返回一个任务 id，必须轮询 ``/api/task/upload/info``
  直到任务真正结束，否则会把"已入队"误判成"已上传"；
* Token 过期时自动用账号密码重新登录并重试一次。OpenList/AList 的鉴权失效既可能
  是 HTTP 401，也可能被包成 HTTP 200 + ``{"code": 401}``，两种都要认。
"""

from __future__ import annotations

import http.client
import json
import os
import posixpath
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

#: OpenList 任务状态（`tache.State`）。
TASK_STATE_PENDING = 0
TASK_STATE_RUNNING = 1
TASK_STATE_SUCCEEDED = 2
TASK_STATE_CANCELING = 3
TASK_STATE_CANCELED = 4
TASK_STATE_ERRORED = 5
TASK_STATE_FAILING = 6
TASK_STATE_FAILED = 7
TASK_STATE_WAITING_RETRY = 8
TASK_STATE_BEFORE_RETRY = 9

TERMINAL_OK = {TASK_STATE_SUCCEEDED}
TERMINAL_FAIL = {TASK_STATE_CANCELED, TASK_STATE_ERRORED, TASK_STATE_FAILING, TASK_STATE_FAILED}

#: 上传时每次从本地文件读取的块大小。
UPLOAD_CHUNK_SIZE = 1024 * 1024

#: 轮询任务状态的间隔（秒）。
TASK_POLL_INTERVAL = 1.5

#: 任务查询路由。新版 OpenList 把任务按分组挂在 ``/api/task/upload/info`` 下，
#: 个别反向代理/管理端配置只暴露 ``/api/admin/task/upload/info``；两条都试，命中的记下来复用。
TASK_INFO_ROUTES = (
    '/api/task/upload/info',
    '/api/admin/task/upload/info',
)

#: 任务分组前缀的兜底值（在还没探测过任何任务路由时使用）。
TASK_DEFAULT_BASE = '/api/task/upload'

#: 取消/删除任务这类"收拾残局"请求的超时上限（秒）：不宜沿用上传时的大超时。
TASK_CLEANUP_TIMEOUT = 15.0


class OpenListError(Exception):
    """OpenList 接口调用失败。``status`` 为 HTTP 状态码（网络错误时为 None）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class OpenListClient:
    def __init__(self, base_url: str, token: str = '', username: str = '', password: str = '',
                 timeout: float = 60.0, as_task: bool = True, logger=None):
        self.base_url = (base_url or '').rstrip('/')
        self._token = (token or '').strip()
        self.username = (username or '').strip()
        self.password = password or ''
        self.timeout = float(timeout)
        self.as_task = bool(as_task)
        self.logger = logger
        #: 最近一次上传产生的任务 id，失败时可据此取消，避免僵尸任务堆在 OpenList 里
        self.last_task_id: Optional[str] = None
        #: 已经验证可用的任务查询路由（首次查询时探测后缓存）
        self._task_info_route: Optional[str] = None
        if not self.base_url:
            raise OpenListError('openlist_url 未配置')

    # ---------------------------------------------------------------- 内部
    def _log(self, level: str, message: str) -> None:
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message)

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _auth_headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self._ensure_token():
            headers['Authorization'] = self._token
        return headers

    def _ensure_token(self) -> bool:
        if self._token:
            return True
        if self.username and self.password:
            self.login()
            return bool(self._token)
        return False

    def _decode(self, raw: bytes) -> dict:
        text = raw.decode('utf-8', 'replace')
        try:
            return json.loads(text)
        except ValueError:
            raise OpenListError('OpenList 返回了非 JSON 内容: {!r}'.format(text[:200])) from None

    def _relogin(self, where: str) -> None:
        """清掉失效的 token 并用账号密码重新登录（调用方负责重试一次）。"""
        self._log('warning', '{} 时 OpenList Token 失效，尝试用账号密码重新登录'.format(where))
        self._token = ''
        self.login()

    def _has_credentials(self) -> bool:
        """能否用账号密码登录：两者都有才行，缺一个去登录只会白跑一趟。"""
        return bool(self.username and self.password)

    def _request_json(self, method: str, path: str, body: Optional[dict] = None,
                      auth: bool = True, retry_auth: bool = True,
                      timeout: Optional[float] = None) -> dict:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if auth and self._ensure_token():
            headers['Authorization'] = self._token
        request = urllib.request.Request(self._url(path), data=data, headers=headers, method=method)
        # 登录请求自己不能触发"重新登录"：那会变成递归。``auth`` 为 False 的请求同理。
        can_retry_auth = bool(retry_auth and auth and self._has_credentials())
        try:
            with urllib.request.urlopen(request, timeout=timeout if timeout is not None else self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raw = b''
            try:
                raw = e.read()
            except Exception:
                pass
            if e.code == 401 and can_retry_auth:
                self._relogin('HTTP 401')
                return self._request_json(method, path, body, auth=auth, retry_auth=False, timeout=timeout)
            message = self._error_message(raw) or 'HTTP {}'.format(e.code)
            raise OpenListError('{} {} 失败: {}'.format(method, path, message), status=e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OpenListError('{} {} 网络错误: {}'.format(method, path, e)) from None

        payload = self._decode(raw)
        # AList/OpenList 在鉴权失效时经常仍返回 HTTP 200，只在 body 里写 code=401
        if payload.get('code') == 401 and can_retry_auth:
            self._relogin('code=401')
            return self._request_json(method, path, body, auth=auth, retry_auth=False, timeout=timeout)
        if payload.get('code') not in (200, None):
            raise OpenListError('{} {} 返回错误: {}'.format(method, path, payload.get('message') or payload),
                                status=payload.get('code'))
        return payload

    @staticmethod
    def _error_message(raw: bytes) -> str:
        try:
            payload = json.loads(raw.decode('utf-8', 'replace'))
            if isinstance(payload, dict):
                return str(payload.get('message') or payload.get('detail') or payload)
        except Exception:
            pass
        return ''

    # ---------------------------------------------------------------- 登录
    def login(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        if not self._has_credentials():
            raise OpenListError('未配置 openlist_token，也没有 openlist_username/password，无法登录 OpenList')
        # retry_auth=False：登录接口自己返回 401 时必须直接报错，绝不能再去"重新登录"
        payload = self._request_json(
            'POST', '/api/auth/login',
            {'username': self.username, 'password': self.password},
            auth=False,
            retry_auth=False,
        )
        data = payload.get('data') or {}
        token = data.get('token') if isinstance(data, dict) else None
        if not token:
            raise OpenListError('登录成功但没有拿到 token: {}'.format(payload))
        self._token = str(token)
        self._log('info', 'OpenList 登录成功（用户 {}）'.format(self.username))
        return self._token

    # ---------------------------------------------------------------- 文件
    def mkdir(self, path: str) -> None:
        """创建目录；已存在时静默忽略。"""
        try:
            self._request_json('POST', '/api/fs/mkdir', {'path': path})
        except OpenListError as e:
            message = str(e).lower()
            if 'already exists' in message or 'exist' in message:
                return
            raise

    def ensure_dir(self, path: str) -> None:
        """
        逐级确保远端目录存在。

        AList 的 ``mkdir`` 在父目录不存在时会直接失败，所以要一级一级建；但反过来，
        对已经存在的层级重复 ``mkdir`` 会白打请求、某些驱动还会报错。因此每一级都先
        ``get`` 探测：存在就跳过，不存在才建。挂载点根目录（``/夸克`` 这类）同理，
        已经存在时不会被再次创建。

        每一级探测到的若不是目录（远端有个同名普通文件占着路径），立刻报错——继续
        ``mkdir`` 只会拿到一个语焉不详的驱动错误，事后上传还会静默失败。
        """
        normalized = (path or '').strip()
        if not normalized or normalized == '/':
            return
        info = self.get(normalized)
        if info is not None:
            self._assert_is_dir(info, normalized)
            return

        current = ''
        for part in [p for p in normalized.split('/') if p]:
            current += '/' + part
            info = self.get(current)
            if info is not None:
                self._assert_is_dir(info, current)
                continue
            self.mkdir(current)

    @staticmethod
    def _assert_is_dir(info: dict, path: str) -> None:
        if not info.get('is_dir'):
            raise OpenListError('远端路径被同名文件占用，无法作为目录使用: {}'.format(path))

    def list_dir(self, path: str, refresh: bool = True) -> List[dict]:
        payload = self._request_json('POST', '/api/fs/list', {
            'path': path,
            'password': '',
            'page': 1,
            'per_page': 0,   # 0 = 全部
            'refresh': bool(refresh),
        })
        data = payload.get('data') or {}
        content = data.get('content') if isinstance(data, dict) else None
        return [item for item in (content or []) if isinstance(item, dict)]

    def get(self, path: str) -> Optional[dict]:
        """查询单个文件/目录信息；不存在时返回 None。"""
        try:
            payload = self._request_json('POST', '/api/fs/get', {'path': path})
        except OpenListError as e:
            message = str(e).lower()
            if e.status in (404, 500) and ('not found' in message or '不存在' in message or 'object not exist' in message):
                return None
            if 'not found' in message or 'object not exist' in message:
                return None
            raise
        data = payload.get('data')
        return data if isinstance(data, dict) else None

    def remove(self, dir_path: str, names: List[str]) -> None:
        if not names:
            return
        self._request_json('POST', '/api/fs/remove', {'dir': dir_path, 'names': list(names)})

    def delete_file(self, path: str) -> None:
        # 远端路径是 POSIX 语义（永远用 '/' 分隔），不能用 os.path.split：
        # 在 Windows 上它只认 '\\'，会把整个路径当成文件名。
        dir_path, name = posixpath.split('/' + (path or '').lstrip('/'))
        self.remove(dir_path or '/', [name])

    # ---------------------------------------------------------------- 上传
    def upload(self, local_path: str, remote_path: str, as_task: Optional[bool] = None,
               progress: Optional[Callable[[int, int], None]] = None,
               cancel_check: Optional[Callable[[], bool]] = None,
               retry_auth: bool = True) -> dict:
        """
        流式上传本地文件到远端路径。

        * ``as_task`` 为 True 时会等待任务真正完成（轮询 ``/api/task/upload/info``）；
        * 返回接口的 ``data`` 字典（可能含 ``task``）；
        * ``retry_auth`` 只允许重试一轮：重新登录后仍 401 就抛错，不会无限递归；
        * 网络错误/5xx 会抛 :class:`OpenListError`，由调用方决定是否重试。
        """
        if not os.path.isfile(local_path):
            raise OpenListError('本地文件不存在: {}'.format(local_path))
        size = os.path.getsize(local_path)
        use_task = self.as_task if as_task is None else bool(as_task)

        self._ensure_token()
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in ('http', 'https'):
            raise OpenListError('openlist_url 必须以 http:// 或 https:// 开头')

        connection_cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        prefix = (parsed.path or '').rstrip('/')
        request_path = prefix + '/api/fs/put'

        connection = connection_cls(parsed.hostname, port, timeout=self.timeout)
        try:
            connection.putrequest('PUT', request_path)
            connection.putheader('Authorization', self._token)
            # AList/OpenList 要求路径做 URL 编码（中文目录名必须编码，否则按 latin-1 发送会崩）
            connection.putheader('File-Path', urllib.parse.quote(remote_path, safe='/'))
            connection.putheader('Content-Type', 'application/octet-stream')
            connection.putheader('Content-Length', str(size))
            connection.putheader('X-File-Size', str(size))
            if use_task:
                connection.putheader('As-Task', 'true')
            connection.endheaders()

            sent = 0
            with open(local_path, 'rb') as f:
                while True:
                    chunk = f.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent, size)
                    if cancel_check is not None and cancel_check():
                        raise OpenListError('上传被取消')
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (http.client.HTTPException, OSError) as e:
            raise OpenListError('上传网络错误: {}'.format(e)) from None
        finally:
            try:
                connection.close()
            except Exception:
                pass

        if status == 401 and retry_auth and self._has_credentials():
            self._relogin('上传返回 HTTP 401')
            return self.upload(local_path, remote_path, as_task=as_task, progress=progress,
                               cancel_check=cancel_check, retry_auth=False)
        if status >= 400:
            message = self._error_message(raw) or 'HTTP {}'.format(status)
            raise OpenListError('上传失败（{}）: {}'.format(status, message), status=status)

        payload = self._decode(raw)
        if payload.get('code') == 401 and retry_auth and self._has_credentials():
            # 同样是"HTTP 200 + body 里 code=401"的鉴权失效，重登后再发一次，最多一轮
            self._relogin('上传返回 code=401')
            return self.upload(local_path, remote_path, as_task=as_task, progress=progress,
                               cancel_check=cancel_check, retry_auth=False)
        if payload.get('code') not in (200, None):
            raise OpenListError('上传被拒绝: {}'.format(payload.get('message') or payload))

        data = payload.get('data') or {}
        task = data.get('task') if isinstance(data, dict) else None
        if use_task and isinstance(task, dict) and task.get('id'):
            task_id = str(task['id'])
            self.last_task_id = task_id
            self._log('info', '已创建上传任务 {}，等待完成…'.format(task_id))
            try:
                self.wait_task(task_id, cancel_check=cancel_check)
            except OpenListError:
                # 失败/超时/取消：先把这个任务收掉，否则它会一直挂在 OpenList 的
                # 「待处理」列表里（undone 包含 Errored / WaitingRetry 等状态），
                # 每重试一次就多一条僵尸任务。
                self.cancel_task(task_id)
                raise
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------------- 任务
    def task_info(self, task_id: str) -> Optional[dict]:
        """
        查询上传任务状态。

        OpenList 的路由是 ``/api/task/upload/info?tid=<id>``（任务按分组挂在
        ``/api/task/<group>/`` 下，上传组的 group 就是 ``upload``）；个别只暴露管理端
        路由的环境退到 ``/api/admin/task/upload/info``。首次探测成功的路由会被缓存，
        之后不再重复试探。
        """
        query = '?tid={}'.format(urllib.parse.quote(str(task_id)))
        routes = (self._task_info_route,) if self._task_info_route else TASK_INFO_ROUTES
        last_error: Optional[OpenListError] = None
        for route in routes:
            if not route:
                continue
            try:
                payload = self._request_json('POST', route + query, {})
            except OpenListError as e:
                last_error = e
                if e.status == 404:
                    self._log('debug', '任务查询路由 {} 不存在，尝试下一条'.format(route))
                    continue
                raise
            self._task_info_route = route
            data = payload.get('data')
            return data if isinstance(data, dict) else None
        if last_error is not None:
            raise last_error
        return None

    def cancel_task(self, task_id: str) -> bool:
        """取消一个上传任务（成功后它会进入已取消，不再占「待处理」）。"""
        try:
            # 取消/删除是"收拾残局"的动作，用短超时：真失败也不该把退出流程卡上半小时
            self._request_json('POST', '{}/cancel?tid={}'.format(
                self._task_base(), urllib.parse.quote(str(task_id))), {},
                timeout=min(self.timeout, TASK_CLEANUP_TIMEOUT))
            self._log('info', '已取消残留上传任务 {}'.format(task_id))
            return True
        except OpenListError as e:
            self._log('warning', '取消上传任务 {} 失败: {}'.format(task_id, e))
            return False

    def delete_task(self, task_id: str) -> bool:
        """把任务记录从 OpenList 的任务列表里删掉。"""
        try:
            self._request_json('POST', '{}/delete?tid={}'.format(
                self._task_base(), urllib.parse.quote(str(task_id))), {},
                timeout=min(self.timeout, TASK_CLEANUP_TIMEOUT))
            return True
        except OpenListError as e:
            self._log('warning', '删除上传任务记录 {} 失败: {}'.format(task_id, e))
            return False

    def _task_base(self) -> str:
        """
        任务接口的分组前缀。

        任务路由是按分组挂在 ``/api/task/<group>`` 或 ``/api/admin/task/<group>`` 下的，
        ``task_info()`` 探测成功后已经把命中的完整路由缓存进 ``self._task_info_route``，
        取消/删除直接由它推出前缀，避免一边查 ``/api/admin/...`` 一边去取消 ``/api/...``。
        """
        route = self._task_info_route or TASK_INFO_ROUTES[0]
        if route.endswith('/info'):
            return route[:-len('/info')]
        return TASK_DEFAULT_BASE

    def wait_task(self, task_id: str, timeout: Optional[float] = None,
                  cancel_check: Optional[Callable[[], bool]] = None) -> dict:
        """轮询任务直到成功/失败/超时。"""
        limit = float(timeout if timeout is not None else self.timeout)
        started = time.time()
        deadline = started + limit
        last_state = None
        while time.time() < deadline:
            if cancel_check is not None and cancel_check():
                raise OpenListError('等待上传任务时被取消')
            info = self.task_info(task_id)
            if info is None:
                # 任务记录可能已被清理：交给上层用 /api/fs/get 校验体积兜底
                self._log('warning', '查不到任务 {}，改为依赖远端文件校验'.format(task_id))
                return {}
            state = info.get('state')
            if state != last_state:
                last_state = state
                self._log('info', '上传任务 {} 状态: {} ({})'.format(
                    task_id, info.get('status') or state, state))
            if state in TERMINAL_OK:
                return info
            if state in TERMINAL_FAIL:
                raise OpenListError('上传任务失败: {}'.format(info.get('error') or info.get('status') or state))
            time.sleep(TASK_POLL_INTERVAL)
        raise OpenListError('等待上传任务超时（{} 秒）'.format(int(limit)))

    # ---------------------------------------------------------------- 校验
    def verify(self, remote_path: str, expected_size: int) -> bool:
        """
        确认远端文件存在且大小一致。

        夸克这类网盘在刚上传完的瞬间，``/api/fs/get`` 可能仍然命中旧缓存（报未找到或
        旧的 size）。第一次核对不通过时，先对父目录做一次 ``refresh=true`` 的强制刷新，
        再从列表里二次核对，避免把"其实已经成功"的上传误判成失败、进而重试出一条重复包。
        """
        if self._size_matches(self.get(remote_path), expected_size):
            return True

        parent, name = posixpath.split('/' + (remote_path or '').lstrip('/'))
        try:
            items = self.list_dir(parent or '/', refresh=True)
        except OpenListError as e:
            self._log('warning', '校验前刷新远端目录失败: {}'.format(e))
            return False
        for item in items:
            if isinstance(item, dict) and item.get('name') == name:
                return self._size_matches(item, expected_size)
        return False

    @staticmethod
    def _size_matches(info: Optional[dict], expected_size: int) -> bool:
        if not isinstance(info, dict):
            return False
        try:
            return int(info.get('size') or 0) == int(expected_size)
        except (TypeError, ValueError):
            return False


def redact(value: str) -> str:
    """日志/命令输出里对凭据做脱敏。"""
    if not value:
        return '(未配置)'
    if len(value) <= 6:
        return '*' * len(value)
    return value[:3] + '*' * (len(value) - 6) + value[-3:]
