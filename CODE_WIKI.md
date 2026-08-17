# OpenList Image API — Code Wiki

> 文档基线：当前 `main` 分支。已发布固定安装版本见 [`VERSION`](VERSION) 和 [`install.sh`](install.sh) 中的 `RELEASE_REF`。
>
> 本文使用相对文件链接和符号名，不绑定易漂移的源码行号。

## 目录

1. [项目定位](#1-项目定位)
2. [仓库结构](#2-仓库结构)
3. [运行架构](#3-运行架构)
4. [核心模块](#4-核心模块)
5. [配置与持久化](#5-配置与持久化)
6. [HTTP 路由与数据流](#6-http-路由与数据流)
7. [前端实现](#7-前端实现)
8. [安装与运维](#8-安装与运维)
9. [安全模型](#9-安全模型)
10. [测试与发布](#10-测试与发布)

## 1. 项目定位

OpenList Image API 为本机 OpenList 中选定目录的图片建立轻量索引，并通过一个纯 Python 标准库 HTTP 服务提供：

- 图片浏览页与管理 WebUI；
- 随机图片元数据与签名 URL 解析；
- 服务端附件代理下载；
- 图片点赞、分类和垃圾桶管理；
- 公告、维护模式、配置备份和日志查看；
- systemd 部署与交互式终端管理。

生产端没有 Python 第三方依赖，前端 HTML/CSS/JavaScript 由 [`src/openlist_image_api.py`](src/openlist_image_api.py) 直接内嵌返回。

## 2. 仓库结构

```text
OpenList-Image-API/
├── .github/workflows/ci.yml       # GitHub Actions 检查
├── src/
│   ├── openlist_image_api.py      # HTTP 服务、索引、缓存、标签和内嵌 WebUI
│   └── openlist_tui.py            # 终端管理界面
├── tests/
│   ├── mock_openlist.py           # 手动测试用 OpenList 模拟服务
│   ├── test_core.py               # 配置、索引、缓存、换链测试
│   └── test_management_features.py# WebUI、管理 API、TUI、安装器测试
├── install.sh                     # 安装、更新、卸载和 OpenList 部署
├── README.md                      # 用户与部署文档
├── CODE_WIKI.md                   # 本文档
├── SHA256SUMS                     # 安装文件校验清单
├── VERSION                        # 已发布版本号
└── LICENSE
```

仓库没有 `requirements.txt`、`pyproject.toml`、数据库、Redis、Dockerfile 或前端打包工具。

## 3. 运行架构

```text
浏览器 / API 客户端
        │ HTTP/1.1，gzip，TCP_NODELAY
        ▼
ConcurrentHTTPServer (ThreadingHTTPServer)
        │ make_handler(Application)
        ▼
Application
 ├── 配置加载与热重载
 ├── IndexRepository ── index.json
 ├── TagRepository ──── tags.json
 ├── UrlCache ───────── 进程内 LRU/TTL + 同路径 singleflight
 └── ThreadPoolExecutor(20)
        │
        ▼
OpenListClient
 ├── POST /api/fs/list
 ├── POST /api/fs/get
 └── POST /api/fs/remove
        │ 本机回环 HTTP
        ▼
OpenList
```

### 3.1 服务进程

systemd 启动命令：

```bash
/usr/bin/python3 /opt/openlist-image-api/openlist_image_api.py \
  --config /etc/openlist-image-api/config.json serve
```

`ConcurrentHTTPServer` 启用守护请求线程、128 个请求队列和地址复用。`Handler` 使用 HTTP/1.1；长度不小于 1024 字节且客户端接受 gzip 时压缩响应。

### 3.2 索引模型

`build_index()` 对配置的虚拟目录执行广度优先遍历：

1. `OpenListClient.list_directory()` 分页读取目录，每页 1000 条；
2. 子目录加入 `deque`，图片按扩展名过滤；
3. 记录图片路径、大小、遍历目录数、错误和耗时；
4. `IndexRepository.save()` 原子写入 `index.json`。

目录选择器不使用持久目录索引。`Application.list_directories()` 每次直接请求 OpenList，根目录请求失败时才回退到当前已配置目录。

### 3.3 URL 解析模型

随机图片接口先返回索引元数据；已缓存图片带 URL，未缓存图片带 `needs_url: true`。浏览器随后批量调用 `POST /api/download-url`，由共享 20 线程执行器解析签名 URL。

`UrlCache.resolve()` 维护：

- 可配置的 LRU 容量和 TTL；
- 每路径一个进行中请求记录；
- 缓存关闭时仍可合并同路径并发请求；
- `fresh=true` 时移除旧缓存，但并发刷新仍合并为一次上游请求。

## 4. 核心模块

### 4.1 `src/openlist_image_api.py`

| 层 | 主要符号 | 职责 |
| --- | --- | --- |
| 配置 | `DEFAULT_CONFIG`、`validate_config()`、`load_config()` | 缺省值、类型/范围校验、JSON 加载。 |
| 文件工具 | `atomic_write_json()`、`read_secret_cached()`、`write_secret()` | 原子 JSON/秘密文件读写和按 mtime 缓存。 |
| 路径安全 | `normalize_directory()`、`normalize_directories()`、`is_loopback_openlist_url()` | 虚拟路径规范化和 OpenList 回环地址限制。 |
| OpenList 客户端 | `OpenListClient` | 列目录、解析文件、删除文件、失败重试。 |
| 图片索引 | `IndexRepository`、`build_index()` | 图片索引缓存、原子持久化和 BFS 重建。 |
| 标签数据 | `TagRepository` | 点赞/踩、分类、统计、垃圾桶和旧路径迁移。 |
| URL 缓存 | `UrlCache`、`_InflightResolve` | LRU/TTL、并发合并和缓存统计。 |
| 应用服务 | `Application` | 配置视图、备份恢复、索引任务、筛选、换链和鉴权。 |
| HTTP | `make_handler()`、`ConcurrentHTTPServer` | 路由、状态码、压缩、下载代理和错误处理。 |
| 前端 | `gallery_html()`、`admin_html()` | 完整浏览页与管理页。 |
| CLI | `command_serve()`、`command_refresh()`、`command_create_admin_token()` | 三个子命令入口。 |

### 4.2 `src/openlist_tui.py`

TUI 从核心模块复用 `atomic_write_json()`、`load_config()` 和 `write_secret()`，负责：

- 读写配置和 token，并设置服务用户属主；
- 通过内置 `install.sh` 安装 OpenList、更新或卸载应用；
- 通过 `systemctl` 管理图片服务；
- 使用 `ss`/`lsof` 检查端口；
- 检测并清理旧 `openlist-random-image` 文件、服务和 Nginx 配置；
- 使用 `runuser` 在后台执行 `refresh`，记录 PID 和日志；
- 显示状态和管理令牌。

### 4.3 `install.sh`

安装器采用 `set -Eeuo pipefail`，支持：

```text
--source github|gitee|auto
--install-openlist
--openlist-download direct|auto
--update
--uninstall api|complete
```

职责包括下载与 SHA-256 校验、用户/目录创建、systemd 单元生成、全局 TUI 命令生成、固定代理测速、更新时恢复原服务状态，以及配置默认值迁移。

## 5. 配置与持久化

### 5.1 文件位置

| 数据 | 默认位置 | 说明 |
| --- | --- | --- |
| 配置 | `/etc/openlist-image-api/config.json` | JSON，原子写，权限 `0600`。 |
| OpenList token | `/etc/openlist-image-api/openlist.token` | 纯文本，权限 `0600`。 |
| 管理令牌 | `/etc/openlist-image-api/admin.token` | `secrets.token_urlsafe(32)`，权限 `0600`。 |
| 图片索引 | `/var/lib/openlist-image-api/index.json` | 图片路径、大小、统计、耗时和错误。 |
| 标签数据 | `/var/lib/openlist-image-api/tags.json` | schema version 1，原子写。 |
| 重建日志 | `/var/lib/openlist-image-api/rebuild.log` | TUI 后台 `refresh` 输出。 |
| 重建 PID | `/run/openlist-image-api/rebuild.pid` | 防止重复 TUI 后台任务。 |

### 5.2 当前配置项

| 配置项 | 源码缺省值 | 约束/用途 |
| --- | --- | --- |
| `listen_host` | `0.0.0.0` | `0.0.0.0` 或 `127.0.0.1`。 |
| `listen_port` | `8790` | 1024–65535。 |
| `openlist_api_url` | `http://127.0.0.1:5244` | 本机回环 HTTP 且必须带端口。 |
| `openlist_token_file` | `/etc/openlist-image-api/openlist.token` | 绝对路径。 |
| `state_dir` | `/var/lib/openlist-image-api` | 绝对路径。 |
| `directories` | `[]` | 规范化虚拟目录数组。 |
| `extensions` | 常见 7 种图片后缀 | 非空，元素以 `.` 开头。 |
| `caption_mode` | `path` | `path`/`name`/`hidden`。 |
| `directory_display_enabled` | `true` | 是否允许完整路径模式显示目录。 |
| `directory_display_depth` | `0` | 隐藏路径前 0–64 层。 |
| `theme` | `dark` | 新浏览器的浏览页缺省主题。 |
| `grid_gap` | `12` | 瀑布流间距 0–48。 |
| `url_cache_size` | `0` | 0–5000；安装器生成配置为 1000。 |
| `url_cache_ttl_seconds` | `1800` | 0–3600 秒。 |
| `announcement_*` | 关闭/空内容/0 秒 | 标题 ≤120，内容 ≤4000，强制阅读 0–3600 秒。 |
| `maintenance_enabled` | `false` | 维护模式总开关。 |
| `tagging_enabled` | `false` | 标签总开关。 |
| `tagging_scope` | `anonymous` | `disabled`/`anonymous`/`token`。 |
| `tagging_categories` | `[]` | 最多 32 个预定义分类。 |
| `filter_enabled` | `true` | 是否显示标签筛选栏。 |
| `log_level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`。 |
| `admin_token_file` | `/etc/openlist-image-api/admin.token` | 绝对路径。 |

为兼容旧配置，`view_layout`、`delivery`、`grid_scale`、`tagging_allow_custom` 和 `tagging_sort_default` 仍被接受和备份，但当前浏览器不依赖这些键完成视图、下载、自定义标签或排序。不要将它们当作当前 WebUI 的有效开关。

### 5.3 WebUI 白名单

`Application.update_admin_config()` 只接受管理页字段。OpenList 地址、token 路径、状态目录、扩展名、端口和缓存参数不能通过 WebUI 修改。

公告标题、内容、开关或强制阅读秒数变化时，`announcement_version` 自动递增。

### 5.4 浏览器持久化

浏览页使用 `openlist-image-preferences-v2` 保存：

- `view_layout`、`slideshow_interval`、`grid_gap`；
- `caption_mode`、`show_tags_enabled`；
- `theme`、`filter_mode`；
- `preview_quality`、`lightbox_quality`。

管理页主题使用独立键 `openlist-admin-theme`，默认暗色。公告关闭状态按 `openlist-image-announcement-v2-<version>` 保存：“本次关闭”保存到当天，“不再显示”永久隐藏该版本。

### 5.5 环境变量

生产程序不读取环境变量。配置来源只有 JSON、`--config` 和 token 文件。`MOCK_STATE_DIR` 仅供 `tests/mock_openlist.py` 使用。

## 6. HTTP 路由与数据流

### 6.1 路由表

| 方法 | 路径 | 访问控制 | 处理 |
| --- | --- | --- | --- |
| GET | `/`、`/gallery` | 公共 | 返回浏览页 HTML；页面根据公共配置显示维护状态。 |
| GET | `/admin` | 公共 HTML | 返回管理页；后续管理接口需令牌。 |
| GET | `/health` | 公共 | `{"status":"ok"}`。 |
| GET | `/api/status` | 公共 | 索引、缓存、刷新和公共配置。 |
| GET | `/api/public-config` | 公共 | 浏览默认值、公告、维护、目录和标签配置。 |
| GET | `/api/images/random` | 维护模式门控 | 过滤索引并返回随机图片元数据。 |
| GET | `/api/download-url` | 维护模式门控 | 解析一张已索引图片。 |
| POST | `/api/download-url` | 维护模式门控 | 解析最多 50 张已索引图片。 |
| GET | `/download` | 维护模式门控 | 服务端流式代理已索引原图附件。 |
| GET | `/random` | 维护模式门控 | 302 到随机签名 URL。 |
| GET | `/api/tagging/stats` | 公共 | 最多 50 个路径的标签统计。 |
| GET | `/api/tagging/categories` | 公共 | 分类计数。 |
| POST | `/api/tagging/vote` | 按标签范围 | 匿名范围按 IP+UA；token 范围校验管理令牌。 |
| GET/PUT | `/api/admin/config` | 管理令牌 | 读取/保存全局配置。 |
| GET | `/api/admin/directories` | 管理令牌 | 直接读取 OpenList 子目录。 |
| POST | `/api/admin/rebuild` | 管理令牌 | 启动后台图片索引重建。 |
| GET/POST | `/api/admin/backup` | 管理令牌 | 下载/恢复配置 ZIP。 |
| GET | `/api/admin/logs` | 管理令牌 | 读取 journalctl。 |
| GET | `/api/admin/tagging/trash` | 管理令牌 | 查看垃圾桶路径。 |
| POST | `/api/admin/tagging/trash/delete` | 管理令牌 | 永久删除 OpenList 文件。 |
| POST | `/api/admin/tagging/reset` | 管理令牌 | 重置单图或全部标签。 |

管理 API 鉴权失败返回 401；维护模式下受门控的浏览 API 未携带有效令牌时返回 503。

### 6.2 随机图片

请求参数：

```text
count=1..50
folder=/virtual/path
min_size=500kb
max_size=10mb
tag=landscape           # 可重复，也接受 tags
filter_mode=union|intersect
```

数据流：

```text
GET /api/images/random
  → Application.choose_images()
  → 路径/大小/标签过滤
  → random.sample 或 random.choice 回填
  → Application.resolve_images_lazy()
  → 已缓存 URL 直接返回，未缓存项标记 needs_url
  → 后台预热未解析项
```

文档中不应出现 `min_likes`、`min_ratio` 或 `sort`，当前处理器不解析这些参数。

### 6.3 批量换链

POST 请求体：

```json
{"paths":["/gallery/a.jpg","/gallery/b.jpg"],"fresh":false}
```

`Application.indexed_images()` 一次扫描索引并保留有效去重顺序。未索引路径返回 `image is not in the current index`；上游解析错误返回 `unable to resolve image URL`。接口不会解析索引外的任意 OpenList 路径。

### 6.4 下载

- 浏览页灯箱的下载按钮刷新后直接点击 OpenList 签名 URL。
- `/download?path=...` 是独立的服务端附件代理：校验索引后解析原图 URL，按 64 KiB 分块写回，透传内容类型和可用的内容长度。
- 客户端中途断开时只记录日志，不让请求线程崩溃。

### 6.5 标签

`TagRepository` 为每张图片维护：

```json
{
  "likes": 0,
  "dislikes": 0,
  "categories": [],
  "voters": {}
}
```

匿名点赞/踩使用 IP+User-Agent 的 HMAC 标识去重；token 范围先验证管理令牌，再生成令牌标识。分类是图片级共享状态，不按投票者分别保存。浏览页当前展示点赞按钮、预定义分类和垃圾桶按钮；后端仍接受 `dislike` 类型，供 API 调用方使用。

索引重建后会将标签路径迁移到当前有效路径：先精确匹配，再尝试不区分大小写和末三段路径匹配。

## 7. 前端实现

### 7.1 浏览页

`gallery_html()` 包含：

- **幻灯片**：首批 6 张、预加载 3 张、历史上限 60、自动播放、页面按钮、菜单按钮和触屏滑动；页面隐藏、灯箱、设置面板或公告打开时暂停。
- **瀑布流**：宽屏 3 列、900 px 及以下 2 列；按估算高度放入最矮列，`IntersectionObserver` 负责接近视口时加载，滚动到 60% 后拉取下一批。
- **灯箱**：0.5–4 倍缩放、90° 旋转、拖拽、捏合、双击复位和失效 URL 恢复。
- **画质**：`sizedThumb()` 只改写已包含 `width`/`height` 参数的缩略图 URL，否则原样返回。
- **公告**：受限 Markdown 转换、阅读倒计时和版本化关闭状态。
- **维护**：验证管理令牌后将其暂存在页面内存，并附加到受门控请求。

旧本地偏好中的 `single`/`grid` 会迁移为 `slideshow`。当前实际渲染类只有 `gallery slideshow` 和 `gallery waterfall`。

### 7.2 管理页

`admin_html()` 使用六个页签：

1. 目录配置；
2. 显示与主题；
3. 网站公告；
4. 维护模式；
5. 标签与日志；
6. 图片索引和备份。

目录树通过 `GET /api/admin/directories?path=...` 延迟展开。保存使用 `PUT /api/admin/config`；索引重建使用 `POST /api/admin/rebuild` 并轮询 `/api/status`；配置 ZIP 可下载和上传恢复。

明暗主题悬浮按钮位于右下角，只保存在当前浏览器。

## 8. 安装与运维

### 8.1 安装产物

```text
/opt/openlist-image-api/{install.sh,openlist_image_api.py,openlist_tui.py,VERSION}
/etc/openlist-image-api/{config.json,admin.token,openlist.token}
/var/lib/openlist-image-api/{index.json,tags.json,rebuild.log}
/etc/systemd/system/openlist-image-api.service
/usr/local/bin/openlist-image-api
```

服务用户为 `openlist-image`。

### 8.2 常用命令

```bash
sudo systemctl status openlist-image-api
sudo systemctl restart openlist-image-api
sudo journalctl -u openlist-image-api -f
sudo openlist-image-api --print-admin-token
curl --fail http://127.0.0.1:8790/health
curl --fail http://127.0.0.1:8790/api/status
```

前台同步重建：

```bash
sudo -u openlist-image /usr/bin/python3 \
  /opt/openlist-image-api/openlist_image_api.py \
  --config /etc/openlist-image-api/config.json refresh
```

推荐使用 TUI 菜单 5 在后台重建。运行时 URL 缓存不持久化，重启服务即可清空。

### 8.3 缓存默认值与迁移

- Python 源码缺省：容量 0，TTL 1800 秒；适用于直接运行且配置缺失时。
- 安装器生成配置：容量 1000，TTL 1800 秒。
- 老安装默认值：200/240 会迁移到 1000/1800；`grid_scale` 125 仍兼容迁移到 150，但当前前端不读取它。

### 8.4 不提供的运维机制

项目不提供 Docker/Kubernetes、Nginx 模板、TLS 自动签发、cron/systemd timer、Prometheus、logrotate、数据库、Redis、防火墙配置或多实例编排。

## 9. 安全模型

### 9.1 令牌

- OpenList token 和管理令牌存放在独立 `0600` 文件中。
- 管理令牌由 `secrets.token_urlsafe(32)` 生成。
- `Application.is_admin()` 使用 `hmac.compare_digest()`。
- 请求头首选 `X-OpenList-Admin-Token`，兼容旧 `X-Admin-Token`。
- token 范围的标签请求必须通过 `Application.is_admin()`，不是只检查非空请求头。

### 9.2 路径和网络

- 虚拟路径被规范化，拒绝 `..`。
- OpenList 地址必须是本机回环 HTTP、带端口、无凭据和额外路径。
- `/download` 和 GET/POST `/api/download-url` 只接受当前图片索引中的路径。
- 垃圾桶删除前再次确认路径存在于图片索引；删除使用 OpenList `/api/fs/remove`。

### 9.3 文件与进程

systemd 服务启用：

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

`atomic_write_json()` 和 `write_secret()` 先写同目录临时文件，再 `os.replace()`。配置目录权限为 `0700`，配置/token 文件为 `0600`。

### 9.4 备份

备份 ZIP 只包含白名单配置，不包含 token 内容，也不导出 `openlist_token_file` 或 `admin_token_file` 路径。恢复时再次按白名单过滤并执行完整配置校验。

## 10. 测试与发布

### 10.1 本地与 CI

```bash
python3 -m py_compile src/openlist_image_api.py src/openlist_tui.py
python3 -m unittest discover -s tests -v
bash -n install.sh
sha256sum --check SHA256SUMS
! grep -q $'\r' install.sh
```

GitHub Actions 在 push 和 pull request 时使用 Ubuntu 与 Python 3.11 执行以上检查。

### 10.2 测试范围

自动测试覆盖：

- 配置、路径和回环地址校验；
- URL 缓存并发合并、刷新和并行解析；
- 图片索引读写、实时目录浏览和状态耗时；
- 批量换链、未索引路径拒绝和附件下载；
- 管理字段白名单、公告版本和备份秘密排除；
- token 标签范围的管理令牌验证；
- 浏览/管理页关键结构；
- TUI 状态、服务、更新、卸载和安装器约束。

前端测试主要是生成 HTML/JavaScript 的字符串断言，不等于完整浏览器端到端覆盖。涉及布局、触控、主题、公告或灯箱的改动还需实际浏览器冒烟验证。

### 10.3 发布一致性

修改任一发布文件后执行：

```bash
sha256sum install.sh src/openlist_image_api.py src/openlist_tui.py VERSION > SHA256SUMS
sha256sum --check SHA256SUMS
```

发布新版本需要同时更新：

1. [`VERSION`](VERSION)；
2. [`install.sh`](install.sh) 的 `RELEASE_REF`；
3. [`README.md`](README.md) 的固定安装链接；
4. [`SHA256SUMS`](SHA256SUMS)；
5. GitHub 与 Gitee 的同名标签。

这些内容必须指向同一提交。固定 `v1.4.0` 安装链接只安装该标签内容；`--update` 从 `main` 获取开发版更新。
