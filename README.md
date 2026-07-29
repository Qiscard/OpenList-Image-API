# OpenList Image API

一个可通过 GitHub 或 Gitee 一键安装的 OpenList 图片 API、终端管理界面和 WebUI。项目只保存通用程序逻辑与默认配置；不包含用户网盘目录、访问令牌、服务器地址或其他个人信息。

## 特性

- **终端 TUI**：内置安装 OpenList、设置 token/端口/监听范围、选择多个图片目录、查看状态、启动或重启服务、后台重建索引、检测并清理旧 API 残留。
- **内置 OpenList 安装器**：不再下载或执行外部管理脚本；只保留非 Docker 的二进制安装、架构识别与 systemd 服务创建逻辑。
- **下载策略**：OpenList 默认直连 GitHub Releases；也可在 TUI 选择四个预设镜像自动测速，使用最快的可用镜像。每次下载最多重试两次。
- **WebUI**：支持多选 OpenList 虚拟目录、单张/网格/瀑布流视图、直接预览/下载预览，以及不含 token 的配置备份下载。
- **可维护性**：纯 Python 标准库、原子索引写入、分页目录扫描、LRU 签名链接缓存、systemd 低权限图片服务、LF 行尾和单元测试。

## 安装

使用固定版本安装器（GitHub）：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiscard/OpenList-Image-API/v1.1.0/install.sh | sudo bash -s -- --source github
```

使用固定版本安装器（Gitee）：

```bash
curl -fsSL https://gitee.com/qiscard/OpenList-Image-API/raw/v1.1.0/install.sh | sudo bash -s -- --source gitee
```

`--source auto` 会优先使用 Gitee，失败时回退到 GitHub。安装器会校验固定标签中的 `install.sh`、两个 Python 文件和版本文件的 SHA-256，然后创建图片 API 服务与全局管理命令。

## TUI

```bash
sudo openlist-image-api
```

菜单提供：

1. 安装/部署 OpenList：选择直连下载（默认）或自动测速镜像。
2. 保存 OpenList API token（仅保存在本机受限文件中）。
3. 设置 OpenList 本地 API 端口。
4. 浏览并多选 OpenList 虚拟目录。
5. 设置 WebUI 视图与阅览方式。
6. 设置图片 API 端口，并在保存前检查端口占用。
7. 设置 API 监听范围：全部网络接口或仅本机回环。
8. 启动图片 API 服务。
9. 重启图片 API 服务。
10. 在后台重建图片索引，基于上次记录显示预计耗时。
11. 查看服务、索引、监听范围和最近重建耗时。
12. 显示 WebUI 管理令牌。
13. 检测并停用、清理旧随机图片 API 残留。

OpenList 的镜像测速候选为：

- `https://edgeone.gh-proxy.com`
- `https://hk.gh-proxy.com`
- `https://gh-proxy.com`
- `https://gh.dpik.top`

自动测速找不到可用镜像时会自动退回 GitHub 直连。安装 OpenList 后，在其初始化完成后回到 TUI 保存 API token、选择目录，然后执行“后台重建图片索引”。后台重建日志保存在状态目录中的 `rebuild.log`。

## WebUI 与 API

图片服务默认监听 `0.0.0.0:8790`，因此服务器商将外部端口 NAT 转发到本机 `8790` 后可直接访问 WebUI。升级时，旧配置中 `127.0.0.1` 的监听地址会自动迁移为 `0.0.0.0`；也可以通过 TUI 菜单 7 切回仅本机监听。

请同时确认主机防火墙、安全组与服务商 NAT 规则允许目标端口。`/admin` 和所有 `/api/admin/*` 路由必须输入管理令牌；如果向公网开放服务，应使用 TLS、访问控制或反向代理进一步保护管理路径。

| 路由 | 用途 |
| --- | --- |
| `/` | 图片浏览页，按照已配置视图展示随机图片。 |
| `/admin` | 管理页面；需要输入管理令牌。 |
| `/random` | 跳转到一张随机图片。 |
| `/download?path=…` | 以下载方式跳转到已索引图片。 |
| `/api/images/random?count=…` | 获取随机图片 JSON。 |
| `/api/status` | 获取图片数量、缓存、重建状态与上次耗时。 |
| `/api/admin/backup` | 需要管理令牌；下载不含 token 的配置备份。 |
| `/health` | 健康检查。 |

管理令牌只能通过本机 TUI 显示：

```bash
sudo openlist-image-api --print-admin-token
```

## 安全说明

- 图片 API 使用专用低权限系统用户运行，不使用 root 运行。
- OpenList API 地址仅允许本地 HTTP 服务，避免把 token 发送至外部地址。
- WebUI 只允许修改目录、视图、阅览方式和扩展名等白名单配置；不能修改 OpenList API 地址或 token 文件。
- WebUI 配置备份会移除 OpenList token 文件和管理 token 文件路径，不包含任何 token 内容。
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
