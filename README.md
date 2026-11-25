# OpenQuarkBackup

把 PrimeBackup 的备份仓库按计划打包，自动上传到 OpenList 挂载的夸克网盘。

本地盘坏了、机器被水泡了、误删 `pb_files` —— 这些情况下云上还有一份完整的仓库，就能救回来。

## 它做什么

- 按 cron 计划（默认每周一 02:00）把 `pb_files` 打成一个未压缩的 `.tar`
- 数据库用 SQLite 的在线备份 API 导快照，服务端正在跑也能拿到一致的库
- 通过 OpenList 上传到夸克，传完调用 `/api/fs/get` 核对远端文件大小
- 只保留最近 N 份，成功上传后删掉更早的
- 打包与上传全在后台线程，不阻塞服务器主线程

它不负责“备份”这件事本身 —— 存档备份还是 PrimeBackup 的活，本插件只把 PB 的仓库送上云。

## 依赖

- MCDReforged 2.0.0 及以上
- Python 3.9+（用到 `zoneinfo`；Windows 上需要 `pip install tzdata`）
- 一个能访问的 OpenList，夸克网盘已经挂载好
- 已启用的 PrimeBackup（本插件读它的仓库目录与数据库）

## 安装

1. 从 [Releases](../../releases) 下载 `open_quark_backup-v*.mcdr`
2. 丢进 MCDR 的 `plugins/` 目录
3. 执行 `!!MCDR plugin load open_quark_backup`，或者重启 MCDR

也可以直接把本项目目录放进 `plugins/`（MCDR 支持目录形式的插件）。想自己打包成单文件：

```bash
python -m mcdreforged pack -i . -o dist
```

`mcdr pack` 默认读取 `.gitignore` 作为忽略规则，所以 `__pycache__`、`dist` 这些不会被塞进包里。

首次加载会在 `config/open_quark_backup/config.json` 生成默认配置，并自动探测 PrimeBackup 的仓库位置（先读 PB 自己的 `config/prime_backup/config.json` 里的 `storage_root`，找不到再猜 `./pb_files`）。探测结果会写进配置，打开文件就能看到实际值。

## 配置

改完执行 `!!oqb reload` 即生效，不需要重启 MCDR。

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关。关掉后不再按计划触发，`!!oqb backup` 手动备份仍可用 |
| `pb_files_dir` | `"./pb_files"` | PrimeBackup 仓库目录，相对路径以 MCDR 根目录为基准。留 `./pb_files` 表示用自动探测结果 |
| `include_pb_config` | `false` | 是否顺带把 PB 自己的配置文件打进包里 |
| `pb_config_path` | `""` | PB 配置文件的路径，留空用默认位置 `config/prime_backup/config.json` |
| `temp_dir` | `""` | 本地打包暂存目录，留空用插件数据目录下的 `tmp`。包很大时可以指到空间更大的盘 |
| `cron` | `"0 2 * * 1"` | 五段式 cron：分 时 日 月 周。默认每周一凌晨 2 点 |
| `timezone` | `"Asia/Shanghai"` | 解释 cron 和生成包名时间码用的时区，填 IANA 名称 |
| `openlist_url` | `"http://127.0.0.1:5244"` | OpenList 地址，可带反代前缀，末尾带不带 `/` 都行 |
| `openlist_username` | `""` | OpenList 账号 |
| `openlist_password` | `""` | OpenList 密码，日志和 `!!oqb config` 里都会脱敏 |
| `openlist_token` | `""` | 静态 Token，填了就优先用它；留空则用账号密码登录，过期自动重新登录 |
| `remote_dir` | `"/夸克/Minecraft_Backup"` | 远端目录，不存在会自动创建 |
| `filename_prefix` | `"pb_backup_"` | 备份包前缀，完整包名是 `{prefix}YYYYMMDD_HHMMSS.tar` |
| `max_keep` | `4` | 远端最多保留份数。**小于等于 0 表示永不清理，永久保留所有备份** |
| `min_valid_size_bytes` | `8192` | 判定“有效备份包”的最小体积，用来挡掉异常空壳和 exFAT 的 `._*` 文件 |
| `as_task` | `true` | 走 OpenList 的异步上传任务，并轮询到任务真正结束 |
| `timeout_sec` | `1800` | 网络超时（秒）。上传几个 G 的包时要留够 |
| `retry_times` | `2` | 上传失败的重试次数 |
| `min_free_space_ratio` | `1.15` | 暂存盘空间安全系数，需要 `数据体积 × 该系数` 的空闲空间 |

凭据也可以用环境变量覆盖（优先级高于配置文件，适合不把密码写进文件）：
`OQB_OPENLIST_URL`、`OQB_OPENLIST_USERNAME`、`OQB_OPENLIST_PASSWORD`、`OQB_OPENLIST_TOKEN`。

最简配置（夸克挂载在 OpenList 的 `/夸克` 下）：

```json
{
    "openlist_url": "http://127.0.0.1:5244",
    "openlist_username": "admin",
    "openlist_password": "换成你自己的密码",
    "remote_dir": "/夸克/Minecraft_Backup",
    "max_keep": 4,
    "cron": "0 2 * * 1",
    "timezone": "Asia/Shanghai"
}
```

## 时间要和 PrimeBackup 错开

**务必把本插件的 cron 和 PrimeBackup 本体的自动备份时间岔开，建议间隔 30-60 分钟。**

同时读写 SQLite 和磁盘 IO 会互相拖慢，撞在一起轻则两个都变慢，重则 PB 那边报数据库忙。比如：

- PrimeBackup：每天 01:00 备份存档
- OpenQuarkBackup：`0 2 * * 1`，每周一 02:00 把仓库送上云

这样既错开了 IO，又能把刚打好的新存档一并带走。

## Cron 写法

标准五段：`分 时 日 月 周`，周字段 `0` 和 `7` 都表示周日。

| 表达式 | 含义 |
| --- | --- |
| `0 2 * * 1` | 每周一 02:00（默认） |
| `30 3 * * 3,0` | 每周三和周日 03:30 |
| `0 4 * * *` | 每天 04:00 |
| `0 2 1 * *` | 每月 1 号 02:00 |
| `*/30 * * * *` | 每 30 分钟一次（测试用，别长期这么配） |

日字段和周字段同时被指定时按“或”处理（比如 `0 3 13 * 5` 是每月 13 号**或**每周五 03:00），和大多数 cron 实现一致。

表达式写错了不会崩：日志和控制台会报具体原因，定时调度暂停，手动 `!!oqb backup` 照常能用，改对后 `!!oqb reload` 恢复。

## 命令

需要 MCDR 管理员（admin，权限 3）或更高。

| 命令 | 作用 |
| --- | --- |
| `!!oqb` | 显示帮助、当前 cron 和下一次计划时间 |
| `!!oqb backup` | 立刻执行一次打包 + 上传 + 清理 |
| `!!oqb status` | 当前阶段、上次备份的时间/大小/耗时/结果 |
| `!!oqb list` | 刷新并列出远端有效备份包 |
| `!!oqb config` | 打印当前生效配置（密码和 Token 已脱敏） |
| `!!oqb reload` | 重载 config.json，校验并刷新定时计划 |

## 几个说明

**OpenList 还是 AList？** 本插件面向 OpenList 开发，用的是 AList v3 那一套接口（`/api/auth/login`、`/api/fs/put`、`/api/fs/list`……）。AList 理论上直接把地址和账号密码/Token 填进来就能用，但没对各个版本做过逐一适配；个别 AList 环境用不了的话，以 OpenList 为准。

**为什么过滤 `._*` 和 `.DS_Store`？** 外接盘是 exFAT，macOS 每碰到一个文件就可能在旁边放一个 `._xxx` 的 AppleDouble 文件（通常只有 4KB），Windows 也会留 `Thumbs.db`。这些东西对恢复毫无用处，打进包里只是白白变大，还会在清理时干扰“哪些才是真备份”的判断。所以打包和清理都按规则跳过：以 `.` 开头的、`Thumbs.db`、`desktop.ini`、`System Volume Information`，以及 PB 自己的 `temp/`、`*.lock`、`*.db-wal`、`*.db-shm`。

**为什么是未压缩的 tar？** PB 的 `blobs` 和 `packs` 本身就是压缩过的数据，再 gzip/zstd 一遍几乎不省体积，只是把凌晨的 CPU 吃满。真要省流量，可以在挂载侧或 `rclone` 那边做，别在这里二次压缩。

**远端清理为什么不会误删？** 只认同时满足四个条件的文件：不是目录、不以 `.` 开头、名字严格匹配 `{prefix}YYYYMMDD_HHMMSS.tar`、体积大于 `min_valid_size_bytes`。名字匹配但体积过小的（比如 4KB 的 `._pb_backup_xxx.tar`）只会打一条告警，既不占保留名额也不会被删。同目录里其他文件一律不碰。

**`max_keep` 设成 0 会怎样？** 不自动清理，所有备份永久保留。空间自己盯着点。

**OpenList 任务列表里为什么堆着一串「待处理」的上传任务？** `as_task` 打开时，每次上传都会在 OpenList 里生成一个任务记录。OpenList 的 `/api/task/upload/undone` 把 *Pending / Running / WaitingRetry / Errored / Failing / Canceling* 全都算作未完成，所以失败过的任务不但不会自己消失，还会一直显示成「待处理」——重试几次就攒几条。本插件在上传失败或超时后会自动 `cancel` 掉自己那个任务，并在重试前删掉失败记录；但如果你手动杀过 MCDR 进程、或者早先版本留下的任务，需要自己清一次：

- 网页端任务面板里点「清除已完成」，或对单条任务点取消；
- 命令行：`curl -X POST 'http://127.0.0.1:5244/api/task/upload/clear_done' -H 'Authorization: <token>'`（已完成的），未完成的用 `/api/task/upload/delete?tid=<id>`。

任务列表只是记录，不影响文件本身；看到「1 个进行中 + N 个待处理」不代表有 N 个备份在上传。

## 从云端恢复

1. 把网盘上的 `pb_backup_XXXXXXXX_XXXXXX.tar` 下载到本地
2. 停掉服务器与 MCDR
3. 解压到 PrimeBackup 的仓库目录（`tar -xf pb_backup_xxx.tar -C /path/to/pb_files`），覆盖原来的 `prime_backup.db`、`blobs/`、`packs/`
4. 启动 MCDR，`!!pb list` 确认能看到备份，再 `!!pb restore <id>`

保险起见，覆盖前先把现有 `pb_files` 整个目录备份一份。

## 许可

MIT
