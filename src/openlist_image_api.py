#!/usr/bin/env python3
"""Secure, dependency-free random-image API for OpenList."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import random
import secrets
import threading
import time
from collections import OrderedDict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

DEFAULT_CONFIG: dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 8790,
    "openlist_api_url": "http://127.0.0.1:5244",
    "openlist_token_file": "/etc/openlist-image-api/openlist.token",
    "state_dir": "/var/lib/openlist-image-api",
    "directories": [],
    "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"],
    "view_layout": "single",
    "delivery": "preview",
    "url_cache_size": 200,
    "url_cache_ttl_seconds": 240,
    "admin_token_file": "/etc/openlist-image-api/admin.token",
}
ALLOWED_LAYOUTS = {"single", "grid", "waterfall"}
ALLOWED_DELIVERY = {"preview", "download"}
MAX_REQUEST_BODY = 64 * 1024


def normalize_directory(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("directory must be a string")
    parts = [part for part in value.strip().split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("directory cannot contain '..'")
    return "/" + "/".join(parts)


def normalize_directories(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("directories must be a list")
    normalized = []
    for value in values:
        directory = normalize_directory(value)
        if directory not in normalized:
            normalized.append(directory)
    return normalized


def is_loopback_openlist_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.port is not None
        and 1 <= parsed.port <= 65535
        and not parsed.username
        and not parsed.password
        and not parsed.path.rstrip("/")
    )


def validate_config(candidate: dict[str, Any]) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    config.update(candidate)
    if config["listen_host"] != "127.0.0.1":
        raise ValueError("listen_host must remain 127.0.0.1")
    if not isinstance(config["listen_port"], int) or not 1024 <= config["listen_port"] <= 65535:
        raise ValueError("listen_port must be between 1024 and 65535")
    if not is_loopback_openlist_url(config["openlist_api_url"]):
        raise ValueError("openlist_api_url must point to a local HTTP OpenList service")
    config["directories"] = normalize_directories(config["directories"])
    if config["view_layout"] not in ALLOWED_LAYOUTS:
        raise ValueError("invalid view_layout")
    if config["delivery"] not in ALLOWED_DELIVERY:
        raise ValueError("invalid delivery")
    if not isinstance(config["url_cache_size"], int) or not 0 <= config["url_cache_size"] <= 5000:
        raise ValueError("invalid url_cache_size")
    if not isinstance(config["url_cache_ttl_seconds"], int) or not 0 <= config["url_cache_ttl_seconds"] <= 3600:
        raise ValueError("invalid url_cache_ttl_seconds")
    if not isinstance(config["extensions"], list) or not config["extensions"]:
        raise ValueError("extensions must be a non-empty list")
    config["extensions"] = sorted(
        {str(extension).lower() for extension in config["extensions"] if str(extension).startswith(".")}
    )
    if not config["extensions"]:
        raise ValueError("extensions must contain dotted extensions")
    for key in ("openlist_token_file", "state_dir", "admin_token_file"):
        if not isinstance(config[key], str) or not config[key].startswith("/"):
            raise ValueError(f"{key} must be an absolute system path")
    return config


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return validate_config({})
    try:
        candidate = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid configuration: {error}") from error
    if not isinstance(candidate, dict):
        raise RuntimeError("configuration must be a JSON object")
    return validate_config(candidate)


def atomic_write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def read_secret(path: Path, name: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"unable to read {name}") from error
    if not value:
        raise RuntimeError(f"{name} is empty")
    return value


def write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value.strip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class OpenListClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = config["openlist_api_url"].rstrip("/")
        self.token_path = Path(config["openlist_token_file"])

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = read_secret(self.token_path, "OpenList API token")
        request = Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": token, "Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    result = json.load(response)
                if result.get("code") != 200:
                    raise RuntimeError(result.get("message") or "OpenList rejected request")
                data = result.get("data") or {}
                if not isinstance(data, dict):
                    raise RuntimeError("OpenList returned invalid data")
                return data
            except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise RuntimeError(f"OpenList request failed: {last_error}")

    def list_directory(self, path: str) -> list[dict[str, Any]]:
        page = 1
        entries: list[dict[str, Any]] = []
        while True:
            data = self._post(
                "/api/fs/list",
                {"path": path, "password": "", "page": page, "per_page": 1000, "refresh": False},
            )
            content = data.get("content") or []
            if not isinstance(content, list):
                raise RuntimeError("OpenList returned invalid directory content")
            entries.extend(item for item in content if isinstance(item, dict))
            total = int(data.get("total") or len(entries))
            if not content or len(entries) >= total:
                return entries
            page += 1

    def resolve_file(self, path: str) -> str:
        data = self._post("/api/fs/get", {"path": path, "password": "", "refresh": False})
        url = str(data.get("raw_url") or data.get("url") or "").strip()
        if not url:
            raise RuntimeError("OpenList did not return a file URL")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("OpenList returned an invalid file URL")
        return url


class IndexRepository:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "index.json"
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {"images": [], "directories": [], "generated_at": 0, "errors": []}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"unable to read image index: {error}") from error
            if not isinstance(data, dict) or not isinstance(data.get("images"), list):
                raise RuntimeError("image index is invalid")
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            atomic_write_json(self.path, data)


def join_virtual_path(parent: str, child: str) -> str:
    return normalize_directory(f"{parent.rstrip('/')}/{child}")


def build_index(config: dict[str, Any], repository: IndexRepository) -> dict[str, Any]:
    client = OpenListClient(config)
    extensions = set(config["extensions"])
    queue: deque[str] = deque(config["directories"])
    visited: set[str] = set()
    images: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        try:
            entries = client.list_directory(current)
        except RuntimeError as error:
            logging.warning("Skipping directory %s: %s", current, error)
            errors.append({"directory": current, "error": str(error)})
            continue
        for entry in entries:
            name = str(entry.get("name") or "")
            if not name or "/" in name or "\\" in name:
                continue
            path = join_virtual_path(current, name)
            if entry.get("is_dir"):
                queue.append(path)
            elif Path(name).suffix.lower() in extensions:
                try:
                    size = max(0, int(entry.get("size") or 0))
                except (TypeError, ValueError):
                    size = 0
                images.append({"path": path, "size": size})

    index = {
        "version": 1,
        "generated_at": int(time.time()),
        "directories": config["directories"],
        "directory_count": len(visited),
        "image_count": len(images),
        "errors": errors,
        "images": images,
    }
    repository.save(index)
    return index


class UrlCache:
    def __init__(self, max_size: int, ttl_seconds: int):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def resolve(self, path: str, client: OpenListClient) -> str:
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(path)
            if cached and now - cached[0] < self.ttl_seconds:
                self.hits += 1
                self._entries.move_to_end(path)
                return cached[1]
            if cached:
                self._entries.pop(path, None)
            self.misses += 1
        url = client.resolve_file(path)
        if self.max_size:
            with self._lock:
                self._entries[path] = (now, url)
                self._entries.move_to_end(path)
                while len(self._entries) > self.max_size:
                    self._entries.popitem(last=False)
        return url

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._entries), "hits": self.hits, "misses": self.misses}


class Application:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.repository = IndexRepository(Path(self.config["state_dir"]))
        self.cache = UrlCache(self.config["url_cache_size"], self.config["url_cache_ttl_seconds"])
        self.refresh_lock = threading.Lock()
        self.refreshing = False
        self.last_refresh_error = ""

    def reload_config(self) -> dict[str, Any]:
        self.config = load_config(self.config_path)
        self.repository = IndexRepository(Path(self.config["state_dir"]))
        self.cache = UrlCache(self.config["url_cache_size"], self.config["url_cache_ttl_seconds"])
        return self.config

    def public_config(self) -> dict[str, Any]:
        return {"view_layout": self.config["view_layout"], "delivery": self.config["delivery"]}

    def admin_config(self) -> dict[str, Any]:
        return {
            "directories": self.config["directories"],
            "view_layout": self.config["view_layout"],
            "delivery": self.config["delivery"],
            "extensions": self.config["extensions"],
        }

    def update_admin_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"directories", "view_layout", "delivery", "extensions"}
        if set(payload) - allowed:
            raise ValueError("unsupported configuration field")
        candidate = self.config.copy()
        candidate.update(payload)
        validated = validate_config(candidate)
        atomic_write_json(self.config_path, validated)
        self.reload_config()
        return self.admin_config()

    def is_admin(self, supplied_token: str | None) -> bool:
        if not supplied_token:
            return False
        expected = read_secret(Path(self.config["admin_token_file"]), "admin token")
        return hmac.compare_digest(supplied_token, expected)

    def start_refresh(self) -> bool:
        if not self.refresh_lock.acquire(blocking=False):
            return False
        self.refreshing = True

        def worker() -> None:
            try:
                build_index(self.config, self.repository)
                self.last_refresh_error = ""
            except Exception as error:  # logged and visible through status
                logging.exception("Index rebuild failed")
                self.last_refresh_error = str(error)
            finally:
                self.refreshing = False
                self.refresh_lock.release()

        threading.Thread(target=worker, name="openlist-index-rebuild", daemon=True).start()
        return True

    def status(self) -> dict[str, Any]:
        try:
            index = self.repository.load()
        except RuntimeError as error:
            index = {"images": [], "directory_count": 0, "generated_at": 0, "errors": [str(error)]}
        return {
            "status": "ok",
            "image_count": len(index.get("images", [])),
            "directory_count": int(index.get("directory_count") or 0),
            "generated_at": int(index.get("generated_at") or 0),
            "refreshing": self.refreshing,
            "last_refresh_error": self.last_refresh_error,
            "cache": self.cache.status(),
            **self.public_config(),
        }

    def list_directories(self, path: str) -> list[dict[str, str]]:
        directory = normalize_directory(path)
        client = OpenListClient(self.config)
        entries = client.list_directory(directory)
        results = []
        for entry in entries:
            name = str(entry.get("name") or "")
            if entry.get("is_dir") and name and "/" not in name and "\\" not in name:
                results.append({"name": name, "path": join_virtual_path(directory, name)})
        return sorted(results, key=lambda item: item["name"].casefold())

    def choose_images(self, count: int, folder: str | None, min_size: int | None, max_size: int | None) -> list[dict[str, Any]]:
        index = self.repository.load()
        images = index.get("images", [])
        if folder:
            folder = normalize_directory(folder)
            prefix = folder.rstrip("/") + "/"
            images = [image for image in images if image.get("path", "").startswith(prefix)]
        if min_size is not None:
            images = [image for image in images if int(image.get("size") or 0) >= min_size]
        if max_size is not None:
            images = [image for image in images if int(image.get("size") or 0) <= max_size]
        if not images:
            return []
        count = max(1, min(count, 50))
        if count <= len(images):
            return random.sample(images, count)
        return [random.choice(images) for _ in range(count)]

    def indexed_image(self, path: str) -> dict[str, Any]:
        normalized = normalize_directory(path)
        for image in self.repository.load().get("images", []):
            if image.get("path") == normalized:
                return image
        raise ValueError("image is not in the current index")

    def resolve_images(self, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        client = OpenListClient(self.config)
        results = []
        for image in images:
            path = str(image["path"])
            results.append({"path": path, "size": int(image.get("size") or 0), "url": self.cache.resolve(path, client)})
        return results


def parse_size(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    units = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3}
    suffix = ""
    while text and text[-1].isalpha():
        suffix = text[-1] + suffix
        text = text[:-1]
    if suffix not in units or not text:
        raise ValueError("invalid size")
    number = float(text)
    if number < 0:
        raise ValueError("invalid size")
    return int(number * units[suffix])


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def gallery_html() -> str:
    return """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>OpenList 图片浏览</title><style>
body{margin:0;background:#10131a;color:#e7edf7;font:15px system-ui,sans-serif}header{display:flex;gap:12px;align-items:center;padding:18px;position:sticky;top:0;background:#10131ae8;border-bottom:1px solid #293040}button{background:#4b8cff;border:0;border-radius:7px;color:white;padding:9px 13px;cursor:pointer}.gallery{padding:18px;gap:15px}.single{display:grid;place-items:center}.single img{max-height:75vh;max-width:95vw}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}.waterfall{columns:4 220px}.card{break-inside:avoid;margin:0 0 15px;background:#171c27;border-radius:9px;overflow:hidden}.card img{width:100%;display:block}.card p{margin:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{color:#a9b7cd;font-size:13px}.download{display:inline-block;margin:0 8px 10px;color:#b7d1ff}a{color:inherit}</style></head><body><header><strong>OpenList 图片浏览</strong><span class=\"meta\" id=\"status\"></span><button id=\"refresh\">刷新</button><a href=\"/admin\">管理</a></header><main id=\"gallery\" class=\"gallery\"></main><script>
const gallery=document.querySelector('#gallery'),statusEl=document.querySelector('#status');
async function load(){const status=await (await fetch('/api/status')).json();statusEl.textContent=`${status.image_count} 张图片`;gallery.className='gallery '+status.view_layout;const count=status.view_layout==='single'?1:status.view_layout==='grid'?12:24;const data=await (await fetch('/api/images/random?count='+count)).json();gallery.replaceChildren(...data.images.map(image=>{const card=document.createElement('article');card.className='card';const anchor=document.createElement('a');anchor.href=image.url;anchor.target='_blank';const img=document.createElement('img');img.src=image.url;img.loading='lazy';img.alt=image.path;anchor.append(img);card.append(anchor);const caption=document.createElement('p');caption.textContent=image.path;card.append(caption);if(status.delivery==='download'){const download=document.createElement('a');download.href='/download?path='+encodeURIComponent(image.path);download.className='download';download.textContent='下载';card.append(download)}return card;}));}
document.querySelector('#refresh').onclick=load;load().catch(error=>statusEl.textContent='加载失败：'+error.message);
</script></body></html>"""


def admin_html() -> str:
    return """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>OpenList 图片管理</title><style>body{max-width:880px;margin:30px auto;padding:0 18px;background:#10131a;color:#e7edf7;font:15px system-ui,sans-serif}section{background:#171c27;border-radius:10px;padding:18px;margin:15px 0}input,select,button{padding:8px;border-radius:6px;border:1px solid #445069;background:#10131a;color:#e7edf7}button{background:#4b8cff;border:0;cursor:pointer}.row{display:flex;gap:8px;flex-wrap:wrap}.directory{display:block;padding:7px 0;border-bottom:1px solid #293040}.muted{color:#a9b7cd}</style></head><body><h1>OpenList 图片管理</h1><p class=\"muted\">输入管理令牌后才能读取或修改配置。</p><section><div class=\"row\"><input id=\"token\" type=\"password\" placeholder=\"管理令牌\"><button id=\"load\">加载配置</button></div></section><section><h2>图片目录（可多选）</h2><div class=\"row\"><input id=\"path\" value=\"/\" aria-label=\"目录\"><button id=\"browse\">浏览目录</button></div><div id=\"directories\"></div><h3>已选择</h3><div id=\"selected\"></div></section><section><h2>浏览方式</h2><div class=\"row\"><label>视图 <select id=\"layout\"><option value=\"single\">单张</option><option value=\"grid\">多张网格</option><option value=\"waterfall\">瀑布流</option></select></label><label>阅览 <select id=\"delivery\"><option value=\"preview\">直接预览</option><option value=\"download\">下载预览</option></select></label><button id=\"save\">保存配置</button><button id=\"rebuild\">重建索引</button></div><p id=\"message\" class=\"muted\"></p></section><script>
let config={directories:[]};const message=document.querySelector('#message');const auth=()=>({'X-Admin-Token':document.querySelector('#token').value,'Content-Type':'application/json'});function showSelected(){const root=document.querySelector('#selected');root.replaceChildren(...config.directories.map(path=>{const row=document.createElement('div');row.className='directory';const remove=document.createElement('button');remove.textContent='移除';remove.onclick=()=>{config.directories=config.directories.filter(item=>item!==path);showSelected()};row.append(document.createTextNode(path+' '),remove);return row;}));}
async function load(){const response=await fetch('/api/admin/config',{headers:auth()});if(!response.ok)throw new Error('令牌无效或服务不可用');config=await response.json();document.querySelector('#layout').value=config.view_layout;document.querySelector('#delivery').value=config.delivery;showSelected();message.textContent='配置已加载';}
async function browse(){const path=document.querySelector('#path').value;const response=await fetch('/api/admin/directories?path='+encodeURIComponent(path),{headers:auth()});if(!response.ok)throw new Error('无法列出目录');const data=await response.json();const root=document.querySelector('#directories');root.replaceChildren(...data.directories.map(item=>{const row=document.createElement('label');row.className='directory';const check=document.createElement('input');check.type='checkbox';check.checked=config.directories.includes(item.path);check.onchange=()=>{if(check.checked&&!config.directories.includes(item.path))config.directories.push(item.path);if(!check.checked)config.directories=config.directories.filter(path=>path!==item.path);showSelected()};row.append(check,document.createTextNode(' '+item.name+' ('+item.path+')'));return row;}));}
async function save(){const payload={directories:config.directories,view_layout:document.querySelector('#layout').value,delivery:document.querySelector('#delivery').value};const response=await fetch('/api/admin/config',{method:'PUT',headers:auth(),body:JSON.stringify(payload)});if(!response.ok)throw new Error('保存失败');config=await response.json();showSelected();message.textContent='已保存';}
async function rebuild(){const response=await fetch('/api/admin/rebuild',{method:'POST',headers:auth()});if(!response.ok)throw new Error('重建未启动');message.textContent='索引正在后台重建';}
for(const [id,fn] of Object.entries({load,browse,save,rebuild}))document.querySelector('#'+id).onclick=()=>fn().catch(error=>message.textContent=error.message);
</script></body></html>"""


def make_handler(application: Application):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenListImageAPI/1.0"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _admin_required(self) -> bool:
            try:
                allowed = application.is_admin(self.headers.get("X-Admin-Token"))
            except RuntimeError:
                allowed = False
            if not allowed:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "admin authentication required"})
            return allowed

        def _query_int(self, params: dict[str, list[str]], name: str, default: int) -> int:
            raw = params.get(name, [str(default)])[0]
            value = int(raw)
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                if parsed.path in {"/", "/gallery"}:
                    return self._send_html(gallery_html())
                if parsed.path == "/admin":
                    return self._send_html(admin_html())
                if parsed.path == "/health":
                    return self._send_json(HTTPStatus.OK, {"status": "ok"})
                if parsed.path == "/api/status":
                    return self._send_json(HTTPStatus.OK, application.status())
                if parsed.path == "/api/images/random":
                    count = self._query_int(params, "count", 1)
                    images = application.choose_images(
                        count, params.get("folder", [None])[0], parse_size(params.get("min_size", [None])[0]), parse_size(params.get("max_size", [None])[0])
                    )
                    if not images:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "no matching images"})
                    return self._send_json(HTTPStatus.OK, {"images": application.resolve_images(images)})
                if parsed.path in {"/random", "/download"}:
                    if parsed.path == "/download":
                        path = params.get("path", [""])[0]
                        images = [application.indexed_image(path)]
                    else:
                        images = application.choose_images(1, params.get("folder", [None])[0], None, None)
                    if not images:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "no matching images"})
                    url = application.resolve_images(images)[0]["url"]
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", url)
                    self.send_header("Cache-Control", "no-store")
                    if parsed.path == "/download":
                        self.send_header("Content-Disposition", "attachment")
                    self.end_headers()
                    return
                if parsed.path == "/api/admin/config":
                    if self._admin_required():
                        return self._send_json(HTTPStatus.OK, application.admin_config())
                    return
                if parsed.path == "/api/admin/directories":
                    if not self._admin_required():
                        return
                    directory = params.get("path", ["/"])[0]
                    return self._send_json(HTTPStatus.OK, {"directories": application.list_directories(directory)})
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (ValueError, RuntimeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                logging.exception("Unhandled GET error")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

        def do_PUT(self) -> None:
            if urlparse(self.path).path != "/api/admin/config":
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if not self._admin_required():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BODY:
                    raise ValueError("invalid request body size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be an object")
                self._send_json(HTTPStatus.OK, application.update_admin_config(payload))
            except (ValueError, RuntimeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/admin/rebuild":
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if not self._admin_required():
                return
            if application.start_refresh():
                self._send_json(HTTPStatus.ACCEPTED, {"status": "rebuild started"})
            else:
                self._send_json(HTTPStatus.CONFLICT, {"error": "a rebuild is already running"})

        def log_message(self, message: str, *args: object) -> None:
            logging.info("%s %s", self.client_address[0], message % args)

    return Handler


def command_serve(config_path: Path) -> None:
    application = Application(config_path)
    server = ThreadingHTTPServer((application.config["listen_host"], application.config["listen_port"]), make_handler(application))
    logging.info("Listening on %s:%d", application.config["listen_host"], application.config["listen_port"])
    try:
        server.serve_forever()
    finally:
        server.server_close()


def command_refresh(config_path: Path) -> None:
    application = Application(config_path)
    index = build_index(application.config, application.repository)
    print(json.dumps({"image_count": index["image_count"], "directory_count": index["directory_count"], "errors": index["errors"]}, ensure_ascii=False))


def command_create_admin_token(config_path: Path) -> None:
    config = load_config(config_path)
    token = secrets.token_urlsafe(32)
    write_secret(Path(config["admin_token_file"]), token)
    print(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/openlist-image-api/config.json", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve")
    subcommands.add_parser("refresh")
    subcommands.add_parser("create-admin-token")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "serve":
        command_serve(args.config)
    elif args.command == "refresh":
        command_refresh(args.config)
    else:
        command_create_admin_token(args.config)


if __name__ == "__main__":
    main()
