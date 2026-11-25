"""
配置层测试：文件名前缀校验、MCDR 版本参数兼容、以及 resolve_config 的回退行为。

这些用例只依赖标准库与插件自身，不需要起假 OpenList。
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from open_quark_backup import config as config_module  # noqa: E402
from open_quark_backup.config import (  # noqa: E402
    Config,
    DEFAULT_FILENAME_PREFIX,
    normalize_filename_prefix,
    resolve_config,
)


class _ServerBase:
    """最小化的 MCDR PluginServerInterface 替身。"""

    def __init__(self, root):
        self.root = root
        self._logger = logging.getLogger('oqb.config.test')
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


class _ModernServer(_ServerBase):
    """模拟 MCDR 2.12+：支持 encoding 与 failure_policy。"""

    def load_config_simple(self, file_name=None, default_config=None, target_class=None,
                           encoding='utf8', failure_policy='regen', **kwargs):
        data = {'__kwargs__': dict(kwargs)}
        raw = {}
        path = os.path.join(self.get_data_folder(), file_name or 'config.json')
        if os.path.isfile(path):
            with io.open(path, encoding=encoding) as f:
                raw = json.load(f)
        cfg = target_class() if target_class is not None else Config()
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        data['encoding'] = encoding
        data['failure_policy'] = failure_policy
        return cfg


class _LegacyServer(_ServerBase):
    """模拟 MCDR 2.0–2.11：没有 failure_policy 参数（传了就 TypeError）。"""

    def __init__(self, root):
        super().__init__(root)
        self.seen_encoding = None

    def load_config_simple(self, file_name=None, default_config=None, target_class=None,
                           encoding='utf8'):
        self.seen_encoding = encoding
        path = os.path.join(self.get_data_folder(), file_name or 'config.json')
        raw = {}
        if os.path.isfile(path):
            with io.open(path, encoding=encoding) as f:
                raw = json.load(f)
        cfg = target_class() if target_class is not None else Config()
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg


class FilenamePrefixTest(unittest.TestCase):
    """前缀会被拼进远端文件名，非法值必须回退而不是让任务静默生成怪名字。"""

    def test_normal_prefix_is_kept(self):
        self.assertEqual(normalize_filename_prefix('pb_backup_'), ('pb_backup_', None))
        self.assertEqual(normalize_filename_prefix('  mc_archive-  '), ('mc_archive-', None))

    def test_prefix_with_slash_falls_back(self):
        for bad in ('a/b', 'a\\b', '/abs', 'dir/'):
            prefix, warning = normalize_filename_prefix(bad)
            self.assertEqual(prefix, DEFAULT_FILENAME_PREFIX, '应回退默认值: {!r}'.format(bad))
            self.assertIsNotNone(warning)
            self.assertIn('回退', warning)

    def test_prefix_with_leading_dot_falls_back(self):
        for bad in ('.pb_', '.', '._hidden'):
            prefix, warning = normalize_filename_prefix(bad)
            self.assertEqual(prefix, DEFAULT_FILENAME_PREFIX)
            self.assertIsNotNone(warning)

    def test_empty_prefix_falls_back(self):
        for bad in ('', '   ', None):
            prefix, warning = normalize_filename_prefix(bad)
            self.assertEqual(prefix, DEFAULT_FILENAME_PREFIX)
            self.assertIsNotNone(warning)


class LoadConfigCompatibilityTest(unittest.TestCase):
    """``failure_policy`` 是 MCDR 2.12+ 的参数；老版本必须不被传进去，否则直接 TypeError。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='oqb_cfg_')
        os.makedirs(os.path.join(self.root, 'config', 'open_quark_backup'), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_config(self, server):
        path = os.path.join(server.get_data_folder(), 'config.json')
        with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'cron': '0 3 * * *', 'filename_prefix': '.bad_'}, f)
        return path

    def test_modern_signature_gets_failure_policy(self):
        server = _ModernServer(self.root)
        kwargs = config_module._load_config_kwargs(server)  # noqa: SLF001
        self.assertEqual(kwargs['encoding'], 'utf-8-sig')
        self.assertEqual(kwargs['failure_policy'], 'raise')
        self.assertIs(kwargs['target_class'], Config)

    def test_legacy_signature_omits_failure_policy(self):
        server = _LegacyServer(self.root)
        kwargs = config_module._load_config_kwargs(server)  # noqa: SLF001
        self.assertNotIn('failure_policy', kwargs, '老版本 MCDR 不接受 failure_policy')
        self.assertEqual(kwargs['encoding'], 'utf-8-sig')

    def test_load_config_works_on_legacy_server(self):
        server = _LegacyServer(self.root)
        self._write_config(server)
        # 老签名上调用不能抛 TypeError —— 这就是 >=2.0.0 兼容性的核心断言
        cfg, resolved = config_module.load_config(server, server.logger)
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.cron, '0 3 * * *')
        self.assertEqual(resolved.filename_prefix, DEFAULT_FILENAME_PREFIX)


class ResolvePrefixTest(unittest.TestCase):
    """resolve_config 必须把回退结果和告警一起放进 ResolvedConfig。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='oqb_resolve_')
        self.server = _ModernServer(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_illegal_prefix_is_normalized_in_resolved_config(self):
        cfg = Config()
        cfg.filename_prefix = 'a/b'
        cfg.temp_dir = os.path.join(self.root, 'tmp')
        resolved = resolve_config(cfg, self.server)
        self.assertEqual(resolved.filename_prefix, DEFAULT_FILENAME_PREFIX)
        self.assertIsNotNone(resolved.prefix_warning)

    def test_legal_prefix_has_no_warning(self):
        cfg = Config()
        cfg.filename_prefix = 'nightly_'
        cfg.temp_dir = os.path.join(self.root, 'tmp')
        resolved = resolve_config(cfg, self.server)
        self.assertEqual(resolved.filename_prefix, 'nightly_')
        self.assertIsNone(resolved.prefix_warning)


if __name__ == '__main__':
    unittest.main(verbosity=2)
