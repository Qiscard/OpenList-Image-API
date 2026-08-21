#!/usr/bin/env python3
"""Interactive manager for OpenList Image API."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from openlist_image_api import atomic_write_json, load_config, write_secret

CONFIG_PATH = Path("/etc/openlist-image-api/config.json")
TOKEN_PATH = Path("/etc/openlist-image-api/openlist.token")
ADMIN_TOKEN_PATH = Path("/etc/openlist-image-api/admin.token")
APP_PATH = Path("/opt/openlist-image-api/openlist_image_api.py")
APP_INSTALLER_PATH = Path("/opt/openlist-image-api/install.sh")
SERVICE_NAME = "openlist-image-api"
SERVICE_USER = "openlist-image"
LEGACY_SERVICE_NAME = "openlist-random-image"
LEGACY_ARTIFACTS = (
    Path("/opt/openlist-random-image"),
    Path("/etc/openlist-random-image.json"),
    Path("/var/lib/openlist-random-image"),
    Path("/var/cache/openlist-random-image"),
    Path("/usr/local/bin/openlistapi"),
    Path("/etc/systemd/system/openlist-random-image.service"),
    Path("/tmp/openlist_webui.py"),
    Path("/tmp/webui.log"),
    Path("/tmp/index_build.log"),
)
LEGACY_NGINX_DIRS = (Path("/etc/nginx/conf.d"), Path("/www/server/panel/vhost/nginx"))
MIGRATION_DIR = Path("/tmp")


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("请使用 sudo 运行管理命令")


def pause() -> None:
    input("\n按 Enter 继续…")


def clear() -> None:
    print("\033[2J\033[H", end="")


def run(
    command: list[str],
    check: bool = True,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=check, capture_output=capture_output, env=env)


def command_output(command: list[str]) -> str:
    result = run(command, check=False, capture_output=True)
    return (result.stdout or "").strip()


def service_action(action: str) -> None:
    require_root()
    run(["systemctl", action, SERVICE_NAME])


def read_config() -> dict[str, Any]:
    return load_config(CONFIG_PATH)


def make_service_owned(path: Path) -> None:
    run(["chown", f"{SERVICE_USER}:{SERVICE_USER}", str(path)])


def write_config(config: dict[str, Any]) -> None:
    atomic_write_json(CONFIG_PATH, config)
    make_service_owned(CONFIG_PATH)


def set_openlist_token() -> None:
    require_root()
    token = getpass.getpass("OpenList API token（输入不会回显）: ").strip()
    if not token:
        raise ValueError("token 不能为空")
    write_secret(TOKEN_PATH, token)
    make_service_owned(TOKEN_PATH)
    print("OpenList token 已保存。")


def install_openlist() -> None:
    require_root()
    if not APP_INSTALLER_PATH.is_file():
        raise RuntimeError("内置安装器缺失，请重新运行本项目安装命令")
    print("OpenList 下载方式：")
    print("  1. 直连下载（默认）")
    print("  2. 自动测速镜像并使用最快可用项")
    choice = input("选择 [1-2]: ").strip() or "1"
    modes = {"1": "direct", "2": "auto"}
    if choice not in modes:
        raise ValueError("无效的下载方式")
    run(["bash", str(APP_INSTALLER_PATH), "--install-openlist", "--openlist-download", modes[choice]])
    print("OpenList 内置安装流程已完成。请在 OpenList 初始化完成后回到本菜单设置 token。")


def update_application(source: str) -> None:
    require_root()
    if source not in {"github", "gitee"}:
        raise ValueError("无效的更新来源")
    if not APP_INSTALLER_PATH.is_file():
        raise RuntimeError("内置安装器缺失，请重新运行本项目安装命令")
    print(f"正在从 {source} 拉取最新脚本并更新图片 API；服务会恢复到更新前的启用和运行状态。")
    run(["bash", str(APP_INSTALLER_PATH), "--source", source, "--update"])
    print("更新完成。当前 TUI 会话仍使用旧代码；退出后重新运行即可使用新菜单。")


def uninstall_application() -> None:
    require_root()
    if not APP_INSTALLER_PATH.is_file():
        raise RuntimeError("内置安装器缺失，无法执行安全卸载")
    print("卸载方式：")
    print("  1. 仅卸载图片 API（服务、启动命令、脚本、配置和索引）")
    print("  2. 完全卸载（图片 API 与本项目安装的 OpenList）")
    choice = input("选择 [1-2]: ").strip()
    scopes = {"1": "api", "2": "complete"}
    if choice not in scopes:
        raise ValueError("无效的卸载方式")
    if input("此操作不可恢复，输入 YES 确认: ").strip() != "YES":
        print("已取消卸载。")
        return
    run(["bash", str(APP_INSTALLER_PATH), "--uninstall", scopes[choice]])
    print("卸载完成。")


def maintenance_menu() -> None:
    require_root()
    while True:
        clear()
        print("维护工具")
        print("  1. 更新项目（github）")
        print("  2. 更新项目（gitee）")
        print("  3. 卸载")
        print("  4. 清理旧 API 残留 / 运行缓存")
        print("  5. 全局迁移")
        print("  0. 返回")
        choice = input("选择 [0-5]: ").strip()
        if choice == "0":
            return
        if choice == "1":
            update_application("github")
        elif choice == "2":
            update_application("gitee")
        elif choice == "3":
            uninstall_application()
        elif choice == "4":
            cleanup_residuals_and_runtime_cache()
        elif choice == "5":
            export_global_migration()
        else:
            raise ValueError("无效的维护操作")
        pause()


def configure_port() -> None:
    require_root()
    config = read_config()
    raw = input(f"图片 API 端口 [{config['listen_port']}]: ").strip()
    if not raw:
        return
    port = int(raw)
    if not 1024 <= port <= 65535:
        raise ValueError("端口必须在 1024-65535 范围内")
    listeners = listeners_on_port(port)
    if listeners and port != int(config["listen_port"]):
        print("检测到该端口已被占用：")
        print(listeners)
        raise RuntimeError("请先处理端口冲突后再修改")
    config["listen_port"] = port
    write_config(config)
    print("端口已保存；重启服务后生效。")


def configure_listen_host() -> None:
    require_root()
    config = read_config()
    print("图片 API 监听范围：")
    print("  1. 全部网络接口（适用于公网或服务器商 NAT 转发）")
    print("  2. 仅本机回环")
    current = "1" if config["listen_host"] == "0.0.0.0" else "2"
    choice = input(f"选择 [1-2] [{current}]: ").strip() or current
    hosts = {"1": "0.0.0.0", "2": "127.0.0.1"}
    if choice not in hosts:
        raise ValueError("无效的监听范围")
    config["listen_host"] = hosts[choice]
    write_config(config)
    print("监听范围已保存；重启服务后生效。")


def service_management() -> None:
    require_root()
    while True:
        clear()
        print("图片 API 服务管理")
        print("  1. 设置 API 监听范围")
        print("  2. 启动图片 API 服务")
        print("  3. 停止图片 API 服务")
        print("  4. 重启图片 API 服务")
        print("  0. 返回")
        choice = input("选择 [0-4]: ").strip()
        if choice == "0":
            return
        if choice == "1":
            configure_listen_host()
        elif choice == "2":
            service_action("start")
        elif choice == "3":
            service_action("stop")
        elif choice == "4":
            service_action("restart")
        else:
            raise ValueError("无效的服务管理操作")
        pause()


def listeners_on_port(port: int) -> str:
    if shutil.which("ss"):
        result = run(["ss", "-ltnp"], check=False, capture_output=True)
        output = result.stdout or ""
        lines = [line for line in output.splitlines() if f":{port}" in line]
        return "\n".join(lines)
    if shutil.which("lsof"):
        return command_output(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
    return "无法检测（缺少 ss 或 lsof）"


def legacy_nginx_artifacts() -> list[Path]:
    artifacts: list[Path] = []
    for directory in LEGACY_NGINX_DIRS:
        if not directory.is_dir():
            continue
        for candidate in directory.glob("*_random_image.conf"):
            try:
                content = candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "127.0.0.1:8790" in content or "openlist-random-image" in content:
                artifacts.append(candidate)
    return artifacts


def detect_legacy_residuals() -> dict[str, Any]:
    config = read_config()
    files = [path for path in LEGACY_ARTIFACTS if path.exists()]
    nginx_files = legacy_nginx_artifacts()
    legacy_state = command_output(["systemctl", "is-active", LEGACY_SERVICE_NAME]) or "not-found"
    legacy_enabled = command_output(["systemctl", "is-enabled", LEGACY_SERVICE_NAME]) or "not-found"
    ports = {port: listeners_on_port(port) for port in sorted({8790, 8791, int(config["listen_port"])})}
    return {"files": files, "nginx_files": nginx_files, "legacy_state": legacy_state, "legacy_enabled": legacy_enabled, "ports": ports}


def show_residuals() -> dict[str, Any]:
    residuals = detect_legacy_residuals()
    print("旧图片 API 服务:", residuals["legacy_state"])
    print("旧服务开机状态:", residuals["legacy_enabled"])
    if residuals["files"] or residuals["nginx_files"]:
        print("检测到旧文件残留：")
        for path in [*residuals["files"], *residuals["nginx_files"]]:
            print(f"  - {path}")
    else:
        print("未检测到已知旧文件残留。")
    print("端口监听：")
    for port, output in residuals["ports"].items():
        print(f"  {port}: {output or '未监听'}")
    return residuals


def cleanup_legacy_residuals() -> None:
    require_root()
    residuals = show_residuals()
    artifacts = [*residuals["files"], *residuals["nginx_files"]]
    legacy_present = residuals["legacy_state"] not in {"inactive", "not-found", "unknown"} or residuals["legacy_enabled"] == "enabled"
    if not artifacts and not legacy_present:
        print("没有可自动清理的旧图片 API 残留。")
        return
    answer = input("将停用旧服务并删除以上旧 API 文件，继续？[y/N] ").strip().lower()
    if answer != "y":
        return
    run(["systemctl", "disable", "--now", LEGACY_SERVICE_NAME], check=False)
    for path in artifacts:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            print(f"已清理: {path}")
        except OSError as error:
            print(f"无法清理 {path}: {error}")
    run(["systemctl", "daemon-reload"], check=False)
    if residuals["nginx_files"]:
        run(["systemctl", "reload", "nginx"], check=False)
    print("旧 API 清理完成。未识别的端口占用不会被自动终止，请自行确认后处理。")


def cleanup_residuals_and_runtime_cache() -> None:
    require_root()
    cleanup_legacy_residuals()
    cache_path: Path | None = None
    try:
        cache_path = Path(read_config()["state_dir"]) / "url_cache.json"
    except (OSError, KeyError, TypeError, ValueError):
        pass
    if cache_path is not None:
        try:
            cache_path.unlink()
            print(f"已清理: {cache_path}")
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"无法清理 {cache_path}: {error}")
    if command_output(["systemctl", "is-active", SERVICE_NAME]) == "active":
        run(["systemctl", "restart", SERVICE_NAME])
        print("已重启图片 API，内存和持久化签名链接缓存已清理。")
    else:
        print("图片 API 当前未运行；持久化缓存已清理（如文件存在）。")


def request_status(config: dict[str, Any]) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{config['listen_port']}/api/status", timeout=3) as response:
        return json.load(response)


def show_status() -> None:
    config = read_config()
    state = command_output(["systemctl", "is-active", SERVICE_NAME]) or "unknown"
    print(f"图片 API 服务: {state}")
    scope = "全部网络接口（可通过 NAT 转发访问）" if config["listen_host"] == "0.0.0.0" else "仅本机回环"
    print(f"图片 API 端口: {config['listen_port']}（{scope}）")
    print(f"已配置目录: {len(config['directories'])}")
    try:
        status = request_status(config)
        print(f"已索引图片: {status['image_count']}")
        print(f"索引目录数: {status['directory_count']}")
        print(f"索引重建中: {'是' if status['refreshing'] else '否'}")
        duration = float(status.get("last_build_duration_seconds") or 0)
        if duration:
            print(f"上次索引耗时: 约 {duration:.1f} 秒")
        if status["last_refresh_error"]:
            print(f"最近错误: {status['last_refresh_error']}")
    except Exception as error:
        print(f"无法获取 API 状态: {error}")


def show_status_with_admin_token() -> None:
    show_status()
    try:
        token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"无法读取 WebUI 管理令牌: {error}")
        return
    print(f"WebUI 管理令牌: {token}")


def export_global_migration() -> Path:
    require_root()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = MIGRATION_DIR / f"openlist-image-api-migration-{timestamp}.tar.gz"
    MIGRATION_DIR.mkdir(parents=True, exist_ok=True)
    state_dir = Path("/var/lib/openlist-image-api")
    try:
        state_dir = Path(read_config()["state_dir"])
    except (OSError, KeyError, TypeError, ValueError):
        pass
    with tarfile.open(archive_path, "w:gz") as archive:
        if CONFIG_PATH.is_file():
            archive.add(CONFIG_PATH, arcname="config.json")
        for name in ("index.json", "tags.json", "url_cache.json", "index.checkpoint.json"):
            path = state_dir / name
            if path.is_file():
                archive.add(path, arcname=name)
        for name in ("openlist.token", "admin.token"):
            info = tarfile.TarInfo(name)
            info.size = 0
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(b""))
    print(f"迁移包已写入: {archive_path}")
    return archive_path


def print_admin_token() -> None:
    require_root()
    print(ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip())


def ensure_admin_token() -> None:
    if not ADMIN_TOKEN_PATH.exists():
        require_root()
        run([sys.executable, str(APP_PATH), "--config", str(CONFIG_PATH), "create-admin-token"])
        make_service_owned(ADMIN_TOKEN_PATH)


def main_menu() -> None:
    actions = {
        "1": install_openlist,
        "2": set_openlist_token,
        "3": configure_port,
        "4": service_management,
        "5": show_status_with_admin_token,
        "6": maintenance_menu,
    }
    while True:
        clear()
        print("╔══════════════════════════════════════════════╗")
        print("║            OpenList 图片 API 管理             ║")
        print("╠══════════════════════════════════════════════╣")
        print("║  1. 安装 / 部署 OpenList                      ║")
        print("║  2. 设置 OpenList API token                   ║")
        print("║  3. 设置图片 API 端口                         ║")
        print("║  4. 图片 API 服务管理                         ║")
        print("║  5. 查看状态 / WebUI 管理令牌                 ║")
        print("║  6. 维护（更新 / 卸载 / 清理 / 全局迁移）     ║")
        print("║  0. 退出                                      ║")
        print("╚══════════════════════════════════════════════╝")
        choice = input("选择 [0-6]: ").strip()
        if choice == "0":
            return
        action = actions.get(choice)
        if not action:
            continue
        try:
            action()
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
            print(f"操作失败: {error}")
        pause()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-admin-token", action="store_true")
    args = parser.parse_args()
    if args.print_admin_token:
        print_admin_token()
        return
    ensure_admin_token()
    main_menu()


if __name__ == "__main__":
    main()
