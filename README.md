# OpenList Image API

一个纯 Python 标准库实现的 OpenList 图片 API、浏览图库、管理 WebUI 与终端管理工具。仓库只包含通用程序逻辑和默认配置，不包含用户目录、访问令牌、服务器地址或其他部署机私有数据。

> **版本说明**：下方固定链接安装已发布的 `v1.4.0`。本文功能说明以当前 `main` 分支为准，其中晚于 `v1.4.0` 的改动会进入后续版本。已安装用户可在 TUI 的维护菜单中选择“更新图片 API”，从 `main` 获取开发版更新。

## 主要功能

- **终端 TUI**：安装 OpenList、保存 token、设置端口和监听范围、管理服务、后台重建索引、查看状态、更新、卸载，以及清理旧 API 残留和运行时缓存。
- **内置 OpenList 安装器**：直接安装非 Docker 二进制，自动识别架构并创建 systemd 服务；可直连 GitHub Releases，或测速四个内置镜像后选择最快可用项。
- **幻灯片浏览**：首批加载 6 张并预加载后续 3 张，保留最多 60 条播放历史，支持自动播放、页面两侧切换按钮、暂停按钮和触屏左右滑动。
- **瀑布流浏览**：每批加载 20 张，宽屏 3 列、900 px 及以下 2 列；新卡进入当前最矮列，接近可视区时才加载，并在滚动到页面 60% 后继续取下一批。
- **批量换链**：浏览页通过 `POST /api/download-url` 批量解析当前图片的签名直链，空闲时预热下一批；相同路径的并发请求会合并。
- **大图预览**：站内灯箱支持滚轮/双指缩放、捏合、拖拽、左右旋转和双击复位；签名 URL 失效时自动刷新。
- **画质选择**：列表缩略图提供 176/480/800/1280/2560 px 档位，大图提供原图/2560/1280 px 档位。只有 OpenList 返回的缩略图 URL 已包含 `width`/`height` 参数时，程序才会改写尺寸；否则沿用原缩略图 URL。
- **标签管理**：可启用点赞、分类标签、并集/交集筛选和内置“垃圾桶”标签；管理页可查看垃圾列表并从 OpenList 删除选中的原文件。
- **公告与维护模式**：支持 Markdown 公告、强制阅读倒计时和按公告版本记忆关闭状态；维护期间浏览相关 API 返回 503，管理员可在当前会话中使用管理令牌临时解锁。
- **实时目录浏览**：管理页按需读取 OpenList 目录树，不维护单独的目录缓存；图片索引仍需显式重建。
- **安全与运维**：OpenList API 只允许本机回环 HTTP 地址，服务以低权限 systemd 用户运行，配置备份不包含 token，所有下载和换链路径必须已存在于当前图片索引。

## 运行要求

- Linux + systemd，安装和 TUI 管理需要 root 或 `sudo`。
- Python 3.10 或更高版本；CI 使用 Python 3.11。
- 安装图片 API 需要 `curl`、`sha256sum` 和 `systemctl`。
- 后台索引重建需要 `runuser`；端口检查优先使用 `ss`，缺失时回退到 `lsof`。
- OpenList 必须作为本机 HTTP 服务运行，默认地址为 `http://127.0.0.1:5244`。

生产程序不读取环境变量：配置来自 JSON 文件、`--config` 参数和两个 token 文件。仅手动测试辅助程序 `tests/mock_openlist.py` 支持 `MOCK_STATE_DIR`。

## 安装

GitHub 固定版本：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiscard/OpenList-Image-API/v1.4.0/install.sh | sudo bash -s -- --source github
```

Gitee 固定版本：

```bash
curl -fsSL https://gitee.com/qiscard/OpenList-Image-API/raw/v1.4.0/install.sh | sudo bash -s -- --source gitee
```

`--source auto` 优先使用 Gitee，失败时回退到 GitHub。固定版本安装器会下载并校验 `install.sh`、两个 Python 文件和 `VERSION` 的 SHA-256，然后创建图片 API 服务与全局管理命令。

安装完成后运行：

```bash
sudo openlist-image-api
```

推荐首次配置顺序：

1. 在 TUI 安装/部署 OpenList，并完成 OpenList 自身初始化。
2. 在 TUI 保存 OpenList API token。
3. 打开 `/admin`，粘贴管理令牌并选择图片目录。
4. 在 TUI 或管理页启动后台图片索引重建。

TUI 菜单 6 会显示管理令牌，也可直接运行：

```bash
sudo openlist-image-api --print-admin-token
```

## TUI 功能

1. 安装/部署 OpenList，可选择直连或自动测速镜像。
2. 保存 OpenList API token，内容只写入本机受限文件。
3. 修改图片 API 端口，保存前检查端口占用。
4. 设置监听范围，或启动、停止、重启图片 API 服务。
5. 后台重建图片索引，并基于上次记录显示预计耗时。
6. 查看服务、索引、监听范围、最近重建耗时和 WebUI 管理令牌。
7. 更新、卸载，或清理旧 API 残留与运行时签名链接缓存。

OpenList 镜像测速候选：

- `https://edgeone.gh-proxy.com`
- `https://hk.gh-proxy.com`
- `https://gh-proxy.com`
- `https://gh.dpik.top`

所有镜像不可用时会回退到 GitHub 直连。后台重建日志位于状态目录的 `rebuild.log`。

## 浏览器设置与全局配置

浏览器本地偏好保存在当前访问来源的 `localStorage` 中：

- 幻灯片/瀑布流视图；
- 自动播放间隔与瀑布流间距；
- 图片文字模式和标签按钮显示；
- 标签筛选的并集/交集模式；
- 列表图片画质与大图画质；
- 浏览页主题。

服务器全局配置由 `/admin` 管理：

- 图片目录；
- 新访客的文字模式；
- 目录显示开关和隐藏层数；
- 浏览页默认主题；
- 公告、维护模式、标签策略和日志级别。

管理页主题独立保存在浏览器中，默认暗色。多名管理员并发保存配置时，以最后一次成功保存为准。

公告的“本次关闭”实际在当前浏览器中隐藏到当天结束；“不再显示”会永久隐藏当前公告版本。公告内容或相关设置改变时版本递增，新版本会重新显示。

## WebUI 与 API

服务默认监听 `0.0.0.0:8790`。公网使用前还需配置主机防火墙、安全组、NAT 和 TLS/反向代理；项目本身不提供 TLS 或访问控制代理。

| 方法与路由 | 鉴权与用途 |
| --- | --- |
| `GET /`、`GET /gallery` | 返回浏览页 HTML；维护状态由页面读取公共配置后展示。 |
| `GET /admin` | 返回管理页 HTML；实际管理 API 仍需令牌。 |
| `GET /health` | 健康检查。 |
| `GET /api/status` | 图片数、索引状态、缓存统计和公共配置。 |
| `GET /api/public-config` | 浏览默认值、目录展示、主题、公告、维护和标签状态。 |
| `GET /api/images/random` | 获取随机图片元数据；维护模式下需要管理令牌。 |
| `GET /api/download-url?path=…&fresh=1` | 获取一张已索引图片的签名 URL；维护模式下需要管理令牌。 |
| `POST /api/download-url` | 批量获取最多 50 张已索引图片的签名 URL；未索引或解析失败项返回独立错误。 |
| `GET /download?path=…` | 服务端代理一张已索引原图并返回附件；维护模式下需要管理令牌。 |
| `GET /random` | 302 跳转到一张随机图片的签名 URL；维护模式下需要管理令牌。 |
| `GET /api/tagging/stats?paths=…` | 批量读取最多 50 个路径的标签统计。 |
| `GET /api/tagging/categories` | 获取分类标签计数。 |
| `POST /api/tagging/vote` | 提交点赞/踩或分类变更；匿名范围按 IP+UA 去重，token 范围要求有效管理令牌。 |
| `GET/PUT /api/admin/config` | 管理令牌；读取或保存全局配置。 |
| `GET /api/admin/directories?path=…` | 管理令牌；实时读取 OpenList 的直接子目录。 |
| `POST /api/admin/rebuild` | 管理令牌；启动后台图片索引重建。 |
| `GET/POST /api/admin/backup` | 管理令牌；下载或恢复不含 token 的 ZIP 配置备份。 |
| `GET /api/admin/logs?lines=…` | 管理令牌；读取最近 1–500 行 systemd journal。 |
| `GET /api/admin/tagging/trash` | 管理令牌；读取垃圾桶路径。 |
| `POST /api/admin/tagging/trash/delete` | 管理令牌；从 OpenList 永久删除选中或全部垃圾图片。 |
| `POST /api/admin/tagging/reset?path=…` | 管理令牌；重置指定图片或全部标签数据。 |

### 随机图片参数

`GET /api/images/random` 支持：

- `count`：1–50，默认 1；符合条件的图片不足时可能随机重复。
- `folder`：只选择该虚拟目录下的图片。
- `min_size` / `max_size`：支持 `b`、`k`/`kb`、`m`/`mb`、`g`/`gb`，例如 `500kb`。
- 重复的 `tag`（或 `tags`）：按分类标签筛选。
- `filter_mode=union|intersect`：任一标签匹配或全部标签匹配。

为缩短首包延迟，随机接口可能返回 `url: ""`、`needs_url: true`；浏览页随后通过批量换链接口补齐 URL。

### 批量换链请求

```json
{
  "paths": ["/gallery/a.jpg", "/gallery/b.jpg"],
  "fresh": false
}
```

返回结果与输入中的有效去重路径对应，每项包含 `path`、`url`、`thumbnail`，或 `error`。`fresh=true` 会刷新已缓存的签名 URL。

大图中的“下载”按钮会刷新并点击 OpenList 签名 URL；独立的 `/download` 路由用于需要服务端附件代理语义的 API 调用方。

## 缓存与并发

- 服务共享 20 个签名 URL 解析线程。
- 相同路径的并发未命中会合并为一次 OpenList 请求，即使缓存容量为 0 也生效。
- 源码缺省配置的缓存容量为 0；安装器生成的部署配置使用 1000 条、1800 秒。
- 旧安装配置中的 200 条/240 秒会迁移为 1000 条/1800 秒。
- 缓存只存在于进程内，重启服务即清空。

## 安全说明

- 图片 API 以专用低权限用户 `openlist-image` 运行，不以 root 运行。
- `openlist_api_url` 只允许带端口的本机回环 HTTP 地址，避免把 OpenList token 发送到外部服务。
- 管理令牌使用常量时间比较；token 范围标签操作要求有效管理令牌。
- 下载和签名换链接口拒绝不在当前图片索引中的路径。
- WebUI 只能修改白名单中的服务器配置，不能修改 OpenList API 地址、token 文件路径或图片扩展名。
- 配置备份排除 OpenList token 文件和管理 token 文件路径，不包含任何 token 内容。
- 完全卸载会删除本项目安装的 OpenList 二进制、服务、配置与本地状态；独立数据必须提前备份。
- 不要将管理令牌、OpenList token 或部署机配置提交到 Git。

## 开发与验证

本地执行与 CI 相同的检查：

```bash
python3 -m py_compile src/openlist_image_api.py src/openlist_tui.py
python3 -m unittest discover -s tests -v
bash -n install.sh
sha256sum --check SHA256SUMS
! grep -q $'\r' install.sh
```

测试使用标准库 `unittest`。前端自动测试主要检查生成的 HTML/JavaScript 结构和关键 HTTP 行为，不是完整的浏览器端到端测试；用户可见交互变更还应进行浏览器冒烟验证。

源码或安装文件改变后重新生成校验清单：

```bash
sha256sum install.sh src/openlist_image_api.py src/openlist_tui.py VERSION > SHA256SUMS
```

发布新版本时必须同步更新：

1. `VERSION`；
2. `install.sh` 中的 `RELEASE_REF`；
3. `README.md` 的固定版本安装链接；
4. `SHA256SUMS`；
5. GitHub 与 Gitee 的同名标签。

固定标签、文档版本和校验清单必须来自同一提交。`--update` 始终从 `main` 拉取，不受首次安装标签限制。

更多架构与运维细节见 [CODE_WIKI.md](CODE_WIKI.md)。

## License

MIT
