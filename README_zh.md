# PiwigoDownloader

[English](README.md) | **简体中文**

批量下载 [Piwigo](https://piwigo.org/) 图库站点上的全部照片。公开站点直接开爬；遇到需要账号密码的站点会自动提示输入用户名与密码，登录后再抓。

## 特性

- **官方接口**：全程走 Piwigo Web Service（`/ws.php?format=json`），不依赖 HTML 结构，皮实。
- **按需登录**：匿名能读到内容就直接下载；读不到（或接口返回 401/403）才提示输入账号密码。
- **双通道鉴权**：优先 `pwg.session.login`，失败自动回退 `identification.php` 表单登录，两条路径都会复验会话。
- **原图优先**：使用 `element_url` 取原图，缺失时退到下载入口或最大尺寸衍生图。
- **目录还原**：按 `uppercats` 还原相册层级，图片存入所属最深分类目录。
- **并发 + 续传**：多线程下载（默认 16），`.part` + `Range` 断点续传，重跑自动跳过已下载文件。
- **配置文件**：`piwigo.toml` 统一管理线程数、目录、超时、代理等，命令行可随时覆盖任意一项。
- **增量同步**：`downloads/.piwigo_index.json` 记录已下载图片，重跑同一站点只补新增的那几张。
- **清单**：下载结束写 `manifest.json`，记录每张图的 id、地址、尺寸、落盘路径。
- **安全命名**：净化非法字符、规避 Windows 保留名、同名不同图追加图片 id，绝不互相覆盖。

## 安装

```bash
python -m pip install -r requirements.txt
```

仅依赖 `requests`，Python 3.9+（用 TOML 配置需 3.11+，或用 `.json` 配置）。

## 配置文件

仓库自带 `piwigo.toml`，常用项示意：

```toml
[site]
url = ""                    # 默认站点，留空则命令行给

[download]
output = "downloads"        # 下载根目录
threads = 16                # 并发线程数，想跑多快改这里
per_page = 100              # 接口分页大小
timeout = 30.0              # 单请求超时（秒）
retries = 3                 # 单张失败重试
delay = 0.12                # 接口分页间隔（秒）
max_images = 0              # 0 为不限
overwrite = false
flat = false
progress = true
index = true                # 本地索引，重跑只补新增
index_file = ".piwigo_index.json"

[network]
proxy = ""
insecure = false

[auth]
username = ""
force_login = false
no_login_prompt = false
```

优先级：**命令行 > 配置文件 > 内置默认**。想临时换线程数：`--threads 32`；想彻底不用配置：`--no-config`。

```bash
python piwigo_downloader.py --init-config          # 生成配置模板
python piwigo_downloader.py --config my.toml       # 指定其他配置文件
python piwigo_downloader.py --threads 32           # 临时覆盖线程数
```

## 增量下载

第一次跑完会在下载目录写 `.piwigo_index.json`，记录 `图片 id → 相对路径 + 文件大小`。

- 再次运行同一站点：命中索引的图片**不产生任何请求**，只下载站点新增的图片；日志会打印 `本地索引已有 N 张，待补 M 张`。
- 文件被手动删除或大小不符：索引视为失效，下次自动重下。
- `--check-local` 不联网体检：输出正常/缺失/大小不符的数量，并剔除失效记录。

```bash
python piwigo_downloader.py --check-local
# 17:20:01 INFO    https://gallery.example.com：正常 20431 张，缺失 3 张，大小不符 0 张
# 17:20:01 INFO    索引已更新，失效记录已剔除，下次运行会重新下载这些图片
```

## 用法

公开站点（不需要登录，直接开始）：

```bash
python piwigo_downloader.py https://gallery.example.com/ -o downloads
```

需要登录的站点（脚本会自己提示输入）：

```bash
python piwigo_downloader.py https://example.com/piwigo/ -o downloads
# 输出：该站点需要账号密码才能读取图库内容，准备登录。
# Piwigo 用户名: ______
# Piwigo 密码:   ______
```

也可以显式传参（密码不建议写进命令行历史）：

```bash
python piwigo_downloader.py https://example.com/ -u alice -p 'xxx' --threads 8
# 或者只给用户名，密码交互输入
python piwigo_downloader.py https://example.com/ -u alice --ask-password
# 或者走环境变量
export PIWIGO_PASSWORD='xxx'   # Windows: $env:PIWIGO_PASSWORD="xxx"
python piwigo_downloader.py https://example.com/ -u alice
```

先看看有什么再决定下多少：

```bash
python piwigo_downloader.py https://gallery.example.com/ --list          # 列出分类与张数
python piwigo_downloader.py https://gallery.example.com/ --dry-run       # 只枚举不下载
python piwigo_downloader.py https://gallery.example.com/ --max-images 20 # 只下 20 张试试
python piwigo_downloader.py https://gallery.example.com/ --category 154  # 只下某个分类
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `url` | 站点根地址，可带子路径，如 `https://host/piwigo/`；也可写在配置里 |
| `-c, --config` | 配置文件路径，默认 `piwigo.toml` |
| `--no-config` | 忽略配置文件 |
| `--init-config` | 生成配置模板后退出 |
| `-o, --output` | 下载目录，默认 `downloads` |
| `-u, --username` | 用户名；不给则按需交互询问 |
| `-p, --password` | 密码；优先级低于 `--ask-password` |
| `--ask-password` | 强制交互输入密码 |
| `--force-login` | 匿名可读也先登录，用于纳入私有相册 |
| `--no-login-prompt` | 需要登录时不询问直接退出（适合定时任务） |
| `--threads` | 并发线程数，默认 16（可写在配置里） |
| `--per-page` | 接口分页大小，默认 100 |
| `--max-images` | 最多下载多少张，0 为不限 |
| `--category` | 只下载指定分类 id，可重复 |
| `--flat` | 不建分类子目录，全部平铺 |
| `--dry-run` | 只枚举不下载 |
| `--list` | 只列出分类与张数 |
| `--check-local` | 离线体检本地图片，剔除索引失效记录 |
| `--overwrite` | 已存在文件也重下 |
| `--no-index` | 关闭本地索引，仅按文件是否存在跳过 |
| `--no-progress` | 关闭进度条 |
| `--timeout / --retries / --delay` | 超时、重试、分页间隔 |
| `--proxy` | HTTP(S) 代理 |
| `--insecure` | 跳过 TLS 校验 |
| `-v, --verbose` | 调试日志 |

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 全部成功（含已存在跳过） |
| `1` | 有图片下载失败，或站点/网络异常 |
| `2` | 提供了凭据但登录失败 |
| `3` | 站点需要登录且没拿到可用凭据 |
| `130` | 用户中断 |

## 工作原理

1. `pwg.session.getStatus` 读取站点版本与当前身份。
2. `pwg.categories.getList&recursive=true` 拉全部分类；若匿名得到 0 个分类或 0 张图，则判定需要登录。
3. 需要登录时取凭据：`pwg.session.login` → 失败回退 `identification.php` 表单；都失败则重新提示。
4. `pwg.categories.getImages`（每页 100 张）逐分类遍历，按图片 id 全局去重。
5. 对照本地索引：已有且文件完好的图片直接跳过，不发请求。
6. 其余图片取 `element_url` 原图地址，多线程下载到「分类层级目录/文件名」，`.part` + `Range` 续传，边下边更新索引。
7. 收尾写 `manifest.json`（本次快照）并落盘完整索引。

## 目录结构

```
downloads/
├── .piwigo_index.json      # 本地索引：已下载图片清单（增量同步依据）
├── manifest.json           # 本次运行的清单快照
├── 最近更新/
│   └── 11月第二批/
│       ├── 一之濑明日奈901285表情3.jpg
│       └── 魔女950288B.png
└── 图库搜索方式(建议使用)/
    └── 7c02f556949b25a658eb34ac79a5e0c1.jpg
```

## 测试

```bash
python -m unittest discover -s tests -v
```

41 项离线用例，不访问网络：命名净化、目录树还原、跨分类去重、地址回退、探测判定、登录回退、脏响应解析、Range 续传、同名不覆盖、404 计失败、清单写入、配置优先级、索引读写与按站点隔离、索引命中不联网。

## 说明

- 只下载站点对当前身份可见的内容；私有相册需要对应账号权限。
- 默认分页间隔 0.12s，站点压力大时可调 `--delay 0.5`、`--threads 4`。
- 中断后直接重跑即可续传，已完成文件与索引记录都会跳过。
