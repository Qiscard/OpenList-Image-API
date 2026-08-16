# OpenList Image API — Code Wiki

> 版本基线：`v1.4.0`（见 [VERSION](file:///e:/Other/Github/OpenList-Image-API/VERSION)）
> 适用仓库：[OpenList-Image-API](file:///e:/Other/Github/OpenList-Image-API)
> 本文档基于仓库实际源码生成，覆盖整体架构、模块职责、关键类与函数、依赖关系与运行方式。

---

## 目录

1. [项目概览](#1-项目概览)
2. [仓库结构](#2-仓库结构)
3. [整体架构](#3-整体架构)
4. [主要模块职责](#4-主要模块职责)
5. [关键类与函数说明](#5-关键类与函数说明)
6. [HTTP 路由与数据流](#6-http-路由与数据流)
7. [前端 WebUI 与浏览页](#7-前端-webui-与浏览页)
8. [配置体系](#8-配置体系)
9. [依赖关系](#9-依赖关系)
10. [安装与运行方式](#10-安装与运行方式)
11. [测试与 CI](#11-测试与-ci)
12. [安全模型](#12-安全模型)
13. [运维手册](#13-运维手册)
14. [发布流程](#14-发布流程)

---

## 1. 项目概览

OpenList Image API 是一个面向 [OpenList](https://github.com/OpenListTeam/OpenList) 的图片浏览与随机图床服务。它把 OpenList 网盘中指定目录里的图片建立为本地索引，再通过一个自带 WebUI、管理 API 和终端 TUI 的 HTTP 服务对外提供浏览、随机图、签名直链解析和附件下载能力。

核心特征：

- **零第三方依赖**：仅使用 Python 标准库（[openlist_image_api.py:1-27](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1-L27)）。
- **安全优先**：OpenList 地址仅允许本机回环；管理令牌与 OpenList token 以独立文件存储；systemd 低权限用户运行。
- **一体化部署**：Bash 安装器 [install.sh](file:///e:/Other/Github/OpenList-Image-API/install.sh) 负责安装、更新、卸载、OpenList 二进制部署与 systemd 单元生成。
- **终端管理**：交互式 TUI [openlist_tui.py](file:///e:/Other/Github/OpenList-Image-API/src/openlist_tui.py) 覆盖日常运维操作。
- **内置 WebUI**：`/` 浏览页与 `/admin` 管理页直接由 Python 返回内嵌 HTML/CSS/JS。

服务默认监听 `0.0.0.0:8790`，OpenList API 地址默认为 `http://127.0.0.1:5244`。

---

## 2. 仓库结构

```text
OpenList-Image-API/
├── .github/
│   └── workflows/
│       └── ci.yml                 # GitHub Actions 验证流水线
├── src/
│   ├── openlist_image_api.py      # 核心 HTTP 服务、索引、缓存、WebUI、管理 API
│   └── openlist_tui.py            # 终端管理界面（TUI）
├── tests/
│   ├── mock_openlist.py           # 本地测试用的 OpenList HTTP 模拟
│   ├── test_core.py               # 核心单元测试（配置、缓存、索引等）
│   └── test_management_features.py # 管理、WebUI、TUI、安装器相关测试
├── .gitattributes
├── .gitignore
├── CODE_WIKI.md                    # 本文档
├── LICENSE                         # MIT
├── README.md                       # 用户文档
├── SHA256SUMS                      # 发布文件校验
├── VERSION                         # 当前版本号
└── install.sh                      # 安装、更新、卸载、OpenList 部署脚本
```

文件体量集中在 [src/openlist_image_api.py](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py)（约 3000 行，含内嵌前端代码）。仓库不包含 `requirements.txt`、`pyproject.toml`、`Dockerfile` 或 `docker-compose.yml`。

---

## 3. 整体架构

### 3.1 分层视图

```text
┌──────────────────────────────────────────────────────────────┐
│                      用户 / 浏览器                           │
│   / 浏览页        /admin 管理页        /api/* JSON 接口      │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP（HTTP/1.1, gzip, TCP_NODELAY）
┌───────────────────────────▼──────────────────────────────────┐
│              ConcurrentHTTPServer (ThreadingHTTPServer)      │
│                     make_handler(application)                │
│   do_GET / do_PUT / do_POST  ·  鉴权 · 维护模式 · 下载代理    │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│                       Application                            │
│  · 配置加载与热重载        · 索引仓库 IndexRepository        │
│  · UrlCache 签名链接缓存   · ThreadPoolExecutor(12) 解析并发 │
│  · 管理配置写入与公告版本   · 后台索引/目录刷新任务           │
└──────┬───────────────────────┬───────────────────────────────┘
       │                       │
┌──────▼─────────────┐  ┌──────▼──────────────────────────────┐
│  IndexRepository   │  │  OpenListClient                      │
│  index.json 读写   │  │  POST /api/fs/list  POST /api/fs/get │
│  原子写 + 锁       │  │  重试 3 次 · 读取本地 token 文件     │
└────────────────────┘  └──────────────┬───────────────────────┘
                                       │ 本机 HTTP
                                ┌──────▼──────┐
                                │   OpenList  │
                                │ 127.0.0.1   │
                                └─────────────┘
```

### 3.2 运行模式

- 服务进程：`python3 openlist_image_api.py --config config.json serve`，由 systemd 以 `openlist-image` 用户启动（[install.sh:148-173](file:///e:/Other/Github/OpenList-Image-API/install.sh#L148-L173)）。
- 索引重建：`refresh` 子命令同步重建；TUI 通过 `runuser` 在后台执行（[openlist_tui.py:348-389](file:///e:/Other/Github/OpenList-Image-API/src/openlist_tui.py#L348-L389)）。
- 管理令牌：`create-admin-token` 子命令生成并写入 `admin.token`（[openlist_image_api.py:1805-1809](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1805-L1809)）。

### 3.3 数据持久化

| 数据 | 位置 | 说明 |
|---|---|---|
| 配置 | `/etc/openlist-image-api/config.json` | JSON，原子写，`0600` |
| OpenList token | `/etc/openlist-image-api/openlist.token` | 纯文本，`0600` |
| 管理令牌 | `/etc/openlist-image-api/admin.token` | `secrets.token_urlsafe(32)`，`0600` |
| 图片/目录索引 | `/var/lib/openlist-image-api/index.json` | 原子写，含构建耗时与错误 |
| 重建日志 | `/var/lib/openlist-image-api/rebuild.log` | 后台 `refresh` 输出 |
| 重建 PID | `/run/openlist-image-api/rebuild.pid` | 防止重复后台重建 |

---

## 4. 主要模块职责

### 4.1 `src/openlist_image_api.py`

单一文件承担服务端全部职责，可按职能切分为七层：

| 层 | 代表符号 | 职责 |
|---|---|---|
| 配置 | `DEFAULT_CONFIG`、`validate_config`、`load_config` | 默认值、校验、加载与热重载 |
| 安全工具 | `normalize_directory`、`is_loopback_openlist_url`、`read_secret`、`write_secret`、`atomic_write_json`、`admin_token_from_headers` | 路径规范化、本机地址校验、秘密读写、令牌提取 |
| OpenList 客户端 | `OpenListClient` | 分页列目录、解析文件签名直链、删除文件、3 次重试 |
| 索引 | `IndexRepository`、`build_index`、`build_directory_index`、`join_virtual_path` | 索引读写、BFS 遍历、目录索引独立构建 |
| 缓存与并发 | `UrlCache` | LRU + TTL 签名链接缓存、按路径哈希分片合并并发请求 |
| 标签 | `TagRepository`、`Application.voter_id`、`Application.TRASH_TAG` | 点赞/踩、分类标签、垃圾桶标记与删除 |
| 应用核心 | `Application` | 配置持有、状态聚合、管理配置更新、备份恢复、后台刷新、日志级别 |
| HTTP 层 | `make_handler`、`ConcurrentHTTPServer`、`command_serve`、`command_refresh`、`command_create_admin_token`、`main` | 路由分发、响应、子命令入口 |

此外还内嵌了 `gallery_html()` 与 `admin_html()` 两个返回完整前端页面的函数。

### 4.2 `src/openlist_tui.py`

交互式终端管理器，依赖 `openlist_image_api` 暴露的 `atomic_write_json`、`load_config`、`write_secret`（[openlist_tui.py:17](file:///e:/Other/Github/OpenList-Image-API/src/openlist_tui.py#L17)）。职责：

- 读写 `config.json` 与 token 文件并修正属主为 `openlist-image`。
- 包装 `install.sh` 完成 OpenList 安装、更新、卸载。
- 通过 `systemctl` 管理服务、检测端口占用、清理旧版 `openlist-random-image` 残留。
- 以 `runuser` 启动后台索引重建并写入 PID。
- 显示状态与管理令牌（`--print-admin-token`）。

### 4.3 `install.sh`

Bash 安装器，采用 `set -Eeuo pipefail`，职能：

- 从 GitHub/Gitee 拉取固定版本文件并执行 SHA-256 校验。
- 创建服务用户、配置目录、状态目录、systemd 单元与全局命令。
- 内置 OpenList 二进制安装（非 Docker）、架构识别与镜像测速。
- 执行更新（从 `main` 分支）与卸载（`api` / `complete`）。
- 迁移旧配置：`listen_host` 从 `127.0.0.1` 升级为 `0.0.0.0`，性能默认值迁移（[install.sh:98-137](file:///e:/Other/Github/OpenList-Image-API/install.sh#L98-L137)）。

### 4.4 `.github/workflows/ci.yml`

CI 在 `push`/`pull_request` 触发，依次执行：`py_compile`、`unittest discover`、`bash -n install.sh`、`sha256sum --check`、以及 `install.sh` 必须为 LF 行尾的检查。

### 4.5 `tests/`

使用标准库 `unittest`，向 `sys.path` 注入 `src` 后导入被测模块。`mock_openlist.py` 提供本地测试用的 OpenList HTTP 模拟（`/api/fs/get` 返回 `thumb`）。`test_core.py` 覆盖配置、缓存并发、索引、状态、备份、批量换链；`test_management_features.py` 覆盖管理配置、公告版本、WebUI 结构（瀑布流最矮列、视口优先出图、批量换链）、下载响应、TUI 与安装器约束（含禁止 Docker 管理）。

---

## 5. 关键类与函数说明

### 5.1 配置层

#### `DEFAULT_CONFIG` 与常量

[openlist_image_api.py:29-62](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L29-L62)

内置默认配置字典，并定义 `ALLOWED_LAYOUTS`、`ALLOWED_DELIVERY`、`ALLOWED_CAPTION_MODES`、`MAX_REQUEST_BODY`（64 KiB）、`URL_RESOLVE_WORKERS`（12）与 `DEVICE_PREFERENCE_DEFAULTS`。

#### `normalize_directory(value)` / `normalize_directories(values)`

[openlist_image_api.py:65-82](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L65-L82)

将虚拟目录规范化为以 `/` 开头、无空段、无 `.`/`..` 的形式，并对列表去重。是所有路径参数的统一入口。

#### `is_loopback_openlist_url(value)`

[openlist_image_api.py:85-97](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L85-L97)

强制 OpenList API URL 必须是 `http` 协议、主机为 `127.0.0.1`/`localhost`/`::1`、带端口、无用户名密码、无路径。该函数是 token 不外泄的第一道防线。

#### `validate_config(candidate)`

[openlist_image_api.py:100-152](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L100-L152)

将 `DEFAULT_CONFIG` 与传入字典合并后逐项校验：端口范围、目录规范、布局/投递/文字模式枚举、公告长度与秒数、维护模式布尔、网格参数、缓存参数、扩展名非空且以 `.` 开头、敏感路径必须绝对路径。失败抛 `ValueError`。

#### `load_config(config_path)` / `atomic_write_json` / `read_secret` / `write_secret`

[openlist_image_api.py:155-190](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L155-L190)

- `load_config`：文件不存在时返回默认配置；存在则解析 JSON 并校验。
- `atomic_write_json`：先写 `.tmp` 再 `os.replace`，权限 `0600`，避免读写竞争。
- `read_secret`/`write_secret`：读取/写入秘密文件，写时 `0600`，读时空值抛错。

#### `admin_token_from_headers(headers)`

[openlist_image_api.py:193-195](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L193-L195)

兼容 `X-OpenList-Admin-Token` 与旧版 `X-Admin-Token`，返回字符串或 `None`。

### 5.2 OpenList 客户端

#### `class OpenListClient`

[openlist_image_api.py:198-253](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L198-L253)

- `__init__`：保存 `base_url` 与 `token_path`。
- `_post(endpoint, payload)`：构造 `Request`，`Authorization` 直接使用 token 文件内容，`Content-Type: application/json`；最多重试 3 次，退避 `1/2` 秒；校验返回 `code == 200` 且 `data` 为字典，否则抛 `RuntimeError`。
- `list_directory(path)`：调用 `/api/fs/list` 分页（`per_page=1000`）汇总目录条目，直到累计数达到 `total`。
- `resolve_file(path)`：调用 `/api/fs/get`，优先取 `raw_url` 再取 `url`，并校验协议与 `netloc`。

### 5.3 索引层

#### `class IndexRepository`

[openlist_image_api.py:256-275](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L256-L275)

- `path = state_dir / "index.json"`，内部 `threading.Lock` 保护。
- `load()`：不存在返回空索引骨架；存在则解析，要求为字典且 `images` 为列表。
- `save(data)`：通过 `atomic_write_json` 原子写入。

#### `build_index(config, repository)`

[openlist_image_api.py:282-333](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L282-L333)

使用 `deque` BFS 遍历配置目录：对每个目录调用 `list_directory`，分类子目录与图片（按扩展名过滤、记录 `size`），构建 `directory_index`，记录错误，最后写入 `version=2`、耗时、计数等元数据并 `repository.save`。

#### `build_directory_index(config)`

[openlist_image_api.py:336-367](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L336-L367)

只重建目录索引（不解析图片 URL），结果通过 `IndexRepository.save` 合并回 `index.json`，供管理页目录浏览器使用。

### 5.4 缓存与并发

#### `class UrlCache`

[openlist_image_api.py:370-422](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L370-L422)

- `OrderedDict` 作为 LRU 容器，`max_size`/`ttl_seconds` 来自配置。
- `_cached_url(path)`：命中且未过期则 `move_to_end` 并返回；过期则弹出。
- `resolve(path, client, refresh=False)`：
  - `refresh=True` 先 `invalidate`。
  - 否则先查缓存，命中直接返回。
  - 使用 **64 把分片锁** `self._resolve_locks[hash(path) % 64]` 合并同路径并发未命中，进入临界区后再次查缓存（double-check），未命中则 `client.resolve_file` 并写入缓存，超容量时从头部淘汰。
- `status()`：返回 `{size, hits, misses}`，用于 `/api/status`。
- 命中/未命中计数无锁更新（仅用于统计），存在轻微竞态但不影响正确性。

### 5.5 应用核心

#### `class Application`

[openlist_image_api.py:425-672](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L425-L672)

| 方法 | 行号 | 职责 |
|---|---|---|
| `__init__` | 426-435 | 加载配置、创建 `IndexRepository`、`UrlCache`、`ThreadPoolExecutor(12)`、刷新锁 |
| `reload_config` | 437-447 | 重新加载配置；若缓存相关键变化则重建 `UrlCache`（否则保留） |
| `visitor_config` / `public_config` | 449-465 | 对外公开配置：设备默认值、文字、目录展示、公告、维护状态 |
| `admin_config` | 467-479 | 管理页可编辑字段视图 |
| `update_admin_config(payload)` | 481-504 | 仅允许白名单字段；公告字段变化时 `announcement_version += 1`；校验后原子写并重载 |
| `create_config_backup` | 506-530 | 生成 ZIP 备份，`schema_version=3`，**不含** token 文件路径与 token 内容 |
| `restore_config_backup(body)` | 532-560 | 校验 ZIP 结构与大小，按白名单过滤后委托 `update_admin_config` |
| `is_admin(supplied_token)` | 562-566 | `hmac.compare_digest` 常量时间比较管理令牌 |
| `start_refresh` | 568-585 | 非阻塞获取 `refresh_lock`，成功则起守护线程执行 `build_index` |
| `start_directory_refresh` | 587-606 | 同上，执行 `build_directory_index` 并合并回索引 |
| `status` | 608-624 | 聚合索引计数、构建耗时、刷新状态、最近错误、缓存状态与公共配置 |
| `list_directories(path)` | 626-637 | 从 `directory_index` 返回子目录；根目录返回配置的顶层目录 |
| `choose_images(count, folder, min_size, max_size)` | 639-655 | 按目录前缀与大小过滤后随机抽样，上限 50，不足则 `random.choice` 回填 |
| `resolve_images(images, refresh=False)` | 664-672 | 通过 `url_executor.map` 并发解析签名链接，附加 `size` 与 `url` |
| `indexed_image(path)` | 657-662 | 校验图片是否在索引中，不在则抛 `ValueError` |

### 5.6 HTTP 工具与响应

#### 响应辅助

[openlist_image_api.py:675-700](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L675-L700)

- `parse_size(value)`：支持 `b/k/m/g` 单位解析为字节整数。
- `json_bytes(payload)`：紧凑 JSON 编码。
- `attachment_disposition(filename)`：生成 `attachment` 头，ASCII 文件名 + UTF-8 `filename*`。

#### `make_handler(application)` → `Handler`

[openlist_image_api.py:1574-1780](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1574-L1780)

闭包捕获 `application`，返回 `BaseHTTPRequestHandler` 子类：

- `server_version = "OpenListImageAPI/1.4"`，`protocol_version = "HTTP/1.1"`。
- `setup()`：设置 `TCP_NODELAY`。
- `_send_body`：根据 `Accept-Encoding` 对 ≥1024 字节内容 gzip 压缩（level 5），写 `Content-Length`、`Cache-Control`、`Vary`。
- `_send_json` / `_send_html` / `_send_attachment`：分别封装 JSON、HTML、ZIP 附件响应。
- `_proxy_download(image)`：向 OpenList 取原图，按 64 KiB 分块流式回写；有 `Content-Length` 则透传，否则 `Connection: close`；捕获 `BrokenPipeError`/`ConnectionResetError`。
- `_admin_required()`：校验管理令牌，失败返回 401。
- `_maintenance_access_required()`：维护模式下未带有效令牌返回 503。
- `do_GET` / `do_PUT` / `do_POST`：路由分发（见第 6 节）。
- `log_message`：通过 `logging.info` 记录访问日志。

#### `ConcurrentHTTPServer`

[openlist_image_api.py:1783-1786](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1783-L1786)

继承 `ThreadingHTTPServer`，`daemon_threads=True`、`request_queue_size=128`、`allow_reuse_address=True`。

#### 子命令入口

[openlist_image_api.py:1789-1827](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1789-L1827)

- `command_serve(config_path)`：构造 `Application` 与 `ConcurrentHTTPServer`，`serve_forever`。
- `command_refresh(config_path)`：同步 `build_index` 并打印 JSON 摘要。
- `command_create_admin_token(config_path)`：生成 `secrets.token_urlsafe(32)` 并写秘密文件。
- `main()`：`argparse` 解析 `--config` 与 `serve`/`refresh`/`create-admin-token` 子命令。

### 5.7 前端页面生成

#### `gallery_html()`

[openlist_image_api.py:703-1569](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L703-L1569)

返回浏览页完整 HTML：包含暗色主题 CSS、幻灯片/瀑布流布局、大图灯箱（缩放/旋转/拖拽/捏合）、公告弹窗、维护模式、显示设置面板，以及大量原生 JS（`fetchJsonWithRetry`、`refreshImageUrl`、`ensureFreshImage`、`openLightbox`、`setupWaterfallColumns`、`scheduleSlideshow`、`renderSlideHistory` 等）。偏好仅写入 `localStorage`（`PREFERENCE_KEY`），公告状态按 `version` 隔离（`ANNOUNCEMENT_KEY_PREFIX`）。

#### `admin_html()`

[openlist_image_api.py:1488-1571](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1488-L1571)

返回管理页 HTML：令牌输入、目录多选浏览器、图片文字/目录展示/公告/维护模式表单、目录缓存刷新、索引重建轮询、配置备份与恢复。所有管理请求附带 `X-OpenList-Admin-Token`。

### 5.8 TUI 关键函数

[openlist_tui.py](file:///e:/Other/Github/OpenList-Image-API/src/openlist_tui.py)

| 函数 | 行号 | 职责 |
|---|---|---|
| `require_root` | 42-44 | 校验 `EUID == 0` |
| `run` / `command_output` | 55-66 | 包装 `subprocess.run` |
| `service_action(action)` | 69-71 | `systemctl <action> openlist-image-api` |
| `read_config` / `write_config` | 74-85 | 读写配置并 `chown` 给服务用户 |
| `set_openlist_token` | 87-94 | `getpass` 不回显读取并写秘密文件 |
| `install_openlist` | 97-109 | 选择下载方式后调用 `install.sh --install-openlist` |
| `update_application` / `uninstall_application` | 112-136 | 调用 `install.sh --update` / `--uninstall` |
| `configure_port` | 162-178 | 修改端口前用 `ss`/`lsof` 检测占用 |
| `configure_listen_host` | 181-194 | 切换 `0.0.0.0` / `127.0.0.1` |
| `listeners_on_port` | 223-231 | 优先 `ss`，回退 `lsof` |
| `legacy_nginx_artifacts` / `detect_legacy_residuals` / `cleanup_legacy_residuals` | 234-299 | 检测并清理旧版 `openlist-random-image` 残留与 Nginx 配置 |
| `cleanup_residuals_and_runtime_cache` | 302-309 | 清理残留并重启服务以清空运行时缓存 |
| `rebuild_index` | 348-389 | 用 `runuser` 后台执行 `refresh`，写 PID 与日志，基于上次耗时给出预估 |
| `print_admin_token` / `ensure_admin_token` | 392-401 | 显示或在缺失时自动创建管理令牌 |
| `main_menu` / `main` | 404-448 | 主菜单循环与 `--print-admin-token` 入口 |

---

## 6. HTTP 路由与数据流

### 6.1 路由表

| 方法 | 路径 | 鉴权 | 处理 |
|---|---|---|---|
| GET | `/`、`/gallery` | 维护模式 | 返回浏览页 HTML |
| GET | `/admin` | — | 返回管理页 HTML |
| GET | `/health` | — | `{"status":"ok"}` |
| GET | `/api/status` | — | 聚合状态 |
| GET | `/api/public-config` | — | 访客可见配置（含标签、主题、筛选开关） |
| GET | `/api/images/random` | 维护模式 | 随机图片 JSON（含签名 URL 与标签），支持 `tag`、`min_likes`、`min_ratio`、`sort` 筛选 |
| POST | `/api/download-url` | 维护模式 | 批量解析签名直链，`fresh=1` 强制刷新 |
| GET | `/download?path=…` | 维护模式 | 流式代理原图附件 |
| GET | `/random` | 维护模式 | 302 跳转到随机图签名直链 |
| GET | `/api/tagging/stats?paths=…` | — | 批量查询图片标签统计 |
| GET | `/api/tagging/categories` | — | 所有分类标签及计数 |
| POST | `/api/tagging/vote` | — | 提交点赞/踩或分类投票 |
| GET | `/api/admin/config` | 管理令牌 | 读取可编辑服务器配置 |
| GET | `/api/admin/backup` | 管理令牌 | 下载配置 ZIP |
| GET | `/api/admin/directories?path=…` | 管理令牌 | 列出目录缓存子目录 |
| GET | `/api/admin/logs?lines=…` | 管理令牌 | 读取最近 journalctl 服务日志 |
| GET | `/api/admin/tagging/trash` | 管理令牌 | 查看垃圾桶标签下的图片 |
| PUT | `/api/admin/config` | 管理令牌 | 保存全局服务器配置 |
| POST | `/api/admin/rebuild` | 管理令牌 | 启动后台索引重建 |
| POST | `/api/admin/directories/refresh` | 管理令牌 | 后台刷新目录缓存，`path=` 时只重建该目录 |
| POST | `/api/admin/backup` | 管理令牌 | 上传 ZIP 恢复配置 |
| POST | `/api/admin/tagging/reset?path=…` | 管理令牌 | 重置指定图片或全部标签数据 |
| POST | `/api/admin/tagging/trash/delete` | 管理令牌 | 删除选中的垃圾桶图片 |

错误处理：`ValueError`/`RuntimeError` 返回 400，其他异常返回 500 并 `logging.exception`。

### 6.2 典型数据流：随机图浏览

```text
浏览器 GET /api/images/random?count=15
  → Handler._maintenance_access_required（维护模式校验）
  → Application.choose_images（索引过滤 + random.sample）
  → Application.resolve_images
      → UrlCache.resolve（命中直接返回）
      → 未命中 → OpenListClient.resolve_file → /api/fs/get
      → 写入 LRU 缓存
  → 20 线程 ThreadPoolExecutor.map 并发
  → 返回 [{path, size, url}, ...]
浏览器 <img src=url> 直接访问 OpenList 签名直链
```

### 6.3 典型数据流：附件下载

```text
浏览器 GET /download?path=/a/b.jpg
  → Application.indexed_image（校验索引）
  → Application.resolve_images([image])[0].url
  → urlopen(upstream, timeout=60)
  → 64 KiB 分块写回，带 attachment 头
  → 客户端断开时捕获 BrokenPipeError/ConnectionResetError
```

### 6.4 索引重建流程

```text
POST /api/admin/rebuild（或 TUI 菜单 5）
  → Application.start_refresh（非阻塞 refresh_lock）
  → 守护线程 build_index
      → OpenListClient.list_directory 分页 BFS
      → 分类图片/目录，记录错误
      → IndexRepository.save（原子写）
  → /api/status.refreshing 反映进行中
```

---

## 7. 前端 WebUI 与浏览页

### 7.1 浏览页 `/`

- **布局**：幻灯片（`single`）与瀑布流（`waterfall`），桌面端瀑布流 3 列，宽高比 ≥1.45 的图片跨 2 列，平板/手机降为 2/1 列；新卡进入当前最矮列以保持列高接近，按视口优先级出图，避免重排旧图。
- **幻灯片**：首批加载 6 张并预加载后续 3 张，保留最近播放历史（上限 60），支持自动播放间隔（0 关闭），页面隐藏/灯箱打开/设置面板打开时暂停。
- **批量换链与预热**：当前批次通过 `POST /api/download-url` 批量解析签名直链，空闲时预热下一批；同路径请求合并，未解析的图片会在后台补齐。
- **大图灯箱**：滚轮/双指缩放（0.5–4 倍）、双指捏合、左右旋转、双击复位、拖拽平移；URL 失效自动 `refreshImageUrl`，页面切回前台且空闲超 5 分钟自动恢复。
- **标签与投票**：启用后每张图片下方显示点赞/踩按钮与分类标签，浏览页顶部标签栏可按分类筛选；内置“垃圾桶”标签用于标记待删除图片。
- **公告**：Markdown 渲染（含 `<font color>`、代码块、标题、链接），强制阅读秒数倒计时，`localStorage` 按 `version` 记录“本次关闭”/“不再显示”。
- **维护模式**：显示维护提示，管理员在页面内输入令牌后当前会话可浏览。
- **偏好隔离**：视图、间距、自动播放间隔、标签显示、大图目录开关存 `localStorage`，按访问来源隔离；图片文字、目录展示、主题、公告、维护模式、标签与日志为全局服务器配置。

### 7.2 管理页 `/admin`

- 输入管理令牌后 `GET /api/admin/config` 加载配置。
- 目录多选：`GET /api/admin/directories` 浏览缓存，单击进入、双击添加。
- 保存：`PUT /api/admin/config`；公告字段变化自动递增版本。
- 目录缓存刷新：`POST /api/admin/directories/refresh`，`path=` 时只重建该目录，随后轮询 `/api/status`。
- 索引重建：`POST /api/admin/rebuild`，基于 `last_build_duration_seconds` 给出预估并轮询。
- 标签配置：启用开关、投票范围、分类标签编辑、筛选开关；可重置单图或全部标签数据。
- 垃圾桶：`GET /api/admin/tagging/trash` 查看待删除图片，`POST /api/admin/tagging/trash/delete` 删除选中图片。
- 日志查看：`GET /api/admin/logs?lines=…` 读取最近 journalctl 服务日志。
- 主题：暗色/浅色，管理页右上角可切换并记忆到 `localStorage`。
- 备份/恢复：`GET`/`POST /api/admin/backup`，ZIP 不含 token。

---

## 8. 配置体系

### 8.1 配置文件

默认 `/etc/openlist-image-api/config.json`，可通过 `--config` 覆盖。安装器生成的初始配置见 [install.sh:75-96](file:///e:/Other/Github/OpenList-Image-API/install.sh#L75-L96)；应用内置默认值见 [openlist_image_api.py:29-53](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L29-L53)。`load_config` 会以内置默认值覆盖文件缺失字段。

### 8.2 配置项一览

| 配置项 | 默认值 | 约束 |
|---|---|---|
| `listen_host` | `0.0.0.0` | `0.0.0.0` 或 `127.0.0.1` |
| `listen_port` | `8790` | 整数 1024–65535 |
| `openlist_api_url` | `http://127.0.0.1:5244` | 本机回环 HTTP，带端口 |
| `openlist_token_file` | `/etc/openlist-image-api/openlist.token` | 绝对路径 |
| `state_dir` | `/var/lib/openlist-image-api` | 绝对路径 |
| `directories` | `[]` | 虚拟目录数组，去重，禁 `..` |
| `extensions` | 7 种图片扩展名 | 非空，元素以 `.` 开头 |
| `view_layout` | `single` | `single`/`grid`/`waterfall` |
| `delivery` | `preview` | `preview`/`download` |
| `caption_mode` | `path` | `path`/`name`/`hidden` |
| `directory_display_enabled` | `true` | 布尔 |
| `directory_display_depth` | `0` | 0–64 |
| `announcement_enabled` | `false` | 布尔 |
| `announcement_title` | `网站公告` | ≤120 字符 |
| `announcement_content` | `""` | ≤4000 字符 |
| `announcement_required_seconds` | `0` | 0–3600 |
| `announcement_version` | `0` | 非负整数，变更自动递增 |
| `maintenance_enabled` | `false` | 布尔 |
| `theme` | `dark` | `light`/`dark` |
| `grid_gap` | `12` | 0–48 |
| `grid_scale` | `150` | 75–200 |
| `url_cache_size` | `0` | 0–5000，0 表示不缓存只合并 |
| `url_cache_ttl_seconds` | `1800` | 0–3600 |
| `tagging_enabled` | `false` | 布尔，总开关 |
| `tagging_scope` | `anonymous` | `disabled`/`anonymous`/`token` |
| `tagging_categories` | `[]` | 字符串数组，最多 32 项 |
| `tagging_allow_custom` | `false` | 布尔 |
| `tagging_sort_default` | `likes` | `likes`/`dislikes`/`ratio` |
| `filter_enabled` | `true` | 布尔，是否显示标签筛选栏 |
| `log_level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `admin_token_file` | `/etc/openlist-image-api/admin.token` | 绝对路径 |

### 8.3 WebUI 可编辑字段

仅以下字段可通过 `PUT /api/admin/config` 修改：`directories`、`caption_mode`、`directory_display_enabled`、`directory_display_depth`、`theme`、`announcement_*`、`maintenance_enabled`、`tagging_enabled`、`tagging_scope`、`tagging_categories`、`tagging_allow_custom`、`tagging_sort_default`、`filter_enabled`、`log_level`（[openlist_image_api.py:481-504](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L481-L504)）。`openlist_api_url`、token 文件路径、扩展名等受保护字段不能通过 WebUI 修改。

### 8.4 环境变量

**应用不读取任何环境变量**，配置完全来自 JSON 文件、`--config` 参数与两个令牌文件。`install.sh` 中的 `APP_DIR`、`SOURCE` 等是脚本内部 Shell 变量。

### 8.5 配置迁移

升级时 `install.sh` 自动执行：

- `migrate_listen_host`：旧 `127.0.0.1` → `0.0.0.0`（[install.sh:98-112](file:///e:/Other/Github/OpenList-Image-API/install.sh#L98-L112)）。
- `migrate_performance_defaults`：`grid_scale` 125→150、`url_cache_size` 200→0（默认只合并不缓存）、`url_cache_ttl_seconds` 240→1800，仅迁移仍为旧默认值的项（[install.sh:114-137](file:///e:/Other/Github/OpenList-Image-API/install.sh#L114-L137)）。

---

## 9. 依赖关系

### 9.1 Python 依赖

无第三方依赖。导入均为标准库：`argparse`、`gzip`、`hashlib`、`hmac`、`io`、`json`、`logging`、`os`、`random`、`secrets`、`socket`、`subprocess`、`threading`、`time`、`zipfile`、`collections`、`concurrent.futures`、`http`、`pathlib`、`typing`、`urllib`（[openlist_image_api.py:1-27](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py#L1-L27)）。TUI 额外使用 `getpass`、`shutil`、`sys`、`urllib.request`。

### 9.2 模块内依赖

```text
openlist_tui.py
  └── openlist_image_api.atomic_write_json / load_config / write_secret

openlist_image_api.py
  ├── OpenListClient ── read_secret
  ├── IndexRepository ── atomic_write_json
  ├── UrlCache ── OpenListClient
  └── Application ── IndexRepository, UrlCache, OpenListClient, ThreadPoolExecutor
```

### 9.3 系统命令依赖

- Image API 安装：`curl`、`python3`、`sha256sum`、`systemctl`。
- 后台重建：`runuser`。
- 端口检测：`ss`（优先）或 `lsof`（回退）。
- OpenList 安装：`find`、`tar`、`uname`、`awk`、`head`、`mktemp`、`install`、`useradd`。

### 9.4 外部服务依赖

- OpenList 必须以本机 HTTP 服务运行（默认 `127.0.0.1:5244`）。
- 安装/更新时需要访问 GitHub 或 Gitee。

### 9.5 平台要求

- Linux + systemd + root/sudo。
- CI 验证版本：Python 3.11（源码使用 `str | None` 语法，至少需 Python 3.10）。
- OpenList 支持架构：`amd64`、`arm64`、`arm-7`、`arm-6`、`386`。

### 9.6 不提供的依赖

无 Docker、无数据库、无 Redis、无 Nginx 模板、无 TLS 自动签发、无定时任务、无 Prometheus 指标。

---

## 10. 安装与运行方式

### 10.1 一键安装（固定版本）

GitHub：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiscard/OpenList-Image-API/v1.4.0/install.sh | sudo bash -s -- --source github
```

Gitee：

```bash
curl -fsSL https://gitee.com/qiscard/OpenList-Image-API/raw/v1.4.0/install.sh | sudo bash -s -- --source gitee
```

`--source auto` 优先 Gitee，失败回退 GitHub。安装器会校验 `SHA256SUMS`。

### 10.2 安装器参数

```text
--source github|gitee|auto
--install-openlist
--openlist-download direct|auto
--update
--uninstall api|complete
```

### 10.3 安装产物

```text
/opt/openlist-image-api/{install.sh, openlist_image_api.py, openlist_tui.py, VERSION}
/etc/openlist-image-api/{config.json, admin.token, openlist.token}
/var/lib/openlist-image-api/
/etc/systemd/system/openlist-image-api.service
/usr/local/bin/openlist-image-api
```

服务用户：`openlist-image`。

### 10.4 服务管理

systemd 执行：

```bash
/usr/bin/python3 /opt/openlist-image-api/openlist_image_api.py \
  --config /etc/openlist-image-api/config.json serve
```

常用命令：

```bash
sudo systemctl start openlist-image-api
sudo systemctl stop openlist-image-api
sudo systemctl restart openlist-image-api
sudo systemctl status openlist-image-api
```

安装时自动 `systemctl enable --now`。

### 10.5 TUI

```bash
sudo openlist-image-api
```

菜单：1 安装 OpenList、2 设置 token、3 设置端口、4 服务管理、5 后台重建索引、6 查看状态与管理令牌、7 维护（更新/卸载/清理）。

### 10.6 源码直接运行

```bash
python3 src/openlist_image_api.py --config /etc/openlist-image-api/config.json serve
python3 src/openlist_image_api.py --config /etc/openlist-image-api/config.json refresh
python3 src/openlist_image_api.py --config /etc/openlist-image-api/config.json create-admin-token
```

`refresh` 输出：

```json
{"image_count": 0, "directory_count": 0, "errors": []}
```

### 10.7 OpenList 安装

```bash
sudo bash /opt/openlist-image-api/install.sh --install-openlist --openlist-download direct
sudo bash /opt/openlist-image-api/install.sh --install-openlist --openlist-download auto
```

二进制安装到 `/opt/openlist/openlist`，服务名 `openlist.service`。

### 10.8 更新与卸载

```bash
sudo bash /opt/openlist-image-api/install.sh --update
sudo bash /opt/openlist-image-api/install.sh --uninstall api
sudo bash /opt/openlist-image-api/install.sh --uninstall complete
```

更新从 `main` 分支拉取，并恢复服务原有启用/运行状态。

### 10.9 部署后首次配置流程

1. 安装 OpenList（TUI 菜单 1）。
2. 在 OpenList 初始化完成后，保存 token（菜单 2）。
3. 在 `/admin` 输入管理令牌并选择图片目录。
4. 执行后台重建索引（菜单 5 或 `/admin`）。

---

## 11. 测试与 CI

### 11.1 本地验证

```bash
python3 -m py_compile src/openlist_image_api.py src/openlist_tui.py
python3 -m unittest discover -s tests -v
bash -n install.sh
sha256sum --check SHA256SUMS
! grep -q $'\r' install.sh
```

### 11.2 CI

[.github/workflows/ci.yml](file:///e:/Other/Github/OpenList-Image-API/.github/workflows/ci.yml) 在 `push`/`pull_request` 触发，`ubuntu-latest` + Python 3.11，执行上述全部 5 项检查。

### 11.3 测试覆盖范围

- 配置规范化、校验、本机地址校验。
- 管理令牌请求头兼容。
- `UrlCache` 并发未命中合并、并行解析、重载时缓存保留。
- `IndexRepository` 读写与耗时状态。
- 配置备份排除敏感字段。
- 管理配置字段限制与公告版本递增。
- WebUI 关键结构（瀑布流最矮列、视口优先出图、批量换链）。
- 下载接口流式附件响应。
- 批量换链接口并发解析与缺失路径处理。
- TUI 状态、更新、卸载、服务管理。
- 安装器固定代理列表与禁止 Docker 管理。

---

## 12. 安全模型

### 12.1 身份与令牌

- 管理令牌：`secrets.token_urlsafe(32)`，存 `admin.token`（`0600`），仅本机 TUI 可见。
- 校验：`Application.is_admin` 使用 `hmac.compare_digest` 常量时间比较。
- 请求头：`X-OpenList-Admin-Token`（兼容旧 `X-Admin-Token`）。
- OpenList token：独立文件 `openlist.token`，请求时作为 `Authorization` 头发送给本机 OpenList。

### 12.2 网络限制

- OpenList API URL 强制本机回环，避免 token 外泄。
- `listen_host` 仅允许 `0.0.0.0` 或 `127.0.0.1`。
- 维护模式下浏览相关路由未携带有效令牌返回 503。
- `/admin` 与所有 `/api/admin/*` 必须校验管理令牌。

### 12.3 文件权限

- 配置目录 `0700`，配置与令牌文件 `0600`（[install.sh:324-336](file:///e:/Other/Github/OpenList-Image-API/install.sh#L324-L336)）。
- 原子写：`atomic_write_json` 与 `write_secret` 先写 `.tmp` 再 `os.replace`。

### 12.4 systemd 加固

```ini
User=openlist-image
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/etc/openlist-image-api /var/lib/openlist-image-api
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
```

### 12.5 备份安全

`create_config_backup` 显式排除 `openlist_token_file`、`admin_token_file` 与 token 内容，仅导出可迁移配置。

### 12.6 公网部署建议

项目不自带 TLS、防火墙或反向代理。公网场景应使用 TLS、访问控制或反向代理保护管理路径（[README.md:76-78](file:///e:/Other/Github/OpenList-Image-API/README.md#L76-L78)）。

---

## 13. 运维手册

### 13.1 健康检查

```bash
curl --fail http://127.0.0.1:8790/health
```

### 13.2 状态检查

```bash
curl --fail http://127.0.0.1:8790/api/status
```

返回：服务状态、图片数、目录数、索引时间、上次耗时、是否刷新中、最近错误、缓存命中/未命中、公告与维护配置。

TUI：`sudo openlist-image-api` → 菜单 6。

### 13.3 索引重建

前台同步：

```bash
sudo -u openlist-image /usr/bin/python3 \
  /opt/openlist-image-api/openlist_image_api.py \
  --config /etc/openlist-image-api/config.json refresh
```

后台（推荐）：TUI 菜单 5。PID 存 `/run/openlist-image-api/rebuild.pid`，日志存 `/var/lib/openlist-image-api/rebuild.log`。

### 13.4 日志

应用日志走 `stdout/stderr`，由 systemd journal 接管：

```bash
sudo journalctl -u openlist-image-api
sudo journalctl -u openlist-image-api -f
sudo journalctl -u openlist-image-api --since today
sudo journalctl -u openlist -f
sudo tail -f /var/lib/openlist-image-api/rebuild.log
```

### 13.5 缓存

进程内 LRU（默认 1000 条 / 1800 秒），不持久化。重启服务即清空：

```bash
sudo systemctl restart openlist-image-api
```

TUI 维护菜单的“清理运行缓存”也是通过重启实现。

### 13.6 配置备份与恢复

```bash
ADMIN_TOKEN="$(sudo openlist-image-api --print-admin-token)"

curl --fail \
  -H "X-OpenList-Admin-Token: ${ADMIN_TOKEN}" \
  http://127.0.0.1:8790/api/admin/backup \
  --output /tmp/openlist-image-api-backup.zip

curl --fail -X POST \
  -H "X-OpenList-Admin-Token: ${ADMIN_TOKEN}" \
  -H "Content-Type: application/zip" \
  --data-binary @/tmp/openlist-image-api-backup.zip \
  http://127.0.0.1:8790/api/admin/backup
```

### 13.7 查看管理令牌

```bash
sudo openlist-image-api --print-admin-token
```

### 13.8 未提供的运维机制

Docker/K8s、Nginx 模板、TLS 自动签发、cron 定时索引、Prometheus 指标、logrotate、数据库、Redis、systemd timer、自动防火墙、多实例编排均未提供。

---

## 14. 发布流程

### 14.1 生成校验文件

```bash
sha256sum install.sh src/openlist_image_api.py src/openlist_tui.py VERSION > SHA256SUMS
```

### 14.2 版本同步

发布新版本时需同步更新（[README.md:112-129](file:///e:/Other/Github/OpenList-Image-API/README.md#L112-L129)）：

1. `VERSION` 文件。
2. `install.sh` 中的 `RELEASE_REF`（[install.sh:7](file:///e:/Other/Github/OpenList-Image-API/install.sh#L7)）。
3. `SHA256SUMS`。
4. `README.md` 中的安装命令版本号。

然后向 GitHub 与 Gitee 推送同名标签。`--update` 始终从 `main` 分支拉取，标签仅用于首次安装的固定版本校验。

---

## 附录：关键文件速查

| 文件 | 作用 |
|---|---|
| [src/openlist_image_api.py](file:///e:/Other/Github/OpenList-Image-API/src/openlist_image_api.py) | HTTP 服务、索引、缓存、WebUI、管理 API、子命令入口 |
| [src/openlist_tui.py](file:///e:/Other/Github/OpenList-Image-API/src/openlist_tui.py) | 终端管理界面 |
| [install.sh](file:///e:/Other/Github/OpenList-Image-API/install.sh) | 安装、更新、卸载、OpenList 部署、systemd 单元 |
| [.github/workflows/ci.yml](file:///e:/Other/Github/OpenList-Image-API/.github/workflows/ci.yml) | CI 验证流水线 |
| [tests/test_core.py](file:///e:/Other/Github/OpenList-Image-API/tests/test_core.py) | 核心单元测试 |
| [tests/test_management_features.py](file:///e:/Other/Github/OpenList-Image-API/tests/test_management_features.py) | 管理功能测试 |
| [SHA256SUMS](file:///e:/Other/Github/OpenList-Image-API/SHA256SUMS) | 发布文件校验 |
| [VERSION](file:///e:/Other/Github/OpenList-Image-API/VERSION) | 当前版本号 |
