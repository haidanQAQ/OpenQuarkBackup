"""
配置模型与"运行时解析"。

设计原则（见 README 的"配置"一节）：
* 所有路径、地址、凭据与计划都来自 ``config.json``，业务代码里不出现任何硬编码；
* 首次加载自动生成规范 JSON；改动后 ``!!oqb reload`` 立即生效，不需要重启 MCDR；
* 相对路径一律相对 MCDR 根目录解释（与 PrimeBackup 的 ``storage_root`` 语义一致）。
"""

from __future__ import annotations

import inspect
import io
import json
import os
from dataclasses import dataclass
from datetime import tzinfo
from typing import Optional

from mcdreforged.api.all import Serializable

from .cron import CronSpec, CronSyntaxError, upcoming_fires

#: 环境变量覆盖（凭据放环境变量比写进配置文件更安全）。
ENV_OPENLIST_URL = 'OQB_OPENLIST_URL'
ENV_OPENLIST_USERNAME = 'OQB_OPENLIST_USERNAME'
ENV_OPENLIST_PASSWORD = 'OQB_OPENLIST_PASSWORD'
ENV_OPENLIST_TOKEN = 'OQB_OPENLIST_TOKEN'

#: 探测 PB 数据库时优先尝试的文件名（新版 PB 是 prime_backup.db）。
DB_CANDIDATE_NAMES = ('prime_backup.db', 'pbmeta.db')

#: ``filename_prefix`` 非法时回退到的默认值。
DEFAULT_FILENAME_PREFIX = 'pb_backup_'

#: PrimeBackup 自己的配置文件相对路径（相对 MCDR 根目录）。
PB_CONFIG_RELATIVE = os.path.join('config', 'prime_backup', 'config.json')


class Config(Serializable):
    """
    ``config.json`` 的结构。

    注意：本插件面向 OpenList（兼容 AList v3 API）；AList 理论上可直接填入地址与
    账号密码/Token 使用，但不保证全版本兼容，用不了时以 OpenList 为准。
    """

    #: 总开关；关闭后不再按 Cron 触发（手动 ``!!oqb backup`` 仍可用）。
    enabled: bool = True
    #: PrimeBackup 仓库目录；留空或写 ``./pb_files`` 时优先使用自动探测结果。
    pb_files_dir: str = './pb_files'
    #: 是否顺带备份 PrimeBackup 自身的配置文件。
    include_pb_config: bool = False
    #: PB 配置文件路径；留空则在 include_pb_config=true 时用默认位置。
    pb_config_path: str = ''
    #: 打包暂存目录；留空则用插件数据目录下的 tmp。
    temp_dir: str = ''
    #: 标准 5 段式 Cron：分 时 日 月 周。默认每周一凌晨 02:00。
    #: 务必与 PrimeBackup 本体自动备份时间岔开至少 30-60 分钟。
    cron: str = '0 2 * * 1'
    #: IANA 时区，用于解释 cron 与生成包名时间码。
    timezone: str = 'Asia/Shanghai'
    #: OpenList 地址（支持 IP:端口 或 域名/反代前缀，末尾是否带 / 都可以）。
    openlist_url: str = 'http://127.0.0.1:5244'
    openlist_username: str = ''
    openlist_password: str = ''
    #: 静态 Token；填写后优先使用，留空则用账号密码登录并自动续期。
    openlist_token: str = ''
    #: 远端目标目录，自动规范化前导/尾部斜杠。
    remote_dir: str = '/夸克/Minecraft_Backup'
    #: 备份包名前缀，完整包名为 {filename_prefix}YYYYMMDD_HHMMSS.tar。
    filename_prefix: str = 'pb_backup_'
    #: 远端最多保留份数；<=0 表示永不自动清理（永久保留所有备份）。
    max_keep: int = 4
    #: 清理时判定"有效备份包"的最小体积，用于挡掉异常空壳与 exFAT 隐藏文件。
    min_valid_size_bytes: int = 8192
    #: 是否使用 OpenList 的异步上传任务模式（推荐开启，避免长连接被反代掐断）。
    as_task: bool = True
    timeout_sec: int = 1800
    retry_times: int = 2
    #: 暂存盘剩余空间的安全系数：需要 数据体积 × 该系数 的空闲空间。
    min_free_space_ratio: float = 1.15


@dataclass
class ResolvedConfig:
    """把配置解析成可直接使用的绝对路径与对象。"""

    raw: Config
    mcdr_root: str
    pb_files_dir: str
    db_path: str
    db_name: str
    temp_dir: str
    include_pb_config: bool
    pb_config_path: Optional[str]
    cron_expression: str
    cron_spec: Optional[CronSpec]
    cron_error: Optional[str]
    timezone_name: str
    tz: tzinfo
    tz_error: Optional[str]
    openlist_url: str
    openlist_username: str
    openlist_password: str
    openlist_token: str
    remote_dir: str
    filename_prefix: str
    max_keep: int
    min_valid_size_bytes: int
    as_task: bool
    timeout_sec: int
    retry_times: int
    min_free_space_ratio: float
    #: ``filename_prefix`` 非法时的告警（已自动回退默认值），供上层打日志
    prefix_warning: Optional[str] = None

    def next_fire_times(self, now, count: int = 3):
        if self.cron_spec is None:
            return []
        return upcoming_fires(self.cron_spec, now, count)


# --------------------------------------------------------------------- 工具
def resolve_path(path: str, base: str) -> str:
    """相对路径基于 base 解析，并规范化为绝对路径。"""
    path = os.path.expanduser(os.path.expandvars(path.strip()))
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(os.path.normpath(path))


def normalize_base_url(url: str) -> str:
    """去掉末尾斜杠，保留可能的反代前缀。"""
    url = (url or '').strip()
    while url.endswith('/'):
        url = url[:-1]
    return url


def normalize_remote_dir(path: str) -> str:
    """规范化远端目录：保证前导 /，去掉尾部 /，折叠重复斜杠。"""
    path = (path or '').strip().replace('\\', '/')
    if not path:
        return '/'
    parts = [p for p in path.split('/') if p not in ('', '.')]
    normalized = '/' + '/'.join(parts)
    return normalized


def get_mcdr_root(server) -> str:
    """
    MCDR 根目录：插件里的相对路径都以它为基准。

    注意：MCDR 配置文件里的 ``working_directory`` 是**服务端**的工作目录（通常是
    ``server/``），而 PrimeBackup 的 ``storage_root: ./pb_files`` 是相对 MCDR 进程
    的工作目录（也就是 MCDR 根目录）解析的，所以这里以进程 cwd 为准；只有在 cwd
    里探测不到任何 PB 仓库时，才退回 ``working_directory`` 作为兜底猜测。
    """
    root = os.path.abspath(os.getcwd())
    if detect_pb_files_dir(root):
        return root
    try:
        cfg = server.get_mcdr_config() or {}
        working_directory = cfg.get('working_directory')
        if working_directory:
            candidate = os.path.abspath(working_directory)
            if detect_pb_files_dir(candidate):
                return candidate
    except Exception:
        pass
    return root


def find_database(pb_files_dir: str) -> Optional[str]:
    """在仓库目录里找 PB 的数据库文件。"""
    for name in DB_CANDIDATE_NAMES:
        candidate = os.path.join(pb_files_dir, name)
        if os.path.isfile(candidate):
            return candidate
    try:
        dbs = sorted(f for f in os.listdir(pb_files_dir) if f.lower().endswith('.db'))
    except OSError:
        return None
    return os.path.join(pb_files_dir, dbs[0]) if dbs else None


def _pb_config_storage_root(pb_config_path: str) -> Optional[str]:
    """从 PrimeBackup 的 config.json 里读出 storage_root（可能是相对路径）。"""
    try:
        with io.open(pb_config_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    root = data.get('storage_root')
    if isinstance(root, str) and root.strip():
        return root.strip()
    storage_path = data.get('storage_path')
    if isinstance(storage_path, str) and storage_path.strip():
        return storage_path.strip()
    if isinstance(storage_path, dict):
        root = storage_path.get('root')
        if isinstance(root, str) and root.strip():
            return root.strip()
    return None


def detect_pb_files_dir(mcdr_root: str) -> Optional[str]:
    """
    自动探测 PrimeBackup 仓库目录，优先级：

    1. ``config/prime_backup/config.json`` 里的 ``storage_root``（PB 的权威配置）；
    2. MCDR 根目录下的 ``pb_files``；
    3. MCDR 根目录下一层子目录里能找到数据库的那个。
    """
    configured = _pb_config_storage_root(os.path.join(mcdr_root, PB_CONFIG_RELATIVE))
    if configured:
        candidate = resolve_path(configured, mcdr_root)
        if find_database(candidate):
            return candidate

    default_dir = os.path.join(mcdr_root, 'pb_files')
    if find_database(default_dir):
        return default_dir

    try:
        children = sorted(os.listdir(mcdr_root))
    except OSError:
        children = []
    for name in children:
        candidate = os.path.join(mcdr_root, name)
        if os.path.isdir(candidate) and find_database(candidate):
            return candidate
    return None


# --------------------------------------------------------------------- 加载
def ensure_config_file(server, logger) -> str:
    """
    首次加载时生成 config.json；已存在则原样保留。

    生成时会把自动探测到的 PB 仓库路径写进 ``pb_files_dir``，
    这样运维打开文件就能看到实际值，而不必猜"自动探测"到底探测到了什么。
    """
    data_folder = server.get_data_folder()
    os.makedirs(data_folder, exist_ok=True)
    path = os.path.join(data_folder, 'config.json')
    if os.path.isfile(path):
        return path

    cfg = Config()
    mcdr_root = get_mcdr_root(server)
    detected = detect_pb_files_dir(mcdr_root)
    if detected:
        try:
            rel = os.path.relpath(detected, mcdr_root)
            cfg.pb_files_dir = './' + rel.replace(os.sep, '/') if not rel.startswith('..') else detected
        except ValueError:
            cfg.pb_files_dir = detected
        logger.info('已自动探测到 PrimeBackup 仓库: {}'.format(detected))
    else:
        logger.warning('未探测到 PrimeBackup 仓库，暂时按 {} 写入配置，请按实际情况修改'.format(cfg.pb_files_dir))

    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(cfg.serialize(), f, ensure_ascii=False, indent=4)
        f.write('\n')
    logger.info('已生成默认配置: {}'.format(path))
    return path


def load_config(server, logger) -> tuple:
    """生成（如缺失）、读取并解析配置，返回 (raw_config, resolved)。"""
    ensure_config_file(server, logger)
    cfg = server.load_config_simple(**_load_config_kwargs(server))
    if not isinstance(cfg, Config):
        raise RuntimeError('配置文件解析结果不是 Config 实例')
    resolved = resolve_config(cfg, server)
    return cfg, resolved


def _accepted_params(func):
    """
    反射出 ``load_config_simple`` 支持的参数。

    MCDR 2.12 才加入 ``failure_policy``，更早的版本没有；直接用会让插件在声明支持的
    ``>=2.0.0`` 区间里 ImportError/TypeError。这里按签名过滤，老的用默认 regen 行为、
    新的用 raise（配置写坏时明确报错，而不是被默默覆盖成默认值）。
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return None, True
    names = set()
    for name, param in params.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return names, True
        if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            names.add(name)
    return names, False


def _load_config_kwargs(server) -> dict:
    names, any_keyword = _accepted_params(server.load_config_simple)

    def accepts(name: str) -> bool:
        return names is None or any_keyword or name in names

    kwargs = {'file_name': 'config.json', 'target_class': Config}
    if accepts('encoding'):
        kwargs['encoding'] = 'utf-8-sig'  # 兼容 Windows 记事本写出的 BOM
    if accepts('failure_policy'):
        kwargs['failure_policy'] = 'raise'
    return kwargs


def normalize_filename_prefix(prefix: str):
    """
    校验备份包名前缀，返回 ``(前缀, 告警或 None)``。

    前缀会被拼进远端文件名，带 ``/``、``\\`` 就等于把包写进别的目录；以 ``.`` 开头则会
    变成隐藏文件，和 exFAT/macOS 的 ``._*`` 残留混在一起，清理逻辑也会忽略它。这两种
    情况都回退到默认值并告警，而不是让任务在半夜里静默生成一个找不到的包。
    """
    raw = (prefix or '').strip()
    if not raw:
        return DEFAULT_FILENAME_PREFIX, 'filename_prefix 为空，已回退为默认值 {!r}'.format(DEFAULT_FILENAME_PREFIX)
    if '/' in raw or '\\' in raw:
        return DEFAULT_FILENAME_PREFIX, \
            'filename_prefix {!r} 含有路径分隔符，已回退为默认值 {!r}'.format(raw, DEFAULT_FILENAME_PREFIX)
    if raw.startswith('.'):
        return DEFAULT_FILENAME_PREFIX, \
            'filename_prefix {!r} 不能以 . 开头（会与 exFAT/macOS 的 ._* 隐藏文件混淆），已回退为默认值 {!r}'.format(
                raw, DEFAULT_FILENAME_PREFIX)
    return raw, None


def resolve_config(cfg: Config, server) -> ResolvedConfig:
    """把配置对象解析成运行期结构（不抛异常，Cron/时区问题通过字段反馈）。"""
    mcdr_root = get_mcdr_root(server)
    data_folder = server.get_data_folder()

    # ---- PB 仓库与数据库
    raw_dir = (cfg.pb_files_dir or '').strip()
    if raw_dir in ('', './pb_files', 'pb_files'):
        detected = detect_pb_files_dir(mcdr_root)
        pb_files_dir = detected or resolve_path('./pb_files', mcdr_root)
    else:
        pb_files_dir = resolve_path(raw_dir, mcdr_root)
    db_path = find_database(pb_files_dir) or os.path.join(pb_files_dir, 'prime_backup.db')

    # ---- 暂存目录
    temp_dir = resolve_path(cfg.temp_dir, mcdr_root) if (cfg.temp_dir or '').strip() \
        else os.path.join(data_folder, 'tmp')

    # ---- PB 配置文件
    include_pb_config = bool(cfg.include_pb_config)
    pb_config_path: Optional[str] = None
    if include_pb_config:
        pb_config_path = resolve_path(cfg.pb_config_path, mcdr_root) if (cfg.pb_config_path or '').strip() \
            else os.path.join(mcdr_root, PB_CONFIG_RELATIVE)

    # ---- Cron
    cron_expression = (cfg.cron or '').strip()
    cron_spec: Optional[CronSpec] = None
    cron_error: Optional[str] = None
    try:
        cron_spec = CronSpec.parse(cron_expression)
    except CronSyntaxError as e:
        cron_error = str(e)

    # ---- 时区
    timezone_name = (cfg.timezone or '').strip() or 'Asia/Shanghai'
    tz: tzinfo
    tz_error: Optional[str] = None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
    except Exception as e:  # ZoneInfoNotFoundError / ValueError / ImportError
        tz = _local_timezone()
        tz_error = '无法加载时区 {!r}（{}），已退回系统本地时区 {}'.format(
            timezone_name, e, tz)

    # ---- 凭据（环境变量优先）
    username = os.environ.get(ENV_OPENLIST_USERNAME, '').strip() or (cfg.openlist_username or '').strip()
    password = os.environ.get(ENV_OPENLIST_PASSWORD, '') or cfg.openlist_password or ''
    token = os.environ.get(ENV_OPENLIST_TOKEN, '').strip() or (cfg.openlist_token or '').strip()
    url = os.environ.get(ENV_OPENLIST_URL, '').strip() or cfg.openlist_url

    filename_prefix, prefix_warning = normalize_filename_prefix(cfg.filename_prefix)

    return ResolvedConfig(
        raw=cfg,
        mcdr_root=mcdr_root,
        pb_files_dir=pb_files_dir,
        db_path=db_path,
        db_name=os.path.basename(db_path),
        temp_dir=temp_dir,
        include_pb_config=include_pb_config,
        pb_config_path=pb_config_path,
        cron_expression=cron_expression,
        cron_spec=cron_spec,
        cron_error=cron_error,
        timezone_name=timezone_name,
        tz=tz,
        tz_error=tz_error,
        openlist_url=normalize_base_url(url),
        openlist_username=username,
        openlist_password=password,
        openlist_token=token,
        remote_dir=normalize_remote_dir(cfg.remote_dir),
        filename_prefix=filename_prefix,
        max_keep=int(cfg.max_keep),
        min_valid_size_bytes=int(cfg.min_valid_size_bytes),
        as_task=bool(cfg.as_task),
        timeout_sec=max(10, int(cfg.timeout_sec)),
        retry_times=max(0, int(cfg.retry_times)),
        min_free_space_ratio=max(1.0, float(cfg.min_free_space_ratio)),
        prefix_warning=prefix_warning,
    )


def _local_timezone() -> tzinfo:
    from datetime import datetime
    return datetime.now().astimezone().tzinfo
