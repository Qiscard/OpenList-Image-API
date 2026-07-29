#!/usr/bin/env python3
"""Interactive manager for OpenList Image API."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from openlist_image_api import (
    ALLOWED_DELIVERY,
    ALLOWED_LAYOUTS,
    DEFAULT_CONFIG,
    OpenListClient,
    atomic_write_json,
    load_config,
    normalize_directories,
    write_secret,
)

CONFIG_PATH = Path("/etc/openlist-image-api/config.json")
TOKEN_PATH = Path("/etc/openlist-image-api/openlist.token")
ADMIN_TOKEN_PATH = Path("/etc/openlist-image-api/admin.token")
APP_PATH = Path("/opt/openlist-image-api/openlist_image_api.py")
SERVICE_NAME = "openlist-image-api"
SERVICE_USER = "openlist-image"
OFFICIAL_INSTALLER_URL = "https://res.oplist.org/script/v4.sh"


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("请使用 sudo 运行管理命令")


def pause() -> None:
    input("\n按 Enter 继续…")


def clear() -> None:
    print("\033[2J\033[H", end="")


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=check)


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
    token = input("OpenList API token（输入内容不会显示在菜单中）: ").strip()
    if not token:
        raise ValueError("token 不能为空")
    write_secret(TOKEN_PATH, token)
    make_service_owned(TOKEN_PATH)
    print("OpenList token 已保存。")


def install_openlist() -> None:
    require_root()
    answer = input("将运行 OpenList 官方安装脚本，继续？[y/N] ").strip().lower()
    if answer != "y":
        return
    with tempfile.TemporaryDirectory(prefix="openlist-installer-") as temporary:
        script = Path(temporary) / "install-openlist-v4.sh"
        print("正在下载官方 OpenList v4 安装脚本…")
        urllib.request.urlretrieve(OFFICIAL_INSTALLER_URL, script)
        run(["bash", str(script)])
    print("OpenList 安装脚本已执行。请在 OpenList 初始化完成后回到本菜单设置 token。")


def configure_port() -> None:
    require_root()
    config = read_config()
    raw = input(f"图片 API 端口 [{config['listen_port']}]: ").strip()
    if not raw:
        return
    port = int(raw)
    if not 1024 <= port <= 65535:
        raise ValueError("端口必须在 1024-65535 范围内")
    config["listen_port"] = port
    write_config(config)
    print("端口已保存；重启服务后生效。")


def configure_openlist_port() -> None:
    require_root()
    config = read_config()
    current = config["openlist_api_url"].rsplit(":", 1)[-1]
    raw = input(f"OpenList 本地 API 端口 [{current}]: ").strip()
    if not raw:
        return
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1-65535 范围内")
    config["openlist_api_url"] = f"http://127.0.0.1:{port}"
    write_config(config)
    print("OpenList API 地址已保存。")


def manage_directories() -> None:
    require_root()
    config = read_config()
    client = OpenListClient(config)
    current = "/"
    selected = list(config["directories"])
    while True:
        clear()
        print("OpenList 图片目录选择（可多选）")
        print(f"当前目录: {current}")
        print("已选择:", ", ".join(selected) if selected else "无")
        try:
            directories = [item for item in client.list_directory(current) if item.get("is_dir")]
        except RuntimeError as error:
            print(f"\n无法读取目录: {error}")
            pause()
            return
        for index, item in enumerate(directories, start=1):
            print(f"  {index:>2}. {item.get('name', '')}")
        print("\n命令：数字进入目录；a 数字 添加；r 数字 移除；u 返回上级；m 手动添加；s 保存；q 取消")
        command = input("> ").strip()
        if command == "q":
            return
        if command == "s":
            config["directories"] = normalize_directories(selected)
            write_config(config)
            print("目录选择已保存。")
            pause()
            return
        if command == "u":
            current = "/" if current == "/" else current.rsplit("/", 1)[0] or "/"
            continue
        if command == "m":
            value = input("OpenList 虚拟目录: ").strip()
            if value and value not in selected:
                selected = normalize_directories(selected + [value])
            continue
        parts = command.split(maxsplit=1)
        if len(parts) == 1 and parts[0].isdigit():
            index = int(parts[0]) - 1
            if 0 <= index < len(directories):
                name = str(directories[index].get("name") or "")
                current = (current.rstrip("/") + "/" + name) if current != "/" else "/" + name
            continue
        if len(parts) == 2 and parts[0] in {"a", "r"} and parts[1].isdigit():
            index = int(parts[1]) - 1
            if 0 <= index < len(directories):
                name = str(directories[index].get("name") or "")
                path = (current.rstrip("/") + "/" + name) if current != "/" else "/" + name
                if parts[0] == "a" and path not in selected:
                    selected.append(path)
                if parts[0] == "r":
                    selected = [item for item in selected if item != path]


def configure_view() -> None:
    require_root()
    config = read_config()
    layout = input(f"视图 single/grid/waterfall [{config['view_layout']}]: ").strip().lower() or config["view_layout"]
    delivery = input(f"阅览 preview/download [{config['delivery']}]: ").strip().lower() or config["delivery"]
    if layout not in ALLOWED_LAYOUTS or delivery not in ALLOWED_DELIVERY:
        raise ValueError("视图或阅览方式无效")
    config["view_layout"] = layout
    config["delivery"] = delivery
    write_config(config)
    print("浏览设置已保存。")


def request_status(config: dict[str, Any]) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{config['listen_port']}/api/status", timeout=3) as response:
        return json.load(response)


def show_status() -> None:
    config = read_config()
    result = run(["systemctl", "is-active", SERVICE_NAME], check=False)
    print(f"图片 API 服务: {result.stdout.strip() or 'unknown'}")
    print(f"图片 API 端口: {config['listen_port']}（仅监听本机）")
    print(f"已配置目录: {len(config['directories'])}")
    try:
        status = request_status(config)
        print(f"已索引图片: {status['image_count']}")
        print(f"索引目录数: {status['directory_count']}")
        print(f"索引重建中: {'是' if status['refreshing'] else '否'}")
        if status["last_refresh_error"]:
            print(f"最近错误: {status['last_refresh_error']}")
    except Exception as error:
        print(f"无法获取 API 状态: {error}")


def rebuild_index() -> None:
    require_root()
    run([sys.executable, str(APP_PATH), "--config", str(CONFIG_PATH), "refresh"])


def print_admin_token() -> None:
    require_root()
    print(ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip())


def ensure_admin_token() -> None:
    if not ADMIN_TOKEN_PATH.exists():
        run([sys.executable, str(APP_PATH), "--config", str(CONFIG_PATH), "create-admin-token"])
        make_service_owned(ADMIN_TOKEN_PATH)


def main_menu() -> None:
    actions = {
        "1": install_openlist,
        "2": set_openlist_token,
        "3": configure_openlist_port,
        "4": manage_directories,
        "5": configure_view,
        "6": configure_port,
        "7": lambda: service_action("start"),
        "8": lambda: service_action("restart"),
        "9": rebuild_index,
        "10": show_status,
        "11": print_admin_token,
    }
    while True:
        clear()
        print("╔══════════════════════════════════════════════╗")
        print("║            OpenList 图片 API 管理             ║")
        print("╠══════════════════════════════════════════════╣")
        print("║  1. 安装 / 部署 OpenList                      ║")
        print("║  2. 设置 OpenList API token                   ║")
        print("║  3. 设置 OpenList 本地 API 端口               ║")
        print("║  4. 选择图片目录（可多选）                    ║")
        print("║  5. 设置视图与阅览方式                        ║")
        print("║  6. 设置图片 API 端口                         ║")
        print("║  7. 启动图片 API 服务                         ║")
        print("║  8. 重启图片 API 服务                         ║")
        print("║  9. 重建图片索引                              ║")
        print("║ 10. 查看状态                                  ║")
        print("║ 11. 显示 WebUI 管理令牌                       ║")
        print("║  0. 退出                                      ║")
        print("╚══════════════════════════════════════════════╝")
        choice = input("选择 [0-11]: ").strip()
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
