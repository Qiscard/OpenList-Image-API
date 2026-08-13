# OpenList Image API

一个可通过 GitHub 或 Gitee 一键安装的 OpenList 图片 API、终端管理界面和 WebUI。项目只保存通用程序逻辑与默认配置；不包含用户网盘目录、访问令牌、服务器地址或其他个人信息。

## 特性

- **终端 TUI**：内置安装 OpenList、设置 token/端口/监听范围、选择多个图片目录、查看状态、后台重建索引、清理旧 API 残留、更新与卸载。
- **内置 OpenList 安装器**：不再下载或执行外部管理脚本；只保留非 Docker 的二进制安装、架构识别与 systemd 服务创建逻辑。
- **下载策略**：OpenList 默认直连 GitHub Releases；也可在 TUI 选择四个预设镜像自动测速，使用最快的可用镜像。每次下载最多重试两次。
- **图片浏览页**：幻灯片视图按批预加载并保留播放历史，支持自动播放；瀑布流每批加载 15 张，桌面端固定 3 列，宽图自动跨 2 列，按固定列从底部稳定追加，不再重排旧图片。
- **大图窗口**：点击图片在站内打开预览，支持滚轮/双指缩放、双指捏合、左右旋转、双击复位与拖拽平移；URL 失效时自动刷新，页面切回前台并空闲超时后自动恢复连接。
- **清晰浏览交互**：只有点击“下载”才会通过附件接口下载原文件；视图、间距、自动播放间隔与大图目录开关仅保存在当前浏览器，图片文字与目录展示由管理员统一配置。
- **网站公告**：Markdown 公告弹窗，可设置强制阅读秒数；修改标题、内容、开关或秒数会自动生成新版本，访客可“本次关闭”或“不再显示”。
- **维护模式**：开启后浏览页仅显示维护提示，管理员输入管理令牌验证后可在当前会话临时查看与下载图片。
- **目录展示**：图片文字为完整路径时可隐藏前 N 层目录，避免泄露网盘根目录结构。
- **网络与并发优化**：HTTP/1.1 Keep-Alive、TCP_NODELAY、gzip、12 路签名链接解析、同路径请求合并，以及默认 1000 条/30 分钟的签名链接缓存。
- **WebUI**：支持全局目录多选、图片文字、目录展示、网站公告与维护模式配置，目录缓存独立刷新，后台索引重建进度提示，以及不含 token 的配置备份与恢复。
- **可维护性**：纯 Python 标准库、原子索引写入、分页目录扫描、LRU 签名链接缓存、systemd 低权限图片服务、LF 行尾和单元测试。

## 安装

使用固定版本安装器（GitHub）：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiscard/OpenList-Image-API/v1.4.0/install.sh | sudo bash -s -- --source github
```

使用固定版本安装器（Gitee）：

```bash
curl -fsSL https://gitee.com/qiscard/OpenList-Image-API/raw/v1.4.0/install.sh | sudo bash -s -- --source gitee
```

`--source auto` 会优先使用 Gitee，失败时回退到 GitHub。安装器会校验固定标签中的 `install.sh`、两个 Python 文件和版本文件的 SHA-256，然后创建图片 API 服务与全局管理命令。

## TUI

```bash
sudo openlist-image-api
```

菜单提供：

1. 安装/部署 OpenList：选择直连下载（默认）或自动测速镜像。
2. 保存 OpenList API token（仅保存在本机受限文件中）。
3. 设置图片 API 端口，并在保存前检查端口占用。
4. 图片 API 服务管理：设置监听范围、启动、停止或重启服务。
5. 在后台重建图片索引，基于上次记录显示预计耗时。
6. 查看服务、索引、监听范围、最近重建耗时和 WebUI 管理令牌。
7. 维护工具：更新图片 API、卸载，或清理旧 API 残留与运行缓存。

图片目录、图片文字、目录展示、网站公告与维护模式属于全局服务器配置，需要 WebUI 管理令牌；视图、间距、自动播放间隔与大图目录开关只保存在当前浏览器，不需要令牌。管理令牌就是 TUI 菜单 6 显示的令牌，可直接粘贴到 `/admin` 页面加载服务器配置。

OpenList 的镜像测速候选为：

- `https://edgeone.gh-proxy.com`
- `https://hk.gh-proxy.com`
- `https://gh-proxy.com`
- `https://gh.dpik.top`

自动测速找不到可用镜像时会自动退回 GitHub 直连。安装 OpenList 后，在其初始化完成后回到 TUI 保存 API token、选择目录，然后执行“后台重建图片索引”。后台重建日志保存在状态目录中的 `rebuild.log`。

## WebUI 与 API

图片服务默认监听 `0.0.0.0:8790`，因此服务器商将外部端口 NAT 转发到本机 `8790` 后可直接访问 WebUI。升级时，旧配置中 `127.0.0.1` 的监听地址会自动迁移为 `0.0.0.0`；也可以通过 TUI 菜单 4 切回仅本机监听。

浏览页支持两种模式：

- **幻灯片视图**：每次显示 1 张，后台按批预加载后续 3 张并保留最近播放历史，可在底部历史条点击回看；支持自动播放间隔（0 表示关闭）。URL 失效时自动刷新，页面切到后台再切回并空闲超过 5 分钟时自动恢复连接。
- **瀑布流视图**：每批加载 15 张并保留图片原始比例；桌面端固定 3 列，宽高比不低于 1.45 的宽图自动横跨 2 列，平板与手机自动降为 2 列或 1 列；按独立固定列轮转追加，新批次只进入各列底部，不会像 CSS 多栏布局那样重排旧图片。

点击任意图片会在当前页面打开大图预览，不会跳转或直接下载；大图窗口支持滚轮/双指缩放、双指捏合、左右旋转、双击复位与拖拽平移。只有放大预览中的“下载”按钮会请求附件下载接口。视图、间距、自动播放间隔与大图目录开关保存在浏览器 `localStorage` 中，以访问来源为单位隔离，不同设备、不同浏览器互不覆盖；图片文字、目录展示、网站公告与维护模式属于全局配置，由管理员在 `/admin` 统一设置。

开启网站公告后，访客首次进入会看到 Markdown 公告弹窗；设置强制阅读秒数时，访客需等待指定时间才能“本次关闭”或“不再显示”。开启维护模式后，浏览页仅显示维护提示；管理员在维护面板输入管理令牌验证成功后，当前会话可临时查看与下载图片，访问浏览相关接口时需附带管理令牌。

服务使用多线程 HTTP 服务器并共享 12 个 OpenList 签名解析线程，可供多台设备同时浏览。相同图片的并发签名请求会合并，默认缓存 1000 条/1800 秒；升级时只迁移仍使用旧默认值的配置，不覆盖手动设置。服务器配置由多名管理员同时保存时采用最后一次保存结果。

请同时确认主机防火墙、安全组与服务商 NAT 规则允许目标端口。`/admin` 和所有 `/api/admin/*` 路由必须输入管理令牌；维护模式下浏览相关路由未携带有效管理令牌时返回 503；如果向公网开放服务，应使用 TLS、访问控制或反向代理进一步保护管理路径。

| 路由 | 用途 |
| --- | --- |
| `/` | 图片浏览页。 |
| `/admin` | 管理页面；输入管理令牌后可编辑全局配置。 |
| `/random` | 跳转到一张随机图片。 |
| `/download?path=…` | 流式代理已索引图片，并以附件响应下载原文件。 |
| `/api/images/random?count=…` | 获取随机图片 JSON。 |
| `/api/download-url?path=…` | 获取已索引图片的当前签名直链，`fresh=1` 强制刷新。 |
| `/api/status` | 获取图片数量、签名缓存和索引重建状态。 |
| `/api/public-config` | 获取设备浏览默认值、图片文字、目录展示、公告与维护状态。 |
| `/api/admin/config` | 管理令牌校验；GET 读取、PUT 保存全局服务器配置。 |
| `/api/admin/directories?path=…` | 管理令牌；列出目录缓存中的子目录。 |
| `/api/admin/directories/refresh` | 管理令牌；POST 后台刷新目录缓存。 |
| `/api/admin/rebuild` | 管理令牌；POST 后台重建图片与目录索引。 |
| `/api/admin/backup` | 管理令牌；GET 下载不含 token 的配置备份，POST 上传 ZIP 恢复可编辑配置。 |
| `/health` | 健康检查。 |

管理令牌只能通过本机 TUI 显示：

```bash
sudo openlist-image-api --print-admin-token
```

## 安全说明

- 图片 API 使用专用低权限系统用户运行，不使用 root 运行。
- OpenList API 地址仅允许本地 HTTP 服务，避免把 token 发送至外部地址。
- WebUI 的服务器接口只允许修改目录、图片文字、目录展示、网站公告与维护模式；设备浏览偏好仅写入当前浏览器，不能通过 WebUI 修改 OpenList API 地址、token 文件或图片扩展名。
- WebUI 配置备份会移除 OpenList token 文件和管理 token 文件路径，不包含任何 token 内容。
- TUI 的完全卸载会删除本项目安装的 OpenList 二进制、服务、配置与本地状态；如存在独立部署的 OpenList 数据，请先自行备份。
- 不要把管理令牌、OpenList token 或部署机配置提交到 Git。

## 开发与发布

运行验证：

```bash
python3 -m py_compile src/openlist_image_api.py src/openlist_tui.py
python3 -m unittest discover -s tests -v
bash -n install.sh
sha256sum --check SHA256SUMS
```

生成发布校验文件：

```bash
sha256sum install.sh src/openlist_image_api.py src/openlist_tui.py VERSION > SHA256SUMS
```

发布新版本时，同步更新 `VERSION`、`install.sh` 中的 `RELEASE_REF`、`SHA256SUMS` 与 README 安装命令，并向 GitHub、Gitee 推送同名标签。

## License

MIT
