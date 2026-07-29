# OpenList Image API

一个可通过 GitHub 或 Gitee 一键安装的 OpenList 图片 API、终端管理界面和 WebUI。项目只保存通用程序逻辑与默认配置；不包含任何用户特定数据、访问令牌或真实网络地址。

## 特性

- 终端 TUI：安装 OpenList、设置 token 和端口、选择多个图片目录、查看状态、启动/重启服务、重建索引、检测并清理旧 API 残留。
- 安全的图片服务：默认仅在本机监听；OpenList API 仅允许配置为本地服务，避免 token 被转发到外部地址。
- WebUI：支持多选 OpenList 目录、单张 / 多张网格 / 瀑布流视图、直接预览 / 下载预览和无 token 的配置备份下载。
- 管理安全：WebUI 配置接口必须提供安装时生成的管理令牌；公开图片接口不会返回管理配置或 token。
- 可维护性：纯 Python 标准库、原子索引写入、分页目录扫描、LRU 签名链接缓存、systemd 低权限服务、LF 行尾和单元测试。

## 安装

使用固定版本安装器（GitHub）：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiscard/OpenList-Image-API/v1.0.2/install.sh | sudo bash -s -- --source github
```

使用固定版本安装器（Gitee）：

```bash
curl -fsSL https://gitee.com/qiscard/OpenList-Image-API/raw/v1.0.2/install.sh | sudo bash -s -- --source gitee
```

`--source auto` 会优先使用 Gitee，失败时回退到 GitHub。安装器会创建图片 API 服务和全局管理命令，但不会自动安装 OpenList。首次运行 TUI 后，选择菜单中的“安装 / 部署 OpenList”即可执行官方命令。

已核验 OpenList 官方 v4 管理脚本：其程序下载源为 GitHub Releases，并支持传入 GitHub 代理；目前没有可验证的上游 Gitee 发布源。因此项目不会复制一份容易过期的上游安装器，而是在 TUI 中提供代理输入，并保留官方脚本作为唯一上游来源。

## TUI

```bash
sudo openlist-image-api
```

菜单提供：

1. 安装 / 部署 OpenList。
2. 保存 OpenList API token（仅保存在本机受限文件中）。
3. 设置 OpenList 本地 API 端口。
4. 浏览并多选 OpenList 虚拟目录。
5. 设置 WebUI 视图与阅览方式。
6. 设置图片 API 端口。
7. 启动服务。
8. 重启服务。
9. 重建索引。
10. 查看服务状态。
11. 显示 WebUI 管理令牌。
12. 检测并停用、清理旧随机图片 API 残留。

配置目录或 token 后，执行“重建图片索引”使图片可被 API 选择。

## WebUI 与 API

浏览页面与管理页面随图片 API 服务一起提供。服务默认仅在本机监听；如需经反向代理公开图片浏览，请只公开浏览/API 路由，并为管理路由额外设置认证、访问控制和 TLS。

| 路由 | 用途 |
| --- | --- |
| `/` | 图片浏览页，自动按照已配置视图展示随机图片。 |
| `/admin` | 管理页面；需要输入管理令牌。 |
| `/random` | 跳转到一张随机图片。 |
| `/download?path=…` | 以下载方式跳转到已索引图片。 |
| `/api/images/random?count=…` | 获取随机图片 JSON。 |
| `/api/status` | 获取图片数量、缓存和重建状态。 |
| `/api/admin/backup` | 需要管理令牌；下载不含 token 的配置备份。 |
| `/health` | 健康检查。 |

管理令牌只能通过本机 TUI 显示：

```bash
sudo openlist-image-api --print-admin-token
```

## 安全说明

- 服务使用专用低权限系统用户运行，不使用 root 运行 API。
- 管理接口只接受目录、视图、阅览方式和文件扩展名等白名单字段；不能通过 WebUI 修改 OpenList API 地址或 token 文件。
- OpenList API 地址被限制为本地 HTTP 服务，降低 SSRF 与 token 外泄风险。
- 安装器支持 Gitee、GitHub 或自动回退来源，并通过版本标签下载源码、对运行时 Python 文件和版本文件进行 SHA-256 校验。
- 不要把管理令牌、OpenList token 或本机配置提交到 Git。

## 开发与发布

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

生成发布校验文件：

```bash
sha256sum src/openlist_image_api.py src/openlist_tui.py VERSION > SHA256SUMS
```

在创建 `v1.0.2` 标签前，将 `SHA256SUMS` 提交到仓库。后续版本需要同步更新 `VERSION`、`install.sh` 中的 `RELEASE_REF`、校验文件，并向 GitHub 与 Gitee 推送同名标签。

## License

MIT
